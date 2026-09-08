# AigoSmart / Aigostar Reverse Engineering Notes

Source: AigoSmart 2.15.6 (`com.aigostar.smart`, XAPK from APKPure), decompiled with
androguard (DEX parsing, constant extraction, bytecode analysis).

## TL;DR

AigoSmart is **NOT a Tuya system**. Despite some Aigostar devices shipping Tuya-OEM
chips (e.g. TG7100C which is actually a *Bouffalo Lab* chip Tuya uses), the cloud is
**Alibaba Cloud IoT Platform (AliGenie / Living line)**, the account system is
**Aigostar's own User Center (UC)**, and the LAN protocol is **ALCS** (Alibaba Local
Channel Service) — CoAP over UDP multicast `224.0.1.187:5683`. The Android app embeds
`com.aliyun.iot.aep.sdk` (the official "AIoT" app SDK) with Aigostar branding.

## App structure

```
com.aigostar.smart
├── com.aigostar.module.*            # device UIs (26 categories)
│   ├── devicelight (18 activities)  # RGB/CCT/mesh lights
│   ├── ilop (35)                    # "internet of things" device panels
│   ├── simpledevice (19)            # heaters, kettles, purifiers, door contacts...
│   ├── roboticvacuum, petfeeder, catlitterbox, dryer, airfryer, aquarium...
│   └── matter (8)                   # Matter controller support
├── com.aigostar.lib.aigo.api       # Aigostar UC constants (appkeys, secrets)
├── com.aliyun.iot.aep.sdk          # Alibaba AIoT app SDK (IoTSmart entry point)
│   ├── apiclient                   # IoT API Gateway client (x-ca-signature)
│   ├── oa                          # OpenAccount (login)
│   └──abus                          # message bus
├── com.aliyun.alink.linksdk        # device SDK wrapper ("tmp" panel layer)
│   ├── tmp.device.panel.*          # LocalChannelDevice / cloud channel selection
│   └── tmp.device.request.*        # GateWayRequest subclasses (API paths)
├── com.aliyun.linksdk.alcs        # ALCS local CoAP SDK
└── native libs: libiotcommon.so, libiotmgr.so, libiotapiclient.so, libcoap.so
```

## Credentials (from `CommonValueApiConstant`, classes2.dex)

| Item | Value |
|---|---|
| UC AppKey (header) | `smart-android-v1` |
| UC TenantId | `1000` |
| OAuth client_id | `C28098DEE9664BABBB9AE8E6E47505B0` |
| OAuth client_secret | `C3575D1E-7A5F-411F-920D-5C469AA53AB7` |
| IoT AppKey | `28770785` (dev: `28796419`, WisdomLeads tenant: `32615515`) |
| IoT AppSecret | `41fd4a1eb18fa7ace5e2abbbe3867f93` (dev: `380c8f87554e451611b3dcc0b42f3bf3`) |
| Password AES key | `tCx8BA0yKVr+NbBChH928URAV90=0000` (SHA1 of signing cert + "0000") |

These are public app credentials shipped inside the APK — same class of secret as
any mobile app's embedded keys; they are not user secrets.

## Cloud login flow (5 steps)

1. **UC login** — `POST https://uc.aigostar.com/v1.0/connect/token`
   - form: `account_type=email&username=...&password=<AES-256-CBC(zero IV, b64)>&grant_type=password&client_id=...&client_secret=...&cuid=<uuid5>`
   - headers: `AppKey: smart-android-v1`, `Timestamp: <ms>`, `TenantId: 1000`,
     `Signature: MD5(AppKey + AESKey + ts + "POST" + url [+ sortedParams]).upper()`
   - success → `access_token`, `refresh_token`, `user_id`
   - failure `UC/NEED_SECURITY_CODE` → email verification required
   - verification codes via `POST https://smartapi.aigostar.com/message/v1.1/security/sendcode/anonymous`
     (same UC signature headers) with `{"send_to": email, "account_type": "email", "action": "LoginSecurity", ...}`

2. **UC authorize** — `GET /v1.0/connect/authorize?response_type=code&client_id=smartapp&redirect_uri=none&scope=openid+profile&response_mode=json`
   with `Authorization: Bearer <access_token>` → `{"code": "<authCode>"}`

3. **Region discovery** — `POST https://api.link.aliyun.com/living/account/region/get`
   (apiVer 1.0.2) `{"type": "THIRD_AUTHCODE", "authCode": <authCode>}` → `data.oaApiGatewayEndpoint`
   (EU accounts: `living-account.eu-central-1.aliyuncs.com`)

4. **OA login** — `POST https://{oaHost}/api/prd/loginbyoauth.json`
   - form: `loginByOauthRequest={"oauthPlateform":23,"accessToken":null,"openId":null,"oauthAppKey":"28770785","tokenType":null,"authCode":"<authCode>","userData":null}`
     (`oauthPlateform` must be **integer 23**; the field name misspelling is in the API)
   - response: `data.data.loginSuccessResult.sid`

5. **IoT session** — `POST https://eu-central-1.api-iot.aliyuncs.com/account/createSessionByAuthCode`
   (apiVer 1.0.4) `{"request": {"authCode": <sid>, "appKey": ..., "accountType": "OA_SESSION"}}`
   → `data.iotToken / refreshToken / identityId / iotTokenExpire` (default 7200 s)

### IoT API Gateway calls (x-ca-signature)

Every call: `POST https://<region>.api-iot.aliyuncs.com/<path>`

```json
{
  "id": "<UUID>",
  "version": "1.0",
  "request": {"language": "en-US", "appKey": "28770785", "apiVer": "1.0.x", "iotToken": "..."},
  "params": {...}
}
```

Signature: canonical = `POST\nACCEPT\nContent-MD5\nContent-Type\nDate\nx-ca-key:...\nx-ca-nonce:...\nx-ca-stage:RELEASE\nx-ca-timestamp:...\nx-ca-version:1\n<path>`
(headers sorted alphabetically). `X-Ca-Signature = Base64(HMAC-SHA1(canonical, AppSecret))`.
Content-MD5 = base64(MD5(body)).

### API paths (apiVer) — from GateWayRequest subclasses

| Path | apiVer | Purpose |
|---|---|---|
| `/uc/listBindingByAccount` | 1.0.8 | full device list |
| `/uc/getByAccountAndDev` | 1.0.2 | single device detail |
| `/thing/properties/get` | 1.0.0 | read TSL properties |
| `/thing/properties/set` | 1.0.0 | write TSL properties |
| `/thing/service/invoke` | 1.0.0 | invoke TSL service |
| `/thing/tsl/get` | 1.0.0 | full TSL (data model) |
| `/thing/status/get` | 1.0.0 | online status |
| `/thing/extended/property/get` / `/thing/extended/property/set` | 1.0.2 | extended props |
| `/thing/deviceinfo/update`, `/thing/deviceinfo/update_reply` | — | device info |
| `/thing/productInfo/getByAppKey`, `/thing/productInfo/queryProductKey` | — | product info |
| `/thing/ota/info/queryByUser`, `/thing/ota/version/reportByUser` | — | OTA |
| `/thing/model/up_raw` | — | raw passthrough |
| `/account/createSessionByAuthCode` | 1.0.4 | login step 5 |
| `/account/checkOrRefreshSession` | 1.0.4 | token refresh |
| `/living/device/properties/batch/set` | — | batch property set |
| `/living/alias/batch/get`, `/living/alias/update` | — | property aliases |
| `/living/device/net/type/get` | — | device net type |
| `/living/device/bt/protocol/convert` | — | BLE protocol conversion |
| **Local control keys:** | | |
| `/alcs/device/accessInfo/get` (iotIdList) | 1.0.0 | ALCS accessKey/accessToken per device |
| `/living/device/localcontrol/accessinfo/get` (pk/dn/iotId) | — | combo (BLE mesh) local keys |
| `/living/device/localcontrol/accessinfo/sync/notify` | — | key sync notify |
| `/thing/lan/prefix/get`, `/thing/lan/prefix/update` | — (MQTT RPC) | LAN prefix register |
| `/thing/lan/blacklist/update` | — | LAN blacklist |

Device list item fields (from `EventCallbackbean`): `iotId, productKey, deviceName,
nickName, status, categoryKey, thingType, gmtCreate, groupId, tenantId, batchId...`.

## TSL data model

`/thing/tsl/get` returns the full thing model: `properties[]` with
`identifier/name/accessMode(rw)/required/dataType{type,specs{min,max,step,unit,...}}`,
`services[]`, `events[]`. Types: int, float, double, bool, enum, string, text, date,
array, struct (from `TSLProperty.parseSpecs` in classes4.dex).

### Observed property identifiers (per category, from app beans)

- **Lights**: `LightSwitch` (bool), `Brightness` (1–100), `ColorTemperature` (0–100, 0=2700K warm, 100=6500K cool),
  `LightMode` (0=white, 1=color), `HSVColor` `{Hue: 0–360, Saturation: 0–100, Value: 0–100}`,
  `LightScene`, `LightType`, `CountDown`, `LocalTimer`, `PowerSwitch`, `PowerSwitch_1/2`
- **Plugs/switches**: `PowerSwitch`, `powerSwitch1/2`, `Switch`, `childLockOnOff`, `indMode`, `powerOffMemory`
- **ACs**: `powerstate`, `targetTemperature`, `CuTemperature`, `Fahrenheit_degree`,
  `Target_temperature_Fahrenheit`, `Temperaturescale`, `mode`, `windspeed`,
  `angleAutoLROnOff`, `sleepOnOff`, `defrostOnOff`, `IonsSwitch`, `childLockOnOff`, `errorCode`
- **Heaters (ElectricHeater)**: `powerstate`, `targetTemperature`, `fahrenheit`, `mode`, `current_state`, `closeTime/openTime`
- **Dehumidifiers**: `powerstate`, `targetHumidity`, `humidity`, `mode`, `WaterStatus`, `childLockOnOff`
- **Fans (tower/intelligent)**: `powerstate`, `windspeed`, `mode`, `angleAutoLROnOff`,
  `coolWindOnOff`, `buzzerSwitch`, `reserveLeftTime`, `appointmentClosingTime`
- **Ovens/airfryers**: `Start_switch`, `Open_state`, `Equipmentstatus`, `Time`,
  `Dried_fruit_degrees_Celsius/Fahrenheit`, `Dry_fruit_time`, `Appointment_time`,
  `PreheatSwitch`, `Rotary_switch`, `Light_switch`, `Temperature_scale`, `Pan_turning_timing`
- **Curtains**: `curtainControl`, `position`, `motorOrientation`, `timeSet`, `status`
- **Pet feeder**: `FeedWaterPropertiesBean` — `Switch`, `WaterGear`, `WaterStatus`,
  `mode`, `waterLevvel`, `errorCode`, `FilterRemainderDay`, `CleaningRemainderDay`, resets
- **Cameras**: `lightSwitch`, `brightness`, `micSwitch`, `speakerSwitch`, `imageFlipState`,
  `streamVideoQuality`, `storageRecordMode`, `storageRemainCapacity`, ...

Property values arrive as `CommonTimeIntValueBean`-style objects: `{value, time}`.

## Local protocol — ALCS (CoAP)

### BLE provisioning — Breeze (adding devices without the phone app)

From `com.aliyun.iot.breeze.*` (classes16.dex):

**GATT layout** (`BreezeUuid`):
| UUID | Role |
|---|---|
| `0000FEB3-...` | Breeze service |
| `0000FED5-...` | write (requests) |
| `0000FED7-...` | write-no-response |
| `0000FED8-...` | notify (responses) |
| `0000FED4/FED6` | read / indicate |

**Pairing advertisement** (`BreezeScanRecord.parse`, manufacturer data id `0x0819`):
```
byte0     version (low nibble ≥3) | subType (high nibble, v≥4)
byte1     FMSK flags — bit5 = secure broadcast (sign+seq follow)
bytes2..  MID/productId (2B LE if v<4, 4B LE if v≥4)
next 6    MAC
[secure]  4B sign + 4B seq (LE)
rest      extra
```

**Provisioning TLV** (`TLV` constants): 1=SDK_VERSION, 2=PRODUCT_KEY,
3=DEVICE_NAME, 4=RANDOM, 5=SIGN, 7=UPDATE_SEQ, 9=CHECK_SECURITY,
10=UPDATE_DEVICE_SECRET, 11=UPDATE_AUTHCODE_SECRET, 12=CHECK_SIGN.
Transported inside SYS command **13** (`BreezeHelper.SYS_CMD`), payload = concatenated
`type(1) len(1) value(len)` elements. `getDeviceInfo()` sends TLVs 2/3/4/5 with
empty values; the device answers with its real PK/DN/random/sign.

**Cloud bind** (`BreezeHelper.bindBreezeDevice`): `POST /awss/ble/user/bind`
(apiVer 1.0.0, iotAuth) with `{"deviceName", "productId": <MID hex>,
"sign", "signMethod": "sha256", "signParams": {"clientId": <MIDhex upper>,
"random": <random>}}` → returns the deviceSecret/authCode that gets written
back to the device over BLE (TLVs 10/11) before pushing WiFi credentials.

Also available: `/awss/time/window/user/bind` (1.0.3, zero-config bind by
deviceName+productKey only) and `/awss/subdevice/unbind` (1.0.2).

Implementation: `python_client/aigosmart/breeze_provision.py` (bleak-based,
runs inside HA through its bluetooth integration — including ESPHome
bluetooth proxies).

## ALCS constants (com.aliyun.linksdk.alcs)

From `com.aliyun.linksdk.alcs.AlcsConstant` / `AlcsCmpSDK`:

| Constant | Value |
|---|---|
| Discovery multicast | `224.0.1.187:5683` |
| CoAP ports | 5683 (plain), 5684 (secure) |
| Discovery topic | `/dev/core/service/dev` |
| Methods | `core.service.dev` (discovery), `core.service.auth`, `core.service.heartBeat`, `thing.service.property.get`, `thing.service.property.set` |
| Payload format | Alink JSON: `{"id":n,"version":"1.0","method":"...","params":{...}}` |
| Session lifetime | 86400 s; heartbeat 600000 ms |
| Auth payload | `{"params":{"accessKey":"...","accessToken":"..."}}` (ICAAuthParams) — keys provisioned from cloud `/alcs/device/accessInfo/get` |
| LAN topics (MQTT, device side) | `/sys/{pk}/{dn}/thing/lan/prefix/get/update`, `/thing/lan/blacklist/update` |

The ALCS auth handshake (ECC/HMAC-MD5 session negotiation per `AlcsConstant.ALGECC/ALGHMACMD5`)
runs inside `libiotcommon.so` (native `AlcsCoAP.authHasKey/initAuth` JNI). The open-source
device SDK (iotkit-embedded) shows the server side: discovery replies carry
`{productKey, deviceName, ip, port}` in params; auth establishes a per-session
key; encrypted devices require the native handshake — plain devices accept
Alink JSON over unicast CoAP after `core.service.auth` with cloud-issued keys.

Practical local-control strategy (implemented):
1. Discover via multicast `core.service.dev`.
2. Fetch per-device `accessKey/accessToken` from the cloud (app does the same).
3. Unicast `core.service.auth` → then `thing.service.property.get/set` with Alink payloads.
4. Devices that enforce the encrypted native handshake may refuse — cloud remains the fallback.

## ProductKey catalog

`assets/aigo-smart-db-pk_product_table.csv` — 498 products with
`product_key;product_name;communication_mode;device_type;model_number;online_or_not;category`.
Categories: RGBLamp(116), Lamp(55), CeilingLight(51), DownLighting(36), CloudLamp(25),
Switch(23), StripLight(23), FilamentLamp(18), AirConditioner(12), Converter(11),
Socket(10), PanelLight(9), PurifyingLamp(7), Airfryer(7), FanLighting(6), Heater(5),
MeteredSocket(4), CatLitter(4), Feeder(4), Camera(4)...

The catalog ships in the APK so the app can map productKey → category → device panel
without a cloud round-trip. `aigosmart_device_merge.json` maps bindingKey → category.
`tenants.json` shows the app is multi-tenant (Aigostar/WisdomLeads, tenant 1001).

## Files in this repo

```
analysis/                      RE + PK catalog (498 products)
  REVERSE_ENGINEERING.md       this file
python_client/                 standalone client (no HA needed)
  aigosmart/const.py           all extracted constants
  aigosmart/cloud.py           5-step login + device/property API
  aigosmart/local.py           CoAP encoder/decoder + ALCS discovery + control
  aigosmart/util.py            AES password encryption (cryptography + pure fallback)
  aigo_cli.py                  CLI: login/devices/props/set/tsl/discover/localkeys
custom_components/aigosmart/   Home Assistant integration
```

## Test status

- `python -m unittest` in python_client: 4/4 pass (AES cross-check against
  `cryptography`, CoAP roundtrip).
- Live endpoint check: UC login with fake credentials returns proper
  `UC/INVALID_GRANT — The user does not exist` (signature + encryption + endpoint all
  verified correct).
- Local ALCS: implemented; needs a real device on the LAN to validate handshake.
