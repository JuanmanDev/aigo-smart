"""Local ALCS (CoAP on the LAN) with transparent cloud fallback.

Used only when "local control" is turned on in the integration options
(experimental). Strategy:
  1. the device's LAN address comes from discovery (the background listener
     or a sweep), never from a multicast round-trip per request;
  2. per-device accessKey/accessToken come from the cloud
     (/alcs/device/accessInfo/get) and are cached;
  3. the encrypted session (lib.local.AlcsDevice) is kept and re-used;
  4. any failure (not on the LAN, no keys, auth refused, timeout) backs off
     and the caller gets the cloud answer instead.

Reads go local; writes stay on the cloud. A local write is applied by the
device but does not always reach the cloud shadow, which left the AigoSmart
app showing stale state.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .lib.local import (
    AlcsDevice,
    discover_alcs_devices,
    forget_lan_device,
    known_lan_address,
)

_LOGGER = logging.getLogger(__name__)

# Keys are long-lived; refetch them now and then (or after an auth refusal)
KEYS_TTL = 6 * 3600
# After a failure, don't try the local path for this long
LOCAL_FAIL_BACKOFF = 600


class LocalFirstMixin:
    """Mixin for the HA-facing client wrapper: local ALCS with cloud fallback."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # iot_id -> AlcsDevice with a (possibly) open session
        self._local_ok: dict[str, AlcsDevice] = {}
        # iot_id -> monotonic time until which local attempts are skipped
        self._local_bad: dict[str, float] = {}
        # iot_id -> (key dict, monotonic time fetched)
        self._local_keys: dict[str, tuple[dict, float]] = {}
        # iot_id -> last local outcome, for diagnostics
        self.local_status: dict[str, str] = {}

    # -- internal helpers -------------------------------------------------------

    def _local_key(self, iot_id: str, refresh: bool = False) -> dict | None:
        cached = self._local_keys.get(iot_id)
        if cached and not refresh and time.monotonic() - cached[1] < KEYS_TTL:
            return cached[0]
        keys = self.get_alcs_access_info([iot_id])
        key = next((k for k in keys or []
                    if isinstance(k, dict) and k.get("accessKey") and k.get("accessToken")),
                   None)
        if key is not None:
            self._local_keys[iot_id] = (key, time.monotonic())
        return key

    def _local_fail(self, iot_id: str, reason: str) -> None:
        self._local_ok.pop(iot_id, None)
        self._local_bad[iot_id] = time.monotonic() + LOCAL_FAIL_BACKOFF
        self.local_status[iot_id] = reason
        _LOGGER.debug("local: %s uses the cloud (%s)", iot_id, reason)

    def _local_dev(self, iot_id: str) -> AlcsDevice | None:
        """A local device handle, or None to use the cloud."""
        dev = self._local_ok.get(iot_id)
        if dev is not None:
            return dev
        if self._local_bad.get(iot_id, 0) > time.monotonic():
            return None
        try:
            pk, dn = self.get_pk_dn(iot_id)
        except Exception as exc:
            self._local_fail(iot_id, f"unknown device ({type(exc).__name__})")
            return None
        addr = known_lan_address(pk, dn)
        if addr is None:
            self._local_fail(iot_id, "not seen on the LAN")
            return None
        try:
            key = self._local_key(iot_id)
        except Exception as exc:
            self._local_fail(iot_id, f"no local key ({type(exc).__name__})")
            return None
        if key is None:
            self._local_fail(iot_id, "the cloud has no local key for this device")
            return None
        dev = AlcsDevice(addr[0], addr[1], product_key=pk, device_name=dn,
                         access_key=key["accessKey"], access_token=key["accessToken"])
        self._local_ok[iot_id] = dev
        return dev

    # -- public local-first API ---------------------------------------------------

    def get_properties_prefer_local(self, iot_id: str,
                                    identifiers: list[str] | None = None) -> dict:
        """Read properties from the device itself; the cloud shadow as fallback."""
        dev = self._local_dev(iot_id)
        if dev is not None:
            try:
                props = dev.get_properties(identifiers)
            except Exception as exc:
                if "auth" in str(exc):
                    # keys may have been rotated: fetch new ones next time
                    self._local_keys.pop(iot_id, None)
                if isinstance(exc, TimeoutError):
                    forget_lan_device(dev.product_key, dev.device_name)
                self._local_fail(iot_id, f"{type(exc).__name__}: {exc}")
            else:
                if props:
                    self.local_status[iot_id] = "ok"
                    return props
                self._local_fail(iot_id, "the device returned no properties")
        return self.get_properties(iot_id)

    def probe_local(self, iot_id: str, identifiers: list[str] | None = None,
                    discover: bool = True) -> dict[str, Any]:
        """Run every local step once and report the outcome (for diagnostics).

        Never raises, and returns no addresses or keys.
        """
        report: dict[str, Any] = {}
        try:
            pk, dn = self.get_pk_dn(iot_id)
        except Exception as exc:
            return {"result": f"unknown device ({type(exc).__name__})"}
        addr = known_lan_address(pk, dn)
        if addr is None and discover:
            try:
                discover_alcs_devices(timeout=2.0, attempts=1)
            except OSError as exc:
                report["discovery_error"] = str(exc)
            addr = known_lan_address(pk, dn)
        report["seen_on_lan"] = addr is not None
        try:
            key = self._local_key(iot_id, refresh=True)
        except Exception as exc:
            key = None
            report["local_key"] = f"error: {type(exc).__name__}"
        else:
            report["local_key"] = "available" if key else "none from the cloud"
        if addr is None or key is None:
            report["result"] = "local control not possible"
            return report
        dev = AlcsDevice(addr[0], addr[1], product_key=pk, device_name=dn,
                         access_key=key["accessKey"], access_token=key["accessToken"])
        try:
            dev.auth()
            report["auth"] = "ok"
        except Exception as exc:
            report["auth"] = f"{type(exc).__name__}: {exc}"
            report["result"] = "the device refused the local session"
            return report
        try:
            started = time.monotonic()
            props = dev.get_properties(identifiers)
            report["read_ms"] = round((time.monotonic() - started) * 1000)
            report["properties_read"] = sorted(props)
            report["result"] = "ok" if props else "session ok, but no properties returned"
        except Exception as exc:
            report["read"] = f"{type(exc).__name__}: {exc}"
            report["result"] = "session ok, read failed"
        return report

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
