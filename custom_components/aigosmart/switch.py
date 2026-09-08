"""Switch platform: smart plugs/sockets + fan buzzer sub-entities."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commander import get_commander
from .const import DOMAIN, PROP_FAN_BUZZER, STATUS_ONLINE
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry, platform_for_device
from .helpers import is_fan_device

_LOGGER = logging.getLogger(__name__)

PROP_SWITCH = "PowerSwitch"
PROP_SWITCH_ALT = "Switch"
PROP_LIGHT_SWITCH = "LightSwitch"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry: DeviceRegistry = state["registry"]
    pk_catalog: dict = state["pk_catalog"]
    registry.register("switch")

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            if not iot_id or iot_id in registry._known["switch"]:
                continue
            # fan buzzer sub-entity (fans get their own switch for the beep)
            if is_fan_device(dev):
                if PROP_FAN_BUZZER in coordinator.props.get(iot_id, {}):
                    registry._known["switch"].add(iot_id)
                    out.append(AigoFanBuzzer(coordinator, dev, state))
                continue
            if platform_for_device(dev, pk_catalog) == "switch":
                registry._known["switch"].add(iot_id)
                out.append(AigoSmartSwitch(coordinator, dev, state))
        return out

    @callback
    def _async_handle_new(*_args) -> None:
        new = _make_entities()
        if new:
            async_add_entities(new)

    _async_handle_new()
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_DEVICES_CHANGED, _async_handle_new))


class AigoSmartSwitch(CoordinatorEntity, SwitchEntity):
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
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo plug",
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "smart plug",
        )
        self._is_on = False
        # Alibaba IoT status: 0=NOT_ACTIVATED, 1=ONLINE, 3=OFFLINE, 8=DISABLED
        self._available = dev.get("status") == 1
        self._prop = PROP_SWITCH
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

    @property
    def available(self) -> bool:
        return self._available

    @property
    def is_on(self) -> bool:
        return self._is_on

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        for d in self._coordinator.devices:
            if d.get("iotId") == self._iot_id:
                self._available = d.get("status") == 1
                break
        if props and self._available and not (
            self._commander and self._commander.in_skip()
        ):
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        if PROP_SWITCH in props:
            self._prop = PROP_SWITCH
            self._is_on = bool(props[PROP_SWITCH])
        elif PROP_SWITCH_ALT in props:
            self._prop = PROP_SWITCH_ALT
            self._is_on = bool(props[PROP_SWITCH_ALT])
        elif PROP_LIGHT_SWITCH in props:
            self._prop = PROP_LIGHT_SWITCH
            self._is_on = bool(props[PROP_LIGHT_SWITCH])

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._is_on = True
        self.async_write_ha_state()
        if self._commander is not None:
            await self._commander.async_send({self._prop: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        self.async_write_ha_state()
        if self._commander is not None:
            await self._commander.async_send({self._prop: 0})

    async def async_will_remove_from_hass(self) -> None:
        """Flush any pending local-first writes before removal."""
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()


class AigoFanBuzzer(CoordinatorEntity, SwitchEntity):
    """Key beep on/off for a fan (buzzerSwitch)."""

    _attr_name = "Key beep"
    _attr_icon = "mdi:volume-high"

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = f"{self._iot_id}_buzzer"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo fan",
            manufacturer="Aigostar",
            model=dev.get("productName") or "",
        )
        self._is_on = False
        self._attr_available = dev.get("status") == STATUS_ONLINE
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(coordinator.props.get(self._iot_id, {}))

    @property
    def is_on(self) -> bool:
        return self._is_on

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        if PROP_FAN_BUZZER in props:
            self._is_on = bool(int(props[PROP_FAN_BUZZER]))
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        if PROP_FAN_BUZZER in props:
            try:
                self._is_on = bool(int(props[PROP_FAN_BUZZER]))
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._is_on = True
        self.async_write_ha_state()
        if self._commander is not None:
            await self._commander.async_send({PROP_FAN_BUZZER: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        self.async_write_ha_state()
        if self._commander is not None:
            await self._commander.async_send({PROP_FAN_BUZZER: 0})

    async def async_will_remove_from_hass(self) -> None:
        """Flush any pending local-first writes before removal."""
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()
