"""The AigoSmart integration.

Cloud control of Aigostar smart-home devices via the Alibaba IoT platform
backend used by the AigoSmart app, plus optional local ALCS control.

Autodiscovery:
  Layer 1 (cloud) — the DataUpdateCoordinator refreshes the device list every
  5 minutes; new devices trigger dispatcher signals and entities are added
  to their platforms dynamically (no reload).
  Layer 2 (LAN)   — a background ALCS multicast listener discovers devices on
  the local network and fires events (shown in HA via the device registry).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from . import config_validation as cv
from .api import AigoSmartApiClient, NeedSecurityCodeError
from .const import CONF_SECURITY_CODE, DOMAIN
from .coordinator import AigoDataUpdateCoordinator
from .discovery import DeviceRegistry, load_pk_catalog

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT, Platform.SWITCH, Platform.CLIMATE, Platform.FAN,
             Platform.SENSOR, Platform.NUMBER, Platform.WATER_HEATER]
SERVICE_SYNC = "sync_devices"
SERVICE_DISCOVER_LOCAL = "discover_local"
SERVICE_ADD_DEVICE = "add_device"
SERVICE_DISCOVER_BLE = "discover_bluetooth"

CONFIG_SCHEMA = cv.config_schema()


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]
    security_code = entry.data.get(CONF_SECURITY_CODE, "")

    client = AigoSmartApiClient(email, password, security_code)

    try:
        await hass.async_add_executor_job(client.login)
    except NeedSecurityCodeError as exc:
        raise ConfigEntryAuthFailed("Security code required — re-authenticate") from exc
    except Exception as exc:
        raise ConfigEntryNotReady(f"Login failed: {exc}") from exc

    coordinator = AigoDataUpdateCoordinator(hass, client, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        raise ConfigEntryNotReady(f"First device sync failed: {exc}") from exc

    registry = DeviceRegistry()
    pk_catalog = await hass.async_add_executor_job(load_pk_catalog)

    state = {
        "hass": hass,
        "client": client,
        "coordinator": coordinator,
        "registry": registry,
        "pk_catalog": pk_catalog,
    }
    hass.data[DOMAIN][entry.entry_id] = state

    # ---- Layer 1: cloud autodiscovery ------------------------------------
    # Entities added by platforms during async_setup_entry and via dispatcher on every new device.

    # ---- Layer 2: LAN listener -------------------------------------------
    from .local_listener import async_start_listener
    await async_start_listener(hass)

    # Register discovered LAN devices into the HA device registry as
    # "via integration" placeholders so the user sees them in HA UI.
    def _handle_alcs_event(event) -> None:
        data = event.data
        devreg = dr.async_get(hass)
        d = devreg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, data.get("productKey", "") + data.get("deviceName", ""))},
            name=data.get("deviceName") or "ALCS device",
            manufacturer="Aigostar",
        )
        _LOGGER.debug("ALCS device registered in HA registry: %s", d.name)

    entry.async_on_unload(
        hass.bus.async_listen("aigosmart_alcs_found", _handle_alcs_event))

    # ---- services ----------------------------------------------------------
    async def _handle_sync(call: ServiceCall) -> None:
        for eid, st in list(hass.data.get(DOMAIN, {}).items()):
            try:
                await st["coordinator"].async_refresh()
            except Exception as exc:
                _LOGGER.warning("Sync failed for %s: %s", eid, exc)

    async def _handle_discover_local(call: ServiceCall) -> None:
        found = await hass.async_add_executor_job(client.discover_local, 3.0)
        for dev in found:
            hass.bus.async_fire("aigosmart_alcs_found", {
                "addr": dev.addr, "port": dev.port,
                "productKey": dev.product_key, "deviceName": dev.device_name,
            })
        _LOGGER.info("AigoSmart local discovery: %d device(s) found", len(found))

    async def _handle_add_device(call: ServiceCall) -> None:
        """Open the Add-Device flow (BLE + WiFi enrollee discovery)."""
        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "add_device"})

    async def _handle_discover_ble(call: ServiceCall) -> None:
        try:
            from .ble_discovery import async_discover_aigo_ble
            found = async_discover_aigo_ble(hass)
            for d in found:
                hass.bus.async_fire("aigosmart_ble_found", d)
            _LOGGER.info("AigoSmart BLE discovery: %d device(s)", len(found))
        except Exception as exc:
            _LOGGER.warning("BLE discovery failed (bluetooth integration active?): %s", exc)

    hass.services.async_register(DOMAIN, SERVICE_SYNC, _handle_sync)
    hass.services.async_register(DOMAIN, SERVICE_DISCOVER_LOCAL, _handle_discover_local)
    hass.services.async_register(DOMAIN, SERVICE_ADD_DEVICE, _handle_add_device)
    hass.services.async_register(DOMAIN, SERVICE_DISCOVER_BLE, _handle_discover_ble)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for svc in (SERVICE_SYNC, SERVICE_DISCOVER_LOCAL,
                        SERVICE_ADD_DEVICE, SERVICE_DISCOVER_BLE):
                if hass.services.has_service(DOMAIN, svc):
                    hass.services.async_remove(DOMAIN, svc)
            from .local_listener import async_stop_listener
            await async_stop_listener(hass)
    return unload_ok
