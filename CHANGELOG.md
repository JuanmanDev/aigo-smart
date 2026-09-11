# Changelog

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
