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

from .const import AQUARIUM_ERROR_CODES, AQUARIUM_FEED_STATE, AQUARIUM_WATER_PUMP_STATUS, DOMAIN
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry
from .helpers import is_aquarium_device

_LOGGER = logging.getLogger(__name__)

SENSITIVE_PROPS = {
    "CuTemperature": (SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
    "Fahrenheit_degree": (SensorDeviceClass.TEMPERATURE, "°F", SensorStateClass.MEASUREMENT),
    "humidity": (SensorDeviceClass.HUMIDITY, "%", SensorStateClass.MEASUREMENT),
}

# Aquarium sensor properties
AQUARIUM_SENSITIVE_PROPS = {
    "currentTemperature": (SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
    "waterPumpStatus": None,  # special enum handling
    "feedState": None,  # special enum handling
    "feedCnt": (None, None, SensorStateClass.TOTAL_INCREASING),
    "errorCode": None,  # special enum handling
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry: DeviceRegistry = state["registry"]
    pk_catalog: dict = state.get("pk_catalog", {})
    registry.register("sensor")

    known_sensors = registry._known["sensor"]

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            if not iot_id:
                continue
            props = coordinator.props.get(iot_id, {})

            # Standard sensors
            for prop in SENSITIVE_PROPS:
                key = f"{iot_id}_{prop}"
                if prop in props and key not in known_sensors:
                    known_sensors.add(key)
                    out.append(AigoSmartSensor(coordinator, dev, prop))

            # Aquarium sensors
            if is_aquarium_device(dev, pk_catalog):
                for prop in AQUARIUM_SENSITIVE_PROPS:
                    key = f"{iot_id}_{prop}"
                    if prop in props and key not in known_sensors:
                        known_sensors.add(key)
                        out.append(AigoSmartAquariumSensor(coordinator, dev, prop))
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


class AigoSmartAquariumSensor(CoordinatorEntity, SensorEntity):
    """Aquarium sensor entity."""

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 prop: str) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._dev = dev
        self._iot_id = dev.get("iotId", "")
        self._prop = prop
        self._attr_unique_id = f"{self._iot_id}_{prop}"

        info = AQUARIUM_SENSITIVE_PROPS.get(prop)
        if info:
            dev_class, unit, sclass = info
            self._attr_device_class = dev_class
            self._attr_native_unit_of_measurement = unit
            self._attr_state_class = sclass
        else:
            self._attr_device_class = None
            self._attr_native_unit_of_measurement = None
            self._attr_state_class = None

        names = {
            "currentTemperature": ("Water Temperature", "mdi:thermometer"),
            "waterPumpStatus": ("Water Pump Status", "mdi:pump"),
            "feedState": ("Feed State", "mdi:fish-food"),
            "feedCnt": ("Feed Count", "mdi:counter"),
            "errorCode": ("Error Status", "mdi:alert-circle"),
        }
        name, icon = names.get(prop, (prop, "mdi:eye"))
        self._attr_name = name
        self._attr_icon = icon

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo Aquarium",
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "smart aquarium",
        )
        self._state = None
        self._update_state_from_props(coordinator.props.get(self._iot_id, {}))

    @property
    def native_value(self):
        return self._state

    def _update_state_from_props(self, props: dict) -> None:
        val = props.get(self._prop)
        if val is None:
            return
        if self._prop == "waterPumpStatus":
            try:
                self._state = AQUARIUM_WATER_PUMP_STATUS.get(int(val), str(val))
            except (TypeError, ValueError):
                self._state = str(val)
        elif self._prop == "feedState":
            try:
                self._state = AQUARIUM_FEED_STATE.get(int(val), str(val))
            except (TypeError, ValueError):
                self._state = str(val)
        elif self._prop == "errorCode":
            try:
                self._state = AQUARIUM_ERROR_CODES.get(int(val), str(val))
            except (TypeError, ValueError):
                self._state = str(val)
        elif self._prop == "feedCnt":
            try:
                self._state = int(val)
            except (TypeError, ValueError):
                self._state = val
        elif self._prop == "currentTemperature":
            try:
                num = float(val)
                self._state = int(num) if num == int(num) else num
            except (TypeError, ValueError):
                self._state = val
        else:
            self._state = val

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        self._update_state_from_props(props)
        self.async_write_ha_state()
