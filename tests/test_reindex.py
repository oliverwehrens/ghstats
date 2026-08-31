"""Tests for trailer parsing and identity classification.

Run: python -m unittest tests.test_reindex -v
"""
import unittest

from ghstats.reindex import is_bot_login, parse_trailers

CLAUDE = 'noreply@anthropic.com'


class TrailerParsingTest(unittest.TestCase):

    def test_extracts_a_plain_trailer(self):
        self.assertEqual(
            parse_trailers('Fix it\n\nCo-authored-by: Claude <%s>' % CLAUDE),
            [('Claude', CLAUDE)])

    def test_is_case_insensitive_on_the_token(self):
        self.assertEqual(
            parse_trailers('x\n\nCo-Authored-By: C <%s>' % CLAUDE)[0][1], CLAUDE)

    def test_lowercases_the_address_but_keeps_the_name(self):
        name, email = parse_trailers(
            'x\n\nCo-authored-by: Claude Opus 4.8 (1M context) <NoReply@Anthropic.COM>')[0]
        self.assertEqual(name, 'Claude Opus 4.8 (1M context)')
        self.assertEqual(email, CLAUDE)

    def test_tolerates_an_indented_trailer(self):
        """Four real commits indent the whole body by four spaces."""
        self.assertEqual(
            parse_trailers('x\n\n    Co-Authored-By: Claude Sonnet 4.6 <%s>' % CLAUDE),
            [('Claude Sonnet 4.6', CLAUDE)])

    def test_tolerates_crlf(self):
        self.assertEqual(
            parse_trailers('x\r\n\r\nCo-authored-by: Claude <%s>\r\n' % CLAUDE),
            [('Claude', CLAUDE)])

    def test_collects_every_trailer_on_a_commit(self):
        """A session spanning two models leaves two trailers."""
        got = parse_trailers(
            'x\n\nCo-authored-by: Claude Opus 4.8 <%s>\n'
            'Co-authored-by: Cursor <cursoragent@cursor.com>' % CLAUDE)
        self.assertEqual([e for _, e in got],
                         [CLAUDE, 'cursoragent@cursor.com'])

    def test_ignores_conventional_commit_prefixes(self):
        """`feat:` has the shape of a trailer but is a subject line."""
        self.assertEqual(parse_trailers('feat: add a thing\n\nfix: another'), [])

    def test_ignores_a_mention_without_angle_brackets(self):
        self.assertEqual(
            parse_trailers('Discussed co-authored-by: Claude, no address'), [])


class BotClassificationTest(unittest.TestCase):

    def test_bracket_suffix_is_a_bot(self):
        for login in ('lovable-dev[bot]', 'renovate[bot]', 'dependabot[bot]',
                      'github-actions[bot]', 'phrase-strings-eu[bot]'):
            self.assertTrue(is_bot_login(login), login)

    def test_named_exceptions_are_bots(self):
        for login in ('semantic-release-bot', 'goreleaserbot',
                      'renovate-approve', 'copilot-pull-request-reviewer'):
            self.assertTrue(is_bot_login(login), login)

    def test_a_login_containing_bot_is_still_a_person(self):
        """One real org had a prolific committer behind such a login. A `bot`
        substring test loses every one of their commits."""
        self.assertFalse(is_bot_login('robotnik'))

    def test_ordinary_logins_are_people(self):
        for login in ('ada', 'grace-h', 'a-long-hyphenated-login'):
            self.assertFalse(is_bot_login(login), login)


if __name__ == '__main__':
    unittest.main()
