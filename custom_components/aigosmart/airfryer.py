"""Air fryer entities (Aigostar "Airfryer" category, e.g. productKey a1MqVKrp9JA).

Contributed by @jazzvivi2 (issue #5) and tested on a real a1MqVKrp9JA.

TSL (single-letter identifiers), verified from a real device diagnostic:
  K  switch      enum 0=standby 1=setting 2=running
  M  menu        enum 11=DIY 1=default 2=cake 3=fries 4=shrimp 5=chicken leg
                      6=steak 7=pork chop 8=fish 9=keep warm 10=preheat
  W  temperature int 80-200 degC, step 10
  T  cook time   int 1-60 min
  F  flip timer  int 0-30 min
  Y  delay start int 0-960 min
  E  error code  enum 0=ok 225=NTC open 226=NTC short   (read-only)
  Z  flip alert  bool 0=ok 1=flip food                  (read-only)
  G  lid alert   bool 0=ok 1=lid open
  LocalTimer     array (ignored)

Other products in the Airfryer category (CKZ-1913W, 嘉乐...) may use a
different TSL, so entities are only created when the K/M/W/T signature above
is present in the device's properties.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commander import get_commander
from .const import DOMAIN
from .coordinator import AigoDataUpdateCoordinator
from .helpers import is_airfryer_device

_LOGGER = logging.getLogger(__name__)

PROP_POWER = "K"
PROP_MENU = "M"
PROP_TEMP = "W"
PROP_TIME = "T"
PROP_FLIP_TIMER = "F"
PROP_DELAY = "Y"
PROP_ERROR = "E"
PROP_FLIP_ALERT = "Z"
PROP_LID = "G"

# Properties that identify this TSL (other air fryers may differ)
AIRFRYER_SIGNATURE = (PROP_POWER, PROP_MENU, PROP_TEMP, PROP_TIME)

STATE_STANDBY = 0
STATE_SETTING = 1
STATE_RUNNING = 2

AF_STATES = {0: "standby", 1: "setting", 2: "running"}
AF_ERRORS = {0: "ok", 225: "ntc_open", 226: "ntc_short"}
AF_PROGRAMS = {
    11: "DIY", 1: "Default", 2: "Cake", 3: "Fries", 4: "Shrimp",
    5: "Chicken leg", 6: "Steak", 7: "Pork chop", 8: "Fish",
    9: "Keep warm", 10: "Preheat",
}
AF_PROGRAM_TO_INT = {v: k for k, v in AF_PROGRAMS.items()}


class AirfryerEntity(CoordinatorEntity):
    """Common base: device info, availability, optimistic writes."""

    _attr_has_entity_name = True
    _prop: str = ""

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = f"{self._iot_id}_af_{self._prop}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo air fryer",
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "air fryer",
        )
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

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
        items = {self._prop: int(value)}
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
            _LOGGER.warning("AigoSmart air fryer write %s failed for %s: %s",
                            items, self._iot_id, exc)
            self._coordinator.clear_optimistic_props(self._iot_id)
            await self._coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()


# ---------------------------------------------------------------- switch ----

class AirfryerPower(AirfryerEntity, SwitchEntity):
    """K: on = running (2), off = standby (0). 'setting' (1) counts as off."""

    _prop = PROP_POWER
    _attr_name = "Cooking"
    _attr_icon = "mdi:air-fryer"
    _is_on = False

    @property
    def is_on(self) -> bool:
        return self._is_on

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            try:
                self._is_on = int(props[self._prop]) == STATE_RUNNING
            except (TypeError, ValueError):
                pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(STATE_RUNNING)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(STATE_STANDBY)


# ---------------------------------------------------------------- number ----

class AirfryerNumber(AirfryerEntity, NumberEntity):
    """Generic integer property with fixed range."""

    _attr_mode = NumberMode.BOX
    _value: float | None = None

    @property
    def native_value(self) -> float | None:
        return self._value

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            try:
                self._value = float(props[self._prop])
            except (TypeError, ValueError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(int(value))


class AirfryerTemperature(AirfryerNumber):
    _prop = PROP_TEMP
    _attr_name = "Temperature"
    _attr_icon = "mdi:thermometer"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 80
    _attr_native_max_value = 200
    _attr_native_step = 10
    _attr_mode = NumberMode.SLIDER


class AirfryerCookTime(AirfryerNumber):
    _prop = PROP_TIME
    _attr_name = "Cook time"
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER


class AirfryerFlipTimer(AirfryerNumber):
    _prop = PROP_FLIP_TIMER
    _attr_name = "Flip reminder"
    _attr_icon = "mdi:rotate-3d-variant"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1


class AirfryerDelay(AirfryerNumber):
    _prop = PROP_DELAY
    _attr_name = "Delay start"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_max_value = 960
    _attr_native_step = 1


# ---------------------------------------------------------------- select ----

class AirfryerProgram(AirfryerEntity, SelectEntity):
    _prop = PROP_MENU
    _attr_name = "Program"
    _attr_icon = "mdi:chef-hat"
    _attr_options = list(AF_PROGRAMS.values())
    _current: str | None = None

    @property
    def current_option(self) -> str | None:
        return self._current

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            try:
                self._current = AF_PROGRAMS.get(int(props[self._prop]))
            except (TypeError, ValueError):
                pass

    async def async_select_option(self, option: str) -> None:
        if option in AF_PROGRAM_TO_INT:
            await self._async_write(AF_PROGRAM_TO_INT[option])


# ---------------------------------------------------------------- sensor ----

class AirfryerSensor(AirfryerEntity, SensorEntity):
    """Read-only text sensor mapped from an enum/bool property."""

    _mapping: dict = {}
    _state: str | None = None

    @property
    def native_value(self) -> str | None:
        return self._state

    def _apply(self, props: dict) -> None:
        if self._prop in props:
            try:
                raw = int(props[self._prop])
            except (TypeError, ValueError):
                self._state = str(props[self._prop])
                return
            self._state = self._mapping.get(raw, str(raw))


class AirfryerStatus(AirfryerSensor):
    _prop = PROP_POWER
    _attr_name = "Status"
    _attr_icon = "mdi:air-fryer"
    _mapping = AF_STATES


class AirfryerError(AirfryerSensor):
    _prop = PROP_ERROR
    _attr_name = "Error"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _mapping = AF_ERRORS


class AirfryerFlipAlert(AirfryerSensor):
    _prop = PROP_FLIP_ALERT
    _attr_name = "Flip food"
    _attr_icon = "mdi:rotate-3d-variant"
    _mapping = {0: "ok", 1: "flip"}


class AirfryerLid(AirfryerSensor):
    _prop = PROP_LID
    _attr_name = "Lid"
    _attr_icon = "mdi:door-open"
    _mapping = {0: "closed", 1: "open"}


# --------------------------------------------------------------- factory ----

ENTITIES: dict[str, tuple[type[AirfryerEntity], ...]] = {
    "switch": (AirfryerPower,),
    "number": (AirfryerTemperature, AirfryerCookTime, AirfryerFlipTimer,
               AirfryerDelay),
    "select": (AirfryerProgram,),
    "sensor": (AirfryerStatus, AirfryerError, AirfryerFlipAlert, AirfryerLid),
}


def make_airfryer_entities(platform: str, coordinator: AigoDataUpdateCoordinator,
                           state: dict, pk_catalog: dict, known: set) -> list:
    """Build not-yet-created air fryer entities for one HA platform."""
    out: list = []
    for dev in coordinator.devices:
        iot_id = dev.get("iotId", "")
        if not iot_id or not is_airfryer_device(dev, pk_catalog):
            continue
        props = coordinator.props.get(iot_id, {})
        if not all(p in props for p in AIRFRYER_SIGNATURE):
            # Not polled yet, or an air fryer with a different TSL
            continue
        for cls in ENTITIES.get(platform, ()):
            key = f"{iot_id}_af_{cls._prop}_{platform}"
            if cls._prop in props and key not in known:
                known.add(key)
                out.append(cls(coordinator, dev, state))
    return out
