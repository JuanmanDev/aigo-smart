"""Diagnostics support for AigoSmart.

Adds the native "Download diagnostics" button on both the integration page
and every device page (fixes the ha-config-device-page.ts console error:
``{code: 'not_found', message: 'Domain not supported'}``).

Device diagnostics include everything useful for debugging: cloud metadata,
live properties, the full TSL data model, ALCS local keys, LAN reachability
and connection info.
"""
from __future__ import annotations

import socket
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    DOMAIN,
    STATUS_DISABLED,
    STATUS_NOT_ACTIVATED,
    STATUS_OFFLINE,
    STATUS_ONLINE,
)

# Diagnostics files get pasted into public GitHub issues. identityAlias /
# identityId identify the AigoSmart account, iotId / deviceName the hardware,
# and the ALCS keys grant local control of the device.
TO_REDACT = {
    "iotId", "deviceName", "identityAlias", "identityId", "identifiers",
    "accessKey", "accessToken", "link", "mac", "entry_title",
}

_STATUS_LABELS = {
    STATUS_NOT_ACTIVATED: "NOT_ACTIVATED",
    STATUS_ONLINE: "ONLINE",
    STATUS_OFFLINE: "OFFLINE",
    STATUS_DISABLED: "DISABLED",
}


def _status_label(status: Any) -> str:
    try:
        return _STATUS_LABELS.get(int(status), f"UNKNOWN({status})")
    except (TypeError, ValueError):
        return f"UNKNOWN({status})"


def _redact(value: str | None, keep: int = 4) -> str:
    """Redact a sensitive string, keeping a short prefix for debugging."""
    if not value:
        return ""
    return value[:keep] + "…" + value[-2:] if len(value) > keep + 4 else "***"


def _tcp_probe(host: str, port: int, timeout: float = 1.5) -> dict:
    """Quick TCP reachability check used in device diagnostics."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True}
    except OSError as exc:
        return {"reachable": False, "error": str(exc)}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (integration page download)."""
    state = hass.data[DOMAIN].get(entry.entry_id, {})
    coordinator = state.get("coordinator")
    client = state.get("client")

    devices_out = []
    props_out: dict[str, Any] = {}
    if coordinator:
        for index, dev in enumerate(coordinator.devices):
            iot_id = dev.get("iotId", "")
            # properties are keyed by iotId; re-key by a stable alias so the
            # mapping survives redaction
            alias = f"device_{index}"
            props_out[alias] = coordinator.props.get(iot_id, {})
            devices_out.append({
                "alias": alias,
                "iotId": iot_id,
                "nickName": dev.get("nickName"),
                "deviceName": dev.get("deviceName"),
                "productKey": dev.get("productKey"),
                "productName": dev.get("productName"),
                "categoryKey": dev.get("categoryKey"),
                "netType": dev.get("netType"),
                "status": dev.get("status"),
                "status_label": _status_label(dev.get("status")),
                "bindTime": dev.get("bindTime"),
                "owned": dev.get("owned"),
                "nodeType": dev.get("nodeType"),
                "isEdgeGateway": dev.get("isEdgeGateway"),
                "identityAlias": dev.get("identityAlias"),
            })

    return async_redact_data({
        "integration": {
            "domain": DOMAIN,
            "entry_title": entry.title,
            "entry_version": entry.version,
            "iot_host": client._client.iot_host if client else None,
            "token_expires_in": (client._client.token_expire if client else None),
            "token_age_seconds": round(
                __import__("time").time() - client._client.token_created, 1
            ) if client else None,
            "iot_token_redacted": _redact(
                client._client.iot_token if client else None),
            "identity_id_redacted": _redact(
                client._client.identity_id if client else None),
        },
        "coordinator": {
            "poll_interval": getattr(coordinator, "update_interval", None).total_seconds()
            if coordinator else None,
            "poll_count": getattr(coordinator, "_poll_count", None) if coordinator else None,
            "tsl_cache_keys": list(getattr(coordinator, "tsl_models", {}).keys())
            if coordinator else [],
        },
        "devices": devices_out,
        "properties": props_out,
    }, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return rich diagnostics for a single device (device page download)."""
    state = hass.data[DOMAIN].get(entry.entry_id, {})
    coordinator = state.get("coordinator")
    client = state.get("client")

    iot_id = None
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN:
            iot_id = identifier[1]
            break

    dev_data: dict | None = None
    props: dict = {}
    tsl: dict | None = None
    alcs_keys: list = []
    if coordinator and iot_id:
        props = coordinator.props.get(iot_id, {})
        for d in coordinator.devices:
            if d.get("iotId") == iot_id:
                dev_data = d
                break
        if dev_data and client:
            pk = dev_data.get("productKey", "")
            try:
                tsl = await coordinator.async_get_tsl(pk, iot_id)
            except Exception as exc:
                tsl = {"_error": str(exc)}
            try:
                alcs_keys = await hass.async_add_executor_job(
                    client.get_alcs_access_info, [iot_id])
            except Exception:
                alcs_keys = []

    # LAN reachability: the cloud tells us nothing about local IPs; probe the
    # ALCS default port on the WAN-facing host is pointless, so we only report
    # the metadata needed to run `aigo_cli.py discover` on the LAN.
    status = dev_data.get("status") if dev_data else None

    diagnostics: dict[str, Any] = {
        "device_registry": {
            "name": device.name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
            "identifiers": [list(i) for i in device.identifiers],
        },
        "cloud": dev_data,
        "cloud_status_label": _status_label(status),
        "properties": props,
        "tsl_model": tsl,
        "local_control": {
            "alcs_access_info": alcs_keys or "not_available",
            "how_to_probe_lan": (
                "Run: python aigo_cli.py discover  (multicast 224.0.1.187:5683) "
                "then python aigo_cli.py localkeys <IOT_ID>"
            ),
        },
    }
    return async_redact_data(diagnostics, TO_REDACT)
