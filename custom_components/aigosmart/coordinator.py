"""Data coordinator: polls cloud devices + properties and detects new devices.

Autodiscovery layer 1 (cloud):
  - Polls per-device properties every POLL_INTERVAL (30 s).
  - Refreshes the full device list every DEVICE_LIST_EVERY (10th poll = 5 min).
  - When new devices appear, listeners (platforms) add entities on the fly —
    no integration reload needed.
  - Lazily fetches and caches the TSL model per productKey (used by the light
    platform to resolve colour properties by shape).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AigoSmartApiClient

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 30           # seconds
DEVICE_LIST_EVERY = 10       # refresh device list every Nth poll (10*30s = 5 min)

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
        # data layout:
        #   {"devices": [ {device dict} ],
        #    "props":   { iotId: {prop: value} }}
        self.data = {"devices": [], "props": {}}
        # iotId -> { prop_name: (optimistic_value, hold_until_epoch) }
        self._optimistic_locks: dict[str, dict[str, tuple[Any, float]]] = {}

    @property
    def devices(self) -> list[dict]:
        return self.data.get("devices", [])

    @property
    def props(self) -> dict[str, dict]:
        return self.data.get("props", {})

    def async_set_optimistic_props(
        self, iot_id: str, items: dict[str, Any], hold_duration: float = 5.0
    ) -> None:
        """Optimistically record new properties and prevent them from being overwritten by stale polls."""
        import time

        now = time.time()
        hold_until = now + hold_duration

        props = self.data.setdefault("props", {}).setdefault(iot_id, {})
        locks = self._optimistic_locks.setdefault(iot_id, {})

        for k, v in items.items():
            props[k] = v
            locks[k] = (v, hold_until)

    def _merge_props(self, iot_id: str, cloud_props: dict[str, Any]) -> dict[str, Any]:
        """Merge freshly polled cloud properties with active optimistic holds."""
        import time

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
        try:
            # Full device list every Nth poll, else properties only
            if self._poll_count % DEVICE_LIST_EVERY == 0:
                devices = await self.hass.async_add_executor_job(
                    self.client.list_devices)
                self._poll_count += 1
            else:
                self._poll_count += 1
                devices = self.data.get("devices", [])

            props: dict[str, dict] = {}
            for dev in devices:
                iot_id = dev.get("iotId")
                if not iot_id:
                    continue
                try:
                    cloud_props = await self.hass.async_add_executor_job(
                        self.client.get_properties, iot_id)
                    props[iot_id] = self._merge_props(iot_id, cloud_props)
                except Exception as exc:  # one offline device must not kill the batch
                    _LOGGER.debug("Property poll failed for %s: %s", iot_id, exc)
                    props[iot_id] = self._merge_props(iot_id, {})

            old_ids = {d.get("iotId") for d in self.data.get("devices", [])}
            new_ids = {d.get("iotId") for d in devices} - old_ids
            if new_ids:
                _LOGGER.info("AigoSmart autodiscovery: %d new device(s): %s",
                             len(new_ids), new_ids)
                self.hass.bus.async_fire(
                    EVENT_DEVICES_CHANGED, {"new": list(new_ids), "iot_ids": list(new_ids)})

            return {"devices": devices, "props": props}
        except Exception as exc:
            raise UpdateFailed(f"AigoSmart update failed: {exc}") from exc
