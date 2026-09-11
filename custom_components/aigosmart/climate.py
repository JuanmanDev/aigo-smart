"""Climate platform for AigoSmart ACs/heaters/dehumidifiers."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commander import get_commander
from .const import DOMAIN
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry, platform_for_device

_LOGGER = logging.getLogger(__name__)

PROP_POWER = "powerstate"
PROP_TARGET_T = "targetTemperature"
PROP_CURRENT_T = "CuTemperature"
PROP_MODE = "mode"

CLIMATE_CATEGORIES = ("airconditioner", "heater", "dehumidifier",
                      "oil-filledradiator", "petthermostat", "catbed")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry: DeviceRegistry = state["registry"]
    pk_catalog: dict = state["pk_catalog"]
    registry.register("climate")

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            if iot_id and iot_id not in registry._known["climate"] \
                    and platform_for_device(dev, pk_catalog) == "climate":
                out.append(AigoSmartClimate(coordinator, dev, state))
        return out

    @callback
    def _async_handle_new(*_args) -> None:
        new = _make_entities()
        if new:
            for e in new:
                registry._known["climate"].add(e._iot_id)
            async_add_entities(new)

    _async_handle_new()
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_DEVICES_CHANGED, _async_handle_new))


class AigoSmartClimate(CoordinatorEntity, ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_min_temp = 16
    _attr_max_temp = 32
    _attr_target_temperature_step = 1
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT,
                        HVACMode.FAN_ONLY, HVACMode.DRY]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._dev = dev
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = self._iot_id
        category = (dev.get("categoryKey") or "").lower()
        self._is_dehumidifier = any(c in category for c in ("dehumidifier",))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo climate",
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "climate device",
        )
        self._hvac_mode = HVACMode.OFF
        self._current_temp = None
        self._target_temp = 24
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

    @property
    def available(self) -> bool:
        # Per-device offline state AND coordinator health (expired session)
        return self._available and self.coordinator.last_update_success

    @property
    def current_temperature(self):
        return self._current_temp

    @property
    def target_temperature(self):
        return self._target_temp

    @property
    def hvac_mode(self):
        return self._hvac_mode

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        self._available = self._coordinator.is_device_online(self._iot_id)
        if props and not (self._commander and self._commander.in_skip()):
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        if PROP_CURRENT_T in props:
            try:
                self._current_temp = float(props[PROP_CURRENT_T])
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
        if PROP_TARGET_T in props:
            try:
                self._target_temp = float(props[PROP_TARGET_T])
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
        if PROP_POWER in props:
            if int(props[PROP_POWER]) == 0:
                self._hvac_mode = HVACMode.OFF
            elif self._is_dehumidifier:
                self._hvac_mode = HVACMode.DRY
            else:
                self._hvac_mode = HVACMode.COOL

    async def async_set_temperature(self, **kwargs: Any) -> None:
        items = {}
        if ATTR_TEMPERATURE in kwargs:
            items[PROP_TARGET_T] = kwargs[ATTR_TEMPERATURE]
        if items:
            self.async_write_ha_state()
            if self._commander is not None:
                await self._commander.async_send(items)
            else:
                try:
                    await self.hass.async_add_executor_job(
                        self._coordinator.client.set_properties,
                        self._iot_id, items,
                    )
                except Exception as exc:
                    _LOGGER.warning("AigoSmart set_temperature failed for %s: %s",
                                    self._iot_id, exc)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        power = 0 if hvac_mode == HVACMode.OFF else 1
        self._hvac_mode = hvac_mode
        self.async_write_ha_state()
        if self._commander is not None:
            await self._commander.async_send({PROP_POWER: power})
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties,
                    self._iot_id, {PROP_POWER: power},
                )
            except Exception as exc:
                _LOGGER.warning("AigoSmart set_hvac_mode failed for %s: %s",
                                self._iot_id, exc)
                self._coordinator.mark_device_offline(self._iot_id)

    async def async_will_remove_from_hass(self) -> None:
        """Flush any pending local-first writes before removal."""
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()
