"""Water Heater platform for Aigostar — smart kettle support.

TSL properties (from hass-aigosmart):
  HeatingSwitch        bool  0/1
  temperature          int   current temperature (°C)
  Target_temperature   int   target temperature (°C)
  heatpreservation     bool  keep warm
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.water_heater import (
    STATE_ELECTRIC,
    STATE_OFF,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commander import get_commander
from .const import (
    DOMAIN,
    PROP_KETTLE_KEEP_WARM,
    PROP_KETTLE_SWITCH,
    PROP_KETTLE_TARGET,
    PROP_KETTLE_TEMP,
)
from .coordinator import AigoDataUpdateCoordinator
from .helpers import is_kettle_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]

    entities = []
    for dev in coordinator.devices:
        if is_kettle_device(dev):
            entities.append(AigoSmartKettle(coordinator, dev, state))
    async_add_entities(entities)


class AigoSmartKettle(CoordinatorEntity, WaterHeaterEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.ON_OFF
    )
    _attr_min_temp = 40
    _attr_max_temp = 100
    _attr_operation_list = [STATE_OFF, STATE_ELECTRIC]

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = self._iot_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo kettle",
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "kettle",
        )
        self._current = None
        self._target = 100
        self._on = False
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

    @property
    def available(self) -> bool:
        # Per-device offline state AND coordinator health (expired session)
        return self._available and self.coordinator.last_update_success

    @property
    def current_temperature(self) -> float | None:
        return self._current

    @property
    def target_temperature(self) -> float | None:
        return self._target

    @property
    def state(self) -> str:
        return STATE_ELECTRIC if self._on else STATE_OFF

    @property
    def current_operation(self) -> str:
        return STATE_ELECTRIC if self._on else STATE_OFF

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        self._available = self._coordinator.is_device_online(self._iot_id)
        if props and not (self._commander and self._commander.in_skip()):
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        if PROP_KETTLE_TEMP in props:
            try:
                self._current = float(props[PROP_KETTLE_TEMP])
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
        if PROP_KETTLE_TARGET in props:
            try:
                self._target = float(props[PROP_KETTLE_TARGET])
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
        if PROP_KETTLE_SWITCH in props:
            self._on = bool(int(props[PROP_KETTLE_SWITCH]))

    async def _send(self, items: dict, prev: bool) -> None:
        if self._commander is not None:
            await self._commander.async_send(items)
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties,
                    self._iot_id, items,
                )
            except Exception as exc:
                _LOGGER.warning("AigoSmart kettle command failed for %s: %s",
                                self._iot_id, exc)
                self._on = prev
                self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if ATTR_TEMPERATURE in kwargs:
            self._target = float(kwargs[ATTR_TEMPERATURE])
            self.async_write_ha_state()
            if self._commander is not None:
                await self._commander.async_send({PROP_KETTLE_TARGET: int(self._target)})

    async def async_turn_on(self, **kwargs: Any) -> None:
        prev = self._on
        self._on = True
        self.async_write_ha_state()
        await self._send({PROP_KETTLE_SWITCH: 1}, prev)

    async def async_turn_off(self, **kwargs: Any) -> None:
        prev = self._on
        self._on = False
        self.async_write_ha_state()
        await self._send({PROP_KETTLE_SWITCH: 0}, prev)

    async def async_will_remove_from_hass(self) -> None:
        """Flush any pending local-first writes before removal."""
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()
