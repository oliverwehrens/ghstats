"""`ActivityAnalyzer` counts instants into the reader's day, not UTC's.

The case that matters is the one either side of midnight. A 23:30 commit in
Berlin is 21:30 UTC the same day in winter, so a UTC bucketing is off by two
hours but lands on the right date; a 00:30 commit is 23:30 UTC on the *previous*
date, which moves it to another day, another weekday, and out of the calendar
cell it belongs in. Both are asserted here, because only the second one looks
obviously broken.
"""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from ghstats.analysis import ActivityAnalyzer

# 2026-01-14T23:30Z. Berlin is UTC+1 in January, so this is 00:30 on the 15th
# there -- a Thursday, where UTC says late Wednesday.
LATE = datetime(2026, 1, 14, 23, 30, tzinfo=timezone.utc)

# 2026-07-15T21:30Z, which is 23:30 on the same day in Berlin (UTC+2 in July).
# Same date either way; only the hour moves.
EVENING = datetime(2026, 7, 15, 21, 30, tzinfo=timezone.utc)


class FakeClient:
    """The slice of the client interface `ActivityAnalyzer` actually touches."""

    def __init__(self, commits=(), created=(), reviews=()):
        self.repo = SimpleNamespace(name='acme/widget')
        self._commits = [SimpleNamespace(date=at) for at in commits]
        self._created = [SimpleNamespace(created_at=at, merged=False,
                                         merged_at=None) for at in created]
        self._reviews = [SimpleNamespace(state='APPROVED', submitted_at=at,
                                         pr_number=n)
                         for n, at in enumerate(reviews, 1)]

    def get_organization_repos(self, org, force_refresh=False):
        return [self.repo]

    def get_user_commits(self, repo, username, since, until):
        return self._commits

    def get_commit_stats(self, commit):
        return (1, 0)

    def get_pull_requests_created(self, repo, username, since, until):
        return self._created

    def get_pull_requests_reviewed(self, repo, username, since, until):
        return []

    def get_reviews_by_user(self, repo, username, since, until):
        return self._reviews


def analyze(client, tz_name):
    return ActivityAnalyzer(client, quiet=True, tz_name=tz_name) \
        .analyze_user_activity('acme', 'ada',
                               datetime(2026, 1, 1, tzinfo=timezone.utc),
                               datetime(2026, 12, 31, tzinfo=timezone.utc))


class CommitBucketTest(unittest.TestCase):

    def test_a_commit_after_midnight_local_lands_on_the_local_day(self):
        metrics = analyze(FakeClient(commits=[LATE]), 'Europe/Berlin')
        self.assertEqual(metrics['commits']['by_day_of_week'], {'Thursday': 1})
        self.assertEqual(metrics['commits']['by_hour'], {0: 1})
        self.assertEqual(metrics['activity_by_date'], {'2026-01-15': 1})

    def test_utc_still_available_and_still_answers_differently(self):
        """Proof the zone is doing the work, not a coincidence of the fixture."""
        metrics = analyze(FakeClient(commits=[LATE]), 'UTC')
        self.assertEqual(metrics['commits']['by_day_of_week'], {'Wednesday': 1})
        self.assertEqual(metrics['commits']['by_hour'], {23: 1})
        self.assertEqual(metrics['activity_by_date'], {'2026-01-14': 1})

    def test_summer_time_is_a_different_offset_than_winter(self):
        """A fixed +01:00 would put this evening commit at 22:30, not 23:30."""
        metrics = analyze(FakeClient(commits=[EVENING]), 'Europe/Berlin')
        self.assertEqual(metrics['commits']['by_hour'], {23: 1})
        self.assertEqual(metrics['activity_by_date'], {'2026-07-15': 1})


class PullAndReviewBucketTest(unittest.TestCase):

    def test_pull_requests_follow_the_same_zone(self):
        metrics = analyze(FakeClient(created=[LATE]), 'Europe/Berlin')
        self.assertEqual(metrics['pull_requests']['created_by_day_of_week'],
                         {'Thursday': 1})
        self.assertEqual(metrics['pull_requests']['created_by_hour'], {0: 1})

    def test_reviews_follow_the_same_zone(self):
        metrics = analyze(FakeClient(reviews=[LATE]), 'Europe/Berlin')
        self.assertEqual(metrics['reviews']['by_day_of_week'], {'Thursday': 1})
        self.assertEqual(metrics['reviews']['approvals_by_hour'], {0: 1})
        self.assertEqual(metrics['pull_requests']['reviewed_by_hour'], {0: 1})


class ReportedZoneTest(unittest.TestCase):

    def test_the_metrics_say_which_zone_they_were_counted_in(self):
        """A histogram without its zone cannot be read, or re-bucketed later."""
        self.assertEqual(analyze(FakeClient(), 'Europe/Berlin')['timezone'],
                         'Europe/Berlin')

    def test_an_unknown_zone_degrades_to_utc_rather_than_raising(self):
        metrics = analyze(FakeClient(commits=[LATE]), 'Mars/Olympus')
        self.assertEqual(metrics['commits']['by_hour'], {23: 1})

    def test_naive_timestamps_are_read_as_utc_then_converted(self):
        """The cache has held naive rows; they must not be read as local."""
        naive = LATE.replace(tzinfo=None)
        metrics = analyze(FakeClient(commits=[naive]), 'Europe/Berlin')
        self.assertEqual(metrics['activity_by_date'], {'2026-01-15': 1})


if __name__ == '__main__':
    unittest.main()
