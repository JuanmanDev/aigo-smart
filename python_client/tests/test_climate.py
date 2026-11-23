"""Unit tests for the climate entity: on/off support and power writes."""
import importlib.util
import os
import sys
import unittest
from enum import IntFlag, StrEnum
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
    "homeassistant.components.climate",
):
    sys.modules[_mod] = MagicMock()


class ClimateEntityFeature(IntFlag):
    TARGET_TEMPERATURE = 1
    TURN_OFF = 128
    TURN_ON = 256


class HVACMode(StrEnum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class MockClimateEntity:
    def async_write_ha_state(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


class MockCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


_climate = sys.modules["homeassistant.components.climate"]
_climate.ClimateEntity = MockClimateEntity
_climate.ClimateEntityFeature = ClimateEntityFeature
_climate.HVACMode = HVACMode
_climate.ATTR_TEMPERATURE = "temperature"
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity
sys.modules["homeassistant.core"].callback = lambda func: func

_spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.climate",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                 "custom_components", "aigosmart", "climate.py")))
climate_mod = importlib.util.module_from_spec(_spec)
climate_mod.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.climate"] = climate_mod
_spec.loader.exec_module(climate_mod)


class TestClimateOnOff(unittest.IsolatedAsyncioTestCase):
    def _entity(self, category, props):
        coordinator = MagicMock()
        coordinator.props = {"ac": props}
        coordinator.is_device_online.return_value = True
        entity = climate_mod.AigoSmartClimate(
            coordinator, {"iotId": "ac", "categoryKey": category}, None)
        entity._commander = MagicMock()
        entity._commander.async_send = AsyncMock()
        return entity

    def test_declares_turn_on_off(self):
        feats = climate_mod.AigoSmartClimate._attr_supported_features
        self.assertIn(ClimateEntityFeature.TURN_ON, feats)
        self.assertIn(ClimateEntityFeature.TURN_OFF, feats)
        self.assertIn(ClimateEntityFeature.TARGET_TEMPERATURE, feats)

    async def test_turn_on_off_take_no_arguments(self):
        # HA calls entity.async_turn_on() / async_turn_off() without arguments
        ac = self._entity("AirConditioner", {"powerstate": 0})
        await ac.async_turn_on()
        ac._commander.async_send.assert_awaited_with({"powerstate": 1})
        self.assertEqual(ac.hvac_mode, HVACMode.COOL)
        await ac.async_turn_off()
        ac._commander.async_send.assert_awaited_with({"powerstate": 0})
        self.assertEqual(ac.hvac_mode, HVACMode.OFF)

    async def test_dehumidifier_turns_on_in_dry(self):
        dh = self._entity("Dehumidifier", {"powerstate": 0})
        await dh.async_turn_on()
        self.assertEqual(dh.hvac_mode, HVACMode.DRY)

    async def test_set_hvac_mode_unchanged(self):
        ac = self._entity("Heater", {"powerstate": 1})
        await ac.async_set_hvac_mode(HVACMode.OFF)
        ac._commander.async_send.assert_awaited_with({"powerstate": 0})


if __name__ == "__main__":
    unittest.main()
