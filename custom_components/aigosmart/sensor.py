"""Sensor platform: temperature/humidity/power metrics from AigoSmart devices."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry

_LOGGER = logging.getLogger(__name__)

SENSITIVE_PROPS = {
    "CuTemperature": (SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
    "Fahrenheit_degree": (SensorDeviceClass.TEMPERATURE, "°F", SensorStateClass.MEASUREMENT),
    "humidity": (SensorDeviceClass.HUMIDITY, "%", SensorStateClass.MEASUREMENT),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry: DeviceRegistry = state["registry"]
    registry.register("sensor")

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            props = coordinator.props.get(dev.get("iotId", ""), {})
            for prop in SENSITIVE_PROPS:
                if prop in props:  # only create sensors the device actually reports
                    out.append(AigoSmartSensor(coordinator, dev, prop))
        return out

    @callback
    def _async_handle_new(*_args) -> None:
        new = _make_entities()
        if new:
            async_add_entities(new)

    _async_handle_new()
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_DEVICES_CHANGED, _async_handle_new))


class AigoSmartSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict, prop: str) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._iot_id = dev.get("iotId", "")
        self._prop = prop
        self._attr_unique_id = f"{self._iot_id}_{prop}"
        dev_class, unit, sclass = SENSITIVE_PROPS[prop]
        self._attr_device_class = dev_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = sclass
        self._attr_name = prop
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo device",
            manufacturer="Aigostar",
            model=dev.get("productName") or "",
        )
        self._state = None

    @property
    def native_value(self):
        return self._state

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        val = props.get(self._prop)
        if val is not None:
            try:
                num = float(val)
                self._state = int(num) if num == int(num) else num
            except (TypeError, ValueError):
                self._state = val
        self.async_write_ha_state()
