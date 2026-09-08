"""Bluetooth (Breeze) device discovery for Home Assistant.

Hooks into HA's bluetooth integration (websocket + BluetoothServiceInfoBleak)
to find Aigostar BLE devices — both pairing-mode devices and BLE-Mesh lights.

Identification:
  * Breeze service UUID 0000FEB3 (Alibaba BLE pairing) → device is in
    pairing mode and can be added
  * Local name / manufacturer data containing a known Aigostar productKey
    prefix pattern
"""
from __future__ import annotations

import logging
import re

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.core import HomeAssistant, callback

from .lib import const as aigo_const

_LOGGER = logging.getLogger(__name__)

# 11-char Alibaba productKeys start with "a1" (e.g. a1JLKGWCSzO)
_PK_RE = re.compile(r"\ba1[0-9A-Za-z]{9}\b")

BREEZE_SERVICE = aigo_const.BREEZE_SERVICE_UUID.upper()


def is_breeze_record(service_info: BluetoothServiceInfoBleak) -> bool:
    """True if the advertisement carries the Breeze pairing service."""
    return any(u.upper().startswith("0000FEB3") for u in service_info.advertisement.service_uuids)


def extract_product_key(service_info: BluetoothServiceInfoBleak) -> str | None:
    """Try to pull an AigoSmart productKey out of the local name or mfr data."""
    name = service_info.advertisement.local_name or ""
    m = _PK_RE.search(name)
    if m:
        return m.group(0)
    # Manufacturer data is bytes; productKey sometimes appears as ASCII there
    for _, data in (service_info.advertisement.manufacturer_data or {}).items():
        try:
            text = bytes(data).decode("ascii", "ignore")
            m = _PK_RE.search(text)
            if m:
                return m.group(0)
        except Exception:
            continue
    return None


@callback
def async_discover_aigo_ble(hass: HomeAssistant) -> list[dict]:
    """Return discovered BLE devices that look like Aigostar devices."""
    out: list[dict] = []
    for service_info in async_discovered_service_info(hass):
        pk = extract_product_key(service_info)
        breeze = is_breeze_record(service_info)
        if not (breeze or pk):
            continue
        out.append({
            "address": service_info.address,
            "name": service_info.advertisement.local_name or service_info.name,
            "rssi": service_info.rssi,
            "product_key": pk,
            "pairing": breeze,   # Breeze service present => in pairing mode
        })
    return out
