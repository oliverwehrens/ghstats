"""Tests for the SQLite store's schema guarantees and timestamp canonicalization.

Run: python -m unittest tests.test_sqlite_store -v
"""
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ghstats.store import sqlite as sqlite_store
from ghstats.store.sqlite import canonical_ts, connect, repo_id


class CanonicalTimestampTest(unittest.TestCase):
    """One spelling, one precision -- because TEXT compares lexically."""

    def test_passes_canonical_through(self):
        self.assertEqual(canonical_ts('2025-01-01T00:00:00Z'),
                         '2025-01-01T00:00:00Z')

    def test_converts_offset_spelling(self):
        self.assertEqual(canonical_ts('2025-01-01T00:00:00+00:00'),
                         '2025-01-01T00:00:00Z')

    def test_floors_sub_second_precision(self):
        self.assertEqual(canonical_ts('2026-08-17T13:48:43.869494+00:00'),
                         '2026-08-17T13:48:43Z')

    def test_converts_non_utc_offset(self):
        self.assertEqual(canonical_ts('2025-01-01T02:00:00+02:00'),
                         '2025-01-01T00:00:00Z')

    def test_none_survives(self):
        self.assertIsNone(canonical_ts(None))

    def test_rejects_sub_second_when_flooring_is_unsafe(self):
        with self.assertRaises(ValueError):
            canonical_ts('2026-08-17T13:48:43.869494Z', floor=False)

    def test_commit_inside_the_window_is_not_excluded_by_it(self):
        """The bug this function exists to prevent, in its data-losing form.

        A commit at 13:48:43 sits inside a window whose ceiling is
        13:48:43.869494. Compared raw, `'...43Z' <= '...43.869494+00:00'` is
        false -- `'Z'` (0x5A) sorts after `'.'` (0x2E) -- so the commit drops
        out of its own coverage window.
        """
        commit_ts = '2026-08-17T13:48:43Z'
        covered_to = '2026-08-17T13:48:43.869494+00:00'
        conn = sqlite3.connect(':memory:')
        within = lambda a, b: conn.execute('SELECT ? <= ?', (a, b)).fetchone()[0]
        self.assertEqual(within(commit_ts, covered_to), 0)          # raw: dropped
        self.assertEqual(                                            # canonical: kept
            within(canonical_ts(commit_ts), canonical_ts(covered_to)), 1)
        conn.close()

    def test_identical_instants_compare_equal(self):
        """The two spellings of the same moment must not be distinguishable."""
        conn = sqlite3.connect(':memory:')
        equal = lambda a, b: conn.execute('SELECT ? = ?', (a, b)).fetchone()[0]
        self.assertEqual(
            equal('2025-01-01T00:00:00Z', '2025-01-01T00:00:00+00:00'), 0)
        self.assertEqual(
            equal(canonical_ts('2025-01-01T00:00:00Z'),
                  canonical_ts('2025-01-01T00:00:00+00:00')), 1)
        conn.close()


class UnusablePairsTest(unittest.TestCase):
    """The check that stops a gap in the store reading as a quiet fortnight."""

    def exempt(self, *names):
        """Stand in for an operator's own `UNSYNCABLE_REPOS` entries."""
        return mock.patch.object(
            sqlite_store, 'UNSYNCABLE_REPOS',
            {n: 'unfetchable, for the purposes of this test' for n in names})

    def test_complete_store_is_silent(self):
        self.assertEqual(
            sqlite_store.unusable_pairs({'missing': [], 'unreadable': []}), [])

    def test_missing_repo_is_reported(self):
        self.assertEqual(
            sqlite_store.unusable_pairs(
                {'missing': ['some-repo/commits'], 'unreadable': []}),
            ['some-repo/commits'])

    def test_known_unsyncable_repo_is_exempt(self):
        """Otherwise the error fires nightly and stops being read.

        `UNSYNCABLE_REPOS` ships empty -- which repos are unfetchable is a
        property of an organization -- so the exemption is what is under test
        here, not the shipped contents.
        """
        with self.exempt('doomed-repo'):
            pairs = sqlite_store.unusable_pairs(
                {'missing': ['doomed-repo/commits'], 'unreadable': []})
        self.assertEqual(pairs, [])

    def test_exemption_does_not_leak_to_other_repos(self):
        with self.exempt('doomed-repo'):
            pairs = sqlite_store.unusable_pairs({
                'missing': ['doomed-repo/commits', 'real-repo/pulls'],
                'unreadable': []})
        self.assertEqual(pairs, ['real-repo/pulls'])

    def test_unreadable_is_never_exempt(self):
        """A corrupt file is a fault, not a standing condition."""
        pairs = sqlite_store.unusable_pairs(
            {'missing': [], 'unreadable': ['doomed-repo/commits: bad']})
        self.assertEqual(len(pairs), 1)

    def test_accepts_either_stores_summary_shape(self):
        self.assertEqual(sqlite_store.unusable_pairs({}), [])


class SchemaTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / 'test.db')

    def tearDown(self):
        self.dir.cleanup()

    def test_creates_schema_and_stamps_version(self):
        conn = connect(self.path)
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(version, sqlite_store.SCHEMA_VERSION)
        conn.close()

    def test_rejects_unknown_version(self):
        conn = connect(self.path)
        conn.execute('PRAGMA user_version = 99')
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            connect(self.path)

    def test_reader_on_a_missing_store_says_so(self):
        """`create=False` is a reader. A reader must not invent a store."""
        with self.assertRaises(ValueError) as caught:
            connect(self.path, create=False)
        self.assertIn('ghstats-sync', str(caught.exception))

    def test_reader_on_a_missing_store_leaves_no_file_behind(self):
        """`sqlite3.connect` creates the file before anyone can check it.

        Left unguarded, a reader's failure turns "no store yet" into "a store
        at schema 0" for every run after it -- so the first clear error becomes
        a permanently confusing one.
        """
        with self.assertRaises(ValueError):
            connect(self.path, create=False)
        self.assertFalse(Path(self.path).exists())

    def test_reader_on_a_file_that_is_not_a_store_says_so(self):
        Path(self.path).touch()
        with self.assertRaises(ValueError) as caught:
            connect(self.path, create=False)
        self.assertIn('ghstats-sync', str(caught.exception))

    def test_reader_opens_an_existing_store(self):
        connect(self.path).close()
        conn = connect(self.path, create=False)
        self.assertEqual(
            conn.execute('PRAGMA user_version').fetchone()[0],
            sqlite_store.SCHEMA_VERSION)
        conn.close()

    def test_repo_id_is_stable(self):
        conn = connect(self.path)
        first = repo_id(conn, 'o', 'r')
        self.assertEqual(repo_id(conn, 'o', 'r'), first)
        self.assertNotEqual(repo_id(conn, 'o', 'other'), first)
        conn.close()

    def test_same_oid_survives_in_two_repos(self):
        """A great many real commits do this, wherever repos are forks.

        A bare `oid` primary key would discard one copy silently.
        """
        conn = connect(self.path)
        a, b = repo_id(conn, 'o', 'fork-a'), repo_id(conn, 'o', 'fork-b')
        for rid in (a, b):
            conn.execute(
                'INSERT INTO commits (repo_id, oid, committed_date) VALUES (?,?,?)',
                (rid, 'deadbeef', '2025-01-01T00:00:00Z'))
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM commits').fetchone()[0], 2)
        conn.close()

    def test_duplicate_oid_within_one_repo_is_rejected(self):
        conn = connect(self.path)
        rid = repo_id(conn, 'o', 'r')
        conn.execute(
            'INSERT INTO commits (repo_id, oid, committed_date) VALUES (?,?,?)',
            (rid, 'deadbeef', '2025-01-01T00:00:00Z'))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO commits (repo_id, oid, committed_date) VALUES (?,?,?)',
                (rid, 'deadbeef', '2025-01-01T00:00:00Z'))
        conn.close()

    def test_review_needs_a_parent_pull(self):
        conn = connect(self.path)
        rid = repo_id(conn, 'o', 'r')
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO reviews (id, repo_id, pull_number, state) '
                'VALUES (?,?,?,?)', ('PRR_1', rid, 404, 'APPROVED'))
        conn.close()

    def test_state_domains_are_enforced(self):
        conn = connect(self.path)
        rid = repo_id(conn, 'o', 'r')
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO pulls (repo_id, number, state, created_at) '
                'VALUES (?,?,?,?)', (rid, 1, 'PENDING', '2025-01-01T00:00:00Z'))
        conn.close()

    def test_coverage_is_one_row_per_repo_and_kind(self):
        conn = connect(self.path)
        rid = repo_id(conn, 'o', 'r')
        insert = ('INSERT INTO coverage (repo_id, kind, covered_from, covered_to) '
                  'VALUES (?,?,?,?)')
        conn.execute(insert, (rid, 'commits', '2025-01-01T00:00:00Z',
                              '2026-01-01T00:00:00Z'))
        conn.execute(insert, (rid, 'pulls', '2025-01-01T00:00:00Z',
                              '2026-01-01T00:00:00Z'))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(insert, (rid, 'commits', '2025-01-01T00:00:00Z',
                                  '2026-01-01T00:00:00Z'))
        conn.close()


class CuratedRescueTest(unittest.TestCase):
    """Hand-corrected identities must outlive the database they live in."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / 'test.db')

    def tearDown(self):
        self.dir.cleanup()

    def test_rescues_identity_rows(self):
        from ghstats.tools import import_cache
        conn = connect(self.path)
        conn.execute("INSERT INTO identities VALUES ('a@b.c', 'alice', 0)")
        conn.commit()
        conn.close()
        self.assertEqual(import_cache.read_curated(self.path),
                         [('a@b.c', 'alice', 0)])

    def test_absent_database_rescues_nothing(self):
        from ghstats.tools import import_cache
        self.assertEqual(import_cache.read_curated(self.path), [])

    def test_unreadable_database_rescues_nothing(self):
        """A corrupt store must not stop an import that would replace it."""
        from ghstats.tools import import_cache
        Path(self.path).write_bytes(b'not a database')
        self.assertEqual(import_cache.read_curated(self.path), [])


if __name__ == '__main__':
    unittest.main()
