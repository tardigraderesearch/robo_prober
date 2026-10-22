"""The importable API used by other programs."""
import time

import pytest

import robo_prober as rp
from robo_prober.reference_device import ReferenceDevice
from robo_prober.transport import ProtocolError


def test_public_names_are_exported():
    for name in ("discover", "connect", "Controller", "load_map", "save_map", "RomChannel", "RomSession",
                 "rom_range", "get_rom", "set_rom", "load_markers"):
        assert hasattr(rp, name), name
    assert rp.__version__ == "0.2.0"


def test_connect_requires_a_port_or_the_emulator():
    with pytest.raises(ValueError):
        rp.connect()


def test_nothing_moves_until_armed_and_close_disarms():
    device = ReferenceDevice()
    with rp.connect(emulator=True, device=device) as ctl:
        assert ctl.info["proto"] == "ROBOPROBE/1" and "limit" in ctl.caps
        with pytest.raises(ProtocolError, match="not armed"):
            ctl.move(0, 1500)
        with pytest.raises(ProtocolError, match="cutoff"):
            ctl.arm(power_cutoff_confirmed=False)
        token = ctl.arm(power_cutoff_confirmed=True)
        assert len(token) == 16 and device.armed and device.write_log == []
        with pytest.raises(ProtocolError, match="not started"):
            ctl.move(0, 1520)            # no hidden start pulse
        with pytest.raises(ProtocolError, match="not started"):
            ctl.set_pulse(0, 1520)
        assert device.write_log == []
        ctl.start(0, 1480)
        assert device.write_log == [(0, 1480)]
        with pytest.raises(ProtocolError, match="already started"):
            ctl.start(0, 1500)
        ctl.move(0, 1520)
        assert ctl.positions() == {0: 1520}
    assert not device.armed and device.output(0) is None


def test_set_pulse_ramps_at_host_slew_limit():
    device = ReferenceDevice()
    with rp.connect(emulator=True, device=device) as ctl:
        ctl.set_limits(0, 1300, 1700, neutral_us=1500, slew_us_s=2000)
        ctl.arm(power_cutoff_confirmed=True)
        ctl.start(0, 1500)
        started = time.monotonic()
        assert ctl.set_pulse(0, 1600, slew_us_s=1000, tick_s=0.01) == 1600
        elapsed = time.monotonic() - started
        assert elapsed >= 0.08  # 100 us at 1000 us/s cannot be faster than ~0.1 s
        assert ctl.status(0)["current_us"] == 1600 and ctl.position(0) == 1600
        with pytest.raises(ProtocolError, match="out_of_limits"):
            ctl.move(0, 1750)


def test_set_limits_default_neutral_and_estop_cycle():
    device = ReferenceDevice()
    with rp.connect(emulator=True, device=device) as ctl:
        reply = ctl.set_limits(4, 1000, 1400)
        assert reply["neutral_us"] == "1200"
        ctl.arm(power_cutoff_confirmed=True)
        ctl.start(4, 1200)
        ctl.move(4, 1250)
        ctl.estop()
        assert not ctl.armed and device.estop and device.output(4) is None
        with pytest.raises(ProtocolError, match="estop_latched"):
            ctl.arm(power_cutoff_confirmed=True)
        ctl.clear_estop(power_cutoff_confirmed=True)
        assert ctl.hello()["mode"] == "disarmed"


def test_exception_inside_with_block_stops_controller():
    device = ReferenceDevice()
    with pytest.raises(RuntimeError):
        with rp.connect(emulator=True, device=device) as ctl:
            ctl.arm(power_cutoff_confirmed=True)
            ctl.start(0, 1500)
            ctl.move(0, 1550)
            raise RuntimeError("caller failed")
    assert device.fault == "stop" and device.output(0) is None


def test_map_round_trip(tmp_path):
    from robo_prober.cli import demo_confirmations, demo_fixture_path, demo_permissions
    from robo_prober.permissions import load_permissions
    from robo_prober.session import build_map_from_session

    hw = build_map_from_session(controller_identity={}, permissions=load_permissions(demo_permissions()),
                                observation_paths=[demo_fixture_path()], confirmations=demo_confirmations(),
                                probe_events=[])
    path = rp.save_map(rp.set_rom(hw, 0, 1300, 1700, set_by="api"), tmp_path / "m.json")
    assert rp.get_rom(rp.load_map(path), 0)["max_us"] == 1700
