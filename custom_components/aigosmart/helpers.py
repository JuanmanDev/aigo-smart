"""Shared helpers for the AigoSmart entity platforms.

Adapted from hass-aigosmart (andreazllin) — device-type detection that
prevents fans/kettles/gateways from becoming bogus light entities.
"""
from __future__ import annotations

FAN_LIGHT_CATEGORIES = ("fanlight", "fanlighting")


def is_fan_light_device(dev: dict, pk_catalog: dict[str, str] | None = None) -> bool:
    """Ceiling fan with an integrated lamp (cloud categoryKey "fanLight").

    The bundled catalog spells the category "FanLighting"; the cloud returns
    "fanLight". Both are accepted, plus the Chinese product names.
    """
    category = (dev.get("categoryKey") or "").strip().lower()
    if not category and pk_catalog:
        category = pk_catalog.get(dev.get("productKey", ""), "").lower()
    product = (dev.get("productName") or "").strip().lower()
    return (
        category in FAN_LIGHT_CATEGORIES
        or product.replace(" ", "").startswith("fanlight")
        or "风扇灯" in product or "吊扇灯" in product
    )


def _category(dev: dict, pk_catalog: dict[str, str] | None) -> str:
    category = (dev.get("categoryKey") or "").strip().lower()
    if not category and pk_catalog:
        category = pk_catalog.get(dev.get("productKey", ""), "").lower()
    return category


def is_pet_fountain_device(dev: dict, pk_catalog: dict[str, str] | None = None) -> bool:
    """Pet water fountain (cloud "petWaterFountain", catalog "WaterDispenser")."""
    product = (dev.get("productName") or "").strip().lower()
    return (
        _category(dev, pk_catalog) in ("petwaterfountain", "waterdispenser")
        or "pet water fountain" in " ".join(product.split())
        or "宠物饮水机" in product
    )


def is_pet_feeder_device(dev: dict, pk_catalog: dict[str, str] | None = None) -> bool:
    """Pet feeder (cloud "petFeeder", catalog "Feeder")."""
    product = (dev.get("productName") or "").strip().lower()
    return (
        _category(dev, pk_catalog) in ("petfeeder", "feeder")
        or "pet feeder" in " ".join(product.split())
        or "喂食" in product
    )


def is_fan_device(dev: dict) -> bool:
    category = (dev.get("categoryKey") or "").strip().lower()
    product = (dev.get("productName") or "").strip().lower()
    return (
        category == "fan"
        or "fan" in product.split()
        or product.startswith("fan")
        or is_fan_light_device(dev)
    )


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


def is_airfryer_device(dev: dict, pk_catalog: dict[str, str] | None = None) -> bool:
    """Check if a device is an air fryer."""
    category = (dev.get("categoryKey") or "").strip().lower()
    if not category and pk_catalog:
        category = pk_catalog.get(dev.get("productKey", ""), "").lower()
    product = (dev.get("productName") or "").strip().lower()
    return (
        category == "airfryer"
        or "空气炸锅" in product
        or "airfryer" in product.replace(" ", "")
    )
