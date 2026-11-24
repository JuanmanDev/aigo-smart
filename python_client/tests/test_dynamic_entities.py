"""Entities whose properties / devices appear after startup are still created, once."""
import asyncio
import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.modules["voluptuous"] = MagicMock()
ha_mock = MagicMock()
ha_mock.__path__ = []
sys.modules["homeassistant"] = ha_mock
for _mod in (
    "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator", "homeassistant.const",
    "homeassistant.exceptions", "homeassistant.components",
    "homeassistant.components.light", "homeassistant.components.number",
    "homeassistant.components.button", "homeassistant.components.binary_sensor",
    "homeassistant.components.event", "homeassistant.components.time",
    "homeassistant.components.select", "homeassistant.components.sensor",
    "homeassistant.components.switch", "homeassistant.components.water_heater",
    "homeassistant.util", "homeassistant.util.dt",
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
                   ("sensor", "SensorEntity"), ("switch", "SwitchEntity"),
                   ("water_heater", "WaterHeaterEntity"), ("light", "LightEntity"),
                   ("number", "RestoreNumber"), ("button", "ButtonEntity"),
                   ("binary_sensor", "BinarySensorEntity"), ("event", "EventEntity"),
                   ("time", "TimeEntity")):
    setattr(sys.modules[f"homeassistant.components.{_mod}"], _cls, type(_cls, (_Entity,), {}))
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity
sys.modules["homeassistant.core"].callback = lambda func: func

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                     "custom_components", "aigosmart"))


# Drop integration modules cached by other test files: they were built with
# those files' Home Assistant stubs (e.g. a non-identity @callback), and the
# platforms below must import fresh copies of .pet / .airfryer with ours.
for _cached in [m for m in sys.modules if m.startswith("custom_components.aigosmart.")]:
    del sys.modules[_cached]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"custom_components.aigosmart.{name}", os.path.join(_BASE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "custom_components.aigosmart"
    sys.modules[f"custom_components.aigosmart.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


discovery = _load("discovery")
platforms = {name: _load(name)
             for name in ("switch", "select", "sensor", "number", "water_heater", "button")}

AQUARIUM = {"iotId": "aq", "productKey": "a191KWgv5BZ", "categoryKey": "Aquarium",
            "productName": "Aquarium"}
TOWER_FAN = {"iotId": "fan", "productKey": "a1mZFNZz7pq", "categoryKey": "TowerFan",
             "productName": "Tower fan"}
KETTLE = {"iotId": "kettle", "productKey": "k", "categoryKey": "Kettle", "productName": "Kettle"}
FRYER = {"iotId": "fryer", "productKey": "a1MqVKrp9JA", "categoryKey": "Airfryer",
         "productName": "Air fryer"}
FOUNTAIN = {"iotId": "fountain", "productKey": "a163VQp0e0e",
            "categoryKey": "petWaterFountain", "productName": "Pet Water Fountain"}

LATE_PROPS = {
    "aq": {"powerstate": 1, "childLockOnOff": 0, "waterPumpLevel": 2,
           "currentTemperature": 25.5, "feedCnt": 3},
    "fan": {"powerstate": 1, "windspeed": 2, "buzzerSwitch": 1, "appointmentClosingTime": 0},
    "kettle": {"HeatingSwitch": 0, "temperature": 30},
    "fryer": {"K": 0, "M": 3, "W": 180, "T": 15, "E": 0},
    "fountain": {"Switch": 1, "mode": 0, "WaterGear": 2, "waterLevvel": 100},
}


class Harness:
    """Runs a platform's async_setup_entry against fake hass / entry objects."""

    def __init__(self, devices, props):
        self.coordinator = MagicMock()
        self.coordinator.devices = devices
        self.coordinator.props = props
        self.coordinator.is_device_online.return_value = True
        self.poll_listeners = []
        self.coordinator.async_add_listener.side_effect = (
            lambda cb: self.poll_listeners.append(cb) or (lambda: None))
        self.state = {"coordinator": self.coordinator, "registry": discovery.DeviceRegistry(),
                      "pk_catalog": {}, "hass": MagicMock(), "client": MagicMock()}
        self.hass = MagicMock()
        self.hass.data = {"aigosmart": {"entry": self.state}}
        # background TSL fetches for pet devices are not run here
        self.hass.async_create_task.side_effect = lambda coro: coro.close()
        self.new_device_listeners = []
        self.hass.bus.async_listen.side_effect = (
            lambda event, cb: self.new_device_listeners.append(cb) or (lambda: None))
        self.entry = MagicMock()
        self.entry.entry_id = "entry"
        self.added = {}

    def setup(self, name):
        def _add(entities):
            self.added.setdefault(name, []).extend(entities)
        asyncio.run(platforms[name].async_setup_entry(self.hass, self.entry, _add))

    def poll(self):
        for cb in list(self.poll_listeners):
            cb()

    def new_devices(self):
        for cb in list(self.new_device_listeners):
            cb(MagicMock())

    def unique_ids(self, name):
        return [e._attr_unique_id for e in self.added.get(name, [])]


class TestLateProperties(unittest.TestCase):
    """Devices known at startup, but their properties only arrive with a later poll."""

    def setUp(self):
        self.h = Harness([AQUARIUM, TOWER_FAN, KETTLE, FRYER, FOUNTAIN], {})
        for name in platforms:
            self.h.setup(name)

    def test_gated_entities_appear_after_properties_arrive(self):
        self.assertEqual(self.h.added.get("select", []), [])
        self.assertNotIn("aq_power", self.h.unique_ids("switch"))
        self.h.coordinator.props = dict(LATE_PROPS)
        self.h.poll()
        switches = self.h.unique_ids("switch")
        for uid in ("aq_power", "aq_child_lock", "fan_buzzer", "fryer_af_K", "fountain_pet_Switch"):
            self.assertIn(uid, switches)
        self.assertIn("aq_pump_speed", self.h.unique_ids("select"))
        self.assertIn("fountain_pet_WaterGear", self.h.unique_ids("select"))
        sensors = self.h.unique_ids("sensor")
        self.assertTrue(any("currentTemperature" in u for u in sensors))
        self.assertIn("fryer_af_W", self.h.unique_ids("number"))

    def test_no_duplicates_over_many_polls(self):
        self.h.coordinator.props = dict(LATE_PROPS)
        for _ in range(5):
            self.h.poll()
            self.h.new_devices()
        for name in platforms:
            ids = self.h.unique_ids(name)
            self.assertEqual(len(ids), len(set(ids)), name)

    def test_property_independent_entities_created_at_setup(self):
        # fan timer and kettle don't depend on properties
        self.assertEqual(self.h.unique_ids("number"), ["fan_timer"])
        self.assertEqual(self.h.unique_ids("water_heater"), ["kettle"])


class TestDevicesAddedLater(unittest.TestCase):
    """Fan timers and kettles used to be created only at startup."""

    def test_new_fan_and_kettle_after_startup(self):
        h = Harness([], {})
        h.setup("number")
        h.setup("water_heater")
        self.assertEqual(h.added, {})
        h.coordinator.devices = [TOWER_FAN, KETTLE]
        h.coordinator.props = {"fan": LATE_PROPS["fan"], "kettle": LATE_PROPS["kettle"]}
        h.new_devices()
        h.new_devices()
        self.assertEqual(h.unique_ids("number"), ["fan_timer"])
        self.assertEqual(h.unique_ids("water_heater"), ["kettle"])

    def test_fan_timer_shows_value_immediately(self):
        h = Harness([TOWER_FAN], {"fan": dict(LATE_PROPS["fan"], appointmentClosingTime=5)})
        h.setup("number")
        self.assertEqual(h.added["number"][0].native_value, 5.0)



class TestPetTslFetch(unittest.TestCase):
    """The feeder's manual-feed button appears once its TSL has been fetched."""

    def test_feed_button_after_tsl_fetch(self):
        feeder = {"iotId": "feeder", "productKey": "a1xEttfYqF2", "categoryKey": "petFeeder"}
        h = Harness([feeder], {"feeder": {"lampSwitch": 1}})
        h.coordinator.tsl_models = {}
        tsl = {"properties": [{"identifier": "manualFeeding", "accessMode": "rw",
                               "dataType": {"type": "int", "specs": {"min": "1", "max": "24"}}}]}

        async def _get_tsl(pk, iot_id):
            h.coordinator.tsl_models[pk] = tsl
            return tsl

        h.coordinator.async_get_tsl.side_effect = _get_tsl
        pending = []
        h.hass.async_create_task.side_effect = pending.append
        h.setup("button")
        self.assertEqual(h.added.get("button", []), [])
        self.assertEqual(len(pending), 1)          # one fetch per product
        h.poll()
        self.assertEqual(len(pending), 1)          # not re-requested while pending
        asyncio.run(pending[0])
        self.assertEqual(h.unique_ids("button"), ["feeder_pet_manualFeeding_button"])


if __name__ == "__main__":
    unittest.main()
