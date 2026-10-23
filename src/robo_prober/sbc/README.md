# Linux SBC agent

Install on the target SBC with `pip install '.[sbc]'`. Run from outside the
checkout root (which contains the legacy `robo_prober.py` launcher):

```sh
python -m robo_prober.sbc
ssh pi@host python -m robo_prober.sbc
python -m robo_prober.sbc --tcp 8765
python -m robo_prober.sbc --backend emulated
```

TCP binds to `127.0.0.1` unless `--host` is supplied. Use an SSH tunnel for
remote TCP access. One connection owns a session at a time; EOF, disconnect,
SIGINT, and SIGTERM release outputs. E-stop remains latched between TCP
connections. Protocol replies alone go to stdout; startup failures go to stderr.

`auto` uses lgpio `tx_servo` on Raspberry Pi when installed, otherwise the
software PWM backend. `--backend software` selects the Python timing fallback;
`--backend gpiod` supports GPIO input, edges, and digital pulses without servo
PWM. Both lgpio and Python servo timing use Linux software scheduling and can
have jitter; neither is a hardware PWM timing guarantee. Optional libraries
are imported only when a corresponding hardware backend or scan is used.

Board detection uses the device-tree model and the Tegra release-file marker.
The profile IDs match lane C: `raspberry_pi_4`, `raspberry_pi_5`,
`raspberry_pi_zero_2w`, `jetson_orin_nano_devkit`, `jetson_orin_nx_devkit`, and
`jetson_agx_orin_devkit`. `--board` overrides identity. GPIO chips are selected
by label (Pi 5: `pinctrl-rp1`), not a fixed gpiochip number. Known profiles with
multiple chips are routed separately, including the Jetson always-on bank.
`--chip /dev/gpiochipN` selects a single chip explicitly; profile records on
other chips remain unavailable. Missing/ambiguous chip labels fail startup.

The catalog adapter calls `get_profile(board_id)` in `catalog`, then
`catalog_sbc` if needed. With a profile, wire pin numbers are its dictionary
keys (physical header positions for lane C). Only exposed, nonreserved,
usable GPIO records qualify. Kernel line names take precedence over numeric
offsets. Missing names stay unavailable. Without a profile, wire pin numbers
are offsets on the selected chip, and every operation excludes currently
claimed lines. `BOARD pins=` reports the usable, currently unclaimed inventory.
Catalog `safe_*` flags remain metadata; runtime ARM and electrical checks are
still mandatory.

## Protocol and safety choices

The firmware reference core supplies ARM/cutoff/token, LIMIT, START, MOVE,
STATUS, HB, STOP, ESTOP, ESTOP_CLEAR, and DISARM semantics. Every whole-session
release resets every band; RELEASE resets the associated channel's band.
No default neutral pulse is sent at startup or ARM. Active probes are exclusive:
finish/release the current pin before probing another.

```text
SURVEY pin=7
ARM token=session1 cutoff=confirmed
PROBE pin=7 mode=servo start_us=1500 token=session1
LIMIT channel=0 min_us=1400 neutral_us=1500 max_us=1600 slew_us_s=100
MOVE channel=0 target_us=1550 seq=1 token=session1
HB token=session1
RELEASE pin=7 token=session1
DISARM
```

Read the `channel` returned by PROBE; do not assume it is always 0. The first
binding gets channel 0, subsequent pins get the next free slot. RELEASE retains
the binding for a later explicit START until the slot must be reused; reused
slots begin with the default band. START on an unbound channel is refused with
`no_pwm_slot`. Existing firmware error names are preserved; new active commands
use `not_armed` when disarmed.

The shared spec leaves these details unspecified; this implementation chooses:

- **BLINK completion:** `blinked=N` acknowledges scheduling immediately. The
  first high pulse starts then; each high/low phase lasts at least 250 ms.
  Ticks never compress missed phases into a burst. The pin releases after the
  final low phase. Continue sending HB during longer blinks. STOP/ESTOP and
  the watchdog interrupt the pattern immediately at the next service tick.
- **Survey classification:** input-only samples use disabled bias, pull-up,
  and pull-down. A level that resists both biases is `driven_high`/`driven_low`.
  Strong external pulls cannot be distinguished from drivers by digital GPIO
  alone, so they are conservatively refused too (including strong I2C pulls).
  Stable unbiased levels that follow both biases are `pulled_up`/`pulled_down`;
  changing unbiased levels are `floating`. Contradictory readings are
  `reserved`. This is a digital heuristic, not proof of a component's identity;
  active operations resurvey before enabling output. No ADC is assumed.
- **I2C quick read:** use smbus2 `read_byte` (SMBus receive-byte, no preceding
  register write). `write_quick` would send a write; zero-length I2C reads are
  not portable across adapters. Scan only `0x08..0x77`, never force access to
  kernel-owned slave addresses. Unsupported/broken buses return `driver_error`.
- **Potentially slow input operations:** SURVEY, WATCH on, and I2C_SCAN refuse
  while a servo or blink is active (`no_pwm_slot`), so a blocking bus ioctl or
  large survey cannot delay output watchdog handling. EVENTS and WATCH off
  remain available. WATCH uses pull-up inputs and skips driven/claimed pins.
- **Wire encoding:** BOARD model whitespace/punctuation becomes underscores;
  empty inventories and event/device lists use `none`. EVENTS drains at most
  64 edges per call; its queue holds the newest 4096 edges. The legacy parser
  is extended specifically for the digit in `I2C_SCAN` and for `WATCH on|off`.

The service ticks every 10 ms even when the peer sends nothing or leaves a
partial line. Reads and writes are nonblocking and buffers are bounded, so a
peer that stops reading replies cannot stall the watchdog. Software PWM worker
errors trigger the same release path. Each GPIO release changes the line to
input with bias disabled before freeing its request; a release error records
the reference core's `driver` fault while cleanup continues for other lines.

## Hardware-free validation

`EmulatedBackend` models hidden wiring for servo, LED, button, pulled, driven,
claimed, and I2C pins. Tests observe pulses/edges rather than granting access
based on hidden component names. Additional injected gpiod/lgpio/smbus2 modules
verify chip selection, bias requests, claim races, input restoration, software
worker cleanup, and the absence of I2C writes. No test opens hardware devices.

API references: [libgpiod v2 Python API](https://libgpiod.readthedocs.io/en/v2.3.1/python_api.html),
[lgpio](https://pypi.org/project/lgpio/0.2.2.0/), and
[smbus2](https://smbus2.readthedocs.io/en/latest/).
