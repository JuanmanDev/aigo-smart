"""Unit tests for pet fountain / feeder support (issue #4)."""
import importlib.util
import os
import sys
import unittest
import unittest.mock
from datetime import datetime, timezone
from datetime import time as dtime
from unittest.mock import AsyncMock, MagicMock

sys.modules["voluptuous"] = MagicMock()
ha_mock = MagicMock()
ha_mock.__path__ = []
sys.modules["homeassistant"] = ha_mock
for _mod in (
    "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator", "homeassistant.const",
    "homeassistant.helpers.service", "homeassistant.helpers.config_validation",
    "homeassistant.exceptions", "homeassistant.components",
    "homeassistant.components.button", "homeassistant.components.binary_sensor",
    "homeassistant.components.event", "homeassistant.components.time",
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


class _EventEntity(_Entity):
    """Stand-in for homeassistant.components.event.EventEntity."""

    def _trigger_event(self, event_type, event_attributes=None):
        self.__dict__.setdefault("fired", []).append((event_type, event_attributes))


sys.modules["homeassistant.components.event"].EventEntity = _EventEntity
for _mod, _cls in (("number", "NumberEntity"), ("select", "SelectEntity"),
                   ("sensor", "SensorEntity"), ("switch", "SwitchEntity"),
                   ("binary_sensor", "BinarySensorEntity"), ("time", "TimeEntity")):
    setattr(sys.modules[f"homeassistant.components.{_mod}"], _cls,
            type(_cls, (_Entity,), {}))
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity


class _RestoreNumber(_Entity):
    """Stand-in for homeassistant.components.number.RestoreNumber."""

    _last_number_data = None

    async def async_added_to_hass(self):
        pass

    async def async_get_last_number_data(self):
        return self._last_number_data


sys.modules["homeassistant.components.number"].RestoreNumber = _RestoreNumber
sys.modules["homeassistant.components.button"].ButtonEntity = type("ButtonEntity", (_Entity,), {})
sys.modules["homeassistant.core"].callback = lambda func: func


class Invalid(Exception):
    pass


class ServiceValidationError(Exception):
    pass


class HomeAssistantError(Exception):
    pass


sys.modules["voluptuous"].Invalid = Invalid
sys.modules["homeassistant.exceptions"].ServiceValidationError = ServiceValidationError
sys.modules["homeassistant.exceptions"].HomeAssistantError = HomeAssistantError
# "today" in Europe/Lisbon (UTC+0 in November) on the day of the report
_DT = sys.modules["homeassistant.util.dt"]
_DT.start_of_local_day = lambda: datetime(2026, 11, 23, tzinfo=timezone.utc)
_DT.as_local = lambda when: when
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
set_properties = _load("set_properties")
feeding_plan = _load("feeding_plan")

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
        self.coordinator.tsl_models = {}

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
        self.assertEqual(len(names("sensor", "fountain")), 5)
        self.assertEqual(names("number", "fountain"), ["Cleaning interval", "Filter change interval"])
        self.assertEqual(names("binary_sensor", "fountain"),
                         ["Cleaning due", "Filter change due", "Water low"])
        self.assertEqual(names("binary_sensor", "feeder"),
                         ["Battery low", "Feeding", "Food jammed", "Food low", "Mains power"])
        self.assertEqual(names("event", "feeder"), ["Feeding"])
        self.assertEqual(names("switch", "feeder")[:2], ["Indicator light", "Meal 1"])
        self.assertEqual(names("time", "feeder"), [f"Meal {i} time" for i in range(1, 5)])
        self.assertEqual(names("switch", "feeder"),
                         ["Indicator light", "Meal 1", "Meal 2", "Meal 3", "Meal 4",
                          "Slow feed", "Voice on feed"])
        self.assertEqual(names("select", "feeder"), [])
        self.assertEqual(len(names("sensor", "feeder")), 8)
        # without the TSL: no manual feed and no reset buttons
        self.assertEqual(self.make("button"), [])
        self.assertEqual(names("number", "feeder"), [f"Meal {i} portions" for i in range(1, 5)])

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
        ids = [e._attr_unique_id for plat in ("switch", "select", "sensor", "number",
                                               "time", "binary_sensor", "event")
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

    async def test_fountain_writes_are_remembered(self):
        flow = self.with_commander(self.by_name("select", "fountain", "Flow"))
        await flow.async_select_option("High")
        self.coordinator.remember_write.assert_called_with("fountain", {"WaterGear": 3})

    async def test_feeder_writes_are_not_remembered(self):
        light = self.with_commander(self.by_name("switch", "feeder", "Indicator light"))
        await light.async_turn_off()
        self.coordinator.remember_write.assert_not_called()

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


# Excerpts of the TSLs in the reporter's device diagnostics (#4)
def _prop(identifier, access, type_, specs):
    return {"identifier": identifier, "accessMode": access,
            "dataType": {"type": type_, "specs": specs}}


FEEDER_TSL = {"properties": [
    _prop("manualFeeding", "rw", "int", {"min": "1", "max": "24", "step": "1"}),
    _prop("errorCode", "r", "enum", {"0": "正常", "244": "余粮不足", "245": "设备卡粮", "247": "电量低"}),
    _prop("feedState", "r", "enum", {"0": "待机", "1": "喂食中", "2": "喂食完成"}),
    _prop("batteryState", "r", "enum", {"0": "外电供电", "1": "电池供电"}),
    _prop("weightPer", "r", "enum", {"7": "每份约7g"}),
    _prop("lampSwitch", "rw", "bool", {"0": "关闭", "1": "打开"}),
    _prop("recordControl", "rw", "bool", {"0": "off", "1": "on"}),
    _prop("restoreFactory", "rw", "bool", {"0": "失败", "1": "成功"}),
]}
FOUNTAIN_TSL = {"properties": [
    _prop("Switch", "rw", "bool", {"0": "关", "1": "开"}),
    _prop("mode", "rw", "enum", {"0": "持续出水", "1": "间歇出水"}),
    _prop("WaterGear", "rw", "enum", {"1": "低档", "2": "中档", "3": "高档"}),
    _prop("FilterReset", "rw", "bool", {"0": "关", "1": "开"}),
    _prop("CleaningReset", "rw", "bool", {"0": "关", "1": "开"}),
    _prop("errorCode", "r", "enum", {"0": "正常", "236": "水位过低"}),
]}


class TestTslHelpers(unittest.TestCase):
    def test_writable_and_range(self):
        self.assertTrue(pet.tsl_writable(FEEDER_TSL, "manualFeeding"))
        self.assertFalse(pet.tsl_writable(FEEDER_TSL, "errorCode"))
        self.assertIsNone(pet.tsl_writable(FEEDER_TSL, "nope"))
        self.assertIsNone(pet.tsl_writable({}, "manualFeeding"))
        self.assertEqual(pet.tsl_int_range(FEEDER_TSL, "manualFeeding", (0, 0)), (1, 24))
        self.assertEqual(pet.tsl_int_range({}, "manualFeeding", (1, 9)), (1, 9))

    def test_tsl_enums_match_our_mappings(self):
        for prop, mapping in (("mode", pet.FOUNTAIN_MODES), ("WaterGear", pet.FOUNTAIN_FLOW)):
            specs = pet.tsl_property(FOUNTAIN_TSL, prop)["dataType"]["specs"]
            self.assertEqual(sorted(int(k) for k in specs), sorted(mapping))
        specs = pet.tsl_property(FEEDER_TSL, "errorCode")["dataType"]["specs"]
        self.assertEqual(sorted(int(k) for k in specs), sorted(pet.FEEDER_ERRORS))


class TestWithTsl(_PetBase):
    def setUp(self):
        super().setUp()
        self.coordinator.tsl_models = {"a1xEttfYqF2": FEEDER_TSL, "a163VQp0e0e": FOUNTAIN_TSL}

    def test_feed_entities_created_from_tsl(self):
        self.assertIn("Feed portions", [e._attr_name for e in self.make("number")])
        buttons = sorted(e._attr_name for e in self.make("button"))
        self.assertEqual(buttons, ["Feed", "Reset cleaning", "Reset filter"])

    def test_restore_factory_never_exposed(self):
        names = [e._attr_name for plat in ("button", "number", "switch", "select", "sensor")
                 for e in self.make(plat)]
        self.assertFalse(any("factory" in n.lower() for n in names))

    def _state(self):
        return {"coordinator": self.coordinator, "hass": MagicMock(), "client": MagicMock()}

    async def test_feed_button_sends_portions(self):
        state = self._state()
        number = next(e for e in pet.make_pet_entities("number", self.coordinator, state, {}, set())
                      if e._attr_name == "Feed portions")
        button = next(e for e in pet.make_pet_entities("button", self.coordinator, state, {}, set())
                      if e._attr_name == "Feed")
        self.with_commander(number)
        self.with_commander(button)
        self.assertEqual(number.native_value, 1)          # TSL minimum by default
        await number.async_set_native_value(3)
        number._commander.async_send.assert_not_awaited()  # setting portions never feeds
        await button.async_press()
        button._commander.async_send.assert_awaited_once_with({"manualFeeding": 3})

    async def test_feed_portions_restored_and_clamped_without_writing(self):
        state = self._state()
        number = next(e for e in pet.make_pet_entities("number", self.coordinator, state, {}, set())
                      if e._attr_name == "Feed portions")
        self.with_commander(number)
        number._last_number_data = MagicMock(native_value=40)
        await number.async_added_to_hass()
        self.assertEqual(number.native_value, 24)
        number._commander.async_send.assert_not_awaited()

    async def test_reset_buttons(self):
        reset = self.with_commander(self.by_name("button", "fountain", "Reset filter"))
        await reset.async_press()
        reset._commander.async_send.assert_awaited_with({"FilterReset": 1})

    def test_read_only_in_tsl_means_no_write_entity(self):
        tsl = {"properties": [_prop("Switch", "r", "bool", {})]}
        self.coordinator.tsl_models = {"a163VQp0e0e": tsl}
        self.assertEqual([e for e in self.make("switch") if e._iot_id == "fountain"], [])

    def test_grams_attribute(self):
        today = self.by_name("sensor", "feeder", "Portions today")
        self.assertEqual(today.extra_state_attributes, {"approx_grams": 63})


class TestNamedCodes(_PetBase):
    def test_codes_have_names_and_unknown_stay_raw(self):
        self.coordinator.props["feeder"].update(errorCode=244, feedState=1, batteryState=1)
        self.assertEqual(self.by_name("sensor", "feeder", "Error").native_value, "low_food")
        self.assertEqual(self.by_name("sensor", "feeder", "Feed state").native_value, "feeding")
        self.assertEqual(self.by_name("sensor", "feeder", "Power source").native_value, "battery")
        self.coordinator.props["feeder"]["errorCode"] = 999
        err = self.by_name("sensor", "feeder", "Error")
        self.assertEqual(err.native_value, 999)
        self.assertEqual(err.extra_state_attributes, {"code": 999})
        self.coordinator.props["fountain"].update(WaterStatus=1, errorCode=236)
        self.assertEqual(self.by_name("sensor", "fountain", "Water status").native_value, "low_water")
        self.assertEqual(self.by_name("sensor", "fountain", "Error").native_value,
                         "water_level_too_low")

    async def test_voice_switch(self):
        voice = self.with_commander(self.by_name("switch", "feeder", "Voice on feed"))
        await voice.async_turn_on()
        voice._commander.async_send.assert_awaited_with({"recordControl": 1})


class TestFountainOnePropertyPerWrite(unittest.TestCase):
    def test_fountain_commander_set_to_single_property(self):
        coordinator = MagicMock()
        coordinator.props = {"fountain": dict(FOUNTAIN_PROPS)}
        coordinator.tsl_models = {}
        state = {"coordinator": coordinator, "hass": MagicMock(), "client": MagicMock()}
        pet.FountainPump(coordinator, FOUNTAIN, state)
        self.assertTrue(state["commanders"]["fountain"].one_per_write)
        pet.FeederLight(coordinator, FEEDER, state)
        self.assertFalse(state["commanders"]["feeder"].one_per_write)


PLAN = FEEDER_PROPS["feedingPlan"]


class TestPlanEditing(unittest.TestCase):
    def test_edit_one_meal_keeps_the_others(self):
        self.assertEqual(pet.edit_feeding_plan(PLAN, 1, enabled=False),
                         "7F00000201,7F06000400,7F0C000201,7F12000301")
        self.assertEqual(pet.edit_feeding_plan(PLAN, 3, hour=19, minute=30, portions=1),
                         "7F00000201,7F06000401,7F0C000201,7F131E0101")

    def test_weekday_mask_and_unknown_items_are_kept(self):
        raw = "15080003 01,2A0A1E0201"   # the first item is not a valid meal
        self.assertEqual(pet.edit_feeding_plan(raw, 0, portions=4),
                         "15080003 01,2A0A1E0401")

    def test_missing_meal(self):
        self.assertIsNone(pet.edit_feeding_plan(PLAN, 4, enabled=False))
        self.assertIsNone(pet.edit_feeding_plan(None, 0, enabled=False))

    def test_build_sorted_every_day(self):
        meals = [{"time": dtime(18, 0), "portions": 3},
                 {"time": dtime(6, 5), "portions": 12, "enabled": False}]
        self.assertEqual(pet.build_feeding_plan(meals), "7F06050C00,7F12000301")
        self.assertEqual([s["time"] for s in pet.parse_feeding_plan(
            pet.build_feeding_plan(meals))], ["06:05", "18:00"])

    def test_classify_feeding(self):
        slots = pet.parse_feeding_plan(PLAN)

        def at(h, m):
            return datetime(2026, 11, 23, h, m, tzinfo=timezone.utc)

        self.assertEqual(pet.classify_feeding(at(12, 0), slots), "scheduled")
        self.assertEqual(pet.classify_feeding(at(23, 58), slots), "scheduled")  # 00:00 meal
        self.assertEqual(pet.classify_feeding(at(15, 36), slots), "manual")
        slots[2]["enabled"] = False
        self.assertEqual(pet.classify_feeding(at(12, 0), slots), "manual")


class TestAlerts(_PetBase):
    def test_all_clear(self):
        for iot_id, name in (("fountain", "Water low"), ("fountain", "Filter change due"),
                             ("fountain", "Cleaning due"), ("feeder", "Food low"),
                             ("feeder", "Food jammed"), ("feeder", "Battery low"),
                             ("feeder", "Feeding")):
            self.assertFalse(self.by_name("binary_sensor", iot_id, name).is_on, name)
        self.assertTrue(self.by_name("binary_sensor", "feeder", "Mains power").is_on)

    def test_problems(self):
        self.coordinator.props["fountain"].update(WaterStatus=0, errorCode=236,
                                                  FilterRemainderDay=0)
        self.assertTrue(self.by_name("binary_sensor", "fountain", "Water low").is_on)
        self.assertTrue(self.by_name("binary_sensor", "fountain", "Filter change due").is_on)
        self.coordinator.props["fountain"].update(WaterStatus=1, errorCode=0)
        self.assertTrue(self.by_name("binary_sensor", "fountain", "Water low").is_on)
        self.coordinator.props["feeder"].update(errorCode=244, feedState=1, batteryState=1)
        self.assertTrue(self.by_name("binary_sensor", "feeder", "Food low").is_on)
        self.assertFalse(self.by_name("binary_sensor", "feeder", "Food jammed").is_on)
        self.assertTrue(self.by_name("binary_sensor", "feeder", "Feeding").is_on)
        self.assertFalse(self.by_name("binary_sensor", "feeder", "Mains power").is_on)

    def test_unknown_until_seen(self):
        self.coordinator.props["feeder"] = {"errorCode": "?"}
        self.assertIsNone(self.by_name("binary_sensor", "feeder", "Food low").is_on)


class TestMeals(_PetBase):
    async def test_switch_disables_one_meal(self):
        meal = self.with_commander(self.by_name("switch", "feeder", "Meal 2"))
        self.assertTrue(meal.is_on)
        self.assertEqual(meal.extra_state_attributes["time"], "06:00")
        await meal.async_turn_off()
        meal._commander.async_send.assert_awaited_with(
            {"feedingPlan": "7F00000201,7F06000400,7F0C000201,7F12000301"})
        self.assertFalse(meal.is_on)                     # optimistic
        self.coordinator.remember_write.assert_not_called()

    async def test_time_and_portions(self):
        meal_time = self.with_commander(self.by_name("time", "feeder", "Meal 4 time"))
        self.assertEqual(meal_time.native_value, dtime(18, 0))
        await meal_time.async_set_value(dtime(19, 15))
        meal_time._commander.async_send.assert_awaited_with(
            {"feedingPlan": "7F00000201,7F06000401,7F0C000201,7F130F0301"})
        portions = self.with_commander(self.by_name("number", "feeder", "Meal 1 portions"))
        self.assertEqual(portions.native_value, 2)
        await portions.async_set_native_value(5)
        sent = portions._commander.async_send.await_args.args[0]["feedingPlan"]
        self.assertTrue(sent.startswith("7F00000501,"))

    def test_removed_meal_is_unavailable(self):
        meal = self.by_name("switch", "feeder", "Meal 4")
        self.coordinator.last_update_success = True
        self.coordinator.props["feeder"]["feedingPlan"] = "7F00000201"
        meal._handle_coordinator_update()
        self.assertFalse(meal.available)

    def test_new_meals_get_entities(self):
        known: set = set()
        self.make("switch", known)
        self.coordinator.props["feeder"]["feedingPlan"] = PLAN + ",7F15000101"
        self.assertEqual([e._attr_name for e in self.make("switch", known)], ["Meal 5"])

    def test_read_only_plan_in_tsl_means_no_meal_entities(self):
        self.coordinator.tsl_models = {"a1xEttfYqF2": {"properties": [
            _prop("feedingPlan", "r", "text", {})]}}
        self.assertEqual(self.make("time"), [])


class TestIntervals(_PetBase):
    async def test_interval_write_is_remembered(self):
        number = self.with_commander(self.by_name("number", "fountain", "Filter change interval"))
        self.assertEqual(number.native_value, 31)
        self.assertEqual((number._attr_native_min_value, number._attr_native_max_value), (0, 90))
        await number.async_set_native_value(45)
        number._commander.async_send.assert_awaited_with({"FilterReplaceTime": 45})
        self.coordinator.remember_write.assert_called_with("fountain", {"FilterReplaceTime": 45})

    def test_retired_sensors_removed(self):
        registry = MagicMock()

        def entity_id(domain, platform, uid):
            return f"sensor.{uid}" if uid.endswith("FilterReplaceTime") else None

        registry.async_get_entity_id.side_effect = entity_id
        er = MagicMock()
        er.async_get.return_value = registry
        sys.modules["homeassistant.helpers"].entity_registry = er
        pet.remove_retired_entities(MagicMock(), MagicMock(), self.coordinator)
        removed = sorted(c.args[0] for c in registry.async_remove.call_args_list)
        self.assertEqual(removed, ["sensor.feeder_pet_FilterReplaceTime",
                                   "sensor.fountain_pet_FilterReplaceTime"])


class TestFeedEvent(_PetBase):
    def test_history_never_fires_new_feeds_do(self):
        event = self.by_name("event", "feeder", "Feeding")
        self.coordinator.last_update_success = True
        event._handle_coordinator_update()
        self.assertEqual(event.__dict__.get("fired", []), [])
        # the 18:00 meal (3 portions), then a manual feed of 2 at 18:20
        noon = 1795435202                                  # 2026-11-23 12:00:02 UTC
        new = f"{noon + 6 * 3600 + 1200},2:{noon + 6 * 3600 + 1},3:" + FEED_RECORD
        self.coordinator.props["feeder"]["feedRecord"] = new
        event._handle_coordinator_update()
        self.assertEqual([(t, a["portions"]) for t, a in event.fired],
                         [("scheduled", 3), ("manual", 2)])
        event._handle_coordinator_update()                 # same data: nothing new
        self.assertEqual(len(event.fired), 2)


class TestSetFeedingPlanAction(_PetBase):
    def _call(self, meals):
        self.client = MagicMock()
        self.coordinator.async_refresh_device = AsyncMock()
        state = {"coordinator": self.coordinator, "client": self.client, "pk_catalog": {}}
        hass = MagicMock()

        async def run_job(func, *args):
            return func(*args)

        hass.async_add_executor_job = run_job
        call = MagicMock()
        call.data = {"device_id": "dev", "meals": [
            {"time": dtime(*m[0]), "portions": m[1], "enabled": m[2]} for m in meals]}
        return hass, call, state

    async def test_writes_whole_plan(self):
        hass, call, state = self._call([((19, 0), 3, True), ((7, 30), 2, False)])
        with unittest.mock.patch.object(feeding_plan, "resolve_device",
                                        return_value=("feeder", state, "Feeder")):
            await feeding_plan.async_handle_set_feeding_plan(hass, call)
        self.client.set_properties.assert_called_once_with(
            "feeder", {"feedingPlan": "7F071E0200,7F13000301"})

    async def test_rejects_non_feeder_and_bad_portions(self):
        self.coordinator.tsl_models = {"a1xEttfYqF2": FEEDER_TSL}
        hass, call, state = self._call([((7, 0), 30, True)])
        with unittest.mock.patch.object(feeding_plan, "resolve_device",
                                        return_value=("feeder", state, "Feeder")):
            with self.assertRaises(ServiceValidationError):
                await feeding_plan.async_handle_set_feeding_plan(hass, call)
        with unittest.mock.patch.object(feeding_plan, "resolve_device",
                                        return_value=("fountain", state, "Fountain")):
            with self.assertRaises(ServiceValidationError):
                await feeding_plan.async_handle_set_feeding_plan(hass, call)
        self.client.set_properties.assert_not_called()

    def test_duplicate_times_rejected(self):
        with self.assertRaises(Invalid):
            feeding_plan._unique_times([{"time": dtime(7, 0)}, {"time": dtime(7, 0)}])


if __name__ == "__main__":
    unittest.main()
