# Changelog

## [0.6.2] - 2026-10-23

### Fixed

- Devices powered off at a wall switch (no smart switch involved) stayed "available" in Home Assistant with their last state (e.g. light shown ON) until HA was restarted. Once a device had been seen online, the coordinator wrote `status = 1` back into its cached device list and then trusted that value on every following poll, so `/thing/status/get` was never asked again. The cloud keeps serving the last property shadow for an unpowered device, so property polls could not catch it either.
  - Online state now comes from `/thing/status/get` on every poll (~30 s). The listBinding `status` is only a fallback.
  - Offline devices become unavailable within about one poll after the cloud marks them offline. The stale cloud shadow is no longer polled for them.
  - A single failed status query keeps the previous verdict; two in a row mark the device offline.
  - Online/offline transitions are logged at INFO level.

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
