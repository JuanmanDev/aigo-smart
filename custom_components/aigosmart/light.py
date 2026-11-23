"""Light platform for AigoSmart devices.

Entities are created from the coordinator's device list; new devices detected
by the autodiscovery layer are added dynamically via the dispatcher signal.

Colour handling uses color_model.py (from hass-aigosmart, andreazllin):
the colour property is recognised by its *shape* (any struct with hue/
saturation or rgb members), with ranges read from the device's own TSL model
when available. Wi-Fi (TG7100C) and BLE Mesh products both work.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .color_model import (
    ColorSpec,
    ModeSpec,
    as_source_snippet,
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
    PROP_AQUARIUM_BRIGHTNESS,
    PROP_AQUARIUM_LIGHT_SCENE,
    PROP_AQUARIUM_LIGHT_SCENE_ID,
    PROP_AQUARIUM_LIGHT_SWITCH,
    PROP_FANLIGHT_LIGHT_SWITCH,
    PROP_LIGHT_BRIGHTNESS,
    PROP_LIGHT_COLOR_TEMP,
    PROP_LIGHT_MODE,
    PROP_LIGHT_SWITCH,
    PROP_MESH_BRIGHTNESS,
    PROP_MESH_COLOR_TEMP,
    PROP_MESH_SWITCH,
)
from .coordinator import EVENT_DEVICES_CHANGED, AigoDataUpdateCoordinator
from .discovery import DeviceRegistry, platform_for_device
from .helpers import (
    is_aquarium_device,
    is_bt_device,
    is_fan_device,
    is_fan_light_device,
    is_gateway_device,
    is_kettle_device,
)

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
    pk_catalog: dict = state.get("pk_catalog", {})
    registry.register("light")

    def _is_light(dev: dict) -> bool:
        # gateways, fans, kettles and aquariums must not become standard lights
        if is_gateway_device(dev) or is_fan_device(dev) or is_kettle_device(dev) or is_aquarium_device(dev, pk_catalog):
            return False
        return platform_for_device(dev, pk_catalog) == "light"

    def _is_aquarium_light(dev: dict) -> bool:
        # Only process aquarium devices
        if not is_aquarium_device(dev, pk_catalog):
            return False
        # Should not be a gateway, fan, or kettle
        if is_gateway_device(dev) or is_fan_device(dev) or is_kettle_device(dev):
            return False
        return True

    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            if not iot_id or iot_id in registry._known["light"]:
                continue
            if _is_aquarium_light(dev):
                registry._known["light"].add(iot_id)
                out.append(AigoSmartAquariumLight(coordinator, dev, state))
            elif is_fan_light_device(dev, pk_catalog):
                # the fan half is created by fan.py on the same device
                registry._known["light"].add(iot_id)
                out.append(AigoSmartFanLightLight(coordinator, dev, state))
            elif _is_light(dev):
                registry._known["light"].add(iot_id)
                out.append(AigoSmartLight(coordinator, dev, state))
        return out

    @callback
    def _async_handle_new(*_args) -> None:
        new = _make_entities()
        if new:
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

        # Colour capability: pinned profile > device TSL > live props snapshot.
        # The TSL is fetched in async_added_to_hass; a snapshot-based guess is
        # refined there, and again once properties arrive.
        self._color_spec: ColorSpec | None = None
        self._mode_spec: ModeSpec = ModeSpec(identifier=PROP_LIGHT_MODE)
        self._spec_source = self._resolve_color_specs()

        self._is_on = False
        self._brightness = 255
        self._color_temp_k = 3700
        self._hs = None
        # last known white/colour mode value (None = not reported yet)
        self._light_mode: int | None = None
        self._available = coordinator.is_device_online(self._iot_id)
        self._commander = get_commander(state, self._iot_id) if state else None
        self._apply(self._coordinator.props.get(self._iot_id, {}))

    # -- colour capability ---------------------------------------------------

    def _resolve_color_specs(self) -> str:
        """Pick the colour + white/colour-mode handling; return where it came from."""
        product_key = self._dev.get("productKey", "")
        profile = known_profile(product_key)
        if profile is not None:
            self._color_spec, self._mode_spec = profile.color, profile.mode
            source = "profile"
        elif tsl := (self._coordinator.tsl_models.get(product_key) or {}):
            self._color_spec = color_spec_from_tsl(tsl)
            self._mode_spec = mode_spec_from_tsl(tsl, PROP_LIGHT_MODE)
            source = "tsl"
        else:
            # last resort: infer from a live property snapshot (empty right
            # after a restart if the first poll missed this device)
            props = self._coordinator.props.get(self._iot_id, {})
            self._color_spec = color_spec_from_props(props)
            self._mode_spec = ModeSpec(identifier=PROP_LIGHT_MODE)
            source = "props" if props else "none"

        if self._color_spec is not None:
            _LOGGER.debug(
                "AigoSmart resolved colour profile for %s (%s, from %s):\n%s",
                product_key, self._iot_id, source,
                as_source_snippet(product_key, self._color_spec, self._mode_spec),
            )
        else:
            _LOGGER.debug("AigoSmart: no colour property for %s (%s, from %s)",
                          product_key, self._iot_id, source)
        return source

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._spec_source in ("profile", "tsl"):
            return
        product_key = self._dev.get("productKey", "")
        if not product_key:
            return
        # Before, the TSL was only ever fetched by a diagnostics download, so
        # the "resolve from TSL" branch never ran in practice.
        tsl = await self._coordinator.async_get_tsl(product_key, self._iot_id)
        if tsl:
            self._spec_source = self._resolve_color_specs()
            self.async_write_ha_state()

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
        if props and self._spec_source == "none":
            # Entity was created before the first property poll: without this
            # the light stayed colour_temp-only until the entry was reloaded.
            self._spec_source = self._resolve_color_specs()
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
                self._light_mode = int(props[self._mode_spec.identifier])
                if self._light_mode == self._mode_spec.white_value:
                    self._hs = None
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )

    # -- commands -------------------------------------------------------------

    def _power_on_items(self) -> dict[str, Any]:
        """Switch part of a turn_on write."""
        return {self._prop_switch: 1}

    def _mode_change(self, value: int) -> dict[str, Any]:
        """Light-mode write, only when the mode actually changes."""
        if self._mode_spec is None or self._light_mode == value:
            return {}
        self._light_mode = value
        return {self._mode_spec.identifier: value}

    async def async_turn_on(self, **kwargs: Any) -> None:
        items: dict[str, Any] = self._power_on_items()
        # The white/colour mode switch is written separately, after the colour
        # or colour-temperature value it activates, and only when it changes.
        # In one combined write the gateway may apply the mode first, and the
        # lamp then falls back to its previously stored colour (#2, #3; same
        # approach as hass-aigosmart).
        mode_item: dict[str, Any] = {}
        # Save rollback state — restored if the write fails terminally
        prev = (self._is_on, self._brightness, self._color_temp_k, self._hs,
                self._light_mode)
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
                mode_item = self._mode_change(self._mode_spec.white_value)
            self._color_temp_k = k
            self._hs = None

        if ATTR_HS_COLOR in kwargs and self._color_spec is not None:
            h, s = kwargs[ATTR_HS_COLOR]
            b = kwargs.get(ATTR_BRIGHTNESS, self._brightness) / 255 * 100
            items[self._color_spec.identifier] = self._color_spec.build(h, s, b)
            if self._mode_spec is not None:
                mode_item = self._mode_change(self._mode_spec.color_value)
            self._hs = (h, s)

        if not items:
            if mode_item:
                items, mode_item = mode_item, {}
            else:
                # already on, nothing to change: a bare power-on is a
                # harmless resync for a lamp switched off elsewhere
                items = {self._prop_switch: 1}

        # Optimistic state is already applied above; the commander serializes
        # rapid consecutive calls (slider moves) into one verified write.
        self.async_write_ha_state()
        self._coordinator.async_set_optimistic_props(
            self._iot_id, {**items, **mode_item}, hold_duration=12.0)
        if self._commander is not None:
            await self._commander.async_send(items, followup=mode_item or None)
        else:
            try:
                await self.hass.async_add_executor_job(
                    self._coordinator.client.set_properties, self._iot_id, items
                )
                if mode_item:
                    await self.hass.async_add_executor_job(
                        self._coordinator.client.set_properties, self._iot_id,
                        mode_item,
                    )
            except Exception as exc:
                _LOGGER.warning("AigoSmart turn_on failed for %s: %s", self._iot_id, exc)
                self._revert(prev)

    async def async_turn_off(self, **kwargs: Any) -> None:
        prev = (self._is_on, self._brightness, self._color_temp_k, self._hs,
                self._light_mode)
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
        (self._is_on, self._brightness, self._color_temp_k, self._hs,
         self._light_mode) = prev
        self._coordinator.clear_optimistic_props(self._iot_id)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._commander is not None:
            await self._commander.async_flush()
        await super().async_will_remove_from_hass()

class AigoSmartFanLightLight(AigoSmartLight):
    """Lamp half of a ceiling fan-light (categoryKey "fanLight").

    The TSL has two channels: powerstate_1 / mode_1 drive the lamp and
    powerstate_2 / mode_2 the fan (see fan.py). Brightness, colour
    temperature, HSVColor and LightMode are the usual light properties, so
    everything except the switch identifier is inherited.
    """

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator, dev, state)
        self._prop_switch = PROP_FANLIGHT_LIGHT_SWITCH
        self._attr_unique_id = f"{self._iot_id}_light"
        self._attr_name = "Light"
        self._apply(self._coordinator.props.get(self._iot_id, {}))

    def _power_on_items(self) -> dict[str, Any]:
        # Same firmware as the fan half: a value bundled with a redundant
        # powerstate = 1 can be dropped (diagnosed in #2), so only send the
        # switch when the lamp is off.
        return {} if self._is_on else {self._prop_switch: 1}


class AigoSmartAquariumLight(AigoSmartLight):
    """Aquarium light with RGB color and preset scene effect support."""

    def __init__(self, coordinator: AigoDataUpdateCoordinator, dev: dict,
                 state: dict | None = None) -> None:
        super().__init__(coordinator, dev, state)
        self._prop_switch = PROP_AQUARIUM_LIGHT_SWITCH
        self._prop_brightness = PROP_AQUARIUM_BRIGHTNESS
        self._prop_color_temp = None

        self._attr_unique_id = f"{self._iot_id}_light"
        self._attr_name = "Light"
        self._attr_supported_features = LightEntityFeature.EFFECT
        self._attr_effect_list = [f"Scene {i}" for i in range(1, 13)]
        self._attr_effect = None

        # Override device info to link to the main aquarium device
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
        self._apply(self._coordinator.props.get(self._iot_id, {}))

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {ColorMode.HS}

    @property
    def color_mode(self) -> ColorMode:
        return ColorMode.HS

    @property
    def effect_list(self) -> list[str] | None:
        return self._attr_effect_list

    @property
    def effect(self) -> str | None:
        return self._attr_effect

    def _apply(self, props: dict) -> None:
        # switch
        if self._prop_switch in props:
            try:
                self._is_on = bool(int(props[self._prop_switch]))
            except (TypeError, ValueError):
                self._is_on = bool(props[self._prop_switch])

        # brightness (aquarium uses 1-100, HA uses 0-255)
        if self._prop_brightness in props:
            try:
                v = int(props[self._prop_brightness])
                self._brightness = max(1, round(v / 100 * 255))
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "AigoSmart: non-numeric property value for %s",
                    self._iot_id,
                )

        # scene ID (1..12)
        if PROP_AQUARIUM_LIGHT_SCENE_ID in props:
            try:
                sid = int(props[PROP_AQUARIUM_LIGHT_SCENE_ID])
                if 1 <= sid <= 12:
                    self._attr_effect = f"Scene {sid}"
            except (TypeError, ValueError):
                pass

        # LightScene struct: extract HS color if color mode active
        if PROP_AQUARIUM_LIGHT_SCENE in props:
            raw_scene = props[PROP_AQUARIUM_LIGHT_SCENE]
            scene = as_struct(raw_scene)
            if scene:
                color_arr_raw = scene.get("ColorArr")
                if color_arr_raw:
                    color_arr = None
                    if isinstance(color_arr_raw, str):
                        try:
                            color_arr = json.loads(color_arr_raw)
                        except (TypeError, ValueError):
                            color_arr = None
                    elif isinstance(color_arr_raw, list):
                        color_arr = color_arr_raw

                    if color_arr and isinstance(color_arr, list) and isinstance(color_arr[0], dict):
                        h = color_arr[0].get("Hue")
                        s = color_arr[0].get("Saturation")
                        if h is not None and s is not None:
                            try:
                                h_val = float(h)
                                s_val = float(s)
                                if scene.get("LightMode") == 1 or h_val > 0 or s_val > 0:
                                    self._hs = (max(0.0, min(360.0, h_val)), max(0.0, min(100.0, s_val)))
                            except (TypeError, ValueError):
                                pass

        self._color_temp_k = None  # Ensure color temperature is cleared

    async def async_turn_on(self, **kwargs: Any) -> None:
        items: dict[str, Any] = {self._prop_switch: 1}
        # Save rollback state — restored if the write fails terminally
        prev = (self._is_on, self._brightness, self._color_temp_k, self._hs, self._attr_effect)
        self._is_on = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            items[self._prop_brightness] = max(1, round(brightness / 255 * 100))
            self._brightness = brightness

        if ATTR_EFFECT in kwargs:
            effect_name = str(kwargs[ATTR_EFFECT])
            digits = re.findall(r"\d+", effect_name)
            if digits:
                sid = int(digits[0])
                if 1 <= sid <= 12:
                    items[PROP_AQUARIUM_LIGHT_SCENE_ID] = sid
                    self._attr_effect = f"Scene {sid}"

        if ATTR_HS_COLOR in kwargs:
            h, s = kwargs[ATTR_HS_COLOR]
            self._hs = (float(h), float(s))
            self._attr_effect = None
            val = max(1, min(100, round(self._brightness / 255 * 100))) if self._brightness else 100
            color_arr = [{"Hue": round(float(h)), "Saturation": round(float(s)), "Value": val}]
            items[PROP_AQUARIUM_LIGHT_SCENE] = {
                "LightMode": 1,
                "SceneMode": 0,
                "ColorSpeed": 10,
                "Enable": 1,
                "ColorArr": json.dumps(color_arr),
            }

        _LOGGER.debug(
            "AigoSmart aquarium light turn_on for %s: %s",
            self._iot_id, items,
        )

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
                _LOGGER.warning("AigoSmart aquarium light turn_on failed for %s: %s", self._iot_id, exc)
                self._revert(prev)

    async def async_turn_off(self, **kwargs: Any) -> None:
        prev = (self._is_on, self._brightness, self._color_temp_k, self._hs, self._attr_effect)
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
                _LOGGER.warning("AigoSmart aquarium light turn_off failed for %s: %s", self._iot_id, exc)
                self._revert(prev)

    def _revert(self, prev: tuple) -> None:
        """Restore pre-command state after a failed write."""
        self._is_on, self._brightness, self._color_temp_k, self._hs, self._attr_effect = prev
        self._coordinator.clear_optimistic_props(self._iot_id)
        self.async_write_ha_state()
