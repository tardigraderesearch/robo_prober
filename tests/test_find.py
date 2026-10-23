"""The find-servos cascade against the emulated board.

Emulated wiring (reference_device.DEFAULT_SOURCES / fake_board.h): a servo on
GPIO4, an LED on GPIO5, a button with a pull-up on GPIO6, another chip's
output driving GPIO7 low, a PCA9685 on GPIO8/9.
"""
import time

import pytest

from robo_prober.api import connect
from robo_prober.find import Found, Target, Wiggle, blink, build_map, discover, targets
from robo_prober.reference_device import ReferenceDevice
from robo_prober.rom import channels_from_map
from robo_prober.transport import ProtocolError


@pytest.fixture
def setup():
    device = ReferenceDevice()
    ctl = connect(emulator=True, device=device)
    yield ctl, device
    ctl.close()


def test_discover_classifies_pins_without_driving_anything(setup):
    ctl, device = setup
    survey = discover(ctl)
    classes = {info.pin: info.cls for info in survey.pins}
    assert (classes[4], classes[5], classes[6], classes[7]) == ("floating", "pulled_down", "pulled_up", "driven_low")
    assert 8 not in classes and 19 not in classes          # PCA9685 bus and USB: not offered
    assert survey.pca_bus == (8, 9) and survey.pca_addresses == ["0x40"]
    assert survey.i2c == {(8, 9): ["0x40", "0x70"]}
    assert "PCA9685 at 0x40 on SDA 8 / SCL 9" in survey.summary()
    assert device.write_log == [] and device.level_log == []
    keys = [t.key for t in targets(survey)]
    pins = [int(k.split()[1]) for k in keys if k.startswith("GPIO ")]
    assert pins == sorted(pins) and "GPIO 4" in keys and keys[-1].startswith("PCA9685 ch")  # pin-number order
    assert "GPIO 7" not in keys                              # driven: never a target
    assert keys.index("GPIO 5") < keys.index("GPIO 21")      # pin order, whatever the class
    assert "GPIO 0" not in keys and "GPIO 45" not in keys      # boot pins left out by default
    assert [t for t in targets(survey, include_boot_pins=True) if t.key == "GPIO 0"][0].last  # opt-in: still confirmed


def test_cascade_finds_servo_led_button_and_never_drives_a_driven_pin(setup):
    ctl, device = setup
    survey = discover(ctl)
    ctl.watch(True)
    device.board.set_source(6, 0, 0)                         # operator presses the button
    time.sleep(0.01)
    ctl.events()
    device.board.set_source(6, 3300, 10000)
    time.sleep(0.01)
    pressed = {pin for pin, _ in ctl.events()}
    ctl.watch(False)
    assert pressed == {6}

    ctl.arm(power_cutoff_confirmed=True, heartbeat="manual")
    servo = Target(4, cls="floating")
    Wiggle(ctl, servo, 1500, 60, slew_us_s=2000).run(timeout_s=5)
    pulses = [p for ch, p in device.write_log if ch == 104]
    assert pulses[0] == 1500 and max(pulses) == 1560 and min(pulses) == 1440 and pulses[-1] == 1500
    assert device.output(104) is None                         # released after the probe
    assert (device.channels[16 + 4].min_us, device.channels[16 + 4].max_us) == (1400, 1600)

    blink(ctl, Target(5, cls="pulled_down"))
    assert device.level_log[0] == (105, True)

    with pytest.raises(ProtocolError, match="pin_driven"):
        Wiggle(ctl, Target(7, cls="floating"), 1500, 60, slew_us_s=2000).run(timeout_s=5)
    with pytest.raises(ProtocolError, match="pin_driven"):
        ctl.blink(7, 1)
    assert all(ch != 107 for ch, _ in device.write_log + device.level_log)

    Wiggle(ctl, Target(pca_channel=3), 1500, 60, slew_us_s=2000).run(timeout_s=5)
    assert device.output(3) == 1500                           # a PCA9685 channel stays held
    ctl.disarm()
    assert device.output(3) is None and device.output(105) is None

    found = [Found(servo, "servo", "shoulder", 1500), Found(Target(5), "led", "status LED"),
             Found(Target(6), "button", "e button"), Found(Target(pca_channel=3), "servo", "elbow", 1500),
             Found(Target(10), "unused")]
    hw = build_map(ctl.info, survey, found)
    assert hw["joints"]["shoulder"]["channel"] == 104 and hw["joints"]["elbow"]["channel"] == 3
    assert hw["devices"]["status_LED"] == {"kind": "led", "pin": 5}
    assert hw["devices"]["e_button"]["kind"] == "button"
    roms = channels_from_map(hw)
    assert [(r.channel, r.label, r.start_us) for r in roms] == [(3, "elbow", 1500), (104, "shoulder", 1500)]


def test_gpio_servo_works_in_the_range_of_motion_session(setup):
    from robo_prober.rom import RomChannel, RomSession

    ctl, device = setup
    session = RomSession(ctl, [RomChannel(104, "shoulder", 1500)], host_slew_us_s=2000, heartbeat="manual")
    session.start(power_cutoff_confirmed=True)
    session.enable(104)
    assert device.write_log == [(104, 1500)]
    session.set_target(104, 1700)
    session.run_until_settled(timeout_s=5)
    assert ctl.position(104) == 1700
    session.close()
    assert device.output(104) is None


def test_wiggle_is_a_quick_buzz():
    from robo_prober import find
    from robo_prober.find import Target, Wiggle

    class Ctl:
        def __init__(self):
            self.limits = None
        def position(self, ch):
            return None
        def set_limits(self, ch, low, high, neutral_us, slew_us_s):
            self.limits = (low, high, slew_us_s)
        def start(self, ch, pulse):
            pass

    ctl = Ctl()
    w = Wiggle(ctl, Target(4, cls="floating"))
    w.begin()
    start, d = find.DEFAULT_START_US, find.DEFAULT_DELTA_US
    assert w.steps == [start + d, start - d] * find.BUZZ_CYCLES + [start]
    assert ctl.limits == (start - d, start + d, 2000)  # fast, but the firmware band still clamps it
    swing_s = sum(abs(b - a) for a, b in zip([start] + w.steps, w.steps)) / ctl.limits[2]
    assert swing_s < 1.0


def test_faulty_servo_is_a_device_never_a_joint_and_rom_refuses_it(setup):
    from robo_prober.find import FAULTY_NOTE
    from robo_prober.map_schema import validate_map
    from robo_prober.rom import RomError, faulty_servos, set_rom

    ctl, _ = setup
    survey = discover(ctl)
    found = [Found(Target(4), "servo", "shoulder", 1500),
             Found(Target(pca_channel=2), "faulty", "pitch2", None, FAULTY_NOTE)]
    hw = build_map(ctl.info, survey, found)
    validate_map(hw)
    assert "pitch2" not in hw["joints"]
    assert hw["devices"]["pitch2"]["kind"] == "faulty_servo" and hw["devices"]["pitch2"]["channel"] == 2
    assert "one way only" in hw["devices"]["pitch2"]["note"]
    assert faulty_servos(hw) == {2: ("pitch2", FAULTY_NOTE)}
    assert [r.channel for r in channels_from_map(hw)] == [104]
    with pytest.raises(RomError, match="pitch2 .*faulty.*refused"):
        channels_from_map(hw, [2])
    with pytest.raises(RomError, match="faulty"):
        set_rom(hw, 2, 1400, 1600, set_by="test")
    with pytest.raises(ValueError, match="no working servo"):
        build_map(ctl.info, survey, found[1:])


def test_servo_type_goes_into_the_map_and_sets_the_starting_band(setup):
    ctl, _ = setup
    survey = discover(ctl)
    hw = build_map(ctl.info, survey, [Found(Target(4), "servo", "pitch", 1500, servo_id="corona-ds-929mg"),
                                      Found(Target(5), "servo", "yaw", 1500, servo_id="no-such-servo")])
    assert hw["joints"]["pitch"]["servo_type"]["safe_start_us"] == [1180, 1820]
    assert "servo_type" not in hw["joints"]["yaw"]                 # unknown id: just the default band
    bands = {r.label: r.band for r in channels_from_map(hw)}
    assert bands == {"pitch": (1180, 1820), "yaw": (1200, 1800)}
