"""Shared helpers for the AigoSmart entity platforms.

Adapted from hass-aigosmart (andreazllin) — device-type detection that
prevents fans/kettles/gateways from becoming bogus light entities.
"""
from __future__ import annotations

from .const import DOMAIN


def is_fan_device(dev: dict) -> bool:
    category = (dev.get("categoryKey") or "").strip().lower()
    product = (dev.get("productName") or "").strip().lower()
    return category == "fan" or "fan" in product.split() or product.startswith("fan")


def is_kettle_device(dev: dict) -> bool:
    category = (dev.get("categoryKey") or "").strip().lower()
    product = (dev.get("productName") or "").strip().lower()
    return category == "kettle" or "kettle" in product or "bouilloire" in product


def is_gateway_device(dev: dict) -> bool:
    return (dev.get("categoryKey") or "").strip().lower() == "gateway" \
        or (dev.get("category") or "").strip().lower() == "gateway"


def is_bt_device(dev: dict) -> bool:
    """BLE mesh device (routed through a gateway / phone app)."""
    return dev.get("netType") == "NET_BT"


def is_aquarium_device(dev: dict, pk_catalog: dict[str, str] | None = None) -> bool:
    """Check if a device is an aquarium."""
    category = (dev.get("categoryKey") or "").strip().lower()
    if not category and pk_catalog:
        category = pk_catalog.get(dev.get("productKey", ""), "").lower()
    product = (dev.get("productName") or "").strip().lower()
    name = (dev.get("deviceName") or "").strip().lower()
    nick = (dev.get("nickName") or "").strip().lower()
    pk = (dev.get("productKey") or "").strip()
    return (
        category == "aquarium"
        or pk == "a191KWgv5BZ"
        or "aquarium" in product or "水族" in product
        or "aquarium" in name or "水族" in name
        or "aquarium" in nick or "水族" in nick
    )
