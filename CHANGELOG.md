# Changelog

All notable changes to emu-serial-bridge.

## [1.2.0] — 2026-04-21

### Added
- **Home Assistant handler** (`handlers/homeassistant.py`, handler v3.0.0)
  now ships with the bridge. Previously lived out-of-tree on individual
  installs. Requires `python3-yaml` and `python3-requests` (added to
  install.sh dependency list).
- Installer provisions `/etc/emu-serial-bridge/` (mode 755, owned by the
  installing user) and creates a stub `homeassistant.conf` (mode 600) on
  first install. Existing configs are preserved across reinstalls.

### Changed — breaking (HA handler only)
- HA action commands (`ON`, `OFF`, `TOGGLE`, `DIM`, `SCENE`, `PRESS`) now
  reply with the updated device line instead of a short confirmation:
  - Old: `OK HA 02 ON`
  - New: `OK|02|Coach|switch|ON|toggle|`
  This lets Mac-side clients update their UI from a single round-trip
  without an extra `HA LIST` call.
- All HA error replies standardized to `ERR|<CODE>[|<id>[|<msg>]]`
  (was: `ERR HA <CODE> <id>`). Codes unchanged: `UNKNOWN`, `BADARG`,
  `FAILED`, `UNREACHABLE`, `NOTCONFIGURED`, `EXCEPTION`, `NOCMD`, `BADCMD`.
- HA handler version bumped 2.0.0 → 3.0.0 to reflect breaking wire-format
  change.

### Fixed
- Bridge was emitting `\r\n` line terminators instead of the spec'd bare
  `\r`. This caused a phantom leading glyph on every line after the first
  in multi-line replies on the Mac side (bare LF interpreted as printable
  by HyperCard field widgets). Affected any Mac-side consumer that
  displayed multi-line replies; single-line battery/brightness/volume
  replies were unaffected.

## [1.1.0] — 2026 (prior release)
- SIGHUP reload() hook for plugins.

## [1.0.3]
- Read PWM period from sysfs instead of hardcoding 40000.

## [1.0.2]
- XDG prefs paths, stale file cleanup, prefs detection in UI.

## [1.0.1]
- Fix tray toggle, macos-bridge migration, sudoers removal.

## [1.0.0]
- Initial release. Serial bridge for classic Mac OS emulators.
