"""Find what is wired to the controller: discover, identify, save a hardware map.

The cascade, driven by the operator (from the window or from Python):

1. Discover, with nothing armed and nothing moving: the board profile
   (BOARD), every usable pin classified read-only (SURVEY: floating,
   pulled up/down, driven by something else, reserved), the PCA9685 the
   firmware found at boot, and the I2C devices on pairs of pulled-up pins.
   WATCH then shows which pins change while the operator presses buttons.
2. Identify, after ARM with the power cutoff confirmed: one target at a time
   (floating pins first, then resistively pulled pins, then PCA9685
   channels, boot-strapping pins last), the operator picks "wiggle as a
   servo" (explicit start pulse, then a quick +/-delta buzz, inside a LIMIT band
   around it) or "blink" (LEDs, buzzers), then says what happened and names
   it. A pin something else drives is never a target, and the firmware
   refuses it anyway (pin_driven). Every GPIO is released after its probe.
3. The named servos go into a hardware map; the range-of-motion step loads it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .catalog import BOARD_PROFILES, PIN_INPUT_ONLY, PIN_LAST, discovery_pin_flags
from .map_schema import SCHEMA_VERSION, stable_json_hash, validate_map
from .rom import ABS_MAX_US, ABS_MIN_US, DEFAULT_BAND_HALF_US, channel_name
from .transport import ProtocolError

GPIO_BASE = 100
DEFAULT_START_US = 1500
DEFAULT_DELTA_US = 80             # ~7 degrees each way, buzzed quickly: easy to see or feel
DEFAULT_WIGGLE_SLEW_US_S = 2000    # the firmware maximum: each 160 us swing takes ~80 ms
BUZZ_CYCLES = 3                   # back-and-forth swings per wiggle (~0.6 s in all)
BLINK_COUNT = 3
MAX_I2C_PAIRS = 30
DRIVEN = ("driven_high", "driven_low")
# A servo that turns the same way for every pulse (or keeps spinning) has broken
# position feedback, or is a continuous-rotation servo: it cannot hold a position,
# so the bench and range of motion refuse it.
FAULTY_NOTE = ("moved one way only / kept spinning for every pulse: broken position feedback "
               "(or a continuous-rotation servo); it cannot hold a position")


@dataclass
class PinInfo:
    pin: int
    cls: str
    adc_mv: int | None = None
    last: bool = False          # boot strapping / JTAG: probed last, after a confirmation
    input_only: bool = False

    @property
    def drivable(self) -> bool:
        return not self.input_only and self.cls in ("floating", "pulled_up", "pulled_down")


@dataclass
class Survey:
    profile_id: str
    model: str
    pins: list[PinInfo]
    pca_bus: tuple[int, int] | None = None     # (sda, scl) of the PCA9685 the firmware drives
    pca_addresses: list[str] = field(default_factory=list)
    i2c: dict[tuple[int, int], list[str]] = field(default_factory=dict)  # (sda, scl) -> addresses

    def pin(self, number: int) -> PinInfo | None:
        return next((info for info in self.pins if info.pin == number), None)

    def summary(self) -> str:
        if self.pca_bus and self.pca_addresses:
            sda, scl = self.pca_bus
            driver = f"Servo driver: PCA9685 at {self.pca_addresses[0]} on SDA {sda} / SCL {scl}."
        else:
            driver = "No PCA9685 found: servos must be on the GPIO pins."
        held = sum(info.cls in DRIVEN for info in self.pins)
        return (f"{self.model} ({self.profile_id}): {len(self.pins)} pins surveyed, "
                f"{held} driven by something else (never driven). {driver}")


@dataclass(frozen=True)
class Target:
    """Something to identify: a GPIO pin or a PCA9685 channel."""
    pin: int | None = None
    pca_channel: int | None = None
    cls: str = ""
    last: bool = False

    @property
    def channel(self) -> int:
        return GPIO_BASE + self.pin if self.pin is not None else self.pca_channel

    @property
    def key(self) -> str:
        return f"GPIO {self.pin}" if self.pin is not None else f"PCA9685 ch {self.pca_channel}"


@dataclass
class Found:
    target: Target
    kind: str                   # servo, faulty (servo that moved one way only), led, button, unknown, unused
    name: str = ""
    start_us: int | None = None
    note: str = ""
    servo_id: str = ""          # servo_db id the operator picked (optional)


def servo_type(servo_id: str) -> dict[str, Any] | None:
    """The map's ``servo_type`` block for a servo_db id: model and safe start band."""
    from . import servo_db

    if not servo_id:
        return None
    try:
        entry = servo_db.get(servo_id)
    except KeyError:
        return None
    low, high = servo_db.safe_pulse_range(entry)
    return {"id": entry["id"], "model": f"{entry['brand']} {entry['model']}", "safe_start_us": [low, high]}


def _split(text: str | None) -> list[str]:
    return [] if not text or text == "none" else text.split(",")


def discover(ctl, *, scan_i2c: bool = True) -> Survey:
    """Read-only survey of the controller's pins and I2C. Needs v0.3 firmware."""
    board = ctl.board()
    profile_id = board.get("board", "")
    flags = discovery_pin_flags(profile_id) if profile_id in BOARD_PROFILES else {}
    pins = []
    for text in _split(board.get("pins")):
        number = int(text)
        reply = ctl.survey(number)
        adc = reply.get("adc", "-")
        bits = flags.get(number, 0)
        pins.append(PinInfo(number, reply.get("class", "reserved"), int(adc) if adc.isdigit() else None,
                            last=bool(bits & PIN_LAST), input_only=bool(bits & PIN_INPUT_ONLY)))
    survey = Survey(profile_id, board.get("chip", "?"), pins)
    bus = _split(ctl.info.get("i2c"))
    if len(bus) == 2:
        survey.pca_bus = (int(bus[0]), int(bus[1]))
        survey.pca_addresses = _split(ctl.info.get("pca9685"))
    if scan_i2c:
        pairs = [survey.pca_bus] if survey.pca_bus else []
        high = [info.pin for info in pins if info.cls in ("pulled_up", "driven_high") and not info.input_only]
        pairs += [(a, b) for a in high for b in high if a != b]
        for sda, scl in pairs[:MAX_I2C_PAIRS]:
            devices, _ = ctl.i2c_scan(sda, scl)
            if devices:
                survey.i2c[(sda, scl)] = devices
    return survey


def targets(survey: Survey) -> list[Target]:
    """What to identify: every drivable GPIO in pin-number order (boot-strapping /
    JTAG pins still ask for a confirmation when reached), then PCA9685 channels.
    Driven, reserved and input-only pins are never targets."""
    usable = sorted((info for info in survey.pins if info.drivable), key=lambda info: info.pin)
    pins = [Target(info.pin, cls=info.cls, last=info.last) for info in usable]
    pca = [Target(pca_channel=ch, cls="pca9685") for ch in range(16)] if survey.pca_addresses else []
    return pins + pca


class Wiggle:
    """Non-blocking servo buzz of one target: start pulse, then +/-delta a few times, back.

    ``begin()`` sends a LIMIT band of start +/- delta, then the
    explicit start pulse (PROBE for a GPIO, START for a PCA9685 channel). The
    small band keeps the fast swing safe: the firmware clamps to it.
    Call ``step()`` repeatedly (a GUI timer, or ``run()``): it waits for the
    firmware to reach each target (STATUS), feeding the watchdog meanwhile.
    A GPIO is released when the wiggle is done (unless ``release=False``); a
    PCA9685 channel stays held at its start pulse until DISARM.
    """

    def __init__(self, ctl, target: Target, start_us: int = DEFAULT_START_US, delta_us: int = DEFAULT_DELTA_US,
                 slew_us_s: int = DEFAULT_WIGGLE_SLEW_US_S, release: bool = True):
        if not 1 <= delta_us <= 400:
            raise ValueError("wiggle delta must be 1..400 us")
        if not ABS_MIN_US <= start_us - delta_us or not start_us + delta_us <= ABS_MAX_US:
            raise ValueError(f"start +/- delta must stay inside {ABS_MIN_US}..{ABS_MAX_US} us")
        self.ctl, self.target = ctl, target
        self.start_us, self.delta_us, self.slew_us_s = start_us, delta_us, slew_us_s
        self.release = release  # False: keep holding the start pulse after the buzz (the bench)
        self.steps: list[int] = []
        self.done = False

    def begin(self) -> None:
        ch, start, delta = self.target.channel, self.start_us, self.delta_us
        held = self.ctl.position(ch)
        low, high = start - delta, start + delta
        if held is not None:
            low, high = min(low, held), max(high, held)
        self.ctl.set_limits(ch, low, high, neutral_us=start, slew_us_s=self.slew_us_s)
        if held is None:
            self.ctl.start(ch, start)
        self.steps = (([start] if held not in (None, start) else [])
                      + [start + delta, start - delta] * BUZZ_CYCLES + [start])

    def step(self) -> bool:
        if self.done:
            return True
        ch = self.target.channel
        state = self.ctl.status(ch)
        if not state["armed"] or not state["active"]:
            raise ProtocolError(f"{self.target.key} was released during the wiggle (stop, e-stop or watchdog)")
        if state["current_us"] != state["target_us"]:
            self.ctl.heartbeat()
            return False
        if self.steps:
            self.ctl.move(ch, self.steps.pop(0))
            return False
        if self.target.pin is not None and self.release:
            self.ctl.release_pin(self.target.pin)
        self.done = True
        return True

    def run(self, timeout_s: float = 30.0, poll_s: float = 0.02) -> None:
        """Blocking: begin and step until done (scripts and tests)."""
        self.begin()
        deadline = time.monotonic() + timeout_s
        while not self.step():
            if time.monotonic() > deadline:
                raise ProtocolError(f"{self.target.key}: wiggle did not finish in time")
            time.sleep(poll_s)


def blink(ctl, target: Target, count: int = BLINK_COUNT) -> None:
    """Slow on/off pulses on a GPIO target; the firmware releases the pin after."""
    if target.pin is None:
        raise ValueError("only GPIO pins can blink")
    ctl.blink(target.pin, count)


def safe_name(text: str, taken: set[str] | None = None) -> str:
    """A map-safe identifier from what the operator typed ("status LED" -> "status_LED")."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", text.strip()).strip("_")[:60] or "part"
    if not name[0].isalpha():
        name = "part_" + name
    base, n = name, 2
    while taken is not None and name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_map(info: dict[str, str], survey: Survey, found: list[Found]) -> dict[str, Any]:
    """Hardware map with the named servos as joints and the rest as devices.

    Each servo's last position is its wiggle start pulse (where it was left),
    so the range-of-motion step starts it there without a jump.
    """
    joints: dict[str, Any] = {}
    devices: dict[str, Any] = {}
    taken: set[str] = set()
    stamp = _now()
    for item in found:
        if item.kind in ("unused", "unknown") or not item.name.strip():
            continue
        name = safe_name(item.name, taken)
        taken.add(name)
        target = item.target
        if item.kind == "servo":
            start = item.start_us or DEFAULT_START_US
            joints[name] = {
                "channel": target.channel, "direction": "unknown", "invert": False,
                "neutral_us": start, "min_us": max(ABS_MIN_US, start - DEFAULT_BAND_HALF_US),
                "max_us": min(ABS_MAX_US, start + DEFAULT_BAND_HALF_US),
                "route": "gpio" if target.pin is not None else "pca9685",
                "last_position": {"pulse_us": start, "at": stamp,
                                  "note": "start pulse of the find-servos wiggle; not measured"},
            }
            if target.pin is not None:
                joints[name]["pin"] = target.pin
            typed = servo_type(item.servo_id)
            if typed is not None:
                joints[name]["servo_type"] = typed
        elif item.kind == "faulty":
            # Never a joint: nothing downstream may try to hold a position with it.
            devices[name] = {"kind": "faulty_servo", "pin": target.pin, "channel": target.channel,
                             "route": "gpio" if target.pin is not None else "pca9685",
                             "note": item.note or FAULTY_NOTE}
        else:
            devices[name] = {"kind": item.kind, "pin": target.pin}
    for (sda, scl), addresses in sorted(survey.i2c.items()):
        for address in addresses:
            name = safe_name(f"i2c_{sda}_{scl}_{address}", taken)
            taken.add(name)
            devices[name] = {"kind": "i2c", "sda": sda, "scl": scl, "address": address,
                             "pca9685": address in survey.pca_addresses and (sda, scl) == survey.pca_bus}
    if not joints:
        raise ValueError("no working servo has a name yet: identify at least one servo before saving")
    controller = {key: info[key] for key in ("id", "fw", "proto") if key in info}
    hardware_map = {
        "schema_version": SCHEMA_VERSION,
        "status": "operator_identified_physical_unverified",
        "identities": {"controller": controller, "board_profile_id": survey.profile_id or "unknown"},
        "references": {"catalog": "robo_prober.catalog (reference metadata only; never a probing permission)",
                       "protocol": "ROBOPROBE/1 reference protocol"},
        "probe_permissions": {},
        "permission_hash": stable_json_hash({}),
        "observations": [],
        "classification_provenance": {
            "method": "find servos: the operator watched each pin wiggle or blink and named what moved",
            "human_confirmed": True,
        },
        "joints": joints,
        "devices": devices,
        "warnings": [],
    }
    validate_map(hardware_map)
    return hardware_map


__all__ = ["DEFAULT_DELTA_US", "DEFAULT_START_US", "FAULTY_NOTE", "Found", "PinInfo", "Survey", "Target", "Wiggle", "blink",
           "build_map", "servo_type", "channel_name", "discover", "safe_name", "targets"]
