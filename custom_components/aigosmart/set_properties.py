"""aigosmart.set_properties: raw TSL property writes (advanced / debugging).

Lets a user send an exact payload to a device, e.g. to find out which write
a new or misbehaving device accepts, without waiting for a release. Admin
only, and validated: a non-empty mapping of property identifier -> value
(numbers, strings, booleans, or nested objects / lists of those).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_PROPERTIES = "set_properties"
ATTR_DEVICE_ID = "device_id"
ATTR_PROPERTIES = "properties"
MAX_PROPERTIES = 20
_MAX_DEPTH = 4


def _valid_value(value: Any, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_valid_value(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _valid_value(v, depth + 1) for k, v in value.items())
    return False


def validate_properties(value: Any) -> dict[str, Any]:
    """Voluptuous validator for the ``properties`` field."""
    if not isinstance(value, dict) or not value:
        raise vol.Invalid("properties must be a non-empty mapping of property -> value")
    if len(value) > MAX_PROPERTIES:
        raise vol.Invalid(f"at most {MAX_PROPERTIES} properties per call")
    for key, val in value.items():
        if not isinstance(key, str) or not key.strip():
            raise vol.Invalid("property identifiers must be non-empty strings")
        if not _valid_value(val):
            raise vol.Invalid(
                f"unsupported value for {key}: use numbers, strings, booleans, "
                "or objects / lists of those")
    return value


SCHEMA = vol.Schema({
    vol.Required(ATTR_DEVICE_ID): cv.string,
    vol.Required(ATTR_PROPERTIES): validate_properties,
})


def resolve_device(hass: HomeAssistant, device_id: str) -> tuple[str, dict, str]:
    """device_id -> (iotId, config entry state, device name) or raise."""
    device = dr.async_get(hass).async_get(device_id)
    iot_id = next((ident[1] for ident in device.identifiers if ident[0] == DOMAIN),
                  None) if device else None
    if not iot_id:
        raise ServiceValidationError(f"{device_id} is not an AigoSmart device")
    for state in hass.data.get(DOMAIN, {}).values():
        coordinator = state.get("coordinator")
        if coordinator and any(d.get("iotId") == iot_id for d in coordinator.devices):
            return iot_id, state, (device.name_by_user or device.name or iot_id)
    raise ServiceValidationError(
        f"{device.name or device_id} is not in any loaded AigoSmart account")


async def async_handle_set_properties(hass: HomeAssistant, call: ServiceCall) -> None:
    iot_id, state, name = resolve_device(hass, call.data[ATTR_DEVICE_ID])
    items = call.data[ATTR_PROPERTIES]
    _LOGGER.info("AigoSmart set_properties for %s: %s", name, items)
    try:
        await hass.async_add_executor_job(state["client"].set_properties, iot_id, items)
    except Exception as exc:
        raise HomeAssistantError(f"AigoSmart rejected the write to {name}: {exc}") from exc
    await state["coordinator"].async_refresh_device(iot_id)


def async_register(hass: HomeAssistant) -> None:
    """Register the admin-only action (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_PROPERTIES):
        return

    async def _handle(call: ServiceCall) -> None:
        await async_handle_set_properties(hass, call)

    async_register_admin_service(hass, DOMAIN, SERVICE_SET_PROPERTIES, _handle, schema=SCHEMA)
