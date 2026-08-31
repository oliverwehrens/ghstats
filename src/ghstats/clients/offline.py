"""Offline activity source: reads the local cache, never the network.

Drop-in replacement for the subset of `GitHubClient` that `ActivityAnalyzer`
uses. All API access now lives in `ghstats.sync`; reporting is a pure function of
whatever the last sync wrote.

Records are exposed as small flat objects rather than dicts so the analyzer's
attribute access keeps working unchanged.
"""
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ghstats.store.json_cache import (
    COMMITS, PULLS, CacheStore, CoverageError, normalize, parse,
)

# The analyzer touches at most four cache files before moving to the next repo
# (one commits read, three pulls reads), so a tiny cache avoids re-parsing
# without holding the whole of the parsed JSON in memory.
_PAYLOAD_CACHE_SIZE = 4


class Repo:
    """Minimal stand-in for a PyGithub Repository."""

    __slots__ = ('name',)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f'Repo({self.name!r})'


class Commit:
    """A cached commit.

    `date` is the git *author* date, matching what the previous REST path
    recorded (`commit.commit.author.date`) and filtered on. That is also the
    right choice for the day-of-week and hour histograms, which are meant to
    show when a person worked rather than when their branch happened to land.
    """

    __slots__ = ('sha', 'date', 'additions', 'deletions', 'author_login',
                 'author_email', 'author_name')

    def __init__(self, record: Dict[str, Any]):
        self.sha = record['oid']
        self.date = parse(record.get('authored_date')
                          or record['committed_date'])
        self.additions = record.get('additions') or 0
        self.deletions = record.get('deletions') or 0
        self.author_login = record.get('author_login')
        self.author_email = record.get('author_email')
        self.author_name = record.get('author_name')


class PullRequest:
    """A cached pull request."""

    __slots__ = ('number', 'author_login', 'title', 'state', 'created_at',
                 'updated_at', 'merged_at', 'merged')

    def __init__(self, record: Dict[str, Any]):
        self.number = record['number']
        self.author_login = record.get('author_login')
        self.title = record.get('title', '')
        self.state = record.get('state')
        self.created_at = parse(record['created_at'])
        self.updated_at = parse(record['updated_at'])
        self.merged_at = parse(record['merged_at']) if record.get('merged_at') else None
        self.merged = self.merged_at is not None


class Review:
    """A cached review, carrying the pull request it belongs to."""

    __slots__ = ('id', 'pr_number', 'author_login', 'state', 'submitted_at')

    def __init__(self, record: Dict[str, Any], pr_number: int):
        self.id = record['id']
        self.pr_number = pr_number
        self.author_login = record.get('author_login')
        self.state = record.get('state') or ''
        self.submitted_at = (parse(record['submitted_at'])
                             if record.get('submitted_at') else None)


def _in_window(moment: Optional[datetime], since: datetime,
               until: datetime) -> bool:
    """Whether a timestamp falls inside an inclusive window."""
    return moment is not None and since <= moment <= until


class OfflineClient:
    """Serves activity data from the cache with no network access."""

    def __init__(self, store: CacheStore, org: str):
        """Initialize the client.

        Args:
            store: Cache store to read from.
            org: Organization name.
        """
        self.store = store
        self.org = org
        self._payloads: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
        self._order: List[Tuple[str, str]] = []
        self.missing_repos: set = set()

    # -- payload access ----------------------------------------------------

    def _payload(self, repo: str, kind: str) -> Optional[Dict[str, Any]]:
        """Load and briefly cache one repo's cache file."""
        key = (repo, kind)
        if key in self._payloads:
            return self._payloads[key]
        try:
            payload = self.store.load(self.org, repo, kind)
        except ValueError as exc:
            # Unreadable or wrong-schema file: treat as absent, but loudly.
            print(f'Warning: {exc}')
            payload = None
        if payload is None:
            self.missing_repos.add(f'{repo}/{kind}')
        self._payloads[key] = payload
        self._order.append(key)
        while len(self._order) > _PAYLOAD_CACHE_SIZE:
            self._payloads.pop(self._order.pop(0), None)
        return payload

    def _items(self, repo: str, kind: str) -> List[Dict[str, Any]]:
        """Return the item list for a repo, or empty if uncached."""
        payload = self._payload(repo, kind)
        return payload.get(kind, []) if payload else []

    # -- repository listing ------------------------------------------------

    def get_organization_repos(self, org_name: str,
                              force_refresh: bool = False) -> List[Repo]:
        """List repositories present in the cache.

        Repos are discovered by directory, not from a live API listing: a repo
        that has since been archived keeps its history, and dropping it would
        retroactively erase past work from everyone's stats.

        Args:
            org_name: Organization name.
            force_refresh: Ignored; kept so the analyzer needs no change.

        Returns:
            List of Repo objects.
        """
        return [Repo(name) for name in self.store.list_repos(org_name)]

    def get_specific_repos(self, org_name: str,
                           repo_names: Iterable[str]) -> List[Repo]:
        """Return the requested repositories that exist in the cache."""
        available = set(self.store.list_repos(org_name))
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
        since, until = normalize(since), normalize(until)
        result = []
        for record in self._items(repo.name, COMMITS):
            if record.get('author_login') != username:
                continue
            commit = Commit(record)
            if _in_window(commit.date, since, until):
                result.append(commit)
        return result

    def get_commit_stats(self, commit: Commit) -> Tuple[int, int]:
        """Additions and deletions for a commit.

        Free here: the sync stored them inline from GraphQL, where the previous
        REST path needed one extra request per commit.
        """
        return commit.additions, commit.deletions

    def get_pull_requests_created(self, repo: Repo, username: str,
                                  since: datetime,
                                  until: datetime) -> List[PullRequest]:
        """Pull requests opened by a user within a window."""
        since, until = normalize(since), normalize(until)
        result = []
        for record in self._items(repo.name, PULLS):
            if record.get('author_login') != username:
                continue
            pull = PullRequest(record)
            if _in_window(pull.created_at, since, until):
                result.append(pull)
        return result

    def get_pull_requests_reviewed(self, repo: Repo, username: str,
                                   since: datetime,
                                   until: datetime) -> List[PullRequest]:
        """Pull requests a user reviewed within a window, deduplicated."""
        since, until = normalize(since), normalize(until)
        result = []
        for record in self._items(repo.name, PULLS):
            for review in record.get('reviews', []):
                if review.get('author_login') != username:
                    continue
                submitted = (parse(review['submitted_at'])
                             if review.get('submitted_at') else None)
                if _in_window(submitted, since, until):
                    result.append(PullRequest(record))
                    break
        return result

    def get_reviews_by_user(self, repo: Repo, username: str, since: datetime,
                            until: datetime) -> List[Review]:
        """Individual reviews submitted by a user within a window."""
        since, until = normalize(since), normalize(until)
        result = []
        for record in self._items(repo.name, PULLS):
            for review in record.get('reviews', []):
                if review.get('author_login') != username:
                    continue
                item = Review(review, record['number'])
                if _in_window(item.submitted_at, since, until):
                    result.append(item)
        return result
