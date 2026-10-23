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


def test_find_servos_then_range_of_motion(tmp_path):
    """Step 1 discover + watch, step 2 identify (button, servo, LED, a driven pin never offered),
    save the map, step 3 shows only the named servos."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no X display (run under xvfb-run)")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.withdraw()
    import time

    from robo_prober.map_schema import load_map
    from robo_prober.rom_gui import NO_ROWS_TEXT, RomApp
    app = RomApp(root, port="emulator", operator="gui-test", run_timer=False, host_slew_us_s=2000)
    app.wiggle_slew_us_s = 2000
    try:
        assert app.rows == {} and app.no_rows.cget("text") == NO_ROWS_TEXT
        app.discover()
        device = app.controller.transport.connection.device
        assert "PCA9685 at 0x40 on SDA 8 / SCL 9" in app.survey_var.get()
        assert device.write_log == [] and not app.armed
        app.watch_var.set(True)
        app.toggle_watch()
        device.board.set_source(6, 0, 0)          # a button press
        for _ in range(8):
            app.tick()
        assert app.pressed == {6}
        app.watch_var.set(False)
        app.toggle_watch()

        assert app.current_target().key == "GPIO 6"          # buttons are named first
        app.name_var.set("e button")
        app.save_name()
        app.cutoff_var.set(True)
        app.connect_and_arm()
        assert app.armed and app.session is None, app.last_error
        keys = [t.key for t in app._identify_targets()]
        assert "GPIO 7" not in keys                           # driven by something else: never offered
        while app.current_target().key != "GPIO 4":
            app.skip_target()
        app.start_var.set("1500")
        app.delta_var.set("60")
        app.do_wiggle()
        for _ in range(400):
            app.tick(0.05)
            time.sleep(0.005)
            if app.wiggle is None:
                break
        assert app.wiggle is None and app.answer_row.winfo_manager() == "grid", app.last_error
        pulses = [p for ch, p in device.write_log if ch == 104]
        assert (pulses[0], max(pulses), min(pulses), pulses[-1]) == (1500, 1560, 1440, 1500)
        assert device.output(104) is None                     # released after the wiggle
        app.answer("servo")
        app.name_var.set("shoulder")
        app.save_name()
        while app.current_target().key != "GPIO 5":
            app.skip_target()
        app.do_blink()
        assert device.level_log[0] == (105, True)
        app.answer("led")
        app.name_var.set("status LED")
        app.save_name()
        app.answer("unused")                                   # next pin: nothing happened
        assert [i.kind for i in app.found.values()] == ["button", "servo", "led", "unused"]

        path = tmp_path / "found.map.json"
        app.go_to_rom(path)
        assert not app.armed and device.output(104) is None
        saved = load_map(path)
        assert saved["joints"]["shoulder"]["channel"] == 104
        assert saved["devices"]["e_button"]["kind"] == "button"
        assert set(app.rows) == {104}                          # only the named servo
        assert app.rows[104]["frame"].cget("text") == "shoulder  (GPIO 4)"
        app.connect_and_arm()
        app.enable(104)
        assert device.write_log[-1] == (104, 1500)              # starts where the wiggle left it
        app.estop()
        assert device.output(104) is None and app.estop_latched
    finally:
        app.close()


def _fw_root():
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no X display (run under xvfb-run)")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.withdraw()
    return root


FAKE_PORT = "/dev/robo-prober-test-nonexistent"


def _fw_app(tmp_path, *, status, device=None, answer="Back up and install"):
    """A window on a fake serial port: connect goes to the emulator, flashing is mocked."""
    from robo_prober import flasher
    from robo_prober.api import connect
    from robo_prober.rom_gui import RomApp

    calls = {"install": [], "asked": []}

    def install_fn(port, **kwargs):
        calls["install"].append(port)
        calls.pop("device", None)  # after the install the board runs current firmware
        kwargs["progress"]("Backing up", 50, 100)
        return flasher.InstallResult(False, "Installed robo-prober firmware.", tmp_path / "b.bin")

    app = RomApp(_fw_root(), port=FAKE_PORT, run_timer=False, run_async=False, backup_dir=tmp_path,
                 connect_fn=lambda port: connect(emulator=True, device=calls.get("device", device)),
                 firmware_check_fn=lambda port: status, install_fn=install_fn)

    def ask(title, text, choices):
        calls["asked"].append(text)
        return answer

    app.ask_fn = ask
    return app, calls


def test_foreign_firmware_asks_then_installs_and_discovers(tmp_path):
    from robo_prober import flasher
    app, calls = _fw_app(tmp_path, status=flasher.FirmwareStatus("foreign", "This board isn't running robo-prober."))
    try:
        app.discover()
        assert calls["install"] == [FAKE_PORT]
        assert "Back up everything on it to" in calls["asked"][0] and "Unplug servo power" in calls["asked"][0]
        assert app.survey is not None  # continued to Discover
    finally:
        app.close()


def test_cancel_never_flashes(tmp_path):
    from robo_prober import flasher
    app, calls = _fw_app(tmp_path, status=flasher.FirmwareStatus("foreign", "x"), answer=None)
    try:
        app.discover()
        assert calls["install"] == [] and app.controller is None and app.survey is None
    finally:
        app.close()


def test_current_firmware_skips_install(tmp_path):
    from robo_prober import flasher
    app, calls = _fw_app(tmp_path, status=flasher.FirmwareStatus("current", "already current"))
    try:
        app.discover()
        assert calls["install"] == [] and calls["asked"] == [] and app.survey is not None
    finally:
        app.close()


def test_older_robo_prober_firmware_offers_install_not_error(tmp_path):
    """HELLO looked fine, but Discover hits a missing cap: offer the same backup-and-install."""
    from robo_prober import flasher
    from robo_prober.reference_device import ReferenceDevice

    class OldFirmware(ReferenceDevice):
        def _cmd_hello(self, fields, now_ms):
            return super()._cmd_hello(fields, now_ms).replace("fw=0.3.0", "fw=0.2.0").split(" caps=")[0] + \
                " caps=limit,status,hb,token,estop,start fault=none"

    app, calls = _fw_app(tmp_path, status=flasher.FirmwareStatus("current", "ok"))
    try:
        calls["device"] = OldFirmware()
        app.discover()
        assert calls["asked"] and "older robo-prober firmware" in calls["asked"][0]
        assert calls["install"] == [FAKE_PORT]
        assert app.survey is not None  # the new firmware answers Discover
    finally:
        app.close()


def test_identify_back_and_table_jump_redo(tmp_path):
    """Back returns to the previous target; clicking a found row redoes it and replaces the old result."""
    from robo_prober import find
    from robo_prober.rom_gui import RomApp

    root = _fw_root()
    app = RomApp(root, port="emulator", operator="gui-test", run_timer=False, host_slew_us_s=2000)
    try:
        assert app.delta_var.get() == "80" == str(find.DEFAULT_DELTA_US)
        app.discover()
        targets = [t.key for t in app._identify_targets()]
        assert len(targets) >= 3
        first = app.current_target().key
        assert first == targets[0] and app.back_btn.instate(["disabled"])
        app.answer("unused")
        second = app.current_target().key
        assert second == targets[1] and app.back_btn.instate(["!disabled"])
        app.skip_target()
        assert app.current_target().key == targets[2]

        app.back_target()                                   # back to the skipped one
        assert app.current_target().key == second and second not in app.skipped
        app.back_target()                                   # and to the first (answered "nothing")
        assert app.current_target().key == first
        app.answer("led")
        app.name_var.set("status LED")
        app.save_name()
        assert app.found[first].kind == "led" and app.found[first].name == "status LED"
        assert app.current_target().key == second           # walk continues where it left off

        # click the row in the Pin / channel table: jump there and redo it
        app.root.update()
        row = next(r for r in app.found_table.get_children() if app.found_table.item(r, "values")[0] == first)
        app.found_table.selection_set(row)
        app._table_clicked(app.found_table)
        assert app.current_target().key == first and app.steps.index("current") == 1
        app.answer("unused")
        assert app.found[first].kind == "unused" and len([k for k in app.found if k == first]) == 1
        assert app.current_target().key == second

        assert app.jump_to("GPIO 999") is False and app.current_target().key == second
    finally:
        app.close()
