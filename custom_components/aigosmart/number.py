"""Number platform for AigoSmart fans — auto-off timer (0-24 h)."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commander import get_commander
from .const import DOMAIN, PROP_FAN_TIMER, STATUS_ONLINE
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .helpers import is_fan_device

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
        if is_fan_device(dev):
            entities.append(AigoSmartFanTimer(coordinator, dev, state))
    async_add_entities(entities)


class AigoSmartFanTimer(CoordinatorEntity, NumberEntity):
    """Auto-off timer for a fan (appointmentClosingTime, 0-24 h, 0 = off)."""

    _attr_name = "Timer"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 24
    _attr_native_step = 1
    _attr_icon = "mdi:timer"

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = f"{self._iot_id}_timer"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=dev.get("nickName") or dev.get("deviceName") or "Aigo fan",
            manufacturer="Aigostar",
            model=dev.get("productName") or "",
        )
        self._value = 0.0
        self._commander = get_commander(state, self._iot_id) if state else None

    @property
    def native_value(self) -> float | None:
        return self._value

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        val = props.get(PROP_FAN_TIMER)
        if val is not None:
            try:
                self._value = float(val)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        self.async_write_ha_state()
        items = {PROP_FAN_TIMER: int(value)}
        if self._commander is not None:
            await self._commander.async_send(items)
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties,
                    self._iot_id, items,
                )
            except Exception as exc:
                _LOGGER.warning("AigoSmart timer set failed for %s: %s",
                                self._iot_id, exc)

    async def async_will_remove_from_hass(self) -> None:
        """Flush any pending local-first writes before removal."""
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()
