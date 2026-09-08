"""Local ALCS (Alibaba Local Channel Service) client — CoAP over UDP.

Protocol (from APK decompilation of com.aliyun.linksdk.alcs.* and the open
Alibaba iotkit-embedded device SDK):

Discovery:
  Multicast POST to 224.0.1.187:5683, URI path /dev/core/service/dev
  Payload: {"id": <n>, "version": "1.0", "method": "core.service.dev",
            "params": {}}
  Devices answer unicast with productKey/deviceName/ip/port in params.

Auth (core.service.auth):
  The app obtains per-device accessKey/accessToken from the cloud
  (/alcs/device/accessInfo/get) and performs a CoAP auth handshake; the
  device derives a sessionKey. For many devices the auth payload format is:
  {"id": n, "version":"1.0", "method":"core.service.auth",
   "params":{"accessKey": ..., "accessToken": ...}}
  On success the session is established for 86400s and property
  get/set uses method thing.service.property.get/set (Alink JSON).

This module implements:
  * Multicast discovery
  * Unicast property set/get (works on devices that don't enforce ALCS auth)
  * Optional auth handshake when accessKey/accessToken are provided

NOTE: The auth/session crypto in the native libiotcommon.so uses
AES with keys derived from the accessKey/accessToken exchange; on some
firmwares unencrypted CoAP works after auth is provisioned by the cloud.
Local control is best-effort — the cloud path is the reliable fallback.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any

from . import const

# --- Minimal CoAP encoder/decoder (RFC 7252 subset) -----------------------

COAP_CONFIRMABLE = 0
COAP_NON_CONFIRMABLE = 1
COAP_ACK = 2
COAP_RESET = 3

CODE_POST = 2
CODE_CREATED = 65
CODE_DELETED = 66
CODE_VALID = 67
CODE_CHANGED = 68
CODE_CONTENT = 69
CODE_BAD_REQUEST = 128
CODE_UNAUTHORIZED = 129
CODE_FORBIDDEN = 131
CODE_NOT_FOUND = 132
CODE_METHOD_NOT_ALLOWED = 133
CODE_UNSUPPORTED = 134
CODE_INTERNAL_ERROR = 160

OPTION_CONTENT_FORMAT = 12
OPTION_URI_PATH = 11
OPTION_URI_QUERY = 15


def _encode_option(delta: int, value: bytes) -> bytes:
    out = bytearray()
    d = delta
    if d < 13:
        out.append(d << 4)
    elif d < 269:
        out.append(13 << 4)
        out.append(d - 13)
    else:
        out.append(14 << 4)
        out.append((d - 269) >> 8)
        out.append((d - 269) & 0xFF)
    if len(value) < 13:
        out[0] |= len(value)
    elif len(value) < 269:
        out[0] |= 13
        out.append(len(value) - 13)
    else:
        out[0] |= 14
        out.append((len(value) - 269) >> 8)
        out.append((len(value) - 269) & 0xFF)
    return bytes(out) + value


def build_coap(
    msg_type: int,
    code: int,
    msg_id: int,
    token: bytes,
    uri_path: str,
    payload: bytes,
    options: list[tuple[int, bytes]] | None = None,
) -> bytes:
    """Build a CoAP packet. options is a list of (delta_option_number, value)."""
    assert len(token) <= 8
    assert int(code) == code and int(msg_type) == msg_type and int(msg_id) == msg_id
    msg_type = int(msg_type)
    code = int(code)
    msg_id = int(msg_id)
    header = bytes([(msg_type << 6) | (len(token) << 4) | 1, code,
                    (msg_id >> 8) & 0xFF, msg_id & 0xFF]) + token

    opts = bytearray()
    last = 0
    all_opts: list[tuple[int, bytes]] = []
    for seg in uri_path.split("/"):
        if seg:
            all_opts.append((OPTION_URI_PATH, seg.encode()))
    if options:
        all_opts.extend(options)
    all_opts.append((OPTION_CONTENT_FORMAT, bytes([50])))  # application/json

    for num, val in all_opts:
        opts += _encode_option(num - last, val)
        last = num

    if payload:
        marker = b"\xff"
        return header + bytes(opts) + marker + payload
    return header + bytes(opts)


def parse_coap(data: bytes) -> dict[str, Any]:
    """Parse a CoAP packet into header fields, options, payload."""
    if len(data) < 4:
        raise ValueError("short packet")
    tkl = data[0] & 0x0F
    msg_type = (data[0] >> 6) & 0x03
    code = data[1]
    msg_id = struct.unpack(">H", data[2:4])[0]
    token = data[4:4 + tkl]
    pos = 4 + tkl

    options: list[tuple[int, bytes]] = []
    option_number = 0
    while pos < len(data):
        b = data[pos]
        if b == 0xFF:
            pos += 1
            break
        delta = (b >> 4) & 0x0F
        length = b & 0x0F
        pos += 1
        if delta == 13:
            delta = data[pos] + 13
            pos += 1
        elif delta == 14:
            delta = int.from_bytes(data[pos:pos + 2], "big") + 269
            pos += 2
        if length == 13:
            length = data[pos] + 13
            pos += 1
        elif length == 14:
            length = int.from_bytes(data[pos:pos + 2], "big") + 269
            pos += 2
        option_number += delta
        options.append((option_number, data[pos:pos + length]))
        pos += length

    payload = data[pos:]
    return {
        "type": msg_type,
        "code": code,
        "msg_id": msg_id,
        "token": token,
        "options": options,
        "payload": payload,
    }


def alink_request(msg_id: int, method: str, params: dict) -> bytes:
    return json.dumps(
        {"id": msg_id, "version": "1.0", "params": params, "method": method},
        separators=(",", ":"),
    ).encode("utf-8")


def alink_response_of(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))


# --- Discovery ------------------------------------------------------------

class DiscoveredDevice:
    def __init__(self, addr: str, port: int, info: dict):
        self.addr = addr
        self.port = port
        self.info = info  # params dict: productKey/deviceName/ip/port...

    @property
    def product_key(self) -> str:
        return self.info.get("productKey", "")

    @property
    def device_name(self) -> str:
        return self.info.get("deviceName", "")

    @property
    def dev_id(self) -> str:
        return self.product_key + self.device_name

    def __repr__(self):
        return f"<DiscoveredDevice {self.dev_id} at {self.addr}:{self.port}>"


def discover_alcs_devices(timeout: float = 3.0, attempts: int = 2) -> list[DiscoveredDevice]:
    """Multicast discovery of ALCS devices on the LAN."""
    found: dict[str, DiscoveredDevice] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))
    sock.settimeout(timeout)

    group = (const.ALCS_DISCOVERY_ADDR, const.ALCS_DISCOVERY_PORT)
    for attempt in range(attempts):
        mid = (int(time.time()) & 0xFFFF) + attempt
        payload = alink_request(mid, const.ALCS_METHOD_DISCOVERY, {})
        pkt = build_coap(COAP_NON_CONFIRMABLE, CODE_POST, mid, bytes(4), const.ALCS_DISCOVERY_TOPIC, payload)
        try:
            sock.sendto(pkt, group)
        except OSError:
            break
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            try:
                msg = parse_coap(data)
                body = alink_response_of(msg["payload"]) if msg["payload"] else {}
                params = body.get("params", body.get("data", {})) or {}
                # direct discovery responses carry productKey/deviceName
                if "productKey" in params or "deviceName" in params:
                    dev = DiscoveredDevice(addr[0], addr[1], params)
                    found[dev.dev_id] = dev
            except Exception:
                continue
    sock.close()
    return list(found.values())


# --- Per-device control ----------------------------------------------------

class AlcsDevice:
    """Unicast CoAP control of a discovered ALCS device."""

    def __init__(self, addr: str, port: int = const.ALCS_DEFAULT_PORT,
                 product_key: str = "", device_name: str = "",
                 access_key: str = "", access_token: str = ""):
        self.addr = addr
        self.port = port
        self.product_key = product_key
        self.device_name = device_name
        self.access_key = access_key
        self.access_token = access_token
        self._mid = int(time.time()) & 0xFFFF
        self._lock = threading.Lock()
        self._session_ok = False

    def _next_mid(self) -> int:
        with self._lock:
            self._mid = (self._mid + 1) & 0xFFFF
            return self._mid

    def _request(self, method: str, params: dict, uri: str, timeout: float = 3.0) -> dict:
        mid = self._next_mid()
        token = bytes(4)
        payload = alink_request(mid, method, params)
        pkt = build_coap(COAP_CONFIRMABLE, CODE_POST, mid, token, uri, payload)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(pkt, (self.addr, self.port))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    break
                msg = parse_coap(data)
                if msg["msg_id"] == mid and msg["payload"]:
                    return alink_response_of(msg["payload"])
            raise TimeoutError(f"ALCS: no response from {self.addr}:{self.port} for {method}")
        finally:
            sock.close()

    # -- auth ---------------------------------------------------------------

    def auth(self) -> dict:
        """core.service.auth with cloud-provisioned accessKey/accessToken."""
        params: dict[str, Any] = {"accessKey": self.access_key, "accessToken": self.access_token}
        rsp = self._request(const.ALCS_METHOD_AUTH, params, "/auth")
        if rsp.get("code", 200) == 200:
            self._session_ok = True
        return rsp

    # -- properties -----------------------------------------------------------

    def get_properties(self, identifiers: list[str] | None = None) -> dict:
        params: dict[str, Any] = {}
        if identifiers:
            params["items"] = identifiers
        rsp = self._request(const.ALCS_METHOD_PROPERTY_GET, params,
                            f"/thing/{self.product_key}/{self.device_name}/properties")
        return rsp.get("data", {})

    def set_properties(self, items: dict) -> dict:
        rsp = self._request(const.ALCS_METHOD_PROPERTY_SET, {"items": items},
                            f"/thing/{self.product_key}/{self.device_name}/properties")
        return rsp

    def invoke_service(self, identifier: str, args: dict | None = None) -> dict:
        return self._request(
            const.ALCS_METHOD_SERVICE_INVOKE + identifier,
            {"args": args or {}},
            f"/thing/{self.product_key}/{self.device_name}/service",
        )
