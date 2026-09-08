"""Tests for the commander: rapid consecutive changes coalesce correctly.

Simulates exactly the user's scenario: dragging brightness/CCT sliders
produces a burst of turn_on() calls. Verifies:
  * all bursts coalesce into ordered writes (final values, never stale ones)
  * the device receives the LAST value for each property
  * retry logic keeps failed writes pending instead of dropping them
  * skip window opens after a write (stale shadow can't overwrite)
  * _matches() verifies shadow consistency properly
"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "custom_components", "aigosmart"))

from commander import DEBOUNCE, AigoCommander, _matches  # noqa: E402


class FakeHass:
    def async_add_executor_job(self, fn, *args):
        # run the (fake) sync client call immediately
        return asyncio.get_event_loop().run_in_executor(None, fn, *args)


class FakeClient:
    def __init__(self):
        self.writes: list[dict] = []
        self.fail_next = 0
        self.shadow: dict = {}

    def set_properties_prefer_local(self, iot_id, items):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("network down")
        self.writes.append(dict(items))
        self.shadow.update(items)
        return {"code": 200}

    def get_properties_prefer_local(self, iot_id):
        return dict(self.shadow)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestCommander(unittest.TestCase):
    def test_rapid_slider_burst_coalesces(self):
        """20 brightness writes in a burst -> ordered writes, last wins."""
        client = FakeClient()
        cmd = AigoCommander(FakeHass(), client, "iot1")

        async def scenario():
            # simulate 20 slider positions arriving quickly
            tasks = [cmd.async_send({"Brightness": v}) for v in range(1, 21)]
            await asyncio.gather(*tasks)
            await cmd.async_flush()

        run(scenario())
        # every write must be ordered (increasing Brightness, never decreasing)
        brightnesses = [w["Brightness"] for w in client.writes if "Brightness" in w]
        self.assertTrue(all(
            a <= b for a, b in zip(brightnesses, brightnesses[1:])),
            f"out-of-order writes: {brightnesses}")
        # the final state must be the last requested value
        self.assertEqual(client.shadow.get("Brightness"), 20)
        self.assertEqual(len(client.writes), 1)  # coalesced into ONE write
        self.assertTrue(client.writes[0]["Brightness"] == 20)

    def test_brightness_then_cct_then_brightness_final_wins(self):
        """The exact user report: change brightness, then CCT, then check both."""
        client = FakeClient()
        cmd = AigoCommander(FakeHass(), client, "iot1")

        async def scenario():
            await cmd.async_send({"Brightness": 50})
            await asyncio.sleep(DEBOUNCE + 0.1)  # flush happens
            await cmd.async_send({"ColorTemperature": 80})
            await asyncio.sleep(DEBOUNCE + 0.1)
            await cmd.async_send({"Brightness": 10})   # change brightness again
            await cmd.async_flush()

        run(scenario())
        self.assertEqual(client.shadow["Brightness"], 10)   # NOT 50 (stale)
        self.assertEqual(client.shadow["ColorTemperature"], 80)  # preserved!

    def test_skip_window_after_write(self):
        """After a write completes, in_skip() is True -> stale poll won't overwrite."""
        client = FakeClient()
        cmd = AigoCommander(FakeHass(), client, "iot1")
        self.assertFalse(cmd.in_skip())

        async def scenario():
            await cmd.async_send({"Brightness": 42})
            await cmd.async_flush()
            # write has completed -> skip window must be active
            return cmd.in_skip()

        self.assertTrue(run(scenario()))

    def test_retry_on_failure_keeps_values(self):
        """A failed write must be retried, never silently dropped."""
        client = FakeClient()
        client.fail_next = 1  # first write fails
        cmd = AigoCommander(FakeHass(), client, "iot1")

        async def scenario():
            await cmd.async_send({"Brightness": 77})
            # wait long enough for retry cycle (debounce * 4)
            for _ in range(60):
                if client.writes:
                    break
                await asyncio.sleep(0.05)
            await cmd.async_flush()

        run(scenario())
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0]["Brightness"], 77)
        self.assertEqual(client.shadow["Brightness"], 77)

    def test_matches_helper(self):
        self.assertTrue(_matches({"Brightness": 50}, {"Brightness": 50}))
        self.assertTrue(_matches({"Brightness": "50"}, {"Brightness": 50}))
        self.assertFalse(_matches({"Brightness": 49}, {"Brightness": 50}))
        self.assertTrue(_matches({"HSVColor": {"Hue": 120}}, {"HSVColor": {"Hue": 120}}))
        self.assertFalse(_matches({"HSVColor": {"Hue": 121}}, {"HSVColor": {"Hue": 120}}))
        # an empty shadow cannot confirm a commanded value -> False (correct)
        # shadow reports None for a commanded value -> NOT confirmed
        self.assertFalse(_matches({"X": None}, {"X": 5}))
        # commanded None -> don't care, any shadow value confirms
        self.assertTrue(_matches({"X": 5}, {"X": None}))


if __name__ == "__main__":
    unittest.main()
