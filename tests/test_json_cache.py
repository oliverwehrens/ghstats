"""Tests for the cache merge rules and coverage guard.

Run: python -m unittest tests.test_json_cache -v
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from ghstats.store import json_cache as cache_store
from ghstats.store.json_cache import CacheStore, COMMITS, PULLS, CoverageError, SCHEMA_VERSION

FROM = datetime(2025, 1, 1, tzinfo=timezone.utc)
TO = datetime(2026, 8, 17, tzinfo=timezone.utc)


def commit(oid, date='2026-01-01T00:00:00Z', additions=1):
    return {'oid': oid, 'committed_date': date, 'additions': additions,
            'deletions': 0, 'author_login': 'someone', 'author_name': 'S',
            'author_email': 's@x', 'authored_date': date, 'message': 'm'}


def review(rid, state='APPROVED', submitted='2026-01-02T00:00:00Z'):
    return {'id': rid, 'author_login': 'r', 'submitted_at': submitted,
            'state': state, 'body': ''}


def pull(number, state='OPEN', merged_at=None, reviews=None):
    return {'number': number, 'author_login': 'a', 'title': 't', 'state': state,
            'created_at': '2026-01-01T00:00:00Z',
            'updated_at': '2026-01-02T00:00:00Z',
            'merged_at': merged_at, 'closed_at': None,
            'reviews': reviews if reviews is not None else []}


def merge_commits(existing, new):
    return cache_store.merge_commits(existing, new, org='o', repo='r',
                                     covered_from=FROM, covered_to=TO)


def merge_pulls(existing, new):
    return cache_store.merge_pulls(existing, new, org='o', repo='r',
                                   covered_from=FROM, covered_to=TO)


class CommitMergeTest(unittest.TestCase):
    """Commits are insert-only, keyed on oid."""

    def test_inserts_new(self):
        payload, added = merge_commits(None, [commit('a'), commit('b')])
        self.assertEqual(added, 2)
        self.assertEqual({c['oid'] for c in payload[COMMITS]}, {'a', 'b'})
        self.assertEqual(payload['schema_version'], SCHEMA_VERSION)

    def test_deduplicates_across_syncs(self):
        first, _ = merge_commits(None, [commit('a')])
        second, added = merge_commits(first, [commit('a'), commit('b')])
        self.assertEqual(added, 1)
        self.assertEqual(len(second[COMMITS]), 2)

    def test_never_overwrites(self):
        """A re-fetched commit must not clobber the stored copy."""
        first, _ = merge_commits(None, [commit('a', additions=10)])
        second, added = merge_commits(first, [commit('a', additions=999)])
        self.assertEqual(added, 0)
        self.assertEqual(second[COMMITS][0]['additions'], 10)

    def test_never_deletes(self):
        """Commits absent from a later fetch survive."""
        first, _ = merge_commits(None, [commit('a'), commit('b')])
        second, _ = merge_commits(first, [commit('c')])
        self.assertEqual({c['oid'] for c in second[COMMITS]}, {'a', 'b', 'c'})

    def test_sorted_by_date(self):
        payload, _ = merge_commits(None, [
            commit('late', '2026-06-01T00:00:00Z'),
            commit('early', '2026-01-01T00:00:00Z'),
        ])
        self.assertEqual([c['oid'] for c in payload[COMMITS]], ['early', 'late'])


class PullMergeTest(unittest.TestCase):
    """Pulls upsert by number; reviews union by id."""

    def test_inserts_new(self):
        payload, added, updated, reviews_added = merge_pulls(
            None, [pull(1, reviews=[review('r1')])])
        self.assertEqual((added, updated, reviews_added), (1, 0, 1))

    def test_upserts_state_and_merged_at(self):
        """A PR open at first sync must reflect its merge later."""
        first, *_ = merge_pulls(None, [pull(1, state='OPEN')])
        second, added, updated, _ = merge_pulls(
            first, [pull(1, state='MERGED', merged_at='2026-08-01T00:00:00Z')])
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(second[PULLS][0]['state'], 'MERGED')
        self.assertEqual(second[PULLS][0]['merged_at'], '2026-08-01T00:00:00Z')

    def test_reviews_union_not_replace(self):
        """A fetch returning only the newest reviews must not drop older ones."""
        first, *_ = merge_pulls(None, [pull(1, reviews=[review('r1'), review('r2')])])
        second, _, _, reviews_added = merge_pulls(
            first, [pull(1, reviews=[review('r3')])])
        self.assertEqual(reviews_added, 1)
        self.assertEqual({r['id'] for r in second[PULLS][0]['reviews']},
                         {'r1', 'r2', 'r3'})

    def test_review_upserted_by_id(self):
        """A dismissed review updates in place rather than duplicating."""
        first, *_ = merge_pulls(None, [pull(1, reviews=[review('r1', 'APPROVED')])])
        second, *_ = merge_pulls(first, [pull(1, reviews=[review('r1', 'DISMISSED')])])
        self.assertEqual(len(second[PULLS][0]['reviews']), 1)
        self.assertEqual(second[PULLS][0]['reviews'][0]['state'], 'DISMISSED')

    def test_untouched_pull_survives(self):
        first, *_ = merge_pulls(None, [pull(1), pull(2)])
        second, *_ = merge_pulls(first, [pull(3)])
        self.assertEqual({p['number'] for p in second[PULLS]}, {1, 2, 3})


class CoverageTest(unittest.TestCase):
    """The coverage window is what stops silent undercounting."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = CacheStore(cache_dir=self.dir)
        payload, _ = merge_commits(None, [commit('a')])
        self.store.save('o', 'r', COMMITS, payload)

    def test_roundtrip(self):
        items, effective = self.store.read_items(
            'o', 'r', COMMITS, FROM, TO)
        self.assertEqual(len(items), 1)
        self.assertEqual(effective, TO)

    def test_rejects_range_older_than_coverage(self):
        with self.assertRaises(CoverageError) as caught:
            self.store.read_items('o', 'r', COMMITS,
                                  FROM - timedelta(days=1), TO)
        self.assertIn('predates covered_from', str(caught.exception))

    def test_clamps_until_to_coverage(self):
        """`--until now` is always past the last sync; clamp, do not fail."""
        _, effective = self.store.read_items(
            'o', 'r', COMMITS, FROM, TO + timedelta(days=5))
        self.assertEqual(effective, TO)

    def test_missing_cache_is_an_error_not_an_empty_list(self):
        with self.assertRaises(CoverageError):
            self.store.read_items('o', 'absent', COMMITS, FROM, TO)

    def test_rejects_foreign_schema(self):
        """The pre-redesign cache files must not be read as if they were v1."""
        legacy = self.store.repo_dir('o', 'legacy')
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / 'commits.json').write_text(
            '{"timestamp": "2026-08-03T15:01:07", "commits": []}')
        with self.assertRaises(ValueError):
            self.store.load('o', 'legacy', COMMITS)

    def test_save_is_atomic(self):
        """No stray temp files survive a write."""
        payload, _ = merge_commits(None, [commit('b')])
        self.store.save('o', 'r', COMMITS, payload)
        leftovers = [p.name for p in self.store.repo_dir('o', 'r').iterdir()
                     if p.name.endswith('.tmp')]
        self.assertEqual(leftovers, [])


if __name__ == '__main__':
    unittest.main()
