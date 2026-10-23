"""SBC safety/protocol tests. All wiring is emulated; no device files opened."""
import queue
import socket
import subprocess
import sys
import threading
import time

import pytest

from robo_prober.reference_device import ReferenceDevice
from robo_prober.sbc import EmulatedBackend, PinTruth, SBCAgent
from robo_prober.sbc.boards import Board, detect_board
from robo_prober.sbc.server import LineFramer, serve_stream, serve_tcp
from robo_prober.transport import InProcessEmulatorConnection, SerialProtocolTransport, parse_response

ARM = "ARM token=t cutoff=confirmed"
PROBE = "PROBE pin=4 mode=servo start_us=1500 token=t"


@pytest.fixture
def rig():
    backend = EmulatedBackend({
        4: "servo", 17: "led", 27: "button", 22: "pulled_up", 6: "pulled_down",
        7: "floating", 23: "driven_high", 25: "driven_low", 24: "claimed",
        2: PinTruth("i2c", bus=1, address=0x40), 3: PinTruth("i2c", bus=1, address=0x68),
    })
    device = SBCAgent(backend)
    yield device, backend
    device.close()


def ok(device, command, now=0):
    status, fields = parse_response(device.handle(command, now))
    assert status == "OK", fields
    return fields


def err(device, command, reason, now=0):
    assert device.handle(command, now) == f"RP1 ERR reason={reason}"


def test_hello_board_and_no_output_on_boot(rig):
    d, b = rig
    hello = ok(d, "HELLO")
    assert hello["proto"] == "ROBOPROBE/1"
    assert set(hello["caps"].split(",")) == set("limit,status,hb,token,estop,start,survey,watch,i2c,probe,blink".split(","))
    assert hello["mode"] == "disarmed"
    assert set(map(int, ok(d, "BOARD")["pins"].split(","))) == set(b.lines()) - {24}
    assert b.outputs == {} and b.write_log == []


@pytest.mark.parametrize("command,reason", [
    (PROBE, "not_armed"), ("BLINK pin=17 count=1 token=t", "not_armed"),
    ("RELEASE pin=4 token=t", "not_armed"),
    ("START channel=0 pulse_us=1500 token=t", "disarmed"),
    ("MOVE channel=0 target_us=1500 seq=1 token=t", "disarmed"),
    ("ARM token=t cutoff=no", "cutoff_unconfirmed"),
    ("ARM token=t", "missing_arm_fields"),
])
def test_no_movement_before_arm(rig, command, reason):
    d, b = rig
    err(d, command, reason)
    assert not b.outputs and not b.write_log


@pytest.mark.parametrize("pin,classification", [
    (23, "driven_high"), (25, "driven_low"), (22, "pulled_up"),
    (6, "pulled_down"), (7, "floating"),
])
def test_passive_survey(rig, pin, classification):
    d, b = rig
    assert ok(d, f"SURVEY pin={pin}") == {"pin": str(pin), "class": classification, "adc": "-"}
    assert not b.write_log
    assert {bias for _, bias in b.input_log} == {"disabled", "pull_up", "pull_down"}


@pytest.mark.parametrize("pin", [23, 25])
@pytest.mark.parametrize("verb", ["PROBE pin={pin} mode=servo start_us=1500 token=t", "BLINK pin={pin} count=1 token=t"])
def test_driven_pin_never_driven(rig, pin, verb):
    d, b = rig
    ok(d, ARM)
    err(d, verb.format(pin=pin), "pin_driven")
    assert not b.outputs and not b.write_log


def test_claimed_and_reserved_are_refused(rig):
    d, b = rig
    ok(d, ARM)
    for command in ("SURVEY pin=24", "PROBE pin=24 mode=servo start_us=1500 token=t", "BLINK pin=24 count=1 token=t"):
        err(d, command, "pin_claimed")
    profile = {"pins": {4: {"type": "gpio", "gpio": 4, "reserved": True, "exposed": True}}}
    reserved = SBCAgent(b, profile=profile)
    assert ok(reserved, "SURVEY pin=4")["class"] == "reserved"
    ok(reserved, ARM)
    err(reserved, PROBE, "pin_reserved")
    assert not b.write_log


def test_ownership_and_survey_rechecked_at_activation(rig):
    d, b = rig
    ok(d, "SURVEY pin=4")
    ok(d, ARM)
    b._truth[4].kind = "claimed"
    err(d, PROBE, "pin_claimed")
    b._truth[4].kind = "driven_high"
    err(d, PROBE, "pin_driven")
    assert not b.write_log


def test_blink_finds_led_at_2hz_and_releases(rig):
    d, b = rig
    ok(d, ARM)
    assert ok(d, "BLINK pin=17 count=3 token=t") == {"pin": "17", "blinked": "3"}
    for ms in range(250, 1501, 250):
        ok(d, "HB token=t", ms)
    assert b.observations == [(17, "led", v) for v in (1, 0, 1, 0, 1, 0)]
    assert not b.outputs and d.blink is None
    assert d.armed


def test_watch_finds_button_drains_events_and_never_outputs(rig):
    d, b = rig
    ok(d, "WATCH on")
    assert {23, 24, 25}.isdisjoint(b.watched)
    b.press(27)
    b.press(27, False)
    assert ok(d, "EVENTS")["events"] == "27:0,27:1"
    assert ok(d, "EVENTS")["events"] == "none"
    ok(d, "WATCH off")
    b.press(27)
    assert ok(d, "EVENTS")["events"] == "none"
    assert not b.write_log


def test_probe_detaches_watched_line(rig):
    d, b = rig
    ok(d, "WATCH on")
    ok(d, ARM)
    ok(d, PROBE)
    assert 4 not in b.watched
    assert 27 in b.watched
    ok(d, "STOP")
    assert not b.watched and not b.outputs


def test_servo_explicit_start_band_slew_and_host_transport(rig):
    d, b = rig
    clock = [0]
    connection = InProcessEmulatorConnection(d, clock=lambda: clock[0])
    with SerialProtocolTransport("emulated", connection=connection) as transport:
        transport.hello()
        transport.limit(0, 1450, 1500, 1550, 100)
        transport.arm("t", cutoff_confirmed=True)
        assert not b.write_log
        err(d, "PROBE pin=4 mode=servo start_us=1600 token=t", "out_of_limits")
        assert ok(d, PROBE)["channel"] == "104"  # host-facing GPIO channel, like the firmware
        assert b.observations == [(4, "servo", 1500)]
        transport.move_us(0, 1550)
        clock[0] = 100
        assert transport.status(0)["current_us"] == 1510
        clock[0] = 500
        assert transport.status(0)["current_us"] == 1550
        err(d, "MOVE channel=0 target_us=1600 seq=2 token=t", "out_of_limits", 500)
        err(d, "LIMIT channel=0 min_us=1400 neutral_us=1450 max_us=1500", "position_outside_limits", 500)
        ok(d, "RELEASE pin=4 token=t", 500)
        status = transport.status(0)
        assert status["min_us"] == 1400 and status["max_us"] == 1600 and status["slew_us_s"] == 250
        assert not b.outputs
        transport.start(0, 1450)
        assert b.outputs[4] == ("servo", 1450)
    assert all(1450 <= value <= 1550 for _, kind, value in b.write_log if kind == "servo")


@pytest.mark.parametrize("command", ["STOP", "DISARM", "ESTOP", "watchdog"])
@pytest.mark.parametrize("active", [PROBE, "BLINK pin=17 count=10 token=t"])
def test_every_shutdown_releases_everything_and_resets_bands(rig, command, active):
    d, b = rig
    ok(d, "WATCH on")
    ok(d, "LIMIT channel=5 min_us=800 neutral_us=1500 max_us=2000 slew_us_s=2000")
    ok(d, ARM)
    ok(d, active)
    assert b.outputs
    if command == "watchdog":
        d.tick(1001)
    else:
        ok(d, command)
    assert not b.outputs and not b.watched
    assert not d.armed and not d.token and d.blink is None
    for state in d.channels:
        assert (state.min_us, state.max_us, state.slew_us_s, state.active) == (1400, 1600, 250, False)
    if command == "ESTOP":
        err(d, ARM, "estop_latched")
        err(d, "ESTOP_CLEAR cutoff=no", "cutoff_unconfirmed")
        ok(d, "ESTOP_CLEAR cutoff=confirmed")
        ok(d, ARM)
        assert not b.outputs


def test_watchdog_uses_only_valid_activity_and_exact_boundary(rig):
    d, b = rig
    ok(d, ARM)
    ok(d, PROBE)
    err(d, "HB token=wrong", "bad_token", 700)
    ok(d, "STATUS channel=0", 1000)
    assert b.outputs
    d.tick(1001)
    assert not b.outputs and d.fault == "watchdog"


def test_one_active_pin_at_a_time_and_release_restarts_default_band(rig):
    d, b = rig
    ok(d, ARM)
    ok(d, PROBE)
    err(d, "BLINK pin=17 count=1 token=t", "no_pwm_slot")
    err(d, "PROBE pin=17 mode=servo start_us=1500 token=t", "no_pwm_slot")
    err(d, "I2C_SCAN bus=1", "no_pwm_slot")
    ok(d, "RELEASE pin=4 token=t")
    ok(d, "BLINK pin=17 count=1 token=t")
    assert set(b.outputs) == {17}
    err(d, "START channel=0 pulse_us=1500 token=t", "no_pwm_slot")


def test_released_slots_can_probe_more_than_sixteen_pins():
    backend = EmulatedBackend({p: "servo" for p in range(32)})
    device = SBCAgent(backend)
    ok(device, ARM)
    for pin in range(32):
        ok(device, f"PROBE pin={pin} mode=servo start_us=1500 token=t")
        ok(device, f"RELEASE pin={pin} token=t")
    assert not backend.outputs


def test_i2c_devices_and_reserved_addresses(rig):
    d, b = rig
    b._truth[8] = PinTruth("i2c", bus=1, address=0x07)
    b._truth[9] = PinTruth("i2c", bus=1, address=0x78)
    assert ok(d, "I2C_SCAN bus=1")["devices"] == "0x40,0x68"
    assert ok(d, "I2C_SCAN bus=0")["devices"] == "none"
    assert not b.write_log


@pytest.mark.parametrize("command,reason", [
    ("SURVEY pin=-1", "bad_pin"), ("SURVEY pin=99", "bad_pin"),
    ("SURVEY pin=4 pin=4", "duplicate_field"), ("SURVEY pin=4 extra=1", "bad_fields"),
    ("WATCH maybe", "bad_syntax"),
    ("EVENTS extra=1", "bad_fields"), ("I2C_SCAN sda=2 scl=3", "bad_fields"),
    ("I2C_SCAN bus=-1", "bad_bus"), ("PROBE pin=4 mode=servo token=t", "bad_fields"),
    ("PROBE pin=4 mode=digital start_us=1500 token=t", "bad_mode"),
    ("PROBE pin=4 mode=servo start_us=1500", "token_required"),
    ("BLINK pin=17 count=0 token=t", "bad_count"), ("BLINK pin=17 count=11 token=t", "bad_count"),
    ("BLINK pin=17 count=1 token=other", "bad_token"),
    ("START channel=0 pulse_us=1500 token=t", "no_pwm_slot"),
    ("HELLO " + "x" * 160, "line_too_long"),
])
def test_rejected_commands_never_write(rig, command, reason):
    d, b = rig
    ok(d, ARM)
    err(d, command, reason)
    assert not b.outputs and not b.write_log


def test_existing_commands_match_reference_core(rig):
    d, _ = rig
    ok(d, ARM)
    ok(d, PROBE)
    ok(d, "STOP")  # establish a GPIO binding for channel 0
    reference = ReferenceDevice()
    script = [
        (0, "STOP"), (0, "LIMIT channel=0 min_us=1200 neutral_us=1500 max_us=1800 slew_us_s=300"),
        (0, "START channel=0 pulse_us=1500 token=t"), (0, ARM),
        (0, "MOVE channel=0 target_us=1700 seq=1 token=t"),
        (0, "START channel=0 pulse_us=1450 token=t"),
        (1, "START channel=0 pulse_us=1450 token=t"),
        (2, "MOVE channel=0 target_us=1700 seq=1 token=t"),
        (200, "STATUS channel=0"), (800, "HB token=t"), (1000, "STATUS channel=0"),
        (1801, "HB token=t"), (1801, "STATUS channel=0"),
        (1801, "ESTOP"), (1801, ARM), (1801, "ESTOP_CLEAR cutoff=confirmed"),
        (1801, ARM), (1801, "START channel=0 pulse_us=1200 token=t"),
        (1801, "DISARM"),
    ]
    for ms, line in script:
        assert d.handle(line, ms) == reference.handle(line, ms), line


def test_driver_failure_releases_inputs_and_outputs(rig, monkeypatch):
    d, b = rig
    ok(d, "WATCH on")
    ok(d, ARM)
    ok(d, PROBE)
    ok(d, "MOVE channel=0 target_us=1600 seq=1 token=t")
    def fail(*_):
        raise OSError("injected I/O failure")
    monkeypatch.setattr(b, "servo", fail)
    d.tick(100)
    assert d.fault == "driver" and not d.armed and not b.outputs and not b.watched


def test_release_failure_disarms_and_resets_band(rig, monkeypatch):
    from robo_prober.sbc.backends import BackendError
    d, b = rig
    ok(d, ARM)
    ok(d, PROBE)
    def fail(*_):
        raise BackendError("driver_error")
    monkeypatch.setattr(b, "release", fail)
    err(d, "RELEASE pin=4 token=t", "driver_error")
    assert not d.armed and not b.outputs and d.fault == "driver"
    assert not d.channels[0].active and d.channels[0].min_us == 1400


@pytest.mark.parametrize("model,profile,label", [
    ("Raspberry Pi 4 Model B Rev 1.4", "raspberry_pi_4", "pinctrl-bcm2711"),
    ("Raspberry Pi 5 Model B Rev 1.0", "raspberry_pi_5", "pinctrl-rp1"),
    ("Raspberry Pi Zero 2 W Rev 1.0", "raspberry_pi_zero_2w", "pinctrl-bcm2835"),
    ("NVIDIA Jetson Orin Nano Developer Kit", "jetson_orin_nano_devkit", "tegra234-gpio"),
    ("NVIDIA Jetson Orin NX Developer Kit", "jetson_orin_nx_devkit", "tegra234-gpio"),
    ("NVIDIA Jetson AGX Orin Developer Kit", "jetson_agx_orin_devkit", "tegra234-gpio"),
])
def test_board_detection_uses_fixture_files(tmp_path, model, profile, label):
    path = tmp_path / "model"
    path.write_text(model + "\0")
    board = detect_board(path, tmp_path / "nv_tegra_release")
    assert board.id == profile and label in board.chip_labels


def test_unknown_tegra_does_not_invent_carrier(tmp_path):
    release = tmp_path / "nv_tegra_release"
    release.touch()
    assert detect_board(tmp_path / "model", release).id == "jetson_unknown"


def test_catalog_namespace_maps_header_ids_to_line_offsets():
    backend = EmulatedBackend({4: "servo", 5: "servo"})
    profile = {"pin_index": "physical_header", "pins": {
        7: {"type": "gpio", "gpio": 4, "reserved": False, "exposed": True, "safe_probe": False},
        29: {"type": "gpio", "gpio": 5, "reserved": True, "exposed": True},
        1: {"type": "power", "gpio": None, "reserved": True, "exposed": True},
    }}
    device = SBCAgent(backend, board=Board("fixture", "Model with spaces"), profile=profile)
    assert ok(device, "BOARD") == {"board": "fixture", "chip": "Model_with_spaces", "pins": "7"}
    ok(device, ARM)
    ok(device, "PROBE pin=7 mode=servo start_us=1500 token=t")
    assert backend.outputs == {4: ("servo", 1500)}


def test_line_framing_is_bounded_and_recovers():
    framer = LineFramer()
    assert list(framer.feed(b"x" * 100000)) == []
    assert len(framer.pending) == 0
    assert list(framer.feed(b"\nHELLO\r\n")) == [None, "HELLO"]


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_idle_partial_line_watchdog_and_disconnect_release():
    backend = EmulatedBackend()
    device = SBCAgent(backend, watchdog_ms=80)
    client, server = socket.socketpair()
    stop = threading.Event()
    worker = threading.Thread(target=serve_stream, args=(device, server.fileno(), server.fileno()), kwargs={"stop": stop})
    worker.start()
    try:
        client.sendall((ARM + "\n" + PROBE + "\n").encode())
        wait_for(lambda: bool(backend.outputs))
        client.sendall(b"HB token=")  # partial commands never feed the watchdog
        wait_for(lambda: device.fault == "watchdog")
        assert not backend.outputs
        client.sendall(("t\n" + ARM + "\n" + PROBE + "\n").encode())
        wait_for(lambda: bool(backend.outputs))
        client.shutdown(socket.SHUT_WR)
        wait_for(lambda: not backend.outputs)
    finally:
        stop.set()
        worker.join(2)
        client.close()
        server.close()
        device.close()
    assert not worker.is_alive()


def test_tcp_sessions_release_and_preserve_estop_latch():
    device = SBCAgent(EmulatedBackend())
    stop, ports = threading.Event(), queue.Queue()
    worker = threading.Thread(target=serve_tcp, args=(device, 0), kwargs={"stop": stop, "ready": ports.put})
    worker.start()
    port = ports.get(timeout=2)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"ESTOP\n")
            assert client.recv(1024) == b"RP1 OK estop=1\n"
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall((ARM + "\n").encode())
            assert client.recv(1024) == b"RP1 ERR reason=estop_latched\n"
    finally:
        stop.set()
        worker.join(2)
        device.close()
    assert not worker.is_alive()


def test_blocked_reply_does_not_block_watchdog():
    backend = EmulatedBackend()
    device = SBCAgent(backend, watchdog_ms=80)
    client, server = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    stop = threading.Event()
    worker = threading.Thread(target=serve_stream, args=(device, server.fileno(), server.fileno()), kwargs={"stop": stop})
    worker.start()
    try:
        client.sendall((ARM + "\n" + PROBE + "\n").encode())
        wait_for(lambda: bool(backend.outputs))
        client.sendall(b"STATUS channel=0\n" * 100)
        # Never consume replies. A blocking write would hold PWM indefinitely.
        wait_for(lambda: device.fault == "watchdog")
        assert not backend.outputs
    finally:
        stop.set()
        worker.join(2)
        client.close()
        server.close()
        device.close()
    assert not worker.is_alive()


def test_module_stdio_without_optional_hardware_libraries():
    result = subprocess.run([sys.executable, "-P", "-m", "robo_prober.sbc", "--backend", "emulated"],
                            input="HELLO\nI2C_SCAN bus=1\n", capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and not result.stderr
    replies = result.stdout.splitlines()
    assert len(replies) == 2 and "proto=ROBOPROBE/1" in replies[0]
    assert replies[1] == "RP1 OK devices=0x40"
