"""The real host against the real firmware core, through a pseudo terminal.

The other firmware tests compare the C++ core with the Python model command
by command. These run the compiled core in its own process with its own
millisecond clock (firmware/.../test/native/bench_main.cpp) and drive it with
the unmodified host API over a tty, the way an ESP32 on USB is used.
"""
import time

import pytest

from robo_prober.api import Controller
from robo_prober.firmware_bench import FirmwareBench, bench_available
from robo_prober.transport import ProtocolError, SerialProtocolTransport

pytestmark = pytest.mark.skipif(not bench_available(), reason="needs a C++ compiler and a POSIX pseudo terminal")


@pytest.fixture
def bench():
    with FirmwareBench() as b:
        yield b


def open_controller(bench):
    return Controller(SerialProtocolTransport(bench.port, timeout_s=1.0)).open()


def test_host_drives_real_firmware_and_close_releases(bench):
    ctl = open_controller(bench)
    try:
        assert ctl.info["id"] == "firmware-bench" and "start" in ctl.caps
        ctl.arm(power_cutoff_confirmed=True)
        assert bench.active_channels() == {}            # arming alone outputs nothing
        ctl.start(3, 1480)
        time.sleep(0.05)
        assert bench.active_channels() == {3: 1480}     # first PWM is exactly the start pulse
        ctl.set_pulse(3, 1560, slew_us_s=400)
        assert bench.outputs[3] == 1560
        pulses = [p for _, ch, p in bench.events if ch == 3 and p is not None]
        assert pulses[0] == 1480 and max(abs(a - b) for a, b in zip(pulses, pulses[1:])) <= 10  # slewed
    finally:
        ctl.close()
    bench.wait_released(0.5)


def test_watchdog_releases_on_host_silence(bench):
    ctl = open_controller(bench)
    ctl.arm(power_cutoff_confirmed=True, heartbeat="manual")
    ctl.start(0, 1500)
    silent_from = time.monotonic()
    bench.wait_released(2.0)
    assert time.monotonic() - silent_from < 1.3          # watchdog_ms=1000 plus a tick
    assert ctl.transport.transact("HELLO")["fault"] == "watchdog"
    with pytest.raises(ProtocolError):
        ctl.move(0, 1510)                                # disarmed by the firmware
    ctl.transport.close()


def test_out_of_band_rejected_and_estop_latches(bench):
    ctl = open_controller(bench)
    ctl.arm(power_cutoff_confirmed=True)
    ctl.start(1, 1500)
    with pytest.raises(ProtocolError, match="out_of_limits"):
        ctl.move(1, 1700)                                # default band 1400..1600
    time.sleep(0.05)
    assert bench.outputs[1] == 1500
    bench.estop_pin()
    bench.wait_released(0.5)
    ctl._end_session()
    with pytest.raises(ProtocolError, match="estop_latched"):
        ctl.arm(power_cutoff_confirmed=True)
    ctl.clear_estop(power_cutoff_confirmed=True)
    ctl.arm(power_cutoff_confirmed=True)
    ctl.close()
    bench.wait_released(0.5)
