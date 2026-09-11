"""Light platform for AigoSmart devices.

Entities are created from the coordinator's device list; new devices detected
by the autodiscovery layer are added dynamically via the dispatcher signal.

Colour handling uses color_model.py (from hass-aigosmart, andreazllin):
the colour property is recognised by its *shape* (any struct with hue/
saturation or rgb members), with ranges read from the device's own TSL model
when available. Wi-Fi (TG7100C) and BLE Mesh products both work.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .color_model import (
    ColorSpec,
    ModeSpec,
    as_struct,
    color_spec_from_props,
    color_spec_from_tsl,
    known_profile,
    mode_spec_from_tsl,
)
from .commander import get_commander
from .const import (
    DOMAIN,
    KELVIN_COOL,
    KELVIN_WARM,
    PROP_BRIGHTNESS_CANDIDATES,
    PROP_COLOR_TEMP_CANDIDATES,
    PROP_HSV_CANDIDATES,
    PROP_LIGHT_BRIGHTNESS,
    PROP_LIGHT_COLOR_TEMP,
    PROP_LIGHT_MODE,
    PROP_LIGHT_SWITCH,
    PROP_MESH_BRIGHTNESS,
    PROP_MESH_COLOR_TEMP,
    PROP_MESH_LIGHT_MODE,
    PROP_MESH_SWITCH,
)
from .coordinator import AigoDataUpdateCoordinator, EVENT_DEVICES_CHANGED
from .discovery import DeviceRegistry, platform_for_device
from .helpers import is_bt_device, is_fan_device, is_gateway_device, is_kettle_device

_LOGGER = logging.getLogger(__name__)


def _pick(props: dict, candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in props:
            return c
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    state = hass.data[DOMAIN][entry.entry_id]
    coordinator: AigoDataUpdateCoordinator = state["coordinator"]
    registry: DeviceRegistry = state["registry"]
    pk_catalog: dict = state["pk_catalog"]
    client = state["client"]
    registry.register("light")

    def _is_light(dev: dict) -> bool:
        # gateways, fans and kettles must not become lights
        if is_gateway_device(dev) or is_fan_device(dev) or is_kettle_device(dev):
            return False
        return platform_for_device(dev, pk_catalog) == "light"

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            if iot_id and iot_id not in registry._known["light"] and _is_light(dev):
                out.append(AigoSmartLight(coordinator, dev, state))
        return out

    @callback
    def _async_handle_new(*_args) -> None:
        new = _make_entities()
        if new:
            for e in new:
                registry._known["light"].add(e._iot_id)
            async_add_entities(new)

    _async_handle_new()
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_DEVICES_CHANGED, _async_handle_new))


class AigoSmartLight(CoordinatorEntity, LightEntity):
    """AigoSmart light (cloud-polled through the coordinator)."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_min_color_temp_kelvin = KELVIN_WARM
    _attr_max_color_temp_kelvin = KELVIN_COOL

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._dev = dev
        self._iot_id = dev.get("iotId", "")
        self._attr_unique_id = self._iot_id
        name = dev.get("nickName") or dev.get("deviceName") or "Aigo light"
        fw = dev.get("firmwareVersion") or dev.get("moduleVersion") or None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._iot_id)},
            name=name,
            manufacturer="Aigostar",
            model=dev.get("productName") or dev.get("productKey") or "smart light",
            sw_version=fw,
            configuration_url="https://www.aigostar.com",
        )

        self._is_bt = is_bt_device(dev)
        # Transport-specific property identifiers
        self._prop_switch = PROP_MESH_SWITCH if self._is_bt else PROP_LIGHT_SWITCH
        self._prop_brightness = PROP_MESH_BRIGHTNESS if self._is_bt else PROP_LIGHT_BRIGHTNESS
        self._prop_color_temp = PROP_MESH_COLOR_TEMP if self._is_bt else PROP_LIGHT_COLOR_TEMP

        # Colour capability: pinned profile for this product, or resolved from TSL
        product_key = dev.get("productKey", "")
        profile = known_profile(product_key)
        if profile is not None:
            self._color_spec: ColorSpec | None = profile.color
            self._mode_spec: ModeSpec = profile.mode
        else:
            tsl = coordinator.tsl_models.get(product_key) or {}
            if tsl:
                self._color_spec = color_spec_from_tsl(tsl)
                self._mode_spec = mode_spec_from_tsl(tsl, PROP_LIGHT_MODE)
            else:
                # last resort: infer from a live property snapshot
                props = coordinator.props.get(self._iot_id, {})
                self._color_spec = color_spec_from_props(props)
                self._mode_spec = ModeSpec(identifier=PROP_LIGHT_MODE)

        self._is_on = False
        self._brightness = 255
        self._color_temp_k = 3700
        self._hs = None
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(self._coordinator.props.get(self._iot_id, {}))

    # -- state -------------------------------------------------------------

    @property
    def available(self) -> bool:
        # Unavailable when the coordinator itself is failing (e.g. expired
        # session → polls erroring for hours) OR the device is offline.
        return self._available and self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int:
        return self._color_temp_k

    @property
    def hs_color(self):
        return self._hs

    @property
    def color_mode(self) -> ColorMode:
        if self._hs is not None and self._color_spec is not None:
            return ColorMode.HS
        return ColorMode.COLOR_TEMP

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        if self._color_spec is not None:
            return {ColorMode.COLOR_TEMP, ColorMode.HS}
        return {ColorMode.COLOR_TEMP}

    @callback
    def _handle_coordinator_update(self) -> None:
        props = self._coordinator.props.get(self._iot_id, {})
        # Online state is resolved by the coordinator (verified via
        # /thing/status/get when the list status is ambiguous) — not gated
        # here anymore, since the listBinding status is unreliable.
        self._available = self._coordinator.is_device_online(self._iot_id)
        # While a command is settling (or its shadow hasn't caught up), the
        # cloud can still return the OLD values — applying them here is what
        # made brightness/CCT "revert" during rapid changes. Skip instead.
        if props and not (
            self._commander and self._commander.in_skip()
        ):
            self._apply(props)
        self.async_write_ha_state()

    def _apply(self, props: dict) -> None:
        # switch (transport-specific first)
        if self._prop_switch in props:
            self._is_on = bool(props[self._prop_switch])
        else:
            alt = _pick(props, (PROP_LIGHT_SWITCH, PROP_MESH_SWITCH))
            if alt:
                self._is_on = bool(props[alt])
                self._prop_switch = alt

        # brightness (suppress rounding jitter if device percentage matches)
        if self._prop_brightness in props:
            try:
                v = int(props[self._prop_brightness])
                new_bright = max(1, round(v / 100 * 255))
                current_pct = round(self._brightness / 255 * 100)
                if v != current_pct:
                    self._brightness = new_bright
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )

        # colour temperature (suppress rounding jitter if device percentage matches)
        if self._prop_color_temp in props:
            try:
                v = int(props[self._prop_color_temp])
                new_k = round(
                    KELVIN_WARM + v / 100 * (KELVIN_COOL - KELVIN_WARM))
                current_pct = round(
                    (self._color_temp_k - KELVIN_WARM) / (KELVIN_COOL - KELVIN_WARM) * 100)
                if v != current_pct:
                    self._color_temp_k = new_k
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )

        # colour (by shape, via ColorSpec)
        if self._color_spec is not None:
            raw = props.get(self._color_spec.identifier)
            hs = self._color_spec.to_hs(raw) if raw is not None else None
            if hs is not None:
                self._hs = hs

        # If mode is explicitly white / CCT, clear HS so color_mode is COLOR_TEMP
        if self._mode_spec is not None and self._mode_spec.identifier in props:
            try:
                if int(props[self._mode_spec.identifier]) == self._mode_spec.white_value:
                    self._hs = None
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )

    # -- commands -------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        items: dict[str, Any] = {self._prop_switch: 1}
        # Save rollback state — restored if the write fails terminally
        prev = (self._is_on, self._brightness, self._color_temp_k, self._hs)
        self._is_on = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            items[self._prop_brightness] = max(1, round(brightness / 255 * 100))
            self._brightness = brightness

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            k = kwargs[ATTR_COLOR_TEMP_KELVIN]
            pct = (k - KELVIN_WARM) / (KELVIN_COOL - KELVIN_WARM)
            items[self._prop_color_temp] = max(0, min(100, round(pct * 100)))
            if self._color_spec is not None and self._mode_spec is not None:
                items[self._mode_spec.identifier] = self._mode_spec.white_value
            self._color_temp_k = k
            self._hs = None

        if ATTR_HS_COLOR in kwargs and self._color_spec is not None:
            h, s = kwargs[ATTR_HS_COLOR]
            b = kwargs.get(ATTR_BRIGHTNESS, self._brightness) / 255 * 100
            items[self._color_spec.identifier] = self._color_spec.build(h, s, b)
            if self._mode_spec is not None:
                items[self._mode_spec.identifier] = self._mode_spec.color_value
            self._hs = (h, s)

        # Optimistic state is already applied above; the commander serializes
        # rapid consecutive calls (slider moves) into one verified write.
        self.async_write_ha_state()
        self._coordinator.async_set_optimistic_props(self._iot_id, items, hold_duration=12.0)
        if self._commander is not None:
            await self._commander.async_send(items)
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties, self._iot_id, items
                )
            except Exception as exc:
                _LOGGER.warning("AigoSmart turn_on failed for %s: %s", self._iot_id, exc)
                self._revert(prev)

    async def async_turn_off(self, **kwargs: Any) -> None:
        prev = (self._is_on, self._brightness, self._color_temp_k, self._hs)
        self._is_on = False
        self.async_write_ha_state()
        self._coordinator.async_set_optimistic_props(
            self._iot_id, {self._prop_switch: 0}, hold_duration=12.0)
        if self._commander is not None:
            await self._commander.async_send({self._prop_switch: 0})
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties,
                    self._iot_id, {self._prop_switch: 0},
                )
            except Exception as exc:
                _LOGGER.warning("AigoSmart turn_off failed for %s: %s", self._iot_id, exc)
                self._revert(prev)

    def _revert(self, prev: tuple) -> None:
        """Restore pre-command state after a failed write."""
        self._is_on, self._brightness, self._color_temp_k, self._hs = prev
        self._coordinator.clear_optimistic_props(self._iot_id)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()
