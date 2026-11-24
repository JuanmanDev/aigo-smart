"""Background ALCS listener — passive LAN autodiscovery (layer 2).

Devices on Alibaba's platform periodically announce themselves (or respond to
our multicast probes) via CoAP on 224.0.1.187:5683 with method core.service.dev.

This module runs a daemon thread in the executor that:
  * sends periodic multicast discovery probes
  * listens for replies/announcements
  * deduplicates and fires a Home Assistant event for each newly seen device

The frontend (config flow / device registry) can then show these devices
under "Discovered" on the HA dashboard.
"""
from __future__ import annotations

import logging
import struct
import threading

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

_LOGGER = logging.getLogger(__name__)

SIGNAL_ALCS_DEVICE_FOUND = "aigosmart_alcs_device_found"

PROBE_INTERVAL = 60  # seconds between multicast probes
SOCKET_TIMEOUT = 20  # listen window per probe cycle


class AlcsListener:
    """Threaded ALCS multicast listener (one per HA instance)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aigosmart-alcs",
                                         daemon=True)
        self._thread.start()
        _LOGGER.info("AigoSmart ALCS listener started")

    def stop(self) -> None:
        self._stop.set()
        # thread exits on next timeout cycle (<= SOCKET_TIMEOUT s)

    # ------------------------------------------------------------------

    def _run(self) -> None:
        import socket
        import time

        from .lib import const
        from .lib.local import discovery_packet

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # join multicast group so we also receive spontaneous announcements
            mreq = struct.pack("4sl", socket.inet_aton(const.ALCS_DISCOVERY_ADDR),
                               socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            _LOGGER.debug("Cannot join multicast group: %s", exc)
        sock.settimeout(SOCKET_TIMEOUT)

        group = (const.ALCS_DISCOVERY_ADDR, const.ALCS_DISCOVERY_PORT)
        mid = int(time.time()) & 0xFFFF

        while not self._stop.is_set():
            # probe
            mid = (mid + 1) & 0xFFFF
            try:
                sock.sendto(discovery_packet(mid), group)
            except OSError as exc:
                _LOGGER.debug("ALCS probe failed: %s", exc)

            # listen window
            deadline = time.time() + PROBE_INTERVAL - SOCKET_TIMEOUT
            while time.time() < deadline and not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    break
                self._handle_packet(data, addr)

        sock.close()
        _LOGGER.info("AigoSmart ALCS listener stopped")

    def _handle_packet(self, data: bytes, addr: tuple) -> None:
        from .lib.local import (
            alink_response_of,
            parse_coap,
            parse_discovery,
            remember_lan_device,
        )

        try:
            msg = parse_coap(data)
            if not msg["payload"]:
                return
            body = alink_response_of(msg["payload"])
        except (ValueError, UnicodeDecodeError):
            return

        for dev in parse_discovery(body, addr[0], addr[1]):
            # local control looks devices up here (no extra multicast)
            remember_lan_device(dev)
            with self._lock:
                if dev.dev_id in self._seen:
                    continue
                self._seen.add(dev.dev_id)
            info = {
                "addr": dev.addr,
                "port": dev.port,
                "productKey": dev.product_key,
                "deviceName": dev.device_name,
            }
            _LOGGER.debug("ALCS discovery: productKey %s answered on the LAN",
                          dev.product_key)
            # dispatch into HA loop
            self.hass.loop.call_soon_threadsafe(self._fire, info)

    @callback
    def _fire(self, info: dict) -> None:
        self.hass.bus.async_fire("aigosmart_alcs_found", info)
        async_dispatcher_send(self.hass, SIGNAL_ALCS_DEVICE_FOUND, info)


_LISTENER: AlcsListener | None = None


async def async_start_listener(hass: HomeAssistant) -> AlcsListener:
    global _LISTENER
    if _LISTENER is None:
        _LISTENER = AlcsListener(hass)
        await hass.async_add_executor_job(_LISTENER.start)
    return _LISTENER


async def async_stop_listener(hass: HomeAssistant) -> None:
    global _LISTENER
    if _LISTENER is not None:
        await hass.async_add_executor_job(_LISTENER.stop)
        _LISTENER = None
