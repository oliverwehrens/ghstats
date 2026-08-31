#!/usr/bin/env python3
"""Live check that COMMIT_OVERLAP recovers commits a plain watermark would miss.

Simulates the failure the overlap exists for: a merge commit whose own date
predates the last sync, landing on the default branch after it. Such a commit is
invisible to `since=<last sync>` and would be lost permanently.

    1. Sync a repo for real.
    2. Rewind covered_to to T and delete commits dated inside [T-14d, T]
       (what a late merge would have added) and one dated before T-14d.
    3. Re-sync.
    4. In-window commits must come back. The out-of-window one must not --
       that is the boundary, and it is why the constant is 14 days and not 1.

Usage: ghstats-verify-overlap --org ORG <repo> [--cache-dir DIR]

NOTE: this predates the SQLite store. It still reads the JSON cache and passes
`--cache-dir` to the sync, which now takes `--db`, so it needs updating before
it will run again.
"""
import argparse
import subprocess
import sys
from datetime import timedelta

from ghstats.store.json_cache import CacheStore, COMMITS, iso, parse
from ghstats.sync import COMMIT_OVERLAP

PYTHON = sys.executable


def run_sync(org: str, cache_dir: str, repo: str, since: str):
    """Invoke the sync for a single repository."""
    result = subprocess.run(
        [PYTHON, '-m', 'ghstats.sync', '--org', org, '--from', since,
         '--cache-dir', cache_dir, '--repos', repo, '--skip-pulls'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f'sync failed for {repo}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('repo')
    parser.add_argument('--org', required=True,
                        help='Organization to sync the repository from')
    parser.add_argument('--cache-dir', default='/tmp/ghverify')
    parser.add_argument('--since', default='2026-05-01')
    args = parser.parse_args()

    store = CacheStore(cache_dir=args.cache_dir)

    print(f'1. Baseline sync of {args.repo} from {args.since}')
    run_sync(args.org, args.cache_dir, args.repo, args.since)
    payload = store.load(args.org, args.repo, COMMITS)
    baseline = {c['oid'] for c in payload[COMMITS]}
    print(f'   {len(baseline)} commits cached, '
          f'covered {payload["covered_from"][:10]} -> {payload["covered_to"][:10]}')

    if len(baseline) < 5:
        raise SystemExit(f'{args.repo} has too few commits for this check')

    dated = sorted(payload[COMMITS], key=lambda c: c['committed_date'])
    newest = parse(dated[-1]['committed_date'])

    # Put T just past the newest commit so the whole overlap window has content.
    pivot = newest + timedelta(hours=1)
    floor = pivot - COMMIT_OVERLAP

    in_window = [c for c in dated if floor <= parse(c['committed_date']) <= pivot]
    out_window = [c for c in dated if parse(c['committed_date']) < floor]

    if not in_window or not out_window:
        raise SystemExit(
            f'{args.repo}: need commits on both sides of {iso(floor)} '
            f'(in={len(in_window)}, out={len(out_window)}); try another repo'
        )

    victim = out_window[-1]
    removed_in = {c['oid'] for c in in_window}
    removed_out = victim['oid']

    print(f'2. Rewinding covered_to to {iso(pivot)} (overlap floor {iso(floor)})')
    print(f'   deleting {len(removed_in)} commits inside the window')
    print(f'   deleting 1 commit outside it ({removed_out[:8]}, '
          f'{victim["committed_date"][:10]})')

    kept = [c for c in payload[COMMITS]
            if c['oid'] not in removed_in and c['oid'] != removed_out]
    payload[COMMITS] = kept
    payload['covered_to'] = iso(pivot)
    store.save(args.org, args.repo, COMMITS, payload)

    print('3. Re-syncing')
    run_sync(args.org, args.cache_dir, args.repo, args.since)
    after = {c['oid'] for c in store.load(args.org, args.repo, COMMITS)[COMMITS]}

    recovered = removed_in & after
    missing = removed_in - after
    resurrected = removed_out in after

    print('\n4. Result')
    print(f'   in-window recovered:  {len(recovered)}/{len(removed_in)}')
    print(f'   out-of-window commit: {"recovered" if resurrected else "still absent"}'
          f'  (expected: still absent)')

    ok = True
    if missing:
        ok = False
        print(f'   FAIL: {len(missing)} in-window commits were not recovered')
    if resurrected:
        ok = False
        print('   FAIL: a commit outside the overlap came back; the window '
              'is not bounding the fetch')

    print('\nPASS: the 14-day overlap recovers late-landing commits, and '
          'bounds the refetch.' if ok else '\nFAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
