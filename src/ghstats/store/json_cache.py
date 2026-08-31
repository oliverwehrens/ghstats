"""Append-mostly cache for GitHub activity data.

Two files per repository, each carrying the range it actually covers:

    .cache/sync_state.json
    .cache/repos/<org>/<repo>/commits.json
    .cache/repos/<org>/<repo>/pulls.json

`covered_from` / `covered_to` are the point of the design. A bare "last fetched"
watermark records when data was collected but never what range it holds, so a
report asking for a wider range gets a silent undercount. Storing both lets the
reader refuse rather than under-report.

Merge rules:
    commits  keyed on oid     insert-only, never overwritten
    pulls    keyed on number  upsert (state and merged_at must be refreshed)
    reviews  keyed on id      upsert, unioned into their pull

Nothing is ever removed.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

# Relative to the working directory, as `store.sqlite.DEFAULT_DB` is. It used to
# be resolved beside this file, which stopped meaning the project root once the
# module moved into a package.
DEFAULT_CACHE_DIR = '.cache'

COMMITS = 'commits'
PULLS = 'pulls'


class CoverageError(Exception):
    """A read asked for a range the cache does not hold."""


def iso(dt: datetime) -> str:
    """Serialize a datetime as a UTC ISO-8601 string."""
    return normalize(dt).isoformat()


def normalize(dt: datetime) -> datetime:
    """Coerce a datetime to UTC, treating naive input as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware UTC datetime."""
    return normalize(datetime.fromisoformat(value.replace('Z', '+00:00')))


def _envelope(org: str, repo: str, kind: str, covered_from: datetime,
              covered_to: datetime, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a cache file payload."""
    return {
        'schema_version': SCHEMA_VERSION,
        'organization': org,
        'repository': repo,
        'covered_from': iso(covered_from),
        'covered_to': iso(covered_to),
        kind: items,
    }


def merge_commits(
    existing: Optional[Dict[str, Any]],
    new_commits: List[Dict[str, Any]],
    *,
    org: str,
    repo: str,
    covered_from: datetime,
    covered_to: datetime,
) -> Tuple[Dict[str, Any], int]:
    """Insert-only merge of commits, keyed on oid.

    Args:
        existing: Previously cached payload, or None.
        new_commits: Freshly fetched commit records.
        org: Organization name.
        repo: Repository name.
        covered_from: Lower bound of the merged coverage window.
        covered_to: Upper bound of the merged coverage window.

    Returns:
        Tuple of (payload, number of commits added).
    """
    merged = list((existing or {}).get(COMMITS, []))
    seen = {c['oid'] for c in merged}
    added = 0
    for commit in new_commits:
        if commit['oid'] in seen:
            continue
        merged.append(commit)
        seen.add(commit['oid'])
        added += 1
    merged.sort(key=lambda c: (c.get('committed_date') or '', c['oid']))
    return _envelope(org, repo, COMMITS, covered_from, covered_to, merged), added


def merge_pulls(
    existing: Optional[Dict[str, Any]],
    new_pulls: List[Dict[str, Any]],
    *,
    org: str,
    repo: str,
    covered_from: datetime,
    covered_to: datetime,
) -> Tuple[Dict[str, Any], int, int, int]:
    """Upsert pulls by number and reviews by id.

    A pull fetched now may carry fewer reviews than the cache already holds (the
    sync requests only the newest N), so reviews are unioned rather than replaced.

    Args:
        existing: Previously cached payload, or None.
        new_pulls: Freshly fetched pull records, each with a `reviews` list.
        org: Organization name.
        repo: Repository name.
        covered_from: Lower bound of the merged coverage window.
        covered_to: Upper bound of the merged coverage window.

    Returns:
        Tuple of (payload, pulls added, pulls updated, reviews added).
    """
    by_number = {p['number']: p for p in (existing or {}).get(PULLS, [])}
    added = updated = reviews_added = 0

    for pull in new_pulls:
        cached = by_number.get(pull['number'])
        if cached is None:
            record = dict(pull)
            record['reviews'] = sorted(
                pull.get('reviews', []),
                key=lambda r: (r.get('submitted_at') or '', str(r.get('id'))),
            )
            by_number[pull['number']] = record
            added += 1
            reviews_added += len(record['reviews'])
            continue

        reviews = {r['id']: r for r in cached.get('reviews', [])}
        before = len(reviews)
        for review in pull.get('reviews', []):
            reviews[review['id']] = review
        reviews_added += len(reviews) - before

        record = dict(pull)
        record['reviews'] = sorted(
            reviews.values(),
            key=lambda r: (r.get('submitted_at') or '', str(r.get('id'))),
        )
        by_number[pull['number']] = record
        updated += 1

    merged = sorted(by_number.values(), key=lambda p: p['number'])
    payload = _envelope(org, repo, PULLS, covered_from, covered_to, merged)
    return payload, added, updated, reviews_added


class CacheStore:
    """Filesystem access to the activity cache."""

    def __init__(self, cache_dir: Optional[str] = None):
        """Initialize the store.

        Args:
            cache_dir: Cache root (default: `.cache` under the working
                directory, matching `store.sqlite.DEFAULT_DB`).
        """
        self.cache_dir = Path(cache_dir) if cache_dir else Path(DEFAULT_CACHE_DIR)

    def repo_dir(self, org: str, repo: str) -> Path:
        """Path to a repository's cache directory."""
        safe_org = org.replace('/', '_').replace('\\', '_')
        safe_repo = repo.replace('/', '_').replace('\\', '_')
        return self.cache_dir / 'repos' / safe_org / safe_repo

    def path(self, org: str, repo: str, kind: str) -> Path:
        """Path to one cache file."""
        return self.repo_dir(org, repo) / f'{kind}.json'

    def load(self, org: str, repo: str, kind: str) -> Optional[Dict[str, Any]]:
        """Read a cache file.

        Returns:
            The payload, or None if absent.

        Raises:
            ValueError: The file exists but is unreadable or of a future schema.
        """
        target = self.path(org, repo, kind)
        if not target.exists():
            return None
        try:
            with open(target, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, IOError) as exc:
            raise ValueError(f'Unreadable cache file {target}: {exc}') from exc

        version = payload.get('schema_version')
        if version != SCHEMA_VERSION:
            raise ValueError(
                f'{target}: schema_version {version!r}, expected {SCHEMA_VERSION}'
            )
        return payload

    def save(self, org: str, repo: str, kind: str, payload: Dict[str, Any]):
        """Write a cache file atomically.

        A torn write would leave a repo's coverage window disagreeing with its
        contents permanently, since nothing expires.
        """
        directory = self.repo_dir(org, repo)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{kind}.json'
        handle = tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=directory,
            prefix=f'.{kind}.', suffix='.tmp', delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, target)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def coverage(self, org: str, repo: str, kind: str
                 ) -> Optional[Tuple[datetime, datetime]]:
        """Return (covered_from, covered_to) for a cache file, or None."""
        payload = self.load(org, repo, kind)
        if payload is None:
            return None
        return parse(payload['covered_from']), parse(payload['covered_to'])

    def list_repos(self, org: str) -> List[str]:
        """List repositories present in the cache for an org.

        Repos are discovered by directory rather than from a repo-list file: a
        repo that has since been archived keeps its cached history, so the live
        active-repo list is not the right source for readers.
        """
        root = self.cache_dir / 'repos' / org.replace('/', '_').replace('\\', '_')
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def read_items(
        self,
        org: str,
        repo: str,
        kind: str,
        since: datetime,
        until: datetime,
    ) -> Tuple[List[Dict[str, Any]], datetime]:
        """Read cached items, enforcing the coverage window.

        The lower bound is a hard error: asking for data older than the cache
        holds is exactly the silent-undercount case this design exists to stop.
        The upper bound is clamped instead, because `--until now` is always
        slightly beyond the last sync; the effective bound is returned so the
        caller can surface it.

        Args:
            org: Organization name.
            repo: Repository name.
            kind: `commits` or `pulls`.
            since: Requested lower bound.
            until: Requested upper bound.

        Returns:
            Tuple of (items, effective_until).

        Raises:
            CoverageError: No cache for this repo, or `since` predates coverage.
        """
        payload = self.load(org, repo, kind)
        if payload is None:
            raise CoverageError(f'{org}/{repo}: no {kind} cache; run ghstats-sync first')

        covered_from = parse(payload['covered_from'])
        covered_to = parse(payload['covered_to'])
        since = normalize(since)
        until = normalize(until)

        if since < covered_from:
            raise CoverageError(
                f'{org}/{repo}: requested since {iso(since)} predates '
                f'covered_from {iso(covered_from)}; re-run ghstats-sync with '
                f'--from {since.date()}'
            )
        return payload.get(kind, []), min(until, covered_to)

    def coverage_summary(self, org: str) -> Dict[str, Any]:
        """Survey what range the whole cache actually holds.

        Reads every repo file once (~0.6s for 1198 repos) so a report can check
        its window up front rather than discovering a shortfall per repo, or
        worse, silently under-reporting.

        Args:
            org: Organization name.

        Returns:
            Dict with `repos`, `covered_from` (the latest floor across repos --
            the binding constraint), `covered_to` (the earliest ceiling, i.e.
            the staleness edge), `missing` (repo/kind pairs with no file), and
            `unreadable`.
        """
        latest_from: Optional[datetime] = None
        earliest_to: Optional[datetime] = None
        missing: List[str] = []
        unreadable: List[str] = []
        repos = self.list_repos(org)

        for repo in repos:
            for kind in (COMMITS, PULLS):
                try:
                    payload = self.load(org, repo, kind)
                except ValueError as exc:
                    unreadable.append(f'{repo}/{kind}: {exc}')
                    continue
                if payload is None:
                    missing.append(f'{repo}/{kind}')
                    continue
                covered_from = parse(payload['covered_from'])
                covered_to = parse(payload['covered_to'])
                if latest_from is None or covered_from > latest_from:
                    latest_from = covered_from
                if earliest_to is None or covered_to < earliest_to:
                    earliest_to = covered_to

        return {
            'repos': len(repos),
            'covered_from': latest_from,
            'covered_to': earliest_to,
            'missing': missing,
            'unreadable': unreadable,
        }

    def load_sync_state(self) -> Dict[str, Any]:
        """Read `sync_state.json`, or an empty dict if absent."""
        target = self.cache_dir / 'sync_state.json'
        if not target.exists():
            return {}
        try:
            with open(target, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except (json.JSONDecodeError, IOError):
            return {}

    def save_sync_state(self, state: Dict[str, Any]):
        """Write `sync_state.json` atomically."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=self.cache_dir,
            prefix='.sync_state.', suffix='.tmp', delete=False,
        )
        try:
            with handle:
                json.dump(state, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.cache_dir / 'sync_state.json')
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
