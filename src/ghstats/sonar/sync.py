"""`ghstats-sonar` -- fetch SonarCloud quality gates into the store.

The second network phase, and deliberately its own command rather than a step
inside `ghstats-sync`. The invariant the explorer depends on is that it does no
I/O, not that exactly one program talks to the network:

    ghstats-sync      GitHub      minutes, an API point budget
    ghstats-reindex   offline     derived tables
    ghstats-sonar     SonarCloud  seconds, under ten requests
    ghstats-explore   offline     serves whatever the three wrote

A gate status goes stale in hours and costs almost nothing to refresh. Folding
it into the commit sweep would make that refresh hostage to a job that takes
minutes and real API budget, so it stands alone and can be re-run by itself.

Writes are all-or-nothing: see `SyncStore.replace_sonar_projects`.
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ghstats.sonar import match as matching
from ghstats.sonar import overrides as override_map
from ghstats.sonar.rest import SONARCLOUD_URL, SonarClient, SonarError
from ghstats.store import sqlite as sqlite_store

# How many unmatched repositories to name before summarizing the rest. Long
# enough to show a convention mismatch, short enough not to bury the summary
# on an organization where most repositories have no Sonar project at all.
UNMATCHED_SHOWN = 20


def resolve_token(explicit: Optional[str]) -> Optional[str]:
    """Find a SonarCloud token from the argument or the environment.

    No `gh`-style CLI fallback: there is no Sonar equivalent that is reliably
    installed, and `SONAR_TOKEN` is the name SonarCloud's own CI documentation
    uses, so a machine already running Sonar analysis has it set.
    """
    if explicit:
        return explicit.strip()
    env = os.getenv('SONAR_TOKEN')
    if env and env.strip():
        return env.strip()
    return None


def resolve_org(explicit: Optional[str]) -> Optional[str]:
    """Find the SonarCloud organization key from the argument or environment."""
    if explicit:
        return explicit.strip()
    env = os.getenv('SONAR_ORG')
    if env and env.strip():
        return env.strip()
    return None


def detect_github_org(conn: sqlite3.Connection) -> Optional[str]:
    """The organization the store holds, when it holds exactly one.

    Same courtesy `ghstats-explore` extends: two org flags on one command is
    one more than anyone wants to type, and this one is recoverable.
    """
    rows = [r[0] for r in conn.execute(
        'SELECT DISTINCT org FROM repos ORDER BY org')]
    return rows[0] if len(rows) == 1 else None


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Fetch SonarCloud quality gates for the repositories the '
                    'store already holds.'
    )
    parser.add_argument(
        '--sonar-org', default=None,
        help='SonarCloud organization key -- the one in the URL, not the '
             'display name (or SONAR_ORG). Required.'
    )
    parser.add_argument(
        '--org', default=None,
        help='GitHub organization whose repositories to match. Detected from '
             'the store when it holds exactly one.'
    )
    parser.add_argument('--sonar-token',
                        help='SonarCloud user token (or SONAR_TOKEN)')
    parser.add_argument(
        '--map-file', default=override_map.DEFAULT_MAP_FILE,
        help=f'Hand-written `repo = project-key` overrides for the pairs no '
             f'naming rule reaches (default: {override_map.DEFAULT_MAP_FILE}). '
             f'Absent is fine and means no overrides.'
    )
    parser.add_argument('--db', default=sqlite_store.DEFAULT_DB,
                        help=f'Store path (default: {sqlite_store.DEFAULT_DB})')
    parser.add_argument(
        '--base-url', default=SONARCLOUD_URL,
        help=f'Sonar server root (default: {SONARCLOUD_URL})'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Fetch and report the matching, but write nothing. The way to '
             'check the key convention before committing to it.'
    )
    parser.add_argument('--debug', action='store_true',
                        help='Print each request and its status')
    return parser.parse_args()


def _report(result: matching.MatchResult, projects_found: int,
            repos_total: int, map_file: str = override_map.DEFAULT_MAP_FILE
            ) -> None:
    """Print what matched, what did not, and what would have been assumed."""
    print()
    print(f'Sonar projects: {projects_found}')
    print(f'Repositories:   {repos_total} in the store, '
          f'{len(result.matched)} matched, {len(result.unmatched)} without a '
          f'Sonar project')

    by_rule = {rule: 0 for rule in matching.RULES}
    for match in result.matched:
        by_rule[match.rule] += 1
    if result.matched:
        print('Matched by:     ' + ', '.join(
            f'{rule} {by_rule[rule]}' for rule in matching.RULES
            if by_rule[rule]))

    # Loudly, and before the rest: a line in a hand-written map that quietly
    # does nothing is the one failure nobody goes looking for.
    if result.unknown_overrides:
        print(f'\n{map_file} names {len(result.unknown_overrides)} project(s) '
              f'this organization does not hold. The entry was ignored and the '
              f'repository matched normally:')
        for repo, key in result.unknown_overrides:
            print(f'  {repo}: no such project {key}')

    if result.conflicting_overrides:
        print(f'\n{map_file} maps {len(result.conflicting_overrides)} '
              f'repository(ies) onto a project another entry already claimed. '
              f'One project describes one codebase:')
        for repo, key in result.conflicting_overrides:
            print(f'  {repo}: {key} is already taken')

    if result.ambiguous:
        print(f'\nAmbiguous ({len(result.ambiguous)}) -- precedence chose the '
              f'first:')
        for item in result.ambiguous:
            print(f'  {item.repo}: {item.chosen} '
                  f'(not {", ".join(item.rejected)})')

    if result.unmatched:
        print(f'\nNo Sonar project ({len(result.unmatched)}), with the key a '
              f'convention-based lookup would have used:')
        for repo in result.unmatched[:UNMATCHED_SHOWN]:
            print(f'  {repo}: looked for {result.assumed[repo]}')
        if len(result.unmatched) > UNMATCHED_SHOWN:
            print(f'  ... and {len(result.unmatched) - UNMATCHED_SHOWN} more')

    if result.orphans:
        print(f'\nSonar projects matching no repository in the store '
              f'({len(result.orphans)}):')
        for key in result.orphans[:UNMATCHED_SHOWN]:
            print(f'  {key}')
        if len(result.orphans) > UNMATCHED_SHOWN:
            print(f'  ... and {len(result.orphans) - UNMATCHED_SHOWN} more')


def main():
    """Entry point."""
    args = parse_arguments()

    sonar_org = resolve_org(args.sonar_org)
    if not sonar_org:
        print('Error: no SonarCloud organization (--sonar-org or SONAR_ORG).\n'
              '       This is the key in the URL, e.g. the "acme" in\n'
              '       https://sonarcloud.io/organizations/acme/projects',
              file=sys.stderr)
        return 2

    token = resolve_token(args.sonar_token)
    if not token:
        print('Error: no SonarCloud token (--sonar-token or SONAR_TOKEN)',
              file=sys.stderr)
        return 2

    # Before the network, because a map somebody has just edited is the most
    # likely thing to be wrong and the cheapest thing to check.
    try:
        overrides = override_map.load(args.map_file)
    except (override_map.MapFileError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2

    # `create=False`: this command enriches a store, it does not start one.
    # Conjuring an empty store here would match zero repositories and report
    # "no Sonar project" for an organization nobody has synced yet.
    try:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_store.connect(args.db, create=False)
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f'Error: could not open {args.db}: {exc}', file=sys.stderr)
        return 2

    org = args.org or detect_github_org(conn)
    if not org:
        print('Error: the store holds several organizations; pass --org to '
              'say which one to match against.', file=sys.stderr)
        conn.close()
        return 2

    store = sqlite_store.SyncStore(conn, dry_run=args.dry_run)
    repos: List[str] = store.list_repos(org)
    if not repos:
        print(f'Error: no repositories for {org} in {args.db}. Run '
              f'ghstats-sync first.', file=sys.stderr)
        conn.close()
        return 2

    now = datetime.now(timezone.utc)
    started = time.monotonic()
    client = SonarClient(token, base_url=args.base_url, debug=args.debug)

    print(f'Reading SonarCloud organization {sonar_org} at '
          f'{now.astimezone():%Y-%m-%d %H:%M:%S %Z}')
    if overrides:
        print(f'  {len(overrides)} override(s) from {args.map_file}')
    if args.dry_run:
        print('DRY RUN: nothing will be written')

    # Everything is fetched before anything is written. A failure here leaves
    # the previous snapshot intact rather than half-replacing it.
    try:
        projects = client.projects(sonar_org)
        result = matching.match_projects(repos, projects, sonar_org,
                                         overrides=overrides)
        gates = client.gate_statuses([m.project_key for m in result.matched])
    except SonarError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        print('Nothing was written; the previous snapshot is unchanged.',
              file=sys.stderr)
        conn.close()
        return 1

    rows = matching.rows(result, projects, gates)
    _report(result, len(projects), len(repos), args.map_file)

    gate_counts = {}
    for row in rows:
        if row['repo_name'] and row['gate_status']:
            gate_counts[row['gate_status']] = \
                gate_counts.get(row['gate_status'], 0) + 1
    if gate_counts:
        print('\nQuality gate:   ' + ', '.join(
            f'{status} {count}' for status, count in sorted(gate_counts.items())))

    elapsed = time.monotonic() - started
    try:
        store.replace_sonar_projects(rows, now)
        store.record_sonar_run(
            started_at=now,
            finished_at=datetime.now(timezone.utc),
            sonar_org=sonar_org,
            base_url=client.base_url,
            projects_found=len(projects),
            repos_matched=len(result.matched),
            seconds=round(elapsed, 1),
        )
    except sqlite3.Error as exc:
        print(f'Error: could not write to {args.db}: {exc}', file=sys.stderr)
        conn.close()
        return 1

    print(f'Requests:       {client.request_count}')
    print(f'Elapsed:        {elapsed:.1f}s')
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
