"""Unit tests for fan / fan-light handling (issue #2): property mapping, speeds, on/off."""
import importlib.util
import os
import sys
import unittest
from enum import IntFlag
from unittest.mock import AsyncMock, MagicMock

sys.modules["voluptuous"] = MagicMock()
ha_mock = MagicMock()
ha_mock.__path__ = []
sys.modules["homeassistant"] = ha_mock
for _mod in (
    "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator", "homeassistant.const",
    "homeassistant.exceptions", "homeassistant.components",
    "homeassistant.components.fan", "homeassistant.util",
    "homeassistant.util.percentage",
):
    sys.modules[_mod] = MagicMock()


class FanEntityFeature(IntFlag):
    SET_SPEED = 1
    OSCILLATE = 2
    DIRECTION = 4
    PRESET_MODE = 8
    TURN_OFF = 16
    TURN_ON = 32


class MockFanEntity:
    def async_write_ha_state(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


class MockCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


def _ranged_value_to_percentage(low_high, value):
    return int(value * 100 // (low_high[1] - low_high[0] + 1))


def _percentage_to_ranged_value(low_high, percentage):
    return (low_high[1] - low_high[0] + 1) * percentage / 100


_fan = sys.modules["homeassistant.components.fan"]
_fan.FanEntity = MockFanEntity
_fan.FanEntityFeature = FanEntityFeature
_fan.DIRECTION_FORWARD = "forward"
_fan.DIRECTION_REVERSE = "reverse"
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity
_pct = sys.modules["homeassistant.util.percentage"]
_pct.ranged_value_to_percentage = _ranged_value_to_percentage
_pct.percentage_to_ranged_value = _percentage_to_ranged_value

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                     "custom_components", "aigosmart"))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"custom_components.aigosmart.{name}", os.path.join(_BASE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "custom_components.aigosmart"
    sys.modules[f"custom_components.aigosmart.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


helpers = _load("helpers")
discovery = _load("discovery")
fan_mod = _load("fan")

# Device dicts as returned by the cloud (sanitised from issue #2)
FAN_LIGHT_RGBCCT = {
    "iotId": "fan_bedroom", "nickName": "Dormitorio", "productName": "Fan light  ",
    "categoryKey": "fanLight", "netType": "NET_BT", "productKey": "a180PpkuyP6",
}
FAN_LIGHT_PROPS = {
    "powerstate_1": 1, "powerstate_2": 0, "windspeed": 5, "mode_1": 0,
    "mode_2": 2, "direction": 1, "brightness": 100, "colorTemperature": 100,
    "LightMode": 0, "buzzerSwitch": 1, "appointmentClosingTime": 0,
}
TOWER_FAN = {
    "iotId": "tower", "productName": "Tower fan", "categoryKey": "TowerFan",
    "netType": "NET_WIFI", "productKey": "a1mZFNZz7pq",
}


class TestFanLightDetection(unittest.TestCase):
    def test_cloud_category_fanlight(self):
        self.assertTrue(helpers.is_fan_light_device(FAN_LIGHT_RGBCCT))
        self.assertTrue(helpers.is_fan_device(FAN_LIGHT_RGBCCT))

    def test_catalog_category_fanlighting(self):
        dev = {"iotId": "x", "productKey": "a16jn5e0fTO", "productName": "风扇灯_RGBCCT"}
        self.assertTrue(helpers.is_fan_light_device(dev, {"a16jn5e0fTO": "FanLighting"}))

    def test_tower_fan_is_not_fan_light(self):
        self.assertFalse(helpers.is_fan_light_device(TOWER_FAN))

    def test_platform_mapping(self):
        self.assertEqual(discovery.platform_for_device(FAN_LIGHT_RGBCCT, {}), "fanlight")


class _FanTestBase(unittest.IsolatedAsyncioTestCase):
    dev: dict = {}
    props: dict = {}

    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.props = {self.dev["iotId"]: dict(self.props)}
        self.coordinator.is_device_online.return_value = True
        self.fan = fan_mod.AigoSmartFan(self.coordinator, self.dev, None)
        self.fan._commander = MagicMock()
        self.fan._commander.async_send = AsyncMock()

    def sent(self):
        return self.fan._commander.async_send.call_args[0][0]


class TestFanLightFan(_FanTestBase):
    dev = FAN_LIGHT_RGBCCT
    props = FAN_LIGHT_PROPS

    def test_reads_fan_channel(self):
        self.assertFalse(self.fan.is_on)          # powerstate_2 = 0 (lamp is on)
        self.assertEqual(self.fan._speed, 5)
        self.assertEqual(self.fan._mode, 2)
        self.assertEqual(self.fan.current_direction, "reverse")
        self.assertEqual(self.fan._attr_speed_count, 6)
        self.assertEqual(self.fan._attr_name, "Fan")

    def test_features_include_turn_on_off_and_direction(self):
        feats = self.fan._attr_supported_features
        for f in (FanEntityFeature.TURN_ON, FanEntityFeature.TURN_OFF,
                  FanEntityFeature.DIRECTION, FanEntityFeature.SET_SPEED):
            self.assertIn(f, feats)
        self.assertNotIn(FanEntityFeature.OSCILLATE, feats)

    def test_transition_speed_ignored(self):
        self.fan._apply({"windspeed": 255})
        self.assertEqual(self.fan._speed, 5)

    async def test_turn_on_positional_args_and_fan_power_prop(self):
        # HA calls async_turn_on(percentage, preset_mode, **kwargs) positionally
        await self.fan.async_turn_on(100, "Fresh")
        self.assertEqual(self.sent(), {"powerstate_2": 1, "windspeed": 6, "mode_2": 3})

    async def test_turn_on_plain(self):
        await self.fan.async_turn_on(None, None)
        self.assertEqual(self.sent(), {"powerstate_2": 1})

    async def test_turn_off(self):
        await self.fan.async_turn_off()
        self.assertEqual(self.sent(), {"powerstate_2": 0})

    async def test_percentage_zero_turns_off_instead_of_windspeed_0(self):
        await self.fan.async_set_percentage(0)
        self.assertEqual(self.sent(), {"powerstate_2": 0})

    async def test_low_percentage_clamped_to_speed_1(self):
        await self.fan.async_set_percentage(1)
        self.assertEqual(self.sent(), {"powerstate_2": 1, "windspeed": 1})

    async def test_direction(self):
        await self.fan.async_set_direction("forward")
        self.assertEqual(self.sent(), {"direction": 0})


class TestTowerFanUnchanged(_FanTestBase):
    dev = TOWER_FAN
    props = {"powerstate": 1, "windspeed": 2, "mode": 1, "angleAutoLROnOff": 1}

    def test_reads_legacy_props(self):
        self.assertTrue(self.fan.is_on)
        self.assertEqual(self.fan._speed, 2)
        self.assertTrue(self.fan.oscillating)
        self.assertEqual(self.fan._attr_speed_count, 3)
        self.assertIsNone(self.fan._attr_name)

    async def test_set_percentage(self):
        await self.fan.async_set_percentage(100)
        self.assertEqual(self.sent(), {"powerstate": 1, "windspeed": 3})


if __name__ == "__main__":
    unittest.main()
