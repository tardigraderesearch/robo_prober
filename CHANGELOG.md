# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Find servos without knowing the wiring. The window opens on **1. Discover**
  (read-only pin survey, the PCA9685 found at boot, I2C devices, a button
  watch), then **2. Identify** (one pin or PCA9685 channel at a time: wiggle as
  a servo or blink, say what happened, name it, save the hardware map), then
  **3. Range of motion** with only the named servos. The ROM step says "Find
  servos first, or open a saved map" instead of showing 16 empty rows.
- Firmware 0.3.0 (ROBOPROBE/1 additions): `BOARD`, `SURVEY`, `WATCH`/`EVENTS`,
  `I2C_SCAN`, `PROBE` (servo PWM straight on a GPIO, channel 100+N), `BLINK`,
  `RELEASE`, with the new refusals `pin_reserved`, `pin_driven`, `pin_claimed`,
  `no_pwm_slot`, `not_armed`, `bad_pin`. A pin held by something else is never
  driven; a pin counts as resistively pulled only with ADC proof.
- The firmware finds its PCA9685 at boot: it scans pairs of pulled-up pins, both
  orders, addresses 0x08-0x77, identifies a PCA9685 by its registers and switches
  it fully off. HELLO reports `i2c=<sda>,<scl>` and `pca9685=<addresses>`.
- Firmware for ESP32, ESP32-S2, ESP32-S3, ESP32-C3 and ESP32-C6, with per-chip
  pin tables generated from the board catalog (`python -m robo_prober.firmware_pins`;
  a test checks they match).
- Hardware maps may list non-servo parts under `devices` (led, button, i2c, ...).

### Changed
- PlatformIO uses the pioarduino platform (Arduino core 3.x, pinned) for every
  environment, since ESP32-C6 needs it. `RP_PCA9685_ADDR` is gone (the first
  PCA9685 found is used; all are reported).
- Without a PCA9685, `ARM` now succeeds (GPIO servos work); `START` on channels
  0-15 is refused with `driver_unavailable`.

### Fixed
- `ARM` failed with `driver_unavailable` when the PCA9685 was not on the board's
  default I2C pins.
- The catalog's ESP32-S2 profile now reserves its USB (19/20) and UART0 (43/44) pins.
- One entry point: `python robo_prober.py` on every OS. It asks (Y/n) before
  installing its libraries into a private `.venv`, or says it's good to go and starts.
  `run.sh`, `run.bat`, `run.command` and `launcher.py` are gone.
- `run.bat` failed on Windows because cmd mis-parsed Python one-liners inside
  `for /f` and parenthesised blocks. `run.sh`, `run.command` and `run.bat` now only
  find Python 3.11+ and hand over to a shared, stdlib-only `launcher.py`, so setup
  behaves the same on every OS.

## [0.2.0] - 2026-10-22

### Added
- Firmware drives servos: a register-level PCA9685 output layer over I2C for
  ESP32 and ESP32-S3 (PlatformIO and Arduino IDE). Outputs are off at boot,
  `ARM` needs `cutoff=confirmed` and a session token that every `MOVE` and
  `HB` must carry, each channel has a pulse band (default 1400/1500/1600 µs)
  that the firmware rejects and clamps against (any release restores the
  default band, so a widened band never outlives its session), moves are slew limited, a
  heartbeat watchdog releases all outputs when the host goes quiet, `STOP`,
  `DISARM` and a latching `ESTOP` (plus an optional hardware e-stop input)
  release everything, and malformed input never moves anything. No channel
  outputs anything until the host sends `START` with an explicit start pulse.
- ROBOPROBE/1 additions, compatible with v0.1: `LIMIT`, `START`, `STATUS`, `HB`,
  `ESTOP`, `ESTOP_CLEAR`, an optional `token` on `MOVE`, and `fw`, `outputs`,
  `watchdog_ms`, `caps` and `fault` fields in the `HELLO` reply. The host
  uses the new commands only when `caps` advertises them.
- The firmware's protocol and safety core is plain C++; the tests compile it
  as a desktop simulator and require identical replies from it and from the
  Python device model for every scenario.
- `rom-gui`: a range-of-motion window. One slider per channel that the servo
  follows slowly, "Set ROM 1" / "Set ROM 2" (safe range = min..max of the two
  marks, in any order), a ±300 µs start band with an explicit "Widen range",
  firmware clamping once both marks exist, an always-visible E-STOP, disarm on
  close, and saving to the hardware map. Each channel needs an explicit
  Enable at a shown start pulse (last saved position, else ROM midpoint, else
  operator-confirmed); last positions are saved to the map on disarm, E-STOP
  and close. Dark theme with green accent and window controls for tiling
  desktops.
- `rom`: record a range of motion from the command line, optionally pushing
  it to the controller's band.
- `estop` (latch or clear the controller e-stop) and `doctor` commands.
- Python API: `discover`, `connect`, `Controller` (`arm`, `disarm`, `stop`,
  `estop`, `start`, `set_limits`, `set_pulse` with slew limiting, `status`),
  `load_map`, `save_map`, `get_rom`, `set_rom`, `set_last_positions`,
  `RomSession`, `load_markers`.
- Optional `markers.json` tag manifests: vision configs can name tags instead
  of marker IDs.
- `run.sh`, `run.bat` and `run.command` launchers that create a private
  environment and install only when something changed.
- CI: tests on Linux, Windows and macOS with Python 3.11 and 3.12 (GUI smoke
  test under Xvfb), a PlatformIO firmware build, and launcher checks.

### Changed
- `probe` sends the approved permission band to v0.2 firmware before arming
  and waits for each slew-limited move to arrive.
- The built-in emulator now models the v0.2 firmware and also runs
  in-process, so `run-demo` works on Windows.
- Camera capture works on Windows and macOS with a camera index; V4L2 listing
  stays Linux-only.
- Hardware maps may carry validated `rom` and `last_position` blocks per joint
  (schema version unchanged).
- Pillow is now a core dependency (rounded widgets in the window theme).

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
