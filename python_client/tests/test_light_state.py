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


if __name__ == "__main__":
    unittest.main()
