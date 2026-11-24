"""Local ALCS (Alibaba Local Channel Service) client — CoAP over UDP.

Protocol as implemented by Alibaba's open-source device SDK
(aliyun/iotkit-embedded v3, src/dev_model/alcs/*): the device is the ALCS
server, the app (and this module) is the client.

Discovery (no auth):
  NON POST to multicast 224.0.1.187:5683, path /dev/core/service/dev,
  method "core.service.dev". Devices answer unicast with
  {"id":..,"code":200,"data":{"devices":{"addr":ip,"port":5683,
   "pal":"linkkit-ica","profile":[{"productKey":..,"deviceName":..}]}}}

Auth (plain JSON):
  CON POST /dev/{pk}/{dn}/core/service/auth, method "core/service/auth",
  params {prodKey, deviceName, encrypt:"payload", randomKey: R_c,
          sign: base64(HMAC-SHA1(accessToken, R_c)), accessKey}
  -> data {sign, randomKey: R_d, sessionId, expire}
  sessionKey = hex(HMAC-SHA1(accessToken, R_c + R_d)); the AES key is its
  first 16 characters. accessKey/accessToken come from the cloud
  (/alcs/device/accessInfo/get).

Secure requests:
  CON POST /sys/{pk}/{dn}/thing/service/property/{get,set} with options
  Content-Format 42 (octet-stream) and SessionId (option 71). The Alink JSON
  payload is AES-128-CBC encrypted with IV "a1b1c1d1e1f1g1h1": the full
  blocks are chained, the padded last block is encrypted on its own with the
  IV again (alcs_encrypt / alcs_decrypt in the SDK). Responses are encrypted
  the same way.

Local control is best-effort: anything unexpected raises, and callers fall
back to the cloud.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import string
import struct
import threading
import time
from typing import Any

from . import const

# --- Minimal CoAP encoder/decoder (RFC 7252 subset) -----------------------

COAP_VERSION = 1

COAP_CONFIRMABLE = 0
COAP_NON_CONFIRMABLE = 1
COAP_ACK = 2
COAP_RESET = 3

CODE_EMPTY = 0
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

OPTION_URI_PATH = 11
OPTION_CONTENT_FORMAT = 12
OPTION_URI_QUERY = 15
OPTION_SESSION_ID = 71        # ALCS private option (COAP_OPTION_SESSIONID)

CONTENT_OCTET_STREAM = 42
CONTENT_JSON = 50


def _encode_option(delta: int, value: bytes) -> bytes:
    out = bytearray([0])
    if delta < 13:
        out[0] = delta << 4
    elif delta < 269:
        out[0] = 13 << 4
        out.append(delta - 13)
    else:
        out[0] = 14 << 4
        out += (delta - 269).to_bytes(2, "big")
    length = len(value)
    if length < 13:
        out[0] |= length
    elif length < 269:
        out[0] |= 13
        out.append(length - 13)
    else:
        out[0] |= 14
        out += (length - 269).to_bytes(2, "big")
    return bytes(out) + value


def uint_option(value: int) -> bytes:
    """CoAP uint option value: minimal big-endian bytes (0 -> empty)."""
    value = int(value)
    if value <= 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def build_coap(
    msg_type: int,
    code: int,
    msg_id: int,
    token: bytes,
    uri_path: str,
    payload: bytes,
    options: list[tuple[int, bytes]] | None = None,
    content_format: int | None = CONTENT_JSON,
) -> bytes:
    """Build a CoAP packet. ``options`` are (option_number, value) pairs."""
    if len(token) > 8:
        raise ValueError("CoAP token longer than 8 bytes")
    msg_id = int(msg_id) & 0xFFFF
    # Ver (2 bits) | Type (2 bits) | Token length (4 bits)
    first = (COAP_VERSION << 6) | ((int(msg_type) & 0x3) << 4) | len(token)
    header = bytes([first, int(code) & 0xFF]) + msg_id.to_bytes(2, "big") + token

    all_opts: list[tuple[int, bytes]] = [
        (OPTION_URI_PATH, seg.encode()) for seg in uri_path.split("/") if seg]
    if content_format is not None:
        all_opts.append((OPTION_CONTENT_FORMAT, uint_option(content_format)))
    if options:
        all_opts.extend(options)
    # deltas must be non-negative: sort by option number (stable, so the
    # Uri-Path segments keep their order)
    all_opts.sort(key=lambda opt: opt[0])

    encoded = bytearray()
    last = 0
    for number, value in all_opts:
        encoded += _encode_option(number - last, value)
        last = number

    if payload:
        return header + bytes(encoded) + b"\xff" + payload
    return header + bytes(encoded)


def parse_coap(data: bytes) -> dict[str, Any]:
    """Parse a CoAP packet into header fields, options, payload."""
    if len(data) < 4:
        raise ValueError("short packet")
    version = data[0] >> 6
    if version != COAP_VERSION:
        raise ValueError(f"not a CoAP v1 packet (version {version})")
    msg_type = (data[0] >> 4) & 0x03
    tkl = data[0] & 0x0F
    if tkl > 8 or len(data) < 4 + tkl:
        raise ValueError("bad token length")
    code = data[1]
    msg_id = struct.unpack(">H", data[2:4])[0]
    token = data[4:4 + tkl]
    pos = 4 + tkl

    options: list[tuple[int, bytes]] = []
    number = 0
    payload = b""
    while pos < len(data):
        byte = data[pos]
        if byte == 0xFF:
            payload = data[pos + 1:]
            break
        delta = (byte >> 4) & 0x0F
        length = byte & 0x0F
        pos += 1
        if delta == 13:
            delta = data[pos] + 13
            pos += 1
        elif delta == 14:
            delta = int.from_bytes(data[pos:pos + 2], "big") + 269
            pos += 2
        elif delta == 15:
            raise ValueError("bad option delta")
        if length == 13:
            length = data[pos] + 13
            pos += 1
        elif length == 14:
            length = int.from_bytes(data[pos:pos + 2], "big") + 269
            pos += 2
        elif length == 15:
            raise ValueError("bad option length")
        number += delta
        options.append((number, data[pos:pos + length]))
        pos += length

    return {
        "type": msg_type,
        "code": code,
        "msg_id": msg_id,
        "token": token,
        "options": options,
        "payload": payload,
    }


def alink_request(msg_id: int, method: str, params: Any) -> bytes:
    return json.dumps(
        {"id": msg_id, "version": "1.0", "params": params, "method": method},
        separators=(",", ":"),
    ).encode("utf-8")


def alink_response_of(data: bytes) -> dict:
    body = json.loads(data.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("ALCS: answer is not a JSON object")
    return body


# --- Crypto -----------------------------------------------------------------

ALCS_IV = b"a1b1c1d1e1f1g1h1"
RANDOM_KEY_LEN = 16


def _aes_cbc(key: bytes, data: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(ALCS_IV))
    ctx = cipher.encryptor() if encrypt else cipher.decryptor()
    return ctx.update(data) + ctx.finalize()


def alcs_encrypt(data: bytes, key: bytes) -> bytes:
    """Encrypt like the SDK: chained full blocks + a separately encrypted pad block."""
    full = len(data) & ~0xF
    out = _aes_cbc(key, data[:full], True) if full else b""
    rest = data[full:]
    pad = 16 - len(rest)
    return out + _aes_cbc(key, rest + bytes([pad]) * pad, True)


def alcs_decrypt(data: bytes, key: bytes) -> bytes:
    """Inverse of alcs_encrypt (the last block is decrypted on its own)."""
    if not data or len(data) % 16:
        raise ValueError(f"ALCS: encrypted payload has bad length {len(data)}")
    last = len(data) - 16
    out = _aes_cbc(key, data[:last], False) if last else b""
    out += _aes_cbc(key, data[last:], False)
    pad = out[-1]
    if not 1 <= pad <= 16:
        raise ValueError("ALCS: bad padding (wrong session key?)")
    return out[:-pad]


def random_key(length: int = RANDOM_KEY_LEN) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def auth_sign(access_token: str, client_random: str) -> str:
    """base64(HMAC-SHA1(accessToken, randomKey)), as the device verifies it."""
    digest = hmac.new(access_token.encode(), client_random.encode(),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def session_key(access_token: str, client_random: str, device_random: str) -> bytes:
    """AES key: first 16 chars of hex(HMAC-SHA1(token, R_client + R_device))."""
    hexdigest = hmac.new(access_token.encode(),
                         (client_random + device_random).encode(),
                         hashlib.sha1).hexdigest()
    return hexdigest[:16].encode()


class AlcsError(Exception):
    """The local channel refused or did not understand a request."""


def _code_of(rsp: dict) -> int:
    try:
        return int(rsp.get("code", 0))
    except (TypeError, ValueError):
        return 0


# --- Discovery ------------------------------------------------------------

class DiscoveredDevice:
    def __init__(self, addr: str, port: int, info: dict):
        self.addr = addr
        self.port = port
        self.info = info  # productKey / deviceName (+ whatever else was sent)

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


# productKey+deviceName -> (addr, port, monotonic time seen). Fed by every
# discovery sweep and by the background listener, so control does not need a
# multicast round-trip of its own.
_LAN_SEEN: dict[str, tuple[str, int, float]] = {}
_LAN_LOCK = threading.Lock()
LAN_SEEN_TTL = 900


def remember_lan_device(dev: DiscoveredDevice) -> None:
    with _LAN_LOCK:
        _LAN_SEEN[dev.dev_id] = (dev.addr, dev.port, time.monotonic())


def forget_lan_device(product_key: str, device_name: str) -> None:
    with _LAN_LOCK:
        _LAN_SEEN.pop(product_key + device_name, None)


def known_lan_address(product_key: str, device_name: str) -> tuple[str, int] | None:
    with _LAN_LOCK:
        seen = _LAN_SEEN.get(product_key + device_name)
    if seen and time.monotonic() - seen[2] < LAN_SEEN_TTL:
        return seen[0], seen[1]
    return None


def _port_of(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return const.ALCS_DEFAULT_PORT
    return port if 0 < port < 65536 else const.ALCS_DEFAULT_PORT


def parse_discovery(body: dict, src_addr: str, src_port: int) -> list[DiscoveredDevice]:
    """Devices announced in one discovery answer (SDK v3 and flat formats)."""
    found: list[DiscoveredDevice] = []
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    devices = data.get("devices")
    if isinstance(devices, dict):
        # the answer comes from the device itself: prefer its source address
        # over the "addr" it reports (that can be 0.0.0.0 or a stale IP)
        addr = src_addr or devices.get("addr", "")
        port = _port_of(devices.get("port"))
        profiles = devices.get("profile") or []
        if isinstance(profiles, dict):
            profiles = [profiles]
        for prof in profiles:
            if isinstance(prof, dict) and prof.get("productKey") and prof.get("deviceName"):
                info = {"productKey": str(prof["productKey"]),
                        "deviceName": str(prof["deviceName"]),
                        "pal": str(devices.get("pal", ""))}
                found.append(DiscoveredDevice(addr, port, info))
        return found
    # older / flat answers: productKey + deviceName directly in params or data
    for params in (body.get("params"), data, body):
        if isinstance(params, dict) and params.get("productKey") and params.get("deviceName"):
            addr = src_addr or params.get("addr") or params.get("ip") or ""
            found.append(DiscoveredDevice(addr, _port_of(params.get("port")),
                                          dict(params)))
            break
    return found


def discovery_packet(msg_id: int) -> bytes:
    return build_coap(COAP_NON_CONFIRMABLE, CODE_POST, msg_id, os.urandom(4),
                      const.ALCS_DISCOVERY_TOPIC,
                      alink_request(msg_id, const.ALCS_METHOD_DISCOVERY, {}))


def discover_alcs_devices(timeout: float = 3.0, attempts: int = 2) -> list[DiscoveredDevice]:
    """Multicast discovery of ALCS devices on the LAN."""
    found: dict[str, DiscoveredDevice] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    except OSError:
        pass
    sock.bind(("", 0))
    sock.settimeout(timeout)

    group = (const.ALCS_DISCOVERY_ADDR, const.ALCS_DISCOVERY_PORT)
    try:
        for attempt in range(attempts):
            mid = (int(time.time()) + attempt) & 0xFFFF
            try:
                sock.sendto(discovery_packet(mid), group)
            except OSError:
                break
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except (socket.timeout, OSError):
                    break
                try:
                    msg = parse_coap(data)
                    body = alink_response_of(msg["payload"]) if msg["payload"] else {}
                except ValueError:
                    continue
                for dev in parse_discovery(body, addr[0], addr[1]):
                    found[dev.dev_id] = dev
                    remember_lan_device(dev)
    finally:
        sock.close()
    return list(found.values())


# --- Per-device control ----------------------------------------------------

class AlcsDevice:
    """Authenticated, encrypted CoAP session with one device."""

    def __init__(self, addr: str, port: int = const.ALCS_DEFAULT_PORT,
                 product_key: str = "", device_name: str = "",
                 access_key: str = "", access_token: str = "",
                 timeout: float = 2.0):
        self.addr = addr
        self.port = port
        self.product_key = product_key
        self.device_name = device_name
        self.access_key = access_key
        self.access_token = access_token
        self.timeout = timeout
        self._mid = int.from_bytes(os.urandom(2), "big")
        self._seq = 0
        self._lock = threading.Lock()
        self._key: bytes | None = None
        self._session_id: int | None = None
        self._expires = 0.0

    # -- transport ----------------------------------------------------------

    def _next_ids(self) -> tuple[int, bytes, int]:
        with self._lock:
            self._mid = (self._mid + 1) & 0xFFFF
            self._seq += 1
            return self._mid, os.urandom(4), self._seq

    def _exchange(self, pkt: bytes, mid: int, token: bytes) -> dict:
        """Send a CON request; wait for its piggybacked or separate response."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(pkt, (self.addr, self.port))
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    data, src = sock.recvfrom(4096)
                except (socket.timeout, OSError):
                    break
                try:
                    msg = parse_coap(data)
                except ValueError:
                    continue
                if msg["type"] == COAP_RESET and msg["msg_id"] == mid:
                    raise AlcsError("device reset the request")
                if msg["token"] != token:
                    continue
                if msg["type"] == COAP_CONFIRMABLE:
                    # separate response: acknowledge it
                    ack = bytes([(COAP_VERSION << 6) | (COAP_ACK << 4), CODE_EMPTY])
                    try:
                        sock.sendto(ack + msg["msg_id"].to_bytes(2, "big"), src)
                    except OSError:
                        pass
                return msg
            raise TimeoutError("ALCS: no answer from the device")
        finally:
            sock.close()

    # -- auth ---------------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        return self._key is not None and time.monotonic() < self._expires

    def auth(self) -> dict:
        """Open a session (core/service/auth) with the cloud-issued keys."""
        if not (self.access_key and self.access_token):
            raise AlcsError("no local access key for this device")
        client_random = random_key()
        mid, token, seq = self._next_ids()
        body = {
            "version": "1.0",
            "method": "core/service/auth",
            "id": seq,
            "params": {
                "prodKey": self.product_key,
                "deviceName": self.device_name,
                "encrypt": "payload",
                "randomKey": client_random,
                "sign": auth_sign(self.access_token, client_random),
                "accessKey": self.access_key,
            },
        }
        pkt = build_coap(COAP_CONFIRMABLE, CODE_POST, mid, token,
                         f"/dev/{self.product_key}/{self.device_name}/core/service/auth",
                         json.dumps(body, separators=(",", ":")).encode())
        msg = self._exchange(pkt, mid, token)
        if not msg["payload"]:
            raise AlcsError(f"auth: empty answer (CoAP code {msg['code']})")
        try:
            rsp = alink_response_of(msg["payload"])
        except ValueError as exc:
            raise AlcsError(f"auth: unreadable answer ({exc})") from exc
        code = _code_of(rsp)
        if code != 200:
            raise AlcsError(f"auth refused by the device (code {code})")
        data = rsp.get("data") if isinstance(rsp.get("data"), dict) else {}
        device_random = data.get("randomKey")
        session_id = data.get("sessionId")
        if not device_random or session_id is None:
            raise AlcsError("auth: answer without randomKey / sessionId")
        try:
            expire = int(data.get("expire", const.ALCS_SESSION_VALID_TIME))
        except (TypeError, ValueError):
            expire = const.ALCS_SESSION_VALID_TIME
        self._key = session_key(self.access_token, client_random, str(device_random))
        self._session_id = int(session_id)
        # renew well before the device drops the session
        self._expires = time.monotonic() + max(60, expire - 300)
        return rsp

    def _secure_request(self, path: str, method: str, params: Any,
                        retry_auth: bool = True) -> dict:
        if not self.authenticated:
            self.auth()
        key, session_id = self._key, self._session_id
        assert key is not None and session_id is not None
        mid, token, seq = self._next_ids()
        plain = json.dumps({"id": str(seq), "version": "1.0", "params": params,
                            "method": method}, separators=(",", ":")).encode()
        pkt = build_coap(
            COAP_CONFIRMABLE, CODE_POST, mid, token, path,
            alcs_encrypt(plain, key),
            options=[(OPTION_SESSION_ID, uint_option(session_id))],
            content_format=CONTENT_OCTET_STREAM)
        msg = self._exchange(pkt, mid, token)
        if msg["code"] in (CODE_UNAUTHORIZED, CODE_FORBIDDEN) and retry_auth:
            self._key = None          # the device dropped the session
            return self._secure_request(path, method, params, retry_auth=False)
        if not msg["payload"]:
            raise AlcsError(f"{method}: empty answer (CoAP code {msg['code']})")
        try:
            rsp = alink_response_of(alcs_decrypt(msg["payload"], key))
        except (ValueError, UnicodeDecodeError) as exc:
            if retry_auth:
                self._key = None
                return self._secure_request(path, method, params, retry_auth=False)
            raise AlcsError(f"{method}: cannot decrypt the answer ({exc})") from exc
        code = _code_of(rsp)
        if code != 200:
            raise AlcsError(f"{method} refused by the device (code {code})")
        return rsp

    def _thing_path(self, suffix: str) -> str:
        return f"/sys/{self.product_key}/{self.device_name}/thing/{suffix}"

    # -- properties -----------------------------------------------------------

    def get_properties(self, identifiers: list[str] | None = None) -> dict:
        """Current values straight from the device: {identifier: value}."""
        rsp = self._secure_request(self._thing_path("service/property/get"),
                                   const.ALCS_METHOD_PROPERTY_GET,
                                   list(identifiers or []))
        data = rsp.get("data")
        if not isinstance(data, dict):
            return {}
        # some firmwares answer {"prop": {"value": v, "time": t}}
        return {k: (v["value"] if isinstance(v, dict) and "value" in v else v)
                for k, v in data.items()}

    def set_properties(self, items: dict) -> dict:
        return self._secure_request(self._thing_path("service/property/set"),
                                    const.ALCS_METHOD_PROPERTY_SET, dict(items))

    def invoke_service(self, identifier: str, args: dict | None = None) -> dict:
        return self._secure_request(self._thing_path(f"service/{identifier}"),
                                    const.ALCS_METHOD_SERVICE_INVOKE + identifier,
                                    args or {})
