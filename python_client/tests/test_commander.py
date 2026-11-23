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
import importlib.util
import os
import sys
import unittest


# Load the module by path: putting custom_components/aigosmart on sys.path
# would let the integration's select.py shadow the stdlib "select" module
# (which asyncio imports) for every test that runs afterwards.
def _load(name):
    path = os.path.join(os.path.dirname(__file__), "..", "..",
                        "custom_components", "aigosmart", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_aigosmart_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up here
    spec.loader.exec_module(module)
    return module


_commander = _load("commander")
DEBOUNCE = _commander.DEBOUNCE
AigoCommander = _commander.AigoCommander
_matches = _commander._matches


class FakeHass:
    def async_add_executor_job(self, fn, *args):
        # run the (fake) sync client call immediately
        return asyncio.get_event_loop().run_in_executor(None, fn, *args)


class FakeClient:
    def __init__(self):
        self.writes: list[dict] = []
        self.fail_next = 0
        self.shadow: dict = {}

    def set_properties(self, iot_id, items):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("network down")
        self.writes.append(dict(items))
        self.shadow.update(items)
        return {"code": 200}

    def get_properties(self, iot_id):
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

    def test_terminal_failure_invokes_on_failure(self):
        """After MAX_ATTEMPTS exhausted the on_failure callback must fire."""
        client = FakeClient()
        client.fail_next = 99  # every write fails
        failures = []

        async def on_failure():
            failures.append(True)

        cmd = AigoCommander(FakeHass(), client, "iot1", on_failure=on_failure)

        async def scenario():
            await cmd.async_send({"Brightness": 50})
            for _ in range(80):
                if failures:
                    break
                await asyncio.sleep(0.05)
            await cmd.async_flush()
            return cmd.in_skip()

        still_skip = run(scenario())
        self.assertEqual(len(failures), 1)
        # skip window must be cleared so polls can restore real state
        self.assertFalse(still_skip)

    def test_terminal_failure_no_silent_loop(self):
        """The old bug: after giving up, the loop continued and dropped silently.
        Now _run must terminate after MAX_ATTEMPTS."""
        client = FakeClient()
        client.fail_next = 99
        cmd = AigoCommander(FakeHass(), client, "iot1")

        async def scenario():
            await cmd.async_send({"Brightness": 50})
            await cmd.async_flush()

        run(scenario())
        # exactly MAX_ATTEMPTS writes attempted, no endless retries
        self.assertEqual(len(client.writes), 0)  # all failed (fail_next=99)
        self.assertEqual(client.fail_next, 99 - 3)  # 3 attempts consumed

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

    def test_matches_quantised_colour(self):
        # mesh lamps report 16-bit quantised values: 9 -> 8.99794, 100 -> 99.998
        shadow = {"HSVColor": {"Hue": 8.99794, "Saturation": 66.9993133, "Value": 99.9984741}}
        self.assertTrue(_matches(shadow, {"HSVColor": {"Hue": 9, "Saturation": 67, "Value": 100}}))
        self.assertFalse(_matches(shadow, {"HSVColor": {"Hue": 4, "Saturation": 67, "Value": 100}}))

    def test_followup_written_after_main_batch(self):
        client = FakeClient()
        cmd = AigoCommander(FakeHass(), client, "iot1")

        async def scenario():
            await cmd.async_send({"HSVColor": {"Hue": 0}}, followup={"LightMode": 1})
            await cmd.async_flush()

        run(scenario())
        self.assertEqual(client.writes, [{"HSVColor": {"Hue": 0}}, {"LightMode": 1}])

    def test_direct_write_supersedes_followup(self):
        client = FakeClient()
        cmd = AigoCommander(FakeHass(), client, "iot1")

        async def scenario():
            await cmd.async_send({"HSVColor": {"Hue": 0}}, followup={"LightMode": 1})
            await cmd.async_send({"LightMode": 0})
            await cmd.async_flush()

        run(scenario())
        self.assertEqual(client.writes, [{"HSVColor": {"Hue": 0}, "LightMode": 0}])


    def test_one_per_write_splits_batches(self):
        client = FakeClient()
        cmd = AigoCommander(FakeHass(), client, "iot1")
        cmd.one_per_write = True

        async def scenario():
            await cmd.async_send({"mode": 1, "WaterGear": 3})
            await cmd.async_flush()

        run(scenario())
        self.assertEqual(client.writes, [{"mode": 1}, {"WaterGear": 3}])


if __name__ == "__main__":
    unittest.main()
