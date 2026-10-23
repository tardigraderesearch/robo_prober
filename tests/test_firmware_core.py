"""Firmware safety semantics.

The same command scripts run through the Python device model
(robo_prober.reference_device) and, when a C++ compiler is available, through
the real firmware core (firmware/.../src/rp_core.cpp) built as a native
simulator. Replies must match line for line, and the explicit assertions
below pin down the safety behaviour itself.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from robo_prober.reference_device import ReferenceDevice

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32_robo_prober"
TOKEN = "t0k"


def run_model(lines):
    device = ReferenceDevice(device_id="reference-emulator", outputs="emulated")
    now = 0
    out = []
    for line in lines:
        if line.startswith("@TICK "):
            now = int(line.split()[1])
            device.tick(now)
            out.append(f"SIM t={now}")
        elif line.startswith("@OUT channel="):
            pulse = device.output(int(line.split("=")[1]))
            out.append("SIM out=" + ("off" if pulse is None else str(pulse)))
        elif line.startswith("@PIN "):
            parts = line.split()
            if parts[2] == "open":
                device.board.open_pin(int(parts[1]))
            else:
                device.board.set_source(int(parts[1]), int(parts[2]), int(parts[3]))
            out.append(f"SIM pin={parts[1]}")
        elif line in ("@DRIVER fail", "@DRIVER ok"):
            device.driver_fails = line.endswith("fail")
            out.append("SIM driver=" + ("fail" if device.driver_fails else "ok"))
        elif line == "@ESTOP_PIN":
            device.external_estop()
            out.append("SIM estop_pin=1")
        else:
            out.append(device.handle(line, now))
    return out


@pytest.fixture(scope="session")
def sim_binary(tmp_path_factory):
    compiler = next((shutil.which(name) for name in ("c++", "g++", "clang++") if shutil.which(name)), None)
    if compiler is None:
        pytest.skip("no C++ compiler; firmware core conformance runs in CI")
    exe = tmp_path_factory.mktemp("sim") / ("rp_sim.exe" if sys.platform == "win32" else "rp_sim")
    result = subprocess.run(
        [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(FIRMWARE / "src"),
         str(FIRMWARE / "src" / "rp_core.cpp"), str(FIRMWARE / "test" / "native" / "sim_main.cpp"), "-o", str(exe)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.fail("firmware core failed to compile:\n" + result.stderr)
    return exe


def run_sim(exe, lines):
    result = subprocess.run([str(exe)], input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=30, check=True)
    return result.stdout.splitlines()


ARM = f"ARM token={TOKEN} cutoff=confirmed"


def move(channel, target, seq, token=TOKEN):
    return f"MOVE channel={channel} target_us={target} seq={seq} token={token}"


def start(channel, pulse, token=TOKEN):
    return f"START channel={channel} pulse_us={pulse} token={token}"


def out(channel):
    return f"@OUT channel={channel}"


def tick(ms):
    return f"@TICK {ms}"


OFF = "SIM out=off"


def has(*parts):
    return lambda reply: all(part in reply for part in parts)


def err(reason):
    return f"RP1 ERR reason={reason}"


ANY = None

# Each scenario is a list of (command, expectation). An expectation is an exact
# reply, a predicate, or ANY. The commands alone are also run through the
# compiled firmware core and must produce identical replies.
SCENARIOS = {
    "boot_and_arm": [
        ("HELLO", has("mode=disarmed", "caps=limit,status,hb,token,estop,start,survey,watch,i2c,probe,blink",
                      "watchdog_ms=1000", "fault=none", "i2c=8,9", "pca9685=0x40")),
        (out(0), OFF),
        (move(0, 1500, 1), err("disarmed")),
        (start(0, 1500), err("disarmed")),
        ("ARM token=x", err("missing_arm_fields")),
        ("ARM token=x cutoff=no", err("cutoff_unconfirmed")),
        ("ARM token=bad! cutoff=confirmed", err("bad_token")),
        (f"ARM token={'a' * 33} cutoff=confirmed", err("bad_token")),
        (ARM, "RP1 OK armed=1"),
        (ARM, err("already_armed")),
        ("HELLO", has("mode=armed")),
        ("HELLO extra=1", err("bad_fields")),
        (out(0), OFF),  # arming alone outputs nothing
    ],
    "no_pwm_until_explicit_start": [
        (ARM, ANY),
        (move(0, 1500, 1), err("not_started")),          # never a hidden default start pulse
        (out(0), OFF),
        (start(0, 1700), err("out_of_limits")),           # start must be inside the band
        (start(0, 1450, token="other"), err("bad_token")),
        ("START channel=0 pulse_us=1450", err("bad_fields")),
        (start(0, 14.5), err("bad_start_values")),
        (out(0), OFF),
        (start(0, 1450), "RP1 OK channel=0 started_us=1450"),
        (out(0), "SIM out=1450"),                          # first PWM is exactly the start pulse
        ("STATUS channel=0", has("active=1", "current_us=1450", "target_us=1450")),
        (start(0, 1500), err("already_started")),
        (out(1), OFF),                                     # other channels stay released
        ("DISARM", ANY),
        (ARM, ANY),
        (move(0, 1450, 2), err("not_started")),            # a new session starts from nothing again
    ],
    "session_token": [
        (ARM, ANY), (start(0, 1500), ANY),
        ("MOVE channel=0 target_us=1500 seq=1", err("token_required")),
        (move(0, 1500, 2, token="other"), err("bad_token")),
        (move(0, 1500, 3), "RP1 OK seq=3 channel=0 target_us=1500"),
        ("HB token=other", err("bad_token")),
        (f"HB token={TOKEN}", "RP1 OK hb=1"),
        ("HB", err("bad_fields")),
    ],
    "limits_and_clamp": [
        ("LIMIT channel=1 min_us=499 neutral_us=1000 max_us=1400", err("bad_limits")),
        ("LIMIT channel=1 min_us=1400 neutral_us=1400 max_us=1400", err("bad_limits")),
        ("LIMIT channel=1 min_us=1000 neutral_us=1500 max_us=1400", err("bad_limits")),
        ("LIMIT channel=1 min_us=1000 neutral_us=1200 max_us=2501", err("bad_limits")),
        ("LIMIT channel=1 min_us=1000 neutral_us=1200 max_us=1400 slew_us_s=0", err("bad_slew")),
        ("LIMIT channel=1 min_us=1000 neutral_us=1200 max_us=1400 slew_us_s=2001", err("bad_slew")),
        ("LIMIT channel=16 min_us=1000 neutral_us=1200 max_us=1400", err("bad_channel")),
        ("LIMIT channel=1 min_us=1000 neutral_us=1200 max_us=1400 slew_us_s=2000",
         "RP1 OK channel=1 min_us=1000 neutral_us=1200 max_us=1400 slew_us_s=2000"),
        (ARM, ANY),
        (start(1, 1150), "RP1 OK channel=1 started_us=1150"),
        (move(1, 1500, 1), err("out_of_limits")),
        (move(1, 999, 2), err("out_of_limits")),
        (move(1, 1050, 3), has("RP1 OK seq=3")),
        (out(1), "SIM out=1150"),
        ("LIMIT channel=1 min_us=900 neutral_us=1200 max_us=1400 slew_us_s=2000", has("RP1 OK")),  # driven, but inside
        (tick(25), ANY), (out(1), "SIM out=1100"),          # 2000 us/s for 25 ms
        (tick(100), ANY), (out(1), "SIM out=1050"),
        (start(0, 1601), err("out_of_limits")),              # default band 1400..1600
        (start(0, 1399), err("out_of_limits")),
        (start(0, 1600), ANY),
        ("LIMIT channel=1 min_us=1060 neutral_us=1200 max_us=1400", err("position_outside_limits")),
        ("LIMIT channel=1 min_us=900 neutral_us=1000 max_us=1040", err("position_outside_limits")),
        ("LIMIT channel=1 min_us=1000 neutral_us=1050 max_us=1050", has("RP1 OK", "slew_us_s=250")),
        (move(1, 1060, 7), err("out_of_limits")),
        ("STATUS channel=1", has("min_us=1000", "max_us=1050")),
    ],
    "slew_accumulates": [
        (ARM, ANY), (start(0, 1500), ANY), (move(0, 1600, 1), ANY),
        (tick(1), ANY), (tick(2), ANY), (tick(3), ANY),
        ("STATUS channel=0", has("current_us=1500")),
        (tick(4), ANY), ("STATUS channel=0", has("current_us=1501")),   # 250 us/s for 4 ms = 1 us
        (tick(400), ANY), ("STATUS channel=0", has("current_us=1600")),
        (f"HB token={TOKEN}", ANY), (tick(800), ANY),
        (move(0, 1450, 2), ANY), (tick(900), ANY), (tick(1300), ANY),
        (out(0), "SIM out=1475"),                                         # 500 ms at 250 us/s = 125 us
        ("STATUS channel=0", has("current_us=1475", "target_us=1450")),
    ],
    "watchdog": [
        (ARM, ANY), (start(0, 1500), ANY), (move(0, 1550, 1), ANY),
        (tick(999), ANY), (f"HB token={TOKEN}", ANY), (tick(1999), ANY),
        ("STATUS channel=0", has("armed=1")),                   # fed at 999, alive at 1999
        (tick(2000), ANY),
        ("STATUS channel=0", has("active=0", "armed=0")),       # 2000 - 999 > 1000 ms
        (out(0), OFF),
        (move(0, 1550, 2), err("disarmed")),
        ("HELLO", has("fault=watchdog")),
        (tick(2500), ANY), (ARM, "RP1 OK armed=1"), ("HELLO", has("fault=none")),
        (tick(3400), ANY), (f"HB token={TOKEN}", ANY), (tick(4401), ANY),
        ("HELLO", has("mode=disarmed", "fault=watchdog")),
    ],
    "parse_errors_never_move": [
        (ARM, ANY), (start(0, 1500), ANY), (move(0, 1550, 1), ANY), (tick(200), ANY),
        ("STATUS channel=0", has("current_us=1550 target_us=1550")),
        (move(0, 1590, 2) + " extra=1", err("bad_move_fields")),
        (f"MOVE channel=0  target_us=1590 seq=3 token={TOKEN}", err("bad_syntax")),
        (f"MOVE channel=0 target_us=15x0 seq=4 token={TOKEN}", err("bad_move_values")),
        (f"MOVE channel=0 target_us=1590 target_us=1590 seq=5 token={TOKEN}", err("duplicate_field")),
        ("move channel=0 target_us=1590 seq=6", err("unknown_command")),
        (f"MOVE channel=0 target_us=1590 seq=7 token={TOKEN}=x", err("bad_syntax")),
        (f"MOVE channel=0 target_us=99999999999 seq=8 token={TOKEN}", err("bad_move_values")),
        (f"MOVE channel=17 target_us=1590 seq=9 token={TOKEN}", err("bad_channel")),
        (f"MOVE channel=-1 target_us=1590 seq=10 token={TOKEN}", err("bad_channel")),
        (f"MOVE Channel=0 target_us=1590 seq=11 token={TOKEN}", err("bad_syntax")),
        (f"MOVE channel= target_us=1590 seq=12 token={TOKEN}", err("bad_syntax")),
        ("MOVE channel=0 target_us=1590", err("bad_move_fields")),
        (f"MOVE channel=0 target_us=1590 seq=13 token={TOKEN} a=1 b=2 c=3 d=4 e=5", err("too_many_fields")),
        (f"START channel=1 pulse_us=1500 token={TOKEN} x=1", err("bad_fields")),
        ("", err("empty_command")), ("   ", err("empty_command")), ("MOVE2 channel=0", err("unknown_command")),
        ("STATUS channel=0", has("current_us=1550 target_us=1550")),
        (out(0), "SIM out=1550"), (out(1), OFF),
    ],
    "stop_and_estop": [
        (ARM, ANY), (start(0, 1550), ANY), ("ESTOP", "RP1 OK estop=1"), (out(0), OFF),
        (ARM, err("estop_latched")), (start(0, 1550), err("estop_latched")), (move(0, 1550, 2), err("estop_latched")),
        (f"HB token={TOKEN}", err("estop_latched")), ("HELLO", has("mode=estop")),
        ("ESTOP_CLEAR", err("bad_fields")), ("ESTOP_CLEAR cutoff=no", err("cutoff_unconfirmed")),
        ("ESTOP_CLEAR cutoff=confirmed", "RP1 OK estop=0"), ("HELLO", has("mode=disarmed")),
        (ARM, "RP1 OK armed=1"), (start(0, 1550), ANY),
        ("STOP", "RP1 OK stopped=1"), (out(0), OFF), ("HELLO", has("fault=stop")),
        (ARM, ANY), (start(3, 1450), ANY), ("@ESTOP_PIN", ANY), (out(3), OFF), ("HELLO", has("mode=estop")),
        (ARM, err("estop_latched")),
    ],
    "disarm_releases_all": [
        (ARM, ANY), (start(0, 1550), ANY), (start(5, 1450), ANY),
        (out(0), "SIM out=1550"), (out(5), "SIM out=1450"),
        ("DISARM", "RP1 OK armed=0"), (out(0), OFF), (out(5), OFF),
        ("STATUS channel=5", has("active=0")),
        (move(0, 1550, 3), err("disarmed")), ("DISARM extra=1", err("bad_fields")),
    ],
    "release_restores_default_band": [
        ("LIMIT channel=2 min_us=1000 neutral_us=1500 max_us=2000", has("RP1 OK")),
        (ARM, ANY), (start(2, 1900), has("RP1 OK")), ("DISARM", ANY),
        ("STATUS channel=2", has("min_us=1400", "max_us=1600")),
        (ARM, ANY), (start(2, 1900), err("out_of_limits")),   # widened band did not survive
        ("STOP", ANY),
        ("LIMIT channel=2 min_us=1000 neutral_us=1500 max_us=2000", has("RP1 OK")),
        (ARM, ANY), (start(2, 1500), ANY), ("@ESTOP_PIN", ANY),
        ("STATUS channel=2", has("min_us=1400", "max_us=1600")),
        ("ESTOP_CLEAR cutoff=confirmed", ANY),
        ("LIMIT channel=2 min_us=1000 neutral_us=1500 max_us=2000", has("RP1 OK")),
        (ARM, ANY), (tick(5000), ANY),                           # watchdog
        ("STATUS channel=2", has("min_us=1400", "max_us=1600")),
    ],
    "survey_is_read_only": [
        ("BOARD", has("board=esp32_s3_chip_reference", "chip=ESP32-S3", "pins=0,1,2,3,4,5,6,7,10,")),
        ("SURVEY pin=4", "RP1 OK pin=4 class=floating adc=0"),          # servo signal input
        ("SURVEY pin=5", has("class=pulled_down")),                     # LED to GND: resistive
        ("SURVEY pin=6", has("class=pulled_up", "adc=3300")),           # button with pull-up
        ("SURVEY pin=7", has("class=driven_low")),                      # another chip's output
        ("SURVEY pin=8", has("class=reserved")),                        # the PCA9685 bus
        ("SURVEY pin=19", has("class=reserved")),                       # USB D-: never touched
        ("@PIN 21 3300 10000", ANY), ("SURVEY pin=21", has("class=driven_high")),  # no ADC: no proof
        ("SURVEY pin=49", err("bad_pin")), ("SURVEY", err("bad_fields")),
        (out(104), OFF), (out(105), OFF), (out(107), OFF),
    ],
    "i2c_scan": [
        ("I2C_SCAN sda=8 scl=9", "RP1 OK devices=0x40,0x70 pca9685=0x40"),
        ("I2C_SCAN sda=9 scl=8", "RP1 OK devices=none pca9685=none"),
        ("I2C_SCAN sda=6 scl=4", "RP1 OK devices=none pca9685=none"),
        ("I2C_SCAN sda=8 scl=4", err("pin_claimed")),
        ("I2C_SCAN sda=19 scl=4", err("pin_reserved")),
        ("I2C_SCAN sda=4 scl=4", err("bad_pin")),
        (ARM, ANY), (start(0, 1500), "RP1 OK channel=0 started_us=1500"),  # the driver bus still works
    ],
    "watch_events": [
        ("EVENTS", "RP1 OK events=none"),
        ("WATCH state=maybe", err("bad_fields")), ("WATCH state=on", "RP1 OK watch=1"),
        ("@PIN 6 0 0", ANY), (tick(100), ANY), ("@PIN 6 3300 10000", ANY), (tick(200), ANY),
        ("@PIN 12 0 0", ANY), (tick(300), ANY),                           # a button to GND on a floating pin
        ("EVENTS", "RP1 OK events=6:0,6:1,12:0"), ("EVENTS", "RP1 OK events=none"),
        (tick(5000), ANY), ("@PIN 6 0 0", ANY), (tick(5400), ANY),
        ("EVENTS", "RP1 OK events=none"),                                 # idle 5 s: watch switched off
        ("WATCH state=on", ANY), ("WATCH state=off", "RP1 OK watch=0"),
    ],
    "probe_safety": [
        (f"PROBE pin=4 mode=servo start_us=1500 token={TOKEN}", err("not_armed")),
        (f"BLINK pin=5 count=1 token={TOKEN}", err("not_armed")),
        (ARM, ANY),
        (f"PROBE pin=7 mode=servo start_us=1500 token={TOKEN}", err("pin_driven")),   # never driven
        (f"BLINK pin=7 count=1 token={TOKEN}", err("pin_driven")),
        (out(107), OFF),
        (f"PROBE pin=8 mode=servo start_us=1500 token={TOKEN}", err("pin_claimed")),  # I2C bus
        (f"PROBE pin=19 mode=servo start_us=1500 token={TOKEN}", err("pin_reserved")),
        (f"PROBE pin=46 mode=servo start_us=1500 token={TOKEN}", err("pin_reserved")),  # input only
        (f"PROBE pin=4 mode=servo start_us=1700 token={TOKEN}", err("out_of_limits")),
        (f"PROBE pin=4 mode=blink start_us=1500 token={TOKEN}", err("bad_fields")),
        (f"PROBE pin=4 mode=servo start_us=1500 token=other", err("bad_token")),
        (out(104), OFF),
        (f"PROBE pin=4 mode=servo start_us=1500 token={TOKEN}", "RP1 OK pin=4 channel=104"),
        (out(104), "SIM out=1500"),
        (f"PROBE pin=4 mode=servo start_us=1500 token={TOKEN}", err("pin_claimed")),
        (move(104, 1560, 1), ANY), (tick(300), ANY),
        ("STATUS channel=104", has("active=1", "current_us=1560")), (out(104), "SIM out=1560"),
        (move(104, 1700, 2), err("out_of_limits")),
        (f"RELEASE pin=4 token={TOKEN}", "RP1 OK pin=4 released=1"), (out(104), OFF),
        ("STATUS channel=104", has("active=0")),
        (start(104, 1500), err("bad_channel")),
        (f"BLINK pin=5 count=2 token={TOKEN}", "RP1 OK pin=5 blinked=2"), (out(105), "SIM out=high"),
        (move(105, 1500, 3), err("pin_claimed")),
        (tick(550), ANY), (out(105), "SIM out=low"), (tick(800), ANY), (out(105), "SIM out=high"),
        (f"HB token={TOKEN}", ANY), (tick(1050), ANY), (out(105), "SIM out=low"),
        (tick(1300), ANY), (out(105), OFF),                                # done: released
        (f"BLINK pin=5 count=11 token={TOKEN}", err("bad_fields")),
        (f"BLINK pin=6 count=1 token={TOKEN}", has("RP1 OK")), ("STOP", ANY), (out(106), OFF),
        (ARM, ANY),
    ] + [(f"PROBE pin={pin} mode=servo start_us=1500 token={TOKEN}", has("RP1 OK"))
         for pin in (1, 2, 3, 4, 10, 11, 12, 13)] + [
        (f"PROBE pin=14 mode=servo start_us=1500 token={TOKEN}", err("no_pwm_slot")),
        ("DISARM", ANY), (out(101), OFF), (out(113), OFF),
        (ARM, ANY), (f"PROBE pin=14 mode=servo start_us=1500 token={TOKEN}", has("RP1 OK")),
        ("ESTOP", ANY), (out(114), OFF),
    ],
    "driver_fault": [
        (ARM, ANY), ("@DRIVER fail", ANY), (start(0, 1550), err("driver_error")),
        ("HELLO", has("mode=disarmed", "fault=driver")),
        ("@DRIVER ok", ANY), (ARM, ANY), (start(0, 1500), ANY), (move(0, 1550, 2), ANY), ("@DRIVER fail", ANY),
        (tick(100), ANY), ("STATUS channel=0", has("armed=0", "active=0")), ("HELLO", has("fault=driver")),
        ("@DRIVER ok", ANY),
    ],
}


def commands(name):
    return [command for command, _ in SCENARIOS[name]]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_firmware_core_matches_python_model(sim_binary, name):
    script = commands(name)
    assert run_sim(sim_binary, script) == run_model(script)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_firmware_safety_semantics(name):
    replies = run_model(commands(name))
    for (command, expected), reply in zip(SCENARIOS[name], replies):
        if expected is None:
            continue
        if callable(expected):
            assert expected(reply), f"{command!r} -> {reply!r}"
        else:
            assert reply == expected, f"{command!r} -> {reply!r}"


def test_line_buffer_rejects_overlong_and_binary_lines(sim_binary):
    out = run_sim(sim_binary, ["@RAW " + "A" * 161, "@RAW HELLO\\x0d", "@RAW \\x01HELLO", "@RAW " + "H" * 160])
    assert out[0] == "RP1 ERR reason=line_too_long"
    assert out[1].startswith("RP1 OK proto=ROBOPROBE/1")  # CRLF line endings accepted
    assert out[2] == "RP1 ERR reason=bad_bytes"
    assert out[3] == "RP1 ERR reason=unknown_command"


def test_legacy_model_matches_v01_reference_firmware():
    device = ReferenceDevice(legacy=True)
    assert device.handle("HELLO", 0) == "RP1 OK proto=ROBOPROBE/1 mode=disarmed id=reference-emulator"
    assert device.handle("MOVE channel=0 target_us=1500 seq=1", 0) == "RP1 ERR reason=disarmed"
    assert device.handle("ARM token=x cutoff=confirmed", 0) == "RP1 OK armed=1"
    assert device.handle("MOVE channel=0 target_us=1500 seq=1 token=x", 0) == "RP1 OK seq=1 channel=0 target_us=1500"
    assert device.handle("LIMIT channel=0 min_us=1 neutral_us=2 max_us=3", 0) == "RP1 ERR reason=unknown_command"
