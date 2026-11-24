"""Pet water fountain and pet feeder entities (issue #4).

Observed on real devices (integration diagnostics + app screenshots from
the reporter; the full TSL has not been seen yet):

Pet water fountain (categoryKey "petWaterFountain", e.g. a163VQp0e0e,
"Nobleza S40101N34"):
  Switch               0/1   pump on/off (app: "Ligar")
  mode                 enum  0 = continuous; the app's only other choice is
                             "intermittent" (assumed 1)
  WaterGear            enum  2 = medium; app offers low / medium / high
                             (assumed 1 / 2 / 3)
  FilterRemainderDay   int   days until filter change (app: 13 = 13 days)
  CleaningRemainderDay int   days until cleaning (app: 10 = 10 days)
  FilterReplaceTime    int   filter change interval, days
  DeviceCleaningTime   int   cleaning interval, days
  waterLevvel          int   water level, % (sic, misspelt in the TSL)
  WaterStatus, errorCode     codes, meaning unknown (0 seen)

Pet feeder (categoryKey "petFeeder", e.g. a1xEttfYqF2):
  lampSwitch           0/1   indicator light
  slowFeed             0/1   slow feeding
  BatteryPercentage    float backup battery, %
  batteryState, feedState, errorCode   codes, meaning unknown (0 seen)
  feedRecord   "<unix>,<portions>:<unix>,<portions>:..."  newest first
  feedingPlan  "MMHHmmPPEE,..."  hex fields: weekday mask, hour, minute,
               portions, enabled (verified against the app's schedule:
               7F0C000201 = every day 12:00, 2 portions, on)

TSL (device diagnostics, #4) confirms the enums above and adds:
  fountain  errorCode 0 normal / 236 water level too low
            WaterStatus 0 normal / 1 low water
            FilterReset / CleaningReset (rw bool) -> reset buttons
  feeder    manualFeeding rw int 1-24 portions (write-only: never in the
            property snapshot; the app's manual feed adds a feedRecord entry)
            errorCode 0 normal / 244 low food / 245 food jammed / 247 low battery
            feedState 0 standby / 1 feeding / 2 done
            batteryState 0 mains / 1 battery
            recordControl rw bool: play the recorded voice when feeding
            weightPer enum {7: ~7 g per portion}
            restoreFactory is deliberately never exposed
Manual feeding is a button: it can only fire when pressed, never on startup,
reload or state restore. The portions are a local setting (not written to
the device until Feed is pressed).

Alerts are binary sensors (problem / battery / running) derived from the
codes above, so automations can use a plain on/off trigger. The feeding
schedule is editable per meal (enabled switch, time, portions) and as a
whole through the aigosmart.set_feeding_plan action; every edit rewrites the
feedingPlan string from the latest snapshot and keeps the other meals (and
their weekday masks) unchanged. A "Feeding" event entity fires for each new
feedRecord entry, as "scheduled" when it matches an enabled meal's time.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.event import EventEntity
from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .color_model import normalize_tsl
from .commander import get_commander
from .const import DOMAIN
from .coordinator import EVENT_DEVICES_CHANGED, AigoDataUpdateCoordinator
from .helpers import is_pet_feeder_device, is_pet_fountain_device

_LOGGER = logging.getLogger(__name__)

FOUNTAIN_MODES = {0: "Continuous", 1: "Intermittent"}
FOUNTAIN_FLOW = {1: "Low", 2: "Medium", 3: "High"}
FOUNTAIN_ERRORS = {0: "normal", 236: "water_level_too_low"}
FOUNTAIN_WATER_STATUS = {0: "normal", 1: "low_water"}
FEEDER_ERRORS = {0: "normal", 244: "low_food", 245: "food_jammed", 247: "low_battery"}
FEED_STATES = {0: "standby", 1: "feeding", 2: "done"}
FEEDER_POWER = {0: "mains", 1: "battery"}

PROP_MANUAL_FEEDING = "manualFeeding"
MANUAL_FEED_DEFAULT_RANGE = (1, 24)


def tsl_property(tsl: Any, identifier: str) -> dict | None:
    """The TSL property definition for ``identifier``, if the TSL has one."""
    for prop in normalize_tsl(tsl).get("properties", []) or []:
        if isinstance(prop, dict) and prop.get("identifier") == identifier:
            return prop
    return None


def tsl_writable(tsl: Any, identifier: str) -> bool | None:
    """True / False from the TSL accessMode; None when the TSL is unknown."""
    if not normalize_tsl(tsl).get("properties"):
        return None
    prop = tsl_property(tsl, identifier)
    if prop is None:
        return None
    return "w" in str(prop.get("accessMode", ""))


def tsl_int_range(tsl: Any, identifier: str,
                  default: tuple[int, int]) -> tuple[int, int]:
    prop = tsl_property(tsl, identifier) or {}
    specs = (prop.get("dataType") or {}).get("specs") or {}
    try:
        return int(specs["min"]), int(specs["max"])
    except (KeyError, TypeError, ValueError):
        return default


# ------------------------------------------------------------------ parsing --

def parse_feed_record(raw: Any) -> list[tuple[datetime, int]]:
    """Parse feedRecord into [(utc time, portions)], newest first."""
    out: list[tuple[datetime, int]] = []
    if not isinstance(raw, str):
        return out
    for entry in raw.split(":"):
        ts, _, portions = entry.partition(",")
        try:
            when = datetime.fromtimestamp(int(ts), timezone.utc)
            out.append((when, int(portions)))
        except (ValueError, OverflowError, OSError):
            continue
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def portions_since(records: list[tuple[datetime, int]], since: datetime) -> int:
    return sum(portions for when, portions in records if when >= since)


def parse_feeding_plan(raw: Any) -> list[dict[str, Any]]:
    """Parse feedingPlan ("7F06000401,...") into readable schedule slots."""
    slots: list[dict[str, Any]] = []
    if not isinstance(raw, str):
        return slots
    for item in raw.split(","):
        item = item.strip()
        if len(item) != 10:
            continue
        try:
            mask, hour, minute, portions, enabled = (
                int(item[i:i + 2], 16) for i in range(0, 10, 2))
        except ValueError:
            continue
        if hour > 23 or minute > 59:
            continue
        slots.append({
            "time": f"{hour:02d}:{minute:02d}",
            "portions": portions,
            "enabled": bool(enabled),
            "every_day": mask == 0x7F,
            "days_mask": f"{mask:02X}",
        })
    return slots


EVERY_DAY_MASK = 0x7F
MAX_PLAN_MEALS = 10      # the reporter's feeder has 4; the app's limit is not known
SCHEDULE_MATCH_MINUTES = 3


def encode_plan_item(mask: int, hour: int, minute: int, portions: int,
                     enabled: bool) -> str:
    return f"{mask:02X}{hour:02X}{minute:02X}{portions:02X}{1 if enabled else 0:02X}"


def edit_feeding_plan(raw: Any, index: int, *, hour: int | None = None,
                      minute: int | None = None, portions: int | None = None,
                      enabled: bool | None = None) -> str | None:
    """Change one meal of a feedingPlan string; None if that meal does not exist.

    ``index`` counts the meals parse_feeding_plan returns. Every other item is
    kept byte for byte, and the edited meal keeps its weekday mask.
    """
    if not isinstance(raw, str):
        return None
    items = raw.split(",")
    seen = -1
    for pos, item in enumerate(items):
        if not parse_feeding_plan(item):
            continue
        seen += 1
        if seen != index:
            continue
        item = item.strip()
        mask, old_h, old_m, old_p, old_e = (int(item[i:i + 2], 16) for i in range(0, 10, 2))
        items[pos] = encode_plan_item(
            mask,
            old_h if hour is None else hour,
            old_m if minute is None else minute,
            old_p if portions is None else portions,
            bool(old_e) if enabled is None else enabled,
        )
        return ",".join(items)
    return None


def build_feeding_plan(meals: list[dict[str, Any]]) -> str:
    """feedingPlan string for [{"time": time, "portions": n, "enabled": bool}], every day."""
    return ",".join(
        encode_plan_item(EVERY_DAY_MASK, meal["time"].hour, meal["time"].minute,
                         int(meal["portions"]), bool(meal.get("enabled", True)))
        for meal in sorted(meals, key=lambda m: (m["time"].hour, m["time"].minute)))


def classify_feeding(when_local: datetime, slots: list[dict[str, Any]]) -> str:
    """"scheduled" when a feed happened at an enabled meal's time, else "manual"."""
    minute_of_day = when_local.hour * 60 + when_local.minute
    for slot in slots:
        if not slot["enabled"]:
            continue
        hour, minute = (int(x) for x in slot["time"].split(":"))
        diff = abs(minute_of_day - (hour * 60 + minute))
        if min(diff, 1440 - diff) <= SCHEDULE_MATCH_MINUTES:
            return "scheduled"
    return "manual"


# --------------------------------------------------------------- base entity --

class PetEntity(CoordinatorEntity):
    """Common base: device info, availability, optimistic writes."""

    _attr_has_entity_name = True
    _prop: str = ""
    _props: tuple[str, ...] = ()      # accepted identifiers, first match wins
    _default_name = "Aigo pet device"

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._iot_id = dev.get("iotId", "")
        self._product_key = dev.get("productKey", "")
        props = coordinator.props.get(self._iot_id, {})
        self._prop = next((p for p in self.identifiers() if p in props),
                          self.identifiers()[0])
        self._attr_unique_id = f"{self._iot_id}_pet_{self.key()}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or self._default_name,
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "pet device",
        )
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        # The fountain applies HA's writes but never reports the new value
        # back: the cloud shadow (and so the app) keeps the old one (#4).
        # Keep showing what was written until the shadow really changes.
        self._remember_writes = is_pet_fountain_device(dev)
        if self._commander is not None and self._remember_writes:
            # one property per write, like the fan / colour fixes
            self._commander.one_per_write = True
        self._apply(props)

    # entities that write must not be created if the TSL marks them read-only
    _writes = False

    @classmethod
    def identifiers(cls) -> tuple[str, ...]:
        return cls._props or (cls._prop,)

    @classmethod
    def wanted(cls, props: dict, tsl: dict) -> bool:
        """Create this entity for a device with these properties / TSL?"""
        if not any(p in props for p in cls.identifiers()):
            return False
        if cls._writes and tsl_writable(tsl, cls.identifiers()[0]) is False:
            return False
        return True

    @classmethod
    def key(cls) -> str:
        return cls.identifiers()[0]

    @property
    def available(self) -> bool:
        return self._available and self.coordinator.last_update_success

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        self._available = self._coordinator.is_device_online(self._iot_id)
        if props and not (self._commander and self._commander.in_skip()):
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        raise NotImplementedError

    async def _async_write(self, value: int) -> None:
        await self._async_write_items({self._prop: int(value)})

    async def _async_write_items(self, items: dict[str, Any]) -> None:
        if self._remember_writes:
            self._coordinator.remember_write(self._iot_id, items)
        self._coordinator.async_set_optimistic_props(
            self._iot_id, items, hold_duration=5.0)
        self._apply(self._coordinator.props.get(self._iot_id, {}))
        self.async_write_ha_state()
        if self._commander is not None:
            await self._commander.async_send(items)
            return
        try:
            await self.hass.async_add_executor_job(
                self._coordinator.client.set_properties, self._iot_id, items)
        except Exception as exc:
            _LOGGER.warning("AigoSmart pet device write %s failed for %s: %s",
                            items, self._iot_id, exc)
            self._coordinator.clear_optimistic_props(self._iot_id)
            await self._coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()


# ------------------------------------------------------------------ switches --

class PetSwitch(PetEntity, SwitchEntity):
    _is_on = False
    _writes = True

    @property
    def is_on(self) -> bool:
        return self._is_on

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            try:
                self._is_on = bool(int(props[self._prop]))
            except (TypeError, ValueError):
                pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(0)


class FountainPump(PetSwitch):
    _prop = "Switch"
    _attr_name = "Pump"
    _attr_icon = "mdi:water-pump"
    _default_name = "Aigo pet fountain"


class FeederLight(PetSwitch):
    _prop = "lampSwitch"
    _attr_name = "Indicator light"
    _attr_icon = "mdi:led-on"
    _attr_entity_category = EntityCategory.CONFIG
    _default_name = "Aigo pet feeder"


class FeederVoice(PetSwitch):
    _prop = "recordControl"
    _attr_name = "Voice on feed"
    _attr_icon = "mdi:account-voice"
    _attr_entity_category = EntityCategory.CONFIG
    _default_name = "Aigo pet feeder"


class FeederSlowFeed(PetSwitch):
    _prop = "slowFeed"
    _attr_name = "Slow feed"
    _attr_icon = "mdi:speedometer-slow"
    _attr_entity_category = EntityCategory.CONFIG
    _default_name = "Aigo pet feeder"


# ------------------------------------------------------------------- selects --

class PetSelect(PetEntity, SelectEntity):
    _mapping: dict[int, str] = {}
    _writes = True
    _current: str | None = None

    @property
    def current_option(self) -> str | None:
        return self._current

    def _apply(self, props: dict) -> None:
        if self._prop not in props:
            return
        try:
            raw = int(props[self._prop])
        except (TypeError, ValueError):
            return
        self._current = self._mapping.get(raw)
        if self._current is None:
            _LOGGER.debug("AigoSmart: unknown %s value %s for %s",
                          self._prop, raw, self._iot_id)

    async def async_select_option(self, option: str) -> None:
        for value, label in self._mapping.items():
            if label == option:
                await self._async_write(value)
                return


class FountainMode(PetSelect):
    _prop = "mode"
    _attr_name = "Mode"
    _attr_icon = "mdi:water-sync"
    _mapping = FOUNTAIN_MODES
    _attr_options = list(FOUNTAIN_MODES.values())
    _default_name = "Aigo pet fountain"


class FountainFlow(PetSelect):
    _prop = "WaterGear"
    _attr_name = "Flow"
    _attr_icon = "mdi:waves"
    _mapping = FOUNTAIN_FLOW
    _attr_options = list(FOUNTAIN_FLOW.values())
    _default_name = "Aigo pet fountain"


# ------------------------------------------------------------------- sensors --

class PetNumberSensor(PetEntity, SensorEntity):
    """Plain numeric property."""

    _value: float | int | None = None

    @property
    def native_value(self) -> float | int | None:
        return self._value

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            raw = props[self._prop]
            try:
                self._value = int(raw) if float(raw).is_integer() else float(raw)
            except (TypeError, ValueError):
                self._value = None


class _DaysSensor(PetNumberSensor):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _default_name = "Aigo pet fountain"


class FountainFilterRemaining(_DaysSensor):
    _prop = "FilterRemainderDay"
    _attr_name = "Filter change in"
    _attr_icon = "mdi:air-filter"


class FountainCleaningRemaining(_DaysSensor):
    _prop = "CleaningRemainderDay"
    _attr_name = "Cleaning due in"
    _attr_icon = "mdi:spray-bottle"


class FountainWaterLevel(PetNumberSensor):
    _props = ("waterLevvel", "waterLevel", "WaterLevel")
    _attr_name = "Water level"
    _attr_icon = "mdi:water-percent"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _default_name = "Aigo pet fountain"


class _CodeSensor(PetNumberSensor):
    """Status / error code, named from the device TSL; unknown codes stay raw."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"
    _names: dict[int, str] = {}

    @property
    def native_value(self) -> str | int | float | None:
        if isinstance(self._value, int) and self._value in self._names:
            return self._names[self._value]
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"code": self._value}


class FountainWaterStatus(_CodeSensor):
    _prop = "WaterStatus"
    _attr_name = "Water status"
    _names = FOUNTAIN_WATER_STATUS
    _default_name = "Aigo pet fountain"


class FountainError(_CodeSensor):
    _prop = "errorCode"
    _attr_name = "Error"
    _names = FOUNTAIN_ERRORS
    _attr_icon = "mdi:alert-circle-outline"
    _default_name = "Aigo pet fountain"


class FeederError(_CodeSensor):
    _prop = "errorCode"
    _attr_name = "Error"
    _names = FEEDER_ERRORS
    _attr_icon = "mdi:alert-circle-outline"
    _default_name = "Aigo pet feeder"


class FeederFeedState(_CodeSensor):
    _prop = "feedState"
    _attr_name = "Feed state"
    _names = FEED_STATES
    _default_name = "Aigo pet feeder"


class FeederBatteryState(_CodeSensor):
    _prop = "batteryState"
    _attr_name = "Power source"
    _names = FEEDER_POWER
    _default_name = "Aigo pet feeder"


class FeederBattery(PetNumberSensor):
    _prop = "BatteryPercentage"
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _default_name = "Aigo pet feeder"


class _FeedRecordSensor(PetEntity, SensorEntity):
    _prop = "feedRecord"
    _default_name = "Aigo pet feeder"
    _records: list[tuple[datetime, int]] = []

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            self._records = parse_feed_record(props[self._prop])


class FeederLastFeed(_FeedRecordSensor):
    _attr_name = "Last feeding"
    _attr_icon = "mdi:food-drumstick"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @classmethod
    def key(cls) -> str:
        return "feedRecord_last"

    @property
    def native_value(self) -> datetime | None:
        return self._records[0][0] if self._records else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"portions": self._records[0][1]} if self._records else {}


def _grams_per_portion(tsl: Any) -> int | None:
    """weightPer is an enum with the portion size as its only value (7 g)."""
    specs = ((tsl_property(tsl, "weightPer") or {}).get("dataType") or {}).get("specs")
    if isinstance(specs, dict) and len(specs) == 1:
        try:
            return int(next(iter(specs)))
        except ValueError:
            return None
    return None


class FeederPortionsToday(_FeedRecordSensor):
    _attr_name = "Portions today"
    _attr_icon = "mdi:bowl-mix"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @classmethod
    def key(cls) -> str:
        return "feedRecord_today"

    @property
    def native_value(self) -> int:
        return portions_since(self._records, dt_util.start_of_local_day())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        grams = _grams_per_portion(
            self._coordinator.tsl_models.get(self._product_key) or {})
        return {"approx_grams": self.native_value * grams} if grams else {}


class FeederPlan(PetEntity, SensorEntity):
    """Read-only view of the schedule; the plan is never written."""

    _prop = "feedingPlan"
    _attr_name = "Feeding plan"
    _attr_icon = "mdi:calendar-clock"
    _default_name = "Aigo pet feeder"
    _slots: list[dict[str, Any]] = []

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            self._slots = parse_feeding_plan(props[self._prop])

    @property
    def native_value(self) -> int:
        """Number of enabled schedule slots."""
        return sum(1 for slot in self._slots if slot["enabled"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"slots": self._slots}


# --------------------------------------------------------------- manual feed --

class _ManualFeedEntity(PetEntity):
    """Created from the TSL: manualFeeding is never in the property snapshot."""

    _prop = PROP_MANUAL_FEEDING
    _default_name = "Aigo pet feeder"

    @classmethod
    def wanted(cls, props: dict, tsl: dict) -> bool:
        return bool(tsl_writable(tsl, PROP_MANUAL_FEEDING))

    def _apply(self, props: dict) -> None:
        pass

    def _portions_store(self) -> dict[str, int]:
        return self._state.setdefault("pet_feed_portions", {})


class FeederFeedPortions(_ManualFeedEntity, RestoreNumber):
    """How many portions the Feed button dispenses (local setting only).

    The last value is restored after a restart. Restoring or changing it never
    writes to the feeder; only pressing Feed does.
    """

    _attr_name = "Feed portions"
    _attr_icon = "mdi:numeric"
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_entity_category = EntityCategory.CONFIG

    @classmethod
    def key(cls) -> str:
        return "manualFeeding_portions"

    def __init__(self, coordinator, dev, state) -> None:
        self._state = state if state is not None else {}
        super().__init__(coordinator, dev, state)
        tsl = coordinator.tsl_models.get(dev.get("productKey", "")) or {}
        low, high = tsl_int_range(tsl, PROP_MANUAL_FEEDING, MANUAL_FEED_DEFAULT_RANGE)
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._portions_store().setdefault(self._iot_id, low)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            low, high = self._attr_native_min_value, self._attr_native_max_value
            self._portions_store()[self._iot_id] = max(low, min(high, int(last.native_value)))

    @property
    def native_value(self) -> int:
        return self._portions_store()[self._iot_id]

    async def async_set_native_value(self, value: float) -> None:
        # local only: nothing is sent to the feeder here
        self._portions_store()[self._iot_id] = int(value)
        self.async_write_ha_state()


class FeederFeedButton(_ManualFeedEntity, ButtonEntity):
    """Dispense the configured portions now (like the app's manual feed)."""

    _attr_name = "Feed"
    _attr_icon = "mdi:food-drumstick"

    @classmethod
    def key(cls) -> str:
        return "manualFeeding_button"

    def __init__(self, coordinator, dev, state) -> None:
        self._state = state if state is not None else {}
        super().__init__(coordinator, dev, state)
        tsl = coordinator.tsl_models.get(dev.get("productKey", "")) or {}
        self._range = tsl_int_range(tsl, PROP_MANUAL_FEEDING, MANUAL_FEED_DEFAULT_RANGE)

    async def async_press(self) -> None:
        low, high = self._range
        portions = self._portions_store().get(self._iot_id, low)
        portions = max(low, min(high, int(portions)))
        _LOGGER.info("AigoSmart manual feed for %s: %d portion(s)", self._iot_id, portions)
        items = {PROP_MANUAL_FEEDING: portions}
        if self._commander is not None:
            await self._commander.async_send(items)
            return
        await self.hass.async_add_executor_job(
            self._coordinator.client.set_properties, self._iot_id, items)


class _FountainResetButton(PetEntity, ButtonEntity):
    """Write 1 to a reset property (TSL rw bool, not in the snapshot)."""

    _attr_entity_category = EntityCategory.CONFIG
    _default_name = "Aigo pet fountain"

    @classmethod
    def wanted(cls, props: dict, tsl: dict) -> bool:
        return bool(tsl_writable(tsl, cls._prop))

    def _apply(self, props: dict) -> None:
        pass

    async def async_press(self) -> None:
        items = {self._prop: 1}
        if self._commander is not None:
            await self._commander.async_send(items)
            return
        await self.hass.async_add_executor_job(
            self._coordinator.client.set_properties, self._iot_id, items)


class FountainResetFilter(_FountainResetButton):
    _prop = "FilterReset"
    _attr_name = "Reset filter"
    _attr_icon = "mdi:air-filter"


class FountainResetCleaning(_FountainResetButton):
    _prop = "CleaningReset"
    _attr_name = "Reset cleaning"
    _attr_icon = "mdi:spray-bottle"


class FeederVoicePrompt(_CodeSensor):
    _prop = "recordExists"
    _attr_name = "Voice prompt recorded"
    _attr_icon = "mdi:microphone"
    _names = {0: "no", 1: "yes"}
    _default_name = "Aigo pet feeder"


# -------------------------------------------------------- interval numbers --

class _IntervalNumber(PetEntity, NumberEntity):
    """Filter / cleaning interval in days (rw in the TSL, set in the app)."""

    _writes = True
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_entity_category = EntityCategory.CONFIG
    _default_name = "Aigo pet fountain"
    _value: int | None = None

    def __init__(self, coordinator, dev, state) -> None:
        tsl = coordinator.tsl_models.get(dev.get("productKey", "")) or {}
        # TSL: 0-90 days (also the fallback until the TSL has been fetched)
        self._attr_native_min_value, self._attr_native_max_value = tsl_int_range(
            tsl, self.identifiers()[0], (0, 90))
        super().__init__(coordinator, dev, state)

    @property
    def native_value(self) -> int | None:
        return self._value

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            try:
                self._value = int(props[self._prop])
            except (TypeError, ValueError):
                self._value = None

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(int(value))


class FountainFilterIntervalNumber(_IntervalNumber):
    _prop = "FilterReplaceTime"
    _attr_name = "Filter change interval"
    _attr_icon = "mdi:air-filter"


class FountainCleaningIntervalNumber(_IntervalNumber):
    _prop = "DeviceCleaningTime"
    _attr_name = "Cleaning interval"
    _attr_icon = "mdi:spray-bottle"


# Replaced by the numbers above; their old sensor entities are removed
RETIRED_SENSOR_KEYS = ("FilterReplaceTime", "DeviceCleaningTime")


# ------------------------------------------------------------ binary sensors --

def _int_prop(props: dict, prop: str) -> int | None:
    try:
        return int(props[prop])
    except (KeyError, TypeError, ValueError):
        return None


class PetAlert(PetEntity, BinarySensorEntity):
    """On / off from one or more codes; stays unknown until a code is seen."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _key = ""
    _on: bool | None = None

    @classmethod
    def key(cls) -> str:
        return cls._key

    @classmethod
    def wanted(cls, props: dict, tsl: dict) -> bool:
        # every listed property feeds the test, so any one of them is enough
        return any(p in props for p in cls.identifiers())

    @property
    def is_on(self) -> bool | None:
        return self._on

    def _test(self, props: dict) -> bool | None:
        raise NotImplementedError

    def _apply(self, props: dict) -> None:
        value = self._test(props)
        if value is not None:
            self._on = value


class FountainWaterLow(PetAlert):
    _props = ("WaterStatus", "errorCode")
    _key = "alert_water_low"
    _attr_name = "Water low"
    _attr_icon = "mdi:water-alert"
    _default_name = "Aigo pet fountain"

    def _test(self, props: dict) -> bool | None:
        status, error = _int_prop(props, "WaterStatus"), _int_prop(props, "errorCode")
        if status is None and error is None:
            return None
        return status == 1 or error == 236


class _DaysDue(PetAlert):
    _default_name = "Aigo pet fountain"

    def _test(self, props: dict) -> bool | None:
        days = _int_prop(props, self._prop)
        return None if days is None else days <= 0


class FountainFilterDue(_DaysDue):
    _prop = "FilterRemainderDay"
    _key = "alert_filter_due"
    _attr_name = "Filter change due"
    _attr_icon = "mdi:air-filter"


class FountainCleaningDue(_DaysDue):
    _prop = "CleaningRemainderDay"
    _key = "alert_cleaning_due"
    _attr_name = "Cleaning due"
    _attr_icon = "mdi:spray-bottle"


class _CodeAlert(PetAlert):
    _code = -1
    _default_name = "Aigo pet feeder"

    def _test(self, props: dict) -> bool | None:
        code = _int_prop(props, self._prop)
        return None if code is None else code == self._code


class FeederFoodLow(_CodeAlert):
    _prop = "errorCode"
    _code = 244
    _key = "alert_food_low"
    _attr_name = "Food low"
    _attr_icon = "mdi:food-drumstick-off"


class FeederFoodJammed(_CodeAlert):
    _prop = "errorCode"
    _code = 245
    _key = "alert_food_jammed"
    _attr_name = "Food jammed"
    _attr_icon = "mdi:alert-octagon"


class FeederBatteryLow(_CodeAlert):
    _prop = "errorCode"
    _code = 247
    _key = "alert_battery_low"
    _attr_name = "Battery low"
    _attr_device_class = BinarySensorDeviceClass.BATTERY


class FeederFeeding(_CodeAlert):
    _prop = "feedState"
    _code = 1
    _key = "feeding"
    _attr_name = "Feeding"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:food-drumstick"


class FeederMainsPower(_CodeAlert):
    _prop = "batteryState"
    _code = 0
    _key = "mains_power"
    _attr_name = "Mains power"
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_entity_category = EntityCategory.DIAGNOSTIC


# ------------------------------------------------------------ feeding plan --

class _MealEntity(PetEntity):
    """One meal of the feeding schedule (feedingPlan), by position."""

    _prop = "feedingPlan"
    _writes = True
    _kind = ""
    _attr_entity_category = EntityCategory.CONFIG
    _default_name = "Aigo pet feeder"

    def __init__(self, coordinator, dev, state, index: int) -> None:
        self._index = index
        self._slot: dict[str, Any] | None = None
        super().__init__(coordinator, dev, state)
        suffix = f"_{self._kind}" if self._kind else ""
        self._attr_unique_id = f"{self._iot_id}_pet_feedingPlan_{index + 1}{suffix}"
        self._attr_name = f"Meal {index + 1}{' ' + self._kind if self._kind else ''}"

    @classmethod
    def meal_key(cls, index: int) -> str:
        return f"feedingPlan_{index + 1}_{cls._kind or 'enabled'}"

    @property
    def available(self) -> bool:
        return super().available and self._slot is not None

    def _apply(self, props: dict) -> None:
        if self._prop not in props:
            return
        slots = parse_feeding_plan(props[self._prop])
        self._slot = slots[self._index] if self._index < len(slots) else None

    async def _async_edit(self, **changes: Any) -> None:
        raw = self._coordinator.props.get(self._iot_id, {}).get(self._prop)
        new = edit_feeding_plan(raw, self._index, **changes)
        if new is None:
            _LOGGER.warning("AigoSmart: meal %d no longer exists on %s",
                            self._index + 1, self._iot_id)
            return
        await self._async_write_items({self._prop: new})


class FeederMealSwitch(_MealEntity, SwitchEntity):
    _attr_icon = "mdi:calendar-clock"

    @property
    def is_on(self) -> bool | None:
        return None if self._slot is None else self._slot["enabled"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._slot is None:
            return {}
        return {k: self._slot[k] for k in ("time", "portions", "every_day", "days_mask")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_edit(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_edit(enabled=False)


class FeederMealTime(_MealEntity, TimeEntity):
    _kind = "time"
    _attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> time | None:
        if self._slot is None:
            return None
        hour, minute = (int(x) for x in self._slot["time"].split(":"))
        return time(hour, minute)

    async def async_set_value(self, value: time) -> None:
        await self._async_edit(hour=value.hour, minute=value.minute)


class FeederMealPortions(_MealEntity, NumberEntity):
    _kind = "portions"
    _attr_icon = "mdi:bowl-mix"
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, coordinator, dev, state, index: int) -> None:
        tsl = coordinator.tsl_models.get(dev.get("productKey", "")) or {}
        # assumed to share the manual feed's range (TSL: 1-24)
        self._attr_native_min_value, self._attr_native_max_value = tsl_int_range(
            tsl, PROP_MANUAL_FEEDING, MANUAL_FEED_DEFAULT_RANGE)
        super().__init__(coordinator, dev, state, index)

    @property
    def native_value(self) -> int | None:
        return None if self._slot is None else self._slot["portions"]

    async def async_set_native_value(self, value: float) -> None:
        await self._async_edit(portions=int(value))


MEAL_ENTITIES: dict[str, type[_MealEntity]] = {
    "switch": FeederMealSwitch,
    "time": FeederMealTime,
    "number": FeederMealPortions,
}


# ------------------------------------------------------------- feed events --

class FeederFeedEvent(PetEntity, EventEntity):
    """Fires once per new feedRecord entry (never for history seen at startup)."""

    _prop = "feedRecord"
    _attr_name = "Feeding"
    _attr_icon = "mdi:food-drumstick"
    _attr_event_types = ["scheduled", "manual"]
    _default_name = "Aigo pet feeder"
    _newest: datetime | None = None

    @classmethod
    def key(cls) -> str:
        return "feedRecord_event"

    def _apply(self, props: dict) -> None:
        if self._prop not in props:
            return
        records = parse_feed_record(props[self._prop])
        if not records:
            return
        if self._newest is None:
            self._newest = records[0][0]
            return
        fresh = [r for r in records if r[0] > self._newest]
        if not fresh:
            return
        self._newest = fresh[0][0]
        slots = parse_feeding_plan(props.get("feedingPlan"))
        for when, portions in reversed(fresh):          # oldest first
            local = dt_util.as_local(when)
            self._trigger_event(classify_feeding(local, slots), {
                "portions": portions,
                "fed_at": when.isoformat(),
            })
            if getattr(self, "hass", None) is not None:
                # one state per feed, so automations see each of them
                self.async_write_ha_state()


# ------------------------------------------------------------------- factory --

FOUNTAIN_ENTITIES: dict[str, tuple[type[PetEntity], ...]] = {
    "binary_sensor": (FountainWaterLow, FountainFilterDue, FountainCleaningDue),
    "button": (FountainResetFilter, FountainResetCleaning),
    "number": (FountainFilterIntervalNumber, FountainCleaningIntervalNumber),
    "switch": (FountainPump,),
    "select": (FountainMode, FountainFlow),
    "sensor": (FountainWaterLevel, FountainFilterRemaining,
               FountainCleaningRemaining, FountainWaterStatus, FountainError),
}

FEEDER_ENTITIES: dict[str, tuple[type[PetEntity], ...]] = {
    "binary_sensor": (FeederFoodLow, FeederFoodJammed, FeederBatteryLow,
                      FeederFeeding, FeederMainsPower),
    "button": (FeederFeedButton,),
    "event": (FeederFeedEvent,),
    "number": (FeederFeedPortions,),
    "switch": (FeederLight, FeederSlowFeed, FeederVoice),
    "sensor": (FeederLastFeed, FeederPortionsToday, FeederPlan, FeederBattery,
               FeederBatteryState, FeederFeedState, FeederError, FeederVoicePrompt),
}


def _make_meal_entities(platform: str, coordinator, dev: dict, state, props: dict,
                        tsl: dict, known: set) -> list:
    """Per-meal entities for each meal currently in the feeding plan."""
    cls = MEAL_ENTITIES.get(platform)
    if cls is None or "feedingPlan" not in props:
        return []
    if tsl_writable(tsl, "feedingPlan") is False:
        return []
    out = []
    iot_id = dev.get("iotId", "")
    for index in range(len(parse_feeding_plan(props["feedingPlan"]))):
        key = f"{iot_id}_pet_{cls.meal_key(index)}_{platform}"
        if key in known:
            continue
        known.add(key)
        out.append(cls(coordinator, dev, state, index))
    return out


def make_pet_entities(platform: str, coordinator: AigoDataUpdateCoordinator,
                      state: dict | None, pk_catalog: dict, known: set) -> list:
    """Build not-yet-created pet fountain / feeder entities for one platform.

    Each entity is only created once its property has been seen, so a model
    with a different TSL gets fewer entities instead of wrong ones.
    """
    out: list = []
    for dev in coordinator.devices:
        iot_id = dev.get("iotId", "")
        if not iot_id:
            continue
        feeder = False
        if is_pet_fountain_device(dev, pk_catalog):
            classes = FOUNTAIN_ENTITIES.get(platform, ())
        elif is_pet_feeder_device(dev, pk_catalog):
            classes = FEEDER_ENTITIES.get(platform, ())
            feeder = True
        else:
            continue
        props = coordinator.props.get(iot_id, {})
        tsl = coordinator.tsl_models.get(dev.get("productKey", "")) or {}
        for cls in classes:
            key = f"{iot_id}_pet_{cls.key()}_{platform}"
            if key in known or not cls.wanted(props, tsl):
                continue
            known.add(key)
            out.append(cls(coordinator, dev, state))
        if feeder:
            out.extend(_make_meal_entities(platform, coordinator, dev, state,
                                           props, tsl, known))
    return out


def remove_retired_entities(hass, entry, coordinator) -> None:
    """Drop entities that newer versions replaced (e.g. interval sensors -> numbers)."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for dev in coordinator.devices:
        iot_id = dev.get("iotId", "")
        for key in RETIRED_SENSOR_KEYS:
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{iot_id}_pet_{key}")
            if entity_id:
                registry.async_remove(entity_id)


def setup_pet_platform(hass, entry, state: dict, platform: str,
                       async_add_entities) -> None:
    """Add pet entities now and whenever new devices / properties appear."""
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry = state["registry"]
    pk_catalog: dict = state.get("pk_catalog", {})
    registry.register(platform)
    if platform == "sensor":
        remove_retired_entities(hass, entry, coordinator)

    @callback
    def _async_add(*_args) -> None:
        new = make_pet_entities(platform, coordinator, state, pk_catalog,
                                registry._known[platform])
        if new:
            async_add_entities(new)
        _async_request_tsl()

    @callback
    def _async_request_tsl() -> None:
        """Fetch each pet product's TSL once (gates manual feed / writes)."""
        requested: set[str] = state.setdefault("pet_tsl_requested", set())
        for dev in coordinator.devices:
            pk, iot_id = dev.get("productKey", ""), dev.get("iotId", "")
            if not pk or not iot_id or pk in requested or pk in coordinator.tsl_models:
                continue
            if not (is_pet_fountain_device(dev, pk_catalog)
                    or is_pet_feeder_device(dev, pk_catalog)):
                continue
            requested.add(pk)
            hass.async_create_task(_async_fetch_tsl(pk, iot_id))

    async def _async_fetch_tsl(pk: str, iot_id: str) -> None:
        tsl = await coordinator.async_get_tsl(pk, iot_id)
        if not tsl:
            # allow another attempt on a later poll
            state.get("pet_tsl_requested", set()).discard(pk)
            return
        new = make_pet_entities(platform, coordinator, state, pk_catalog,
                                registry._known[platform])
        if new:
            async_add_entities(new)

    _async_add()
    entry.async_on_unload(hass.bus.async_listen(EVENT_DEVICES_CHANGED, _async_add))
    entry.async_on_unload(coordinator.async_add_listener(_async_add))
