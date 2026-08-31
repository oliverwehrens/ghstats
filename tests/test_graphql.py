"""Tests for GraphQL client error handling.

Run: python -m unittest tests.test_graphql -v
"""
import unittest
from datetime import timedelta
from unittest import mock

from ghstats.github import graphql as github_graphql
from ghstats.github.graphql import GitHubGraphQL, GraphQLError, GraphQLTransportError

SECONDARY_BODY = (
    '{"message": "You have exceeded a secondary rate limit. Please wait a few '
    'minutes before you try again.", "documentation_url": "https://docs.github.com/"}'
)


def response(status, body=None, json_body=None, headers=None):
    """Build a stand-in for a requests.Response."""
    fake = mock.Mock()
    fake.status_code = status
    fake.text = body if body is not None else ''
    fake.headers = headers or {}
    if json_body is not None:
        fake.json.return_value = json_body
        fake.text = str(json_body)
    else:
        fake.json.side_effect = ValueError('no json')
    return fake


OK = {'data': {'rateLimit': {'remaining': 4999, 'cost': 1, 'resetAt': None}, 'x': 1}}


class SecondaryRateLimitTest(unittest.TestCase):
    """403 secondary limits must back off, not fail the repo."""

    def setUp(self):
        # Keep the test fast: no real sleeping.
        patcher = mock.patch.object(github_graphql, 'SECONDARY_LIMIT_BACKOFF', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleep = mock.patch.object(github_graphql.time, 'sleep').start()
        self.addCleanup(mock.patch.stopall)

    def test_detects_secondary_limit(self):
        client = GitHubGraphQL('t')
        self.assertTrue(client._is_secondary_limit(response(403, SECONDARY_BODY)))
        self.assertTrue(client._is_secondary_limit(response(429, SECONDARY_BODY)))

    def test_plain_403_is_not_a_secondary_limit(self):
        """A genuine permission error must still fail loudly."""
        client = GitHubGraphQL('t')
        self.assertFalse(
            client._is_secondary_limit(response(403, '{"message": "Bad credentials"}'))
        )

    def test_retries_through_secondary_limit(self):
        client = GitHubGraphQL('t')
        session = mock.Mock()
        session.post.side_effect = [
            response(403, SECONDARY_BODY),
            response(200, json_body=OK),
        ]
        with mock.patch.object(client, '_session', return_value=session):
            data = client.query('{x}', label='t')
        self.assertEqual(data['x'], 1)
        self.assertEqual(client.secondary_hits, 1)

    def test_retries_even_when_caller_disabled_retries(self):
        """Batch queries pass max_retries=1; a secondary limit still waits.

        Splitting the batch or failing the repo would not help -- the limit is
        about request rate, not query size.
        """
        client = GitHubGraphQL('t')
        session = mock.Mock()
        session.post.side_effect = [
            response(403, SECONDARY_BODY),
            response(403, SECONDARY_BODY),
            response(200, json_body=OK),
        ]
        with mock.patch.object(client, '_session', return_value=session):
            data = client.query('{x}', label='t', max_retries=1)
        self.assertEqual(data['x'], 1)
        self.assertEqual(client.secondary_hits, 2)

    def test_pause_is_shared_across_threads(self):
        """One thread tripping the limit must stall the others."""
        client = GitHubGraphQL('t')
        client._trip_secondary_limit(response(403, SECONDARY_BODY,
                                              headers={'Retry-After': '30'}))
        self.assertGreater(client._pause_until, 0)

    def test_honours_retry_after(self):
        client = GitHubGraphQL('t')
        wait = client._trip_secondary_limit(
            response(403, SECONDARY_BODY, headers={'Retry-After': '45'})
        )
        self.assertGreater(wait, 40)

    def test_plain_403_still_raises(self):
        client = GitHubGraphQL('t')
        session = mock.Mock()
        session.post.return_value = response(403, '{"message": "Bad credentials"}')
        with mock.patch.object(client, '_session', return_value=session):
            with self.assertRaises(GraphQLTransportError):
                client.query('{x}', label='t')


class PointBudgetTest(unittest.TestCase):
    """The hourly point budget must block, not merely be observed.

    This is the path a multi-hour rebuild depends on: the org is far larger than
    one 5000-point window, so the sweep has to sit out several resets.
    """

    def setUp(self):
        self.sleeps = []
        patcher = mock.patch.object(
            github_graphql.time, 'sleep', side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(mock.patch.stopall)

    def _client(self, remaining, reset_in_seconds):
        client = GitHubGraphQL('t', rate_limit_floor=200)
        client._remaining = remaining
        if reset_in_seconds is not None:
            client._reset_at = (github_graphql.datetime.now(github_graphql.timezone.utc)
                                + timedelta(seconds=reset_in_seconds))
        return client

    def test_does_not_wait_when_budget_is_healthy(self):
        self._client(4000, 600)._await_budget()
        self.assertEqual(self.sleeps, [])

    def test_waits_until_reset_when_budget_exhausted(self):
        """Below the floor with 30 min to reset: sleep, do not query.

        The mock advances the clock; without that the guard loops forever,
        which is the intended behaviour -- it must not fall through and query.
        """
        client = self._client(50, 1800)
        naps = []

        def advance(seconds):
            naps.append(seconds)
            # Roll the window over so the guard can exit on its next pass.
            client._reset_at = (
                github_graphql.datetime.now(github_graphql.timezone.utc)
                - timedelta(seconds=1)
            )

        with mock.patch.object(github_graphql.time, 'sleep', side_effect=advance):
            client._await_budget()

        self.assertEqual(len(naps), 1)
        # Capped at 300s per nap so the loop can re-check rather than
        # oversleeping a full hour in one call.
        self.assertLessEqual(naps[0], 300)
        self.assertIsNone(client._remaining)

    def test_guard_does_not_fall_through_while_throttled(self):
        """While below the floor and before reset, the guard never returns."""
        client = self._client(50, 1800)
        naps = []
        with mock.patch.object(github_graphql.time, 'sleep',
                               side_effect=lambda s: naps.append(s)):
            with self.assertRaises(RuntimeError):
                # Trip out of the otherwise-infinite wait after a few passes.
                def bail(seconds):
                    naps.append(seconds)
                    if len(naps) > 3:
                        raise RuntimeError('still waiting')
                with mock.patch.object(github_graphql.time, 'sleep',
                                       side_effect=bail):
                    client._await_budget()
        self.assertGreater(len(naps), 3)

    def test_resumes_after_reset_passes(self):
        """Once the window rolls over, state clears and work continues."""
        client = self._client(50, -1)
        client._await_budget()
        self.assertEqual(self.sleeps, [])
        self.assertIsNone(client._remaining)

    def test_low_budget_with_unknown_reset_waits(self):
        """Fail closed: never charge ahead just because resetAt is missing."""
        client = self._client(50, None)
        # Second pass reports a healthy budget so the loop can terminate.
        def refill(_):
            client._remaining = 4000
        with mock.patch.object(github_graphql.time, 'sleep', side_effect=refill):
            client._await_budget()
        self.assertEqual(client._remaining, 4000)

    def test_budget_is_checked_before_every_query(self):
        client = self._client(4000, 600)
        session = mock.Mock()
        session.post.return_value = response(200, json_body=OK)
        with mock.patch.object(client, '_session', return_value=session):
            with mock.patch.object(client, '_await_budget') as guard:
                client.query('{x}')
                client.query('{x}')
        self.assertEqual(guard.call_count, 2)

    def test_remaining_tracks_the_response(self):
        client = GitHubGraphQL('t')
        session = mock.Mock()
        session.post.return_value = response(200, json_body={
            'data': {'rateLimit': {'remaining': 137, 'cost': 3,
                                   'resetAt': '2026-08-17T15:00:00Z'}}})
        with mock.patch.object(client, '_session', return_value=session):
            client.query('{x}')
        self.assertEqual(client.remaining, 137)
        self.assertEqual(client.points_spent, 3)


class ErrorArrayTest(unittest.TestCase):
    """HTTP 200 with an `errors` array is a failure, not a success."""

    def setUp(self):
        mock.patch.object(github_graphql.time, 'sleep').start()
        self.addCleanup(mock.patch.stopall)

    def test_errors_array_raises(self):
        client = GitHubGraphQL('t')
        session = mock.Mock()
        session.post.return_value = response(200, json_body={
            'data': {'a0': None},
            'errors': [{'type': 'NOT_FOUND', 'message': 'no such repo'}],
        })
        with mock.patch.object(client, '_session', return_value=session):
            with self.assertRaises(GraphQLError) as caught:
                client.query('{x}', label='t')
        self.assertIn('NOT_FOUND', str(caught.exception))

    def test_resource_limits_not_retried(self):
        """Oversized queries need a smaller batch, not another attempt."""
        client = GitHubGraphQL('t')
        session = mock.Mock()
        session.post.return_value = response(200, json_body={
            'data': None,
            'errors': [{'type': 'RESOURCE_LIMITS_EXCEEDED', 'message': 'too big'}],
        })
        with mock.patch.object(client, '_session', return_value=session):
            with self.assertRaises(GraphQLError):
                client.query('{x}', label='t')
        self.assertEqual(session.post.call_count, 1)

    def test_502_retries_then_succeeds(self):
        client = GitHubGraphQL('t')
        session = mock.Mock()
        session.post.side_effect = [response(502), response(200, json_body=OK)]
        with mock.patch.object(client, '_session', return_value=session):
            self.assertEqual(client.query('{x}', label='t')['x'], 1)


if __name__ == '__main__':
    unittest.main()
