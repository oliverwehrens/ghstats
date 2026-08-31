"""Tests for Jira issue extraction and bot identification.

Run: python -m unittest tests.test_issue_refs -v

Both are derived tables, so both are wrong in the same way if the parser is
naive: an open regex invents projects that do not exist, and a `[bot]` suffix
test finds bot commits while missing every bot pull request. The cases below are
taken from the real store, which is where each mistake was found.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghstats.reindex import (parse_issue_keys, rebuild_bot_logins,
                             rebuild_issue_refs, seed_jira_projects,
                             unknown_prefixes)
from ghstats.store import sqlite as sqlite_store
from ghstats.store.sqlite import connect, repo_id


# Fictional projects, one per trap: a transposed spelling, a dropped leading
# letter, and a zero typed for an O. Nothing here depends on the shipped
# `JIRA_PROJECT_SEED`, which is empty -- keys are org-specific.
PROJECTS = {
    'WARRANTY': 'WARRANTY', 'WARRENTY': 'WARRANTY', 'ARRANTY': 'WARRANTY',
    'INV': 'INV', 'SEO': 'SEO', 'SE0': 'SEO', 'PL': 'PL',
}


class ParseIssueKeysTest(unittest.TestCase):
    """The whitelist is what makes the permissive pattern safe."""

    def keys(self, text):
        return [key for key, _ in parse_issue_keys(text, PROJECTS)]

    def test_finds_a_plain_key(self):
        self.assertEqual(self.keys('WARRANTY-4131 fix the thing'),
                         ['WARRANTY-4131'])

    def test_finds_a_key_inside_a_conventional_commit_prefix(self):
        self.assertEqual(self.keys('chore(INV-4549): add API key'), ['INV-4549'])

    def test_finds_a_key_inside_a_merged_branch_name(self):
        self.assertEqual(
            self.keys('Merge pull request #900 from acme/chore/INV-4549/add-key'),
            ['INV-4549'])

    def test_matches_lowercase(self):
        """533 keys in the real store are lowercase, usually via a branch name.

        A case-sensitive pattern loses all of them, and the whitelist is what
        makes case-insensitivity safe -- `utf-8` still resolves to no project.
        """
        self.assertEqual(self.keys('chore(inv-4549): y'), ['INV-4549'])
        self.assertEqual(self.keys('unclaimed-1'), [])  # not in this fixture

    def test_folds_a_misspelling_onto_the_real_project(self):
        """One project reached five spellings in the real store. It is one project."""
        self.assertEqual(self.keys('WARRENTY-4131'), ['WARRANTY-4131'])
        self.assertEqual(self.keys('ARRANTY-4131'), ['WARRANTY-4131'])
        self.assertEqual(self.keys('SE0-3212'), ['SEO-3212'])

    def test_a_misspelling_and_its_correction_are_one_issue(self):
        self.assertEqual(self.keys('WARRANTY-1 supersedes WARRENTY-1'),
                         ['WARRANTY-1'])

    def test_rejects_standards_that_look_like_keys(self):
        """The reason the prefix cannot decide on its own.

        An open `[A-Z]{2,}-[0-9]+` matches 132 prefixes across the real commit
        messages; most are these.
        """
        for text in ('encode as ISO-8601', 'requires HTTP-2', 'AES-256-GCM',
                     'see RFC-3339', 'autoload PSR-4', 'per ADR-001',
                     'release R2025-04', 'blocked by P1-3', 'UTF-8 only',
                     'SHA-1 digest'):
            self.assertEqual(self.keys(text), [], text)

    def test_normalises_zero_padding(self):
        """Jira does not pad, so the padded spelling is the same issue."""
        self.assertEqual(self.keys('INV-0042'), ['INV-42'])

    def test_deduplicates_but_keeps_first_mention_first(self):
        self.assertEqual(self.keys('PL-2 and INV-1 then PL-2 again'),
                         ['PL-2', 'INV-1'])

    def test_survives_empty_and_none(self):
        self.assertEqual(parse_issue_keys('', PROJECTS), [])
        self.assertEqual(parse_issue_keys(None, PROJECTS), [])

    def test_returns_the_canonical_project_alongside_the_key(self):
        self.assertEqual(parse_issue_keys('WARRENTY-9', PROJECTS),
                         [('WARRANTY-9', 'WARRANTY')])


class IssueRefTableTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = connect(str(Path(self.dir.name) / 'store.db'))
        self.addCleanup(self.conn.close)
        self.rid = repo_id(self.conn, 'acme', 'thing')
        self.conn.executemany(
            'INSERT INTO jira_projects (key, canonical) VALUES (?,?)',
            sorted(PROJECTS.items()))

    def commit(self, oid, message, login='ada'):
        self.conn.execute(
            'INSERT INTO commits (repo_id, oid, author_login, committed_date, '
            'message) VALUES (?,?,?,?,?)',
            (self.rid, oid, login, '2026-08-17T10:00:00Z', message))

    def pull(self, number, title, login='ada'):
        self.conn.execute(
            'INSERT INTO pulls (repo_id, number, author_login, title, state, '
            "created_at) VALUES (?,?,?,?,'MERGED',?)",
            (self.rid, number, login, title, '2026-08-17T10:00:00Z'))

    def test_indexes_commits_and_pulls(self):
        self.commit('a1', 'fix(INV-42): thing')
        self.pull(7, 'WARRANTY-99 add the other thing')
        stats = rebuild_issue_refs(self.conn)
        self.assertEqual(stats['commit_refs'], 1)
        self.assertEqual(stats['pull_refs'], 1)
        self.assertEqual(stats['issues'], 2)

        rows = {(r['kind'], r['ref'], r['issue_key']) for r in
                self.conn.execute('SELECT kind, ref, issue_key FROM issue_refs')}
        self.assertIn(('commit', 'a1', 'INV-42'), rows)
        self.assertIn(('pull', '7', 'WARRANTY-99'), rows)

    def test_a_pull_ref_joins_back_through_a_cast(self):
        """`ref` is TEXT for both kinds, so the documented join must work."""
        self.pull(7, 'INV-42 thing')
        rebuild_issue_refs(self.conn)
        row = self.conn.execute("""
            SELECT p.title FROM issue_refs i
            JOIN pulls p ON p.repo_id = i.repo_id
                        AND p.number = CAST(i.ref AS INTEGER)
            WHERE i.kind = 'pull'""").fetchone()
        self.assertEqual(row['title'], 'INV-42 thing')

    def test_is_idempotent(self):
        self.commit('a1', 'INV-42')
        first = rebuild_issue_refs(self.conn)
        second = rebuild_issue_refs(self.conn)
        self.assertEqual(first, second)

    def test_adding_a_project_reclassifies_existing_history(self):
        """The payoff for deriving this offline: no re-sync, no API call."""
        self.commit('a1', 'RIVER-280 ingest the channel')
        self.assertEqual(rebuild_issue_refs(self.conn)['refs'], 0)

        self.conn.execute(
            "INSERT INTO jira_projects (key, canonical) VALUES ('RIVER','RIVER')")
        self.assertEqual(rebuild_issue_refs(self.conn)['refs'], 1)

    def test_a_hand_added_project_survives_a_reseed(self):
        """Seeding is INSERT OR IGNORE, so a local mapping is never clobbered."""
        self.conn.execute(
            "INSERT INTO jira_projects (key, canonical) VALUES ('ZZZ','ZZZ')")
        with mock.patch.object(sqlite_store, 'JIRA_PROJECT_SEED',
                               {'ZZZ': 'SOMETHING-ELSE', 'NEW': 'NEW'}):
            seed_jira_projects(self.conn)
        rows = dict(self.conn.execute(
            'SELECT key, canonical FROM jira_projects'))
        self.assertEqual(rows['ZZZ'], 'ZZZ')    # the hand mapping wins
        self.assertEqual(rows['NEW'], 'NEW')    # a genuinely new seed row lands

    def test_unknown_prefixes_ranks_candidates(self):
        self.commit('a1', 'RIVER-1 x')
        self.commit('a2', 'RIVER-2 y')
        self.commit('a3', 'ISO-8601 z')
        ranked = dict(unknown_prefixes(self.conn))
        self.assertEqual(ranked['RIVER'], 2)
        self.assertIn('ISO', ranked)               # noise is reported, not hidden
        self.assertNotIn('INV', ranked)            # a known project is not "unknown"


class BotLoginTest(unittest.TestCase):
    """Bot identity, from evidence plus the curated list."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = connect(str(Path(self.dir.name) / 'store.db'))
        self.addCleanup(self.conn.close)
        self.rid = repo_id(self.conn, 'acme', 'thing')

    def member(self, login):
        self.conn.execute(
            'INSERT INTO members (login, first_seen, last_seen, active) '
            "VALUES (?, '2025-01-01T00:00:00Z', '2026-08-17T00:00:00Z', 1)",
            (login,))

    def commit(self, oid, login):
        self.conn.execute(
            'INSERT INTO commits (repo_id, oid, author_login, committed_date) '
            'VALUES (?,?,?,?)', (self.rid, oid, login, '2026-08-17T10:00:00Z'))

    def pull(self, number, login):
        self.conn.execute(
            'INSERT INTO pulls (repo_id, number, author_login, state, created_at) '
            "VALUES (?,?,?,'MERGED',?)",
            (self.rid, number, login, '2026-08-17T10:00:00Z'))

    def logins(self):
        return {r['login'] for r in self.conn.execute(
            'SELECT login FROM bot_logins')}

    def test_records_both_spellings_of_one_bot(self):
        """The miss this table exists to close.

        GraphQL resolves commit authorship to the account record, which carries
        the suffix, but `PullRequest.author` returns the bare handle. Renovate
        opened 15,016 PRs in the real store under the bare spelling.
        """
        self.commit('a1', 'renovate[bot]')
        self.pull(1, 'renovate')
        rebuild_bot_logins(self.conn)
        self.assertIn('renovate[bot]', self.logins())
        self.assertIn('renovate', self.logins())

    def test_does_not_classify_a_person_whose_name_contains_bot(self):
        """A substring test on `bot` cost one real org a person with 2453 commits."""
        self.member('robotnik')
        self.commit('a1', 'robotnik')
        rebuild_bot_logins(self.conn)
        self.assertNotIn('robotnik', self.logins())

    def test_a_member_is_never_adopted_from_a_bare_form(self):
        """The membership guard: a person is not a bot because a bot shares
        their name."""
        self.member('renovate')
        self.commit('a1', 'renovate[bot]')
        rebuild_bot_logins(self.conn)
        self.assertIn('renovate[bot]', self.logins())
        self.assertNotIn('renovate', self.logins())

    def test_curated_bots_are_added_even_when_they_are_members(self):
        """A release bot can hold an org seat and still be an automation, so the
        membership guard must not apply to the curated list."""
        self.member('semantic-release-bot')
        rebuild_bot_logins(self.conn)
        self.assertIn('semantic-release-bot', self.logins())

    def test_includes_the_review_only_automations(self):
        """These never appear bracketed anywhere, so evidence cannot find them.

        Together they authored 14% of all reviews in the real store.
        """
        rebuild_bot_logins(self.conn)
        for login in ('cursor', 'copilot-pull-request-reviewer',
                      'renovate-approve', 'claude'):
            self.assertIn(login, self.logins())

    def test_is_idempotent(self):
        self.commit('a1', 'renovate[bot]')
        rebuild_bot_logins(self.conn)
        first = self.logins()
        rebuild_bot_logins(self.conn)
        self.assertEqual(first, self.logins())

    def test_every_curated_login_is_recorded(self):
        rebuild_bot_logins(self.conn)
        self.assertTrue(sqlite_store.BOT_LOGINS <= self.logins())


if __name__ == '__main__':
    unittest.main()
