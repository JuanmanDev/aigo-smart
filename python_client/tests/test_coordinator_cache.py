"""Unit tests for coordinator optimistic property locking and cache merge logic."""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

# Mock homeassistant modules before importing coordinator
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules.setdefault("homeassistant.config_entries", MagicMock())
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules.setdefault("homeassistant.const", MagicMock())
sys.modules.setdefault("homeassistant.helpers", MagicMock())
sys.modules.setdefault("homeassistant.helpers.update_coordinator", MagicMock())
sys.modules.setdefault("homeassistant.exceptions", MagicMock())
sys.modules.setdefault("homeassistant.components", MagicMock())
sys.modules.setdefault("voluptuous", MagicMock())

# Provide a mock DataUpdateCoordinator base class
class MockDataUpdateCoordinator:
    def __init__(self, hass, logger, name, update_interval, config_entry=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.config_entry = config_entry
        self.data = {}
    def async_update_listeners(self):
        pass

sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = MockDataUpdateCoordinator
sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = Exception

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.coordinator",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "custom_components", "aigosmart", "coordinator.py"))
)
coord_mod = importlib.util.module_from_spec(spec)
coord_mod.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.coordinator"] = coord_mod
# Also mock .api
api_mock = MagicMock()
sys.modules["custom_components.aigosmart.api"] = api_mock
spec.loader.exec_module(coord_mod)

AigoDataUpdateCoordinator = coord_mod.AigoDataUpdateCoordinator


class TestCoordinatorOptimisticLock(unittest.TestCase):
    def setUp(self):
        hass = MagicMock()
        client = MagicMock()
        self.coordinator = AigoDataUpdateCoordinator(hass, client)
        self.iot_id = "test_iot_123"

    def test_optimistic_hold_prevents_stale_overwrite(self):
        self.coordinator.data = {
            "devices": [{"iotId": self.iot_id}],
            "props": {self.iot_id: {"Brightness": 20, "ColorTemperature": 10}},
        }
        self.coordinator.async_set_optimistic_props(
            self.iot_id, {"Brightness": 80}, hold_duration=5.0
        )
        stale_cloud = {"Brightness": 20, "ColorTemperature": 10}
        merged = self.coordinator._merge_props(self.iot_id, stale_cloud)
        self.assertEqual(merged["Brightness"], 80)
        self.assertEqual(merged["ColorTemperature"], 10)

    def test_optimistic_hold_released_early_when_cloud_matches(self):
        self.coordinator.data = {
            "devices": [{"iotId": self.iot_id}],
            "props": {self.iot_id: {"Brightness": 20}},
        }
        self.coordinator.async_set_optimistic_props(
            self.iot_id, {"Brightness": 80}, hold_duration=5.0
        )
        matching_cloud = {"Brightness": 80}
        merged = self.coordinator._merge_props(self.iot_id, matching_cloud)
        self.assertEqual(merged["Brightness"], 80)
        locks = self.coordinator._optimistic_locks.get(self.iot_id, {})
        self.assertNotIn("Brightness", locks)

    def test_cct_and_brightness_independent_holds(self):
        self.coordinator.data = {
            "devices": [{"iotId": self.iot_id}],
            "props": {self.iot_id: {"Brightness": 20, "ColorTemperature": 10}},
        }
        self.coordinator.async_set_optimistic_props(
            self.iot_id, {"Brightness": 80}, hold_duration=5.0
        )
        self.coordinator.async_set_optimistic_props(
            self.iot_id, {"ColorTemperature": 65}, hold_duration=5.0
        )
        stale_cloud = {"Brightness": 20, "ColorTemperature": 10}
        merged = self.coordinator._merge_props(self.iot_id, stale_cloud)
        self.assertEqual(merged["Brightness"], 80)
        self.assertEqual(merged["ColorTemperature"], 65)

    def test_expired_hold_accepts_new_cloud_value(self):
        self.coordinator.data = {
            "devices": [{"iotId": self.iot_id}],
            "props": {self.iot_id: {"Brightness": 20}},
        }
        self.coordinator.async_set_optimistic_props(
            self.iot_id, {"Brightness": 80}, hold_duration=0.05
        )
        time.sleep(0.06)
        cloud_props = {"Brightness": 50}
        merged = self.coordinator._merge_props(self.iot_id, cloud_props)
        self.assertEqual(merged["Brightness"], 50)


class TestAssumedState(unittest.TestCase):
    """Devices that apply writes but never report them back (#4)."""

    def setUp(self):
        self.c = AigoDataUpdateCoordinator(MagicMock(), MagicMock())
        self.c.data = {"devices": [], "props": {}}
        self.iot = "fountain"
        # shadow frozen at the old values
        self.shadow = {"Switch": 1, "mode": 1, "WaterGear": 1}
        self.c._merge_props(self.iot, dict(self.shadow))

    def poll(self, shadow=None):
        merged = self.c._merge_props(self.iot, dict(shadow or self.shadow))
        self.c.data["props"][self.iot] = merged
        return merged

    def test_written_value_survives_frozen_shadow(self):
        self.c.remember_write(self.iot, {"WaterGear": 3})
        self.c.remember_write(self.iot, {"mode": 0})
        merged = self.poll()
        self.assertEqual(merged["WaterGear"], 3)
        self.assertEqual(merged["mode"], 0)
        self.assertEqual(merged["Switch"], 1)
        self.assertEqual(self.c.assumed_props(self.iot), {"WaterGear": 3, "mode": 0})

    def test_real_report_wins(self):
        self.c.remember_write(self.iot, {"WaterGear": 3})
        merged = self.poll({**self.shadow, "WaterGear": 2})   # changed in the app
        self.assertEqual(merged["WaterGear"], 2)
        self.assertEqual(self.c.assumed_props(self.iot), {})
        # later polls keep the real value
        self.assertEqual(self.poll({**self.shadow, "WaterGear": 2})["WaterGear"], 2)

    def test_device_confirms_write(self):
        self.c.remember_write(self.iot, {"WaterGear": 3})
        self.assertEqual(self.poll({**self.shadow, "WaterGear": 3})["WaterGear"], 3)
        self.assertEqual(self.c.assumed_props(self.iot), {})

    def test_writing_back_the_shadow_value_drops_assumption(self):
        self.c.remember_write(self.iot, {"Switch": 0})
        self.c.remember_write(self.iot, {"Switch": 1})
        self.assertEqual(self.c.assumed_props(self.iot), {})
        self.assertEqual(self.poll()["Switch"], 1)

    def test_baseline_is_kept_across_writes(self):
        self.c.remember_write(self.iot, {"WaterGear": 3})
        self.poll()
        self.c.remember_write(self.iot, {"WaterGear": 2})
        self.assertEqual(self.poll()["WaterGear"], 2)
        self.assertEqual(self.c._assumed[self.iot]["WaterGear"], [2, 1])

    def test_persisted_and_restored(self):
        store = MagicMock()
        saved = {}

        def _delay_save(fn, delay):
            saved.update(fn())

        store.async_delay_save.side_effect = _delay_save

        async def _load():
            return None

        store.async_load.side_effect = _load
        import asyncio
        asyncio.run(self.c.async_load_assumed(store))
        self.c.remember_write(self.iot, {"mode": 0})
        self.assertEqual(saved, {self.iot: {"mode": [0, 1]}})

        other = AigoDataUpdateCoordinator(MagicMock(), MagicMock())
        other.data = {"devices": [], "props": {}}

        async def _load_saved():
            return saved

        store2 = MagicMock()
        store2.async_load.side_effect = _load_saved
        asyncio.run(other.async_load_assumed(store2))
        self.assertEqual(other._merge_props(self.iot, dict(self.shadow))["mode"], 0)

    def test_corrupt_store_is_ignored(self):
        store = MagicMock()

        async def _load():
            raise ValueError("bad json")

        store.async_load.side_effect = _load
        import asyncio
        asyncio.run(self.c.async_load_assumed(store))
        self.assertEqual(self.c.assumed_props(self.iot), {})


class TestParallelPoll(unittest.IsolatedAsyncioTestCase):
    """Devices are polled concurrently; local reads only when enabled."""

    async def asyncSetUp(self):
        import asyncio
        self.asyncio = asyncio
        hass = MagicMock()
        self.running = 0
        self.peak = 0

        async def _executor(func, *args):
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0.01)
            try:
                return func(*args)
            finally:
                self.running -= 1

        hass.async_add_executor_job.side_effect = _executor
        self.client = MagicMock()
        self.devices = [{"iotId": f"d{i}", "productKey": "pk", "status": 1}
                        for i in range(10)]
        self.client.list_devices.return_value = self.devices
        self.client.get_status.return_value = {"status": 1}
        self.client.get_properties.side_effect = lambda iot_id: {"id": iot_id}
        self.client.get_properties_prefer_local.side_effect = (
            lambda iot_id, ids: {"id": iot_id, "local": ids})
        self.c = AigoDataUpdateCoordinator(hass, self.client)
        self.c.data = {"devices": [], "props": {}}

    async def test_all_devices_polled_concurrently(self):
        data = await self.c._async_update_data()
        self.assertEqual(sorted(data["props"]), sorted(d["iotId"] for d in self.devices))
        self.assertEqual(data["props"]["d3"]["id"], "d3")
        self.assertGreater(self.peak, 1)
        self.assertLessEqual(self.peak, coord_mod.POLL_CONCURRENCY)
        self.client.get_properties_prefer_local.assert_not_called()

    async def test_one_failing_device_does_not_fail_the_poll(self):
        def _props(iot_id):
            if iot_id == "d2":
                raise OSError("boom")
            return {"id": iot_id}
        self.client.get_properties.side_effect = _props
        data = await self.c._async_update_data()
        self.assertEqual(data["props"]["d2"], {})
        self.assertEqual(data["props"]["d5"]["id"], "d5")

    async def test_local_reads_use_tsl_identifiers(self):
        self.c.local_reads = True
        self.c.tsl_models["pk"] = {"properties": [{"identifier": "Switch"},
                                                  {"identifier": "mode"}]}
        data = await self.c._async_update_data()
        self.assertEqual(data["props"]["d1"]["local"], ["Switch", "mode"])
        self.client.get_properties.assert_not_called()


if __name__ == "__main__":
    unittest.main()
