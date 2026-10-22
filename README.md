# robo-prober

Find the robot controller on your USB bus, probe its servos only within limits
you've approved, and export what you learned as a validated hardware map or URDF.

[![CI](https://github.com/evilhobo1911/robo-prober/actions/workflows/ci.yml/badge.svg)](https://github.com/evilhobo1911/robo-prober/actions/workflows/ci.yml)

## Who it's for

Hobbyists, students and researchers who have a servo-driven robot (an arm, a
legged robot, a pan/tilt rig) on an ESP32 or similar board and want to work out
which channel moves which joint without guessing. If you'd rather not send
random PWM to an unknown board and hope nothing breaks, this tool does the
mapping one small, approved step at a time.

## Safety model

robo-prober is **fail-closed**. If something isn't explicitly allowed, it doesn't
happen.

- **Discovery is read-only.** `discover` lists serial ports without opening them.
- **A USB ID is only a hint.** A VID/PID usually identifies a USB-to-UART bridge
  chip, not the board behind it. You pick the exact board yourself.
- **Identification needs consent.** `identify` resets the chip (via `esptool`),
  so it only runs when you pass `--consent-controller-identification YES_RESET_ALLOWED`.
- **A pinout is not a permission.** The board catalog describes pins, and every
  pin record denies probing, reading, writing and PWM. There is no generic GPIO
  probing at all.
- **Motion needs a permission file.** Servo channels move only if a JSON file
  lists each channel with its neutral, min, max and step pulse widths, who
  approved it, and when. Bounds are checked before anything is sent.
- **Arming is manual.** Probing needs an arm token and `--power-cutoff-confirmed`,
  meaning you have a physical power cutoff within reach.
- **Small moves, one channel at a time.** Each approved channel moves one step
  from neutral and comes straight back.
- **Any doubt stops the session.** A malformed reply, timeout or mismatched
  acknowledgement aborts the session and sends `STOP`. It never sends a
  "recovery" move.
- **People confirm joints, not heuristics.** Marker motion from a camera is
  evidence. A joint assignment only goes into the map once a person confirms it.

## Quickstart

### 1. Install

```bash
pipx install "git+https://github.com/evilhobo1911/robo-prober"
# or, with the optional extras (camera, scan-to-URDF, esptool):
pipx install "robo-prober[all] @ git+https://github.com/evilhobo1911/robo-prober"
```

From a clone:

```bash
python -m venv .venv && . .venv/bin/activate
pip install ".[all,test]"
pytest
```

Python 3.11 or newer. Developed and tested on Linux.

### 2. Try it with no hardware

The demo runs a full probe session against a built-in protocol emulator on a
pseudo-terminal, then exports and reloads a hardware map:

```bash
robo-prober run-demo --out demo-map.json
robo-prober validate-map demo-map.json
```

### 3. Flash the reference firmware to an ESP32

`firmware/esp32_robo_prober/` is a PlatformIO project for any classic ESP32 dev
board. It speaks the `ROBOPROBE/1` protocol. It is a protocol reference: it
acknowledges moves but drives no outputs until you add a PCA9685 output layer
(see [its README](firmware/esp32_robo_prober/README.md)).

```bash
pip install platformio
cd firmware/esp32_robo_prober
pio run -t upload --upload-port /dev/ttyUSB0
```

### 4. Discover, identify, choose the board

```bash
robo-prober discover
robo-prober identify --port /dev/ttyUSB0 --consent-controller-identification YES_RESET_ALLOWED
robo-prober boards --chip ESP32
```

### 5. Approve channels, then probe

Copy and edit the examples in [`examples/`](examples/). Every value in the
permission file should be a limit you've checked by hand on your hardware.

```bash
robo-prober validate-permissions examples/permissions.esp32-pca9685.json
robo-prober probe \
  --port /dev/ttyUSB0 --protocol ROBOPROBE/1 \
  --permissions examples/permissions.esp32-pca9685.json \
  --controller-identity examples/controller-identity.json \
  --confirmations examples/confirmations.json \
  --observations observation.json \
  --arm-token "$(date +%s)" --power-cutoff-confirmed \
  --out my-robot.map.json
```

`--observations` takes one or more marker observations: either recorded with
`robo-prober vision capture` + `vision export`, or written by hand in the same
format as [the bundled synthetic fixture](src/robo_prober/data/synthetic_observation.json).

## Example output

Discovery with an ESP32 dev board on a CP210x bridge:

```text
1. /dev/ttyUSB0 10c4:ea60 interface_recognized - Silicon Labs CP210x USB/UART bridge
   USB ID note: bridge/interface hint only; exact board must be selected manually.
```

`run-demo`:

```json
{
  "out": "demo-map.json",
  "reloaded_joints": ["base_yaw", "elbow", "shoulder"],
  "status": "demo_complete"
}
```

The exported map (abridged):

```json
{
  "schema_version": 1,
  "status": "software_verified_physical_unverified",
  "identities": {"board_profile_id": "esp32_devkitc_v4_wroom32", "controller": {"chip": "ESP32"}},
  "joints": {
    "base_yaw": {"channel": 0, "direction": "positive", "invert": false,
                 "neutral_us": 1500, "min_us": 1460, "max_us": 1540, "route": "pca9685_servo"}
  },
  "permission_hash": "…sha256 of the approved permissions…",
  "observations": ["…marker motion + every transport event…"]
}
```

Exports are atomic (temp file, fsync, rename) and are validated again after
writing. `validate-map` checks the schema, the joint/channel uniqueness and the
permission hash.

## Supported hardware

| Layer | Supported |
| --- | --- |
| Controller | Anything that speaks `ROBOPROBE/1` over serial. Reference firmware: classic ESP32 (`esp32dev`). |
| Servo driver | PCA9685-style 16-channel servo boards (`route: "pca9685_servo"`, channels 0–15, 500–2500 µs). |
| Chip identification | ESP32, ESP32-S2, ESP32-S3, ESP32-C3, ESP32-C6 (via `esptool`). |
| Board catalog | ESP32 SoC references, ESP32-DevKitC V4, ESP32-S3-DevKitC-1, Raspberry Pi Pico / Pico W, Arduino Uno R3. |
| USB bridges recognized | CP210x, CH340, CH343, FT232, Espressif native USB, Arduino Uno, Pico. |
| Cameras (optional) | V4L2 webcams on Linux, ArUco markers via OpenCV. |

## ROBOPROBE/1 protocol

Newline-terminated ASCII over serial (default 115200 baud):

```text
HELLO                                     -> RP1 OK proto=ROBOPROBE/1 mode=disarmed id=<id>
ARM token=<token> cutoff=confirmed        -> RP1 OK armed=1
MOVE channel=<0-15> target_us=<us> seq=<n> -> RP1 OK seq=<n> channel=<c> target_us=<us>
STOP                                      -> RP1 OK stopped=1
DISARM                                    -> RP1 OK armed=0
(any error)                               -> RP1 ERR reason=<reason>
```

The host requires `MOVE` acknowledgements to echo `seq`, `channel` and
`target_us` exactly. Anything else aborts the session.

## Camera observations (optional)

With `pip install "robo-prober[vision]"`, the `vision` commands record bounded,
camera-only sessions (they never open a serial port), detect ArUco markers,
and export reference-relative marker motion in image space:

```bash
robo-prober vision cameras
robo-prober vision capture --device /dev/video0 --config examples/vision.json --out session --frames 30 --seconds 5
robo-prober vision replay session --out session-replay
robo-prober vision export session --out observation.json --before 0 --after -1
```

Units are pixels and image-plane degrees. There is no metric calibration.

## Scan to URDF (optional)

With `pip install "robo-prober[geometry]"`, the `model` commands take an OBJ
scan of your robot, split it into candidate link regions you can correct with
planes, boxes, face lists and merges, and, once you confirm scale, link frames,
joint axes and limits, export a portable visual URDF that is reloaded and
checked with `yourdfpy`.

```bash
robo-prober model propose scan.obj --parts 4 --out proposal.json
robo-prober model segment --config proposal.json --out candidates
robo-prober model articulate --config my-confirmed-model.json --out urdf
```

No scan is bundled. `python examples/build_synthetic_articulation.py` builds a
synthetic two-link arm and runs the whole pipeline. See
[docs/scan-segmentation.md](docs/scan-segmentation.md) for the config format.

## Limitations

- The reference firmware doesn't drive servos yet; you add the PCA9685 output layer.
- Only one motion route exists (PCA9685-style servo channels). No GPIO, no
  stepper or smart-servo buses.
- robo-prober never discovers neutral positions or limits on its own. You supply them.
- Camera observations are image-space only (no calibration, no world-frame pose).
- URDF export is visual and kinematic only: no inertia, collision or dynamics.
- Linux is the tested platform. PTY-based tests and V4L2 camera listing need Linux.
- A successful demo proves the software path, not that your hardware is safe to move.

## License

MIT. See [LICENSE](LICENSE).

## How this was built
AI is used here as a tool to speed up implementation. I design the system and
define what "working" means. When issues are found, they're identified, and
ARI — my own agent system — fixes them against those checks.
