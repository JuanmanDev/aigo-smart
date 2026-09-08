# AigoSmart Home Assistant Integration - Diagnostic & Bug Report

**Date:** 2026-09-03  
**Home Assistant Version:** 2026.9.0 (Docker container on Linux 7.0.14-12-pve x86_64, Python 3.14)  
**Cloud Backend:** Alibaba Cloud IoT EU (`eu-central-1.api-iot.aliyuncs.com`)  

---

## Executive Summary

The connection and authentication to the **AigoSmart / Alibaba Cloud IoT servers succeeded completely**.
- **Login:** Successfully logged in with user credentials and acquired `iot_token` (`<REDACTED>`).  
- **Device Enumeration:** Successfully retrieved 1 bound device from the cloud account:
  - **Name:** `Techo R3`
  - **Product:** `LED Back-lit Panel Light CCT 600x600`
  - **ProductKey:** `a1mevewbgC3`
  - **IoT ID:** `<REDACTED>`
  - **Status:** `3` (Online/bound)
- **Properties Poll:** Successfully retrieved live telemetry:
  - `LightSwitch: 1`, `Brightness: 1`, `ColorTemperature: 0`

### Why it appeared "disconnected" with 0 devices in Home Assistant
The integration did not display devices and crashed when clicking "Configure" due to **6 specific implementation bugs in the Home Assistant custom component code**:
1. **Premature `registry.sync()` call in `__init__.py`**: Marked all devices as "already known" before platforms (`light.py`, etc.) were initialized, causing platforms to skip creating entities for them (0 entities created).
2. **Invalid `supported_color_modes` in `light.py`**: Home Assistant rejected the light entity with `HomeAssistantError: sets invalid supported color modes {<ColorMode.BRIGHTNESS>, <ColorMode.COLOR_TEMP>}` because in HA `COLOR_TEMP` already implies brightness.
3. **`OptionsFlow` crash on "Configure" button**: `AigoSmartOptionsFlow` had `self.config_entry = config_entry`. In Home Assistant, `config_entry` is a read-only property with no setter.
4. **`ModuleNotFoundError: No module named 'python_client'`**: `local_listener.py`, `ble_discovery.py`, and `config_flow.py` imported from `python_client.aigosmart` instead of `aigosmart`, causing the background ALCS listener thread to crash.
5. **Missing `config_schema()` function in `config_validation.py`**: Caused `AttributeError: module '...config_validation' has no attribute 'config_schema'`.
6. **Online status check `status == 1`**: The user's online light returned `status: 3`.

---

## Detailed Log Traces & Errors

### 1. Light Entity Rejection (Home Assistant Core)
```text
2026-09-03 13:20:58.376 ERROR (MainThread) [homeassistant.components.light] Error adding entity None for domain light with platform aigosmart
Traceback (most recent call last):
  File "/usr/src/homeassistant/homeassistant/components/light/__init__.py", line 1029, in __validate_supported_color_modes
    valid_supported_color_modes(supported_color_modes)
  File "/usr/src/homeassistant/homeassistant/components/light/__init__.py", line 87, in valid_supported_color_modes
  File "/usr/src/homeassistant/homeassistant/components/light/__init__.py", line 1031, in __validate_supported_color_modes
    raise HomeAssistantError(
        f"{entity} ({type(entity)}) sets invalid supported color modes {supported_color_modes}"
    ) from err
homeassistant.exceptions.HomeAssistantError: None (<class 'custom_components.aigosmart.light.AigoSmartLight'>) sets invalid supported color modes {<ColorMode.BRIGHTNESS: 'brightness'>, <ColorMode.COLOR_TEMP: 'color_temp'>}
```
**Root Cause:**
In `custom_components/aigosmart/light.py`:
```python
if PROP_BRIGHTNESS in props:
    self._color_modes = {ColorMode.BRIGHTNESS} | {
        m for m in self._color_modes if m != ColorMode.ONOFF
    }
```
When `_color_modes` was already `{ColorMode.COLOR_TEMP}`, unioning `{ColorMode.BRIGHTNESS}` violated Home Assistant's color mode rules: `BRIGHTNESS` must only be declared if no color mode (`COLOR_TEMP`, `HS`, `RGB`, etc.) is supported.

---

### 2. OptionsFlow Setter AttributeError
```text
2026-09-03 13:13:35.124 ERROR (MainThread) [aiohttp.server] Error handling request from 192.168.2.3
Traceback (most recent call last):
  File "/usr/src/homeassistant/homeassistant/data_entry_flow.py", line 309, in async_init
    flow = await self.async_create_flow(handler, context=context, data=data)
  File "/usr/src/homeassistant/homeassistant/config_entries.py", line 3932, in async_create_flow
    return handler.async_get_options_flow(entry)
  File "/config/custom_components/aigosmart/config_flow.py", line 189, in async_get_options_flow
    return AigoSmartOptionsFlow(config_entry)
  File "/config/custom_components/aigosmart/config_flow.py", line 194, in __init__
    self.config_entry = config_entry
AttributeError: property 'config_entry' of 'AigoSmartOptionsFlow' object has no setter
```
**Root Cause:**
In `custom_components/aigosmart/config_flow.py`:
```python
class AigoSmartOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry
```
In HA 2024+, `OptionsFlow` provides `config_entry` as a getter property linked to `self.handler`. Assigning to `self.config_entry` crashes when the user clicks "Configure" in the integrations UI.

---

### 3. Missing `python_client` in ALCS Background Thread
```text
2026-09-03 13:13:16.934 ERROR (aigosmart-alcs) [root] Uncaught thread exception
Traceback (most recent call last):
  File "/usr/local/lib/python3.14/threading.py", line 1024, in run
    self._target(*self._args, **self._kwargs)
  File "/config/custom_components/aigosmart/local_listener.py", line 64, in _run
    from python_client.aigosmart import const
ModuleNotFoundError: No module named 'python_client'
```
**Root Cause:**
`local_listener.py`, `ble_discovery.py`, and `config_flow.py` had hardcoded `from python_client.aigosmart ...`, but `api.py` adds `python_client` itself to `sys.path`, meaning the module name is `aigosmart`, not `python_client.aigosmart`.

---

### 4. Zero Entities Created (Premature Registry Sync)
**In `custom_components/aigosmart/__init__.py`:**
```python
    # Initial sync:
    registry.sync(coordinator.devices, pk_catalog)
...
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
```
**In `custom_components/aigosmart/light.py` (and `switch.py`):**
```python
    def _make_entities() -> list:
        out = []
        for dev in coordinator.devices:
            iot_id = dev.get("iotId", "")
            if iot_id and iot_id not in registry._known["light"] and _is_light(dev):
                out.append(AigoSmartLight(coordinator, dev))
        return out
```
Because `registry.sync()` added `iot_id` to `registry._known["light"]` before `async_forward_entry_setups` was even called, `iot_id not in registry._known["light"]` was `False` for every device. Therefore, 0 entities were created.

---

## Live Cloud API Verification Data

### `client.list_devices()` Result:
```json
[
  {
    "productModel": "600x600",
    "gmtModified": 1765890690000,
    "categoryImage": "http://iotx-paas-admin.oss-cn-shanghai.aliyuncs.com/publish-frankfurt/image/1585829806873.png",
    "netType": "NET_WIFI",
    "nickName": "Techo R3",
    "categoryKey": "Light",
    "description": "<REDACTED>@aigo.com's device",
    "productKey": "a1mevewbgC3",
    "nodeType": "DEVICE",
    "isEdgeGateway": false,
    "categoryName": "灯",
    "deviceName": "<REDACTED>",
    "identityAlias": "<REDACTED>@aigo.com",
    "productName": "LED Back-lit Panel Light CCT 600x600",
    "iotId": "<REDACTED>",
    "productImage": "http://iotx-paas-admin.oss-cn-shanghai.aliyuncs.com/publish-sg/image/1585741685441.png",
    "bindTime": 1765890690000,
    "owned": 0,
    "identityId": "<REDACTED>",
    "thingType": "DEVICE",
    "status": 3
  }
]
```

### `client.get_properties('<REDACTED>')` Result:
```json
{
  "LightType": 1,
  "LightSwitch": 1,
  "CountDownList": {
    "LightSwitch": 0,
    "Target": "LightSwitch",
    "Contents": ""
  },
  "ColorTemperature": 0,
  "LightMode": 0,
  "Brightness": 1,
  "LocalTimer": []
}
```

---

## Status After Applying Fixes

All the bugs above have been patched directly on the codebase and deployed to Home Assistant:
- **Entity State:** `light.techo_r3` is now created, online, and fully controllable in Home Assistant.
- **Attributes in HA:**
  - `state`: `on`
  - `supported_color_modes`: `["color_temp"]`
  - `color_mode`: `color_temp`
  - `brightness`: `3`
  - `color_temp_kelvin`: `2700`
  - `friendly_name`: `Techo R3`
