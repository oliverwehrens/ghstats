"""Tests for the write side of the SQLite store.

The invariant under test is the one the JSON store got for free by keeping a
repository's data and its watermark in the same file: either both land or
neither does.

Run: python -m unittest tests.test_sync_store -v
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ghstats.store import sqlite as sqlite_store
from ghstats.store.sqlite import SyncStore

FROM = datetime(2025, 1, 1, tzinfo=timezone.utc)
TO = datetime(2026, 8, 17, 13, 48, 43, 869494, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 18, tzinfo=timezone.utc)


def commit(oid, date='2025-06-01T00:00:00Z', **kw):
    record = {'oid': oid, 'committed_date': date, 'authored_date': date,
              'author_login': 'alice', 'author_name': 'Alice',
              'author_email': 'a@b.c', 'additions': 1, 'deletions': 0,
              'message': 'm'}
    record.update(kw)
    return record


def review(rid, state='APPROVED', submitted='2025-06-02T00:00:00Z', body=''):
    return {'id': rid, 'author_login': 'bob', 'submitted_at': submitted,
            'state': state, 'body': body}


def pull(number, reviews=None, **kw):
    record = {'number': number, 'author_login': 'alice', 'title': 't',
              'state': 'OPEN', 'created_at': '2025-06-01T00:00:00Z',
              'updated_at': '2025-06-01T00:00:00Z', 'merged_at': None,
              'closed_at': None, 'reviews': reviews or []}
    record.update(kw)
    return record


class StoreTestCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = sqlite_store.connect(str(Path(self.dir.name) / 't.db'))
        self.store = SyncStore(self.conn)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def count(self, table):
        return self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


class CommitMergeTest(StoreTestCase):

    def test_inserts_and_counts(self):
        added = self.store.merge_commits('o', 'r', [commit('a'), commit('b')],
                                         covered_from=FROM, covered_to=TO)
        self.assertEqual(added, 2)
        self.assertEqual(self.count('commits'), 2)

    def test_is_insert_only_across_runs(self):
        self.store.merge_commits('o', 'r', [commit('a')],
                                 covered_from=FROM, covered_to=TO)
        added = self.store.merge_commits('o', 'r', [commit('a'), commit('b')],
                                         covered_from=FROM, covered_to=LATER)
        self.assertEqual(added, 1)
        self.assertEqual(self.count('commits'), 2)

    def test_never_rewrites_an_existing_commit(self):
        """A re-fetch must not clobber what is already stored."""
        self.store.merge_commits('o', 'r', [commit('a', message='original')],
                                 covered_from=FROM, covered_to=TO)
        self.store.merge_commits('o', 'r', [commit('a', message='replaced')],
                                 covered_from=FROM, covered_to=LATER)
        message = self.conn.execute(
            'SELECT message FROM commits WHERE oid = ?', ('a',)).fetchone()[0]
        self.assertEqual(message, 'original')

    def test_the_same_oid_lands_in_two_repositories(self):
        self.store.merge_commits('o', 'fork-a', [commit('shared')],
                                 covered_from=FROM, covered_to=TO)
        self.store.merge_commits('o', 'fork-b', [commit('shared')],
                                 covered_from=FROM, covered_to=TO)
        self.assertEqual(self.count('commits'), 2)

    def test_coverage_lands_with_the_rows(self):
        self.store.merge_commits('o', 'r', [commit('a')],
                                 covered_from=FROM, covered_to=TO)
        got = self.store.coverage('o', 'r', 'commits')
        self.assertIsNotNone(got)
        self.assertEqual(got[0], FROM)
        # The ceiling is floored to whole seconds: understating coverage is
        # safe, overstating it is the silent-undercount bug.
        self.assertEqual(got[1], TO.replace(microsecond=0))

    def test_empty_repository_still_records_coverage(self):
        """Otherwise 'no commits' is indistinguishable from 'never fetched'."""
        self.store.merge_commits('o', 'r', [], covered_from=FROM, covered_to=TO)
        self.assertIsNotNone(self.store.coverage('o', 'r', 'commits'))

    def test_a_failed_merge_advances_no_watermark(self):
        """The whole point of one transaction per repository."""
        self.store.merge_commits('o', 'r', [commit('a')],
                                 covered_from=FROM, covered_to=TO)
        with self.assertRaises(sqlite3.Error):
            # A NOT NULL violation part-way through must take the coverage
            # update down with it.
            self.store.merge_commits(
                'o', 'r', [commit('b'), commit('c', committed_date=None)],
                covered_from=FROM, covered_to=LATER)
        self.assertEqual(self.count('commits'), 1)
        self.assertEqual(self.store.coverage('o', 'r', 'commits')[1],
                         TO.replace(microsecond=0))


class PullMergeTest(StoreTestCase):

    def test_counts_added_updated_and_reviews(self):
        added, updated, reviews = self.store.merge_pulls(
            'o', 'r', [pull(1, [review('v1')]), pull(2)],
            covered_from=FROM, covered_to=TO)
        self.assertEqual((added, updated, reviews), (2, 0, 1))

    def test_second_sight_of_a_pull_counts_as_updated(self):
        self.store.merge_pulls('o', 'r', [pull(1)], covered_from=FROM, covered_to=TO)
        added, updated, _ = self.store.merge_pulls(
            'o', 'r', [pull(1), pull(2)], covered_from=FROM, covered_to=LATER)
        self.assertEqual((added, updated), (1, 1))

    def test_reviews_are_unioned_not_replaced(self):
        """A re-fetch returns only the newest N; the rest must survive."""
        self.store.merge_pulls('o', 'r', [pull(1, [review('old')])],
                               covered_from=FROM, covered_to=TO)
        self.store.merge_pulls('o', 'r', [pull(1, [review('new')])],
                               covered_from=FROM, covered_to=LATER)
        ids = {r[0] for r in self.conn.execute('SELECT id FROM reviews')}
        self.assertEqual(ids, {'old', 'new'})

    def test_a_changed_pull_is_updated_in_place(self):
        self.store.merge_pulls('o', 'r', [pull(1, state='OPEN')],
                               covered_from=FROM, covered_to=TO)
        self.store.merge_pulls(
            'o', 'r', [pull(1, state='MERGED', merged_at='2025-07-01T00:00:00Z')],
            covered_from=FROM, covered_to=LATER)
        row = self.conn.execute('SELECT state, merged_at FROM pulls').fetchone()
        self.assertEqual(tuple(row), ('MERGED', '2025-07-01T00:00:00Z'))
        self.assertEqual(self.count('pulls'), 1)


class MembershipTest(StoreTestCase):

    def test_first_run_reports_everyone_as_joined(self):
        joined, left = self.store.record_members(['bob', 'alice'], FROM)
        self.assertEqual(joined, ['alice', 'bob'])
        self.assertEqual(left, [])

    def test_reports_churn_against_the_previous_roster(self):
        self.store.record_members(['alice', 'bob'], FROM)
        joined, left = self.store.record_members(['alice', 'carol'], LATER)
        self.assertEqual((joined, left), (['carol'], ['bob']))

    def test_a_leaver_is_deactivated_not_forgotten(self):
        self.store.record_members(['alice', 'bob'], FROM)
        self.store.record_members(['alice'], LATER)
        rows = dict(self.conn.execute('SELECT login, active FROM members'))
        self.assertEqual(rows, {'alice': 1, 'bob': 0})

    def test_first_seen_survives_a_departure_and_return(self):
        """The file this replaces could not express it."""
        self.store.record_members(['alice', 'bob'], FROM)
        self.store.record_members(['alice'], LATER)
        self.store.record_members(['alice', 'bob'], LATER)
        first_seen = self.conn.execute(
            'SELECT first_seen FROM members WHERE login = ?', ('bob',)).fetchone()[0]
        self.assertEqual(first_seen, sqlite_store.stamp(FROM))


class RunHistoryTest(StoreTestCase):

    def record(self, **kw):
        base = dict(started_at=FROM, finished_at=LATER, repos_synced=3,
                    repos_failed=0, points=17, seconds=1.5)
        base.update(kw)
        self.store.record_run(**base)

    def test_runs_accumulate(self):
        self.record()
        self.record()
        self.assertEqual(self.count('sync_runs'), 2)

    def test_partial_sweeps_are_marked(self):
        """A members-only refresh must not read as fresh org-wide coverage."""
        self.record(complete=False)
        self.assertEqual(
            self.conn.execute('SELECT complete FROM sync_runs').fetchone()[0], 0)


class DryRunTest(StoreTestCase):

    def setUp(self):
        super().setUp()
        self.store = SyncStore(self.conn, dry_run=True)

    def test_writes_nothing(self):
        self.store.merge_commits('o', 'r', [commit('a')],
                                 covered_from=FROM, covered_to=TO)
        self.store.merge_pulls('o', 'r', [pull(1, [review('v')])],
                               covered_from=FROM, covered_to=TO)
        self.store.record_members(['alice'], FROM)
        self.store.record_run(started_at=FROM, finished_at=LATER,
                              repos_synced=1, repos_failed=0, points=1, seconds=1)
        for table in ('commits', 'pulls', 'reviews', 'members', 'sync_runs',
                      'coverage'):
            self.assertEqual(self.count(table), 0, table)


if __name__ == '__main__':
    unittest.main()


def metrics(**kw):
    record = {'additions': 10, 'deletions': 2, 'changed_files': 3,
              'comments': 1, 'review_comments': 4}
    record.update(kw)
    return record


class PullMetricsTest(StoreTestCase):
    """Size and discussion, written alongside the pull it belongs to.

    The distinction the table exists to keep is between "this pull request was
    seen" and "this pull request was measured". Blurring the two is what makes
    an un-backfilled store look like a quiet one.
    """

    def test_a_record_with_metrics_writes_a_row(self):
        self.store.merge_pulls('o', 'r', [pull(1, metrics=metrics())],
                               covered_from=FROM, covered_to=TO)
        row = self.conn.execute(
            'SELECT additions, deletions, changed_files, comments, '
            'review_comments FROM pull_metrics').fetchone()
        self.assertEqual(tuple(row), (10, 2, 3, 1, 4))

    def test_a_record_without_metrics_writes_no_row(self):
        """An unmeasured pull must stay unmeasured, not become a row of zeroes:
        the explorer reads a missing row as "not measured" and a zero row as
        "a no-op change nobody commented on"."""
        self.store.merge_pulls('o', 'r', [pull(1)],
                               covered_from=FROM, covered_to=TO)
        self.assertEqual(self.count('pulls'), 1)
        self.assertEqual(self.count('pull_metrics'), 0)

    def test_a_record_without_metrics_does_not_erase_one(self):
        """`ghstats-import-cache` replays a JSON cache that predates these
        fields. A blind overwrite there would undo a backfill that cost real
        API budget."""
        self.store.merge_pulls('o', 'r', [pull(1, metrics=metrics())],
                               covered_from=FROM, covered_to=TO)
        self.store.merge_pulls('o', 'r', [pull(1)],
                               covered_from=FROM, covered_to=LATER)
        self.assertEqual(self.count('pull_metrics'), 1)
        self.assertEqual(
            self.conn.execute('SELECT additions FROM pull_metrics').fetchone()[0],
            10)

    def test_a_remeasure_replaces_the_row(self):
        self.store.merge_pulls('o', 'r', [pull(1, metrics=metrics())],
                               covered_from=FROM, covered_to=TO)
        self.store.merge_pulls(
            'o', 'r', [pull(1, metrics=metrics(additions=99, comments=7))],
            covered_from=FROM, covered_to=LATER)
        row = self.conn.execute(
            'SELECT additions, comments FROM pull_metrics').fetchone()
        self.assertEqual(tuple(row), (99, 7))
        self.assertEqual(self.count('pull_metrics'), 1)


class BackfillTest(StoreTestCase):
    """What `ghstats-backfill-pulls` drives."""

    def setUp(self):
        super().setUp()
        self.store.merge_pulls(
            'o', 'r',
            [pull(1, created_at='2025-06-01T00:00:00Z'),
             pull(2, created_at='2025-07-01T00:00:00Z', metrics=metrics()),
             pull(3, created_at='2025-08-01T00:00:00Z')],
            covered_from=FROM, covered_to=TO)

    def test_lists_only_the_unmeasured_ones(self):
        self.assertEqual(self.store.unmeasured_pulls('o'), [('r', 3), ('r', 1)])

    def test_lists_newest_first_so_a_capped_run_covers_recent_history(self):
        self.assertEqual(self.store.unmeasured_pulls('o', limit=1), [('r', 3)])

    def test_can_be_restricted_to_one_repository(self):
        self.store.merge_pulls('o', 'other', [pull(9)],
                               covered_from=FROM, covered_to=TO)
        self.assertEqual(self.store.unmeasured_pulls('o', repo='other'),
                         [('other', 9)])

    def test_merging_metrics_measures_them(self):
        written = self.store.merge_pull_metrics(
            'o', 'r', [dict(metrics(), number=1), dict(metrics(), number=3)],
            LATER)
        self.assertEqual(written, 2)
        self.assertEqual(self.store.unmeasured_pulls('o'), [])

    def test_a_pull_the_store_does_not_hold_is_skipped(self):
        """A PR opened since the last sync can show up in an enumeration with
        no `pulls` row behind it; inserting metrics for it would break the
        foreign key and fail the whole batch."""
        written = self.store.merge_pull_metrics(
            'o', 'r', [dict(metrics(), number=404)], LATER)
        self.assertEqual(written, 0)
        self.assertEqual(self.count('pull_metrics'), 1)

    def test_a_backfill_does_not_move_a_coverage_watermark(self):
        """It measures what was already collected. Claiming coverage from it
        would assert the sync had fetched a window it had not."""
        before = self.store.coverage('o', 'r', 'pulls')
        self.store.merge_pull_metrics(
            'o', 'r', [dict(metrics(), number=1)], LATER)
        self.assertEqual(self.store.coverage('o', 'r', 'pulls'), before)
