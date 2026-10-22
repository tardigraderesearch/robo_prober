import threading
import time

import pytest

from robo_prober.cli import demo_permissions
from robo_prober.permissions import load_permissions
from robo_prober.reference_device import ReferenceDevice
from robo_prober.session import run_bounded_probe
from robo_prober.transport import (
    Heartbeat,
    InProcessEmulatorConnection,
    ProtocolError,
    PtyReferenceEmulator,
    SerialProtocolTransport,
    TransportTimeout,
    parse_response,
    pty_available,
    serve_forever,
)

requires_pty = pytest.mark.skipif(not pty_available(), reason="needs a POSIX pseudo terminal (Linux/macOS)")


def serve(emulator):
    serve_forever(emulator, poll_s=0.1)


def in_process(device=None):
    return SerialProtocolTransport("emulator", connection=InProcessEmulatorConnection(device or ReferenceDevice()))


def test_parse_response_rejects_malformed_ack():
    with pytest.raises(ProtocolError):
        parse_response("OK sure")


@requires_pty
def test_transport_uses_real_pty_loopback():
    emulator = PtyReferenceEmulator()
    port = emulator.open()
    thread = threading.Thread(target=serve, args=(emulator,), daemon=True)
    thread.start()
    try:
        with SerialProtocolTransport(port, timeout_s=1.0) as transport:
            assert transport.hello()["id"] == "reference-emulator"
            assert transport.has_caps("limit", "status", "hb", "token", "estop")
            transport.arm("test", cutoff_confirmed=True)
            assert transport.has_caps("start")
            transport.start(2, 1500)
            assert transport.move_us(2, 1510)["target_us"] == "1510"
            transport.disarm()
    finally:
        emulator.close()
        thread.join(timeout=1.0)


@requires_pty
def test_transport_stops_on_malformed_ack():
    emulator = PtyReferenceEmulator(malformed_after=2)
    port = emulator.open()
    thread = threading.Thread(target=serve, args=(emulator,), daemon=True)
    thread.start()
    try:
        with SerialProtocolTransport(port, timeout_s=1.0) as transport:
            transport.hello()
            transport.arm("test", cutoff_confirmed=True)
            with pytest.raises(ProtocolError, match="malformed"):
                transport.move_us(0, 1500)
    finally:
        emulator.close()
        thread.join(timeout=1.0)


@requires_pty
def test_transport_timeout_without_emulator_response():
    emulator = PtyReferenceEmulator()
    port = emulator.open()
    try:
        with SerialProtocolTransport(port, timeout_s=0.1) as transport:
            with pytest.raises(TransportTimeout):
                transport.hello()
    finally:
        emulator.close()


def test_in_process_emulator_needs_no_pty():
    with in_process() as transport:
        assert transport.hello()["proto"] == "ROBOPROBE/1"
        assert transport.watchdog_ms == 1000


def test_arm_token_format_checked_on_host():
    with in_process() as transport:
        transport.hello()
        with pytest.raises(ProtocolError, match="arm token"):
            transport.arm("has space", cutoff_confirmed=True)
        with pytest.raises(ProtocolError, match="cutoff"):
            transport.arm("ok", cutoff_confirmed=False)


def test_probe_hands_permission_band_to_v02_firmware_and_settles():
    device = ReferenceDevice()
    permissions = load_permissions(demo_permissions())
    with in_process(device) as transport:
        events = run_bounded_probe(transport=transport, permissions=permissions, channels=[0, 1],
                                   arm_token="probe", cutoff_confirmed=True, slew_us_s=2000)
    assert [e["event"] for e in events] == ["hello", "limit", "limit", "start", "move", "return_to_approved_neutral",
                                             "start", "move", "return_to_approved_neutral", "disarm"]
    assert events[3]["pulse_us"] == 1500 and events[6]["pulse_us"] == 1490  # approved neutral, explicitly
    assert events[4]["settled_us"] == 1520 and events[5]["settled_us"] == 1500
    assert device.write_log[0] == (0, 1500)  # first PWM is exactly the start pulse
    assert (device.channels[1].min_us, device.channels[1].max_us) == (1450, 1530)
    assert device.armed is False and device.output(0) is None


def test_probe_still_works_with_v01_firmware():
    device = ReferenceDevice(legacy=True)
    permissions = load_permissions(demo_permissions())
    with in_process(device) as transport:
        events = run_bounded_probe(transport=transport, permissions=permissions, channels=[0],
                                   arm_token="probe", cutoff_confirmed=True)
    assert [e["event"] for e in events] == ["hello", "move", "return_to_approved_neutral", "disarm"]


def test_firmware_rejects_moves_outside_band_even_if_host_asks():
    with in_process() as transport:
        transport.hello()
        transport.limit(0, 1450, 1500, 1550, 2000)
        transport.arm("band", cutoff_confirmed=True)
        with pytest.raises(ProtocolError, match="not_started"):
            transport.move_us(0, 1500)
        transport.start(0, 1500)
        with pytest.raises(ProtocolError, match="out_of_limits"):
            transport.move_us(0, 1560)


def test_watchdog_releases_outputs_when_host_goes_quiet():
    device = ReferenceDevice(watchdog_ms=100)
    with in_process(device) as transport:
        transport.hello()
        transport.arm("quiet", cutoff_confirmed=True)
        transport.start(0, 1500)
        transport.move_us(0, 1520)
        time.sleep(0.25)
        with pytest.raises(ProtocolError, match="disarmed"):
            transport.move_us(0, 1520)
    assert device.fault == "watchdog" and device.output(0) is None


def test_heartbeat_feeds_watchdog_and_failures_surface():
    device = ReferenceDevice(watchdog_ms=100)
    with in_process(device) as transport:
        transport.hello()
        transport.arm("alive", cutoff_confirmed=True)
        heartbeat = Heartbeat.for_transport(transport).start()
        try:
            transport.start(0, 1500)
            transport.move_us(0, 1520)
            time.sleep(0.3)
            transport.move_us(0, 1500)  # still armed thanks to HB
            device.external_estop()      # e.g. the hardware e-stop input trips
            time.sleep(0.1)
            with pytest.raises(ProtocolError, match="heartbeat failed"):
                heartbeat.check()
        finally:
            heartbeat.stop()


def test_no_heartbeat_for_firmware_without_watchdog():
    with in_process(ReferenceDevice(legacy=True)) as transport:
        transport.hello()
        assert Heartbeat.for_transport(transport) is None


def test_estop_latches_until_cleared():
    with in_process() as transport:
        transport.hello()
        transport.arm("e", cutoff_confirmed=True)
        transport.estop()
        assert transport.hello()["mode"] == "estop"
        with pytest.raises(ProtocolError, match="estop_latched"):
            transport.arm("e", cutoff_confirmed=True)
        with pytest.raises(ProtocolError):
            transport.estop_clear(cutoff_confirmed=False)
        transport.estop_clear(cutoff_confirmed=True)
        assert transport.hello()["mode"] == "disarmed"


def test_probe_stops_controller_on_failure():
    device = ReferenceDevice()
    permissions = load_permissions(demo_permissions())
    with in_process(device) as transport:
        original = transport.wait_settled

        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise ProtocolError("simulated camera/host failure")

        transport.wait_settled = broken
        with pytest.raises(ProtocolError, match="simulated"):
            run_bounded_probe(transport=transport, permissions=permissions, channels=[0],
                              arm_token="fail", cutoff_confirmed=True, slew_us_s=2000)
    assert device.fault == "stop" and device.armed is False and device.output(0) is None
