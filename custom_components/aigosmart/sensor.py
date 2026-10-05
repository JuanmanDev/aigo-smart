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

from .const import DOMAIN, AQUARIUM_ERROR_CODES, AQUARIUM_WATER_PUMP_STATUS, AQUARIUM_FEED_STATE
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry, is_aquarium_device

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
    registry.register("sensor")
    registry.register("aquarium")

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            props = coordinator.props.get(iot_id, {})

            # Standard sensors
            for prop in SENSITIVE_PROPS:
                if prop in props and iot_id not in registry._known["sensor"]:
                    out.append(AigoSmartSensor(coordinator, dev, prop))
                    registry._known["sensor"].add(iot_id)
            
            # Aquarium sensors
            if is_aquarium_device(dev):
                for prop in AQUARIUM_SENSITIVE_PROPS:
                    if prop in props and f"{iot_id}_{prop}" not in registry._known["aquarium"]:
                        out.append(AigoSmartAquariumSensor(coordinator, dev, prop))
                        registry._known["aquarium"].add(f"{iot_id}_{prop}")
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


class AigoSmartAquariumSensor(AigoSmartSensor):
    """Aquarium sensor entity."""

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 prop: str) -> None:
        super().__init__(coordinator, dev, prop)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo Aquarium",
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "smart aquarium",
        )

    @property
    def native_value(self):
        return self._state

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        val = props.get(self._prop)
        if val is not None:
            # Handle enum state translations
            if self._prop == "waterPumpStatus":
                self._state = AQUARIUM_WATER_PUMP_STATUS.get(int(val), str(val))
            elif self._prop == "feedState":
                self._state = AQUARIUM_FEED_STATE.get(int(val), str(val))
            elif self._prop == "errorCode":
                self._state = AQUARIUM_ERROR_CODES.get(int(val), str(val))
            elif self._prop == "feedCnt":
                try:
                    self._state = int(val)
                except (TypeError, ValueError):
                    self._state = val
            else:
                super()._handle_coordinator_update()
        self.async_write_ha_state()
