"""Range of motion (ROM): find each servo's safe pulse range by hand.

No servo model is assumed: everything is pulse width in microseconds. The
operator moves a channel slowly to one end of its safe travel and marks
ROM 1, then to the other end and marks ROM 2. The safe range is
[min(ROM 1, ROM 2), max(ROM 1, ROM 2)], so it does not matter which way the
servo turns, which end is marked first, or how the horn is mounted.

Until both marks exist a channel can only move inside a conservative band
around its start pulse (default +/-300 us), which the operator may widen
explicitly. Once both marks exist the firmware is told to clamp the channel
to the range. This module holds the logic used by the ROM window, the
``robo-prober rom`` command and the Python API; it has no GUI code.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .map_schema import MapValidationError, validate_map

ABS_MIN_US = 500
ABS_MAX_US = 2500
DEFAULT_START_US = 1500
DEFAULT_BAND_HALF_US = 300
WIDEN_STEP_US = 200
NARROW_STEP_US = 100
MIN_BAND_HALF_US = 25
DEFAULT_HOST_SLEW_US_S = 200
DEFAULT_FIRMWARE_SLEW_US_S = 250
REQUIRED_CAPS = ("limit", "status", "hb", "token")


class RomError(ValueError):
    pass


def channel_name(channel: int) -> str:
    """"ch 3" for a PCA9685 output, "GPIO 4" for channel 104 (a servo straight on a pin)."""
    return f"GPIO {channel - 100}" if channel >= 100 else f"ch {channel}"


def _pulse(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not ABS_MIN_US <= value <= ABS_MAX_US:
        raise RomError(f"{name} must be an integer pulse in {ABS_MIN_US}..{ABS_MAX_US} us, got {value!r}")
    return value


def rom_range(rom1_us: int, rom2_us: int) -> tuple[int, int]:
    """The safe range from two marks, in either order."""
    a, b = _pulse(rom1_us, "ROM 1"), _pulse(rom2_us, "ROM 2")
    if a == b:
        raise RomError("ROM 1 and ROM 2 must be different pulses")
    return min(a, b), max(a, b)


@dataclass
class RomChannel:
    """ROM state of one servo channel.

    ``start_us`` is the first pulse the channel will be given. It is never a
    built-in default: it comes from the last position saved in the hardware
    map, from the ROM midpoint, or from the operator. Until it is known,
    ``suggested_us`` (e.g. the joint's approved neutral) only centres the
    display and the conservative band.

    ``safe_us`` (optional) is a known servo type's safe start band
    (``servo_db.safe_pulse_range``): with it, the band before ROM is that
    range (always including the start pulse) instead of start +/- half.
    """

    channel: int
    label: str = ""
    start_us: int | None = None
    band_half_us: int = DEFAULT_BAND_HALF_US
    rom1_us: int | None = None
    rom2_us: int | None = None
    suggested_us: int = DEFAULT_START_US
    start_source: str = "none"
    safe_us: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.channel, bool) or not isinstance(self.channel, int) or \
                not (0 <= self.channel <= 15 or self.channel >= 100):
            raise RomError("channel must be 0..15 (PCA9685) or 100+N (GPIO N)")
        if self.start_us is not None:
            _pulse(self.start_us, "start pulse")
            if self.start_source == "none":
                self.start_source = "operator"
        _pulse(self.suggested_us, "suggested start pulse")
        if not isinstance(self.band_half_us, int) or self.band_half_us < 1:
            raise RomError("band_half_us must be a positive integer")
        self.label = self.label or channel_name(self.channel)
        if self.safe_us is not None:
            low, high = (_pulse(int(v), "safe band") for v in self.safe_us)
            if not low < high:
                raise RomError("safe band must be low < high")
            self.safe_us = (low, high)

    @property
    def center(self) -> int:
        return self.start_us if self.start_us is not None else self.suggested_us

    @property
    def band(self) -> tuple[int, int]:
        """Where the channel may move before its ROM is set."""
        if self.safe_us is not None:
            low, high = self.safe_us
            return min(low, self.center), max(high, self.center)
        return max(ABS_MIN_US, self.center - self.band_half_us), min(ABS_MAX_US, self.center + self.band_half_us)

    @property
    def half_span(self) -> int:
        """The larger side of the band, from the start pulse."""
        low, high = self.band
        return max(self.center - low, high - self.center)

    def can_narrow(self) -> bool:
        low, high = self.band
        return not self.complete and (self.center - low > MIN_BAND_HALF_US or high - self.center > MIN_BAND_HALF_US)

    @property
    def complete(self) -> bool:
        return self.rom1_us is not None and self.rom2_us is not None

    @property
    def range(self) -> tuple[int, int] | None:
        return rom_range(self.rom1_us, self.rom2_us) if self.complete else None

    def slider_range(self) -> tuple[int, int]:
        return self.range or self.band

    def clamp(self, pulse_us: float) -> int:
        low, high = self.slider_range()
        return min(max(int(round(pulse_us)), low), high)

    def can_widen(self) -> bool:
        return not self.complete and self.band != (ABS_MIN_US, ABS_MAX_US)

    def widen(self, step_us: int = WIDEN_STEP_US) -> tuple[int, int]:
        if self.complete:
            raise RomError("clear the ROM before widening the band")
        if step_us < 1:
            raise RomError("widen step must be positive")
        if self.safe_us is not None:
            low, high = self.band
            self.safe_us = (max(ABS_MIN_US, low - step_us), min(ABS_MAX_US, high + step_us))
            return self.band
        full = max(self.center - ABS_MIN_US, ABS_MAX_US - self.center)
        self.band_half_us = min(self.band_half_us + step_us, full)
        return self.band

    def narrow(self, step_us: int = NARROW_STEP_US, keep_inside: int | None = None) -> tuple[int, int]:
        """Shrink the allowed band around its center; never past a held position."""
        if self.complete:
            raise RomError("clear the ROM before narrowing the band")
        if step_us < 1:
            raise RomError("narrow step must be positive")
        if self.safe_us is not None:
            low, high = self.band
            c = self.center
            new_low = min(low + step_us, c - MIN_BAND_HALF_US) if c - low > MIN_BAND_HALF_US else low
            new_high = max(high - step_us, c + MIN_BAND_HALF_US) if high - c > MIN_BAND_HALF_US else high
            if keep_inside is not None:
                new_low, new_high = min(new_low, keep_inside), max(new_high, keep_inside)
            self.safe_us = (max(ABS_MIN_US, new_low), min(ABS_MAX_US, new_high))
            return self.band
        floor = MIN_BAND_HALF_US if keep_inside is None else max(MIN_BAND_HALF_US, abs(keep_inside - self.center))
        self.band_half_us = max(self.band_half_us - step_us, floor)
        return self.band

    def set_start(self, pulse_us: int, source: str = "operator") -> None:
        pulse_us = _pulse(pulse_us, "start pulse")
        if self.complete:
            low, high = self.range
            if not low <= pulse_us <= high:
                raise RomError(f"start pulse {pulse_us} us is outside the saved range {low}..{high} us")
        self.start_us = pulse_us
        self.start_source = source

    def mark(self, which: int, pulse_us: int) -> None:
        if which not in (1, 2):
            raise RomError("mark ROM 1 or ROM 2")
        pulse_us = _pulse(pulse_us, f"ROM {which}")
        low, high = self.slider_range()
        if not low <= pulse_us <= high:
            raise RomError(f"{pulse_us} us is outside the allowed {low}..{high} us")
        other = self.rom2_us if which == 1 else self.rom1_us
        if other is not None and other == pulse_us:
            raise RomError("ROM 1 and ROM 2 must be different pulses; move the slider first")
        if which == 1:
            self.rom1_us = pulse_us
        else:
            self.rom2_us = pulse_us

    def clear(self, keep_inside: int | None = None) -> None:
        """Forget both marks; the band grows if needed to keep ``keep_inside`` reachable."""
        self.rom1_us = None
        self.rom2_us = None
        if keep_inside is not None:
            self.band_half_us = max(self.band_half_us, abs(keep_inside - self.center))
            if self.safe_us is not None:
                self.safe_us = (min(self.safe_us[0], keep_inside), max(self.safe_us[1], keep_inside))

    def firmware_band(self) -> tuple[int, int, int]:
        """(min, neutral, max) for the firmware LIMIT command."""
        low, high = self.slider_range()
        return low, min(max(self.center, low), high), high


class RomSession:
    """Drive channels slowly toward slider targets and record ROM marks.

    A channel outputs nothing until ``enable(channel, start_us)`` sends its
    explicit start pulse. Hobby servos report no position, so that first
    pulse moves the servo from wherever it is; the start pulse should be the
    last saved position and the operator should confirm the joint is clear.

    ``step(dt_s)`` moves every enabled channel at most
    ``host_slew_us_s * dt_s`` toward its target; the firmware applies its own
    slew limit on top. With ``heartbeat="manual"`` each ``step`` also feeds the
    firmware watchdog, so if the caller stops stepping (a frozen window) the
    firmware releases every output.
    """

    def __init__(self, controller, channels: Iterable[RomChannel], *,
                 host_slew_us_s: int = DEFAULT_HOST_SLEW_US_S,
                 firmware_slew_us_s: int = DEFAULT_FIRMWARE_SLEW_US_S,
                 heartbeat: str = "thread"):
        self.controller = controller
        self.channels = {rom.channel: rom for rom in channels}
        if not self.channels:
            raise RomError("no channels")
        if not 1 <= host_slew_us_s <= 2000 or not 1 <= firmware_slew_us_s <= 2000:
            raise RomError("slew limits must be 1..2000 us/s")
        self.host_slew_us_s = host_slew_us_s
        self.firmware_slew_us_s = firmware_slew_us_s
        self.targets: dict[int, int] = {}
        self.enabled: set[int] = set()
        self.heartbeat = heartbeat
        self.started = False
        self.last_positions: dict[int, int] = {}

    # -- session ----------------------------------------------------------
    def start(self, *, power_cutoff_confirmed: bool, token: str | None = None) -> str:
        """Send each channel's band and arm. No channel outputs anything yet."""
        if not power_cutoff_confirmed:
            raise RomError("confirm that a physical power cutoff is within reach")
        transport = self.controller.transport
        transport.require_caps(*REQUIRED_CAPS)
        mode = self.controller.info.get("mode")
        if mode == "estop":
            raise RomError("the controller e-stop is latched; clear it first")
        if mode == "armed":
            raise RomError("the controller is already armed by another session")
        for rom in self.channels.values():
            self._push_band(rom)
        token = self.controller.arm(power_cutoff_confirmed=True, token=token, heartbeat=self.heartbeat)
        self.targets.clear()
        self.enabled.clear()
        self.started = True
        return token

    def _push_band(self, rom: RomChannel) -> None:
        low, neutral, high = rom.firmware_band()
        self.controller.set_limits(rom.channel, low, high, neutral_us=neutral, slew_us_s=self.firmware_slew_us_s)

    def _require_started(self) -> None:
        if not self.started or not self.controller.armed:
            raise RomError("not armed")

    def _require_enabled(self, channel: int) -> None:
        self._require_started()
        if channel not in self.enabled:
            raise RomError(f"channel {channel} is not enabled; confirm its start pulse first")

    def enable(self, channel: int, start_us: int | None = None) -> int:
        """Begin output on a channel at an explicit start pulse.

        ``start_us`` defaults to the channel's known start (last saved
        position or ROM midpoint); with neither, the caller must pass one.
        """
        self._require_started()
        rom = self.channels[channel]
        if channel in self.enabled:
            raise RomError(f"channel {channel} is already enabled")
        if start_us is None:
            if rom.start_us is None:
                raise RomError(f"channel {channel} has no saved position; confirm a start pulse")
            start_us = rom.start_us
            source = rom.start_source
        else:
            source = "operator"
        rom.set_start(int(start_us), source)
        self._push_band(rom)  # band re-centred on the confirmed start
        self.controller.start(channel, rom.start_us)
        self.targets[channel] = rom.start_us
        self.enabled.add(channel)
        return rom.start_us

    def set_target(self, channel: int, pulse_us: float) -> int:
        self._require_enabled(channel)
        self.targets[channel] = self.channels[channel].clamp(pulse_us)
        return self.targets[channel]

    def position(self, channel: int) -> int | None:
        return self.controller.position(channel)

    def step(self, dt_s: float) -> dict[int, int]:
        """Advance every moving channel one host slew step; returns new positions."""
        self._require_started()
        max_step = max(1, int(round(self.host_slew_us_s * max(dt_s, 0.0))))
        moved = {channel: self.controller.step_toward(channel, self.targets[channel], max_step)
                 for channel in self.moving()}
        if not moved and self.heartbeat == "manual":
            self.controller.heartbeat()  # MOVE feeds the watchdog too
        return moved

    def moving(self) -> list[int]:
        """Enabled channels whose commanded pulse has not reached the target yet."""
        return sorted(ch for ch in self.enabled if self.controller.position(ch) != self.targets[ch])

    def run_until_settled(self, *, tick_s: float = 0.02, timeout_s: float = 30.0) -> None:
        """Blocking helper for scripts and tests: step until every target is reached."""
        import time

        deadline = time.monotonic() + timeout_s
        while self.moving():
            if time.monotonic() > deadline:
                raise RomError("channels did not reach their targets in time")
            self.step(tick_s)
            time.sleep(tick_s)

    def mark(self, channel: int, which: int) -> int:
        """Mark the channel's current output pulse as ROM 1 or ROM 2."""
        self._require_enabled(channel)
        rom = self.channels[channel]
        state = self.controller.status(channel)
        if not state["active"]:
            raise RomError("this channel is not being driven")
        pulse = state["current_us"]
        rom.mark(which, pulse)
        # Hold here: stop any ramp still in progress so the firmware target is
        # the marked position before the band changes.
        self.targets[channel] = pulse
        if self.controller.position(channel) != pulse or state["target_us"] != pulse:
            self.controller.move(channel, pulse)
        if rom.complete:
            self._push_band(rom)
        return pulse

    def clear(self, channel: int) -> None:
        rom = self.channels[channel]
        rom.clear(keep_inside=self.controller.position(channel) if channel in self.enabled else None)
        if self.started and self.controller.armed:
            self._push_band(rom)

    def widen(self, channel: int, step_us: int = WIDEN_STEP_US) -> tuple[int, int]:
        rom = self.channels[channel]
        band = rom.widen(step_us)
        if self.started and self.controller.armed:
            self._push_band(rom)
        return band

    def narrow(self, channel: int, step_us: int = NARROW_STEP_US) -> tuple[int, int]:
        rom = self.channels[channel]
        band = rom.narrow(step_us, keep_inside=self.controller.position(channel) if channel in self.enabled else None)
        if self.started and self.controller.armed:
            self._push_band(rom)
        return band

    def _remember_positions(self) -> None:
        """Keep each started channel's last commanded pulse as its next start."""
        self.last_positions.update(self.controller.positions())
        for channel, pulse in self.last_positions.items():
            rom = self.channels.get(channel)
            if rom is not None and (not rom.complete or rom.range[0] <= pulse <= rom.range[1]):
                rom.set_start(pulse, "last_position")

    def estop(self) -> None:
        self._remember_positions()
        self.started = False
        self.enabled.clear()
        self.controller.estop()

    def close(self) -> None:
        self._remember_positions()
        if self.started and self.controller.armed:
            self.controller.disarm()
        self.started = False
        self.enabled.clear()

    def completed(self) -> list[RomChannel]:
        return [rom for rom in self.channels.values() if rom.complete]


# -- hardware map storage ---------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def faulty_servos(hardware_map: dict[str, Any]) -> dict[int, tuple[str, str]]:
    """Servos the operator marked faulty (moved one way only): channel -> (name, note)."""
    found = {}
    for name, values in (hardware_map.get("devices") or {}).items():
        if values.get("kind") == "faulty_servo" and values.get("channel") is not None:
            found[int(values["channel"])] = (name, str(values.get("note") or "marked faulty"))
    return found


def faulty_reason(name: str, channel: int, note: str) -> str:
    return f"{name} ({channel_name(channel)}) is marked faulty: {note}. Range of motion is refused for it."


def _joint_for_channel(hardware_map: dict[str, Any], channel: int) -> str:
    for joint, values in hardware_map["joints"].items():
        if int(values["channel"]) == channel:
            return joint
    faulty = faulty_servos(hardware_map).get(channel)
    if faulty is not None:
        raise RomError(faulty_reason(faulty[0], channel, faulty[1]))
    raise MapValidationError(f"channel {channel} is not in the hardware map")


def get_rom(hardware_map: dict[str, Any], channel: int) -> dict[str, Any] | None:
    return hardware_map["joints"][_joint_for_channel(hardware_map, channel)].get("rom")


def set_rom(hardware_map: dict[str, Any], channel: int, rom1_us: int, rom2_us: int, *, set_by: str,
            set_at: str | None = None, method: str = "operator") -> dict[str, Any]:
    """Return a copy of the map with the ROM stored on the channel's joint.

    The joint's approved permission band is not changed; the ROM is the
    operator's measurement to build the next permission file from.
    """
    if not set_by:
        raise RomError("set_by (operator name) is required")
    low, high = rom_range(rom1_us, rom2_us)
    updated = copy.deepcopy(hardware_map)
    joint = _joint_for_channel(updated, channel)
    updated["joints"][joint]["rom"] = {
        "rom1_us": rom1_us,
        "rom2_us": rom2_us,
        "min_us": low,
        "max_us": high,
        "method": method,
        "set_by": set_by,
        "set_at": set_at or _now(),
    }
    validate_map(updated)
    return updated


def apply_rom_to_map(hardware_map: dict[str, Any], channels: Iterable[RomChannel], *, set_by: str,
                     set_at: str | None = None) -> dict[str, Any]:
    updated = hardware_map
    for rom in channels:
        if rom.complete:
            updated = set_rom(updated, rom.channel, rom.rom1_us, rom.rom2_us, set_by=set_by, set_at=set_at,
                              method="operator_slider")
    return updated


def channels_from_map(hardware_map: dict[str, Any], selected: list[int] | None = None, *,
                      band_half_us: int = DEFAULT_BAND_HALF_US) -> list[RomChannel]:
    """One RomChannel per mapped joint.

    Start pulse: the joint's saved last position, else the midpoint of its
    saved ROM, else unknown (the operator must confirm one; the joint's
    approved neutral is only shown as a suggestion).
    """
    rows = []
    for joint, values in hardware_map["joints"].items():
        channel = int(values["channel"])
        if selected and channel not in selected:
            continue
        safe = (values.get("servo_type") or {}).get("safe_start_us")
        rom = RomChannel(channel, joint, None, band_half_us, suggested_us=int(values["neutral_us"]),
                         safe_us=tuple(safe) if safe else None)
        saved = values.get("rom")
        if saved:
            rom.rom1_us, rom.rom2_us = int(saved["rom1_us"]), int(saved["rom2_us"])
        last = (values.get("last_position") or {}).get("pulse_us")
        if last is not None and (not rom.complete or rom.range[0] <= last <= rom.range[1]):
            rom.set_start(int(last), "last_position")
        elif rom.complete:
            rom.set_start(sum(rom.range) // 2, "rom_midpoint")
        if rom.complete:
            # Widen the pre-ROM band so clearing a saved ROM keeps it reachable.
            low, high = rom.range
            rom.band_half_us = max(band_half_us, rom.center - low, high - rom.center)
            if rom.safe_us is not None:
                rom.safe_us = (min(rom.safe_us[0], low), max(rom.safe_us[1], high))
        rows.append(rom)
    missing = set(selected or ()) - {rom.channel for rom in rows}
    faulty = faulty_servos(hardware_map)
    refused = sorted(missing & set(faulty))
    if refused:
        raise RomError(" ".join(faulty_reason(faulty[ch][0], ch, faulty[ch][1]) for ch in refused))
    if missing:
        raise MapValidationError(f"channels not in the hardware map: {sorted(missing)}")
    return sorted(rows, key=lambda rom: rom.channel)


def set_last_positions(hardware_map: dict[str, Any], positions: dict[int, int], *,
                       at: str | None = None) -> dict[str, Any]:
    """Return a copy of the map with each channel's last commanded pulse.

    It is the pulse the channel was outputting when it was released, not a
    measurement; the next session uses it as the start pulse so the servo
    moves as little as possible. Channels not in the map are ignored.
    """
    updated = copy.deepcopy(hardware_map)
    stamp = at or _now()
    for joint, values in updated["joints"].items():
        pulse = positions.get(int(values["channel"]))
        if pulse is not None:
            values["last_position"] = {"pulse_us": int(pulse), "at": stamp,
                                       "note": "last commanded pulse when output stopped; not measured"}
    validate_map(updated)
    return updated
