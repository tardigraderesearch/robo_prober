# ROBOPROBE/1 reference firmware (ESP32)

A minimal PlatformIO project for any classic ESP32 dev board (`esp32dev`).
It speaks the `ROBOPROBE/1` serial protocol that `robo-prober probe` expects.

**It does not drive any outputs.** MOVE commands are validated and
acknowledged, nothing else. That lets you test discovery, identification,
arming and the probe session end to end with nothing attached. Add your own
PCA9685 output layer (see the comment in `src/main.cpp`) once you have a bench
setup with a physical power cutoff.

## Build and flash

```bash
pip install platformio
cd firmware/esp32_robo_prober
pio run                                   # compile
pio run -t upload --upload-port /dev/ttyUSB0   # flash (your port may differ)
```

## Protocol

ASCII, one command per line, 115200 baud:

| Host sends | Firmware replies |
| --- | --- |
| `HELLO` | `RP1 OK proto=ROBOPROBE/1 mode=disarmed id=<id>` |
| `ARM token=<token> cutoff=confirmed` | `RP1 OK armed=1` |
| `MOVE channel=<0-15> target_us=<us> seq=<n>` | `RP1 OK seq=<n> channel=<c> target_us=<us>` (exact echo) |
| `STOP` | `RP1 OK stopped=1` (outputs stop, disarmed) |
| `DISARM` | `RP1 OK armed=0` |

Errors are `RP1 ERR reason=<reason>`. MOVE while disarmed is rejected.
