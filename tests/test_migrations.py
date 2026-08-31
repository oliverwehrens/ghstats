"""Tests for the schema migration ladder.

Run: python -m unittest tests.test_migrations -v

The property under test is the one that matters operationally: a store already
on disk must reach the current schema **without being rebuilt**. The store this
was written against holds twenty months of history and cost hours of API budget;
a migration path that required a re-sync would not be used.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ghstats.store import sqlite as sqlite_store
from ghstats.store.sqlite import connect, migrate


def schema_of(conn):
    """Every DDL statement in the store, ordered so two stores compare equal."""
    return [row[0] for row in conn.execute(
        'SELECT sql FROM sqlite_master WHERE sql IS NOT NULL '
        'ORDER BY type, name')]


# A version-3 store is exactly `SCHEMA` without `V4_TABLES`: `connect` composes
# the two for a fresh install and the migration applies the second half to an
# existing file, which is what makes the two paths converge by construction
# rather than by review. Building the fixture from `SCHEMA` keeps this test
# honest as the base schema changes, instead of freezing a copy that rots.
V3_TABLES = sqlite_store.SCHEMA


class MigrationLadderTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / 'old.db')
        self.addCleanup(self.dir.cleanup)

    def _make_v3(self):
        """Write a store that looks like version 3, with a row in it."""
        conn = sqlite3.connect(self.path)
        conn.executescript(V3_TABLES)
        conn.execute("INSERT INTO repos (org, name) VALUES ('acme', 'thing')")
        conn.execute(
            "INSERT INTO commits (repo_id, oid, committed_date, message) "
            "VALUES (1, 'abc', '2025-01-01T00:00:00Z', 'first')")
        conn.execute('PRAGMA user_version = 3')
        conn.commit()
        conn.close()

    def test_migrates_in_place_and_keeps_the_data(self):
        self._make_v3()
        conn = connect(self.path, create=False)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute('PRAGMA user_version').fetchone()[0],
            sqlite_store.SCHEMA_VERSION)
        # The expensive part survived.
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM commits').fetchone()[0], 1)
        self.assertEqual(
            conn.execute('SELECT message FROM commits').fetchone()[0], 'first')

    def test_adds_the_v4_tables(self):
        self._make_v3()
        conn = connect(self.path, create=False)
        self.addCleanup(conn.close)
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ('teams', 'team_members', 'team_repos',
                      'jira_projects', 'issue_refs', 'bot_logins'):
            self.assertIn(table, names)

    def test_migrated_store_matches_a_fresh_one_exactly(self):
        """The defect this guards against is drift.

        `V4_TABLES` is applied both by a fresh install and by the migration, so
        the two cannot differ -- but only as long as nobody adds a table to one
        path and forgets the other. This is that alarm.
        """
        self._make_v3()
        migrated = connect(self.path, create=False)
        self.addCleanup(migrated.close)
        fresh = connect(str(Path(self.dir.name) / 'new.db'))
        self.addCleanup(fresh.close)
        self.assertEqual(schema_of(fresh), schema_of(migrated))

    def test_is_idempotent(self):
        self._make_v3()
        connect(self.path, create=False).close()
        first = schema_of(connect(self.path, create=False))
        connect(self.path, create=False).close()
        self.assertEqual(first, schema_of(connect(self.path, create=False)))

    def test_refuses_a_store_from_newer_code(self):
        """A forward version is not migratable and must not be guessed at."""
        self._make_v3()
        conn = sqlite3.connect(self.path)
        conn.execute(f'PRAGMA user_version = {sqlite_store.SCHEMA_VERSION + 5}')
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError) as caught:
            connect(self.path, create=False)
        self.assertIn('newer', str(caught.exception))

    def test_reports_a_gap_in_the_ladder(self):
        """A version with no registered migration fails loudly, not silently."""
        self._make_v3()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        original = dict(sqlite_store.MIGRATIONS)
        sqlite_store.MIGRATIONS.clear()
        try:
            with self.assertRaises(ValueError) as caught:
                migrate(conn, 3)
            self.assertIn('no migration', str(caught.exception))
        finally:
            sqlite_store.MIGRATIONS.update(original)

    def test_returns_the_versions_it_applied(self):
        self._make_v3()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        self.assertEqual(migrate(conn, 3), [4])


class TeamRepoKeyTest(unittest.TestCase):
    """`team_repos` must not create repositories.

    Resolving a grant through `repo_id()` inserts the repository when absent,
    and team grants reach archived repositories the sweep skips -- which added
    562 coverage-less rows to `repos` on the real store. `coverage_summary`
    cross-joins every repository against every kind and `unusable_pairs` turns a
    missing pair into a hard error, so every window-taking command refused for
    every member. This is that regression, pinned.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = connect(str(Path(self.dir.name) / 'store.db'))
        self.addCleanup(self.conn.close)
        self.store = sqlite_store.SyncStore(self.conn)

    def test_a_grant_does_not_add_a_repository(self):
        from datetime import datetime, timezone
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        before = self.conn.execute('SELECT COUNT(*) FROM repos').fetchone()[0]
        self.store.record_teams([{
            'slug': 'platform', 'name': 'Platform', 'members': ['ada'],
            'roles': {'ada': 'MEMBER'},
            'repos': [('archived-thing', 'ADMIN'), ('another', 'WRITE')],
        }], now)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM repos').fetchone()[0], before)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM team_repos').fetchone()[0], 2)

    def test_an_emptied_team_deactivates_its_whole_roster(self):
        """The zero-placeholder case: `IN ()` is a syntax error in SQLite."""
        from datetime import datetime, timezone
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        self.store.record_teams([{
            'slug': 'platform', 'name': 'Platform',
            'members': ['ada', 'grace'], 'roles': {}, 'repos': [('x', 'READ')],
        }], now)
        self.store.record_teams([{
            'slug': 'platform', 'name': 'Platform',
            'members': [], 'roles': {}, 'repos': [],
        }], now)
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM team_members WHERE active = 1').fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM team_members').fetchone()[0], 2)   # kept, not deleted
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM team_repos WHERE active = 1').fetchone()[0], 0)

    def test_first_seen_survives_a_departure_and_return(self):
        from datetime import datetime, timezone
        first = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later = datetime(2026, 8, 17, tzinfo=timezone.utc)
        team = lambda members: [{
            'slug': 'platform', 'name': 'Platform', 'members': members,
            'roles': {}, 'repos': [],
        }]
        self.store.record_teams(team(['ada']), first)
        self.store.record_teams(team([]), later)
        churn = self.store.record_teams(team(['ada']), later)
        row = self.conn.execute(
            'SELECT first_seen, active FROM team_members WHERE login = ?',
            ('ada',)).fetchone()
        self.assertEqual(row['first_seen'], '2026-01-01T00:00:00Z')
        self.assertEqual(row['active'], 1)
        self.assertEqual(churn['member_changes']['platform'], (['ada'], []))


if __name__ == '__main__':
    unittest.main()
