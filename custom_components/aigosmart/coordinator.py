"""Data coordinator: polls cloud devices + properties and detects new devices.

Autodiscovery layer 1 (cloud):
  - Polls per-device properties every POLL_INTERVAL (30 s).
  - Refreshes the full device list every DEVICE_LIST_EVERY (10th poll = 5 min).
  - When new devices appear, listeners (platforms) add entities on the fly —
    no integration reload needed.
  - Lazily fetches and caches the TSL model per productKey (used by the light
    platform to resolve colour properties by shape).

Online-state handling:
  listBindingByAccount's "status" is not a reliable online flag (an online
  device can report 3). When a device reports non-ONLINE there, we confirm
  via /thing/status/get (as the app does) and cache the verdict. A device
  stays available while properties keep flowing in.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AigoSmartApiClient

# Status codes from the app's BaseDeviceStatus (classes11.dex):
# NOT_ACTIVATED=0, ONLINE=1, OFFLINE=3, DISABLED=8. listBindingByAccount's
# "status" is unreliable (online devices can report 3) — ambiguous values are
# confirmed via /thing/status/get.
STATUS_ONLINE = 1


def _is_status_online(status) -> bool:
    try:
        return int(status) == STATUS_ONLINE
    except (TypeError, ValueError):
        return False

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 30           # seconds
DEVICE_LIST_EVERY = 10       # refresh device list every Nth poll (10*30s = 5 min)
# Re-verify online status via /thing/status/get at most this often per device
STATUS_RECHECK_COOLDOWN = 60
# Consecutive property-poll failures after which a device is marked offline
PROP_FAIL_OFFLINE_THRESHOLD = 2

EVENT_DEVICES_CHANGED = "aigosmart_devices_changed"

RELOGIN_COOLDOWN = 300       # min seconds between full re-login attempts


class AigoDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator for all AigoSmart devices of a config entry."""

    def __init__(self, hass: HomeAssistant, client: AigoSmartApiClient,
                 entry: ConfigEntry | None = None) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="aigosmart",
            update_interval=timedelta(seconds=POLL_INTERVAL),
            config_entry=entry,
        )
        self.client = client
        self._poll_count = 0
        # productKey -> TSL model dict (lazy cache)
        self.tsl_models: dict[str, dict] = {}
        # data layout:
        #   {"devices": [ {device dict} ],
        #    "props":   { iotId: {prop: value} }}
        self.data = {"devices": [], "props": {}}
        # iotId -> { prop_name: (optimistic_value, hold_until_epoch) }
        self._optimistic_locks: dict[str, dict[str, tuple[Any, float]]] = {}
        # iotId -> (is_online, last_check_epoch)
        self._online_cache: dict[str, tuple[bool, float]] = {}
        # iotId -> consecutive property-poll failure count
        self._prop_fail_count: dict[str, int] = {}
        # Last full re-login attempt (session-expiry self-healing)
        self._last_relogin_attempt: float = 0.0

    @property
    def devices(self) -> list[dict]:
        return self.data.get("devices", [])

    @property
    def props(self) -> dict[str, dict]:
        return self.data.get("props", {})

    # ------------------------------------------------------------------
    # Online-state tracking
    # ------------------------------------------------------------------

    def is_device_online(self, iot_id: str) -> bool:
        """Best-known online state for a device (verified when ambiguous)."""
        cached = self._online_cache.get(iot_id)
        return cached[0] if cached else True

    def mark_device_offline(self, iot_id: str) -> None:
        """Immediately mark a device offline (e.g. after repeated write failures)."""
        self._online_cache[iot_id] = (False, time.time())
        self._optimistic_locks.pop(iot_id, None)
        self.async_update_listeners()

    def _is_session_error(self, exc: Exception) -> bool:
        """True when an API error means the iotToken/session expired."""
        msg = str(exc).lower()
        return any(k in msg for k in ("session", "token", "expired", "unauthorized",
                                       "401", "403", "not logged in"))

    def _handle_session_expiry(self, exc: Exception) -> None:
        """Try a full re-login with the stored credentials (once per failure burst)."""
        now = time.time()
        if now - self._last_relogin_attempt < RELOGIN_COOLDOWN:
            raise UpdateFailed(f"AigoSmart session expired: {exc}") from exc
        self._last_relogin_attempt = now
        _LOGGER.warning("AigoSmart session appears expired — attempting re-login")
        try:
            self.client.login()
        except Exception as login_exc:
            raise UpdateFailed(f"AigoSmart re-login failed: {login_exc}") from exc

    def _resolve_online(self, dev: dict) -> bool:
        """Determine whether a device is online.

        listBindingByAccount "status" equals 1 (ONLINE) → online.
        Any other value is ambiguous for some accounts/firmwares (an online
        device can report 3), so confirm once with /thing/status/get and
        cache the verdict for STATUS_RECHECK_COOLDOWN seconds.
        """
        iot_id = dev.get("iotId", "")
        if not iot_id:
            return False
        if _is_status_online(dev.get("status")):
            self._online_cache[iot_id] = (True, time.time())
            return True

        now = time.time()
        cached = self._online_cache.get(iot_id)
        if cached and now - cached[1] < STATUS_RECHECK_COOLDOWN:
            return cached[0]

        try:
            status_data = self.client.get_status(iot_id)
            online = _is_status_online(status_data.get("status"))
        except Exception as exc:
            _LOGGER.debug("Status verification failed for %s: %s", iot_id, exc)
            # The device didn't answer the status query — treat as offline
            # (never keep a stale True verdict from a previous check).
            online = False
        self._online_cache[iot_id] = (online, now)
        return online

    # ------------------------------------------------------------------
    # Optimistic property holds (commands vs stale shadow)
    # ------------------------------------------------------------------

    def async_set_optimistic_props(
        self, iot_id: str, items: dict[str, Any], hold_duration: float = 5.0
    ) -> None:
        """Optimistically record new properties and prevent them from being overwritten by stale polls."""
        now = time.time()
        hold_until = now + hold_duration

        props = self.data.setdefault("props", {}).setdefault(iot_id, {})
        locks = self._optimistic_locks.setdefault(iot_id, {})

        for k, v in items.items():
            props[k] = v
            locks[k] = (v, hold_until)

    def clear_optimistic_props(self, iot_id: str) -> None:
        """Drop all optimistic holds so the next poll restores real state."""
        self._optimistic_locks.pop(iot_id, None)

    def _merge_props(self, iot_id: str, cloud_props: dict[str, Any]) -> dict[str, Any]:
        """Merge freshly polled cloud properties with active optimistic holds."""
        now = time.time()
        merged = dict(self.data.get("props", {}).get(iot_id, {}))
        merged.update(cloud_props)

        locks = self._optimistic_locks.get(iot_id, {})
        if not locks:
            return merged

        expired_keys: list[str] = []
        for prop, (opt_val, hold_until) in locks.items():
            if now < hold_until:
                cloud_val = cloud_props.get(prop)
                # If cloud already matches our commanded value, release hold early
                if cloud_val is not None and str(cloud_val) == str(opt_val):
                    expired_keys.append(prop)
                else:
                    # Cloud shadow is still stale; enforce optimistic value
                    merged[prop] = opt_val
            else:
                expired_keys.append(prop)

        for prop in expired_keys:
            locks.pop(prop, None)

        return merged

    async def async_refresh_device(self, iot_id: str) -> None:
        """Poll and update state for a single device after a command."""
        try:
            cloud_props = await self.hass.async_add_executor_job(
                self.client.get_properties, iot_id
            )
            merged = self._merge_props(iot_id, cloud_props)
            self.data.setdefault("props", {})[iot_id] = merged
            self.async_update_listeners()
        except Exception as exc:
            _LOGGER.debug("Post-command device refresh failed for %s: %s", iot_id, exc)

    async def async_get_tsl(self, product_key: str, iot_id: str) -> dict:
        """Fetch (and cache) the TSL model for a product."""
        if product_key in self.tsl_models:
            return self.tsl_models[product_key]
        try:
            tsl = await self.hass.async_add_executor_job(
                self.client.get_tsl, iot_id)
            self.tsl_models[product_key] = tsl or {}
        except Exception as exc:
            _LOGGER.debug("TSL fetch failed for %s: %s", product_key, exc)
            self.tsl_models[product_key] = {}
        return self.tsl_models[product_key]

    async def _async_update_data(self) -> dict:
        # Full device list every Nth poll, else properties only
        if self._poll_count % DEVICE_LIST_EVERY == 0:
            try:
                devices = await self.hass.async_add_executor_job(
                    self.client.list_devices)
            except Exception as exc:
                if self._is_session_error(exc):
                    await self.hass.async_add_executor_job(
                        self._handle_session_expiry, exc)
                    devices = await self.hass.async_add_executor_job(
                        self.client.list_devices)
                else:
                    raise
            self._poll_count += 1
        else:
            self._poll_count += 1
            devices = self.data.get("devices", [])

        # Resolve online state for every device (cheap when status==1;
        # verified via /thing/status/get only when ambiguous)
        for dev in devices:
            iot_id = dev.get("iotId")
            if iot_id:
                online = await self.hass.async_add_executor_job(
                    self._resolve_online, dev)
                dev["status"] = STATUS_ONLINE if online else dev.get("status", 3)

        props: dict[str, dict] = {}
        for dev in devices:
            iot_id = dev.get("iotId")
            if not iot_id:
                continue
            try:
                cloud_props = await self.hass.async_add_executor_job(
                    self.client.get_properties, iot_id)
                self._prop_fail_count[iot_id] = 0
                props[iot_id] = self._merge_props(iot_id, cloud_props)
            except Exception as exc:  # one offline device must not kill the batch
                fails = self._prop_fail_count.get(iot_id, 0) + 1
                self._prop_fail_count[iot_id] = fails
                _LOGGER.debug("Property poll failed (%d) for %s: %s",
                              fails, iot_id, exc)
                if fails >= PROP_FAIL_OFFLINE_THRESHOLD:
                    # Device unreachable on consecutive polls — mark offline
                    # right away instead of waiting for the status cooldown.
                    self._online_cache[iot_id] = (False, time.time())
                    self._optimistic_locks.pop(iot_id, None)
                props[iot_id] = self._merge_props(iot_id, {})

        old_ids = {d.get("iotId") for d in self.data.get("devices", [])}
        new_ids = {d.get("iotId") for d in devices} - old_ids
        if new_ids:
            _LOGGER.info("AigoSmart autodiscovery: %d new device(s): %s",
                         len(new_ids), new_ids)
            self.hass.bus.async_fire(
                EVENT_DEVICES_CHANGED, {"new": list(new_ids), "iot_ids": list(new_ids)})

        return {"devices": devices, "props": props}
