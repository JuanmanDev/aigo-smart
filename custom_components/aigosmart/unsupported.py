"""Repair issues for devices the integration cannot create entities for yet.

One issue per productKey (two identical unsupported plugs share it). Issues
are recomputed on every coordinator update, so they disappear on their own
once a newer version supports the device, the device is removed from the
account, or the integration is unloaded.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .discovery import platform_for_device
from .helpers import is_fan_device, is_kettle_device

_LOGGER = logging.getLogger(__name__)

ISSUE_PREFIX = "unsupported_device_"
TRANSLATION_KEY = "unsupported_device"
DEVICE_SUPPORT_URL = (
    "https://github.com/JuanmanDev/aigo-smart/issues/new?template=device_support.yml"
)
_MAX_NAMES = 3


def is_unsupported(dev: dict, pk_catalog: dict[str, str] | None) -> bool:
    """True when no platform creates entities for this device."""
    return (platform_for_device(dev, pk_catalog) is None
            and not is_fan_device(dev) and not is_kettle_device(dev))


def collect_unsupported(states: list[dict]) -> dict[str, dict[str, Any]]:
    """productKey -> placeholders for every unsupported device of all entries."""
    found: dict[str, dict[str, Any]] = {}
    for state in states:
        coordinator = state.get("coordinator")
        if coordinator is None:
            continue
        pk_catalog = state.get("pk_catalog") or {}
        for dev in coordinator.devices:
            if not dev.get("iotId") or not is_unsupported(dev, pk_catalog):
                continue
            product_key = dev.get("productKey") or "unknown"
            info = found.setdefault(product_key, {
                "names": [],
                "product": (dev.get("productName") or "").strip() or product_key,
                "category": dev.get("categoryKey") or "unknown",
                "product_key": product_key,
            })
            name = (dev.get("nickName") or dev.get("deviceName") or "").strip()
            if name and name not in info["names"]:
                info["names"].append(name)
    return found


def _placeholders(info: dict[str, Any]) -> dict[str, str]:
    names = info["names"] or [info["product"]]
    shown = ", ".join(names[:_MAX_NAMES])
    if len(names) > _MAX_NAMES:
        shown += f" (+{len(names) - _MAX_NAMES})"
    return {
        "name": shown,
        "product": info["product"],
        "category": info["category"],
        "product_key": info["product_key"],
        "url": DEVICE_SUPPORT_URL,
    }


@callback
def async_update_unsupported_issues(hass: HomeAssistant) -> None:
    """Create / refresh / delete the unsupported-device repair issues."""
    states = list(hass.data.get(DOMAIN, {}).values())
    wanted = {ISSUE_PREFIX + pk: _placeholders(info)
              for pk, info in collect_unsupported(states).items()}

    registry = ir.async_get(hass)
    existing = {issue_id for (domain, issue_id) in list(registry.issues)
                if domain == DOMAIN and issue_id.startswith(ISSUE_PREFIX)}

    for issue_id, placeholders in wanted.items():
        current = registry.async_get_issue(DOMAIN, issue_id)
        if current is not None and current.translation_placeholders == placeholders:
            continue
        if current is None:
            _LOGGER.info(
                "AigoSmart: '%s' (category=%s, productKey=%s, product=%s) is not "
                "supported yet, so no entities were created for it. A repair issue "
                "links to the device support form: %s",
                placeholders["name"], placeholders["category"],
                placeholders["product_key"], placeholders["product"], DEVICE_SUPPORT_URL)
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=TRANSLATION_KEY,
            translation_placeholders=placeholders,
            learn_more_url=DEVICE_SUPPORT_URL,
        )

    for issue_id in existing - set(wanted):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def async_delete_all_unsupported_issues(hass: HomeAssistant) -> None:
    """Remove every unsupported-device issue (integration removed)."""
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and issue_id.startswith(ISSUE_PREFIX):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
