"""Offline activity source backed by SQLite.

Interface-compatible with `offline_client.OfflineClient`, so `ActivityAnalyzer`
and the per-user JSON contract are untouched. The record classes are imported
rather than reimplemented: identical parsing by construction is worth more here
than independence, because Phase 3's whole job is proving the two paths agree.

Where the JSON client reads one repo file per call -- every repo file, once per
user, once per member of the org -- this one warms **three queries per user** and
serves the per-repo calls from memory.

Windows are filtered in Python with the same `_in_window`, not in SQL. The
comparison could be pushed into the query, and eventually should be, but during
the correctness gate the point is to leave no room for a boundary to differ.

**Ordering is load-bearing.** The analyzer only accumulates, so within-repo
order cannot change a count -- but the histogram dicts (`by_hour`,
`by_day_of_week`) take their *key order* from the order records are processed,
and `json.dump` preserves insertion order. The ORDER BY clauses below therefore
reproduce how `cache_store` sorted its JSON: commits by (committed_date, oid),
pulls by number, reviews by (submitted_at, id).
"""
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ghstats.store.json_cache import normalize, parse
from ghstats.clients.offline import Commit, PullRequest, Repo, Review, _in_window

KINDS = ('commits', 'pulls')


def coverage_summary(conn: sqlite3.Connection, org: str) -> Dict[str, Any]:
    """Survey what range the store holds, matching `CacheStore.coverage_summary`.

    `covered_from` is the latest floor across repositories -- the binding
    constraint -- and `covered_to` the earliest ceiling, the staleness edge.
    """
    repos = conn.execute(
        'SELECT COUNT(*) FROM repos WHERE org = ?', (org,)).fetchone()[0]
    row = conn.execute("""
        SELECT MAX(c.covered_from) AS latest_from, MIN(c.covered_to) AS earliest_to
        FROM coverage c JOIN repos r ON r.id = c.repo_id
        WHERE r.org = ?""", (org,)).fetchone()
    missing = [f'{r["name"]}/{r["kind"]}' for r in conn.execute("""
        SELECT r.name, k.kind
        FROM repos r
        CROSS JOIN (SELECT 'commits' AS kind UNION ALL SELECT 'pulls') k
        LEFT JOIN coverage c ON c.repo_id = r.id AND c.kind = k.kind
        WHERE r.org = ? AND c.repo_id IS NULL
        ORDER BY r.name, k.kind""", (org,))]
    return {
        'repos': repos,
        'covered_from': parse(row['latest_from']) if row['latest_from'] else None,
        'covered_to': parse(row['earliest_to']) if row['earliest_to'] else None,
        'missing': missing,
        'unreadable': [],  # a corrupt store fails to open; there is no per-repo case
    }


def load_sync_state(conn: sqlite3.Connection, org: str) -> Dict[str, Any]:
    """Rebuild the sync_state block from `sync_runs` and `members`.

    `members_joined` and `members_left` come back empty: the JSON file recorded
    only the most recent run's churn, and the tables that replace it do not
    carry that history yet. Phase 5, which makes `ghstats.sync` the writer, is where
    it starts accumulating.
    """
    # Prefer the most recent *complete* sweep. A partial run -- `--repos`,
    # `--limit`, any `--skip-*` -- covers a handful of repositories, and
    # reporting its figures as the org's sync status would describe a
    # three-repo retry as though it were the nightly sweep.
    run = conn.execute(
        'SELECT * FROM sync_runs WHERE complete = 1 '
        'ORDER BY started_at DESC LIMIT 1').fetchone()
    if run is None:
        run = conn.execute(
            'SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1').fetchone()
    members = conn.execute(
        'SELECT COUNT(*) FROM members WHERE active = 1').fetchone()[0]
    if run is None:
        return {}
    return {
        'organization': org,
        'last_run': run['finished_at'] or run['started_at'],
        'last_run_repos': run['repos_synced'],
        'last_run_failed': run['repos_failed'],
        'last_run_seconds': run['seconds'],
        'last_run_points': run['points'],
        'members': members,
    }


class SqliteClient:
    """Serves activity data from the SQLite store with no network access."""

    def __init__(self, conn: sqlite3.Connection, org: str):
        self.conn = conn
        self.org = org
        self.missing_repos: set = set()
        self._warmed: Optional[str] = None
        self._commits: Dict[str, List[Dict[str, Any]]] = {}
        self._pulls: Dict[str, List[Dict[str, Any]]] = {}
        self._reviews: Dict[str, List[Dict[str, Any]]] = {}

    # -- warming -----------------------------------------------------------

    def _warm(self, username: str) -> None:
        """Load everything this user did, in three queries.

        Filtering is by author only. The window is applied per call, because
        the analyzer may ask for narrower ranges than the warm covers and
        because keeping the boundary in one place keeps it identical to the
        JSON path.
        """
        if self._warmed == username:
            return

        self._commits, self._pulls, self._reviews = {}, {}, {}

        for row in self.conn.execute("""
            SELECT r.name AS repo, c.oid, c.author_login, c.author_name,
                   c.author_email, c.committed_date, c.authored_date,
                   c.additions, c.deletions
            FROM commits c JOIN repos r ON r.id = c.repo_id
            WHERE r.org = ? AND c.author_login = ?
            ORDER BY r.name, c.committed_date, c.oid""", (self.org, username)):
            self._commits.setdefault(row['repo'], []).append(dict(row))

        for row in self.conn.execute("""
            SELECT r.name AS repo, p.number, p.author_login, p.title, p.state,
                   p.created_at, p.updated_at, p.merged_at
            FROM pulls p JOIN repos r ON r.id = p.repo_id
            WHERE r.org = ? AND p.author_login = ?
            ORDER BY r.name, p.number""", (self.org, username)):
            self._pulls.setdefault(row['repo'], []).append(dict(row))

        # Reviews carry their parent pull, so `get_pull_requests_reviewed` needs
        # no second lookup.
        for row in self.conn.execute("""
            SELECT r.name AS repo, v.id, v.author_login, v.submitted_at,
                   v.state, p.number, p.author_login AS pull_author_login,
                   p.title, p.state AS pull_state, p.created_at, p.updated_at,
                   p.merged_at
            FROM reviews v
            JOIN repos r ON r.id = v.repo_id
            JOIN pulls p ON p.repo_id = v.repo_id AND p.number = v.pull_number
            WHERE r.org = ? AND v.author_login = ?
            ORDER BY r.name, p.number, v.submitted_at, v.id""",
                (self.org, username)):
            self._reviews.setdefault(row['repo'], []).append(dict(row))

        self._warmed = username

    @staticmethod
    def _pull_record(row: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild a pull record from a review row's parent columns."""
        return {
            'number': row['number'],
            'author_login': row['pull_author_login'],
            'title': row['title'],
            'state': row['pull_state'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'merged_at': row['merged_at'],
        }

    # -- repository listing ------------------------------------------------

    def get_organization_repos(self, org_name: str,
                               force_refresh: bool = False) -> List[Repo]:
        """List repositories present in the store, in name order.

        Order matters: it drives the key order of every `by_repo` dict in the
        emitted JSON.
        """
        return [Repo(r['name']) for r in self.conn.execute(
            'SELECT name FROM repos WHERE org = ? ORDER BY name', (org_name,))]

    def get_specific_repos(self, org_name: str,
                           repo_names: Iterable[str]) -> List[Repo]:
        """Return the requested repositories that exist in the store."""
        available = {r['name'] for r in self.conn.execute(
            'SELECT name FROM repos WHERE org = ?', (org_name,))}
        found, absent = [], []
        for name in repo_names:
            (found if name in available else absent).append(name)
        if absent:
            print(f'Warning: not in cache, skipping: {", ".join(sorted(absent))}')
        return [Repo(name) for name in found]

    # -- activity ----------------------------------------------------------

    def get_user_commits(self, repo: Repo, username: str, since: datetime,
                         until: datetime) -> List[Commit]:
        """Commits authored by a user in a repository within a window."""
        self._warm(username)
        since, until = normalize(since), normalize(until)
        result = []
        for record in self._commits.get(repo.name, ()):
            commit = Commit(record)
            if _in_window(commit.date, since, until):
                result.append(commit)
        return result

    def get_commit_stats(self, commit: Commit) -> Tuple[int, int]:
        """Additions and deletions for a commit, stored inline by the sync."""
        return commit.additions, commit.deletions

    def get_pull_requests_created(self, repo: Repo, username: str,
                                  since: datetime,
                                  until: datetime) -> List[PullRequest]:
        """Pull requests opened by a user within a window."""
        self._warm(username)
        since, until = normalize(since), normalize(until)
        result = []
        for record in self._pulls.get(repo.name, ()):
            pull = PullRequest(record)
            if _in_window(pull.created_at, since, until):
                result.append(pull)
        return result

    def get_pull_requests_reviewed(self, repo: Repo, username: str,
                                   since: datetime,
                                   until: datetime) -> List[PullRequest]:
        """Pull requests a user reviewed within a window, deduplicated.

        One entry per pull request, on its first in-window review -- the same
        rule the JSON client expressed with a `break`.
        """
        self._warm(username)
        since, until = normalize(since), normalize(until)
        result, seen = [], set()
        for row in self._reviews.get(repo.name, ()):
            if row['number'] in seen:
                continue
            submitted = parse(row['submitted_at']) if row['submitted_at'] else None
            if _in_window(submitted, since, until):
                seen.add(row['number'])
                result.append(PullRequest(self._pull_record(row)))
        return result

    def get_reviews_by_user(self, repo: Repo, username: str, since: datetime,
                            until: datetime) -> List[Review]:
        """Individual reviews submitted by a user within a window."""
        self._warm(username)
        since, until = normalize(since), normalize(until)
        result = []
        for row in self._reviews.get(repo.name, ()):
            item = Review(row, row['number'])
            if _in_window(item.submitted_at, since, until):
                result.append(item)
        return result
