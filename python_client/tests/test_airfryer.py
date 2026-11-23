"""Unit tests for the air fryer entities (issue #5, productKey a1MqVKrp9JA)."""
import importlib.util
import os
import sys
import unittest
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
    "homeassistant.components.number", "homeassistant.components.select",
    "homeassistant.components.sensor", "homeassistant.components.switch",
):
    sys.modules[_mod] = MagicMock()


class _Entity:
    def async_write_ha_state(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


class MockCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


for _mod, _cls in (("number", "NumberEntity"), ("select", "SelectEntity"),
                   ("sensor", "SensorEntity"), ("switch", "SwitchEntity")):
    setattr(sys.modules[f"homeassistant.components.{_mod}"], _cls,
            type(_cls, (_Entity,), {}))
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity
sys.modules["homeassistant.core"].callback = lambda func: func

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
airfryer = _load("airfryer")

FRYER = {
    "iotId": "fryer", "productKey": "a1MqVKrp9JA", "categoryKey": "Airfryer",
    "productName": "空气炸锅_7L_小匠_WB", "netType": "NET_WIFI",
}
FRYER_PROPS = {"K": 0, "M": 3, "W": 180, "T": 15, "F": 0, "Y": 0, "E": 0, "Z": 0, "G": 0}


class TestAirfryer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.devices = [FRYER]
        self.coordinator.props = {"fryer": dict(FRYER_PROPS)}
        self.coordinator.is_device_online.return_value = True

        def _set_optimistic(iot_id, items, hold_duration=5.0):
            self.coordinator.props[iot_id].update(items)

        self.coordinator.async_set_optimistic_props.side_effect = _set_optimistic

    def _make(self, platform, known=None):
        return airfryer.make_airfryer_entities(
            platform, self.coordinator, None, {}, set() if known is None else known)

    def test_detection_and_platform(self):
        self.assertTrue(helpers.is_airfryer_device(FRYER))
        self.assertEqual(discovery.platform_for_device(FRYER, {}), "airfryer")

    def test_entities_per_platform(self):
        self.assertEqual(len(self._make("switch")), 1)
        self.assertEqual(len(self._make("number")), 4)
        self.assertEqual(len(self._make("select")), 1)
        self.assertEqual(len(self._make("sensor")), 4)

    def test_no_duplicates_on_repeated_updates(self):
        known: set = set()
        self.assertEqual(len(self._make("number", known)), 4)
        self.assertEqual(self._make("number", known), [])

    def test_waits_for_signature(self):
        self.coordinator.props = {"fryer": {}}
        self.assertEqual(self._make("switch"), [])
        # an air fryer with another TSL must not get these entities
        self.coordinator.props = {"fryer": {"K": 0, "T": 20}}
        self.assertEqual(self._make("number"), [])

    def test_state_mapping(self):
        program = self._make("select")[0]
        self.assertEqual(program.current_option, "Fries")
        status = next(e for e in self._make("sensor") if e._prop == "K")
        self.assertEqual(status.native_value, "standby")

    async def test_cooking_switch_writes_k(self):
        switch = self._make("switch")[0]
        switch._commander = MagicMock()
        switch._commander.async_send = AsyncMock()
        await switch.async_turn_on()
        switch._commander.async_send.assert_awaited_with({"K": 2})
        self.assertTrue(switch.is_on)
        await switch.async_turn_off()
        switch._commander.async_send.assert_awaited_with({"K": 0})
        self.assertFalse(switch.is_on)

    async def test_select_program_writes_m(self):
        program = self._make("select")[0]
        program._commander = MagicMock()
        program._commander.async_send = AsyncMock()
        await program.async_select_option("Steak")
        program._commander.async_send.assert_awaited_with({"M": 6})


if __name__ == "__main__":
    unittest.main()
