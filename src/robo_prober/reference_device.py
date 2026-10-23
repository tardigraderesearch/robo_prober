"""Python model of the ROBOPROBE/1 v0.2 firmware core.

This mirrors ``firmware/esp32_robo_prober/src/rp_core.cpp`` command for
command, so the host can be tested end to end with no hardware: band limits,
slew limiting, the session token, the heartbeat watchdog and the latching
e-stop all behave like the firmware. A conformance test runs the same command
script through this model and through the compiled C++ core and requires
identical replies.

``legacy=True`` emulates the v0.1 reference firmware (no capabilities, no
token, no limits) so the host's compatibility paths can be tested too.

Time is passed in explicitly as milliseconds; nothing here sleeps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PROTOCOL = "ROBOPROBE/1"
FIRMWARE_VERSION = "0.2.0"
CHANNELS = 16
ABS_MIN_US = 500
ABS_MAX_US = 2500
DEFAULT_MIN_US = 1400
DEFAULT_NEUTRAL_US = 1500
DEFAULT_MAX_US = 1600
DEFAULT_SLEW_US_S = 250
MAX_SLEW_US_S = 2000
DEFAULT_WATCHDOG_MS = 1000
MAX_LINE = 160
MAX_TOKEN = 32
MAX_FIELDS = 8
MAX_SLEW_DT_MS = 60000
CAPS = "limit,status,hb,token,estop,start"

_VERB_RE = re.compile(r"^[A-Z_]+$")
_KEY_RE = re.compile(r"^[a-z_]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_UINT_RE = re.compile(r"^[0-9]{1,10}$")


@dataclass
class ChannelState:
    min_us: int = DEFAULT_MIN_US
    neutral_us: int = DEFAULT_NEUTRAL_US
    max_us: int = DEFAULT_MAX_US
    slew_us_s: int = DEFAULT_SLEW_US_S
    active: bool = False
    current_us: int = 0
    target_us: int = 0
    slew_acc: int = 0
    last_ms: int = 0


class _ParseError(Exception):
    pass


def _parse(line: str) -> tuple[str, dict[str, str]]:
    text = line.strip(" ")
    if not text:
        raise _ParseError("empty_command")
    parts = text.split(" ")
    verb = parts[0]
    if not _VERB_RE.match(verb):
        raise _ParseError("unknown_command")
    fields: dict[str, str] = {}
    for part in parts[1:]:
        if not part:
            raise _ParseError("bad_syntax")
        if len(fields) >= MAX_FIELDS:
            raise _ParseError("too_many_fields")
        key, sep, value = part.partition("=")
        if not sep or not key or not _KEY_RE.match(key) or not value or "=" in value:
            raise _ParseError("bad_syntax")
        if key in fields:
            raise _ParseError("duplicate_field")
        fields[key] = value
    return verb, fields


def _fields_ok(fields: dict[str, str], allowed: tuple[str, ...], required: int) -> bool:
    return set(fields) <= set(allowed) and all(key in fields for key in allowed[:required])


def _uint(text: str | None, maximum: int) -> int | None:
    if text is None or not _UINT_RE.match(text):
        return None
    value = int(text)
    return value if value <= maximum else None


class ReferenceDevice:
    """Firmware state machine plus a record of what the outputs are doing."""

    def __init__(self, *, device_id: str = "reference-emulator", outputs: str = "emulated",
                 watchdog_ms: int = DEFAULT_WATCHDOG_MS, legacy: bool = False):
        self.device_id = device_id
        self.outputs_name = outputs
        self.watchdog_ms = watchdog_ms
        self.legacy = legacy
        self.driver_available = True
        self.driver_fails = False
        self.outputs: dict[int, int | None] = {}
        self.write_log: list[tuple[int, int]] = []  # every (channel, pulse_us) written
        self.begin(0)

    # -- output driver ----------------------------------------------------
    def _driver_write(self, channel: int, pulse_us: int) -> bool:
        if self.driver_fails:
            return False
        self.outputs[channel] = pulse_us
        self.write_log.append((channel, pulse_us))
        return True

    def _driver_release_all(self) -> bool:
        self.outputs = {channel: None for channel in range(CHANNELS)}
        return not self.driver_fails

    def output(self, channel: int) -> int | None:
        """Pulse currently written to a channel in microseconds, or None when off."""
        return self.outputs.get(channel)

    # -- core -------------------------------------------------------------
    def begin(self, now_ms: int) -> None:
        self.channels = [ChannelState() for _ in range(CHANNELS)]
        self.armed = False
        self.estop = False
        self.fault = "none"
        self.token = ""
        self.last_feed_ms = now_ms
        self._driver_release_all()

    def _release_all(self, reason: str | None) -> None:
        ok = self._driver_release_all()
        self.armed = False
        self.token = ""
        for state in self.channels:
            # A band lives for one armed session (matches the firmware).
            state.min_us, state.neutral_us = DEFAULT_MIN_US, DEFAULT_NEUTRAL_US
            state.max_us, state.slew_us_s = DEFAULT_MAX_US, DEFAULT_SLEW_US_S
            state.active = False
            state.current_us = 0
            state.target_us = 0
            state.slew_acc = 0
        if reason is not None:
            self.fault = reason
        if not ok:
            self.fault = "driver"

    def external_estop(self) -> None:
        self._release_all("estop")
        self.estop = True

    def _write_channel(self, index: int) -> bool:
        state = self.channels[index]
        pulse = min(max(state.current_us, state.min_us), state.max_us)
        return self._driver_write(index, pulse)

    def tick(self, now_ms: int) -> None:
        if self.armed and now_ms - self.last_feed_ms > self.watchdog_ms:
            self._release_all("watchdog")
            return
        for index, state in enumerate(self.channels):
            if not state.active:
                continue
            dt = now_ms - state.last_ms
            state.last_ms = now_ms
            if state.current_us == state.target_us:
                state.slew_acc = 0
                continue
            dt = min(dt, MAX_SLEW_DT_MS)
            state.slew_acc += state.slew_us_s * dt
            step = state.slew_acc // 1000
            if step == 0:
                continue
            state.slew_acc %= 1000
            distance = abs(state.target_us - state.current_us)
            if step >= distance:
                state.current_us = state.target_us
                state.slew_acc = 0
            elif state.target_us > state.current_us:
                state.current_us += step
            else:
                state.current_us -= step
            if not self._write_channel(index):
                self._release_all("driver")
                return

    def handle(self, line: str, now_ms: int) -> str:
        if self.legacy:
            return self._handle_legacy(line)
        self.tick(now_ms)
        if len(line) > MAX_LINE:
            return "RP1 ERR reason=line_too_long"
        try:
            verb, fields = _parse(line)
        except _ParseError as error:
            return f"RP1 ERR reason={error}"
        handler = getattr(self, "_cmd_" + verb.lower(), None)
        if handler is None:
            return "RP1 ERR reason=unknown_command"
        return handler(fields, now_ms)

    def _cmd_hello(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        mode = "estop" if self.estop else ("armed" if self.armed else "disarmed")
        return (f"RP1 OK proto={PROTOCOL} mode={mode} id={self.device_id} fw={FIRMWARE_VERSION} "
                f"outputs={self.outputs_name} watchdog_ms={self.watchdog_ms} caps={CAPS} fault={self.fault}")

    def _cmd_arm(self, fields, now_ms):
        if not _fields_ok(fields, ("token", "cutoff"), 2):
            return "RP1 ERR reason=missing_arm_fields"
        if self.estop:
            return "RP1 ERR reason=estop_latched"
        if self.armed:
            return "RP1 ERR reason=already_armed"
        if fields["cutoff"] != "confirmed":
            return "RP1 ERR reason=cutoff_unconfirmed"
        if not _TOKEN_RE.match(fields["token"]):
            return "RP1 ERR reason=bad_token"
        if not self.driver_available:
            return "RP1 ERR reason=driver_unavailable"
        self.token = fields["token"]
        self.armed = True
        self.fault = "none"
        self.last_feed_ms = now_ms
        return "RP1 OK armed=1"

    def _cmd_disarm(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        self._release_all(None)
        return "RP1 OK armed=0"

    def _cmd_stop(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        self._release_all("stop")
        return "RP1 OK stopped=1"

    def _cmd_estop(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        self.external_estop()
        return "RP1 OK estop=1"

    def _cmd_estop_clear(self, fields, now_ms):
        if not _fields_ok(fields, ("cutoff",), 1):
            return "RP1 ERR reason=bad_fields"
        if fields["cutoff"] != "confirmed":
            return "RP1 ERR reason=cutoff_unconfirmed"
        self.estop = False
        return "RP1 OK estop=0"

    def _cmd_limit(self, fields, now_ms):
        if not _fields_ok(fields, ("channel", "min_us", "neutral_us", "max_us", "slew_us_s"), 4):
            return "RP1 ERR reason=bad_fields"
        channel = _uint(fields["channel"], CHANNELS - 1)
        if channel is None:
            return "RP1 ERR reason=bad_channel"
        lo = _uint(fields["min_us"], ABS_MAX_US)
        mid = _uint(fields["neutral_us"], ABS_MAX_US)
        hi = _uint(fields["max_us"], ABS_MAX_US)
        if lo is None or mid is None or hi is None or lo < ABS_MIN_US or lo >= hi or not lo <= mid <= hi:
            return "RP1 ERR reason=bad_limits"
        slew = DEFAULT_SLEW_US_S
        if "slew_us_s" in fields:
            slew = _uint(fields["slew_us_s"], MAX_SLEW_US_S)
            if not slew:
                return "RP1 ERR reason=bad_slew"
        state = self.channels[channel]
        if state.active and not (lo <= state.current_us <= hi and lo <= state.target_us <= hi):
            return "RP1 ERR reason=position_outside_limits"
        state.min_us, state.neutral_us, state.max_us, state.slew_us_s = lo, mid, hi, slew
        return f"RP1 OK channel={channel} min_us={lo} neutral_us={mid} max_us={hi} slew_us_s={slew}"

    def _cmd_move(self, fields, now_ms):
        if not _fields_ok(fields, ("channel", "target_us", "seq", "token"), 3):
            return "RP1 ERR reason=bad_move_fields"
        if self.estop:
            return "RP1 ERR reason=estop_latched"
        if not self.armed:
            return "RP1 ERR reason=disarmed"
        if "token" not in fields:
            return "RP1 ERR reason=token_required"
        if fields["token"] != self.token:
            return "RP1 ERR reason=bad_token"
        channel = _uint(fields["channel"], CHANNELS - 1)
        if channel is None:
            return "RP1 ERR reason=bad_channel"
        target = _uint(fields["target_us"], ABS_MAX_US)
        seq = _uint(fields["seq"], 0xFFFFFFFF)
        if target is None or seq is None:
            return "RP1 ERR reason=bad_move_values"
        state = self.channels[channel]
        if not state.active:
            return "RP1 ERR reason=not_started"
        if not state.min_us <= target <= state.max_us:
            return "RP1 ERR reason=out_of_limits"
        state.target_us = target
        self.last_feed_ms = now_ms
        return f"RP1 OK seq={fields['seq']} channel={fields['channel']} target_us={fields['target_us']}"

    def _cmd_start(self, fields, now_ms):
        if not _fields_ok(fields, ("channel", "pulse_us", "token"), 3):
            return "RP1 ERR reason=bad_fields"
        if self.estop:
            return "RP1 ERR reason=estop_latched"
        if not self.armed:
            return "RP1 ERR reason=disarmed"
        if fields["token"] != self.token:
            return "RP1 ERR reason=bad_token"
        channel = _uint(fields["channel"], CHANNELS - 1)
        if channel is None:
            return "RP1 ERR reason=bad_channel"
        pulse = _uint(fields["pulse_us"], ABS_MAX_US)
        if pulse is None:
            return "RP1 ERR reason=bad_start_values"
        state = self.channels[channel]
        if state.active:
            return "RP1 ERR reason=already_started"
        if not state.min_us <= pulse <= state.max_us:
            return "RP1 ERR reason=out_of_limits"
        state.active = True
        state.current_us = state.target_us = pulse
        state.slew_acc = 0
        state.last_ms = now_ms
        if not self._write_channel(channel):
            self._release_all("driver")
            return "RP1 ERR reason=driver_error"
        self.last_feed_ms = now_ms
        return f"RP1 OK channel={fields['channel']} started_us={fields['pulse_us']}"

    def _cmd_hb(self, fields, now_ms):
        if not _fields_ok(fields, ("token",), 1):
            return "RP1 ERR reason=bad_fields"
        if self.estop:
            return "RP1 ERR reason=estop_latched"
        if not self.armed:
            return "RP1 ERR reason=disarmed"
        if fields["token"] != self.token:
            return "RP1 ERR reason=bad_token"
        self.last_feed_ms = now_ms
        return "RP1 OK hb=1"

    def _cmd_status(self, fields, now_ms):
        if not _fields_ok(fields, ("channel",), 1):
            return "RP1 ERR reason=bad_fields"
        channel = _uint(fields["channel"], CHANNELS - 1)
        if channel is None:
            return "RP1 ERR reason=bad_channel"
        s = self.channels[channel]
        return (f"RP1 OK channel={channel} active={int(s.active)} current_us={s.current_us} target_us={s.target_us} "
                f"min_us={s.min_us} neutral_us={s.neutral_us} max_us={s.max_us} slew_us_s={s.slew_us_s} "
                f"armed={int(self.armed)}")

    # -- v0.1 reference firmware -------------------------------------------
    def _handle_legacy(self, line: str) -> str:
        line = line.strip()
        if line == "HELLO":
            return f"RP1 OK proto={PROTOCOL} mode=disarmed id={self.device_id}"
        if line.startswith("ARM "):
            if "cutoff=confirmed" in line and "token=" in line:
                self.armed = True
                return "RP1 OK armed=1"
            return "RP1 ERR reason=missing_arm_fields"
        if line == "DISARM":
            self.armed = False
            return "RP1 OK armed=0"
        if line == "STOP":
            self.armed = False
            return "RP1 OK stopped=1"
        if line.startswith("MOVE "):
            if not self.armed:
                return "RP1 ERR reason=disarmed"
            fields = dict(re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)=([^ ]+)", line))
            if not all(key in fields for key in ("seq", "channel", "target_us")):
                return "RP1 ERR reason=bad_move_fields"
            return f"RP1 OK seq={fields['seq']} channel={fields['channel']} target_us={fields['target_us']}"
        return "RP1 ERR reason=unknown_command"
