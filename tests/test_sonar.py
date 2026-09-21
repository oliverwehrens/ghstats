"""Tests for the SonarCloud sync: matching, the REST client, and the store.

Run: python -m unittest tests.test_sonar -v

No network. The client is exercised against a stub session, which is the only
part of this that would otherwise need one.

Three areas carry the risk:

- **Matching.** Everything the column shows hangs off resolving a repository to
  a project, and the failure mode is silent: a wrong match reports another
  codebase's gate, a missed one reports "no Sonar project" for a repository
  that has one.
- **Atomicity.** A fetch that fails halfway must leave the previous snapshot,
  not half of a new one.
- **Never-synced.** An empty `sonar_projects` means two different things and
  the explorer must not conflate them.
"""
import sqlite3
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

from ghstats.explorer import queries as q
from ghstats.sonar import match as matching
from ghstats.sonar import overrides
from ghstats.sonar.rest import SonarAuthError, SonarClient, SonarError
from ghstats.store import sqlite as sqlite_store
from ghstats.store.sqlite import connect, repo_id

ORG = 'acme'
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def project(key, name=None, last=None):
    return {'key': key, 'name': name or key, 'last_analysis': last}


class MatchPrecedenceTest(unittest.TestCase):
    """Exact `<org>_<repo>` beats exact `<repo>` beats a suffix match."""

    def test_prefixed_key_wins(self):
        result = matching.match_projects(
            ['widget'], [project('acme_widget'), project('widget')], 'acme')
        self.assertEqual([(m.repo, m.project_key, m.rule) for m in result.matched],
                         [('widget', 'acme_widget', 'prefixed')])

    def test_bare_key_matches_when_there_is_no_prefixed_one(self):
        result = matching.match_projects(['widget'], [project('widget')], 'acme')
        self.assertEqual(result.matched[0].rule, 'bare')

    def test_suffix_matches_a_foreign_prefix(self):
        """A project imported under another organization's prefix still counts."""
        result = matching.match_projects(
            ['widget'], [project('oldcorp_widget')], 'acme')
        self.assertEqual(result.matched[0].project_key, 'oldcorp_widget')
        self.assertEqual(result.matched[0].rule, 'suffix')

    def test_suffix_comparison_ignores_case(self):
        result = matching.match_projects(
            ['Widget'], [project('oldcorp_widget')], 'acme')
        self.assertEqual(result.matched[0].project_key, 'oldcorp_widget')

    def test_an_unrelated_project_matches_nothing(self):
        result = matching.match_projects(
            ['widget'], [project('acme_gadget')], 'acme')
        self.assertEqual(result.matched, [])
        self.assertEqual(result.unmatched, ['widget'])
        self.assertEqual(result.orphans, ['acme_gadget'])


class MatchAmbiguityTest(unittest.TestCase):
    """Ambiguity is resolved deterministically and reported, never silent."""

    def test_two_suffix_candidates_are_reported(self):
        result = matching.match_projects(
            ['widget'],
            [project('oldcorp_widget'), project('newcorp_widget')],
            'acme')
        self.assertEqual(len(result.ambiguous), 1)
        item = result.ambiguous[0]
        # Key order breaks the tie, so the same two inputs always agree.
        self.assertEqual(item.chosen, 'newcorp_widget')
        self.assertEqual(item.rejected, ['oldcorp_widget'])
        self.assertEqual(result.matched[0].project_key, 'newcorp_widget')

    def test_precedence_is_not_ambiguity_when_only_one_rule_fires(self):
        result = matching.match_projects(['widget'], [project('widget')], 'acme')
        self.assertEqual(result.ambiguous, [])

    def test_a_project_is_claimed_by_one_repository_only(self):
        """Two repositories cannot share a project; the second goes unmatched."""
        result = matching.match_projects(
            ['acme_widget', 'widget'], [project('acme_widget')], 'acme')
        claimed = [m.project_key for m in result.matched]
        self.assertEqual(claimed, ['acme_widget'])
        self.assertEqual(len(result.matched) + len(result.unmatched), 2)

    def test_matching_is_stable_regardless_of_input_order(self):
        projects = [project('newcorp_widget'), project('oldcorp_widget')]
        first = matching.match_projects(['b', 'widget', 'a'], projects, 'acme')
        second = matching.match_projects(
            ['a', 'b', 'widget'], list(reversed(projects)), 'acme')
        self.assertEqual(first.matched, second.matched)
        self.assertEqual(first.unmatched, second.unmatched)


class OverrideTest(unittest.TestCase):
    """The map file outranks every derived rule.

    The rules reach pairs that follow some convention. This is the escape
    hatch for the ones that follow none, and it has to beat an inference even
    when the inference would also have matched.
    """

    def test_an_override_beats_the_prefixed_rule(self):
        result = matching.match_projects(
            ['widget'], [project('acme_widget'), project('legacy-thing')],
            'acme', overrides={'widget': 'legacy-thing'})
        self.assertEqual(result.matched[0].project_key, 'legacy-thing')
        self.assertEqual(result.matched[0].rule, 'override')

    def test_an_override_wins_regardless_of_alphabetical_order(self):
        """The reason overrides get a pass of their own.

        Matched in name order, `alpha` would take `shared` by the bare rule
        before `zulu` -- which names it outright -- ever got a turn.
        """
        result = matching.match_projects(
            ['alpha', 'zulu'], [project('shared'), project('acme_alpha')],
            'acme', overrides={'zulu': 'shared'})
        got = {m.repo: m.project_key for m in result.matched}
        self.assertEqual(got.get('zulu'), 'shared')
        self.assertEqual(got.get('alpha'), 'acme_alpha')

    def test_an_override_naming_a_missing_project_is_reported_not_fatal(self):
        """A project deleted in SonarCloud must not cost the repo its gate."""
        result = matching.match_projects(
            ['widget'], [project('acme_widget')], 'acme',
            overrides={'widget': 'gone-away'})
        self.assertEqual(result.unknown_overrides, [('widget', 'gone-away')])
        # It still matched the ordinary way rather than going dark.
        self.assertEqual(result.matched[0].project_key, 'acme_widget')
        self.assertEqual(result.matched[0].rule, 'prefixed')

    def test_two_overrides_on_one_project_are_reported(self):
        """One project describes one codebase."""
        result = matching.match_projects(
            ['alpha', 'zulu'], [project('shared')], 'acme',
            overrides={'alpha': 'shared', 'zulu': 'shared'})
        self.assertEqual([m.repo for m in result.matched], ['alpha'])
        self.assertEqual(result.conflicting_overrides, [('zulu', 'shared')])

    def test_an_override_for_an_untracked_repo_is_silent(self):
        """The map outlives the repository list; a line for a repo that has
        not been synced yet should wait, not warn."""
        result = matching.match_projects(
            ['widget'], [project('acme_widget')], 'acme',
            overrides={'not-synced-yet': 'something'})
        self.assertEqual(result.unknown_overrides, [])
        self.assertEqual(result.conflicting_overrides, [])

    def test_no_overrides_behaves_exactly_as_before(self):
        plain = matching.match_projects(['widget'], [project('acme_widget')],
                                        'acme')
        explicit = matching.match_projects(['widget'], [project('acme_widget')],
                                           'acme', overrides={})
        self.assertEqual(plain, explicit)

    def test_an_overridden_project_is_not_an_orphan(self):
        result = matching.match_projects(
            ['widget'], [project('legacy-thing')], 'acme',
            overrides={'widget': 'legacy-thing'})
        self.assertEqual(result.orphans, [])


class MapFileTest(unittest.TestCase):
    """The override map is a gitignored file, not a constant in the source.

    It names one organization's repositories and its Sonar project keys, so it
    is organization data on the same footing as `members.txt`. Editing it must
    not be a source change.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / 'sonar-projects.txt')

    def _write(self, text):
        Path(self.path).write_text(text, encoding='utf-8')

    def test_a_missing_file_is_not_an_error(self):
        """The normal case: no overrides needed, and nothing to say about it."""
        self.assertEqual(overrides.load(self.path), {})

    def test_reads_pairs(self):
        self._write('legal-trustedshopscore = legal-trusted-shops-core\n'
                    'review-service=review-service:main\n')
        self.assertEqual(overrides.load(self.path), {
            'legal-trustedshopscore': 'legal-trusted-shops-core',
            'review-service': 'review-service:main',
        })

    def test_comments_and_blank_lines_are_ignored(self):
        """Comments are why this is not JSON: every line is a claim somebody
        has to be able to justify later."""
        self._write('# confirmed in SonarCloud, 2026-09-21\n'
                    '\n'
                    '   \n'
                    'widget = acme_widget   # plural in Sonar\n')
        self.assertEqual(overrides.load(self.path), {'widget': 'acme_widget'})

    def test_whitespace_around_the_separator_is_optional(self):
        self._write('  widget   =   acme_widget  \n')
        self.assertEqual(overrides.load(self.path), {'widget': 'acme_widget'})

    def test_a_key_containing_a_colon_survives(self):
        """SonarCloud's legacy branch projects are spelled `repo:branch`."""
        self._write('review-service = review-service:main\n')
        self.assertEqual(overrides.load(self.path)['review-service'],
                         'review-service:main')

    def test_a_line_that_is_not_a_pair_fails_loudly(self):
        """A skipped line is a mapping somebody believes is in effect."""
        self._write('widget = acme_widget\njust-a-name\n')
        with self.assertRaises(overrides.MapFileError) as caught:
            overrides.load(self.path)
        self.assertIn('line 2', str(caught.exception))

    def test_an_empty_side_fails(self):
        self._write('widget =\n')
        with self.assertRaises(overrides.MapFileError):
            overrides.load(self.path)

    def test_a_repository_mapped_twice_fails(self):
        """Two lines for one repository is a contradiction, and whichever won
        would be an accident of file order."""
        self._write('widget = acme_widget\nwidget = other_thing\n')
        with self.assertRaises(overrides.MapFileError) as caught:
            overrides.load(self.path)
        self.assertIn('line 1', str(caught.exception))

    def test_every_bad_line_is_reported_at_once(self):
        self._write('nope\nwidget = acme_widget\nalso-nope\n')
        with self.assertRaises(overrides.MapFileError) as caught:
            overrides.load(self.path)
        self.assertEqual(len(caught.exception.problems), 2)

    def test_the_file_is_gitignored(self):
        """It holds real repository names; it must not become a commit."""
        ignore = Path(__file__).resolve().parent.parent / '.gitignore'
        self.assertIn('/sonar-projects.txt',
                      ignore.read_text(encoding='utf-8').split())

    def test_no_override_map_lives_in_the_source_any_more(self):
        """Adding a mapping must not be a source change."""
        self.assertFalse(hasattr(sqlite_store, 'SONAR_PROJECT_KEYS'))


class AssumedKeyTest(unittest.TestCase):
    """An unmatched repository carries the key a convention would have used.

    This is the whole reason matching beats guessing: without it, "no Sonar
    project" and "the convention here is different" are the same output.
    """

    def test_unmatched_repositories_carry_the_assumed_key(self):
        result = matching.match_projects(['widget'], [], 'acme')
        self.assertEqual(result.assumed, {'widget': 'acme_widget'})

    def test_matched_repositories_do_not(self):
        result = matching.match_projects(
            ['widget'], [project('acme_widget')], 'acme')
        self.assertEqual(result.assumed, {})


class RowsTest(unittest.TestCase):
    """Every project gets a row; a missing gate is NULL, never a guess."""

    def test_orphans_are_kept(self):
        result = matching.match_projects(['widget'], [project('acme_gadget')],
                                         'acme')
        rows = matching.rows(result, [project('acme_gadget')], {})
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['repo_name'])
        self.assertIsNone(rows[0]['match_rule'])

    def test_a_project_with_no_gate_result_stores_null(self):
        projects = [project('acme_widget', last='2026-09-01T10:00:00Z')]
        result = matching.match_projects(['widget'], projects, 'acme')
        rows = matching.rows(result, projects, {})
        self.assertIsNone(rows[0]['gate_status'])
        self.assertEqual(rows[0]['repo_name'], 'widget')

    def test_gate_status_is_carried_through(self):
        projects = [project('acme_widget')]
        result = matching.match_projects(['widget'], projects, 'acme')
        rows = matching.rows(result, projects, {'acme_widget': 'ERROR'})
        self.assertEqual(rows[0]['gate_status'], 'ERROR')


# -- the REST client -------------------------------------------------------

class FakeResponse:

    def __init__(self, status=200, payload=None, headers=None, text=''):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


class FakeSession:
    """Returns queued responses and records what it was asked for."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not self.responses:
            raise AssertionError(f'unexpected request: {url} {params}')
        return self.responses.pop(0)


def client_with(responses):
    client = SonarClient('tok')
    client._session = FakeSession(responses)
    return client


class ClientPaginationTest(unittest.TestCase):

    def test_walks_every_page(self):
        client = client_with([
            FakeResponse(payload={
                'paging': {'total': 3},
                'components': [{'key': 'a', 'name': 'A',
                                'lastAnalysisDate': '2026-09-01T10:00:00+0000'},
                               {'key': 'b', 'name': 'B'}],
            }),
            FakeResponse(payload={
                'paging': {'total': 3},
                'components': [{'key': 'c', 'name': 'C'}],
            }),
        ])
        projects = client.projects('acme')
        self.assertEqual([p['key'] for p in projects], ['a', 'b', 'c'])
        self.assertEqual(projects[0]['last_analysis'], '2026-09-01T10:00:00+0000')
        self.assertIsNone(projects[1]['last_analysis'])

    def test_stops_on_an_empty_page_even_if_the_total_disagrees(self):
        """A total the server cannot honour must not loop forever."""
        client = client_with([
            FakeResponse(payload={'paging': {'total': 99},
                                  'components': [{'key': 'a'}]}),
            FakeResponse(payload={'paging': {'total': 99}, 'components': []}),
        ])
        self.assertEqual(len(client.projects('acme')), 1)

    def test_falls_back_to_the_public_endpoint_when_denied(self):
        """`/api/projects/search` needs a permission not every token has."""
        client = client_with([
            FakeResponse(status=403),
            FakeResponse(payload={
                'paging': {'total': 1},
                'components': [{'key': 'a', 'name': 'A',
                                'analysisDate': '2026-09-01T10:00:00+0000'}],
            }),
        ])
        projects = client.projects('acme')
        self.assertEqual(projects[0]['key'], 'a')
        self.assertEqual(projects[0]['last_analysis'], '2026-09-01T10:00:00+0000')
        self.assertIn('search_projects', client._session.calls[1][0])


class ClientRetryTest(unittest.TestCase):

    def setUp(self):
        patcher = unittest.mock.patch('ghstats.sonar.rest.time.sleep')
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_a_transient_status_then_succeeds(self):
        client = client_with([
            FakeResponse(status=503),
            FakeResponse(payload={'measures': []}),
        ])
        self.assertEqual(client.gate_statuses(['a']), {})

    def test_gives_up_and_says_so(self):
        client = client_with([FakeResponse(status=502)] * 4)
        with self.assertRaises(SonarError):
            client.gate_statuses(['a'])

    def test_honours_retry_after(self):
        client = client_with([
            FakeResponse(status=429, headers={'Retry-After': '30'}),
            FakeResponse(payload={'measures': []}),
        ])
        client.gate_statuses(['a'])
        self.assertEqual(self.sleep.call_args[0][0], 30.0)

    def test_a_rejected_token_is_not_retried(self):
        client = client_with([FakeResponse(status=401)])
        with self.assertRaises(SonarAuthError):
            client.gate_statuses(['a'])


class ClientMeasuresTest(unittest.TestCase):

    def test_batches_project_keys(self):
        client = client_with([
            FakeResponse(payload={'measures': []}),
            FakeResponse(payload={'measures': []}),
        ])
        client.gate_statuses([f'p{i}' for i in range(150)])
        first = client._session.calls[0][1]['projectKeys'].split(',')
        second = client._session.calls[1][1]['projectKeys'].split(',')
        self.assertEqual(len(first), 100)
        self.assertEqual(len(second), 50)

    def test_a_project_with_no_measure_is_absent_not_defaulted(self):
        client = client_with([FakeResponse(payload={'measures': [
            {'metric': 'alert_status', 'value': 'OK', 'component': 'a'},
        ]})])
        self.assertEqual(client.gate_statuses(['a', 'b']), {'a': 'OK'})


# -- the store and the query layer -----------------------------------------

class SonarStoreTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = connect(str(Path(self.dir.name) / 'store.db'))
        self.addCleanup(self.conn.close)
        repo_id(self.conn, ORG, 'widget')
        self.conn.commit()
        self.store = sqlite_store.SyncStore(self.conn)

    def _write(self, rows):
        self.store.replace_sonar_projects(rows, NOW)
        self.store.record_sonar_run(
            started_at=NOW, finished_at=NOW, sonar_org='acme',
            base_url='https://sonarcloud.io', projects_found=len(rows),
            repos_matched=sum(1 for r in rows if r.get('repo_name')),
            seconds=0.4)

    def test_a_replacement_drops_what_sonar_no_longer_holds(self):
        """Sonar reports current state; a deleted project must not linger."""
        self._write([{'project_key': 'acme_widget', 'repo_name': 'widget',
                      'match_rule': 'prefixed', 'gate_status': 'OK',
                      'last_analysis': '2026-09-01T10:00:00Z'}])
        self._write([{'project_key': 'acme_gadget', 'repo_name': None,
                      'match_rule': None, 'gate_status': 'ERROR',
                      'last_analysis': None}])
        keys = [r[0] for r in self.conn.execute(
            'SELECT project_key FROM sonar_projects')]
        self.assertEqual(keys, ['acme_gadget'])

    def test_timestamps_are_canonicalized(self):
        """The store compares timestamps as text; one spelling only."""
        self._write([{'project_key': 'acme_widget', 'repo_name': 'widget',
                      'match_rule': 'prefixed', 'gate_status': 'OK',
                      'last_analysis': '2026-09-01T10:00:00.123456+00:00'}])
        self.assertEqual(
            self.conn.execute(
                'SELECT last_analysis FROM sonar_projects').fetchone()[0],
            '2026-09-01T10:00:00Z')

    def test_a_dry_run_writes_nothing(self):
        store = sqlite_store.SyncStore(self.conn, dry_run=True)
        store.replace_sonar_projects(
            [{'project_key': 'acme_widget', 'repo_name': 'widget'}], NOW)
        store.record_sonar_run(
            started_at=NOW, finished_at=NOW, sonar_org='acme',
            base_url='https://sonarcloud.io', projects_found=1,
            repos_matched=1, seconds=0.1)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM sonar_projects').fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM sonar_runs').fetchone()[0], 0)

    def test_a_failed_write_leaves_the_previous_snapshot(self):
        """All or nothing: the old rows survive a replacement that throws.

        The row that fails carries no `project_key`, which the NOT NULL primary
        key refuses. Without a transaction the DELETE would already have
        emptied the table, leaving no Sonar data at all where there had been a
        complete snapshot.
        """
        self._write([{'project_key': 'acme_widget', 'repo_name': 'widget',
                      'match_rule': 'prefixed', 'gate_status': 'OK',
                      'last_analysis': '2026-09-01T10:00:00Z'}])
        with self.assertRaises(sqlite3.Error):
            self.store.replace_sonar_projects(
                [{'project_key': 'acme_gadget', 'repo_name': None},
                 {'project_key': None, 'repo_name': None}], NOW)
        rows = self.conn.execute(
            'SELECT project_key, gate_status FROM sonar_projects').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('acme_widget', 'OK')])

    # -- what the explorer reads back --------------------------------------

    def test_never_synced_is_not_the_same_as_no_project(self):
        """The distinction the `sonar_runs` table exists for."""
        state = q.sonar(self.conn)
        self.assertFalse(state['synced'])
        self.assertEqual(state['repos'], {})

        self._write([{'project_key': 'acme_gadget', 'repo_name': None,
                      'match_rule': None, 'gate_status': 'OK',
                      'last_analysis': None}])
        state = q.sonar(self.conn)
        # A run happened and matched nothing -- which is a different claim.
        self.assertTrue(state['synced'])
        self.assertEqual(state['repos'], {})

    def test_the_payload_is_keyed_by_repository_with_a_link(self):
        self._write([{'project_key': 'acme_widget', 'repo_name': 'widget',
                      'match_rule': 'prefixed', 'gate_status': 'ERROR',
                      'last_analysis': '2026-09-01T10:00:00Z'}])
        entry = q.sonar(self.conn)['repos']['widget']
        self.assertEqual(entry['gate'], 'ERROR')
        self.assertEqual(entry['key'], 'acme_widget')
        self.assertEqual(entry['last_analysis'], '2026-09-01T10:00:00Z')
        self.assertEqual(
            entry['url'],
            'https://sonarcloud.io/project/overview?id=acme_widget')

    def test_the_link_follows_the_server_the_data_came_from(self):
        """A self-hosted SonarQube must not link to sonarcloud.io."""
        self.store.replace_sonar_projects(
            [{'project_key': 'widget', 'repo_name': 'widget',
              'match_rule': 'bare', 'gate_status': 'OK',
              'last_analysis': None}], NOW)
        self.store.record_sonar_run(
            started_at=NOW, finished_at=NOW, sonar_org='acme',
            base_url='https://sonar.internal/', projects_found=1,
            repos_matched=1, seconds=0.1)
        self.assertEqual(
            q.sonar(self.conn)['repos']['widget']['url'],
            'https://sonar.internal/project/overview?id=widget')

    def test_a_project_key_with_url_characters_is_escaped(self):
        self._write([{'project_key': 'acme:widget/one', 'repo_name': 'widget',
                      'match_rule': 'suffix', 'gate_status': 'OK',
                      'last_analysis': None}])
        self.assertIn('id=acme%3Awidget%2Fone',
                      q.sonar(self.conn)['repos']['widget']['url'])

    def test_orphans_are_not_offered_to_the_page(self):
        """A project matching nothing is stored as evidence, not rendered."""
        self._write([{'project_key': 'acme_gadget', 'repo_name': None,
                      'match_rule': None, 'gate_status': 'ERROR',
                      'last_analysis': None}])
        self.assertEqual(q.sonar(self.conn)['repos'], {})
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM sonar_projects').fetchone()[0], 1)


class RepoDetailSonarTest(unittest.TestCase):
    """The repository page names the key it looked for when nothing matched."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = connect(str(Path(self.dir.name) / 'store.db'))
        self.addCleanup(self.conn.close)
        repo_id(self.conn, ORG, 'widget')
        self.conn.commit()
        self.store = sqlite_store.SyncStore(self.conn)

    def _run(self, rows):
        self.store.replace_sonar_projects(rows, NOW)
        self.store.record_sonar_run(
            started_at=NOW, finished_at=NOW, sonar_org='acme',
            base_url='https://sonarcloud.io', projects_found=len(rows),
            repos_matched=0, seconds=0.1)

    def test_unmatched_carries_the_assumed_key(self):
        self._run([])
        sonar = q._repo_sonar(self.conn, 'widget')
        self.assertTrue(sonar['synced'])
        self.assertIsNone(sonar['key'])
        self.assertEqual(sonar['assumed'], 'acme_widget')

    def test_never_synced_says_nothing_about_the_repository(self):
        sonar = q._repo_sonar(self.conn, 'widget')
        self.assertFalse(sonar['synced'])
        self.assertNotIn('assumed', sonar)


class RepoOverviewSonarTest(unittest.TestCase):
    """`/api/repos` carries Sonar beside the rows, not inside them."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = connect(str(Path(self.dir.name) / 'store.db'))
        self.addCleanup(self.conn.close)
        q.register_functions(self.conn, 'UTC')
        rid = repo_id(self.conn, ORG, 'widget')
        self.conn.execute(
            "INSERT INTO commits (repo_id, oid, author_login, committed_date, "
            "message) VALUES (?, 'abc', 'ada', '2026-09-01T10:00:00Z', 'x')",
            (rid,))
        self.conn.commit()
        store = sqlite_store.SyncStore(self.conn)
        store.replace_sonar_projects(
            [{'project_key': 'acme_widget', 'repo_name': 'widget',
              'match_rule': 'prefixed', 'gate_status': 'OK',
              'last_analysis': '2026-09-01T10:00:00Z'}], NOW)
        store.record_sonar_run(
            started_at=NOW, finished_at=NOW, sonar_org='acme',
            base_url='https://sonarcloud.io', projects_found=1,
            repos_matched=1, seconds=0.1)

    def test_the_bundle_carries_a_sonar_key(self):
        bundle = q.repo_overview(self.conn, ORG, q.Filters(tz='UTC'))
        self.assertIn('sonar', bundle)
        self.assertTrue(bundle['sonar']['synced'])
        self.assertIn('widget', bundle['sonar']['repos'])

    def test_the_rows_themselves_are_unchanged(self):
        """`_grouped` backs People, Teams and Day too; it must stay clean."""
        bundle = q.repo_overview(self.conn, ORG, q.Filters(tz='UTC'))
        self.assertTrue(bundle['repos'])
        for row in bundle['repos']:
            self.assertNotIn('gate', row)
            self.assertNotIn('sonar', row)

    def test_the_team_page_carries_it_too(self):
        """A team's "Repositories worked in" shows the gate beside the counts."""
        self.conn.execute(
            "INSERT INTO teams (slug, name, first_seen, last_seen) "
            "VALUES ('platform', 'Platform', ?, ?)", (NOW.isoformat(),) * 2)
        self.conn.commit()
        bundle = q.team_detail(self.conn, ORG, 'platform', q.Filters(tz='UTC'))
        self.assertTrue(bundle['sonar']['synced'])
        self.assertEqual(bundle['sonar']['repos']['widget']['gate'], 'OK')
        for row in bundle['by_repo']:
            self.assertNotIn('gate', row)

    def test_the_team_page_says_which_repos_the_team_actually_has(self):
        """"Worked in" is activity; a grant is access. They differ often.

        The team here worked in `widget` without holding it, which is the
        case the two columns exist to make visible.
        """
        self.conn.execute(
            "INSERT INTO teams (slug, name, first_seen, last_seen) "
            "VALUES ('platform', 'Platform', ?, ?)", (NOW.isoformat(),) * 2)
        self.conn.execute(
            "INSERT INTO teams (slug, name, first_seen, last_seen) "
            "VALUES ('payments', 'Payments', ?, ?)", (NOW.isoformat(),) * 2)
        self.conn.execute(
            "INSERT INTO team_repos (team_slug, repo_name, permission, active) "
            "VALUES ('payments', 'widget', 'ADMIN', 1)")
        # `by_repo` is scoped to this team's members, so the commit's author
        # has to be on it or the table is empty and there is nothing to grant.
        self.conn.execute(
            "INSERT INTO team_members (team_slug, login, first_seen, "
            "last_seen, active) VALUES ('platform', 'ada', ?, ?, 1)",
            (NOW.isoformat(),) * 2)
        self.conn.commit()

        bundle = q.team_detail(self.conn, ORG, 'platform', q.Filters(tz='UTC'))
        grants = bundle['repo_teams']['widget']
        self.assertEqual([g['slug'] for g in grants], ['payments'])
        # Platform is absent from the grants, which is what renders as
        # "not this team's" beside a repository its members worked in.
        self.assertNotIn('platform', [g['slug'] for g in grants])

    def test_the_gate_does_not_move_with_the_window(self):
        """A gate is current state. A window that excludes every commit still
        reports it, which is why the column tooltips say so."""
        empty = q.repo_overview(
            self.conn, ORG, q.Filters(frm='2020-01-01', to='2020-01-02', tz='UTC'))
        self.assertEqual(empty['repos'], [])
        self.assertEqual(empty['sonar']['repos']['widget']['gate'], 'OK')


if __name__ == '__main__':
    unittest.main()
