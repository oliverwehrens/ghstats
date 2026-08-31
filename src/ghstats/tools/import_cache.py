#!/usr/bin/env python3
"""Load the JSON cache into the SQLite store.

Offline and repeatable: it reads `.cache/` and writes `.cache/ghstats.db`, and
never touches the network. That is the point of doing the migration this way
round -- the expensive network sweep happened once, into a format that already
worked, so the schema can be rewritten and re-imported as many times as it
takes without spending another hour of rate limit.

    ghstats-import-cache --org my-org

Every import starts from an empty database (`--db` is replaced unless
`--keep`), so a partially-imported store is never mistaken for a complete one.
Row counts are verified against the JSON on the way out; a mismatch is an
error, not a warning.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ghstats.store import sqlite as sqlite_store
from ghstats.store.sqlite import canonical_ts

DEFAULT_DB = '.cache/ghstats.db'
DEFAULT_CACHE = '.cache'


def read_curated(db_path: str) -> List[Tuple]:
    """Rescue hand-maintained rows from a database about to be replaced.

    `identities` is auto-seeded but hand-corrected -- that is how the 5971
    commits with no linked GitHub account get attributed to a person. A
    re-import that silently discarded those corrections would make the
    curation worthless, and the loss would be invisible until someone noticed
    a contributor's numbers had quietly dropped again.
    """
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            'SELECT email, canonical_login, is_bot FROM identities').fetchall()
        conn.close()
        return rows
    except sqlite3.Error:
        # An unreadable or older store has nothing worth rescuing.
        return []


def read_repo_file(path: Path) -> Optional[Dict[str, Any]]:
    """Read one repo cache file, or None if it is absent."""
    if not path.exists():
        return None
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def import_org(conn, cache_dir: Path, org: str, quiet: bool = False) -> Dict[str, int]:
    """Import every repository the cache holds for one organization.

    Returns:
        Counts actually inserted, keyed by table name.
    """
    root = cache_dir / 'repos' / org
    if not root.is_dir():
        raise SystemExit(f'Error: no cache for {org!r} under {cache_dir}')

    repos = sorted(p.name for p in root.iterdir() if p.is_dir())
    counts = {'repos': 0, 'coverage': 0, 'commits': 0,
              'pulls': 0, 'reviews': 0, 'missing': 0}

    for index, repo in enumerate(repos, 1):
        if not quiet and index % 100 == 0:
            print(f'    {index}/{len(repos)} repos', file=sys.stderr)

        rid = sqlite_store.repo_id(conn, org, repo)
        counts['repos'] += 1

        commits_payload = read_repo_file(root / repo / 'commits.json')
        pulls_payload = read_repo_file(root / repo / 'pulls.json')
        if commits_payload is None:
            counts['missing'] += 1
        if pulls_payload is None:
            counts['missing'] += 1

        # Coverage first: it is the row that makes the event rows meaningful,
        # and both land in the same transaction as the whole import.
        for kind, payload in (('commits', commits_payload), ('pulls', pulls_payload)):
            if payload is None:
                continue
            conn.execute(
                'INSERT INTO coverage (repo_id, kind, covered_from, covered_to) '
                'VALUES (?, ?, ?, ?)',
                (rid, kind,
                 canonical_ts(payload['covered_from']),
                 canonical_ts(payload['covered_to'])),
            )
            counts['coverage'] += 1

        if commits_payload is not None:
            rows = [
                (rid, c['oid'], c.get('author_login'), c.get('author_name'),
                 c.get('author_email'),
                 canonical_ts(c['committed_date']),
                 canonical_ts(c.get('authored_date')),
                 c.get('additions') or 0, c.get('deletions') or 0,
                 c.get('message') or '')
                for c in commits_payload.get('commits', [])
            ]
            conn.executemany(
                'INSERT INTO commits (repo_id, oid, author_login, author_name, '
                'author_email, committed_date, authored_date, additions, '
                'deletions, message) VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
            counts['commits'] += len(rows)

        if pulls_payload is not None:
            pull_rows: List[Tuple] = []
            review_rows: List[Tuple] = []
            for p in pulls_payload.get('pulls', []):
                pull_rows.append((
                    rid, p['number'], p.get('author_login'),
                    p.get('title') or '', p['state'],
                    canonical_ts(p['created_at']),
                    canonical_ts(p.get('updated_at')),
                    canonical_ts(p.get('merged_at')),
                    canonical_ts(p.get('closed_at')),
                ))
                for r in p.get('reviews') or []:
                    review_rows.append((
                        r['id'], rid, p['number'], r.get('author_login'),
                        canonical_ts(r.get('submitted_at')),
                        r['state'], r.get('body') or '',
                    ))
            conn.executemany(
                'INSERT INTO pulls (repo_id, number, author_login, title, state, '
                'created_at, updated_at, merged_at, closed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)', pull_rows)
            # Reviews after pulls: the foreign key needs its parent present.
            conn.executemany(
                'INSERT INTO reviews (id, repo_id, pull_number, author_login, '
                'submitted_at, state, body) VALUES (?,?,?,?,?,?,?)', review_rows)
            counts['pulls'] += len(pull_rows)
            counts['reviews'] += len(review_rows)

    return counts


def import_members(conn, members_file: Path, seen_at: str) -> int:
    """Seed the members table from the file ghstats-sync exports."""
    if not members_file.exists():
        return 0
    logins = [line.strip() for line in
              members_file.read_text(encoding='utf-8').splitlines() if line.strip()]
    conn.executemany(
        'INSERT OR IGNORE INTO members (login, first_seen, last_seen, active) '
        'VALUES (?, ?, ?, 1)',
        [(login, seen_at, seen_at) for login in logins])
    return len(logins)


def import_sync_state(conn, cache_dir: Path) -> int:
    """Seed sync_runs with the one run sync_state.json remembers."""
    path = cache_dir / 'sync_state.json'
    if not path.exists():
        return 0
    state = json.loads(path.read_text(encoding='utf-8'))
    last_run = canonical_ts(state.get('last_run'))
    if last_run is None:
        return 0
    conn.execute(
        'INSERT INTO sync_runs (started_at, finished_at, repos_synced, '
        'repos_failed, points, seconds) VALUES (?,?,?,?,?,?)',
        (last_run, last_run, state.get('last_run_repos'),
         state.get('last_run_failed'), state.get('last_run_points'),
         state.get('last_run_seconds')))
    return 1


def verify(conn, counts: Dict[str, int]) -> List[str]:
    """Compare what the importer inserted against what the database holds."""
    problems = []
    for table in ('repos', 'commits', 'pulls', 'reviews'):
        actual = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        if actual != counts[table]:
            problems.append(
                f'{table}: inserted {counts[table]}, database holds {actual}')
    orphan_reviews = conn.execute(
        'SELECT COUNT(*) FROM reviews r LEFT JOIN pulls p '
        '  ON p.repo_id = r.repo_id AND p.number = r.pull_number '
        'WHERE p.number IS NULL').fetchone()[0]
    if orphan_reviews:
        problems.append(f'{orphan_reviews} reviews with no parent pull')
    bad_ts = conn.execute(
        "SELECT COUNT(*) FROM commits WHERE committed_date NOT GLOB "
        "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'"
    ).fetchone()[0]
    if bad_ts:
        problems.append(f'{bad_ts} commits with a non-canonical committed_date')
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Import the JSON activity cache into SQLite.')
    parser.add_argument('--org', required=True, help='Organization to import')
    parser.add_argument('--cache-dir', default=DEFAULT_CACHE,
                        help=f'Cache root (default {DEFAULT_CACHE})')
    parser.add_argument('--db', default=DEFAULT_DB,
                        help=f'Database path (default {DEFAULT_DB})')
    parser.add_argument('--keep', action='store_true',
                        help='Import into an existing database instead of '
                             'replacing it. Rarely what you want.')
    parser.add_argument('--discard-curated', action='store_true',
                        help='Do not carry hand-maintained `identities` rows '
                             'over from the database being replaced.')
    parser.add_argument('--quiet', '-q', action='store_true')
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    curated: List[Tuple] = []
    if not args.keep:
        if not args.discard_curated:
            curated = read_curated(args.db)
        for suffix in ('', '-wal', '-shm'):
            path = Path(args.db + suffix)
            if path.exists():
                path.unlink()

    conn = sqlite_store.connect(args.db)
    try:
        with conn:  # one transaction: a failed import leaves no partial store
            if curated:
                conn.executemany(
                    'INSERT OR REPLACE INTO identities '
                    '(email, canonical_login, is_bot) VALUES (?,?,?)', curated)
            counts = import_org(conn, cache_dir, args.org, quiet=args.quiet)
            state_path = cache_dir / 'sync_state.json'
            seen_at = canonical_ts(
                json.loads(state_path.read_text(encoding='utf-8'))['last_run']
            ) if state_path.exists() else '1970-01-01T00:00:00Z'
            members = import_members(conn, Path('members.txt'), seen_at)
            runs = import_sync_state(conn, cache_dir)

        problems = verify(conn, counts)
    finally:
        conn.close()

    size_mb = os.path.getsize(args.db) / 1e6
    print(f'\nImported {args.org} -> {args.db} ({size_mb:.0f} MB)')
    for table in ('repos', 'coverage', 'commits', 'pulls', 'reviews'):
        print(f'  {table:<10} {counts[table]:>8,}')
    print(f'  {"members":<10} {members:>8,}')
    print(f'  {"sync_runs":<10} {runs:>8,}')
    if curated:
        print(f'  {len(curated)} curated identity rows carried over')
    if counts['missing']:
        print(f'  {counts["missing"]} repo/kind pairs had no cache file')

    if problems:
        print('\nVerification FAILED:', file=sys.stderr)
        for p in problems:
            print(f'  {p}', file=sys.stderr)
        return 1
    print('\nVerification passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
