"""Serialized + debounced command sender for rapid consecutive changes.

Solves the rapid-change race:
  * HA calls turn_on() many times as the slider moves → each call MERGES its
    items into a pending dict (last value per property wins)
  * a single flush task sleeps DEBOUNCE, sends the merged state, then a short
    settle+verify confirms the cloud shadow caught up
  * while a command is in flight / settling, coordinator polls cannot
    overwrite the entity with the stale shadow (skip window)

Guarantees for the user: the entity always shows exactly what was last
requested, and the device receives one clean write containing the final
values — no out-of-order HTTP races (the old sync turn_on ran in the
thread pool, so concurrent POSTs could arrive out of order).
"""
from __future__ import annotations

import asyncio
import logging
import time

_LOGGER = logging.getLogger(__name__)

DEBOUNCE = 0.4            # seconds to wait for more slider events before sending
SETTLE = 0.9              # wait after write before verifying the shadow
SKIP_AFTER_CMD = 12.0     # ignore stale shadow for this long after a command
SKIP_AFTER_CONFIRM = 1.5  # shadow confirmed the write -> polls may resume sooner
MAX_ATTEMPTS = 3


class AigoCommander:
    """One per entity. Merges rapid writes into ordered, verified flushes."""

    def __init__(self, hass, client, iot_id: str) -> None:
        self.hass = hass
        self._client = client
        self._iot_id = iot_id
        self._pending: dict = {}
        self._attempts = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.skip_until: float = 0.0

    # -- public ---------------------------------------------------------------

    def in_skip(self) -> bool:
        """True while the last command may not yet be reflected in the shadow."""
        return time.monotonic() < self.skip_until

    async def async_send(self, items: dict) -> None:
        """Queue a property write. Consecutive calls coalesce; last wins."""
        async with self._lock:
            self._pending.update(items)
            self._attempts = 0
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())

    async def async_flush(self) -> None:
        """Force-send anything pending immediately (used on unload)."""
        async with self._lock:
            if self._pending and (self._task is None or self._task.done()):
                self._task = asyncio.create_task(self._run())
        if self._task:
            try:
                await self._task
            except Exception:
                pass

    # -- internals -------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(DEBOUNCE)
            async with self._lock:
                if not self._pending:
                    return
                items = self._pending
                self._pending = {}
            try:
                await self.hass.async_add_executor_job(
                    self._client.set_properties_prefer_local, self._iot_id, items)
            except Exception as exc:
                self._attempts += 1
                _LOGGER.warning("AigoSmart write failed (%s/3) for %s: %s",
                                self._attempts, self._iot_id, exc)
                if self._attempts >= MAX_ATTEMPTS:
                    _LOGGER.error("AigoSmart giving up on batch for %s: %s",
                                  self._iot_id, items)
                    self._attempts = 0
                    continue
                async with self._lock:
                    self._pending.update(items)
                await asyncio.sleep(DEBOUNCE * 4)
                continue
            self._attempts = 0
            # The shadow can still hold the PREVIOUS value for a moment after
            # a successful write — open the skip window immediately so no
            # stale poll can overwrite the optimistic state.
            self.skip_until = time.monotonic() + SKIP_AFTER_CMD
            # Best-effort early confirmation: once the shadow reflects the
            # write, polls may resume.
            try:
                await asyncio.sleep(SETTLE)
                props = await self.hass.async_add_executor_job(
                    self._client.get_properties_prefer_local, self._iot_id)
            except Exception:
                continue
            if props and _matches(props, items):
                self.skip_until = time.monotonic() + SKIP_AFTER_CONFIRM


def _matches(props: dict, items: dict) -> bool:
    """True when every commanded property is already visible in the shadow."""
    for key, want in items.items():
        if want is None:
            continue
        got = props.get(key)
        if isinstance(want, dict):
            if not isinstance(got, dict):
                return False
            for member, mval in want.items():
                if mval is None:
                    continue
                try:
                    if int(float(got.get(member, -1))) != int(float(mval)):
                        return False
                except (TypeError, ValueError):
                    return False
        else:
            try:
                if int(float(got)) != int(float(want)):
                    return False
            except (TypeError, ValueError):
                if got != want:
                    return False
    return True


def get_commander(state: dict, iot_id: str) -> AigoCommander:
    """Lazily create/fetch the shared commander for a device."""
    commanders = state.setdefault("commanders", {})
    if iot_id not in commanders:
        commanders[iot_id] = AigoCommander(state["hass"], state["client"], iot_id)
    return commanders[iot_id]
