#!/usr/bin/env python3
"""Measure pull requests the store already holds but never sized.

`pulls` records that a pull request existed; `pull_metrics` records how big it
was and how much it was argued about. Everything synced before schema 5 has the
first and not the second, and a normal sync will never close that gap: it pages
`pullRequests` by `updatedAt` and stops at the watermark, so a pull request
nobody has touched since is never fetched again. This is the one-off that
measures them.

    ghstats-backfill-pulls --org ORG

**Why a separate command and not part of the sync.** The cost is proportional to
history rather than to what changed, which is the opposite of every other thing
`ghstats-sync` does; folding it in would make an unattended nightly run
occasionally spend an hour of API budget. Spending that is a decision, so it
gets a command.

Safe to interrupt and re-run. Each batch commits on its own and the next run
re-derives what is still missing from the store, so nothing is refetched and no
progress file has to be trusted. `--limit` exists to spend a fixed amount of
budget per run.

**It never advances a coverage watermark.** A backfill measures what was already
collected; claiming coverage from it would assert the sync had fetched a window
it had not.
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence, Tuple

from ghstats.github.graphql import GitHubGraphQL, GraphQLError, GraphQLTransportError
from ghstats.store import sqlite as sqlite_store
from ghstats.sync import _RATE_LIMIT, _pull_metrics, resolve_token

# Pull requests per query. Each node carries a `reviews(last:)` connection, so
# the node budget is PULL_BATCH x REVIEW_BATCH plus the nodes themselves; these
# two multiply exactly the way the sync's own batch sizes do. 25 x 20 = 500
# leaves an order of magnitude of headroom under the limit that bites at ~21k.
PULL_BATCH = 25
REVIEW_BATCH = 20


def _query(count: int) -> str:
    """Build an aliased multi-pull query against one repository.

    Aliased siblings rather than a `pullRequests` page, because the numbers
    being backfilled are scattered through history -- paging to them would
    refetch everything in between, which is the cost this command exists to
    avoid.
    """
    decls = ['$org: String!', '$name: String!']
    blocks = []
    for index in range(count):
        decls.append(f'$p{index}: Int!')
        blocks.append(f"""
      p{index}: pullRequest(number: $p{index}) {{ ...Metrics }}""")
    return """
fragment Metrics on PullRequest {
  number
  additions
  deletions
  changedFiles
  comments { totalCount }
  reviews(last: %d) {
    totalCount
    nodes { comments { totalCount } }
  }
}
query(%s) {
  %s
  repository(owner: $org, name: $name) {
%s
  }
}
""" % (REVIEW_BATCH, ', '.join(decls), _RATE_LIMIT, ''.join(blocks))


def _deep_review_comments(client: GitHubGraphQL, org: str, repo: str,
                          number: int) -> int:
    """Total inline comments across every review on one pull request.

    Only called when a pull request has more reviews than one page. The count
    would otherwise be summed over the newest `REVIEW_BATCH` alone, which
    undercounts discussion on precisely the most-reviewed pull requests -- the
    ones a chart about discussion volume is about.
    """
    query = """
    query($org: String!, $name: String!, $number: Int!, $cursor: String) {
      %s
      repository(owner: $org, name: $name) {
        pullRequest(number: $number) {
          reviews(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes { comments { totalCount } }
          }
        }
      }
    }
    """ % _RATE_LIMIT

    total, cursor = 0, None
    while True:
        data = client.query(
            query,
            {'org': org, 'name': repo, 'number': number, 'cursor': cursor},
            label=f'backfill-reviews/{repo}#{number}',
        )
        pull = (data.get('repository') or {}).get('pullRequest')
        if not pull:
            return total
        connection = pull['reviews']
        total += sum((n.get('comments') or {}).get('totalCount') or 0
                     for n in connection['nodes'])
        if not connection['pageInfo']['hasNextPage']:
            return total
        cursor = connection['pageInfo']['endCursor']


def _measure(client: GitHubGraphQL, org: str, repo: str,
             numbers: Sequence[int]) -> List[Dict[str, Any]]:
    """Fetch metrics for a batch of pull request numbers in one repository."""
    variables: Dict[str, Any] = {'org': org, 'name': repo}
    for index, number in enumerate(numbers):
        variables[f'p{index}'] = number

    data = client.query(_query(len(numbers)), variables,
                        label=f'backfill/{repo}[{len(numbers)}]')
    repository = data.get('repository') or {}

    records = []
    for index, number in enumerate(numbers):
        node = repository.get(f'p{index}')
        if node is None:
            # Deleted, transferred, or in a repo that went private between the
            # sync that recorded it and now. Not an error: it stays unmeasured
            # and the next run will try it again.
            continue
        connection = node.get('reviews') or {}
        reviews = [_review_record_stub(n) for n in connection.get('nodes', [])]
        if (connection.get('totalCount') or 0) > REVIEW_BATCH:
            record = _pull_metrics(node, [])
            record['review_comments'] = _deep_review_comments(
                client, org, repo, number)
        else:
            record = _pull_metrics(node, reviews)
        record['number'] = node['number']
        records.append(record)
    return records


def _review_record_stub(node: Dict[str, Any]) -> Dict[str, Any]:
    """Just the comment count `_pull_metrics` sums over.

    The backfill does not refetch review bodies -- they are already in the
    store, and fetching them again would multiply the cost of this command by
    the size of the text it would throw away.
    """
    return {'comments': (node.get('comments') or {}).get('totalCount') or 0}


def _batches(items: Sequence[Any], size: int) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _group(pairs: Sequence[Tuple[str, int]]) -> Dict[str, List[int]]:
    """Pull numbers per repository, preserving the newest-first order."""
    grouped: Dict[str, List[int]] = {}
    for repo, number in pairs:
        grouped.setdefault(repo, []).append(number)
    return grouped


def parse_arguments():
    parser = argparse.ArgumentParser(
        description='Measure size and discussion for pull requests already in '
                    'the store but never sized.')
    parser.add_argument('--org', required=True, help='GitHub organization name')
    parser.add_argument('--db', default=sqlite_store.DEFAULT_DB,
                        help=f'Store path (default: {sqlite_store.DEFAULT_DB})')
    parser.add_argument('--token', help='GitHub token (or GITHUB_TOKEN, or gh CLI)')
    parser.add_argument('--repo', help='Restrict to one repository')
    parser.add_argument(
        '--limit', type=int, default=None,
        help='Stop after this many pull requests. Newest first, so a capped '
             'run covers the recent end of history.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what is unmeasured and exit without fetching')
    parser.add_argument('--debug', action='store_true')
    return parser.parse_args()


def main():
    """Entry point."""
    args = parse_arguments()

    try:
        conn = sqlite_store.connect(args.db, create=False)
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f'Error: could not open {args.db}: {exc}', file=sys.stderr)
        return 2
    store = sqlite_store.SyncStore(conn)

    pending = store.unmeasured_pulls(args.org, repo=args.repo, limit=args.limit)
    if not pending:
        print('Nothing to backfill: every pull request in the store is measured.')
        return 0

    grouped = _group(pending)
    print(f'{len(pending)} unmeasured pull requests across {len(grouped)} '
          f'repositories')
    if args.dry_run:
        for repo, numbers in sorted(grouped.items(),
                                    key=lambda kv: -len(kv[1])):
            print(f'  {repo}: {len(numbers)}')
        print('DRY RUN: nothing fetched')
        return 0

    token = resolve_token(args.token)
    if not token:
        print('Error: no GitHub token (--token, GITHUB_TOKEN, or `gh auth login`)',
              file=sys.stderr)
        return 2
    client = GitHubGraphQL(token, debug=args.debug)

    written = failed = 0
    for repo, numbers in grouped.items():
        for batch in _batches(numbers, PULL_BATCH):
            try:
                records = _measure(client, args.org, repo, batch)
            except (GraphQLError, GraphQLTransportError) as exc:
                # One bad batch must not end the run: the remaining repositories
                # are independent, and a re-run picks up whatever is still
                # missing. Reported rather than swallowed.
                failed += len(batch)
                print(f'  {repo}: batch of {len(batch)} failed ({exc})',
                      file=sys.stderr)
                continue
            written += store.merge_pull_metrics(
                args.org, repo, records, datetime.now(timezone.utc))
        print(f'  {repo}: {len(numbers)} requested', flush=True)

    print(f'Measured {written} pull requests'
          + (f', {failed} failed' if failed else ''))
    remaining = len(store.unmeasured_pulls(args.org))
    if remaining:
        print(f'{remaining} still unmeasured; re-run to continue.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
