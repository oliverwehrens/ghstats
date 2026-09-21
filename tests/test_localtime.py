"""Which zone the tools display in, and what happens when it cannot be named.

The failure that matters here is silent: a resolver that quietly returns UTC on
a machine set to Berlin produces histograms that are wrong by an hour or two
and look entirely plausible. So the fallback is asserted as deliberately as the
success.
"""
import os
import unittest
import unittest.mock
from datetime import datetime, timezone

from ghstats import localtime


class LocalZoneNameTest(unittest.TestCase):

    def _without_files(self):
        """Silence `/etc/localtime` and `/etc/timezone` for the duration.

        Otherwise the host's own configuration answers before the fallback
        under test is ever reached, and the assertion passes for the wrong
        reason on a developer machine and fails in a UTC container.
        """
        return (unittest.mock.patch.object(localtime.Path, 'is_symlink',
                                           return_value=False),
                unittest.mock.patch.object(localtime.Path, 'read_text',
                                           side_effect=OSError))

    def test_tz_env_names_the_zone(self):
        with unittest.mock.patch.dict(os.environ, {'TZ': 'Europe/Berlin'}):
            self.assertEqual(localtime.local_zone_name(), 'Europe/Berlin')

    def test_a_leading_colon_is_tolerated(self):
        """`TZ=:Europe/Berlin` is a legal spelling that zoneinfo rejects."""
        with unittest.mock.patch.dict(os.environ, {'TZ': ':Europe/Berlin'}):
            self.assertEqual(localtime.local_zone_name(), 'Europe/Berlin')

    def test_a_nonsense_tz_falls_through_to_the_files(self):
        """A bad `TZ` should not shadow a perfectly good `/etc/timezone`."""
        with unittest.mock.patch.dict(os.environ, {'TZ': 'Mars/Olympus'}), \
                unittest.mock.patch.object(localtime.Path, 'is_symlink',
                                           return_value=False), \
                unittest.mock.patch.object(localtime.Path, 'read_text',
                                           return_value='Asia/Tokyo\n'):
            self.assertEqual(localtime.local_zone_name(), 'Asia/Tokyo')

    def test_utc_when_nothing_names_a_zone(self):
        symlink, read_text = self._without_files()
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
                symlink, read_text:
            self.assertEqual(localtime.local_zone_name(), 'UTC')

    def test_an_unreadable_etc_is_not_fatal(self):
        """A container with no `/etc/timezone` should still start."""
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
                unittest.mock.patch.object(localtime.Path, 'is_symlink',
                                           side_effect=OSError), \
                unittest.mock.patch.object(localtime.Path, 'read_text',
                                           side_effect=OSError):
            self.assertEqual(localtime.local_zone_name(), 'UTC')


class ZoneTest(unittest.TestCase):

    def test_a_named_zone_carries_its_dst_history(self):
        """The point of a name over an offset: summer and winter differ."""
        berlin = localtime.zone('Europe/Berlin')
        january = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).astimezone(berlin)
        july = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).astimezone(berlin)
        self.assertEqual(january.hour, 13)
        self.assertEqual(july.hour, 14)

    def test_an_unknown_zone_falls_back_rather_than_raising(self):
        self.assertEqual(localtime.zone('Mars/Olympus'), timezone.utc)
        self.assertEqual(localtime.zone(''), timezone.utc)


if __name__ == '__main__':
    unittest.main()
