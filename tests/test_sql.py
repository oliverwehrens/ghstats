"""Tests for the SQL editor's query runner and its HTTP endpoint.

Run: python -m unittest tests.test_sql -v

The runner executes text a person typed against the store that took hours of
API budget to fill, so most of what is pinned here is what it must refuse:
writes of every spelling, a second statement smuggled after the first, a query
that never finishes, a result too large to ship.
"""
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from ghstats.explorer import queries as q
from ghstats.explorer import schema_docs, server, sql
from ghstats.store.sqlite import connect, repo_id

ORG = 'acme'


class SqlFixture(unittest.TestCase):
    """Two repositories, a person, a bot, and a pull request in each."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / 'store.db')
        conn = connect(self.path)
        alpha = repo_id(conn, ORG, 'alpha')
        beta = repo_id(conn, ORG, 'beta')
        conn.executemany(
            'INSERT INTO pulls (repo_id, number, author_login, title, state, '
            "created_at) VALUES (?,?,?,'t','OPEN',?)",
            ((alpha, 1, 'ada', '2026-08-16T23:30:00Z'),     # 17th in Berlin
             (beta, 2, 'renovate', '2026-08-17T10:00:00Z')))
        conn.execute("INSERT INTO bot_logins (login, source) "
                     "VALUES ('renovate', 'bare')")
        conn.commit()
        conn.close()

    def run_sql(self, text, **filters):
        filters.setdefault('tz', 'Europe/Berlin')
        return sql.run(self.path, text, q.Filters(**filters))

    def rows(self, text, **filters):
        return self.run_sql(text, **filters)['rows']


class RunTest(SqlFixture):

    def test_returns_columns_and_rows(self):
        out = self.run_sql('SELECT number, author_login FROM pulls ORDER BY number')
        self.assertEqual(out['columns'], ['number', 'author_login'])
        self.assertEqual(out['rows'], [[1, 'ada'], [2, 'renovate']])
        self.assertFalse(out['truncated'])
        self.assertEqual(out['row_count'], 2)

    def test_a_trailing_semicolon_is_one_statement(self):
        self.assertEqual(self.rows('SELECT 1;'), [[1]])

    def test_blank_text_is_refused(self):
        with self.assertRaises(sql.QueryError):
            self.run_sql('   ')

    def test_a_syntax_error_is_a_query_error_not_a_crash(self):
        with self.assertRaises(sql.QueryError) as caught:
            self.run_sql('SELEC 1')
        self.assertIn('syntax error', str(caught.exception))


class ParameterTest(SqlFixture):
    """The filter bar arrives as named parameters."""

    def test_the_window_binds_as_a_half_open_utc_range(self):
        """The same conversion the cards use, so `created_at >= :from AND
        created_at < :to` selects what the card counted."""
        out = self.run_sql('SELECT :from, :to', frm='2026-08-17', to='2026-08-17')
        self.assertEqual(out['rows'], [['2026-08-16T22:00:00Z',
                                        '2026-08-17T22:00:00Z']])

    def test_the_bound_values_are_reported_back(self):
        """The strip under the editor shows what `:repo` *was*, not what the
        filter bar says now."""
        out = self.run_sql('SELECT 1', repo='alpha', frm='2026-08-17')
        self.assertEqual(out['parameters']['repo'], 'alpha')
        self.assertEqual(out['parameters']['from'], '2026-08-16T22:00:00Z')
        self.assertEqual(sorted(out['parameters']), sorted(sql.PARAMETERS))

    def test_an_unset_filter_binds_null(self):
        self.assertEqual(self.rows('SELECT :repo IS NULL, :user IS NULL, '
                                   ':from IS NULL'), [[1, 1, 1]])

    def test_org_is_the_served_organization(self):
        out = sql.run(self.path, 'SELECT :org', q.Filters(), org=ORG)
        self.assertEqual(out['rows'], [[ORG]])

    def test_repo_user_tz_and_bots(self):
        out = self.rows('SELECT :repo, :user, :tz, :bots',
                        repo='beta', user='ada', bots=True)
        self.assertEqual(out, [['beta', 'ada', 'Europe/Berlin', 1]])
        self.assertEqual(self.rows('SELECT :bots'), [[0]])

    def test_the_bots_parameter_reads_as_the_cards_bot_rule(self):
        text = ('SELECT author_login FROM pulls WHERE :bots OR author_login '
                'NOT IN (SELECT login FROM bot_logins) ORDER BY number')
        self.assertEqual(self.rows(text), [['ada']])
        self.assertEqual(self.rows(text, bots=True), [['ada'], ['renovate']])

    def test_an_unknown_parameter_names_the_ones_that_exist(self):
        with self.assertRaises(sql.QueryError) as caught:
            self.run_sql('SELECT :team')
        message = str(caught.exception)
        self.assertIn(':team', message)
        for name in sql.PARAMETERS:
            self.assertIn(f':{name}', message)

    def test_positional_parameters_are_refused_with_a_hint(self):
        with self.assertRaises(sql.QueryError) as caught:
            self.run_sql('SELECT ?')
        self.assertIn(':repo', str(caught.exception))


class FunctionTest(SqlFixture):

    def test_local_date_follows_the_filter_timezone(self):
        """23:30 UTC on the 16th is the 17th in Berlin and the 16th in UTC. The
        editor must group the way the card it is explaining does."""
        text = 'SELECT local_date(created_at) FROM pulls WHERE number = 1'
        self.assertEqual(self.rows(text), [['2026-08-17']])
        self.assertEqual(self.rows(text, tz='UTC'), [['2026-08-16']])

    def test_local_hour_and_dow_are_registered(self):
        self.assertEqual(
            self.rows('SELECT local_hour(created_at), local_dow(created_at) '
                      'FROM pulls WHERE number = 1'), [[1, 'Monday']])

    def test_median_odd_and_even(self):
        self.assertEqual(self.rows(
            'SELECT median(v) FROM (SELECT 3 AS v UNION ALL SELECT 1 '
            'UNION ALL SELECT 2)'), [[2.0]])
        self.assertEqual(self.rows(
            'SELECT median(v) FROM (SELECT 1 AS v UNION ALL SELECT 2 '
            'UNION ALL SELECT 3 UNION ALL SELECT 4)'), [[2.5]])

    def test_median_ignores_nulls_like_every_sql_aggregate(self):
        self.assertEqual(self.rows(
            'SELECT median(v) FROM (SELECT NULL AS v UNION ALL SELECT 5)'), [[5.0]])

    def test_median_of_nothing_is_null(self):
        self.assertEqual(self.rows('SELECT median(number) FROM pulls WHERE 0'),
                         [[None]])

    def test_median_reads_numeric_text_and_refuses_other_text(self):
        self.assertEqual(self.rows("SELECT median(v) FROM (SELECT '4' AS v "
                                   "UNION ALL SELECT 2)"), [[3.0]])
        with self.assertRaises(sql.QueryError) as caught:
            self.run_sql('SELECT median(author_login) FROM pulls')
        self.assertIn('median() needs numbers', str(caught.exception))

    def test_median_agrees_with_the_cards(self):
        values = [10, 12, 14, 40000, 7, 7]
        union = ' UNION ALL '.join(f'SELECT {v} AS v' for v in values)
        self.assertEqual(self.rows(f'SELECT median(v) FROM ({union})'),
                         [[q._median(values)]])


class GuardTest(SqlFixture):
    """What the runner must refuse."""

    def assert_refused(self, text):
        with self.assertRaises(sql.QueryError, msg=text):
            self.run_sql(text)

    def test_writes_are_refused(self):
        for text in ("INSERT INTO repos (org, name) VALUES ('acme', 'x')",
                     "UPDATE pulls SET title = 'x'",
                     'DELETE FROM pulls',
                     'DROP TABLE pulls',
                     'CREATE TABLE x (a)',
                     'CREATE TEMP TABLE x (a)',
                     'CREATE TEMP VIEW x AS SELECT 1',
                     "ATTACH DATABASE 'other.db' AS other",
                     'PRAGMA user_version = 99',
                     'PRAGMA query_only = 0',
                     'VACUUM',
                     'BEGIN'):
            self.assert_refused(text)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pulls'), [[2]])
        self.assertFalse((Path(self.dir.name) / 'other.db').exists())

    def test_schema_pragmas_are_readable(self):
        names = [r[0] for r in self.rows(
            "SELECT name FROM pragma_table_info('pulls') ORDER BY cid")]
        self.assertEqual(names[:2], ['repo_id', 'number'])

    def test_a_second_statement_is_refused(self):
        self.assert_refused('SELECT 1; DELETE FROM pulls')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pulls'), [[2]])

    def test_rows_stop_at_the_cap_and_say_so(self):
        text = ('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n '
                'WHERE i < 50) SELECT i FROM n')
        out = sql.run(self.path, text, q.Filters(), max_rows=10)
        self.assertEqual(len(out['rows']), 10)
        self.assertTrue(out['truncated'])
        self.assertEqual(out['row_count'], 10)

    def test_a_result_exactly_at_the_cap_is_not_truncated(self):
        text = ('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n '
                'WHERE i < 10) SELECT i FROM n')
        self.assertFalse(sql.run(self.path, text, q.Filters(),
                                 max_rows=10)['truncated'])

    def test_a_runaway_query_is_stopped(self):
        text = ('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) '
                'SELECT COUNT(*) FROM n')
        with self.assertRaises(sql.QueryError) as caught:
            sql.run(self.path, text, q.Filters(), timeout=0.2)
        self.assertIn('stopped', str(caught.exception))

    def test_a_blob_arrives_as_hex_not_as_a_python_repr(self):
        self.assertEqual(self.rows("SELECT x'cafe'"), [['cafe']])


class SchemaDocsTest(SqlFixture):
    """Every column says what it counts, and nothing describes a ghost."""

    def setUp(self):
        super().setUp()
        self.conn = sql.open_connection(self.path, 'UTC')
        self.addCleanup(self.conn.close)
        self.schema = schema_docs.describe(self.conn)

    def test_every_table_and_view_is_described(self):
        for table in self.schema['tables']:
            self.assertTrue(table['about'], table['name'])

    def test_every_column_is_described(self):
        missing = [f"{t['name']}.{c['name']}" for t in self.schema['tables']
                   for c in t['columns'] if not c['about']]
        self.assertEqual(missing, [])

    def test_no_description_names_a_table_or_column_that_is_gone(self):
        live = {t['name']: {c['name'] for c in t['columns']}
                for t in self.schema['tables']}
        for name, (_, columns) in schema_docs.TABLES.items():
            self.assertIn(name, live, f'{name} is described but not in the store')
            self.assertEqual(set(columns) - live[name], set(), name)

    def test_views_are_marked_as_views(self):
        kinds = {t['name']: t['kind'] for t in self.schema['tables']}
        self.assertEqual(kinds['v_commit_ai'], 'view')
        self.assertEqual(kinds['pulls'], 'table')

    def test_columns_carry_type_and_key(self):
        pulls = next(t for t in self.schema['tables'] if t['name'] == 'pulls')
        number = next(c for c in pulls['columns'] if c['name'] == 'number')
        self.assertEqual((number['type'], number['pk']), ('INTEGER', True))

    def test_every_parameter_is_described(self):
        self.assertEqual([p['name'] for p in self.schema['parameters']],
                         list(sql.PARAMETERS))
        self.assertTrue(all(p['about'] for p in self.schema['parameters']))

    def test_every_documented_function_is_registered(self):
        for function in self.schema['functions']:
            argument = 1 if function['name'] == 'median' else "'2026-08-17T10:00:00Z'"
            text = f"SELECT {function['name']}({argument})"
            self.assertEqual(len(self.rows(text)), 1, function['name'])

    def test_the_schema_is_served(self):
        store = server.Store(self.path, ORG, 'UTC')
        body = server.route_api(store, '/api/sql/schema', {})
        self.assertIn('pull_metrics', [t['name'] for t in body['tables']])
        self.assertTrue(body['functions'])


class EndpointTest(SqlFixture):
    """`POST /api/sql` over a real socket: the content-type check is the whole
    point of the endpoint's shape, and only a request can exercise it."""

    def setUp(self):
        super().setUp()
        store = server.Store(self.path, ORG, 'UTC')
        handler = type('Handler', (server.Handler,),
                       {'store': store, 'allow_hosts': ('127.0.0.1',)})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=self.httpd.serve_forever,
                                  kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, body, *, path='/api/sql', content_type='application/json',
             host=None):
        conn = HTTPConnection('127.0.0.1', self.httpd.server_address[1])
        self.addCleanup(conn.close)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {'Content-Type': content_type}
        if host:
            headers['Host'] = host
        conn.request('POST', path, body=payload, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b'null')

    def test_runs_a_query_with_filters_from_the_query_string(self):
        status, body = self.post({'sql': 'SELECT :repo AS repo'},
                                 path='/api/sql?repo=alpha&tz=UTC')
        self.assertEqual(status, 200)
        self.assertEqual(body['columns'], ['repo'])
        self.assertEqual(body['rows'], [['alpha']])

    def test_a_form_post_is_refused(self):
        """A page on another site can send `text/plain` or a form to localhost
        without a preflight. `application/json` needs one, and nothing here
        answers it."""
        for content_type in ('text/plain', 'application/x-www-form-urlencoded'):
            status, _ = self.post({'sql': 'SELECT 1'}, content_type=content_type)
            self.assertEqual(status, 415, content_type)

    def test_a_bad_query_is_a_400_with_the_reason(self):
        status, body = self.post({'sql': 'DELETE FROM pulls'})
        self.assertEqual(status, 400)
        self.assertIn('error', body)

    def test_malformed_bodies_are_400s(self):
        for body in (b'not json', b'[]', json.dumps({'sql': 5}).encode(),
                     json.dumps({}).encode()):
            status, _ = self.post(body)
            self.assertEqual(status, 400, body)

    def test_an_oversized_body_is_refused(self):
        status, _ = self.post({'sql': 'SELECT 1 -- ' + 'x' * sql.MAX_TEXT})
        self.assertEqual(status, 413)

    def test_the_host_check_applies_to_posts(self):
        status, _ = self.post({'sql': 'SELECT 1'}, host='evil.example')
        self.assertEqual(status, 400)

    def test_other_paths_do_not_accept_posts(self):
        status, _ = self.post({'sql': 'SELECT 1'}, path='/api/repos')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
