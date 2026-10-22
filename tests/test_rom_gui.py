"""Smoke test: build the real ROM window against the emulator. Skipped without a display."""
import os
import sys

import pytest

tk = pytest.importorskip("tkinter")

pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="GUI tests are not run on macOS")


@pytest.fixture
def app(tmp_path):
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no X display (run under xvfb-run)")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.withdraw()
    from robo_prober.cli import demo_confirmations, demo_fixture_path, demo_permissions
    from robo_prober.map_schema import export_atomic
    from robo_prober.permissions import load_permissions
    from robo_prober.rom_gui import RomApp
    from robo_prober.session import build_map_from_session

    hw = build_map_from_session(controller_identity={}, permissions=load_permissions(demo_permissions()),
                                observation_paths=[demo_fixture_path()], confirmations=demo_confirmations(),
                                probe_events=[])
    path = tmp_path / "demo.map.json"
    export_atomic(hw, path)
    application = RomApp(root, map_path=path, port="emulator", operator="gui-test", run_timer=False,
                         host_slew_us_s=2000)
    yield application
    if not application._closed:
        application.close()


def drag(app, channel, value):
    """Move the slider the way a user would (through its variable)."""
    app.rows[channel]["var"].set(value)
    app.root.update()
    assert app.session.targets[channel] == value


def settle(app, channel, target):
    import time

    for _ in range(400):
        app.tick(0.05)
        app.root.update()
        time.sleep(0.01)
        if app.controller.position(channel) == target and app.controller.status(channel)["current_us"] == target:
            return
    raise AssertionError("did not settle")


def test_rom_window_end_to_end(app):
    from robo_prober import theme
    from robo_prober.map_schema import load_map
    from robo_prober.rom import get_rom

    assert str(app.root.cget("background")) == theme.BG
    assert app.rows[0]["strip"].cget("background") == theme.SURFACE
    assert set(app.rows) == {0, 1, 2}
    assert str(app.rows[0]["scale"].cget("state")) == "disabled"
    app.connect_and_arm()
    assert app.last_error and "power cutoff" in app.last_error and not app.armed
    app.cutoff_var.set(True)
    app.connect_and_arm()
    assert app.armed, app.last_error
    device = app.controller.transport.connection.device
    # Armed, but nothing outputs until each channel is enabled at a visible start value.
    assert device.write_log == []
    assert str(app.rows[0]["scale"].cget("state")) == "disabled"
    warning = app.rows[0]["warning"].cget("text")
    assert "Servo will move to 1500 us from an unknown position" in warning
    app.rows[0]["start_var"].set("1520")
    app.root.update()
    assert "1520 us" in app.rows[0]["warning"].cget("text")
    app.enable(0)
    assert device.write_log == [(0, 1520)] and app.rows[0]["warning"].cget("text") == ""
    assert str(app.rows[0]["scale"].cget("state")) == "normal"
    assert device.output(1) is None and device.output(2) is None
    scale = app.rows[0]["scale"]
    assert (int(float(scale.cget("from"))), int(float(scale.cget("to")))) == (1220, 1820)

    drag(app, 0, 1750)
    settle(app, 0, 1750)
    app.mark(0, 1)
    drag(app, 0, 1300)
    settle(app, 0, 1300)
    app.mark(0, 2)
    assert app.roms[0].range == (1300, 1750)
    assert (int(float(scale.cget("from"))), int(float(scale.cget("to")))) == (1300, 1750)
    assert (device.channels[0].min_us, device.channels[0].max_us) == (1300, 1750)

    saved = app.save()
    assert saved is not None and get_rom(load_map(saved), 0)["min_us"] == 1300

    app.estop()
    assert app.estop_latched and not app.armed and device.output(0) is None
    assert load_map(saved)["joints"]["base_yaw"]["last_position"]["pulse_us"] == 1300
    assert app.rows[0]["start_var"].get() == "1300"     # next session starts where it stopped
    app.clear_estop()
    assert not app.estop_latched
    app.connect_and_arm()
    assert app.armed
    app.enable(0)
    assert device.write_log[-1] == (0, 1300)
    drag(app, 0, 1400)
    settle(app, 0, 1400)
    app.close()                                          # always disarms, saves last position
    assert not device.armed and device.output(0) is None
    assert load_map(saved)["joints"]["base_yaw"]["last_position"]["pulse_us"] == 1400


def test_window_controls(app):
    app.toggle_fullscreen(True)
    app.root.update()
    app.toggle_fullscreen(False)
    app.root.event_generate("<Escape>")              # keyboard E-STOP with nothing connected
    app.root.update()
    assert not app.armed
    app.request_close()                              # not armed: no question asked
    assert app._closed


def test_start_value_follows_last_position_without_a_map(tmp_path):
    """Found by the virtual bench (DTG sim/): with no map loaded, E-STOP kept the
    old start value on screen, and the next Enable snapped the servo back to it."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no X display (run under xvfb-run)")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.withdraw()
    from robo_prober.rom_gui import RomApp
    app = RomApp(root, port="emulator", channels=[0], operator="gui-test", run_timer=False, host_slew_us_s=2000)
    try:
        app.cutoff_var.set(True)
        app.connect_and_arm()
        app.rows[0]["start_var"].set("1500")
        app.enable(0)
        drag(app, 0, 1350)
        settle(app, 0, 1350)
        app.estop()
        assert app.rows[0]["start_var"].get() == "1350"
        app.clear_estop()
        app.connect_and_arm()
        app.enable(0)
        assert app.controller.transport.connection.device.write_log[-1] == (0, 1350)
    finally:
        app.close()
