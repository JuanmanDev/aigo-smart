"""Standalone BLE identification of Aigostar devices (used by CLI + tests).

Works with bleak (standalone) or with HA's bluetooth integration
(ble_discovery.py). The rules were derived from the APK:

  * Service UUID 0000FEB3 = Alibaba Breeze pairing service
    (com.aliyun.iot.breeze.BluetoothUuid/BreezeUuid, classes16.dex)
    A device advertising FEB3 is in pairing mode and can be provisioned.
  * Characteristics FED4-FED8 (read/write/indicate/write-no-rsp/notify).
  * AigoSmart productKeys look like a1 + 9 alphanumerics (e.g. a1JLKGWCSzO)
    and sometimes appear in the advertised local name or manufacturer data.
"""
from __future__ import annotations

import re
from typing import Any

_PK_RE = re.compile(r"\ba1[0-9A-Za-z]{9}\b")

BREEZE_SERVICE_SHORT = 0xFEB3


def _uuid_is_breeze(uuid: str) -> bool:
    try:
        return int(uuid.split("-")[0], 16) == BREEZE_SERVICE_SHORT
    except (ValueError, IndexError):
        return False


def is_aigo_device(ble_device: Any, adv_data: Any) -> dict | None:
    """Return info dict if the advertisement looks like an Aigostar device.

    bleak-style objects: BleakScanner.discover(return_adv=True) gives
    (BLEDevice, AdvertisementData) pairs.
    """
    uuids = list(getattr(adv_data, "service_uuids", None) or [])
    # bleak uses short uuid strings like '0000feb3-0000-...' or ints; handle both
    norm = []
    for u in uuids:
        if isinstance(u, int):
            norm.append(f"{u:08x}-0000-1000-8000-00805f9b34fb")
        else:
            norm.append(str(u).lower())
    pairing = any(_uuid_is_breeze(u) for u in norm)

    name = getattr(adv_data, "local_name", None) or (getattr(ble_device, "name", None) or "")
    pk = None
    m = _PK_RE.search(name or "")
    if m:
        pk = m.group(0)
    if pk is None:
        mfr = getattr(adv_data, "manufacturer_data", None) or {}
        for data in mfr.values():
            try:
                text = bytes(data).decode("ascii", "ignore")
                m = _PK_RE.search(text)
                if m:
                    pk = m.group(0)
                    break
            except Exception:
                continue

    rssi = getattr(adv_data, "rssi", None) or 0

    if not (pairing or pk):
        return None
    return {
        "address": getattr(ble_device, "address", None),
        "name": name,
        "rssi": rssi,
        "product_key": pk,
        "pairing": pairing,
    }
