# Changelog

## [Unreleased]

### Added

- **Pet alerts as on/off sensors** (#4), so automations can use a plain "turns on" trigger instead of matching codes or numbers. Fountain: **Water low** (the fountain's own low-water flag or error 236), **Filter change due** and **Cleaning due** (0 days left). Feeder: **Food low**, **Food jammed**, **Battery low**, **Feeding** (dispensing now) and **Mains power**.
- **Editable feeding schedule.** Each meal of the feeder's schedule gets a **Meal N** switch (on/off), a **Meal N time** and **Meal N portions**. A change rewrites only that meal and keeps the others as they are. The new **Set feeding schedule** action (`aigosmart.set_feeding_plan`) replaces the whole schedule, to add or remove meals (each meal runs every day).
- **Feeding event.** A **Feeding** event entity fires once for each new feed the feeder records, as `scheduled` when it matches an enabled meal's time (within 3 minutes) or `manual` otherwise, with the portions and time. Feeds recorded while Home Assistant was off are not replayed.
- The fountain's filter and cleaning intervals can be changed (0–90 days), like in the app.

### Changed

- The fountain's **Filter change interval** and **Cleaning interval** are now number entities instead of read-only sensors; the old sensor entities are removed automatically.

## [0.10.2] - 2026-11-24

### Changed

- **Faster cloud polling.** Cloud calls now re-use kept-alive HTTPS connections and one TLS context instead of opening a new connection for every call (measured against the gateway: about 190 ms for the first call, about 40 ms for each following one). Devices are polled four at a time instead of one after the other, and one slow or failing device no longer delays the rest. Only read calls are re-sent when a kept-alive connection turns out to be closed; writes are never repeated by the transport.
- Token refresh is serialised, so parallel polls can't refresh the session several times at once.

### Fixed

- **Local control (ALCS) rewritten to the real protocol.** The old implementation could not work with any device: the CoAP header byte was built wrong (type and token length in the wrong bits), so every packet was malformed; discovery answers in the device SDK's format (`data.devices.profile[]`) were not recognised; and the session handshake and encryption did not match. The new client follows Alibaba's device SDK (iotkit-embedded v3): `core/service/auth` with the cloud-issued accessKey/accessToken and a signed random key, an AES-128 session key, encrypted `thing.service.property.get/set` with the session-id option, re-authentication when the device drops the session, and separate (non-piggybacked) responses. It's covered by tests against a simulated device built from the SDK's device side, but hasn't been confirmed on a real Aigostar device yet.
- LAN discovery no longer creates empty placeholder devices in the device registry; it only records where devices answer.

### Added

- Option **Read device state over the local network (experimental)** (Configure on the integration card, off by default): reads device state over the LAN when the device answers there, with automatic fallback to the cloud. Commands still go through the cloud. English and Spanish translations.
- Device diagnostics: a `local_control.check` section runs each local step once (seen on the LAN, local key available, session, read) and shows where it stops, without IP addresses or keys.

## [0.10.1] - 2026-11-24

### Fixed

- Pet water fountain: Home Assistant jumped back to the old pump / mode / flow values a few seconds after a change, although the fountain itself had switched ([#4](https://github.com/JuanmanDev/aigo-smart/issues/4)). The fountain applies cloud writes but never reports the new value back, so the cloud shadow (which Home Assistant and the AigoSmart app both read) stays frozen at the old values. Home Assistant now keeps showing the value it wrote for as long as the shadow still holds the value it had before; as soon as the shadow really changes (the fountain reports, or the value is changed in the app or on the device), that value wins again. The written values survive a restart and are listed as `assumed_properties` in the device diagnostics. Only pet fountains are affected. Thanks to [@emanuelrg123](https://github.com/emanuelrg123) for the test with the app closed and the debug log.

## [0.10.0] - 2026-11-23

### Requirements

- Still Home Assistant **2024.11.0** or newer (unchanged); nothing in this release needs a newer version. The climate / water heater temperature properties that Home Assistant deprecates for 2027.11 will be migrated in a later release: the replacement API only exists from Home Assistant 2026.11.

### Added

- Devices the integration can't create entities for yet (robot vacuums, cameras, litter boxes, ...) now get a **Repairs** issue in Settings → System → Repairs: one per productKey, naming the devices, with a *Learn more* link to the device-support form. It disappears by itself once a newer version supports the device, or when the device is removed from the account. Translated to English and Spanish.
- Pet feeder: **Feed** button and **Feed portions** setting (1-24, from the device TSL) for manual feeding ([#4](https://github.com/JuanmanDev/aigo-smart/issues/4)). Only pressing Feed dispenses; the portions setting is local, restored after a restart, and never sent on its own. Also a **Voice on feed** switch and approximate grams fed today. Thanks to [@emanuelrg123](https://github.com/emanuelrg123) for the device diagnostics and the before/after capture of a manual feed.
- Pet water fountain: **Reset filter** and **Reset cleaning** buttons ([#4](https://github.com/JuanmanDev/aigo-smart/issues/4)).
- Advanced action **`aigosmart.set_properties`** (admin only): write raw TSL properties to a device, e.g. `{"WaterGear": 3}`, to test what a new or misbehaving device accepts without waiting for a release. Input is validated (non-empty mapping of property to number/string/boolean/object/list, at most 20). Errors from the cloud are shown in the UI, and the device is re-polled right after. All actions now have English and Spanish translations.

### Changed

- Pet fountain / feeder status codes now have names taken from the device TSL (e.g. feeder error `low_food` / `food_jammed` / `low_battery`, feed state `feeding`, power source `mains` / `battery`, fountain water status `low_water`); unknown codes still show the raw number, and the code is kept as an attribute. Entity names lose the "code" suffix; entity IDs don't change. The TSL of pet devices is now fetched once, and write controls are skipped if the TSL marks a property read-only.
- Pet fountain: every write now carries a single property (two quick changes are sent one after the other), as with the fan-light and colour fixes, while the cause of ignored fountain commands is investigated ([#4](https://github.com/JuanmanDev/aigo-smart/issues/4)).

### Fixed

- Entities that depend on a device property (aquarium switches, pump speed and scenes, fan key-beep, temperature/humidity sensors) were only created if that property was already known at startup, e.g. not when the device was offline during the first poll. They are now created as soon as the property shows up. Fan timers and kettles, which were only created at startup, now also appear for devices added later. Nothing is duplicated, and unique IDs are unchanged.
- The fan timer showed 0 until the next poll after a restart; it now shows the device value straight away.
- Changing the brightness of a light in colour mode turned it white ([#2](https://github.com/JuanmanDev/aigo-smart/issues/2)). The integration wrote the separate brightness property, which drives the white channel. In colour mode it now rewrites the colour with the new brightness (the colour's Value) and keeps the hue and saturation. Brightness in colour mode is also read back from the colour, so the slider no longer jumps. Thanks to [@Loginbug](https://github.com/Loginbug) for confirming the colour fix and reporting this.

### Removed

- The unused `DeviceRegistry.sync()` (internal; entity creation never called it).

## [0.9.1] - 2026-11-23

### Fixed

- Colour changes on fan-light lamps and other colour lights were sometimes ignored (most visibly red) ([#2](https://github.com/JuanmanDev/aigo-smart/issues/2), [#3](https://github.com/JuanmanDev/aigo-smart/issues/3)). Every colour write bundled the value with a redundant switch-on and the white/colour `LightMode`. The cloud accepted it, but the lamp could drop the colour. The white/colour mode is now written in a separate write after the colour or colour temperature, and only when it changes. Fan-light lamps no longer get a redundant `powerstate_1: 1` while on, which matches the fan-speed fix. Thanks to [@Loginbug](https://github.com/Loginbug) for the debug log that showed it.
- Hue 360 (or anything that rounds to it) is now sent as 0, which is the same red.
- The post-write check compared the device's quantised colour values by truncation (`8.998` vs `9`), so every colour write looked unconfirmed. It now compares with a small tolerance.
- `climate.turn_on` / `climate.turn_off` were rejected for AigoSmart climate devices: the entity did not declare `TURN_ON` / `TURN_OFF` support. They now map to the device's power property, like the HVAC mode.

### Changed

- CI now runs every test file (including `test_aquarium.py`, which was broken) and `ruff check`. The existing lint findings are fixed.
- The release workflow now files the `[Unreleased]` CHANGELOG section under the new version in the release commit, and detects `BREAKING CHANGE` footers.
- Bug reports and device-support requests now use GitHub issue forms that ask for the versions, productKey / categoryKey, debug log and diagnostics, with a redaction reminder.
- README: supported devices are listed by what actually gets entities; the 498-product catalog is described as a catalog, not a support list.
- `hacs.json` now requires Home Assistant 2024.11.0, the oldest version the integration actually works with (re-authentication / reconfigure flows).

## [0.9.0] - 2026-11-23

### Added

- Pet water fountains (cloud category `petWaterFountain`, e.g. `a163VQp0e0e` / Nobleza S40101N34) ([#4](https://github.com/JuanmanDev/aigo-smart/issues/4)): **Pump** switch, **Mode** (continuous / intermittent) and **Flow** (low / medium / high) selects, **Water level**, plus diagnostic sensors for days until filter change / cleaning, their intervals, and the water-status and error codes.
- Pet feeders (cloud category `petFeeder`, e.g. `a1xEttfYqF2`) ([#4](https://github.com/JuanmanDev/aigo-smart/issues/4)): **Last feeding** (timestamp + portions), **Portions today**, **Feeding plan** (read-only; the schedule is in the attributes), **Indicator light** and **Slow feed** switches, **Battery**, and diagnostic feed-state / battery-state / error codes. Manual feeding is not exposed until its identifier is confirmed. Thanks to [@emanuelrg123](https://github.com/emanuelrg123) for the diagnostics and app screenshots.
- Integration-level diagnostics now include each product's TSL model (`tsl_models`), so one download is enough to add support for a new device.

## [0.8.1] - 2026-11-23

### Fixed

- Fan-light speed changes did not move the motor ([#2](https://github.com/JuanmanDev/aigo-smart/issues/2)). The BLE Mesh fan-lights ignore a `windspeed` sent together with a redundant `powerstate_2: 1`, and ignore any manual speed while a breeze preset (`mode_2` Natural / Sleep / Fresh) is active. A speed change on a running fan-light now sends only `windspeed`, plus `mode_2: 0` when a preset was active. Starting from off still sends power and speed in one write. Selecting a preset sends only `mode_2` while running. Diagnosis by [@davidpla7](https://github.com/davidpla7). Classic fans (tower fan) are unchanged.

## [0.8.0] - 2026-11-23

### Added

- Support for Aigostar fan-lights (cloud category `fanLight`, e.g. `a180PpkuyP6` / `a1aeYLqeLsb`) ([#2](https://github.com/JuanmanDev/aigo-smart/issues/2)): a **Fan** entity (on/off, 6 speeds, Normal/Natural/Sleep/Fresh presets, forward/reverse direction) and a separate **Light** entity (on/off, brightness, colour temperature, colour on RGBCCT models). The auto-off timer uses minutes (0-1440) on these models.
- Support for the Aigostar air fryer `a1MqVKrp9JA` (category `Airfryer`): Cooking switch, Program select, Temperature / Cook time / Flip reminder / Delay start numbers, and Status / Error / Flip food / Lid sensors. Contributed by [@jazzvivi2](https://github.com/jazzvivi2) ([#5](https://github.com/JuanmanDev/aigo-smart/issues/5)).

### Fixed

- Diagnostics downloads no longer expose the account (`identityAlias`, `identityId`), device identifiers (`iotId`, `deviceName`, TSL `link`) or ALCS local-control keys. They are redacted, and properties are keyed by a `device_N` alias instead of the iotId.
- `fan.turn_on` / `fan.turn_off` were rejected by Home Assistant for every AigoSmart fan: the entity did not declare `TURN_ON` / `TURN_OFF` support, which HA requires since 2025.2. `async_turn_on` now also accepts HA's positional arguments.
- Setting a fan to 0 % sent the invalid `windspeed: 0` (cloud error 6309). It now turns the fan off.
- Colour support for lights without a pinned profile was guessed once at startup from a property snapshot, and the device TSL was never downloaded outside diagnostics. Lights restarted with an empty snapshot stayed colour-temperature-only until a reload. The TSL is now fetched when each light is added, colour support is re-evaluated when properties arrive, and failed TSL fetches are retried ([#3](https://github.com/JuanmanDev/aigo-smart/issues/3)).
- Writes are now logged at debug level, together with a debug line when the device does not report back what was sent, so writes the firmware ignores leave a trace.
- Devices that no platform supports are now reported once at INFO level (name, category, productKey) instead of being skipped silently. A device set up as a plain switch that has no known on/off property logs a warning listing its real properties ([#4](https://github.com/JuanmanDev/aigo-smart/issues/4)).

## [0.7.0] - 2026-10-24

### Added

- Re-authentication flow. When the cloud refuses the stored login (password changed, or the server now asks for an email verification code), Home Assistant shows a "Re-authentication required" notification instead of leaving every entity unavailable. Leave the password blank to retry with the stored one, or enter a new one. The verification code is emailed automatically when needed.
- Reconfigure flow to change the account email or password without removing the integration.
- "Send a new code" option on the verification-code step, and an explicit error when the code email could not be requested.
- Spanish translation of all setup dialogs.

### Fixed

- `ConfigEntryAuthFailed` at startup pointed to a re-authentication step that did not exist.
- A refused re-login no longer retries forever: retrying with a missing code or a wrong password only sent more code emails and risked locking the account. It now stops and asks the user.
- Wrong password, wrong or expired code, and network errors now show distinct messages (previously all "Failed to connect").
- The single-use verification code is no longer stored in the config entry.

## [0.6.4] - 2026-10-24

### Fixed

- CI: line continuation in the unit-test command.

## [0.6.3] - 2026-10-24

### Fixed

- Slow recovery when the cloud session is invalidated. The cloud allows one session per account, so a login from the phone app or another client logs Home Assistant out. Before, HA only noticed on the 5-minute device-list refresh and then waited up to 300 s between re-login attempts, so every entity stayed unavailable for minutes. Status calls failing with `identityId is blank` (code 29003) were not recognized as session errors at all.
  - Every cloud call (polls, status checks, commands) now re-logs in and retries once on a session error, so HA recovers on the next call.
  - Session errors are detected by Aliyun error code (401, 403, 2401, 29003) as well as by message.
  - Re-login is serialized across threads and runs at most once per 10 s. Failed logins back off from 30 s up to 10 min.

## [0.6.2] - 2026-10-23

### Fixed

- Devices powered off at a wall switch (no smart switch involved) stayed "available" in Home Assistant with their last state (e.g. light shown ON) until HA was restarted. Once a device had been seen online, the coordinator wrote `status = 1` back into its cached device list and then trusted that value on every following poll, so `/thing/status/get` was never asked again. The cloud keeps serving the last property shadow for an unpowered device, so property polls could not catch it either.
  - Online state now comes from `/thing/status/get` on every poll (~30 s). The listBinding `status` is only a fallback.
  - Offline devices become unavailable within about one poll after the cloud marks them offline. The stale cloud shadow is no longer polled for them.
  - A single failed status query keeps the previous verdict; two in a row mark the device offline.
  - Online/offline transitions are logged at INFO level.

## [0.6.1] - 2026-10-06

### Changed

- README: aquarium screenshot and credit to [@bduartept1](https://github.com/bduartept1).

## [0.6.0] - 2026-10-06

### Added

- Aquarium: water-pump speed select and RGB scene controls; each aquarium switch writes only its own property.

## [0.5.3] - 2026-10-05

### Fixed

- Aquarium: `is_aquarium_device` imports, a sensor `KeyError` and the initial state.

## [0.5.2] - 2026-10-05

### Fixed

- Aquarium switch classes contained escaped backslashes.

## [0.5.0] - 2026-10-05

### Added

- Support for Aigostar Smart Aquarium (`a191KWgv5BZ` / `智能水族_ZS029_ZY_WB`):
  - **Light platform**: Light On/Off and brightness adjustment (`LightSwitch`, `brightness`).
  - **Switch platform**: Master Power (`powerstate`), Child Lock (`childLockOnOff`), Buzzer Sound (`CueSound`), Status Indicator LED (`IndMode`), Feeding Protection (`feedingFunction`), Overfeeding Reminder (`feedCntRemind`), and Rhythm Mode (`DeviceRhythmEnable`).
  - **Sensor platform**: Water temperature monitoring (`currentTemperature` in °C), water pump status (`waterPumpStatus`), feeding state (`feedState`), cumulative feed count (`feedCnt`), and error code diagnostics (`errorCode`).
  - Discovery support for composite appliances with multi-platform entities.

## [0.4.1] - 2026-09-10

### Fixed

- Every light/switch command failing with `'AigoSmartApiClient' object has no attribute '_local_ok'` — the LocalFirstMixin state was never initialized.
- Entities showing stale state for hours: availability now also tracks DataUpdateCoordinator update failures (expired cloud session makes entities unavailable instead of frozen).
- Automatic session re-login with stored credentials when the cloud session expires (previously required integration reload).
- Devices now flip to unavailable within 2 failed polls (~60 s) instead of 5 minutes.
- Terminal command failures now revert optimistic state, clear property locks and mark the device unavailable instead of silently dropping the command.
- Missing error handling on the direct (no-commander) write paths for switch, fan, climate, water_heater and number entities.
- hassfest CI failure: manifest.json keys must be sorted (domain, name, then alphabetical).

### Changed

- Writes go through the cloud API so the AigoSmart phone app and HA stay in sync (local ALCS writes did not update the cloud shadow).
