"""Unit tests for AigoSmartLight state handling, jitter prevention, and CCT/brightness isolation."""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.modules["voluptuous"] = MagicMock()
ha_mock = MagicMock()
ha_mock.__path__ = []
sys.modules["homeassistant"] = ha_mock
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.core"].callback = lambda func: func
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.device_registry"] = MagicMock()
sys.modules["homeassistant.helpers.entity_platform"] = MagicMock()
sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.exceptions"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.light"] = MagicMock()

from enum import StrEnum


class ColorMode(StrEnum):
    UNKNOWN = "unknown"
    ONOFF = "onoff"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    HS = "hs"

sys.modules["homeassistant.components.light"].ColorMode = ColorMode
sys.modules["homeassistant.components.light"].ATTR_BRIGHTNESS = "brightness"
sys.modules["homeassistant.components.light"].ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"
sys.modules["homeassistant.components.light"].ATTR_HS_COLOR = "hs_color"

class MockLightEntity:
    def __init__(self):
        pass
    def async_write_ha_state(self):
        pass
    async def async_will_remove_from_hass(self):
        pass

sys.modules.setdefault("homeassistant.helpers", MagicMock())
sys.modules.setdefault("homeassistant.helpers.update_coordinator", MagicMock())

class MockCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    async def async_added_to_hass(self):
        pass

sys.modules["homeassistant.components.light"].LightEntity = MockLightEntity
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity

import importlib.util

spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.light",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "custom_components", "aigosmart", "light.py"))
)
light_mod = importlib.util.module_from_spec(spec)
light_mod.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.light"] = light_mod
spec.loader.exec_module(light_mod)

AigoSmartLight = light_mod.AigoSmartLight


WIFI_DOWNLIGHT_TSL = {"properties": [
    {"identifier": "LightMode", "dataType": {"type": "enum", "specs": {"0": "白光", "1": "彩光"}}},
    {"identifier": "HSVColor", "dataType": {"type": "struct", "specs": [
        {"identifier": "Hue", "dataType": {"type": "double", "specs": {"max": "360"}}},
        {"identifier": "Saturation", "dataType": {"type": "double", "specs": {"max": "100"}}},
        {"identifier": "Value", "dataType": {"type": "double", "specs": {"max": "100"}}},
    ]}},
]}


class TestColorSpecResolution(unittest.IsolatedAsyncioTestCase):
    """Issue #3: colour support must not depend on the props snapshot at creation."""

    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.props = {}
        self.coordinator.tsl_models = {}
        self.coordinator.is_device_online.return_value = True
        self.dev = {"iotId": "spot", "productKey": "a1CuHgtZg6v", "netType": "NET_WIFI"}

    def test_empty_props_then_poll_enables_hs(self):
        light = AigoSmartLight(self.coordinator, self.dev)
        self.assertEqual(light.supported_color_modes, {ColorMode.COLOR_TEMP})
        self.coordinator.props = {"spot": {
            "LightSwitch": 1, "LightMode": 0,
            "HSVColor": {"Hue": 306, "Saturation": 77, "Value": 100},
        }}
        light._handle_coordinator_update()
        self.assertIn(ColorMode.HS, light.supported_color_modes)

    async def test_tsl_fetched_when_entity_added(self):
        async def _get_tsl(pk, iot_id):
            self.coordinator.tsl_models[pk] = WIFI_DOWNLIGHT_TSL
            return WIFI_DOWNLIGHT_TSL

        self.coordinator.async_get_tsl = AsyncMock(side_effect=_get_tsl)
        light = AigoSmartLight(self.coordinator, self.dev)
        await light.async_added_to_hass()
        self.coordinator.async_get_tsl.assert_awaited_once_with("a1CuHgtZg6v", "spot")
        self.assertEqual(light._spec_source, "tsl")
        self.assertEqual(light._color_spec.identifier, "HSVColor")
        self.assertEqual(light._mode_spec.color_value, 1)

    async def test_pinned_profile_skips_tsl_fetch(self):
        self.coordinator.async_get_tsl = AsyncMock()
        dev = dict(self.dev, productKey="a1uh0UxUu3Z")
        light = AigoSmartLight(self.coordinator, dev)
        await light.async_added_to_hass()
        self.coordinator.async_get_tsl.assert_not_awaited()


class TestLightState(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.props = {}
        self.coordinator.devices = []
        self.coordinator.tsl_models = {}
        self.coordinator.client = MagicMock()
        self.dev = {
            "iotId": "test_lamp_1",
            "deviceName": "Techo R1",
            "productKey": "a1mevewbgC3",
            "status": 1,
        }

    async def test_brightness_and_cct_state_retained(self):
        light = AigoSmartLight(self.coordinator, self.dev)
        light.hass = MagicMock()
        light.hass.loop = MagicMock()
        light.hass.async_add_executor_job = AsyncMock()

        # 1. User sets brightness to 204 (80%)
        await light.async_turn_on(brightness=204)
        self.assertEqual(light.brightness, 204)
        self.assertTrue(light.is_on)

        # 2. User sets CCT to 4500K
        await light.async_turn_on(color_temp_kelvin=4500)
        # Brightness must NOT reset
        self.assertEqual(light.brightness, 204)
        self.assertEqual(light.color_temp_kelvin, 4500)

        # 3. For CCT-only bulb, LightMode must NOT be in commanded items
        call_args = light.hass.async_add_executor_job.call_args_list[-1]
        commanded_items = call_args[0][2]
        self.assertNotIn("LightMode", commanded_items)
        self.assertIn("ColorTemperature", commanded_items)

    def test_apply_suppresses_quantization_jitter(self):
        light = AigoSmartLight(self.coordinator, self.dev)
        # User had picked exactly 4500K
        light._color_temp_k = 4500
        light._brightness = 204

        # Device returns 47% and 80% (which matches the user selections)
        props = {"ColorTemperature": 47, "Brightness": 80}
        light._apply(props)

        # Should NOT jump to rounded 4486 or 204
        self.assertEqual(light.color_temp_kelvin, 4500)
        self.assertEqual(light.brightness, 204)

    def test_apply_accepts_external_changes(self):
        light = AigoSmartLight(self.coordinator, self.dev)
        light._brightness = 204  # 80%

        # External change to 50%
        props = {"Brightness": 50}
        light._apply(props)

        self.assertEqual(light.brightness, 128)


class TestFanLightLight(unittest.IsolatedAsyncioTestCase):
    """Lamp half of a BLE Mesh fan-light (issue #2): powerstate_1 drives the lamp."""

    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.tsl_models = {}
        self.coordinator.client = MagicMock()
        self.dev = {
            "iotId": "fan_bedroom", "productName": "Fan light  ",
            "categoryKey": "fanLight", "netType": "NET_BT", "productKey": "a180PpkuyP6",
        }
        self.coordinator.props = {"fan_bedroom": {
            "powerstate_1": 1, "powerstate_2": 0, "brightness": 40,
            "colorTemperature": 100, "LightMode": 0, "mode_1": 0, "mode_2": 0,
            "HSVColor": {"Hue": 120, "Saturation": 100, "Value": 40},
        }}

    def test_reads_lamp_channel(self):
        light = light_mod.AigoSmartFanLightLight(self.coordinator, self.dev)
        self.assertTrue(light.is_on)
        self.assertEqual(light._attr_unique_id, "fan_bedroom_light")
        self.assertIn(ColorMode.HS, light.supported_color_modes)

    async def test_turn_off_writes_powerstate_1(self):
        light = light_mod.AigoSmartFanLightLight(self.coordinator, self.dev)
        light.hass = MagicMock()
        light.hass.async_add_executor_job = AsyncMock()
        await light.async_turn_off()
        items = light.hass.async_add_executor_job.call_args[0][2]
        self.assertEqual(items, {"powerstate_1": 0})



class TestColourWriteOrdering(unittest.IsolatedAsyncioTestCase):
    """#2 / #3: mode goes in a separate write after the colour, only on change."""

    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.tsl_models = {}
        self.coordinator.is_device_online.return_value = True

    def _light(self, cls, dev, props):
        self.coordinator.props = {dev["iotId"]: props}
        light = cls(self.coordinator, dev, None)
        light._commander = MagicMock()
        light._commander.async_send = AsyncMock()
        return light

    def sent(self, light):
        args, kwargs = light._commander.async_send.call_args
        return args[0], kwargs.get("followup")

    FANLIGHT = {"iotId": "fl", "productName": "Fan light  ", "categoryKey": "fanLight",
                "netType": "NET_BT", "productKey": "a180PpkuyP6"}
    LAMP_ON_COLOUR = {"powerstate_1": 1, "brightness": 100, "colorTemperature": 28,
                      "LightMode": 1, "HSVColor": {"Hue": 60, "Saturation": 96, "Value": 100}}
    WIFI = {"iotId": "spot", "productKey": "a1CuHgtZg6v", "netType": "NET_WIFI"}
    WIFI_WHITE = {"LightSwitch": 1, "Brightness": 60, "ColorTemperature": 100,
                  "LightMode": 0, "HSVColor": {"Hue": 306, "Saturation": 77, "Value": 100}}

    async def test_fanlight_red_while_in_colour_mode_sends_colour_only(self):
        light = self._light(light_mod.AigoSmartFanLightLight, self.FANLIGHT, dict(self.LAMP_ON_COLOUR))
        await light.async_turn_on(hs_color=(0, 100))
        items, followup = self.sent(light)
        self.assertEqual(items, {"HSVColor": {"Hue": 0, "Saturation": 100, "Value": 100}})
        self.assertIsNone(followup)

    async def test_fanlight_colour_from_white_mode_switches_mode_afterwards(self):
        props = dict(self.LAMP_ON_COLOUR, LightMode=0)
        light = self._light(light_mod.AigoSmartFanLightLight, self.FANLIGHT, props)
        await light.async_turn_on(hs_color=(240, 100))
        items, followup = self.sent(light)
        self.assertEqual(items, {"HSVColor": {"Hue": 240, "Saturation": 100, "Value": 100}})
        self.assertEqual(followup, {"LightMode": 1})

    async def test_fanlight_off_lamp_gets_power_with_colour(self):
        props = dict(self.LAMP_ON_COLOUR, powerstate_1=0)
        light = self._light(light_mod.AigoSmartFanLightLight, self.FANLIGHT, props)
        await light.async_turn_on(hs_color=(120, 100))
        items, _ = self.sent(light)
        self.assertEqual(items["powerstate_1"], 1)

    async def test_fanlight_cct_from_colour_mode(self):
        light = self._light(light_mod.AigoSmartFanLightLight, self.FANLIGHT, dict(self.LAMP_ON_COLOUR))
        await light.async_turn_on(color_temp_kelvin=4600)
        items, followup = self.sent(light)
        self.assertEqual(items, {"colorTemperature": 50})
        self.assertEqual(followup, {"LightMode": 0})

    async def test_fanlight_plain_turn_on_while_on_resyncs_power(self):
        light = self._light(light_mod.AigoSmartFanLightLight, self.FANLIGHT, dict(self.LAMP_ON_COLOUR))
        await light.async_turn_on()
        self.assertEqual(self.sent(light), ({"powerstate_1": 1}, None))

    async def test_wifi_light_keeps_switch_and_moves_mode_to_followup(self):
        light = self._light(AigoSmartLight, self.WIFI, dict(self.WIFI_WHITE))
        await light.async_turn_on(hs_color=(120, 100), brightness=153)
        items, followup = self.sent(light)
        self.assertEqual(items, {"LightSwitch": 1, "Brightness": 60,
                                 "HSVColor": {"Hue": 120, "Saturation": 100, "Value": 60}})
        self.assertEqual(followup, {"LightMode": 1})


if __name__ == "__main__":
    unittest.main()
