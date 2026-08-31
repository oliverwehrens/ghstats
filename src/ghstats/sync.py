#!/usr/bin/env python3
"""Incremental sync of GitHub organization activity into the local cache.

Sweeps every active repository in the org and merges what changed into
`.cache/`. Reports are then generated offline from that cache.

Two overlap windows, deliberately not one knob:

  COMMIT_OVERLAP is a correctness parameter. GitHub's `since` filters on a
  commit's own date, not on when it landed on the default branch. Squash and
  rebase merges rewrite the committer date to merge time, so those sync fine,
  but a true merge commit preserves the branch's original dates -- so a June
  branch merged in September is invisible to `since=<September>`, permanently.
  Re-reading the last two weeks closes that hole.

  PR_OVERLAP is just slack. `updated_at` only moves forward, so `since
  last_sync` already catches every pull request change; a day absorbs clock
  skew and partially-failed runs.

On failure nothing is written and no watermark advances, so the next run simply
covers a wider gap. A skipped night is not a data gap.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ghstats.store import sqlite as sqlite_store
from ghstats.store.json_cache import COMMITS, PULLS, iso, normalize, parse
from ghstats.github.graphql import GitHubGraphQL, GraphQLError, GraphQLTransportError

COMMIT_OVERLAP = timedelta(days=14)
PR_OVERLAP = timedelta(days=1)

# Measured ceilings against a 1198-repo org. Two independent limits bite:
#
#   Node count -- 50 repos x 20 PRs x 20 reviews trips RESOURCE_LIMITS_EXCEEDED
#   at ~21k nodes. Repo batch x page size x review page must stay well under it.
#
#   Server CPU -- GitHub applies `since` by walking the commit graph, so a
#   commit query's cost scales with history length, not rows returned. Ten repos
#   is fine over a fortnight and 502s over twenty months; hence --commit-batch.
#
# Page size is the dominant cost on a wide window: at PULL_PAGE 10 a fixed
# five-repo sample took 297 queries, at 50 it took 67 for identical data.
COMMIT_REPO_BATCH = 10
PULL_REPO_BATCH = 5

COMMIT_PAGE = 100
PULL_PAGE = 50

# `reviews(first: N)` returns the OLDEST N, which would systematically miss the
# newest reviews on busy pull requests. `last: N` returns the newest; anything
# deeper than this is paginated separately.
REVIEW_PAGE = 20

_RATE_LIMIT = 'rateLimit { remaining resetAt cost }'

_COMMIT_FIELDS = """
fragment CommitFields on Commit {
  oid
  additions
  deletions
  committedDate
  authoredDate
  message
  author { name email user { login } }
}
"""

_PULL_FIELDS = """
fragment PullFields on PullRequest {
  number
  title
  state
  createdAt
  updatedAt
  mergedAt
  closedAt
  author { login }
  reviews(last: %d) {
    totalCount
    nodes { id state submittedAt body author { login } }
  }
}
""" % REVIEW_PAGE


@dataclass
class RepoOutcome:
    """Per-repository result of one sync pass."""

    repo: str
    ok: bool = True
    commits_added: int = 0
    pulls_added: int = 0
    pulls_updated: int = 0
    reviews_added: int = 0
    errors: List[str] = field(default_factory=list)


def resolve_token(explicit: Optional[str]) -> Optional[str]:
    """Find a GitHub token from the argument, environment, or `gh` CLI."""
    if explicit:
        return explicit.strip()
    env = os.getenv('GITHUB_TOKEN')
    if env and env.strip():
        return env.strip()
    try:
        result = subprocess.run(
            ['gh', 'auth', 'token'], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _commit_record(node: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a GraphQL commit node.

    `author.user` is null for commits whose email is not linked to a GitHub
    account, so name and email are kept as a fallback for later attribution.

    The message is stored whole. It used to be capped at 200 characters, which
    silently amputated the git trailers (`Co-authored-by: ...`) that identify
    which commits were written with an AI assistant -- and did so precisely on
    the long, substantial commits most likely to carry one. Since the merge is
    insert-only, a truncated message is never repaired by a later sync, so any
    cap here is permanent data loss.
    """
    author = node.get('author') or {}
    user = author.get('user') or {}
    return {
        'oid': node['oid'],
        'author_login': user.get('login'),
        'author_name': author.get('name'),
        'author_email': author.get('email'),
        'committed_date': node.get('committedDate'),
        'authored_date': node.get('authoredDate'),
        'additions': node.get('additions') or 0,
        'deletions': node.get('deletions') or 0,
        'message': node.get('message') or '',
    }


def _review_record(node: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a GraphQL review node.

    The body is stored whole, for the same reason as a commit message: a cap
    truncates the substance of exactly the reviews worth measuring, and the
    only way to notice is to compare against GitHub by hand.
    """
    author = node.get('author') or {}
    return {
        'id': node['id'],
        'author_login': author.get('login'),
        'submitted_at': node.get('submittedAt'),
        'state': node.get('state'),
        'body': node.get('body') or '',
    }


def _pull_record(node: Dict[str, Any], reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize a GraphQL pull request node."""
    author = node.get('author') or {}
    return {
        'number': node['number'],
        'author_login': author.get('login'),
        'title': node.get('title') or '',
        'state': node.get('state'),
        'created_at': node.get('createdAt'),
        'updated_at': node.get('updatedAt'),
        'merged_at': node.get('mergedAt'),
        'closed_at': node.get('closedAt'),
        'reviews': reviews,
    }


class Syncer:
    """Drives one sync pass over an organization."""

    def __init__(
        self,
        client: GitHubGraphQL,
        store: sqlite_store.SyncStore,
        org: str,
        *,
        default_from: datetime,
        now: datetime,
        dry_run: bool = False,
        debug: bool = False,
        pull_page: int = PULL_PAGE,
    ):
        """Initialize the syncer.

        Args:
            client: GraphQL client.
            store: Cache store.
            org: Organization login.
            default_from: Coverage floor for repos with no cache yet.
            now: Timestamp this pass will record as `covered_to`.
            dry_run: Fetch and report, but write nothing.
            debug: Verbose per-query output.
            pull_page: Pull requests per page. Small pages mean many round
                trips on a wide window; large ones risk the node ceiling
                when multiplied by the repo batch and nested reviews.
        """
        self.client = client
        self.store = store
        self.org = org
        self.default_from = normalize(default_from)
        self.now = normalize(now)
        self.dry_run = dry_run
        self.debug = debug
        self.pull_page = pull_page

    # -- repository enumeration -------------------------------------------

    def list_active_repos(self) -> List[str]:
        """Enumerate non-archived repositories in the organization."""
        query = """
        query($org: String!, $cursor: String) {
          %s
          organization(login: $org) {
            repositories(first: 100, after: $cursor, isArchived: false,
                         orderBy: {field: PUSHED_AT, direction: DESC}) {
              pageInfo { hasNextPage endCursor }
              nodes { name }
            }
          }
        }
        """ % _RATE_LIMIT

        names: List[str] = []
        cursor = None
        while True:
            data = self.client.query(
                query, {'org': self.org, 'cursor': cursor}, label='repos'
            )
            organization = data.get('organization')
            if not organization:
                raise GraphQLTransportError(f'organization {self.org} not visible')
            connection = organization['repositories']
            names.extend(node['name'] for node in connection['nodes'])
            if not connection['pageInfo']['hasNextPage']:
                return names
            cursor = connection['pageInfo']['endCursor']

    def list_members(self) -> List[str]:
        """Enumerate organization members, sorted.

        Kept in sync so the report cannot silently omit joiners or keep
        producing empty entries for people who have left.
        """
        query = """
        query($org: String!, $cursor: String) {
          %s
          organization(login: $org) {
            membersWithRole(first: 100, after: $cursor) {
              pageInfo { hasNextPage endCursor }
              nodes { login }
            }
          }
        }
        """ % _RATE_LIMIT

        logins: List[str] = []
        cursor = None
        while True:
            data = self.client.query(
                query, {'org': self.org, 'cursor': cursor}, label='members'
            )
            organization = data.get('organization')
            if not organization:
                raise GraphQLTransportError(f'organization {self.org} not visible')
            connection = organization['membersWithRole']
            logins.extend(node['login'] for node in connection['nodes'])
            if not connection['pageInfo']['hasNextPage']:
                return sorted(logins, key=str.lower)
            cursor = connection['pageInfo']['endCursor']

    def list_teams(self) -> List[Dict[str, Any]]:
        """Enumerate teams with their members and repositories.

        Teams are what make "what did this group change on Tuesday" answerable
        without anyone hand-maintaining a roster file. GitHub is the authority,
        so a reorg lands in the explorer on the next sweep.

        **Trap: the inner connections paginate too.** A team with more than 100
        members or 100 repositories returns a first page and a cursor, and
        reading only the first page silently truncates the largest teams --
        exactly the ones whose activity matters most. Any team reporting
        `hasNextPage` is drained with follow-up queries.

        Teams are fetched 20 at a time rather than 100: GraphQL bills nested
        connections multiplicatively, so `teams(100) { members(100) }` is
        priced as 10,000 nodes and can trip the node limit outright.

        Returns:
            Dicts of slug, name, description, parent, members, roles, repos.
        """
        query = """
        query($org: String!, $cursor: String) {
          %s
          organization(login: $org) {
            teams(first: 20, after: $cursor, orderBy: {field: NAME, direction: ASC}) {
              pageInfo { hasNextPage endCursor }
              nodes {
                slug
                name
                description
                parentTeam { slug }
                members(first: 100) {
                  pageInfo { hasNextPage endCursor }
                  edges { role node { login } }
                }
                repositories(first: 100) {
                  pageInfo { hasNextPage endCursor }
                  edges { permission node { name } }
                }
              }
            }
          }
        }
        """ % _RATE_LIMIT

        teams: List[Dict[str, Any]] = []
        cursor = None
        while True:
            data = self.client.query(
                query, {'org': self.org, 'cursor': cursor}, label='teams')
            organization = data.get('organization')
            if not organization:
                raise GraphQLTransportError(f'organization {self.org} not visible')
            connection = organization['teams']
            for node in connection['nodes']:
                teams.append(self._team_record(node))
            if not connection['pageInfo']['hasNextPage']:
                break
            cursor = connection['pageInfo']['endCursor']
        return teams

    def _team_record(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten one team node, draining any truncated inner connection."""
        slug = node['slug']
        members = node.get('members') or {}
        repos = node.get('repositories') or {}

        roles = {edge['node']['login']: edge.get('role')
                 for edge in members.get('edges', [])
                 if edge.get('node')}
        permissions = {edge['node']['name']: edge.get('permission')
                       for edge in repos.get('edges', [])
                       if edge.get('node')}

        page = members.get('pageInfo') or {}
        if page.get('hasNextPage'):
            roles.update(self._drain_team_members(slug, page['endCursor']))
        page = repos.get('pageInfo') or {}
        if page.get('hasNextPage'):
            permissions.update(self._drain_team_repos(slug, page['endCursor']))

        parent = node.get('parentTeam') or {}
        return {
            'slug': slug,
            'name': node.get('name') or slug,
            'description': node.get('description'),
            'parent': parent.get('slug'),
            'members': sorted(roles, key=str.lower),
            'roles': roles,
            'repos': sorted(permissions.items()),
        }

    def _drain_team_members(self, slug: str, cursor: str) -> Dict[str, Any]:
        """Page the rest of one team's roster."""
        query = """
        query($org: String!, $slug: String!, $cursor: String) {
          %s
          organization(login: $org) {
            team(slug: $slug) {
              members(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                edges { role node { login } }
              }
            }
          }
        }
        """ % _RATE_LIMIT
        roles: Dict[str, Any] = {}
        while cursor:
            data = self.client.query(
                query, {'org': self.org, 'slug': slug, 'cursor': cursor},
                label=f'team-members {slug}')
            team = (data.get('organization') or {}).get('team') or {}
            connection = team.get('members') or {}
            for edge in connection.get('edges', []):
                if edge.get('node'):
                    roles[edge['node']['login']] = edge.get('role')
            page = connection.get('pageInfo') or {}
            cursor = page['endCursor'] if page.get('hasNextPage') else None
        return roles

    def _drain_team_repos(self, slug: str, cursor: str) -> Dict[str, Any]:
        """Page the rest of one team's repository grants."""
        query = """
        query($org: String!, $slug: String!, $cursor: String) {
          %s
          organization(login: $org) {
            team(slug: $slug) {
              repositories(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                edges { permission node { name } }
              }
            }
          }
        }
        """ % _RATE_LIMIT
        permissions: Dict[str, Any] = {}
        while cursor:
            data = self.client.query(
                query, {'org': self.org, 'slug': slug, 'cursor': cursor},
                label=f'team-repos {slug}')
            team = (data.get('organization') or {}).get('team') or {}
            connection = team.get('repositories') or {}
            for edge in connection.get('edges', []):
                if edge.get('node'):
                    permissions[edge['node']['name']] = edge.get('permission')
            page = connection.get('pageInfo') or {}
            cursor = page['endCursor'] if page.get('hasNextPage') else None
        return permissions

    # -- window calculation ------------------------------------------------

    def _window(self, repo: str, kind: str, overlap: timedelta
                ) -> Tuple[datetime, datetime]:
        """Work out what to fetch and what coverage to record.

        Returns:
            Tuple of (fetch_since, covered_from).
        """
        coverage = self.store.coverage(self.org, repo, kind)
        if coverage is None:
            return self.default_from, self.default_from
        covered_from, covered_to = coverage
        if self.default_from < covered_from:
            # Explicit backfill: the caller asked for more history than we hold.
            return self.default_from, self.default_from
        return max(covered_to - overlap, covered_from), covered_from

    # -- commits -----------------------------------------------------------

    def _commit_query(self, count: int) -> str:
        """Build an aliased multi-repo commit-history query."""
        decls = ['$org: String!']
        blocks = []
        for index in range(count):
            decls.append(f'$n{index}: String!')
            decls.append(f'$s{index}: GitTimestamp!')
            blocks.append(f"""
          a{index}: repository(owner: $org, name: $n{index}) {{
            defaultBranchRef {{ target {{ ... on Commit {{
              history(first: {COMMIT_PAGE}, since: $s{index}) {{
                pageInfo {{ hasNextPage endCursor }}
                nodes {{ ...CommitFields }}
              }}
            }} }} }}
          }}""")
        return '%s\nquery(%s) {\n  %s\n%s\n}' % (
            _COMMIT_FIELDS, ', '.join(decls), _RATE_LIMIT, ''.join(blocks)
        )

    def _commit_page_query(self) -> str:
        """Build a single-repo paginated commit-history query."""
        return """
        %s
        query($org: String!, $name: String!, $since: GitTimestamp!, $cursor: String) {
          %s
          repository(owner: $org, name: $name) {
            defaultBranchRef { target { ... on Commit {
              history(first: %d, since: $since, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes { ...CommitFields }
              }
            } } }
          }
        }
        """ % (_COMMIT_FIELDS, _RATE_LIMIT, COMMIT_PAGE)

    def _drain_commits(self, repo: str, since: datetime, cursor: Optional[str]
                       ) -> List[Dict[str, Any]]:
        """Follow commit-history pagination for one repo beyond the first page."""
        collected: List[Dict[str, Any]] = []
        query = self._commit_page_query()
        while cursor:
            data = self.client.query(
                query,
                {'org': self.org, 'name': repo, 'since': iso(since), 'cursor': cursor},
                label=f'commits/{repo}',
            )
            branch = (data.get('repository') or {}).get('defaultBranchRef')
            history = ((branch or {}).get('target') or {}).get('history')
            if not history:
                break
            collected.extend(_commit_record(n) for n in history['nodes'])
            page = history['pageInfo']
            cursor = page['endCursor'] if page['hasNextPage'] else None
        return collected

    def sync_commits(self, repos: Sequence[str]) -> Dict[str, RepoOutcome]:
        """Fetch and merge commits for a batch of repositories."""
        outcomes = {repo: RepoOutcome(repo) for repo in repos}
        windows = {repo: self._window(repo, COMMITS, COMMIT_OVERLAP) for repo in repos}

        variables: Dict[str, Any] = {'org': self.org}
        for index, repo in enumerate(repos):
            variables[f'n{index}'] = repo
            variables[f's{index}'] = iso(windows[repo][0])

        data = self.client.query(
            self._commit_query(len(repos)),
            variables,
            label=f'commits[{len(repos)}]',
            # A multi-repo batch has splitting as its recovery path, which beats
            # retrying an oversized query that will time out again.
            max_retries=1 if len(repos) > 1 else None,
        )

        for index, repo in enumerate(repos):
            outcome = outcomes[repo]
            fetch_since, covered_from = windows[repo]
            try:
                node = data.get(f'a{index}')
                if node is None:
                    # Repo vanished or became invisible between enumeration and
                    # fetch. Not an error worth failing the run over.
                    continue
                branch = node.get('defaultBranchRef')
                history = ((branch or {}).get('target') or {}).get('history')
                if history is None:
                    # Empty repository: no default branch. Record coverage so it
                    # is distinguishable from "never fetched".
                    records = []
                else:
                    records = [_commit_record(n) for n in history['nodes']]
                    page = history['pageInfo']
                    if page['hasNextPage']:
                        records.extend(
                            self._drain_commits(repo, fetch_since, page['endCursor'])
                        )

                outcome.commits_added = self.store.merge_commits(
                    self.org, repo, records,
                    covered_from=covered_from, covered_to=self.now,
                )
            except (GraphQLError, GraphQLTransportError, ValueError, IOError) as exc:
                outcome.ok = False
                outcome.errors.append(f'commits: {exc}')

        return outcomes

    # -- pulls and reviews -------------------------------------------------

    def _pull_query(self, count: int) -> str:
        """Build an aliased multi-repo pull-request query."""
        decls = ['$org: String!']
        blocks = []
        for index in range(count):
            decls.append(f'$n{index}: String!')
            blocks.append(f"""
          a{index}: repository(owner: $org, name: $n{index}) {{
            pullRequests(first: {self.pull_page},
                         orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ ...PullFields }}
            }}
          }}""")
        return '%s\nquery(%s) {\n  %s\n%s\n}' % (
            _PULL_FIELDS, ', '.join(decls), _RATE_LIMIT, ''.join(blocks)
        )

    def _pull_page_query(self) -> str:
        """Build a single-repo paginated pull-request query."""
        return """
        %s
        query($org: String!, $name: String!, $cursor: String) {
          %s
          repository(owner: $org, name: $name) {
            pullRequests(first: %d, after: $cursor,
                         orderBy: {field: UPDATED_AT, direction: DESC}) {
              pageInfo { hasNextPage endCursor }
              nodes { ...PullFields }
            }
          }
        }
        """ % (_PULL_FIELDS, _RATE_LIMIT, self.pull_page)

    def _all_reviews(self, repo: str, number: int) -> List[Dict[str, Any]]:
        """Fetch every review on one pull request, oldest first."""
        query = """
        query($org: String!, $name: String!, $number: Int!, $cursor: String) {
          %s
          repository(owner: $org, name: $name) {
            pullRequest(number: $number) {
              reviews(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes { id state submittedAt body author { login } }
              }
            }
          }
        }
        """ % _RATE_LIMIT

        collected: List[Dict[str, Any]] = []
        cursor = None
        while True:
            data = self.client.query(
                query,
                {'org': self.org, 'name': repo, 'number': number, 'cursor': cursor},
                label=f'reviews/{repo}#{number}',
            )
            pull = (data.get('repository') or {}).get('pullRequest')
            if not pull:
                return collected
            connection = pull['reviews']
            collected.extend(_review_record(n) for n in connection['nodes'])
            if not connection['pageInfo']['hasNextPage']:
                return collected
            cursor = connection['pageInfo']['endCursor']

    def _collect_pulls(
        self, repo: str, nodes: List[Dict[str, Any]], cutoff: datetime
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Turn raw pull nodes into records, stopping at the cutoff.

        Returns:
            Tuple of (records, whether pagination should continue).
        """
        records = []
        for node in nodes:
            if parse(node['updatedAt']) < cutoff:
                return records, False
            connection = node.get('reviews') or {}
            if (connection.get('totalCount') or 0) > REVIEW_PAGE:
                reviews = self._all_reviews(repo, node['number'])
            else:
                reviews = [_review_record(n) for n in connection.get('nodes', [])]
            records.append(_pull_record(node, reviews))
        return records, True

    def sync_pulls(self, repos: Sequence[str]) -> Dict[str, RepoOutcome]:
        """Fetch and merge pull requests and their reviews for a batch."""
        outcomes = {repo: RepoOutcome(repo) for repo in repos}
        windows = {repo: self._window(repo, PULLS, PR_OVERLAP) for repo in repos}

        variables: Dict[str, Any] = {'org': self.org}
        for index, repo in enumerate(repos):
            variables[f'n{index}'] = repo

        data = self.client.query(
            self._pull_query(len(repos)), variables, label=f'pulls[{len(repos)}]',
            max_retries=1 if len(repos) > 1 else None,
        )

        page_query = self._pull_page_query()
        for index, repo in enumerate(repos):
            outcome = outcomes[repo]
            cutoff, covered_from = windows[repo]
            try:
                node = data.get(f'a{index}')
                if node is None:
                    continue
                connection = node['pullRequests']
                records, keep_going = self._collect_pulls(
                    repo, connection['nodes'], cutoff
                )
                page = connection['pageInfo']
                cursor = page['endCursor'] if page['hasNextPage'] else None
                while keep_going and cursor:
                    page_data = self.client.query(
                        page_query,
                        {'org': self.org, 'name': repo, 'cursor': cursor},
                        label=f'pulls/{repo}',
                    )
                    repository = page_data.get('repository')
                    if not repository:
                        break
                    connection = repository['pullRequests']
                    more, keep_going = self._collect_pulls(
                        repo, connection['nodes'], cutoff
                    )
                    records.extend(more)
                    page = connection['pageInfo']
                    cursor = page['endCursor'] if page['hasNextPage'] else None

                added, updated, reviews_added = self.store.merge_pulls(
                    self.org, repo, records,
                    covered_from=covered_from, covered_to=self.now,
                )
                outcome.pulls_added = added
                outcome.pulls_updated = updated
                outcome.reviews_added = reviews_added
            except (GraphQLError, GraphQLTransportError, ValueError, IOError) as exc:
                outcome.ok = False
                outcome.errors.append(f'pulls: {exc}')

        return outcomes


def _run_batch(syncer: Syncer, repos: Sequence[str], kind: str
               ) -> Dict[str, RepoOutcome]:
    """Run one batch, falling back to per-repo queries if the batch fails.

    A whole batch failing must not fail every repo in it -- a single oversized
    repo would otherwise poison its nine neighbours.
    """
    method = syncer.sync_commits if kind == COMMITS else syncer.sync_pulls
    try:
        return method(repos)
    except (GraphQLError, GraphQLTransportError) as exc:
        if len(repos) == 1:
            return {repos[0]: RepoOutcome(repos[0], ok=False,
                                          errors=[f'{kind}: {exc}'])}
        print(f'  Batch of {len(repos)} failed ({exc}); retrying individually',
              flush=True)
        outcomes: Dict[str, RepoOutcome] = {}
        for repo in repos:
            outcomes.update(_run_batch(syncer, [repo], kind))
        return outcomes


def write_members(path: str, members: Sequence[str]) -> None:
    """Export the member list as a plain file.

    The `members` table is authoritative; this exists so `report.sh` and any
    other shell tooling can keep reading a line-per-login file without learning
    to query SQLite. Joiners and leavers come from the table, which unlike this
    file remembers them.

    Args:
        path: Destination file, one login per line.
        members: Current organization members.
    """
    target = Path(path)
    directory = target.parent if str(target.parent) else Path('.')
    handle = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', dir=directory,
        prefix=f'.{target.name}.', suffix='.tmp', delete=False,
    )
    try:
        with handle:
            handle.write('\n'.join(members) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _chunks(items: Sequence[str], size: int) -> List[Sequence[str]]:
    """Split a sequence into fixed-size chunks."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _sweep(
    syncer: Syncer,
    repos: Sequence[str],
    kind: str,
    batch_size: int,
    concurrency: int,
) -> Dict[str, RepoOutcome]:
    """Run all batches of one kind, optionally in parallel."""
    batches = _chunks(repos, batch_size)
    merged: Dict[str, RepoOutcome] = {}
    done = 0

    def work(batch):
        return _run_batch(syncer, batch, kind)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for outcomes in pool.map(work, batches):
            merged.update(outcomes)
            done += 1
            print(f'\r  {kind}: {done}/{len(batches)} batches '
                  f'({len(merged)}/{len(repos)} repos)', end='', flush=True)
    print(flush=True)
    return merged


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Incrementally sync GitHub org activity into the local cache.'
    )
    parser.add_argument('--org', required=True, help='GitHub organization name')
    parser.add_argument(
        '--from', dest='since', default=None,
        help='Coverage floor (YYYY-MM-DD). Required the first time a repo is '
             'synced; also triggers a backfill if earlier than current coverage.'
    )
    parser.add_argument('--token', help='GitHub token (or GITHUB_TOKEN, or gh CLI)')
    parser.add_argument('--db', default=sqlite_store.DEFAULT_DB,
                        help=f'Store path (default: {sqlite_store.DEFAULT_DB})')
    parser.add_argument(
        '--concurrency', type=int, default=3,
        help='Parallel GraphQL requests (default: 3). Secondary rate limits '
             'fire on concurrency and server CPU time, not on the point '
             'budget, so wide-window rebuilds need this low.'
    )
    parser.add_argument(
        '--commit-batch', type=int, default=COMMIT_REPO_BATCH,
        help=f'Repositories per commit query (default: {COMMIT_REPO_BATCH}). '
             f'GitHub applies `since` by walking the commit graph, so cost '
             f'scales with history length, not rows returned. Wide windows '
             f'(a from-scratch rebuild) need a smaller batch: try 3.'
    )
    parser.add_argument(
        '--pull-page', type=int, default=PULL_PAGE,
        help=f'Pull requests per page (default: {PULL_PAGE}). Raise it for a\n'
             f'rebuild -- a wide window paginates heavily -- but lower\n'
             f'--pull-batch to stay under the node ceiling.'
    )
    parser.add_argument('--pull-batch', type=int, default=PULL_REPO_BATCH,
                        help=f'Repositories per pull query '
                             f'(default: {PULL_REPO_BATCH})')
    parser.add_argument('--repos', nargs='+',
                        help='Sync only these repositories')
    parser.add_argument('--limit', type=int,
                        help='Sync only the N most recently pushed repositories')
    parser.add_argument(
        '--members-file', default='members.txt',
        help='Where to write the organization member list (default: members.txt)'
    )
    parser.add_argument('--skip-members', action='store_true',
                        help='Skip refreshing the member list')
    parser.add_argument('--skip-teams', action='store_true',
                        help='Skip refreshing teams and their rosters')
    parser.add_argument('--skip-commits', action='store_true',
                        help='Skip the commit sweep')
    parser.add_argument('--skip-pulls', action='store_true',
                        help='Skip the pull request sweep')
    parser.add_argument('--dry-run', action='store_true',
                        help='Fetch and report, but write nothing')
    parser.add_argument('--debug', action='store_true',
                        help='Print per-query debug output')
    return parser.parse_args()


def main():
    """Entry point."""
    args = parse_arguments()

    token = resolve_token(args.token)
    if not token:
        print('Error: no GitHub token (--token, GITHUB_TOKEN, or `gh auth login`)',
              file=sys.stderr)
        return 2

    try:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_store.connect(args.db)
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f'Error: could not open {args.db}: {exc}', file=sys.stderr)
        return 2
    store = sqlite_store.SyncStore(conn, dry_run=args.dry_run)

    default_from = None
    if args.since:
        try:
            default_from = parse(args.since)
        except ValueError as exc:
            print(f'Error: bad --from value: {exc}', file=sys.stderr)
            return 2

    now = datetime.now(timezone.utc)
    client = GitHubGraphQL(token, debug=args.debug)
    started = time.monotonic()

    print(f'Syncing {args.org} at {iso(now)}')
    if args.dry_run:
        print('DRY RUN: no files will be written')

    # Membership first: it is three queries, and knowing who joined or left is
    # useful even if the repository sweep later fails.
    members: List[str] = []
    members_joined: List[str] = []
    members_left: List[str] = []
    if not args.skip_members:
        try:
            members = Syncer(
                client, store, args.org,
                default_from=default_from or now, now=now, debug=args.debug,
            ).list_members()
            if args.dry_run:
                print(f'  Members: {len(members)} (not written, dry run)')
            else:
                # The table is authoritative -- it keeps `first_seen` across a
                # departure and return, and marks leavers inactive rather than
                # forgetting them. The file is an export, so `report.sh` and any
                # shell tooling keep working untouched.
                members_joined, members_left = store.record_members(members, now)
                write_members(args.members_file, members)
                print(f'  Members: {len(members)} -> {args.members_file}')
                if members_joined:
                    print(f'    joined ({len(members_joined)}): '
                          f'{", ".join(members_joined)}')
                if members_left:
                    print(f'    left ({len(members_left)}): '
                          f'{", ".join(members_left)}')
        except (GraphQLError, GraphQLTransportError, IOError, OSError) as exc:
            print(f'Warning: could not refresh member list: {exc}',
                  file=sys.stderr)

    # Teams next, for the same reason: a handful of queries, and the grouping
    # they provide is what turns "what changed on Tuesday" into a question
    # about a group rather than one person at a time.
    if not args.skip_teams:
        try:
            teams = Syncer(
                client, store, args.org,
                default_from=default_from or now, now=now, debug=args.debug,
            ).list_teams()
            people = len({login for t in teams for login in t['members']})
            if args.dry_run:
                print(f'  Teams: {len(teams)} covering {people} members '
                      f'(not written, dry run)')
            else:
                churn = store.record_teams(teams, now)
                print(f'  Teams: {len(teams)} covering {people} members')
                if churn['teams_joined']:
                    print(f'    new ({len(churn["teams_joined"])}): '
                          f'{", ".join(churn["teams_joined"])}')
                if churn['teams_left']:
                    print(f'    gone ({len(churn["teams_left"])}): '
                          f'{", ".join(churn["teams_left"])}')
                for slug, (added, dropped) in sorted(
                        churn['member_changes'].items()):
                    moves = []
                    if added:
                        moves.append('+' + ', +'.join(added))
                    if dropped:
                        moves.append('-' + ', -'.join(dropped))
                    print(f'    {slug}: {"; ".join(moves)}')
        except (GraphQLError, GraphQLTransportError, sqlite3.Error) as exc:
            print(f'Warning: could not refresh teams: {exc}', file=sys.stderr)

    try:
        if args.repos:
            repos = list(args.repos)
            print(f'  Targeting {len(repos)} specified repositories')
        else:
            repos = Syncer(
                client, store, args.org,
                default_from=default_from or now, now=now, debug=args.debug,
            ).list_active_repos()
            print(f'  Found {len(repos)} active repositories')
        if args.limit:
            repos = repos[:args.limit]
            print(f'  Limited to {len(repos)} repositories')
    except (GraphQLError, GraphQLTransportError) as exc:
        print(f'Error: could not enumerate repositories: {exc}', file=sys.stderr)
        return 1

    # A repo with no cache needs an explicit floor; there is nothing to infer.
    if default_from is None:
        uncovered = [
            r for r in repos
            if store.coverage(args.org, r, COMMITS) is None
            or store.coverage(args.org, r, PULLS) is None
        ]
        if uncovered:
            print(
                f'Error: {len(uncovered)} repositories have no coverage on disk '
                f'(e.g. {", ".join(uncovered[:3])}) and --from was not given.\n'
                f'       Pass --from YYYY-MM-DD to set the coverage floor.',
                file=sys.stderr,
            )
            return 2

    syncer = Syncer(
        client, store, args.org,
        default_from=default_from or now, now=now,
        dry_run=args.dry_run, debug=args.debug, pull_page=args.pull_page,
    )

    outcomes: Dict[str, RepoOutcome] = {}

    def absorb(batch_outcomes: Dict[str, RepoOutcome]):
        for repo, outcome in batch_outcomes.items():
            existing = outcomes.get(repo)
            if existing is None:
                outcomes[repo] = outcome
                continue
            existing.ok = existing.ok and outcome.ok
            existing.commits_added += outcome.commits_added
            existing.pulls_added += outcome.pulls_added
            existing.pulls_updated += outcome.pulls_updated
            existing.reviews_added += outcome.reviews_added
            existing.errors.extend(outcome.errors)

    if not args.skip_commits:
        absorb(_sweep(syncer, repos, COMMITS, args.commit_batch, args.concurrency))
    if not args.skip_pulls:
        absorb(_sweep(syncer, repos, PULLS, args.pull_batch, args.concurrency))

    failed = [o for o in outcomes.values() if not o.ok]
    elapsed = time.monotonic() - started

    print()
    print(f'Repositories:  {len(outcomes)} synced, {len(failed)} failed')
    print(f'Commits added: {sum(o.commits_added for o in outcomes.values())}')
    print(f'Pulls:         {sum(o.pulls_added for o in outcomes.values())} added, '
          f'{sum(o.pulls_updated for o in outcomes.values())} updated')
    print(f'Reviews added: {sum(o.reviews_added for o in outcomes.values())}')
    print(f'GraphQL:       {client.query_count} queries, '
          f'{client.points_spent} points, {client.remaining} remaining')
    print(f'Elapsed:       {elapsed / 60:.1f} min')

    if failed:
        print(f'\nFailed repositories ({len(failed)}):', file=sys.stderr)
        for outcome in failed[:20]:
            for message in outcome.errors:
                print(f'  {outcome.repo}: {message}', file=sys.stderr)
        if len(failed) > 20:
            print(f'  ... and {len(failed) - 20} more', file=sys.stderr)
        print('\nWatermarks for failed repositories were NOT advanced; '
              'the next run will cover the gap.', file=sys.stderr)

    # Every run is appended rather than overwriting the last one, so the history
    # the JSON state file discarded every night now accumulates.
    store.record_run(
        started_at=now,
        finished_at=datetime.now(timezone.utc),
        repos_synced=len(outcomes),
        repos_failed=len(failed),
        points=client.points_spent,
        seconds=round(elapsed, 1),
        # A partial sweep must not read as fresh coverage of the whole org.
        complete=not (args.skip_commits or args.skip_pulls or args.skip_members
                      or args.skip_teams or args.repos or args.limit),
    )
    conn.close()

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
