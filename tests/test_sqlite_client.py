"""Tests for the SQLite-backed activity client.

The real proof is the 218-user comparison against the JSON pipeline, which
cannot run without the live cache. These pin the semantics that comparison
would only catch by accident: window boundaries, review deduplication, and the
ordering the emitted JSON's key order depends on.

Run: python -m unittest tests.test_sqlite_client -v
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ghstats.store import sqlite as sqlite_store
from ghstats.clients.offline import Repo
from ghstats.clients.sqlite import SqliteClient, coverage_summary, load_sync_state

SINCE = datetime(2025, 1, 1, tzinfo=timezone.utc)
UNTIL = datetime(2025, 12, 31, tzinfo=timezone.utc)


class ClientTestCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = sqlite_store.connect(str(Path(self.dir.name) / 't.db'))
        self.rid = sqlite_store.repo_id(self.conn, 'o', 'repo-b')
        self.other = sqlite_store.repo_id(self.conn, 'o', 'repo-a')
        self.client = SqliteClient(self.conn, 'o')

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def commit(self, oid, authored, login='alice', repo=None):
        self.conn.execute(
            'INSERT INTO commits (repo_id, oid, author_login, committed_date, '
            'authored_date) VALUES (?,?,?,?,?)',
            (repo or self.rid, oid, login, authored, authored))

    def pull(self, number, created, login='alice', repo=None):
        self.conn.execute(
            'INSERT INTO pulls (repo_id, number, author_login, state, created_at, '
            'updated_at) VALUES (?,?,?,?,?,?)',
            (repo or self.rid, number, login, 'OPEN', created, created))

    def review(self, rid_, number, submitted, login='alice', repo=None):
        self.conn.execute(
            'INSERT INTO reviews (id, repo_id, pull_number, author_login, '
            'submitted_at, state) VALUES (?,?,?,?,?,?)',
            (rid_, repo or self.rid, number, login, submitted, 'APPROVED'))


class WindowTest(ClientTestCase):
    """`_in_window` is inclusive at both ends; the SQL path must not narrow it."""

    def test_includes_both_boundaries(self):
        self.commit('a', '2025-01-01T00:00:00Z')
        self.commit('b', '2025-12-31T00:00:00Z')
        got = self.client.get_user_commits(Repo('repo-b'), 'alice', SINCE, UNTIL)
        self.assertEqual({c.sha for c in got}, {'a', 'b'})

    def test_excludes_outside(self):
        self.commit('early', '2024-12-31T23:59:59Z')
        self.commit('late', '2025-12-31T00:00:01Z')
        got = self.client.get_user_commits(Repo('repo-b'), 'alice', SINCE, UNTIL)
        self.assertEqual(got, [])

    def test_filters_by_author(self):
        self.commit('mine', '2025-06-01T00:00:00Z', login='alice')
        self.commit('theirs', '2025-06-01T00:00:00Z', login='bob')
        got = self.client.get_user_commits(Repo('repo-b'), 'alice', SINCE, UNTIL)
        self.assertEqual([c.sha for c in got], ['mine'])

    def test_scopes_to_the_named_repository(self):
        self.commit('here', '2025-06-01T00:00:00Z')
        self.commit('there', '2025-06-01T00:00:00Z', repo=self.other)
        got = self.client.get_user_commits(Repo('repo-b'), 'alice', SINCE, UNTIL)
        self.assertEqual([c.sha for c in got], ['here'])


class ReviewTest(ClientTestCase):

    def test_reviewed_pulls_are_deduplicated(self):
        """Two reviews on one pull is one reviewed pull, but two reviews."""
        self.pull(7, '2025-06-01T00:00:00Z', login='bob')
        self.review('r1', 7, '2025-06-02T00:00:00Z')
        self.review('r2', 7, '2025-06-03T00:00:00Z')
        pulls = self.client.get_pull_requests_reviewed(
            Repo('repo-b'), 'alice', SINCE, UNTIL)
        reviews = self.client.get_reviews_by_user(
            Repo('repo-b'), 'alice', SINCE, UNTIL)
        self.assertEqual([p.number for p in pulls], [7])
        self.assertEqual(len(reviews), 2)

    def test_out_of_window_review_does_not_surface_its_pull(self):
        self.pull(7, '2025-06-01T00:00:00Z', login='bob')
        self.review('r1', 7, '2024-01-01T00:00:00Z')
        self.assertEqual(self.client.get_pull_requests_reviewed(
            Repo('repo-b'), 'alice', SINCE, UNTIL), [])

    def test_reviewed_pull_carries_its_own_author(self):
        """The pull belongs to bob; alice only reviewed it."""
        self.pull(7, '2025-06-01T00:00:00Z', login='bob')
        self.review('r1', 7, '2025-06-02T00:00:00Z', login='alice')
        pull = self.client.get_pull_requests_reviewed(
            Repo('repo-b'), 'alice', SINCE, UNTIL)[0]
        self.assertEqual(pull.author_login, 'bob')


class OrderingTest(ClientTestCase):
    """Order drives JSON key order, so it is part of the contract."""

    def test_repos_come_back_sorted(self):
        self.assertEqual([r.name for r in self.client.get_organization_repos('o')],
                         ['repo-a', 'repo-b'])

    def test_commits_are_ordered_by_date_then_oid(self):
        self.commit('zzz', '2025-01-02T00:00:00Z')
        self.commit('bbb', '2025-01-01T00:00:00Z')
        self.commit('aaa', '2025-01-01T00:00:00Z')
        got = self.client.get_user_commits(Repo('repo-b'), 'alice', SINCE, UNTIL)
        self.assertEqual([c.sha for c in got], ['aaa', 'bbb', 'zzz'])

    def test_pulls_are_ordered_by_number(self):
        for n in (30, 10, 20):
            self.pull(n, '2025-06-01T00:00:00Z')
        got = self.client.get_pull_requests_created(
            Repo('repo-b'), 'alice', SINCE, UNTIL)
        self.assertEqual([p.number for p in got], [10, 20, 30])


class CoverageSummaryTest(ClientTestCase):

    def insert_coverage(self, repo_id, kind, frm, to):
        self.conn.execute(
            'INSERT INTO coverage (repo_id, kind, covered_from, covered_to) '
            'VALUES (?,?,?,?)', (repo_id, kind, frm, to))

    def test_takes_the_binding_constraint_from_each_end(self):
        """Latest floor, earliest ceiling -- the narrowest honest window."""
        self.insert_coverage(self.rid, 'commits',
                             '2025-01-01T00:00:00Z', '2026-06-01T00:00:00Z')
        self.insert_coverage(self.other, 'commits',
                             '2025-03-01T00:00:00Z', '2026-08-01T00:00:00Z')
        summary = coverage_summary(self.conn, 'o')
        self.assertEqual(summary['covered_from'].isoformat(),
                         '2025-03-01T00:00:00+00:00')
        self.assertEqual(summary['covered_to'].isoformat(),
                         '2026-06-01T00:00:00+00:00')

    def test_reports_repos_with_no_coverage_row(self):
        self.insert_coverage(self.rid, 'commits',
                             '2025-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        summary = coverage_summary(self.conn, 'o')
        self.assertEqual(summary['repos'], 2)
        self.assertEqual(sorted(summary['missing']),
                         ['repo-a/commits', 'repo-a/pulls', 'repo-b/pulls'])


class SyncStateTest(ClientTestCase):
    """The reported sync status must describe an org-wide sweep."""

    def run_row(self, started, complete, repos):
        self.conn.execute(
            'INSERT INTO sync_runs (started_at, finished_at, repos_synced, '
            'repos_failed, points, seconds, complete) VALUES (?,?,?,?,?,?,?)',
            (started, started, repos, 0, 1, 1.0, complete))

    def test_prefers_the_latest_complete_sweep(self):
        """A three-repo retry must not be reported as the nightly sync."""
        self.run_row('2025-01-01T00:00:00Z', 1, 1198)
        self.run_row('2025-01-02T00:00:00Z', 0, 3)
        state = load_sync_state(self.conn, 'o')
        self.assertEqual(state['last_run_repos'], 1198)

    def test_falls_back_when_no_complete_run_exists(self):
        self.run_row('2025-01-02T00:00:00Z', 0, 3)
        self.assertEqual(load_sync_state(self.conn, 'o')['last_run_repos'], 3)

    def test_empty_history_reports_nothing(self):
        self.assertEqual(load_sync_state(self.conn, 'o'), {})


if __name__ == '__main__':
    unittest.main()
