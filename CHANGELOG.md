# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-22

First public release.

### Added
- `discover`: read-only serial port enumeration with USB bridge/interface hints.
- `identify`: consent-gated ESP32 chip identification via `esptool`.
- `boards`: fail-closed board catalog (ESP32 family, ESP32-DevKitC V4,
  ESP32-S3-DevKitC-1, Raspberry Pi Pico, Arduino Uno R3) where every pin denies
  probing by default.
- `probe`: bounded, one-channel-at-a-time PCA9685-style servo probing over the
  `ROBOPROBE/1` serial protocol, gated by per-channel permission files, a
  manual arm token and power-cutoff confirmation.
- `run-demo`: full probe session against a built-in pseudo-terminal emulator.
- Atomic hardware map export with reload validation (`validate-map`).
- Optional `vision` commands: bounded camera capture, ArUco detection, replay
  verification and marker-motion export.
- Optional `model` commands: OBJ scan segmentation, operator corrections and
  confirmed URDF export with independent reload validation.
- ESP32 `ROBOPROBE/1` reference firmware (protocol only, no outputs driven).
- Examples, synthetic fixtures and a CI workflow for Python 3.11 and 3.12.
