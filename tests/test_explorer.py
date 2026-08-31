"""Tests for the explorer's query layer and its HTTP routing.

Run: python -m unittest tests.test_explorer -v

The query layer is deliberately socket-free, so everything here builds a small
store, asks it questions, and checks the answers. Two areas carry most of the
risk and most of the tests:

- **Window boundaries.** A local day is not a UTC day, and the store is UTC.
- **The event union.** Four kinds share one filter vocabulary; a filter that
  applies to three of them is a silent undercount.
"""
import tempfile
import unittest
from pathlib import Path

from ghstats.explorer import queries as q
from ghstats.explorer import server
from ghstats.reindex import (rebuild_bot_logins, rebuild_issue_refs,
                            seed_ai_tools)
from ghstats.store.sqlite import connect, repo_id

ORG = 'acme'


class WindowTest(unittest.TestCase):
    """Local dates in, half-open UTC instants out."""

    def test_a_utc_day_is_midnight_to_midnight(self):
        self.assertEqual(q.window_utc('2026-08-17', '2026-08-17', 'UTC'),
                         ('2026-08-17T00:00:00Z', '2026-08-18T00:00:00Z'))

    def test_the_end_is_exclusive_so_the_last_day_is_whole(self):
        """`to` inclusive on the way in. Comparing `<= '2026-08-17'` would keep
        only the midnight second of the final day."""
        _, end = q.window_utc('2026-08-01', '2026-08-17', 'UTC')
        self.assertEqual(end, '2026-08-18T00:00:00Z')

    def test_summer_offset(self):
        self.assertEqual(q.window_utc('2026-08-17', '2026-08-17', 'Europe/Berlin'),
                         ('2026-08-16T22:00:00Z', '2026-08-17T22:00:00Z'))

    def test_winter_offset_differs_from_summer(self):
        """The reason a fixed offset is not good enough: the same zone shifts.

        A constant `+02:00` would put January activity on the wrong day either
        side of 23:00 local.
        """
        self.assertEqual(q.window_utc('2026-01-15', '2026-01-15', 'Europe/Berlin'),
                         ('2026-01-14T23:00:00Z', '2026-01-15T23:00:00Z'))

    def test_open_ended_windows(self):
        self.assertEqual(q.window_utc(None, None, 'UTC'), (None, None))
        self.assertEqual(q.window_utc('2026-01-01', None, 'UTC')[1], None)

    def test_an_unknown_zone_falls_back_to_utc(self):
        """A bad --timezone should render UTC, not refuse to start."""
        self.assertEqual(q.window_utc('2026-08-17', '2026-08-17', 'Mars/Olympus'),
                         ('2026-08-17T00:00:00Z', '2026-08-18T00:00:00Z'))


class FiltersTest(unittest.TestCase):

    def test_unknown_kinds_are_dropped_not_rejected(self):
        """A stale bookmark should degrade, not error."""
        self.assertEqual(q.Filters(kinds=('commit', 'nonsense')).kinds, ('commit',))

    def test_an_entirely_unknown_kind_set_falls_back_to_all(self):
        self.assertEqual(q.Filters(kinds=('nonsense',)).kinds, q.KINDS)

    def test_limit_is_clamped(self):
        self.assertEqual(q.Filters(limit=99999).limit, 1000)
        self.assertEqual(q.Filters(limit=0).limit, 1)

    def test_offset_cannot_go_negative(self):
        self.assertEqual(q.Filters(offset=-5).offset, 0)

    def test_describe_round_trips(self):
        f = q.Filters(frm='2026-01-01', user='ada', kinds=('commit',))
        self.assertEqual(q.Filters(**f.describe()).describe(), f.describe())


class StoreFixture(unittest.TestCase):
    """A small store with two people, two repos, a team and some issue keys."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = connect(str(Path(self.dir.name) / 'store.db'))
        self.addCleanup(self.conn.close)
        q.register_functions(self.conn, 'Europe/Berlin')

        self.alpha = repo_id(self.conn, ORG, 'alpha')
        self.beta = repo_id(self.conn, ORG, 'beta')
        for login in ('ada', 'grace'):
            self.conn.execute(
                'INSERT INTO members (login, first_seen, last_seen, active) '
                "VALUES (?, '2025-01-01T00:00:00Z', '2026-08-17T00:00:00Z', 1)",
                (login,))
        self.conn.execute(
            "INSERT INTO teams (slug, name, first_seen, last_seen, active) "
            "VALUES ('platform','Platform','2025-01-01T00:00:00Z',"
            "'2026-08-17T00:00:00Z',1)")
        self.conn.execute(
            "INSERT INTO team_members (team_slug, login, first_seen, last_seen, "
            "active) VALUES ('platform','ada','2025-01-01T00:00:00Z',"
            "'2026-08-17T00:00:00Z',1)")

        # ada: two commits in alpha, one on 2026-08-17 late enough that its
        # local date differs from its UTC date.
        self.commit('c1', self.alpha, 'ada', '2026-08-17T09:00:00Z',
                    'fix(INV-42): the thing', 10, 2)
        self.commit('c2', self.alpha, 'ada', '2026-08-17T22:30:00Z',
                    'chore: late night', 1, 1)
        # grace: one commit in beta, one bot commit that must not be counted.
        self.commit('c3', self.beta, 'grace', '2026-08-16T12:00:00Z',
                    'WARRANTY-99 other thing', 5, 0)
        self.commit('c4', self.beta, 'renovate[bot]', '2026-08-16T13:00:00Z',
                    'bump the dep', 3, 3)

        # A PR opened one day and merged the next -- two events, not one.
        self.conn.execute(
            'INSERT INTO pulls (repo_id, number, author_login, title, state, '
            'created_at, merged_at) VALUES (?,?,?,?,?,?,?)',
            (self.alpha, 1, 'ada', 'INV-42 land the thing', 'MERGED',
             '2026-08-16T08:00:00Z', '2026-08-17T08:00:00Z'))
        # A bot PR under the bare spelling, which a `[bot]` suffix test misses.
        self.conn.execute(
            'INSERT INTO pulls (repo_id, number, author_login, title, state, '
            "created_at) VALUES (?,?,?,?,'OPEN',?)",
            (self.beta, 2, 'renovate', 'bump again', '2026-08-16T09:00:00Z'))

        self.conn.execute(
            'INSERT INTO reviews (id, repo_id, pull_number, author_login, '
            "submitted_at, state) VALUES ('r1',?,1,'grace',?,'APPROVED')",
            (self.alpha, '2026-08-17T09:30:00Z'))
        # A review by a review-only automation.
        self.conn.execute(
            'INSERT INTO reviews (id, repo_id, pull_number, author_login, '
            "submitted_at, state) VALUES ('r2',?,1,'cursor',?,'COMMENTED')",
            (self.alpha, '2026-08-17T09:40:00Z'))

        self.conn.execute(
            "INSERT INTO commit_trailers (repo_id, oid, name, email) "
            "VALUES (?, 'c1', 'Claude', 'noreply@anthropic.com')", (self.alpha,))

        # `JIRA_PROJECT_SEED` ships empty -- keys are org-specific -- so the
        # fixture declares the two projects these tests reference.
        self.conn.executemany(
            'INSERT INTO jira_projects (key, canonical) VALUES (?,?)',
            (('INV', 'INV'), ('WARRANTY', 'WARRANTY'),
             ('WARRENTY', 'WARRANTY')))     # an alias, for the aliases test
        seed_ai_tools(self.conn)          # v_commit_ai joins it; empty means no AI
        rebuild_issue_refs(self.conn)
        rebuild_bot_logins(self.conn)
        self.conn.commit()

    def commit(self, oid, rid, login, when, message, add, delete):
        self.conn.execute(
            'INSERT INTO commits (repo_id, oid, author_login, committed_date, '
            'message, additions, deletions) VALUES (?,?,?,?,?,?,?)',
            (rid, oid, login, when, message, add, delete))

    def events(self, **kw):
        return q.events(self.conn, ORG, q.Filters(tz='Europe/Berlin', **kw))

    def actors(self, **kw):
        return {e['actor'] for e in self.events(**kw)['events']}


class EventStreamTest(StoreFixture):

    def test_a_pull_contributes_both_an_open_and_a_merge(self):
        """Collapsing these onto `created_at` answers "what changed on Tuesday"
        with Monday's PRs."""
        opened = self.events(kinds=('pull',), frm='2026-08-16', to='2026-08-16')
        merged = self.events(kinds=('merge',), frm='2026-08-17', to='2026-08-17')
        self.assertEqual(opened['total'], 1)
        self.assertEqual(merged['total'], 1)
        self.assertEqual(merged['events'][0]['ref'], '1')

    def test_a_merge_is_absent_on_the_day_the_pull_opened(self):
        self.assertEqual(
            self.events(kinds=('merge',), frm='2026-08-16', to='2026-08-16')['total'], 0)

    def test_bots_are_excluded_by_default_in_both_spellings(self):
        """The suffixed commit and the bare pull request must both go."""
        self.assertNotIn('renovate[bot]', self.actors())
        self.assertNotIn('renovate', self.actors())
        self.assertNotIn('cursor', self.actors())

    def test_bots_can_be_asked_for(self):
        self.assertIn('renovate[bot]', self.actors(bots=True))
        self.assertIn('renovate', self.actors(bots=True))

    def test_the_local_day_boundary_moves_an_event(self):
        """`c2` is 22:30 UTC on the 17th, which is 00:30 on the 18th in Berlin.

        This is the case a UTC `date()` grouping gets wrong.
        """
        on_17 = self.events(kinds=('commit',), frm='2026-08-17', to='2026-08-17')
        on_18 = self.events(kinds=('commit',), frm='2026-08-18', to='2026-08-18')
        self.assertEqual({e['ref'] for e in on_17['events']}, {'c1'})
        self.assertEqual({e['ref'] for e in on_18['events']}, {'c2'})

    def test_the_same_instant_lands_on_the_17th_in_utc(self):
        stream = q.events(self.conn, ORG, q.Filters(
            tz='UTC', kinds=('commit',), frm='2026-08-17', to='2026-08-17'))
        self.assertEqual({e['ref'] for e in stream['events']}, {'c1', 'c2'})

    def test_filters_by_user(self):
        self.assertEqual(self.actors(user='ada'), {'ada'})

    def test_filters_by_repo(self):
        repos = {e['repo'] for e in self.events(repo='beta')['events']}
        self.assertEqual(repos, {'beta'})

    def test_filters_by_team_membership(self):
        """ada is on platform; grace is not."""
        self.assertEqual(self.actors(team='platform'), {'ada'})

    def test_filters_by_issue_across_kinds(self):
        """INV-42 is named by a commit and a pull request, so both come back.

        So does the **review** of that pull request, which names nothing itself.
        That is deliberate: reviewing the PR that implements an issue is work on
        the issue, and an issue view that omitted it would describe half the
        lifecycle. It does mean the event count and the reference count answer
        different questions -- see `_issue_breakdown`.
        """
        stream = self.events(issue='INV-42')
        self.assertEqual({(e['kind'], e['ref']) for e in stream['events']},
                         {('commit', 'c1'), ('pull', '1'), ('merge', '1'),
                          ('review', '1')})

    def test_a_review_is_reached_through_its_pull_request(self):
        """grace never wrote the key; she reviewed the PR that carries it."""
        stream = self.events(issue='INV-42', kinds=('review',))
        self.assertEqual({e['actor'] for e in stream['events']}, {'grace'})

    def test_filters_by_project(self):
        self.assertEqual(
            {e['ref'] for e in self.events(project='WARRANTY')['events']}, {'c3'})

    def test_attaches_issue_keys_to_events(self):
        row = next(e for e in self.events(kinds=('commit',))['events']
                   if e['ref'] == 'c1')
        self.assertEqual(row['issues'], ['INV-42'])

    def test_attaches_ai_tools_to_events(self):
        row = next(e for e in self.events(kinds=('commit',))['events']
                   if e['ref'] == 'c1')
        self.assertEqual(row['tools'], ['Claude'])

    def test_ai_only_keeps_assisted_commits(self):
        stream = self.events(kinds=('commit',), ai='only')
        self.assertEqual({e['ref'] for e in stream['events']}, {'c1'})

    def test_ai_none_keeps_the_rest(self):
        stream = self.events(kinds=('commit',), ai='none')
        self.assertEqual({e['ref'] for e in stream['events']}, {'c2', 'c3'})

    def test_ai_only_excludes_kinds_that_cannot_qualify(self):
        """Only commits carry trailers. Returning reviews under an AI filter
        would imply they had been classified."""
        self.assertEqual(self.events(kinds=('review',), ai='only')['total'], 0)

    def test_text_search_matches_a_commit_message(self):
        self.assertEqual(
            {e['ref'] for e in self.events(kinds=('commit',), q='late night')['events']},
            {'c2'})

    def test_text_search_matches_a_pull_title(self):
        self.assertEqual(self.events(kinds=('pull',), q='land the')['total'], 1)

    def test_subject_is_the_first_line_only(self):
        self.commit('c9', self.alpha, 'ada', '2026-08-15T10:00:00Z',
                    'subject line\n\nbody paragraph', 0, 0)
        self.conn.commit()
        row = next(e for e in self.events(kinds=('commit',))['events']
                   if e['ref'] == 'c9')
        self.assertEqual(row['subject'], 'subject line')
        self.assertEqual(row['body'], 'body paragraph')

    def test_builds_a_github_url_per_kind(self):
        commit = next(e for e in self.events(kinds=('commit',))['events']
                      if e['ref'] == 'c1')
        self.assertEqual(commit['url'],
                         f'https://github.com/{ORG}/alpha/commit/c1')
        review = self.events(kinds=('review',))['events'][0]
        self.assertEqual(review['url'], f'https://github.com/{ORG}/alpha/pull/1')

    def test_total_is_independent_of_the_page(self):
        page = self.events(limit=1)
        self.assertEqual(page['returned'], 1)
        self.assertGreater(page['total'], 1)

    def test_paging_does_not_repeat_or_skip(self):
        everything = self.events(limit=100)
        seen = []
        for offset in range(0, everything['total'], 2):
            seen.extend((e['kind'], e['repo'], e['ref'])
                        for e in self.events(limit=2, offset=offset)['events'])
        self.assertEqual(len(seen), everything['total'])
        self.assertEqual(len(set(seen)), everything['total'])

    def test_ordering_is_newest_first(self):
        stamps = [e['at'] for e in self.events()['events']]
        self.assertEqual(stamps, sorted(stamps, reverse=True))


class AggregateTest(StoreFixture):

    def test_totals_count_lines_from_commits_only(self):
        totals = q._totals(self.conn, ORG, q.Filters(tz='Europe/Berlin'))
        self.assertEqual(totals['commits'], 3)          # c4 is a bot
        self.assertEqual(totals['lines_added'], 16)     # 10 + 1 + 5
        self.assertEqual(totals['lines_removed'], 3)    # 2 + 1 + 0
        self.assertEqual(totals['lines_net'], 13)

    def test_by_day_uses_local_dates(self):
        days = {d['day']: d for d in q._by_day(
            self.conn, ORG, q.Filters(tz='Europe/Berlin', kinds=('commit',)))}
        self.assertIn('2026-08-18', days)               # c2, 22:30 UTC
        self.assertEqual(days['2026-08-18']['commit'], 1)

    def test_by_day_carries_the_quiet_days_as_zeroes(self):
        """Without the zeroes a gap draws as no gap: the bars either side end
        up adjacent and a stop-start month reads as a steady one."""
        days = q._by_day(self.conn, ORG, q.Filters(
            tz='Europe/Berlin', frm='2026-08-14', to='2026-08-18'))
        self.assertEqual([d['day'] for d in days],
                         ['2026-08-14', '2026-08-15', '2026-08-16',
                          '2026-08-17', '2026-08-18'])
        quiet = {d['day']: d for d in days}['2026-08-15']
        self.assertEqual(quiet['total'], 0)
        self.assertEqual([quiet[k] for k in q.KINDS], [0, 0, 0, 0])

    def test_an_open_window_does_not_invent_days_before_the_data(self):
        """"All" means every day the store covers, not every day since the
        epoch."""
        days = q._by_day(self.conn, ORG, q.Filters(tz='Europe/Berlin'))
        self.assertEqual(days[0]['day'], '2026-08-16')      # the first event
        self.assertTrue(days[0]['total'])
        self.assertTrue(days[-1]['total'])

    def test_a_window_reaching_into_the_future_stops_at_today(self):
        """A row of empty days after the last one that could exist is not a
        gap in the work, it is the calendar."""
        days = q._by_day(self.conn, ORG, q.Filters(
            tz='Europe/Berlin', frm='2026-08-16', to='2099-01-01'))
        self.assertLess(days[-1]['day'], '2099-01-01')

    def test_an_empty_slice_stays_empty_rather_than_a_flat_row(self):
        """"No activity in this window" is a sentence; a chart of zeroes is
        the same claim made badly."""
        days = q._by_day(self.conn, ORG, q.Filters(
            tz='Europe/Berlin', frm='2020-01-01', to='2020-02-01'))
        self.assertEqual(days, [])

    def test_the_filled_days_do_not_disturb_the_real_ones(self):
        wide = {d['day']: d for d in q._by_day(self.conn, ORG, q.Filters(
            tz='Europe/Berlin', frm='2026-08-01', to='2026-08-18'))}
        self.assertEqual(wide['2026-08-18']['commit'], 1)   # c2, 22:30 UTC
        self.assertEqual(wide['2026-08-17']['added'], 10)

    def test_rhythm_is_broken_down_by_kind(self):
        """A summed rhythm hides the thing worth seeing -- that commits peak in
        the morning and the reviews of them land later."""
        rhythm = q._rhythm(self.conn, ORG, q.Filters(tz='Europe/Berlin'))
        self.assertEqual(sorted(rhythm['by_hour']), sorted(q.KINDS))
        # c1 at 09:00 UTC is 11:00 in Berlin; its review is the same hour.
        self.assertEqual(rhythm['by_hour']['commit']['11'], 1)
        self.assertEqual(rhythm['by_hour']['review']['11'], 1)
        self.assertEqual(rhythm['by_hour']['commit']['12'], 0)

    def test_rhythm_zero_fills_every_hour_and_day(self):
        """A 24-point series with holes is a chart that invents its own gaps."""
        rhythm = q._rhythm(self.conn, ORG, q.Filters(tz='Europe/Berlin'))
        for kind in q.KINDS:
            self.assertEqual(len(rhythm['by_hour'][kind]), 24, kind)
            self.assertEqual(list(rhythm['by_day_of_week'][kind]),
                             list(q.DAYS_OF_WEEK), kind)
        self.assertEqual(rhythm['days_of_week'][0], 'Monday')

    def test_rhythm_respects_the_kind_filter(self):
        rhythm = q._rhythm(self.conn, ORG,
                           q.Filters(tz='Europe/Berlin', kinds=('commit',)))
        self.assertTrue(any(rhythm['by_hour']['commit'].values()))
        self.assertFalse(any(rhythm['by_hour']['review'].values()))

    def test_user_overview_carries_the_window_and_the_roster(self):
        """The People page is also the org-wide page: it draws the same three
        graphs a person's page does, over everyone."""
        bundle = q.user_overview(self.conn, ORG, q.Filters(tz='Europe/Berlin'))
        self.assertEqual(sorted(bundle),
                         ['by_day', 'filters', 'rhythm', 'totals', 'users'])
        self.assertEqual(sorted(u['login'] for u in bundle['users']),
                         ['ada', 'grace'])
        self.assertEqual(bundle['totals']['commits'], 3)
        self.assertTrue(bundle['by_day'])

    def test_user_overview_ignores_a_pinned_person(self):
        """An entry point that narrowed to one person would be a person page
        wearing the wrong title."""
        pinned = q.user_overview(self.conn, ORG,
                                 q.Filters(tz='Europe/Berlin', user='ada'))
        self.assertEqual(sorted(u['login'] for u in pinned['users']),
                         ['ada', 'grace'])
        self.assertIsNone(pinned['filters']['user'])

    def test_the_overview_window_is_what_the_calendar_draws(self):
        """The calendar pads the filter window out to whole weeks, so the
        window has to survive the round trip to the client."""
        bundle = q.user_overview(self.conn, ORG, q.Filters(
            tz='Europe/Berlin', frm='2026-08-01', to='2026-08-18'))
        self.assertEqual(bundle['filters']['frm'], '2026-08-01')
        self.assertEqual(bundle['filters']['to'], '2026-08-18')

    def test_ai_breakdown_counts_commits_not_trailers(self):
        """A session across two models leaves two trailers on one commit."""
        self.conn.execute(
            "INSERT INTO commit_trailers (repo_id, oid, name, email) "
            "VALUES (?, 'c1', 'Claude Opus', 'noreply@anthropic.com')",
            (self.alpha,))
        self.conn.commit()
        ai = q._tool_breakdown(self.conn, ORG, q.Filters(tz='Europe/Berlin'))
        self.assertEqual(ai['assisted'], 1)

    def test_user_detail_reports_teams_and_review_partners(self):
        bundle = q.user_detail(self.conn, ORG, 'ada',
                               q.Filters(tz='Europe/Berlin'))
        self.assertEqual([t['slug'] for t in bundle['user']['teams']], ['platform'])
        self.assertEqual([r['login'] for r in bundle['reviewers']], ['grace'])

    def test_team_detail_rolls_up_per_member(self):
        bundle = q.team_detail(self.conn, ORG, 'platform',
                              q.Filters(tz='Europe/Berlin'))
        roster = {p['login']: p for p in bundle['team']['roster']}
        self.assertEqual(roster['ada']['commits'], 2)
        self.assertTrue(bundle['team']['membership_is_current_only'])

    def test_team_list_counts_members_on_no_team(self):
        listing = q.team_list(self.conn)
        self.assertEqual(listing['unassigned'], 1)      # grace

    def test_day_detail_splits_by_team_including_no_team(self):
        bundle = q.day_detail(self.conn, ORG, '2026-08-16',
                              q.Filters(tz='Europe/Berlin'))
        teams = {t['team'] for t in bundle['day']['by_team']}
        self.assertIn('(no team)', teams)

    def test_issue_detail_spans_repositories_and_people(self):
        bundle = q.issue_detail(self.conn, ORG, 'inv-42',
                               q.Filters(tz='Europe/Berlin'))
        self.assertEqual(bundle['issue']['key'], 'INV-42')
        self.assertEqual(bundle['issue']['project'], 'INV')
        self.assertGreater(bundle['events']['total'], 0)

    def test_project_detail_lists_its_misspellings(self):
        bundle = q.project_detail(self.conn, ORG, 'WARRANTY',
                                  q.Filters(tz='Europe/Berlin'))
        self.assertIn('WARRENTY', bundle['project']['aliases'])

    def test_repo_detail_flags_an_unknown_repository(self):
        bundle = q.repo_detail(self.conn, ORG, 'nope',
                              q.Filters(tz='Europe/Berlin'))
        self.assertFalse(bundle['repo']['known'])

    def test_meta_reports_counts_and_presence(self):
        meta = q.meta(self.conn, ORG, 'Europe/Berlin')
        self.assertEqual(meta['org'], ORG)
        self.assertEqual(meta['counts']['teams'], 1)
        self.assertTrue(meta['issues_present'])

    def test_search_spans_every_dimension(self):
        self.assertEqual(q.search(self.conn, ORG, 'ada')['users'], ['ada'])
        self.assertEqual(q.search(self.conn, ORG, 'alph')['repos'], ['alpha'])
        self.assertEqual(
            [t['slug'] for t in q.search(self.conn, ORG, 'platf')['teams']],
            ['platform'])
        self.assertIn('INV-42', q.search(self.conn, ORG, 'inv-42')['issues'])

    def test_search_on_empty_input_returns_nothing(self):
        self.assertEqual(q.search(self.conn, ORG, '  ')['users'], [])


class RequestParsingTest(unittest.TestCase):
    """The query-string layer, which is the only place bad input arrives."""

    def parse(self, query):
        from urllib.parse import parse_qs
        return server.filters_from(parse_qs(query), 'UTC')

    def test_reads_a_window(self):
        f = self.parse('from=2026-08-01&to=2026-08-17')
        self.assertEqual((f.frm, f.to), ('2026-08-01', '2026-08-17'))

    def test_rejects_a_malformed_date(self):
        with self.assertRaises(server.BadRequest):
            self.parse('from=last-tuesday')

    def test_rejects_a_malformed_number(self):
        with self.assertRaises(server.BadRequest):
            self.parse('limit=lots')

    def test_reads_kinds_as_a_list(self):
        self.assertEqual(self.parse('kinds=commit,merge').kinds,
                         ('commit', 'merge'))

    def test_blank_values_are_treated_as_absent(self):
        self.assertIsNone(self.parse('user=&repo=').user)

    def test_flags_accept_falsey_spellings(self):
        self.assertFalse(self.parse('bots=false').bots)
        self.assertFalse(self.parse('bots=0').bots)
        self.assertTrue(self.parse('bots=1').bots)


class RoutingTest(StoreFixture):
    """Dispatch, without a socket."""

    def setUp(self):
        super().setUp()
        self.store = _FixedStore(self.conn, ORG, 'Europe/Berlin')

    def call(self, path, query=''):
        from urllib.parse import parse_qs
        return server.route_api(self.store, path, parse_qs(query))

    def test_routes_each_collection(self):
        self.assertIn('users', self.call('/api/users'))
        self.assertIn('repos', self.call('/api/repos'))
        self.assertIn('teams', self.call('/api/teams'))
        self.assertIn('projects', self.call('/api/projects'))
        self.assertIn('events', self.call('/api/events'))
        self.assertIn('org', self.call('/api/meta'))

    def test_routes_each_detail_view(self):
        self.assertEqual(self.call('/api/users/ada')['user']['login'], 'ada')
        self.assertEqual(self.call('/api/repos/alpha')['repo']['name'], 'alpha')
        self.assertEqual(self.call('/api/teams/platform')['team']['slug'], 'platform')
        self.assertEqual(self.call('/api/issues/INV-42')['issue']['key'], 'INV-42')
        self.assertEqual(self.call('/api/days/2026-08-17')['day']['date'],
                         '2026-08-17')

    def test_the_people_collection_carries_the_org_wide_graphs(self):
        body = self.call('/api/users')
        for key in ('users', 'totals', 'by_day', 'rhythm', 'filters'):
            self.assertIn(key, body)

    def test_the_event_stream_takes_its_entity_as_a_parameter(self):
        """`/api/events` has no path segment for the entity, which is how the
        client pages a person's stream past the first hundred rows."""
        mine = self.call('/api/events', 'user=ada')
        self.assertTrue(mine['events'])
        self.assertEqual({e['actor'] for e in mine['events']}, {'ada'})

    def test_404s_an_unknown_repository(self):
        with self.assertRaises(server.NotFound):
            self.call('/api/repos/nope')

    def test_404s_an_unknown_team(self):
        with self.assertRaises(server.NotFound):
            self.call('/api/teams/nope')

    def test_404s_an_unknown_endpoint(self):
        with self.assertRaises(server.NotFound):
            self.call('/api/nonsense')

    def test_400s_a_malformed_day(self):
        with self.assertRaises(server.BadRequest):
            self.call('/api/days/tuesday')

    def test_a_user_who_never_committed_is_not_an_error(self):
        """An empty result is a valid answer; only an unknown *entity* is not.

        Repositories and teams are enumerable, so a typo can be caught. A login
        cannot be: someone may simply have done nothing in the window.
        """
        bundle = self.call('/api/users/nobody')
        self.assertEqual(bundle['events']['total'], 0)


class _FixedStore:
    """A `server.Store` that hands out one already-open connection."""

    def __init__(self, conn, org, tz_name):
        self._conn = conn
        self.org = org
        self.tz_name = tz_name

    def conn(self):
        return self._conn


class StaticFileTest(unittest.TestCase):
    """The assets the server ships must exist and be servable."""

    def test_the_shipped_assets_are_present(self):
        for name in ('index.html', 'explorer.css', 'explorer.js', 'charts.js',
                     'vendor/chart.umd.min.js'):
            self.assertTrue((server.STATIC / name).is_file(), name)

    def test_every_script_the_page_asks_for_is_shipped(self):
        """A missing `<script>` is a blank page, and the only thing that
        notices is a browser. Cheaper to notice here."""
        import re
        page = (server.STATIC / 'index.html').read_text()
        for src in re.findall(r'<script src="/([^"]+)"', page):
            self.assertTrue((server.STATIC / src).is_file(), src)
            self.assertIn(Path(src).suffix, server.CONTENT_TYPES, src)

    def test_the_chart_library_is_vendored_not_fetched(self):
        """Everything downstream of the sync is offline by contract, so the
        page must not reach a CDN to draw."""
        for name in ('index.html', 'explorer.js', 'charts.js'):
            source = (server.STATIC / name).read_text()
            self.assertNotIn('src="http', source, name)
            self.assertNotIn('cdn.jsdelivr', source.replace(
                'jsdelivr.net/npm/chart.js', ''), name)

    def test_every_shipped_asset_is_declared_as_package_data(self):
        """`static/*` does not recurse. The vendored bundle lives a directory
        down, so an installed copy would 404 on the chart library while the
        working tree served it happily -- a failure nobody sees until someone
        installs the package rather than running from a checkout.
        """
        try:
            import tomllib
        except ImportError:                       # 3.10 has no tomllib
            self.skipTest('tomllib needs 3.11')
        root = Path(__file__).resolve().parent.parent
        config = tomllib.loads((root / 'pyproject.toml').read_text())
        patterns = config['tool']['setuptools']['package-data']['ghstats.explorer']
        package = server.STATIC.parent
        for asset in sorted(server.STATIC.rglob('*')):
            if not asset.is_file():
                continue
            relative = asset.relative_to(package)
            self.assertTrue(any(relative.match(p) for p in patterns),
                            f'{relative} matches no package-data pattern')

    def test_only_whitelisted_extensions_are_servable(self):
        self.assertIn('.html', server.CONTENT_TYPES)
        self.assertNotIn('.py', server.CONTENT_TYPES)
        self.assertNotIn('.db', server.CONTENT_TYPES)
        # The vendor note sits next to the bundle and is not an asset.
        self.assertNotIn('.md', server.CONTENT_TYPES)

    def test_the_client_never_builds_markup_from_data(self):
        """`innerHTML` with interpolated data is how a commit message becomes
        script. The client uses `textContent` throughout; this pins that.

        Ours only: the vendored bundle is third-party code that renders into a
        canvas, and grepping it would pin someone else's internals.
        """
        for name in ('explorer.js', 'charts.js'):
            self.assertNotIn('innerHTML', (server.STATIC / name).read_text(), name)


if __name__ == '__main__':
    unittest.main()
