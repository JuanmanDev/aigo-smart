"""Select platform for AigoSmart devices (aquarium pump speed, lighting scenes)."""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commander import get_commander
from .const import (
    AQUARIUM_PUMP_LEVEL_TO_INT,
    AQUARIUM_PUMP_LEVELS,
    AQUARIUM_PUMP_OPTIONS,
    DOMAIN,
    PROP_AQUARIUM_LIGHT_SCENE_ID,
    PROP_AQUARIUM_PUMP_LEVEL,
)
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry
from .helpers import is_aquarium_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry: DeviceRegistry = state["registry"]
    pk_catalog: dict = state.get("pk_catalog", {})
    registry.register("select")

    known_selects = registry._known["select"]

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            if not iot_id:
                continue

            if is_aquarium_device(dev, pk_catalog):
                props = coordinator.props.get(iot_id, {})
                pump_key = f"{iot_id}_pump_speed"
                if PROP_AQUARIUM_PUMP_LEVEL in props and pump_key not in known_selects:
                    known_selects.add(pump_key)
                    out.append(AigoSmartAquariumPumpSelect(coordinator, dev, state))

                scene_key = f"{iot_id}_light_scene"
                if PROP_AQUARIUM_LIGHT_SCENE_ID in props and scene_key not in known_selects:
                    known_selects.add(scene_key)
                    out.append(AigoSmartAquariumSceneSelect(coordinator, dev, state))
        return out

    @callback
    def _async_handle_new(*_args) -> None:
        new = _make_entities()
        if new:
            async_add_entities(new)

    _async_handle_new()
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_DEVICES_CHANGED, _async_handle_new)
    )


class AigoSmartAquariumPumpSelect(CoordinatorEntity, SelectEntity):
    """Water pump speed select entity for the aquarium."""

    _attr_has_entity_name = True
    _attr_name = "Water Pump Speed"
    _attr_icon = "mdi:pump"
    _attr_options = AQUARIUM_PUMP_OPTIONS

    def __init__(
        self,
        coordinator: AigoDataUpdateCoordinator,
        dev: dict,
        state: dict | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._dev = dev
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = f"{self._iot_id}_pump_speed"

        name = dev.get("nickName") or dev.get("deviceName") or "Aigo Aquarium"
        fw = dev.get("firmwareVersion") or dev.get("moduleVersion") or None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=name,
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "smart aquarium",
            sw_version=fw,
            configuration_url="https://www.aigostar.com",
        )
        self._current_option = AQUARIUM_PUMP_OPTIONS[0]
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

    @property
    def available(self) -> bool:
        return self._available and self.coordinator.last_update_success

    @property
    def options(self) -> list[str]:
        return self._attr_options

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        self._available = self._coordinator.is_device_online(self._iot_id)
        if props:
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        if PROP_AQUARIUM_PUMP_LEVEL in props:
            try:
                level = int(props[PROP_AQUARIUM_PUMP_LEVEL])
                if level in AQUARIUM_PUMP_LEVELS:
                    self._current_option = AQUARIUM_PUMP_LEVELS[level]
            except (TypeError, ValueError):
                pass

    async def async_select_option(self, option: str) -> None:
        if option not in AQUARIUM_PUMP_LEVEL_TO_INT:
            _LOGGER.warning("Invalid pump speed option: %s", option)
            return

        level_val = AQUARIUM_PUMP_LEVEL_TO_INT[option]
        prev = self._current_option
        self._current_option = option
        self.async_write_ha_state()

        _LOGGER.debug(
            "Setting aquarium pump speed for %s: %s (%s)",
            self._iot_id, option, level_val,
        )

        items = {PROP_AQUARIUM_PUMP_LEVEL: level_val}
        self._coordinator.async_set_optimistic_props(self._iot_id, items, hold_duration=5.0)

        if self._commander is not None:
            await self._commander.async_send(items)
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties,
                    self._iot_id,
                    items,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "AigoSmart aquarium pump speed failed for %s: %s",
                    self._iot_id, exc,
                )
                self._current_option = prev
                self._coordinator.clear_optimistic_props(self._iot_id)
                self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()


class AigoSmartAquariumSceneSelect(CoordinatorEntity, SelectEntity):
    """Lighting scene select entity for the aquarium."""

    _attr_has_entity_name = True
    _attr_name = "Light Scene"
    _attr_icon = "mdi:palette-swatch"
    _attr_options = [f"Scene {i}" for i in range(1, 13)]

    def __init__(
        self,
        coordinator: AigoDataUpdateCoordinator,
        dev: dict,
        state: dict | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._dev = dev
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = f"{self._iot_id}_light_scene"

        name = dev.get("nickName") or dev.get("deviceName") or "Aigo Aquarium"
        fw = dev.get("firmwareVersion") or dev.get("moduleVersion") or None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=name,
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "smart aquarium",
            sw_version=fw,
            configuration_url="https://www.aigostar.com",
        )
        self._current_option = self._attr_options[0]
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

    @property
    def available(self) -> bool:
        return self._available and self.coordinator.last_update_success

    @property
    def options(self) -> list[str]:
        return self._attr_options

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        self._available = self._coordinator.is_device_online(self._iot_id)
        if props:
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        if PROP_AQUARIUM_LIGHT_SCENE_ID in props:
            try:
                sid = int(props[PROP_AQUARIUM_LIGHT_SCENE_ID])
                if 1 <= sid <= 12:
                    self._current_option = f"Scene {sid}"
            except (TypeError, ValueError):
                pass

    async def async_select_option(self, option: str) -> None:
        digits = re.findall(r"\d+", option)
        if not digits:
            _LOGGER.warning("Invalid light scene option: %s", option)
            return

        sid = int(digits[0])
        if not (1 <= sid <= 12):
            return

        prev = self._current_option
        self._current_option = option
        self.async_write_ha_state()

        _LOGGER.debug(
            "Setting aquarium light scene for %s: %s (ID %s)",
            self._iot_id, option, sid,
        )

        items = {PROP_AQUARIUM_LIGHT_SCENE_ID: sid}
        self._coordinator.async_set_optimistic_props(self._iot_id, items, hold_duration=5.0)

        if self._commander is not None:
            await self._commander.async_send(items)
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties,
                    self._iot_id,
                    items,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "AigoSmart aquarium light scene failed for %s: %s",
                    self._iot_id, exc,
                )
                self._current_option = prev
                self._coordinator.clear_optimistic_props(self._iot_id)
                self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()
