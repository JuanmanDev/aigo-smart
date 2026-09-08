"""Registry that maps cloud devices to Home Assistant platforms dynamically.

Autodiscovery layer 1 (cloud): every coordinator refresh, this registry diffs
the device list and fires the "add entities" callback for each new device.

Category mapping uses:
  1. categoryKey from the cloud (definitive)
  2. fallback: the bundled 498-product PK catalog (analysis/pk_catalog.json)
     keyed by productKey
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

# ---------------- category -> HA platform mapping --------------------------

CATEGORY_PLATFORM = {
    # lights
    "lamp": "light", "rgblamp": "light", "ceilinglight": "light",
    "downlighting": "light", "cloudlamp": "light", "striplight": "light",
    "filamentlamp": "light", "panellight": "light", "purifyinglamp": "light",
    "mininglamp": "light", "floodlight": "light", "spotlight": "light",
    "chandelier": "light", "walllamp": "light", "smartwalllamp": "light",
    "smartdesklamp": "light", "caretablelamp": "light", "sockettablelamp": "light",
    "stringoflights": "light", "solarlamp": "light", "fanlighting": "light",
    "dimming_panel": "light", "lighting": "light", "light": "light",
    # switches / plugs
    "socket": "switch", "meteredsocket": "switch", "outlet": "switch",
    "converter": "switch", "powerstrip": "switch", "waterproofoutlet": "switch",
    "switch": "switch", "curtainswitch": "switch", "circuitbreaker": "switch",
    "gateway": "switch",
    # climate
    "airconditioner": "climate", "heater": "climate", "dehumidifier": "climate",
    "oil-filledradiator": "climate", "petthermostat": "climate", "catbed": "climate",
    # fans
    "fan": "fan", "towerfan": "fan", "intelligentfan": "fan",
    # sensors
    "sensor": "sensor", "smokealarm": "sensor", "doorcontact": "sensor",
}

# Heater-family also climate
for _h in ("electrician",):
    pass


def platform_for_device(dev: dict, pk_catalog: dict[str, str] | None = None) -> str | None:
    """Decide which HA platform a cloud device should map to."""
    # 1. cloud-provided categoryKey (definitive when present)
    cat = (dev.get("categoryKey") or "").strip().lower()
    if not cat:
        # 2. productKey catalog fallback
        if pk_catalog:
            cat = pk_catalog.get(dev.get("productKey", ""), "").lower()
    if not cat:
        # 3. name heuristic as last resort
        name = (dev.get("nickName") or dev.get("deviceName") or "").lower()
        for key, plat in CATEGORY_PLATFORM.items():
            if key in name:
                return plat
        return None
    return CATEGORY_PLATFORM.get(cat)


# ---------------- registry --------------------------------------------------

class DeviceRegistry:
    """Tracks which iotIds already have entities, per platform."""

    def __init__(self) -> None:
        self._known: dict[str, set[str]] = {}   # platform -> set(iotId)
        self._devices: dict[str, dict] = {}     # iotId -> device dict

    def register(self, platform: str) -> None:
        self._known.setdefault(platform, set())

    def sync(
        self,
        devices: list[dict],
        pk_catalog: dict[str, str] | None = None,
    ) -> dict[str, list[dict]]:
        """Diff devices against known entities.

        Returns {platform: [new device dicts]} for platforms with additions.
        """
        new_by_platform: dict[str, list[dict]] = {}
        for dev in devices:
            iot_id = dev.get("iotId")
            if not iot_id:
                continue
            plat = platform_for_device(dev, pk_catalog)
            if plat is None:
                _LOGGER.debug("No platform mapping for %s (category=%s pk=%s)",
                              iot_id, dev.get("categoryKey"), dev.get("productKey"))
                continue
            self._devices[iot_id] = dev
            if iot_id not in self._known.setdefault(plat, set()):
                self._known[plat].add(iot_id)
                new_by_platform.setdefault(plat, []).append(dev)
        return new_by_platform


# ---------------- PK catalog -------------------------------------------------

def load_pk_catalog() -> dict[str, str]:
    """Load productKey -> category from the bundled catalog (498 products)."""
    import json as _json
    import os

    base_dir = os.path.dirname(__file__)
    # Vendored copy first (HACS installs only custom_components/),
    # then the repo checkout (dev / standalone use).
    candidates = [
        os.path.join(base_dir, "lib", "aigosmart_data", "pk_catalog.json"),
        os.path.normpath(os.path.join(base_dir, "..", "..", "python_client",
                                      "aigosmart_data", "pk_catalog.json")),
    ]
    path = next((p for p in candidates if os.path.isfile(p)), candidates[0])
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        return {
            pk: info.get("category", "")
            for pk, info in data.get("products", {}).items()
        }
    except Exception as exc:
        _LOGGER.warning("Could not load PK catalog: %s", exc)
        return {}
