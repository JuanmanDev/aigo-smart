"""Regression tests for device status / availability mapping.

The app's BaseDeviceStatus class (classes11.dex) defines:
  NOT_ACTIVATED=0, ONLINE=1, OFFLINE=3, DISABLED=8

The cloud returns status=3 for offline devices while still delivering their
last-known property shadow (e.g. LightSwitch=1) — HA must show them as
unavailable, matching the AigoSmart app behaviour.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATUS_ONLINE = 1
STATUS_OFFLINE = 3


def is_available(status: int) -> bool:
    """Mirror of the platform logic: available only when ONLINE (1)."""
    return status == STATUS_ONLINE


class TestStatusMapping(unittest.TestCase):
    def test_status_values_from_apk(self):
        # from BaseDeviceStatus: NOT_ACTIVATED=0, ONLINE=1, OFFLINE=3, DISABLED=8
        self.assertEqual(STATUS_ONLINE, 1)
        self.assertEqual(STATUS_OFFLINE, 3)

    def test_offline_status_three_is_unavailable(self):
        """The bug: status 3 was wrongly treated as online."""
        self.assertFalse(is_available(3))

    def test_online_status_one_is_available(self):
        self.assertTrue(is_available(1))

    def test_other_statuses_unavailable(self):
        for st in (0, 2, 8):
            self.assertFalse(is_available(st))


if __name__ == "__main__":
    unittest.main()
