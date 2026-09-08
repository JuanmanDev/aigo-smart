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
sys.modules.setdefault("homeassistant.helpers", MagicMock())
sys.modules.setdefault("homeassistant.helpers.update_coordinator", MagicMock())

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


if __name__ == "__main__":
    unittest.main()
