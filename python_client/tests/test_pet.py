"""Unit tests for pet fountain / feeder support (issue #4)."""
import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone
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
                   ("sensor", "SensorEntity"), ("switch", "SwitchEntity")):
    setattr(sys.modules[f"homeassistant.components.{_mod}"], _cls,
            type(_cls, (_Entity,), {}))
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity
sys.modules["homeassistant.core"].callback = lambda func: func
# "today" in Europe/Lisbon (UTC+0 in November) on the day of the report
_DT = sys.modules["homeassistant.util.dt"]
_DT.start_of_local_day = lambda: datetime(2026, 11, 23, tzinfo=timezone.utc)
sys.modules["homeassistant.util"].dt = _DT

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
pet = _load("pet")

# Data from the reporter's diagnostics in #4 (identifiers removed)
FOUNTAIN = {"iotId": "fountain", "productKey": "a163VQp0e0e", "netType": "NET_WIFI",
            "categoryKey": "petWaterFountain", "productName": "Pet Water  Fountain "}
FEEDER = {"iotId": "feeder", "productKey": "a1xEttfYqF2", "netType": "NET_WIFI",
          "categoryKey": "petFeeder", "productName": "Pet  feeder "}
FOUNTAIN_PROPS = {
    "mode": 0, "DeviceCleaningTime": 10, "Switch": 1, "WaterStatus": 0,
    "FilterReplaceTime": 31, "errorCode": 0, "FilterRemainderDay": 13,
    "CleaningRemainderDay": 10, "waterLevvel": 100, "WaterGear": 2,
}
FEED_RECORD = ("1795448206,1:1795448180,1:1795435202,2:1795413603,3:1795392002,2:"
               "1795370403,3:1795348802,2:1795327203,3:1795310364,1")
FEEDER_PROPS = {
    "recordExists": 0, "batteryState": 0, "BatteryPercentage": 0.0, "lampSwitch": 1,
    "errorCode": 0, "feedRecord": FEED_RECORD, "recordControl": 0,
    "feedingPlan": "7F00000201,7F06000401,7F0C000201,7F12000301",
    "feedState": 0, "slowFeed": 0,
}


class TestParsing(unittest.TestCase):
    def test_feed_record(self):
        records = pet.parse_feed_record(FEED_RECORD)
        self.assertEqual(len(records), 9)
        self.assertEqual(records[0], (datetime(2026, 11, 23, 15, 36, 46, tzinfo=timezone.utc), 1))
        # the app showed "9 portions" fed today
        today = datetime(2026, 11, 23, tzinfo=timezone.utc)
        self.assertEqual(pet.portions_since(records, today), 9)

    def test_feed_record_garbage(self):
        self.assertEqual(pet.parse_feed_record(""), [])
        self.assertEqual(pet.parse_feed_record(None), [])
        self.assertEqual(len(pet.parse_feed_record("x,1:1795448206,2:,")), 1)

    def test_feeding_plan_matches_app_schedule(self):
        slots = pet.parse_feeding_plan(FEEDER_PROPS["feedingPlan"])
        # app: 00:00 x2, 06:00 x4, 12:00 x2, 18:00 x3, every day, all on
        self.assertEqual([(s["time"], s["portions"]) for s in slots],
                         [("00:00", 2), ("06:00", 4), ("12:00", 2), ("18:00", 3)])
        self.assertTrue(all(s["enabled"] and s["every_day"] for s in slots))

    def test_feeding_plan_garbage(self):
        self.assertEqual(pet.parse_feeding_plan("ZZ,7F1800"), [])
        self.assertEqual(pet.parse_feeding_plan(None), [])


class TestDetection(unittest.TestCase):
    def test_categories(self):
        self.assertTrue(helpers.is_pet_fountain_device(FOUNTAIN))
        self.assertTrue(helpers.is_pet_feeder_device(FEEDER))
        self.assertFalse(helpers.is_pet_feeder_device(FOUNTAIN))
        self.assertFalse(helpers.is_pet_fountain_device(FEEDER))

    def test_catalog_categories(self):
        dev = {"iotId": "x", "productKey": "a1L836QOWgx"}
        self.assertTrue(helpers.is_pet_fountain_device(dev, {"a1L836QOWgx": "WaterDispenser"}))
        dev = {"iotId": "y", "productKey": "a1qWjyt9Iey"}
        self.assertTrue(helpers.is_pet_feeder_device(dev, {"a1qWjyt9Iey": "Feeder"}))

    def test_no_generic_switch_fallback(self):
        for dev in (FOUNTAIN, FEEDER):
            self.assertEqual(discovery.platform_for_device(dev, {}), "pet")
        self.assertFalse(helpers.is_fan_device(FOUNTAIN))


class _PetBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.devices = [FOUNTAIN, FEEDER]
        self.coordinator.props = {"fountain": dict(FOUNTAIN_PROPS),
                                  "feeder": dict(FEEDER_PROPS)}
        self.coordinator.is_device_online.return_value = True

        def _set_optimistic(iot_id, items, hold_duration=5.0):
            self.coordinator.props[iot_id].update(items)

        self.coordinator.async_set_optimistic_props.side_effect = _set_optimistic

    def make(self, platform, known=None):
        return pet.make_pet_entities(platform, self.coordinator, None, {},
                                     set() if known is None else known)

    def by_name(self, platform, iot_id, name):
        return next(e for e in self.make(platform)
                    if e._iot_id == iot_id and e._attr_name == name)

    @staticmethod
    def with_commander(entity):
        entity._commander = MagicMock()
        entity._commander.async_send = AsyncMock()
        entity._commander.in_skip.return_value = False
        return entity


class TestEntityCreation(_PetBase):
    def test_counts(self):
        names = lambda plat, iid: sorted(e._attr_name for e in self.make(plat) if e._iot_id == iid)  # noqa: E731
        self.assertEqual(names("switch", "fountain"), ["Pump"])
        self.assertEqual(names("select", "fountain"), ["Flow", "Mode"])
        self.assertEqual(len(names("sensor", "fountain")), 7)
        self.assertEqual(names("switch", "feeder"), ["Indicator light", "Slow feed"])
        self.assertEqual(names("select", "feeder"), [])
        self.assertEqual(len(names("sensor", "feeder")), 7)

    def test_no_duplicates(self):
        known: set = set()
        self.assertTrue(self.make("sensor", known))
        self.assertEqual(self.make("sensor", known), [])

    def test_gated_on_properties(self):
        self.coordinator.props = {"fountain": {}, "feeder": {"lampSwitch": 1}}
        self.assertEqual(self.make("select"), [])
        self.assertEqual([e._attr_name for e in self.make("switch")], ["Indicator light"])

    def test_unrelated_devices_ignored(self):
        self.coordinator.devices = [{"iotId": "plug", "categoryKey": "Socket"}]
        self.coordinator.props = {"plug": {"Switch": 1, "mode": 0}}
        self.assertEqual(self.make("switch"), [])

    def test_unique_ids_distinct(self):
        ids = [e._attr_unique_id for plat in ("switch", "select", "sensor")
               for e in self.make(plat)]
        self.assertEqual(len(ids), len(set(ids)))


class TestValues(_PetBase):
    def test_fountain_state(self):
        self.assertTrue(self.by_name("switch", "fountain", "Pump").is_on)
        self.assertEqual(self.by_name("select", "fountain", "Mode").current_option, "Continuous")
        self.assertEqual(self.by_name("select", "fountain", "Flow").current_option, "Medium")
        self.assertEqual(self.by_name("sensor", "fountain", "Water level").native_value, 100)
        self.assertEqual(self.by_name("sensor", "fountain", "Filter change in").native_value, 13)

    def test_unknown_enum_is_none(self):
        self.coordinator.props["fountain"]["WaterGear"] = 9
        self.assertIsNone(self.by_name("select", "fountain", "Flow").current_option)

    def test_feeder_state(self):
        last = self.by_name("sensor", "feeder", "Last feeding")
        self.assertEqual(last.native_value,
                         datetime(2026, 11, 23, 15, 36, 46, tzinfo=timezone.utc))
        self.assertEqual(last.extra_state_attributes, {"portions": 1})
        self.assertEqual(self.by_name("sensor", "feeder", "Portions today").native_value, 9)
        plan = self.by_name("sensor", "feeder", "Feeding plan")
        self.assertEqual(plan.native_value, 4)
        self.assertEqual(plan.extra_state_attributes["slots"][3]["time"], "18:00")
        self.assertEqual(self.by_name("sensor", "feeder", "Battery").native_value, 0)


class TestWrites(_PetBase):
    async def test_pump(self):
        pump = self.with_commander(self.by_name("switch", "fountain", "Pump"))
        await pump.async_turn_off()
        pump._commander.async_send.assert_awaited_with({"Switch": 0})
        self.assertFalse(pump.is_on)

    async def test_flow_and_mode(self):
        flow = self.with_commander(self.by_name("select", "fountain", "Flow"))
        await flow.async_select_option("High")
        flow._commander.async_send.assert_awaited_with({"WaterGear": 3})
        mode = self.with_commander(self.by_name("select", "fountain", "Mode"))
        await mode.async_select_option("Intermittent")
        mode._commander.async_send.assert_awaited_with({"mode": 1})

    async def test_feeder_switches(self):
        light = self.with_commander(self.by_name("switch", "feeder", "Indicator light"))
        await light.async_turn_off()
        light._commander.async_send.assert_awaited_with({"lampSwitch": 0})
        slow = self.with_commander(self.by_name("switch", "feeder", "Slow feed"))
        await slow.async_turn_on()
        slow._commander.async_send.assert_awaited_with({"slowFeed": 1})

    async def test_no_manual_feed_or_plan_writes(self):
        sensors = self.make("sensor")
        self.assertFalse(any(hasattr(e, "async_select_option") or hasattr(e, "async_turn_on")
                             for e in sensors))


if __name__ == "__main__":
    unittest.main()
