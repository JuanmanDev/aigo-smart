# AigoSmart for Home Assistant (unofficial)

<p align="center">
  <img src="docs/logo.png" width="120" alt="AigoSmart logo (extracted from the official Android app)">
</p>

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![Validate](https://img.shields.io/github/actions/workflow/status/JuanmanDev/aigo-smart/validate.yml?branch=main&style=for-the-badge)](https://github.com/JuanmanDev/aigo-smart/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/JuanmanDev/aigo-smart?style=for-the-badge)](https://github.com/JuanmanDev/aigo-smart/releases)
[![Tests](https://img.shields.io/badge/tests-52%2F52-brightgreen?style=for-the-badge)](python_client/tests)

Control your **Aigostar smart-home devices** (lights, plugs, heaters, air
conditioners, fans, and 50+ other categories â€” 498 supported products) directly
from Home Assistant â€” via the **AigoSmart cloud** and, where the device allows
it, **locally over your LAN**.

Everything here was built by **reverse engineering the AigoSmart Android app**
(v2.15.6) â€” the full protocol documentation lives in
[`analysis/REVERSE_ENGINEERING.md`](analysis/REVERSE_ENGINEERING.md).

> **Fun fact:** AigoSmart is *not* a Tuya system. Despite Tuya-OEM chips in some
> devices, the cloud is **Alibaba Cloud IoT** and the local protocol is
> **ALCS** (Alibaba Local Channel Service, CoAP over UDP).

## Screenshots

<p align="center">
  <img src="screenshots/list.jpeg" width="320" alt="AigoSmart shown in the Home Assistant integrations list">
  &nbsp;&nbsp;
  <img src="screenshots/devices.jpeg" width="600" alt="AigoSmart device list — 3 CCT panel lights across 3 rooms">
</p>
<p align="center">
  <img src="screenshots/add.jpeg" width="320" alt="Login / config-flow dialog — enter your AigoSmart email and password">
  &nbsp;&nbsp;
  <img src="screenshots/ccr.jpeg" width="250" alt="CCT light control card — colour temperature (warm) mode">
  &nbsp;&nbsp;
  <img src="screenshots/level.jpeg" width="250" alt="CCT light control card — brightness / level mode">
</p>

## Features

- **Account login** — the same email + password you use in the AigoSmart app
  (with automatic email verification-code handling)
- **Automatic device discovery** — devices you add to the app appear in Home
  Assistant within minutes, no reload needed
- **Add devices from Home Assistant** — guided Add-Device flow that finds devices
  in pairing mode via **Bluetooth** and **WiFi**, and **provisions them over BLE**
  (sends WiFi credentials to the device + binds it to your AigoSmart account).
  Works through **ESPHome bluetooth proxy** — HA doesn't need its own BLE adapter.
- **Local control** (experimental) — ALCS multicast discovery and CoAP control
  for devices that accept local sessions
- **Full colour support** — colour property detected by *shape* from each
  device TSL model (Wi-Fi HSVColor and BLE Mesh RGB/HSL structs both work,
  with per-product 16-bit ranges) — colour engine from
  [hass-aigosmart](https://github.com/andreazllin/hass-aigosmart)
- **Fans done right** — speed 1-3, preset modes (Normal/Natural/Sleep),
  oscillation, auto-off timer (number), key-beep switch
- **Kettles** — exposed as water_heater (target temperature, on/off)
- **Diagnostics** — native Download-diagnostics button on the integration
  and every device page, with cloud metadata, live properties, full TSL model,
  ALCS local keys and connection info
- **Platforms:** light, switch, climate, fan, sensor, number, water_heater
- **Standalone Python client + CLI** — test everything without Home Assistant

<p align="center">
  <img src="screenshots/ccr.jpeg" width="260" alt="CCT light control card — colour temperature (warm) mode">
  &nbsp;&nbsp;
  <img src="screenshots/level.jpeg" width="260" alt="CCT light control card — brightness / level mode">
</p>

## Credits / related projects

This project stands on the shoulders of two earlier reverse-engineering efforts
and extends them:

| Project | What it contributed here |
|---|---|
| [MarcoM1993/ha-aigostar](https://github.com/MarcoM1993/ha-aigostar) | The original AigoSmart cloud reverse engineering: the complete 5-step login flow, x-ca-signature, TSL property mapping for TG7100C bulbs |
| [andreazllin/hass-aigosmart](https://github.com/andreazllin/hass-aigosmart) | Colour-by-TSL-shape engine (Wi-Fi + BLE Mesh), full fan support (presets/oscillation/timer/buzzer), kettle as water_heater, gateway exclusion |

**This repo adds:** the standalone Python client/CLI (test without HA),
autodiscovery coordinator, LAN ALCS listener + local control, BLE/WiFi
add-device flows, diagnostics with device detail, climate/switch/sensor
platforms, the 498-product PK catalog, and the full
[reverse-engineering documentation](analysis/REVERSE_ENGINEERING.md).

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JuanmanDev&repository=aigo-smart&category=integration)

Click the button above, or manually:

1. Open **HACS** in Home Assistant
2. Go to **Integrations** â†’ **â‹®** (top right) â†’ **Custom repositories**
3. Add `https://github.com/JuanmanDev/aigo-smart` with category **Integration**
4. Find **AigoSmart** in the list and install it
5. Restart Home Assistant

### Manual

1. Download or clone this repository
2. Copy `custom_components/aigosmart/` into `/config/custom_components/`
3. Copy `python_client/` into `/config/custom_components/` (the integration
   imports the shared client from there)
4. Restart Home Assistant

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aigosmart)

Or manually:

1. **Settings → Devices & Services → Add Integration**
2. Search for **AigoSmart (unofficial)**
3. Enter your AigoSmart app email and password
4. If the server requests a verification code, it's sent to your email automatically — just type it in
5. All devices bound to your account appear automatically

<p align="center">
  <img src="screenshots/add.jpeg" width="340" alt="Config-flow login dialog — enter your AigoSmart email and password">
  &nbsp;&nbsp;
  <img src="screenshots/devices.jpeg" width="580" alt="Device list in Home Assistant after successful login">
</p>

### Adding devices (full BLE provisioning)

Yes — you can add devices **directly from Home Assistant**, without the phone app:

1. Put the device in **pairing mode** (usually hold its power button ~5 seconds)
2. Run the `aigosmart.add_device` service (Settings → Devices → AigoSmart → ⋮, or
   Developer Tools → Services)
3. Pick the device from the discovered list (BLE pairing devices advertise the
   Breeze service `0xFEB3`)
4. Enter your **WiFi credentials** when prompted
5. The integration provisions the device over Bluetooth (Breeze protocol:
   TLV handshake → cloud bind `/awss/ble/user/bind` → device secret → WiFi
   credentials) and the new device appears via autodiscovery within seconds

**Bluetooth without a local adapter:** the flow runs on top of HA's `bluetooth`
integration, so an **ESPHome bluetooth proxy** works out of the box — bleak
connects through HA's remote adapters transparently. Just make sure the proxy
is near the new device.

Standalone (no HA):

```
python aigo_cli.py blescan                        # find pairing devices
python aigo_cli.py bleprovision AA:BB:CC:DD:EE:FF "MyWiFi" "wifipassword"
```

### Services

| Service | Description |
|---|---|
| `aigosmart.sync_devices` | Force an immediate cloud re-sync |
| `aigosmart.add_device` | Start the guided Add-Device flow (Bluetooth + WiFi) |
| `aigosmart.discover_local` | One-shot ALCS multicast discovery sweep |
| `aigosmart.discover_bluetooth` | Scan BLE for Aigostar pairing devices (needs the `bluetooth` integration) |

### Standalone CLI (no Home Assistant required)

```powershell
cd python_client
pip install cryptography

python aigo_cli.py login EMAIL PASSWORD    # login (may ask for email code)
python aigo_cli.py devices                 # list all devices
python aigo_cli.py props <IOT_ID>          # read current state
python aigo_cli.py set <IOT_ID> LightSwitch=1 Brightness=80
python aigo_cli.py tsl <IOT_ID>            # full device data model
python aigo_cli.py discover                # LAN ALCS discovery
python aigo_cli.py enrollees               # devices in pairing mode
python aigo_cli.py blescan                 # Bluetooth scan (needs bleak)
```

## How it works

The integration implements the exact same login flow as the Android app
(reverse-engineered via APK decompilation and validated against the live
servers):

1. **UC Login** â€” authenticate with Aigostar's User Center
2. **UC Authorize** â€” obtain an authorization code
3. **Region Discovery** â€” resolve the correct regional gateway
4. **OAuth Login** â€” exchange the authCode for a session
5. **IoT Session** â€” obtain the iotToken used for all device API calls

Device control uses the Alibaba IoT API Gateway (`/thing/properties/get|set`,
`/thing/service/invoke`) with the same `x-ca-signature` HMAC-SHA1 signing as
the app. Local control speaks **ALCS** (CoAP over UDP, multicast
`224.0.1.187:5683` for discovery) with per-device keys provisioned from the
cloud (`/alcs/device/accessInfo/get`).

Full technical details: [`analysis/REVERSE_ENGINEERING.md`](analysis/REVERSE_ENGINEERING.md)

## Supported devices

All 498 products that work with the AigoSmart app â€” see
[`analysis/pk_catalog.json`](analysis/pk_catalog.json) for the complete
productKey â†’ category catalog extracted from the APK, including:

| Category | Examples |
|---|---|
| Lights (200+) | RGB lamps, ceiling lights, downlights, strips, filament, spots, mesh lights |
| Power | Sockets, metered sockets, power strips, circuit breakers, curtain switches |
| Climate | Air conditioners, heaters, dehumidifiers, oil-filled radiators, pet thermostats |
| Appliances | Air fryers, ovens, kettles, humidifiers, purifiers, dryers |
| Pets | Feeders, water dispensers, cat litter boxes, cat beds |
| Sensors | Door contacts, smoke alarms |
| Other | Cameras, robotic vacuums, aquariums, string lights, solar lamps |

## Troubleshooting

- **Login fails** â€” use the exact credentials that work in the AigoSmart app; check the verification-code email (including spam).
- **Devices unavailable** â€” the device must be online in the AigoSmart app first.
- **Changes in the app not reflected in HA** — HA re-reads the cloud shadow every 30 s, so external changes appear within ~30 s. Availability uses `/thing/status/get`, because the device-list `status` field is unreliable.
- **New device not appearing** â€” wait â‰¤ 5 minutes for autodiscovery, or call `aigosmart.sync_devices`.
- **BLE add-device finds nothing** â€” make sure the Home Assistant `bluetooth` integration is active and the device is in pairing mode (FEB3 advertisement).

## 💕 Support this project
If you found this project helpful, please consider supporting it!

[![GitHub Sponsor](https://img.shields.io/badge/Sponsor-JuanmanDev-ea4aaa?style=for-the-badge&logo=github)](https://github.com/sponsors/JuanmanDev) [![Ko-fi](https://img.shields.io/badge/Ko--fi-F16061?style=for-the-badge&logo=ko-fi&logoColor=white)](https://ko-fi.com/juanmandev) [![PayPal](https://img.shields.io/badge/PayPal-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/juanmandev)

<br>

## Attribution

This project was created using the **GLM 5.3 Free** model from
[Token Router](https://tokenrouter.io/) â€” the reverse engineering, protocol
implementation, integration architecture, and documentation were all produced
with it as the coding assistant. If you're curious about AI-assisted reverse
engineering of IoT protocols, this repo is a fully worked example.

## Disclaimer

Unofficial and not affiliated with Aigostar or Alibaba. Developed through
reverse engineering of the AigoSmart Android app for personal and educational
use. Use at your own risk.

## License

[MIT](LICENSE)
