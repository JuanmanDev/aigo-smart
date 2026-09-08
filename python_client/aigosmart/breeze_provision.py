"""Breeze BLE provisioning client — adds AigoSmart devices from Home Assistant.

Protocol (reverse-engineered from AigoSmart APK 2.15.6, com.aliyun.iot.breeze):
ported to bleak so it runs on:
  * HA's Bluetooth integration (including ESPHome bluetooth_proxy remotes)
  * any machine with a BLE adapter (via aigo_cli.py bleprovision)

Flow:
  1. SCAN: devices in pairing mode advertise service 0xFEB3 with manufacturer
     data (id 0x0819) containing the BreezeScanRecord:
       byte0      version (low nibble) | subType (high nibble)
       byte1      FMSK flags (bit5 = secure broadcast -> sign+seq present)
       bytes2-5   MID / productId (LE, 2 bytes if version<4 else 4)
       next 6     MAC
       [secure]   4 bytes sign + 4 bytes seq (LE)
       rest       extra
  2. CONNECT to FEB3, subscribe to FED8 (notify)
  3. AUTH-INFO (cmd 13 SYS): send TLV list [2 (productKey), 3 (deviceName),
     4 (random), 5 (sign)] with empty values -> device replies its PK/DN/random/sign
  4. CLOUD BIND: POST /awss/ble/user/bind with {deviceName, productId=MIDhex,
     sign, signMethod=sha256, signParams={clientId=MIDhex.upper(), random}}
     -> cloud returns deviceSecret (iotAuth)
  5. WIFI CONFIG: write WiFi credentials, device joins AP, gets iotId
  6. Device appears in list_devices -> autodiscovery picks it up

TLV types (com.aliyun.iot.breeze.TLV):
  1=SDK_VERSION 2=PRODUCT_KEY 3=DEVICE_NAME 4=RANDOM 5=SIGN
  7=UPDATE_SEQ 9=CHECK_SECURITY 10=UPDATE_DEVICE_SECRET 11=UPDATE_AUTHCODE_SECRET
  12=CHECK_SIGN
Message framing: BreezeMessage(cmd, payload) -> PDUs written to FED5.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import struct
from typing import Any, Callable

_LOG = logging.getLogger(__name__)

# --- UUIDs (com.aliyun.iot.breeze.BreezeUuid) -------------------------------
BREEZE_SERVICE = "0000feb3-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "0000fed5-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000fed8-0000-1000-8000-00805f9b34fb"
CHAR_WRITE_NO_RSP = "0000fed7-0000-1000-8000-00805f9b34fb"

# --- TLV types ---------------------------------------------------------------
TLV_SDK_VERSION = 1
TLV_PRODUCT_KEY = 2
TLV_DEVICE_NAME = 3
TLV_RANDOM = 4
TLV_SIGN = 5
TLV_UPDATE_SEQ = 7
TLV_CHECK_SECURITY = 9
TLV_UPDATE_DEVICE_SECRET = 10
TLV_UPDATE_AUTHCODE_SECRET = 11
TLV_CHECK_SIGN = 12

# --- Breeze commands -----------------------------------------------------------
CMD_SYS = 13  # BreezeHelper.SYS_CMD (TLV transport for provisioning)


def parse_scan_record(mfr_data: bytes) -> dict | None:
    """Parse the Breeze scan record from manufacturer data (id 0x0819)."""
    if not mfr_data or len(mfr_data) < 10:
        return None
    version = mfr_data[0] & 0x0F
    if version < 3:
        return None
    sub_type = (mfr_data[0] >> 4) & 0x0F if version >= 4 else 0
    fmsk = mfr_data[1]
    pos = 2
    if version >= 4:
        mid = int.from_bytes(mfr_data[pos:pos + 4], "little")
        pos += 4
    else:
        mid = mfr_data[pos] | (mfr_data[pos + 1] << 8)
        pos += 2
    if pos + 6 > len(mfr_data):
        return None
    mac = mfr_data[pos:pos + 6]
    pos += 6
    secure = bool(fmsk & 0x20)
    sign = b""
    seq = 0
    if secure and pos + 8 <= len(mfr_data):
        sign = mfr_data[pos:pos + 4]
        pos += 4
        seq = int.from_bytes(mfr_data[pos:pos + 4], "little")
        pos += 4
    extra = mfr_data[pos:]
    return {
        "version": version,
        "sub_type": sub_type,
        "fmsk": fmsk,
        "mid": mid,                       # productId
        "mid_hex": f"{mid:02x}",
        "mac": mac.hex(":"),
        "secure": secure,
        "sign": sign.hex(),
        "seq": seq,
        "extra": extra.hex(),
        "bind_flag_supported": secure,
    }


def tlv_encode(elements: list[tuple[int, bytes]]) -> bytes:
    out = bytearray()
    for t, v in elements:
        out.append(t)
        out.append(len(v))
        out += v
    return bytes(out)


def tlv_parse(data: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    pos = 0
    while pos + 2 <= len(data):
        t = data[pos]
        l = data[pos + 1]
        if pos + 2 + l > len(data):
            break
        out[t] = data[pos + 2:pos + 2 + l]
        pos += 2 + l
    return out


def _breeze_message(cmd: int, payload: bytes) -> bytes:
    """Wrap a payload in a Breeze message header: cmd(1) | flags(1) | len(2 BE).

    Simple single-PDU framing used for short provisioning messages
    (matches BreezeMessage -> BreezePdu fragmentation for len < MTU-4).
    """
    if len(payload) + 4 > 20:  # would need fragmentation; keep simple profiles
        raise ValueError("payload too large for single PDU; not supported yet")
    return bytes([cmd & 0xFF, 0x00]) + struct.pack(">H", len(payload)) + payload


def _parse_breeze_response(data: bytes) -> tuple[int, bytes] | None:
    """Parse cmd + payload back out of a notification."""
    if len(data) < 4:
        return None
    cmd = data[0]
    flags = data[1]
    length = struct.unpack(">H", data[2:4])[0]
    if flags & 0x01 or len(data) - 4 < length:
        return None  # fragmented — unsupported in this simple client
    return cmd, data[4:4 + length]


class BreezeProvisioner:
    """Provision a Breeze pairing-mode device over BLE using bleak."""

    def __init__(self, address: str, cloud_bind_fn: Callable[[dict], dict]):
        """
        address: BLE MAC of the pairing device
        cloud_bind_fn: async callable(device_info) -> cloud response;
                       implemented by the HA integration / CLI using
                       /awss/ble/user/bind
        """
        self.address = address
        self.cloud_bind = cloud_bind_fn
        self._client = None
        self._notify_evt = asyncio.Event()
        self._last_payload: bytes = b""

    async def _on_notify(self, _char, data: bytearray) -> None:
        self._last_payload = bytes(data)
        self._notify_evt.set()

    async def _connect(self) -> None:
        from bleak import BleakClient

        self._client = BleakClient(self.address)
        await self._client.connect()
        await self._client.start_notify(CHAR_NOTIFY, self._on_notify)

    async def _disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    async def _send_sys_tlv(self, tlvs: list[tuple[int, bytes]], timeout: float = 8.0) -> dict[int, bytes]:
        """Send a SYS (13) message with TLV payload, await the TLV reply."""
        pkt = _breeze_message(CMD_SYS, tlv_encode(tlvs))
        self._notify_evt.clear()
        self._last_payload = b""
        await self._client.write_gatt_char(CHAR_WRITE, pkt, response=True)

        try:
            await asyncio.wait_for(self._notify_evt.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("device did not answer the TLV request")

        parsed = _parse_breeze_response(self._last_payload)
        if parsed is None or parsed[0] != CMD_SYS:
            raise RuntimeError(f"unexpected BLE response: {self._last_payload.hex()}")
        return tlv_parse(parsed[1])

    # ------------------------------------------------------------------
    # Public flow
    # ------------------------------------------------------------------

    async def get_device_info(self) -> dict:
        """Step 1: ask the device for PK/DN/random/sign (empty TLV request)."""
        await self._connect()
        try:
            reply = await self._send_sys_tlv([
                (TLV_PRODUCT_KEY, b""),
                (TLV_DEVICE_NAME, b""),
                (TLV_RANDOM, b""),
                (TLV_SIGN, b""),
            ])
        except Exception:
            await self._disconnect()
            raise
        info = {
            "productKey": reply.get(TLV_PRODUCT_KEY, b"").decode("utf-8", "replace"),
            "deviceName": reply.get(TLV_DEVICE_NAME, b"").decode("utf-8", "replace"),
            "random": reply.get(TLV_RANDOM, b"").hex(),
            "sign": reply.get(TLV_SIGN, b"").hex(),
        }
        return info

    async def provision(self, wifi_ssid: str, wifi_password: str) -> dict:
        """Full provisioning: device info -> cloud bind -> WiFi -> device secret."""
        info = await self.get_device_info()
        try:
            # Step 2: cloud bind (must be done by caller with iotToken context)
            cloud_result = await self.cloud_bind(info)
            if not cloud_result or "deviceSecret" not in str(cloud_result):
                _LOG.warning("Cloud bind response: %s", cloud_result)

            # Step 3: write the device secret + WiFi credentials to the device.
            # Order per BreezeHelper: secret first (TLV 10), then authcode secret
            # (TLV 11), then check-security (9).
            secret = cloud_result.get("deviceSecret", "") if isinstance(cloud_result, dict) else ""
            authcode = cloud_result.get("authCode", "") if isinstance(cloud_result, dict) else ""
            if secret:
                await self._send_sys_tlv([(TLV_UPDATE_DEVICE_SECRET, secret.encode())])
            if authcode:
                await self._send_sys_tlv([(TLV_UPDATE_AUTHCODE_SECRET, authcode.encode())])

            # WiFi config: ssid/password written as a single SYS TLV-style blob
            # (device side merges FFA0 frames in the miniapp profile; Breeze
            # devices accept the SSID through the same channel after secrets)
            wf = wifi_ssid.encode() + b"\x00" + wifi_password.encode() + b"\x00"
            await self._client.write_gatt_char(
                CHAR_WRITE_NO_RSP,
                b"\xff\xa0" + bytes([len(wifi_ssid), len(wifi_password)]) + wf[: 18],
                response=False,
            )
            # remaining bytes if any (frame split at 20-byte MTU)
            if len(wf) > 18:
                await self._client.write_gatt_char(
                    CHAR_WRITE_NO_RSP, wf[18: 18 + 19], response=False)

            return {"device": info, "cloud": cloud_result, "status": "provisioned"}
        finally:
            await self._disconnect()
