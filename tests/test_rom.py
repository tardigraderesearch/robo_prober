"""Range-of-motion logic and sessions against the emulated controller."""
import json
import threading
import time
from contextlib import contextmanager

import pytest

from robo_prober import cli
from robo_prober.api import Controller, connect
from robo_prober.cli import demo_confirmations, demo_fixture_path, demo_permissions
from robo_prober.map_schema import MapValidationError, export_atomic, load_map, validate_map
from robo_prober.permissions import load_permissions
from robo_prober.reference_device import ReferenceDevice
from robo_prober.rom import (
    RomChannel,
    RomError,
    RomSession,
    apply_rom_to_map,
    channels_from_map,
    get_rom,
    rom_range,
    set_rom,
)
from robo_prober.session import build_map_from_session
from robo_prober.transport import ProtocolError, PtyReferenceEmulator, SerialProtocolTransport, pty_available, serve_forever

BACKENDS = ["in_process", pytest.param("pty", marks=pytest.mark.skipif(not pty_available(), reason="needs a POSIX pseudo terminal"))]


@contextmanager
def controller(backend, device=None):
    device = device or ReferenceDevice()
    if backend == "pty":
        emulator = PtyReferenceEmulator(device=device)
        port = emulator.open()
        thread = threading.Thread(target=serve_forever, args=(emulator,), daemon=True)
        thread.start()
        ctl = Controller(SerialProtocolTransport(port, timeout_s=1.0)).open()
        try:
            yield ctl, device
        finally:
            ctl.close()
            emulator.close()
            thread.join(timeout=1.0)
    else:
        with connect(emulator=True, device=device) as ctl:
            yield ctl, device


@pytest.fixture
def demo_map():
    return build_map_from_session(
        controller_identity={"status": "simulated"},
        permissions=load_permissions(demo_permissions()),
        observation_paths=[demo_fixture_path()],
        confirmations=demo_confirmations(),
        probe_events=[{"event": "hello"}],
    )


# -- pure logic ---------------------------------------------------------------

@pytest.mark.parametrize("rom1,rom2", [(1200, 1800), (1800, 1200)])
def test_range_is_order_and_direction_agnostic(rom1, rom2):
    assert rom_range(rom1, rom2) == (1200, 1800)


@pytest.mark.parametrize("rom1,rom2", [(1500, 1500), (499, 1500), (1500, 2501), (True, 1500), (1500.5, 1600)])
def test_bad_rom_marks_rejected(rom1, rom2):
    with pytest.raises(RomError):
        rom_range(rom1, rom2)


def test_conservative_band_before_rom_and_explicit_widening():
    rom = RomChannel(0, start_us=1500)
    assert rom.band == (1200, 1800) and rom.slider_range() == (1200, 1800)
    assert rom.clamp(2400) == 1800 and rom.clamp(600) == 1200 and rom.clamp(1555.4) == 1555
    assert rom.widen() == (1000, 2000)
    rom.widen(5000)
    assert rom.band == (500, 2500) and not rom.can_widen()
    edge = RomChannel(1, start_us=600)
    assert edge.band == (500, 900)  # never outside 500..2500


def test_marks_clamp_slider_to_range_and_clear_restores_band():
    rom = RomChannel(0, start_us=1500)
    with pytest.raises(RomError):
        rom.mark(1, 1900)  # outside the current band
    rom.mark(2, 1750)
    with pytest.raises(RomError):
        rom.mark(1, 1750)  # identical marks
    rom.mark(1, 1310)
    assert rom.complete and rom.range == (1310, 1750) == rom.slider_range()
    assert rom.clamp(1200) == 1310 and rom.clamp(1800) == 1750
    assert rom.firmware_band() == (1310, 1500, 1750)
    with pytest.raises(RomError):
        rom.widen()
    rom.clear()
    assert not rom.complete and rom.slider_range() == (1200, 1800)


def test_firmware_band_neutral_stays_inside_range():
    rom = RomChannel(0, start_us=1500, band_half_us=600)
    rom.mark(1, 1600)
    rom.mark(2, 1900)
    assert rom.firmware_band() == (1600, 1600, 1900)


# -- sessions against the emulator ---------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_session_moves_slowly_and_firmware_clamps_to_rom(backend):
    with controller(backend) as (ctl, device):
        session = RomSession(ctl, [RomChannel(0, start_us=1500), RomChannel(3, start_us=1400)],
                             host_slew_us_s=1000, firmware_slew_us_s=2000)
        session.start(power_cutoff_confirmed=True)
        # Conservative band pushed to the firmware before arming; nothing driven yet.
        assert (device.channels[0].min_us, device.channels[0].max_us) == (1200, 1800)
        assert device.write_log == []
        with pytest.raises(RomError, match="not enabled"):
            session.set_target(0, 1600)
        assert session.enable(0) == 1500 and device.write_log == [(0, 1500)]  # first PWM = explicit start
        assert session.set_target(0, 2400) == 1800  # slider cannot leave the band
        positions = []
        while session.moving():
            moved = session.step(0.02)          # 20 us per step at 1000 us/s
            positions.append(moved[0])
        steps = [b - a for a, b in zip([1500] + positions, positions)]
        assert positions[-1] == 1800 and max(steps) <= 20 and min(steps) > 0
        assert 3 not in session.moving() and device.output(3) is None  # untouched channel stays released

        ctl.transport.wait_settled(0, 1800, timeout_s=3)
        assert session.mark(0, 2) == 1800
        session.set_target(0, 1300)
        session.run_until_settled(tick_s=0.01)
        ctl.transport.wait_settled(0, 1300, timeout_s=3)
        assert session.mark(0, 1) == 1300
        assert (device.channels[0].min_us, device.channels[0].max_us) == (1300, 1800)
        with pytest.raises(ProtocolError, match="out_of_limits"):
            ctl.move(0, 1290)                  # the firmware itself refuses
        assert session.set_target(0, 1000) == 1300

        session.clear(0)
        assert (device.channels[0].min_us, device.channels[0].max_us) == (1200, 1800)
        session.widen(0)
        assert (device.channels[0].min_us, device.channels[0].max_us) == (1000, 2000)
        session.close()
        assert device.armed is False and device.output(0) is None


def test_mark_while_ramping_holds_at_actual_position():
    with controller("in_process") as (ctl, device):
        session = RomSession(ctl, [RomChannel(0)], host_slew_us_s=2000, firmware_slew_us_s=50)
        session.start(power_cutoff_confirmed=True)
        session.enable(0, 1500)
        session.set_target(0, 1700)
        session.run_until_settled(tick_s=0.01)   # host done; the firmware is still slewing at 50 us/s
        pulse = session.mark(0, 1)
        state = ctl.status(0)
        assert 1500 <= pulse < 1700 and state["target_us"] == pulse and session.targets[0] == pulse


def test_mark_requires_a_driven_channel():
    with controller("in_process") as (ctl, _):
        session = RomSession(ctl, [RomChannel(0)])
        with pytest.raises(RomError, match="not armed"):
            session.mark(0, 1)
        session.start(power_cutoff_confirmed=True)
        with pytest.raises(RomError, match="not enabled"):
            session.mark(0, 1)
        session.close()


def test_no_output_until_an_explicit_start_pulse():
    with controller("in_process") as (ctl, device):
        rom = RomChannel(0, suggested_us=1450)    # nothing saved: only a suggestion
        session = RomSession(ctl, [rom])
        session.start(power_cutoff_confirmed=True)
        assert (device.channels[0].min_us, device.channels[0].max_us) == (1150, 1750)  # band around the suggestion
        with pytest.raises(RomError, match="confirm a start pulse"):
            session.enable(0)
        assert device.write_log == [] and device.output(0) is None
        assert session.enable(0, 1620) == 1620     # operator-confirmed value
        assert device.write_log == [(0, 1620)] and rom.start_source == "operator"
        assert (device.channels[0].min_us, device.channels[0].max_us) == (1320, 1920)  # band re-centred
        with pytest.raises(RomError, match="already enabled"):
            session.enable(0, 1500)


def test_start_must_lie_inside_a_saved_range():
    with controller("in_process") as (ctl, device):
        session = RomSession(ctl, [RomChannel(0, start_us=1400, rom1_us=1300, rom2_us=1700)])
        session.start(power_cutoff_confirmed=True)
        with pytest.raises(RomError, match="outside the saved range"):
            session.enable(0, 1750)
        assert device.write_log == []
        session.enable(0)
        assert device.write_log == [(0, 1400)]


def test_session_refuses_without_cutoff_legacy_firmware_or_latched_estop():
    with controller("in_process") as (ctl, _):
        with pytest.raises(RomError, match="power cutoff"):
            RomSession(ctl, [RomChannel(0)]).start(power_cutoff_confirmed=False)
    with controller("in_process", ReferenceDevice(legacy=True)) as (ctl, device):
        with pytest.raises(ProtocolError, match="does not support"):
            RomSession(ctl, [RomChannel(0)]).start(power_cutoff_confirmed=True)
        assert device.armed is False
    device = ReferenceDevice()
    with controller("in_process", device) as (ctl, _):
        device.external_estop()
        ctl.hello()
        with pytest.raises(RomError, match="e-stop"):
            RomSession(ctl, [RomChannel(0)]).start(power_cutoff_confirmed=True)


def test_estop_releases_and_watchdog_still_applies():
    device = ReferenceDevice(watchdog_ms=120)
    with controller("in_process", device) as (ctl, _):
        session = RomSession(ctl, [RomChannel(0)], host_slew_us_s=2000)
        session.start(power_cutoff_confirmed=True)
        session.enable(0, 1500)
        session.set_target(0, 1600)
        session.run_until_settled(tick_s=0.01)
        time.sleep(0.4)                          # heartbeat thread keeps the session alive
        assert device.armed and device.fault == "none"
        ctl._stop_heartbeat()                    # simulate a hung host
        time.sleep(0.3)
        session.set_target(0, 1500)
        with pytest.raises(ProtocolError, match="disarmed"):
            session.step(0.05)
        assert device.fault == "watchdog" and device.output(0) is None
    with controller("in_process") as (ctl, device):
        session = RomSession(ctl, [RomChannel(0)], host_slew_us_s=2000)
        session.start(power_cutoff_confirmed=True)
        session.enable(0, 1500)
        session.set_target(0, 1550)
        session.run_until_settled(tick_s=0.01)
        session.estop()
        assert device.estop and device.output(0) is None and not ctl.armed
        assert session.last_positions == {0: 1550} and session.channels[0].start_us == 1550


# -- hardware map ---------------------------------------------------------------

def test_set_rom_stores_on_joint_without_touching_permission_band(demo_map, tmp_path):
    updated = set_rom(demo_map, 1, 1700, 1280, set_by="tester", set_at="2026-01-01T00:00:00Z")
    rom = get_rom(updated, 1)
    assert rom["min_us"] == 1280 and rom["max_us"] == 1700 and rom["rom1_us"] == 1700
    joint = updated["joints"]["shoulder"]
    assert (joint["min_us"], joint["max_us"]) == (1450, 1530)
    assert "rom" not in demo_map["joints"]["shoulder"]   # original untouched
    out = tmp_path / "rom.map.json"
    export_atomic(updated, out)
    assert get_rom(load_map(out), 1)["max_us"] == 1700
    with pytest.raises(MapValidationError):
        set_rom(demo_map, 9, 1200, 1300, set_by="x")
    with pytest.raises(RomError):
        set_rom(demo_map, 1, 1200, 1300, set_by="")


def test_map_rejects_inconsistent_rom(demo_map):
    demo_map["joints"]["base_yaw"]["rom"] = {"rom1_us": 1200, "rom2_us": 1800, "min_us": 1200, "max_us": 1700,
                                            "set_by": "x", "set_at": "y"}
    with pytest.raises(MapValidationError):
        validate_map(demo_map)


def test_channels_from_map_reloads_saved_rom(demo_map):
    updated = apply_rom_to_map(demo_map, [RomChannel(0, start_us=1500, rom1_us=1250, rom2_us=1790)], set_by="t")
    roms = {rom.channel: rom for rom in channels_from_map(updated)}
    assert roms[0].range == (1250, 1790) and roms[0].start_us == 1520 and roms[0].start_source == "rom_midpoint"
    # No saved position or ROM: no start pulse, only the approved neutral as a suggestion.
    assert roms[1].rom1_us is None and roms[1].start_us is None and roms[1].suggested_us == 1490
    assert roms[0].band[0] <= 1250 and roms[0].band[1] >= 1790
    assert [rom.channel for rom in channels_from_map(updated, [2])] == [2]


def test_last_position_is_saved_and_becomes_the_next_start(demo_map, tmp_path):
    from robo_prober.rom import set_last_positions

    path = tmp_path / "m.json"
    with controller("in_process") as (ctl, device):
        session = RomSession(ctl, channels_from_map(demo_map, [1]), host_slew_us_s=2000)
        session.start(power_cutoff_confirmed=True)
        session.enable(1, 1490)
        session.set_target(1, 1600)
        session.run_until_settled(tick_s=0.01)
        session.close()                             # disarm remembers where the channel was
        assert device.output(1) is None
    assert session.last_positions == {1: 1600}
    export_atomic(set_last_positions(demo_map, session.last_positions, at="2026-01-01T00:00:00Z"), path)
    saved = load_map(path)
    assert saved["joints"]["shoulder"]["last_position"]["pulse_us"] == 1600
    rom = channels_from_map(saved, [1])[0]
    assert rom.start_us == 1600 and rom.start_source == "last_position"
    with controller("in_process") as (ctl, device):
        session = RomSession(ctl, [rom])
        session.start(power_cutoff_confirmed=True)
        session.enable(1)                           # no value needed: it resumes where it stopped
        assert device.write_log == [(1, 1600)]
    bad = load_map(path)
    bad["joints"]["shoulder"]["last_position"]["pulse_us"] = 3000
    with pytest.raises(MapValidationError):
        validate_map(bad)


def test_cli_rom_records_and_pushes_to_emulator(demo_map, tmp_path, capsys):
    path = tmp_path / "demo.map.json"
    export_atomic(demo_map, path)
    code = cli.main(["rom", "--map", str(path), "--channel", "2", "--rom1", "1720", "--rom2", "1290",
                     "--operator", "tester", "--emulator"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rom"]["min_us"] == 1290 and payload["rom"]["max_us"] == 1720
    assert payload["firmware_limit"]["min_us"] == "1290" and payload["firmware_limit"]["neutral_us"] == "1510"
    assert get_rom(load_map(path), 2)["set_by"] == "tester"
    assert cli.main(["rom", "--map", str(path), "--channel", "2", "--rom1", "1500", "--rom2", "1500",
                     "--operator", "tester"]) == 1


def test_manual_heartbeat_mode_releases_when_caller_stops_stepping():
    device = ReferenceDevice(watchdog_ms=150)
    with controller("in_process", device) as (ctl, _):
        session = RomSession(ctl, [RomChannel(0)], host_slew_us_s=2000, heartbeat="manual")
        session.start(power_cutoff_confirmed=True)
        session.enable(0, 1500)
        session.set_target(0, 1600)
        for _ in range(40):                      # stepping (moving or idle) keeps it alive
            session.step(0.01)
            time.sleep(0.01)
        assert device.armed and device.fault == "none"
        time.sleep(0.3)                          # a frozen window: no more steps
        with pytest.raises(ProtocolError, match="disarmed"):
            session.step(0.01)
        assert device.fault == "watchdog" and device.output(0) is None


def test_narrow_shrinks_band_but_never_past_held_position():
    from robo_prober.rom import MIN_BAND_HALF_US, RomChannel

    rom = RomChannel(channel=3, label="pitch2")
    low, high = rom.band
    n_low, n_high = rom.narrow(100)
    assert n_low == low + 100 and n_high == high - 100
    for _ in range(20):
        rom.narrow(100)
    assert rom.band_half_us == MIN_BAND_HALF_US
    rom2 = RomChannel(channel=4, label="x")
    rom2.narrow(1000, keep_inside=rom2.center + 150)
    assert rom2.band_half_us == 150          # the held position stays inside, so the firmware accepts it
