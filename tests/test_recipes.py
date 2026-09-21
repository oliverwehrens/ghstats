"""Every recipe reproduces the card it explains.

Run: python -m unittest tests.test_recipes -v

A recipe that drifts from `queries.py` is worse than none: it teaches the wrong
definition with the authority of the tool. So each one is run against a fixture
built to contain the traps -- unmeasured pull requests, bot authors in both
spellings, blank and whitespace-only review bodies, a pull request opened just
before midnight UTC, a repository nobody has measured, a second organization --
and compared with the Python under several filter sets.
"""
import math
import tempfile
import unittest
from pathlib import Path

from ghstats.explorer import queries as q
from ghstats.explorer import recipes, server, sql
from ghstats.reindex import rebuild_bot_logins
from ghstats.store.sqlite import connect, repo_id

ORG = 'acme'
TZ = 'Europe/Berlin'

# The filter sets every recipe is checked under. The last is short enough to
# bucket by week and crosses a local-midnight week boundary.
SETUPS = {
    'defaults': dict(),
    'bots included': dict(bots=True),
    'one repository': dict(repo='alpha'),
    'one person': dict(user='ada'),
    'a Berlin window': dict(frm='2026-07-01', to='2026-08-15'),
}


class RecipeFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.path = str(Path(cls.dir.name) / 'store.db')
        conn = connect(cls.path)
        alpha = repo_id(conn, ORG, 'alpha')
        beta = repo_id(conn, ORG, 'beta')
        gamma = repo_id(conn, ORG, 'gamma')
        foreign = repo_id(conn, 'other', 'alpha')    # same name, other org

        for login in ('ada', 'grace', 'linus'):
            conn.execute(
                'INSERT INTO members (login, first_seen, last_seen, active) '
                "VALUES (?, '2025-01-01T00:00:00Z', '2026-09-01T00:00:00Z', 1)",
                (login,))

        def pull(rid, number, author, opened, merged=None, metrics=None):
            conn.execute(
                'INSERT INTO pulls (repo_id, number, author_login, title, state, '
                'created_at, merged_at) VALUES (?,?,?,?,?,?,?)',
                (rid, number, author, f'change {number}',
                 'MERGED' if merged else 'OPEN', opened, merged))
            if metrics:
                conn.execute(
                    'INSERT INTO pull_metrics VALUES '
                    "(?,?,?,?,?,?,?,'2026-09-01T00:00:00Z')",
                    (rid, number, *metrics))

        # metrics: additions, deletions, changed_files, comments, review_comments
        pull(alpha, 1, 'ada', '2026-07-06T09:00:00Z', '2026-07-07T09:00:00Z',
             (100, 20, 3, 2, 1))
        # 22:30 UTC on Sunday 19 July is 00:30 on Monday 20 July in Berlin:
        # a different day, and a different week.
        pull(alpha, 2, 'grace', '2026-07-19T22:30:00Z', metrics=(5, 0, 1, 0, 0))
        pull(alpha, 3, 'ada', '2026-08-10T10:00:00Z')                 # unmeasured
        pull(alpha, 4, 'renovate', '2026-08-11T10:00:00Z',
             '2026-08-11T11:00:00Z', (300, 300, 2, 0, 0))              # bare bot
        pull(alpha, 5, 'linus', '2026-03-02T10:00:00Z',
             '2026-03-03T10:00:00Z', (40000, 10, 50, 1, 0))            # outlier
        pull(alpha, 6, None, '2026-07-21T10:00:00Z', metrics=(8, 1, 1, 1, 0))  # ghost
        pull(beta, 1, 'grace', '2026-07-08T10:00:00Z', metrics=(0, 0, 0, 3, 0))
        pull(beta, 2, 'linus', '2026-08-12T10:00:00Z', '2026-08-13T10:00:00Z',
             (12, 2, 1, 0, 4))
        pull(gamma, 1, 'ada', '2026-08-01T10:00:00Z')                 # nobody measured
        pull(foreign, 1, 'ada', '2026-07-06T10:00:00Z', metrics=(7, 7, 1, 1, 1))

        reviews = [
            (alpha, 1, 'grace', '2026-07-06T12:00:00Z', 'COMMENTED', 'please split'),
            (alpha, 1, 'linus', '2026-07-06T13:00:00Z', 'APPROVED', ''),
            (alpha, 1, 'cursor', '2026-07-06T14:00:00Z', 'COMMENTED', 'nit: rename'),
            (alpha, 1, 'ada', '2026-07-06T15:00:00Z', 'COMMENTED', '   '),
            (alpha, 2, 'ada', '2026-07-20T09:00:00Z', 'APPROVED', 'lgtm'),
            (alpha, 4, 'grace', '2026-08-11T10:30:00Z', 'APPROVED', ''),
            (beta, 1, 'linus', '2026-07-08T11:00:00Z', 'COMMENTED', 'why no lines?'),
            (beta, 2, 'ada', None, 'COMMENTED', 'pending, never submitted'),
            (foreign, 1, 'grace', '2026-07-06T11:00:00Z', 'COMMENTED', 'elsewhere'),
        ]
        for i, (rid, number, author, when, state, body) in enumerate(reviews):
            conn.execute(
                'INSERT INTO reviews (id, repo_id, pull_number, author_login, '
                'submitted_at, state, body) VALUES (?,?,?,?,?,?,?)',
                (f'r{i}', rid, number, author, when, state, body))

        commits = [
            (alpha, 'c1', 'ada', '2026-07-06T08:00:00Z', 10, 2),
            (beta, 'c2', 'grace', '2026-07-19T22:45:00Z', 5, 5),
            (alpha, 'c3', 'renovate[bot]', '2026-08-11T09:00:00Z', 30, 30),
            (gamma, 'c4', 'linus', '2026-03-02T09:00:00Z', 1, 0),
            (alpha, 'c5', None, '2026-07-10T09:00:00Z', 4, 4),
            (foreign, 'c6', 'ada', '2026-07-06T09:00:00Z', 100, 0),
        ]
        conn.executemany(
            'INSERT INTO commits (repo_id, oid, author_login, committed_date, '
            'additions, deletions) VALUES (?,?,?,?,?,?)', commits)

        rebuild_bot_logins(conn)          # renovate[bot], renovate, cursor, ...
        conn.commit()
        q.register_functions(conn, TZ)
        cls.conn = conn

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.dir.cleanup()

    def filters(self, setup, **overrides):
        fields = dict(tz=TZ, **SETUPS[setup])
        fields.update(overrides)
        return q.Filters(**fields)

    def recipe(self, recipe_id, f):
        out = sql.run(self.path, recipes.get(recipe_id)['sql'], f, org=ORG)
        return [dict(zip(out['columns'], row)) for row in out['rows']]

    def assert_matches(self, recipe_row, card, label):
        """Every column the recipe returns agrees with the card's field."""
        for key, value in recipe_row.items():
            self.assertIn(key, card, f'{label}: the card has no {key!r}')
            expected = card[key]
            if isinstance(expected, float) or isinstance(value, float):
                if expected is None or value is None:
                    self.assertEqual(value, expected, f'{label}: {key}')
                else:
                    self.assertTrue(math.isclose(value, expected, abs_tol=1e-9),
                                    f'{label}: {key} {value} != {expected}')
            else:
                self.assertEqual(value, expected, f'{label}: {key}')


class EqualityTest(RecipeFixture):

    def test_activity_totals(self):
        for setup in SETUPS:
            with self.subTest(setup):
                f = self.filters(setup)
                [row] = self.recipe('activity-totals', f)
                self.assert_matches(row, q._totals(self.conn, ORG, f), setup)

    def test_pr_size_totals(self):
        for setup in SETUPS:
            with self.subTest(setup):
                f = self.filters(setup)
                card = q.pull_discussion(self.conn, ORG, f)
                [row] = self.recipe('pr-size-totals', f)
                expected = dict(card['totals'], total=card['total'],
                                unmeasured=card['unmeasured'])
                self.assert_matches(row, expected, setup)

    def test_pr_size_trend(self):
        for setup in SETUPS:
            with self.subTest(setup):
                f = self.filters(setup)
                card = q.pull_discussion(self.conn, ORG, f)
                rows = self.recipe('pr-size-trend', f)
                self.assertEqual([r['bucket'] for r in rows],
                                 [b['bucket'] for b in card['buckets']], setup)
                for row, bucket in zip(rows, card['buckets']):
                    self.assert_matches(
                        row, dict(bucket, granularity=card['granularity']), setup)

    def test_pr_size_by_repo(self):
        for setup in SETUPS:
            with self.subTest(setup):
                # The Repositories page drops any repository filter first.
                f = self.filters(setup, repo=None)
                card = q.pull_discussion(self.conn, ORG, f, per_repo=True)
                rows = self.recipe('pr-size-by-repo', self.filters(setup))
                self.assertEqual([r['repo'] for r in rows],
                                 [r['repo'] for r in card['by_repo']], setup)
                for row, expected in zip(rows, card['by_repo']):
                    self.assert_matches(row, expected, f"{setup}: {row['repo']}")


class FixtureTest(RecipeFixture):
    """The equality tests only mean something if the traps are exercised."""

    def test_the_fixture_has_the_cases_that_break_a_naive_query(self):
        card = q.pull_discussion(self.conn, ORG, self.filters('defaults'),
                                 per_repo=True)
        self.assertGreater(card['unmeasured'], 0)
        self.assertEqual(card['granularity'], 'month')
        self.assertIn('gamma', [r['repo'] for r in card['by_repo']])
        self.assertIsNone(next(r for r in card['by_repo']
                               if r['repo'] == 'gamma')['per_100_lines'])
        weekly = q.pull_discussion(self.conn, ORG, self.filters('a Berlin window'))
        self.assertEqual(weekly['granularity'], 'week')
        self.assertIn('2026-07-20', [b['bucket'] for b in weekly['buckets']])
        with_bots = q.pull_discussion(self.conn, ORG, self.filters('bots included'))
        self.assertGreater(with_bots['measured'], card['measured'])


class RecipeFileTest(unittest.TestCase):

    def test_every_recipe_parses_with_an_explanation(self):
        loaded = recipes.load()
        self.assertTrue(loaded)
        for recipe in loaded:
            self.assertTrue(recipe['about'], recipe['id'])
            self.assertTrue(recipe['sql'].strip(), recipe['id'])

    def test_the_header_is_read(self):
        recipe = recipes.parse('x', '-- title: T\n-- card: c\n-- ignores: team, ai\n'
                                    '--\n-- First line.\n--   indented\nSELECT 1\n')
        self.assertEqual((recipe['title'], recipe['card'], recipe['ignores']),
                         ('T', 'c', ['team', 'ai']))
        self.assertEqual(recipe['about'], 'First line.\n  indented')

    def test_a_header_without_a_title_is_refused(self):
        with self.assertRaises(recipes.RecipeError):
            recipes.parse('x', '-- card: c\nSELECT 1\n')

    def test_an_unknown_ignored_filter_is_refused(self):
        with self.assertRaises(recipes.RecipeError):
            recipes.parse('x', '-- title: T\n-- card: c\n-- ignores: colour\nSELECT 1\n')

    def test_an_unknown_recipe_is_a_key_error(self):
        for name in ('nope', '../recipes/activity-totals'):
            with self.assertRaises(KeyError):
                recipes.get(name)

    def test_recipes_are_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'store.db')
            connect(path).close()
            body = server.route_api(server.Store(path, ORG, 'UTC'),
                                    '/api/sql/recipes', {})
        self.assertEqual([r['id'] for r in body['recipes']],
                         [r['id'] for r in recipes.load()])

    def test_recipes_are_declared_as_package_data(self):
        try:
            import tomllib
        except ImportError:                       # 3.10 has no tomllib
            self.skipTest('tomllib needs 3.11')
        root = Path(__file__).resolve().parent.parent
        config = tomllib.loads((root / 'pyproject.toml').read_text())
        patterns = config['tool']['setuptools']['package-data']['ghstats.explorer']
        package = recipes.RECIPES.parent
        for path in recipes.RECIPES.glob('*.sql'):
            relative = path.relative_to(package)
            self.assertTrue(any(relative.match(p) for p in patterns), relative)


if __name__ == '__main__':
    unittest.main()
