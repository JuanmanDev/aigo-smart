"""Fan platform for AigoSmart fans — full feature set.

TSL properties (verified on Aigostar tower fan a1mZFNZz7pq by hass-aigosmart):
  powerstate            bool   0/1
  windspeed             enum   1|2|3
  mode                  enum   0=normal 1=natural 2=sleep
  angleAutoLROnOff      bool   left/right auto swing (oscillate)
  appointmentClosingTime int  0-24 h auto-off (number platform)
  buzzerSwitch          bool   key beep (switch platform)

Fan-lights (categoryKey "fanLight", e.g. a180PpkuyP6 / a1aeYLqeLsb) expose a
two-channel TSL instead — the fan half is handled here, the lamp half by
light.py:
  powerstate_2          bool   fan on/off   (powerstate_1 is the lamp)
  windspeed             enum   1-6, 255 = "gear changing" (transient)
  mode_2                enum   0=normal 1=natural 2=sleep 3=fresh
  direction             bool   0=forward 1=reverse

Note: CuTemperature is NOT exposed — the probe sits next to the motor and
reads warm air, not room temperature.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    FanEntity,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .commander import get_commander
from .const import (
    DOMAIN,
    FAN_PRESET_MODES,
    FAN_SPEED_TRANSITION,
    FANLIGHT_PRESET_MODES,
    FANLIGHT_SPEED_RANGE,
    PROP_FAN_DIRECTION,
    PROP_FAN_MODE,
    PROP_FAN_OSCILLATE,
    PROP_FAN_POWER,
    PROP_FAN_SPEED,
    PROP_FANLIGHT_FAN_MODE,
    PROP_FANLIGHT_FAN_SWITCH,
)
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry, platform_for_device
from .helpers import is_fan_device, is_fan_light_device

_LOGGER = logging.getLogger(__name__)

# windspeed enum range 1-3 (tower fan)
SPEED_RANGE = (1, 3)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry: DeviceRegistry = state["registry"]
    pk_catalog: dict = state["pk_catalog"]
    registry.register("fan")

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            is_fan = is_fan_device(dev) or platform_for_device(dev, pk_catalog) == "fan"
            if iot_id and iot_id not in registry._known["fan"] and is_fan:
                out.append(AigoSmartFan(coordinator, dev, state))
        return out

    @callback
    def _async_handle_new(*_args) -> None:
        new = _make_entities()
        if new:
            for e in new:
                registry._known["fan"].add(e._iot_id)
            async_add_entities(new)

    _async_handle_new()
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_DEVICES_CHANGED, _async_handle_new))


class AigoSmartFan(CoordinatorEntity, FanEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._dev = dev
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = self._iot_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo fan",
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "fan",
        )

        pk_catalog = (state or {}).get("pk_catalog")
        self._fan_light = is_fan_light_device(dev, pk_catalog)
        if self._fan_light:
            # Fan half of a fan-light: the lamp is a separate light entity on
            # the same device, so name this one explicitly.
            self._attr_name = "Fan"
            self._prop_power = PROP_FANLIGHT_FAN_SWITCH
            self._prop_mode = PROP_FANLIGHT_FAN_MODE
            self._speed_range = FANLIGHT_SPEED_RANGE
            self._presets = FANLIGHT_PRESET_MODES
            self._attr_supported_features = (
                FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE
                | FanEntityFeature.DIRECTION
            )
        else:
            self._prop_power = PROP_FAN_POWER
            self._prop_mode = PROP_FAN_MODE
            self._speed_range = SPEED_RANGE
            self._presets = FAN_PRESET_MODES
            self._attr_supported_features = (
                FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE
                | FanEntityFeature.OSCILLATE
            )
        # Since HA 2025.2 fan.turn_on/turn_off are only routed to entities
        # that declare TURN_ON/TURN_OFF — without them every on/off call is
        # rejected before it reaches this integration.
        self._attr_supported_features |= (
            FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF)
        self._attr_speed_count = self._speed_range[1] - self._speed_range[0] + 1
        self._attr_preset_modes = list(self._presets.values())
        self._preset_to_value = {v: k for k, v in self._presets.items()}

        self._on = False
        self._speed = self._speed_range[0]
        self._mode = 0
        self._oscillating = False
        self._direction = DIRECTION_FORWARD
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

    @property
    def available(self) -> bool:
        # Per-device offline state AND coordinator health (expired session)
        return self._available and self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def percentage(self) -> int | None:
        if not self._on:
            return None
        return ranged_value_to_percentage(self._speed_range, self._speed)

    @property
    def preset_mode(self) -> str | None:
        if not self._on:
            return None
        return self._presets.get(self._mode)

    @property
    def oscillating(self) -> bool:
        return self._oscillating

    @property
    def current_direction(self) -> str | None:
        return self._direction if self._fan_light else None

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        self._available = self._coordinator.is_device_online(self._iot_id)
        if props and not (self._commander and self._commander.in_skip()):
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        if self._prop_power in props:
            try:
                self._on = int(props[self._prop_power]) == 1
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
        if PROP_FAN_SPEED in props:
            try:
                speed = int(props[PROP_FAN_SPEED])
            except (TypeError, ValueError):
                speed = None
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
            # 255 = "gear changing" on fan-lights: keep the last real speed
            if speed is not None and speed != FAN_SPEED_TRANSITION:
                low, high = self._speed_range
                self._speed = max(low, min(high, speed))
        if self._prop_mode in props:
            try:
                self._mode = int(props[self._prop_mode])
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
        if PROP_FAN_OSCILLATE in props:
            self._oscillating = bool(int(props[PROP_FAN_OSCILLATE]))
        if PROP_FAN_DIRECTION in props:
            try:
                self._direction = (
                    DIRECTION_REVERSE if int(props[PROP_FAN_DIRECTION])
                    else DIRECTION_FORWARD
                )
            except (TypeError, ValueError):
                pass

    def _speed_for(self, percentage: int) -> int:
        low, high = self._speed_range
        return max(low, min(high, round(
            percentage_to_ranged_value(self._speed_range, percentage))))

    async def _send(self, items: dict) -> None:
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
                _LOGGER.warning("AigoSmart fan command failed for %s: %s",
                                self._iot_id, exc)
                self._coordinator.mark_device_offline(self._iot_id)

    async def async_turn_on(self, percentage: int | None = None,
                            preset_mode: str | None = None,
                            **kwargs: Any) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        items: dict[str, Any] = {self._prop_power: 1}
        if percentage is not None:
            self._speed = self._speed_for(percentage)
            items[PROP_FAN_SPEED] = self._speed
        if preset_mode in self._preset_to_value:
            self._mode = self._preset_to_value[preset_mode]
            items[self._prop_mode] = self._mode
        self._on = True
        await self._send(items)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._on = False
        await self._send({self._prop_power: 0})

    async def async_set_percentage(self, percentage: int) -> None:
        # HA sends 0 for "off"; writing windspeed 0 is rejected by the cloud
        # (6309 "tsl parse: enum specs error -> windspeed").
        if percentage == 0:
            await self.async_turn_off()
            return
        self._speed = self._speed_for(percentage)
        self._on = True
        await self._send({self._prop_power: 1, PROP_FAN_SPEED: self._speed})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode in self._preset_to_value:
            self._mode = self._preset_to_value[preset_mode]
            self._on = True
            await self._send({self._prop_power: 1, self._prop_mode: self._mode})

    async def async_oscillate(self, oscillating: bool) -> None:
        self._oscillating = oscillating
        await self._send({PROP_FAN_OSCILLATE: int(oscillating)})

    async def async_set_direction(self, direction: str) -> None:
        self._direction = direction
        await self._send(
            {PROP_FAN_DIRECTION: int(direction == DIRECTION_REVERSE)})

    async def async_will_remove_from_hass(self) -> None:
        """Flush any pending local-first writes before removal."""
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()
