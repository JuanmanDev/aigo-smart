"""Local-first helpers: try ALCS (CoAP on LAN) before the cloud.

Strategy (matches how the app itself works — LocalChannelDevice):
  1. discovery once per device; cache ip/port + cloud-provisioned
     accessKey/accessToken (/alcs/device/accessInfo/get)
  2. every get/set goes to the device directly over CoAP (~5-30 ms)
  3. any failure (offline, auth expired, firewall) transparently falls back
     to the cloud API — the caller never needs to know which path served it
"""
from __future__ import annotations

import logging
import time

from .lib import AlcsDevice, discover_alcs_devices

_LOGGER = logging.getLogger(__name__)

# How long a successful local path is remembered before re-discovery
LOCAL_CACHE_TTL = 300          # 5 min
# How long we avoid local attempts after a failure
LOCAL_FAIL_BACKOFF = 120       # 2 min


class LocalFirstMixin:
    """Mixin for the HA-facing client wrapper: local ALCS with cloud fallback."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # iot_id -> {"dev": AlcsDevice, "ts": float} on success
        self._local_ok: dict[str, dict] = {}
        # iot_id -> timestamp until which local attempts are skipped
        self._local_bad: dict[str, float] = {}

    # -- internal helpers -------------------------------------------------------

    def _local_dev(self, iot_id: str) -> AlcsDevice | None:
        """Return a working local device handle, or None to use the cloud."""
        now = time.monotonic()

        cached = self._local_ok.get(iot_id)
        if cached and now - cached["ts"] < LOCAL_CACHE_TTL:
            return cached["dev"]

        if self._local_bad.get(iot_id, 0) > now:
            return None

        # need cloud metadata for the local handshake
        try:
            pk, dn = self.get_pk_dn(iot_id)
            keys = self.get_alcs_access_info([iot_id])
        except Exception as exc:
            _LOGGER.debug("local-first: metadata fetch failed for %s: %s", iot_id, exc)
            self._local_bad[iot_id] = now + LOCAL_FAIL_BACKOFF
            return None
        if not keys:
            self._local_bad[iot_id] = now + LOCAL_FAIL_BACKOFF
            return None
        key = keys[0]

        # discover the device on the LAN (multicast, short timeout)
        try:
            found = discover_alcs_devices(timeout=1.0, attempts=1)
        except Exception as exc:
            _LOGGER.debug("local-first: discovery failed: %s", exc)
            self._local_bad[iot_id] = now + LOCAL_FAIL_BACKOFF
            return None

        target = next(
            (d for d in found if d.product_key == pk and d.device_name == dn), None)
        if target is None:
            self._local_bad[iot_id] = now + LOCAL_FAIL_BACKOFF
            return None

        dev = AlcsDevice(
            target.addr, target.port,
            product_key=pk, device_name=dn,
            access_key=key.get("accessKey", ""),
            access_token=key.get("accessToken", ""),
        )
        try:
            dev.auth()
        except Exception as exc:
            _LOGGER.debug("local-first: ALCS auth failed for %s: %s", iot_id, exc)
            self._local_bad[iot_id] = now + LOCAL_FAIL_BACKOFF
            return None

        self._local_ok[iot_id] = {"dev": dev, "ts": now}
        return dev

    # -- public local-first API ---------------------------------------------------

    def set_properties_prefer_local(self, iot_id: str, items: dict) -> dict:
        """Write properties; local CoAP first, cloud as fallback."""
        dev = self._local_dev(iot_id)
        if dev is not None:
            try:
                rsp = dev.set_properties(items)
                if rsp.get("code", 200) == 200:
                    _LOGGER.debug("local-first: SET via ALCS for %s", iot_id)
                    return rsp
            except Exception as exc:
                _LOGGER.debug("local-first: local SET failed (%s), falling back to cloud", exc)
                self._local_ok.pop(iot_id, None)
                self._local_bad[iot_id] = time.monotonic() + LOCAL_FAIL_BACKOFF
        return self.set_properties(iot_id, items)

    def get_properties_prefer_local(self, iot_id: str) -> dict:
        """Read properties; local CoAP first, cloud as fallback."""
        dev = self._local_dev(iot_id)
        if dev is not None:
            try:
                rsp = dev.get_properties()
                if rsp.get("code", 200) == 200:
                    _LOGGER.debug("local-first: GET via ALCS for %s", iot_id)
                    return rsp.get("data", rsp)
            except Exception as exc:
                _LOGGER.debug("local-first: local GET failed (%s), falling back to cloud", exc)
                self._local_ok.pop(iot_id, None)
                self._local_bad[iot_id] = time.monotonic() + LOCAL_FAIL_BACKOFF
        return self.get_properties(iot_id)

    # -- metadata helper -----------------------------------------------------------

    def get_pk_dn(self, iot_id: str) -> tuple[str, str]:
        """productKey/deviceName for an iotId, cached from the device list."""
        cache = getattr(self, "_pk_dn_cache", None)
        if cache is None:
            cache = self._pk_dn_cache = {}
        if iot_id not in cache:
            for d in self.list_devices():
                if d.get("iotId") == iot_id:
                    cache[iot_id] = (d.get("productKey", ""), d.get("deviceName", ""))
                    break
            else:
                raise KeyError(iot_id)
        return cache[iot_id]
