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
  device can report 3) and is only refreshed every DEVICE_LIST_EVERY polls.
  /thing/status/get (as the app uses) is the authority: it is queried for
  every device on every poll. The cloud keeps serving the last property
  shadow for an unpowered device (e.g. a light cut at the wall switch), so
  property polls alone can never reveal that a device went offline.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AigoSmartApiClient

# same key as const.CONF_LOCAL_CONTROL (const is not imported here: the unit
# tests load this module without its package)
CONF_LOCAL_CONTROL = "local_control"

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
# (just under POLL_INTERVAL so every regular poll re-checks)
STATUS_RECHECK_COOLDOWN = POLL_INTERVAL - 5
# Consecutive property-poll failures after which a device is marked offline
PROP_FAIL_OFFLINE_THRESHOLD = 2
# Consecutive /thing/status/get failures after which a device is marked offline
STATUS_FAIL_OFFLINE_THRESHOLD = 2
# Devices polled at the same time (each poll = status + properties calls);
# sequential polling took ~1 s per device, so 10 devices took ~10 s
POLL_CONCURRENCY = 4

EVENT_DEVICES_CHANGED = "aigosmart_devices_changed"


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
        # serialises TSL fetches so N lights of one product make one request
        self._tsl_lock = asyncio.Lock()
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
        # iotId -> consecutive /thing/status/get failure count
        self._status_fail_count: dict[str, int] = {}
        # Devices whose firmware applies cloud writes but never reports the
        # new value back (the shadow stays frozen, #4): what HA last wrote is
        # the best known state.  iotId -> {prop: [written, stale_shadow]};
        # kept until the shadow moves away from the stale value it had.
        self._assumed: dict[str, dict[str, list]] = {}
        # iotId -> last raw cloud shadow (without optimistic/assumed values)
        self._raw_props: dict[str, dict[str, Any]] = {}
        # optional persistence (homeassistant.helpers.storage.Store)
        self._assumed_store = None
        # read device state over the LAN when possible (options flow)
        options = getattr(entry, "options", None)
        self.local_reads = bool(isinstance(options, dict)
                                and options.get(CONF_LOCAL_CONTROL, False))

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

    def _resolve_online(self, dev: dict) -> bool:
        """Determine whether a device is online.

        /thing/status/get is authoritative and is re-queried once the cached
        verdict is older than STATUS_RECHECK_COOLDOWN. The listBinding
        "status" is never trusted on its own: it is stale between device-list
        refreshes and an online device can report 3 there. It is only used
        as a fallback when there is no verdict yet and the status query fails.

        The raw device dict is never modified — writing the verdict back into
        dev["status"] previously turned it into a self-confirming "online"
        flag, so a device that lost power stayed available until restart.
        """
        iot_id = dev.get("iotId", "")
        if not iot_id:
            return False

        now = time.time()
        cached = self._online_cache.get(iot_id)
        if cached and now - cached[1] < STATUS_RECHECK_COOLDOWN:
            return cached[0]

        try:
            status_data = self.client.get_status(iot_id)
            online = _is_status_online(status_data.get("status"))
            self._status_fail_count[iot_id] = 0
        except Exception as exc:
            fails = self._status_fail_count.get(iot_id, 0) + 1
            self._status_fail_count[iot_id] = fails
            _LOGGER.debug("Status verification failed (%d) for %s: %s",
                          fails, iot_id, exc)
            if fails >= STATUS_FAIL_OFFLINE_THRESHOLD:
                online = False
            elif cached:
                # One transient API hiccup must not flap the entity
                online = cached[0]
            else:
                online = _is_status_online(dev.get("status"))
        if cached and cached[0] != online:
            _LOGGER.info("AigoSmart device %s (%s) is now %s", iot_id,
                         dev.get("nickName") or dev.get("deviceName") or "?",
                         "online" if online else "offline")
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

    # ------------------------------------------------------------------
    # Assumed state (devices that never report written values back)
    # ------------------------------------------------------------------

    async def async_load_assumed(self, store) -> None:
        """Attach a Store and restore assumed values saved before a restart."""
        self._assumed_store = store
        try:
            data = await store.async_load()
        except Exception as exc:  # corrupt file must not block setup
            _LOGGER.debug("AigoSmart: could not load assumed state: %s", exc)
            data = None
        if isinstance(data, dict):
            self._assumed = {
                iot_id: {k: list(v) for k, v in props.items()
                         if isinstance(v, (list, tuple)) and len(v) == 2}
                for iot_id, props in data.items() if isinstance(props, dict)}

    def _save_assumed(self) -> None:
        if self._assumed_store is not None:
            self._assumed_store.async_delay_save(
                lambda: {k: v for k, v in self._assumed.items() if v}, 5)

    def remember_write(self, iot_id: str, items: dict[str, Any]) -> None:
        """Keep showing ``items`` while the shadow still has its old values."""
        raw = self._raw_props.get(iot_id, {})
        assumed = self._assumed.setdefault(iot_id, {})
        for prop, value in items.items():
            stale = assumed[prop][1] if prop in assumed else raw.get(prop)
            if stale is None or str(stale) == str(value):
                # unknown baseline, or back to what the shadow says anyway
                assumed.pop(prop, None)
            else:
                assumed[prop] = [value, stale]
        self._save_assumed()

    def assumed_props(self, iot_id: str) -> dict[str, Any]:
        """Written values currently shown instead of the frozen shadow."""
        return {k: v[0] for k, v in self._assumed.get(iot_id, {}).items()}

    def _apply_assumed(self, iot_id: str, cloud_props: dict[str, Any],
                       merged: dict[str, Any]) -> None:
        assumed = self._assumed.get(iot_id)
        if not assumed:
            return
        changed = False
        for prop, (value, stale) in list(assumed.items()):
            if prop not in cloud_props:
                merged[prop] = value
            elif str(cloud_props[prop]) == str(stale):
                merged[prop] = value            # shadow still frozen
            else:
                # the shadow moved (device report, app, button on the
                # device): that is real state again
                _LOGGER.debug("AigoSmart %s: shadow now reports %s=%s, "
                              "dropping assumed %s", iot_id, prop,
                              cloud_props[prop], value)
                assumed.pop(prop)
                changed = True
        if changed:
            self._save_assumed()

    def clear_optimistic_props(self, iot_id: str) -> None:
        """Drop all optimistic holds so the next poll restores real state."""
        self._optimistic_locks.pop(iot_id, None)

    def _merge_props(self, iot_id: str, cloud_props: dict[str, Any]) -> dict[str, Any]:
        """Merge freshly polled cloud properties with active optimistic holds."""
        now = time.time()
        merged = dict(self.data.get("props", {}).get(iot_id, {}))
        merged.update(cloud_props)
        if cloud_props:
            self._raw_props[iot_id] = dict(cloud_props)
        self._apply_assumed(iot_id, cloud_props, merged)

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
        dev = next((d for d in self.devices if d.get("iotId") == iot_id),
                   {"iotId": iot_id})
        try:
            cloud_props = await self.hass.async_add_executor_job(
                self._read_props, dev
            )
            merged = self._merge_props(iot_id, cloud_props)
            self.data.setdefault("props", {})[iot_id] = merged
            self.async_update_listeners()
        except Exception as exc:
            _LOGGER.debug("Post-command device refresh failed for %s: %s", iot_id, exc)

    async def async_get_tsl(self, product_key: str, iot_id: str) -> dict:
        """Fetch (and cache) the TSL model for a product.

        Failures are not cached, so a transient error at startup does not
        disable TSL-based detection until the next restart.
        """
        async with self._tsl_lock:
            if self.tsl_models.get(product_key):
                return self.tsl_models[product_key]
            try:
                tsl = await self.hass.async_add_executor_job(
                    self.client.get_tsl, iot_id)
            except Exception as exc:
                _LOGGER.debug("TSL fetch failed for %s: %s", product_key, exc)
                return {}
            if tsl:
                self.tsl_models[product_key] = tsl
            return tsl or {}

    def _raise_if_auth_failed(self) -> None:
        """Hand a refused re-login to HA's re-authentication flow."""
        failure = getattr(self.client, "auth_failed", None)
        if isinstance(failure, Exception):
            raise ConfigEntryAuthFailed(
                f"AigoSmart login refused, re-authentication required: {failure}"
            ) from failure

    def _read_props(self, dev: dict) -> dict[str, Any]:
        """Device properties: over the LAN when enabled and possible, else cloud."""
        iot_id = dev["iotId"]
        if self.local_reads and hasattr(self.client, "get_properties_prefer_local"):
            tsl = self.tsl_models.get(dev.get("productKey", "")) or {}
            identifiers = [p.get("identifier") for p in tsl.get("properties", []) or []
                           if isinstance(p, dict) and p.get("identifier")]
            return self.client.get_properties_prefer_local(iot_id, identifiers)
        return self.client.get_properties(iot_id)

    async def _async_poll_device(self, dev: dict) -> dict[str, Any]:
        """Online check + property poll for one device (never raises)."""
        iot_id = dev["iotId"]
        try:
            online = await self.hass.async_add_executor_job(self._resolve_online, dev)
        except Exception as exc:  # one device must not fail the whole poll
            _LOGGER.debug("Online check failed for %s: %s", iot_id, exc)
            online = self.is_device_online(iot_id)
        if not online:
            # Offline: the cloud would only return the stale shadow from
            # before the device lost power — keep what we have and drop
            # optimistic holds so nothing is "settling" on reconnect.
            self._optimistic_locks.pop(iot_id, None)
            return dict(self.data.get("props", {}).get(iot_id, {}))
        try:
            cloud_props = await self.hass.async_add_executor_job(self._read_props, dev)
        except Exception as exc:
            fails = self._prop_fail_count.get(iot_id, 0) + 1
            self._prop_fail_count[iot_id] = fails
            _LOGGER.debug("Property poll failed (%d) for %s: %s",
                          fails, iot_id, exc)
            if fails >= PROP_FAIL_OFFLINE_THRESHOLD:
                # Device unreachable on consecutive polls — mark offline
                # right away instead of waiting for the status cooldown.
                self._online_cache[iot_id] = (False, time.time())
                self._optimistic_locks.pop(iot_id, None)
            return self._merge_props(iot_id, {})
        self._prop_fail_count[iot_id] = 0
        return self._merge_props(iot_id, cloud_props)

    async def _async_update_data(self) -> dict:
        self._raise_if_auth_failed()
        # Full device list every Nth poll, else properties only.
        # Session expiry is healed inside the client (re-login + retry).
        if self._poll_count % DEVICE_LIST_EVERY == 0:
            try:
                devices = await self.hass.async_add_executor_job(
                    self.client.list_devices)
            except Exception as exc:
                self._raise_if_auth_failed()
                raise UpdateFailed(f"AigoSmart device list failed: {exc}") from exc
            self._poll_count += 1
        else:
            self._poll_count += 1
            devices = self.data.get("devices", [])

        # Status + properties per device, several devices at a time
        semaphore = asyncio.Semaphore(POLL_CONCURRENCY)

        async def _poll(dev: dict) -> tuple[str, dict] | None:
            iot_id = dev.get("iotId")
            if not iot_id:
                return None
            async with semaphore:
                return iot_id, await self._async_poll_device(dev)

        results = await asyncio.gather(*(_poll(dev) for dev in devices))
        props: dict[str, dict] = dict(r for r in results if r is not None)

        # A per-device call may have hit the refused re-login
        self._raise_if_auth_failed()

        old_ids = {d.get("iotId") for d in self.data.get("devices", [])}
        new_ids = {d.get("iotId") for d in devices} - old_ids
        if new_ids:
            _LOGGER.info("AigoSmart autodiscovery: %d new device(s): %s",
                         len(new_ids), new_ids)
            self.hass.bus.async_fire(
                EVENT_DEVICES_CHANGED, {"new": list(new_ids), "iot_ids": list(new_ids)})

        return {"devices": devices, "props": props}
