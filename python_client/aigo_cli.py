#!/usr/bin/env python3
"""CLI to exercise the AigoSmart API without Home Assistant.

Usage:
  python aigo_cli.py login EMAIL PASSWORD [CODE]
  python aigo_cli.py devices
  python aigo_cli.py props IOT_ID
  python aigo_cli.py set IOT_ID Key=value [Key2=value2 ...]
  python aigo_cli.py tsl IOT_ID
  python aigo_cli.py discover          (local ALCS multicast discovery)
  python aigo_cli.py localkeys IOT_ID [IOT_ID...]   (fetch ALCS accessKey/accessToken)
  python aigo_cli.py on IOT_ID / off IOT_ID          (quick helpers for lights)
  python aigo_cli.py enrollees       (devices in pairing mode via WiFi)
  python aigo_cli.py blescan          (BLE scan for Breeze pairing devices)
  python aigo_cli.py bleprovision MAC SSID WIFIPW   (add device to account via BLE!)
  python aigo_cli.py bind PK DN       (bind a BLE-provisioned device)

Credentials are cached in .aigo_session.json so you only log in once.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from python_client.aigosmart import AigoCloudClient, NeedSecurityCodeError
from python_client.aigosmart.local import discover_alcs_devices

SESSION_FILE = os.path.join(os.path.dirname(__file__), ".aigo_session.json")


def _save_session(client: AigoCloudClient, email: str, password: str) -> None:
    data = {
        "email": email,
        "password": password,
        "iot_token": client.iot_token,
        "refresh_token": client.refresh_token,
        "identity_id": client.identity_id,
        "token_expire": client.token_expire,
        "token_created": client.token_created,
        "iot_host": client.iot_host,
    }
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _load_or_die() -> tuple[AigoCloudClient, dict]:
    if not os.path.exists(SESSION_FILE):
        print("No saved session. Run: python aigo_cli.py login EMAIL PASSWORD")
        sys.exit(1)
    with open(SESSION_FILE, encoding="utf-8") as f:
        data = json.load(f)
    client = AigoCloudClient(iot_host=data.get("iot_host", "eu-central-1.api-iot.aliyuncs.com"))
    client.iot_token = data["iot_token"]
    client.refresh_token = data.get("refresh_token")
    client.identity_id = data.get("identity_id")
    client.token_expire = data.get("token_expire", 7200)
    client.token_created = data.get("token_created", 0)
    return client, data


def cmd_login(email: str, password: str, code: str = "") -> None:
    client = AigoCloudClient()
    try:
        client.login(email, password, code)
    except NeedSecurityCodeError:
        print("Server requires a verification code. Check your email.")
        print("Re-run with: login EMAIL PASSWORD <CODE>")
        # try to trigger the code being sent via smartapi
        try:
            from python_client.aigosmart.cloud import _uc_headers
            import urllib.request, urllib.parse
            from python_client.aigosmart import const

            body = {
                "send_to": email.strip(),
                "account_type": "email" if "@" in email else "phone_number",
                "action": "LoginSecurity",
                "re_send_count": 0,
                "captcha_token": "",
            }
            url = const.SMART_API_BASE + const.PATH_SEND_CODE
            headers = {"Content-Type": const.CONTENT_TYPE}
            headers.update(_uc_headers("POST", url))
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=15)
            print("Verification code requested.")
        except Exception as exc:
            print(f"(Could not auto-send verification code: {exc})")
        sys.exit(2)
    print(f"Logged in. iotToken expires in {client.token_expire}s")
    _save_session(client, email, password)
    devices = client.list_devices()
    print(f"{len(devices)} device(s):")
    for d in devices:
        print(f"  - {d.get('nickName') or d.get('deviceName')}  iotId={d.get('iotId')}  "
              f"status={d.get('status')}  productKey={d.get('productKey')}")


def cmd_devices() -> None:
    client, _ = _load_or_die()
    devices = client.list_devices()
    print(json.dumps(devices, indent=2, ensure_ascii=False))


def cmd_props(iot_id: str) -> None:
    client, _ = _load_or_die()
    print(json.dumps(client.get_properties(iot_id), indent=2, ensure_ascii=False))


def cmd_set(iot_id: str, pairs: list[str]) -> None:
    client, _ = _load_or_die()
    items: dict = {}
    for p in pairs:
        k, v = p.split("=", 1)
        try:
            v = int(v)
        except ValueError:
            if v.lower() in ("true", "false"):
                v = v.lower() == "true"
        items[k] = v
    client.set_properties(iot_id, items)
    print("OK")


def cmd_tsl(iot_id: str) -> None:
    client, _ = _load_or_die()
    print(json.dumps(client.get_tsl(iot_id), indent=2, ensure_ascii=False))


def cmd_discover() -> None:
    devices = discover_alcs_devices(timeout=3.0)
    if not devices:
        print("No ALCS devices found on the LAN.")
        return
    for d in devices:
        print(f"{d.addr}:{d.port}  PK={d.product_key}  DN={d.device_name}")


def cmd_localkeys(iot_ids: list[str]) -> None:
    client, _ = _load_or_die()
    infos = client.get_alcs_access_info(iot_ids)
    print(json.dumps(infos, indent=2, ensure_ascii=False))


def cmd_enrollees() -> None:
    """List devices in pairing mode reported by bound devices (WiFi autodiscovery)."""
    client, _ = _load_or_die()
    enrollees = client.list_enrollees()
    if not enrollees:
        print("No devices in pairing mode found on your LAN.")
        print("(Put a device in pairing mode: hold its button ~5 seconds.)")
        return
    for e in enrollees:
        print(f"  PK={e.get('productKey')}  DN={e.get('deviceName')}  "
              f"rssi={e.get('rssi')}")


def cmd_bind(product_key: str, device_name: str) -> None:
    """Bind a BLE-provisioned device to the account."""
    client, _ = _load_or_die()
    result = client.bind_ble_device(product_key, device_name)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_ble_scan() -> None:
    """Standalone BLE scan for Breeze pairing devices (needs bleak)."""
    try:
        import asyncio
        from bleak import BleakScanner
    except ImportError:
        print("pip install bleak  (standalone BLE scanning)")
        sys.exit(1)

    from python_client.aigosmart import const as aigo_const
    from python_client.aigosmart.ble_scanner import is_aigo_device

    async def run():
        print("Scanning BLE for 10 seconds...")
        devices = await BleakScanner.discover(timeout=10, return_adv=True)
        found = []
        for addr, (dev, adv) in devices.items():
            info = is_aigo_device(dev, adv)
            if info:
                found.append((addr, info))
        if not found:
            print("No Aigostar BLE devices found.")
            return
        for addr, info in found:
            mode = "PAIRING MODE" if info["pairing"] else "bound/mesh"
            print(f"  {addr}  {info['name'] or '?'}  PK={info['product_key'] or '?'}  [{mode}]  rssi={info['rssi']}")

    asyncio.run(run())


def cmd_ble_provision(address: str, ssid: str, wifi_pw: str) -> None:
    """Provision a pairing-mode Breeze device over BLE + bind it to the account."""
    try:
        import asyncio
    except ImportError:
        sys.exit(1)

    from aigosmart.breeze_provision import BreezeProvisioner
    client, _ = _load_or_die()

    async def _cloud_bind(info: dict) -> dict:
        return client.bind_ble_device(info["productKey"], info["deviceName"])

    async def run():
        prov = BreezeProvisioner(address, _cloud_bind)
        print(f"Connecting to {address} ...")
        info = await prov.get_device_info()
        print(f"  deviceName: {info['deviceName']}")
        print(f"  productKey: {info['productKey']}")
        print(f"Provisioning WiFi {ssid!r} ...")
        result = await prov.provision(ssid, wifi_pw)
        print("PROVISIONED:", json.dumps(result, indent=2, ensure_ascii=False))
        print("The device should now join your WiFi and appear in 'devices' shortly.")

    asyncio.run(run())


def cmd_on(iot_id: str) -> None:
    cmd_set(iot_id, ["LightSwitch=1"])


def cmd_off(iot_id: str) -> None:
    cmd_set(iot_id, ["LightSwitch=0"])


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    cmd, rest = args[0], args[1:]
    if cmd == "login" and len(rest) >= 2:
        cmd_login(rest[0], rest[1], rest[2] if len(rest) > 2 else "")
    elif cmd == "devices":
        cmd_devices()
    elif cmd == "props" and rest:
        cmd_props(rest[0])
    elif cmd == "set" and len(rest) >= 2:
        cmd_set(rest[0], rest[1:])
    elif cmd == "tsl" and rest:
        cmd_tsl(rest[0])
    elif cmd == "discover":
        cmd_discover()
    elif cmd == "localkeys" and rest:
        cmd_localkeys(rest)
    elif cmd == "on" and rest:
        cmd_on(rest[0])
    elif cmd == "off" and rest:
        cmd_off(rest[0])
    elif cmd == "enrollees":
        cmd_enrollees()
    elif cmd == "blescan":
        cmd_ble_scan()
    elif cmd == "bleprovision" and len(rest) >= 3:
        cmd_ble_provision(rest[0], rest[1], rest[2])
    elif cmd == "bind" and len(rest) >= 2:
        cmd_bind(rest[0], rest[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
