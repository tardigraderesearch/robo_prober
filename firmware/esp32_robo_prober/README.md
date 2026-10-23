# ROBOPROBE/1 firmware (ESP32 family: servos on a PCA9685 or on GPIO pins)

PlatformIO (and Arduino IDE) project that speaks the `ROBOPROBE/1` serial
protocol `robo-prober` expects. It drives servos through a PCA9685 16-channel
PWM board over I2C (found at boot, on any pins) and straight on GPIO pins (50 Hz
LEDC PWM), and it surveys the pins read-only so the host can work out what is
wired where. It contains no robot-specific maps: which pin is which joint is the
host's business. Which pins exist, and which must never be touched (flash/PSRAM,
USB, the host UART), comes from `src/pin_profiles.h`, generated from the board
catalog (`python -m robo_prober.firmware_pins`).

**Use it only on a bench with a physical power cutoff within reach.** The
firmware limits what the host can ask for, but it cannot know whether your
mechanism can safely reach even the default band.

## Safety behaviour

| Rule | What the firmware does |
| --- | --- |
| Boot | All GPIOs inputs; OE pin (if wired) held high before anything else runs. Every pin the profile allows is surveyed read-only (pull-up, pull-down, ADC). Pairs of pulled-up pins are scanned for I2C devices at the weakest drive strength; every PCA9685 found is switched fully off and the first one becomes channels 0-15. |
| Survey | `SURVEY`, `WATCH`/`EVENTS`, `I2C_SCAN` and `BOARD` only read. A pin that holds its level against the internal pulls is *driven* unless the ADC proves a resistor (the opposing pull moves it 150 mV or more). |
| GPIO probes | `PROBE` (servo PWM at an explicit start pulse, channel 100+N) and `BLINK` (2 Hz on/off, then released) need ARM, drive one pin, at the weakest drive strength, and refuse pins the profile reserves, input-only pins, pins the firmware uses, and any pin a fresh survey finds driven. `RELEASE` returns a pin to input. |
| Arming | `MOVE` is refused until `ARM token=<t> cutoff=confirmed`. The token is a session token; every `MOVE` and `HB` must carry it. Without a PCA9685, `START` on channels 0-15 is refused (`driver_unavailable`). |
| Pulse band | Each channel has `min_us`/`neutral_us`/`max_us`. Default band: 1400 / 1500 / 1600 µs. The host can set another band with `LIMIT`; a driven channel only gets a band that contains its current position and target, so a band change never makes it jump. Targets outside the band are rejected (`out_of_limits`), and the written pulse is clamped to the band as a second guard. Absolute range 500–2500 µs. |
| First output | Arming outputs nothing. A channel gets PWM only from `START channel=<c> pulse_us=<us> token=<t>`, at exactly that host-chosen pulse (inside the band); `MOVE` before that is refused (`not_started`). The firmware never picks a start pulse. |
| Slew limit | After `START`, each `MOVE` target is approached at most `slew_us_s` µs per second (default 250, max 2000). |
| Watchdog | While armed, if no `MOVE` or `HB` arrives for `watchdog_ms` (default 1000 ms), every output is released (PWM off) and the session is disarmed. |
| Stop | `DISARM` and `STOP` release every output. `ESTOP` does too and latches: `ARM` is refused until `ESTOP_CLEAR cutoff=confirmed`. An optional normally-closed e-stop input (`RP_ESTOP_PIN`) latches the same e-stop when opened. |
| Bad input | Malformed, unknown or overlong lines, non-printable bytes, unknown or duplicate fields are rejected with `RP1 ERR` and change nothing. They never move a servo. |
| I2C errors | Any failed write releases everything and disarms (`fault=driver`). |

## Wiring

No fixed pins. Wire the PCA9685's SDA/SCL to any two free GPIOs (its board has
the pull-ups the boot scan looks for) and servo signal wires to any free GPIOs.
3V3 and GND to the PCA9685 logic side.

Servo power goes to the PCA9685 `V+` terminal (or the servos' own supply) from a
separate supply through your physical cutoff, never from the ESP32's 3V3 or 5V
pins. Options, as `build_flags` in `platformio.ini`:

| Flag | Default | Meaning |
| --- | --- | --- |
| `RP_I2C_SDA`, `RP_I2C_SCL` | found at boot | scan only this PCA9685 pin pair |
| `RP_PCA9685_OE_PIN` | `-1` | GPIO wired to OE (active low), `-1` = not wired |
| `RP_PCA9685_OSC_HZ` | `25000000` | oscillator calibration |
| `RP_ESTOP_PIN` | `-1` | normally-closed e-stop to GND; pin high latches ESTOP |
| `RP_WATCHDOG_MS` | `1000` | host watchdog |
| `RP_OUTPUTS_NONE` | off | protocol-only build that drives nothing |

## Build and flash

```bash
pip install platformio
cd firmware/esp32_robo_prober
pio run                                                    # builds every chip below
pio run -e esp32-s3-devkitc-1 -t upload --upload-port /dev/ttyACM0   # Linux
pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0   # Linux
pio run -e esp32dev -t upload --upload-port COM3           # Windows
pio run -e esp32dev -t upload --upload-port /dev/cu.usbserial-0001   # macOS
```

Environments: `esp32dev`, `esp32-s2-saola-1`, `esp32-s3-devkitc-1`,
`esp32-c3-devkitm-1`, `esp32-c6-devkitc-1` (all on the pinned pioarduino
platform, Arduino core 3.x). `pio run -e esp32dev-protocol-only` builds a variant that acknowledges
everything but drives no outputs, for testing the host path with nothing
attached.

Arduino IDE: open `esp32_robo_prober.ino` (the code lives in `src/`), install
the ESP32 board package (3.x), pick your board and upload. No extra libraries are
needed; the PCA9685 driver is a few register writes over `Wire`.

## Testing without hardware

`src/rp_core.cpp` (the protocol and every safety rule) has no Arduino
dependency. The Python test suite compiles it with the desktop C++ compiler
together with `test/native/sim_main.cpp`, runs the same command scripts
through it and through the Python device model, and requires identical replies.

The simulator's pins and I2C bus are `test/native/fake_board.h` (a servo input,
an LED, a button, another chip's output and a PCA9685), mirrored by the Python
model.

```bash
c++ -std=c++17 -Isrc src/rp_core.cpp test/native/sim_main.cpp -o rp_sim
printf 'HELLO\nARM token=t cutoff=confirmed\nMOVE channel=0 target_us=1550 seq=1 token=t\n@TICK 200\nSTATUS channel=0\n' | ./rp_sim
```

## Protocol

See the [main README](../../README.md#roboprobe1-protocol) for the command table.
