"""Linux GPIO adapter for the firmware's tested ROBOPROBE/1 state machine."""
from __future__ import annotations

import re
import threading
from collections import deque

from ..reference_device import (
    ABS_MAX_US, CAPS, CHANNELS, MAX_LINE, ChannelState, ReferenceDevice,
    _ParseError, _fields_ok, _parse, _uint,
)
from .backends import BackendError, GPIOBackend
from .boards import Board, get_profile, pin_map
from .i2c import SMBusScanner


class SBCAgent(ReferenceDevice):
    def __init__(self, backend: GPIOBackend, *, board=None, profile=None,
                 scanner=None, watchdog_ms=1000):
        self.backend = backend
        self.board = board or Board("emulated", "EMULATED")
        if profile is None:
            try:
                profile = get_profile(self.board.id)
            except KeyError:
                pass
        self.known_pins, self.pin_lines = pin_map(profile, backend)
        self.scanner = scanner or (backend if hasattr(backend, "scan") else SMBusScanner())
        self.lock = threading.RLock()
        self.bindings = {}  # firmware channel -> public catalog pin ID
        self.surveys = {}
        self.watching = False
        self.watch_pins = set()
        self.event_queue = deque(maxlen=4096)
        self.blink = None
        super().__init__(device_id=self.board.id, outputs=backend.name, watchdog_ms=watchdog_ms)

    def _driver_write(self, channel, pulse_us):
        pin = self.bindings.get(channel)
        if pin is None:
            return False
        try:
            self.backend.servo(self.pin_lines[pin], pulse_us)
        except Exception:
            return False
        self.outputs[channel] = pulse_us
        self.write_log.append((channel, pulse_us))
        return True

    def _driver_release_all(self):
        self.blink = None
        self.watching = False
        self.watch_pins.clear()
        self.event_queue.clear()
        self.outputs = {channel: None for channel in range(CHANNELS)}
        try:
            self.backend.release_all()
        except Exception:
            return False
        return True

    def tick(self, now_ms):
        with self.lock:
            super().tick(now_ms)
            try:
                self.backend.check()
                if self.blink and now_ms >= self.blink[2]:
                    pin, remaining, _, level = self.blink
                    if remaining == 0:
                        self._release_pin(pin)
                    else:
                        level = 1 - level
                        self.backend.digital(self.pin_lines[pin], level)
                        self.blink = (pin, remaining - 1, now_ms + 250, level)
                if self.watching:
                    inverse = {self.pin_lines[p]: p for p in self.watch_pins}
                    self.event_queue.extend((inverse[p], v) for p, v in self.backend.events() if p in inverse)
            except Exception:
                self._release_all("driver")

    def handle(self, line, now_ms):
        with self.lock:
            self.tick(now_ms)
            if len(line) > MAX_LINE:
                return "RP1 ERR reason=line_too_long"
            # WATCH is the sole command with a positional argument in the spec.
            if line.strip(" ") in ("WATCH on", "WATCH off"):
                line = "WATCH state=" + line.strip(" ").split(" ")[1]
            elif line.strip(" ").startswith("WATCH") and "state=" in line:
                return "RP1 ERR reason=bad_fields"
            try:
                # The reference grammar predates I2C_SCAN and disallows digits
                # in verbs. Keep its field parser and extend only this verb.
                i2c = line.strip(" ").split(" ", 1)[0] == "I2C_SCAN"
                parsed = "IIC_SCAN" + line.strip(" ")[8:] if i2c else line
                verb, fields = _parse(parsed)
                handler = self._cmd_i2c_scan if i2c else getattr(self, "_cmd_" + verb.lower(), None)
                return handler(fields, now_ms) if handler else "RP1 ERR reason=unknown_command"
            except _ParseError as error:
                return f"RP1 ERR reason={error}"
            except BackendError as error:
                return f"RP1 ERR reason={error}"
            except Exception:
                self._release_all("driver")
                return "RP1 ERR reason=driver_error"

    def external_estop(self):
        with self.lock:
            super().external_estop()

    def close(self):
        with self.lock:
            self._release_all(None)
            self.backend.close()

    def _cmd_hello(self, fields, now_ms):
        return super()._cmd_hello(fields, now_ms).replace(
            f"caps={CAPS}", f"caps={CAPS},survey,watch,i2c,probe,blink")

    def _pin(self, text):
        pin = _uint(text, 0xFFFFFFFF)
        if pin is None or pin not in self.known_pins:
            raise BackendError("bad_pin")
        if pin not in self.pin_lines:
            raise BackendError("pin_reserved")
        if self.backend.claimed(self.pin_lines[pin]):
            raise BackendError("pin_claimed")
        return pin

    def _auth(self, fields):
        if self.estop:
            return "RP1 ERR reason=estop_latched"
        if not self.armed:
            return "RP1 ERR reason=not_armed"
        if "token" not in fields:
            return "RP1 ERR reason=token_required"
        if fields["token"] != self.token:
            return "RP1 ERR reason=bad_token"
        return None

    def _busy(self, except_channel=None):
        return self.blink is not None or any(
            state.active for index, state in enumerate(self.channels) if index != except_channel)

    def _unwatch(self, pin):
        self.watch_pins.discard(pin)
        self.backend.watch(self.pin_lines[p] for p in self.watch_pins)

    def _survey(self, pin):
        self._unwatch(pin)
        line = self.pin_lines[pin]
        idle = [self.backend.sample(line, "disabled") for _ in range(3)]
        up = [self.backend.sample(line, "pull_up") for _ in range(2)]
        down = [self.backend.sample(line, "pull_down") for _ in range(2)]
        if all(v == 1 for v in up + down):
            result = "driven_high"
        elif all(v == 0 for v in up + down):
            result = "driven_low"
        elif up == [1, 1] and down == [0, 0]:
            result = "pulled_up" if idle == [1, 1, 1] else (
                "pulled_down" if idle == [0, 0, 0] else "floating")
        else:
            # Unstable or contradictory bias readings cannot authorize output.
            result = "reserved"
        self.surveys[pin] = result
        return result

    def _prepare(self, pin):
        result = self._survey(pin)  # fresh check, including ownership on each read
        if result.startswith("driven_"):
            raise BackendError("pin_driven")
        if result == "reserved":
            raise BackendError("pin_reserved")

    def _cmd_board(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        pins = ",".join(str(p) for p, line in sorted(self.pin_lines.items()) if not self.backend.claimed(line))
        model = re.sub(r"[^A-Za-z0-9_.-]", "_", self.board.model)
        return f"RP1 OK board={self.board.id} chip={model} pins={pins or 'none'}"

    def _cmd_survey(self, fields, now_ms):
        if not _fields_ok(fields, ("pin",), 1):
            return "RP1 ERR reason=bad_fields"
        try:
            pin = self._pin(fields["pin"])
        except BackendError as error:
            if str(error) == "pin_reserved":
                return f"RP1 OK pin={int(fields['pin'])} class=reserved adc=-"
            raise
        if self._busy():
            return "RP1 ERR reason=no_pwm_slot"
        was_watched = pin in self.watch_pins
        result = self._survey(pin)
        if was_watched and result in ("floating", "pulled_up", "pulled_down"):
            self.watch_pins.add(pin)
            self.backend.watch(self.pin_lines[p] for p in self.watch_pins)
        return f"RP1 OK pin={pin} class={result} adc=-"

    def _cmd_watch(self, fields, now_ms):
        if not _fields_ok(fields, ("state",), 1) or fields["state"] not in ("on", "off"):
            return "RP1 ERR reason=bad_fields"
        if fields["state"] == "off":
            self.backend.watch([])
            self.watch_pins.clear()
            self.watching = False
            return "RP1 OK watch=0"
        if self._busy():
            return "RP1 ERR reason=no_pwm_slot"
        self.backend.watch([])
        self.watch_pins.clear()
        try:
            for pin, line in self.pin_lines.items():
                if self.backend.claimed(line):
                    continue
                if self._survey(pin) in ("floating", "pulled_up", "pulled_down"):
                    self.watch_pins.add(pin)
            self.backend.watch(self.pin_lines[p] for p in self.watch_pins)
        except Exception:
            self.backend.watch([])
            self.watch_pins.clear()
            self.watching = False
            raise
        self.watching = True
        return "RP1 OK watch=1"

    def _cmd_events(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        # Drain in bounded chunks, retaining any remaining events for next call.
        events = [self.event_queue.popleft() for _ in range(min(64, len(self.event_queue)))]
        text = ",".join(f"{pin}:{level}" for pin, level in events) or "none"
        return f"RP1 OK events={text}"

    def _cmd_i2c_scan(self, fields, now_ms):
        if not _fields_ok(fields, ("bus",), 1):
            return "RP1 ERR reason=bad_fields"
        bus = _uint(fields["bus"], 0xFFFF)
        if bus is None:
            return "RP1 ERR reason=bad_bus"
        # A bus ioctl can block: never keep PWM running during a scan.
        if self._busy():
            return "RP1 ERR reason=no_pwm_slot"
        devices = ",".join(f"0x{a:02x}" for a in self.scanner.scan(bus)) or "none"
        return f"RP1 OK devices={devices}"

    def _cmd_start(self, fields, now_ms):
        # Preserve the reference core's validation order/replies. Only a valid
        # activation needs the SBC-specific binding and electrical checks.
        if (_fields_ok(fields, ("channel", "pulse_us", "token"), 3)
                and self.armed and not self.estop and fields["token"] == self.token):
            channel = _uint(fields["channel"], CHANNELS - 1)
            pulse = _uint(fields["pulse_us"], ABS_MAX_US)
            if channel is not None and pulse is not None:
                state = self.channels[channel]
                if not state.active and state.min_us <= pulse <= state.max_us:
                    if channel not in self.bindings or self._busy() or not self.backend.supports_pwm:
                        return "RP1 ERR reason=no_pwm_slot"
                    pin = self._pin(str(self.bindings[channel]))
                    self._prepare(pin)
        return super()._cmd_start(fields, now_ms)

    def _cmd_probe(self, fields, now_ms):
        if not _fields_ok(fields, ("pin", "mode", "start_us", "token"), 3):
            return "RP1 ERR reason=bad_fields"
        if error := self._auth(fields):
            return error
        pin = self._pin(fields["pin"])
        if fields["mode"] != "servo":
            return "RP1 ERR reason=bad_mode"
        if self._busy():
            return "RP1 ERR reason=no_pwm_slot"
        channel = next((c for c, p in self.bindings.items() if p == pin), None)
        if channel is None:
            channel = next((c for c in range(CHANNELS) if c not in self.bindings), None)
        if channel is None:
            # RELEASE makes slots reusable, but preserves bindings for START
            # until all slots have been used. Reassignment begins a fresh band.
            channel = next((c for c, s in enumerate(self.channels) if not s.active), None)
            if channel is None:
                return "RP1 ERR reason=no_pwm_slot"
            self.channels[channel] = ChannelState()
        previous = self.bindings.get(channel)
        self.bindings[channel] = pin
        try:
            result = self._cmd_start({"channel": str(channel), "pulse_us": fields["start_us"],
                                      "token": fields["token"]}, now_ms)
        except Exception:
            if previous is None:
                del self.bindings[channel]
            else:
                self.bindings[channel] = previous
            raise
        if result.startswith("RP1 ERR"):
            if previous is None:
                del self.bindings[channel]
            else:
                self.bindings[channel] = previous
            return result
        return f"RP1 OK pin={pin} channel={channel}"

    def _cmd_blink(self, fields, now_ms):
        if not _fields_ok(fields, ("pin", "count", "token"), 2):
            return "RP1 ERR reason=bad_fields"
        if error := self._auth(fields):
            return error
        pin = self._pin(fields["pin"])
        count = _uint(fields["count"], 10)
        if not count:
            return "RP1 ERR reason=bad_count"
        if self._busy():
            return "RP1 ERR reason=no_pwm_slot"
        self._prepare(pin)
        try:
            self.backend.digital(self.pin_lines[pin], 1)
        except Exception:
            self._release_all("driver")
            raise
        self.blink = (pin, count * 2 - 1, now_ms + 250, 1)
        self.last_feed_ms = now_ms
        return f"RP1 OK pin={pin} blinked={count}"

    def _release_pin(self, pin):
        self.backend.release(self.pin_lines[pin])
        if self.blink and self.blink[0] == pin:
            self.blink = None
        self.watch_pins.discard(pin)
        for channel, bound in self.bindings.items():
            if bound == pin:
                self.channels[channel] = ChannelState()
                self.outputs[channel] = None

    def _cmd_release(self, fields, now_ms):
        if not _fields_ok(fields, ("pin", "token"), 1):
            return "RP1 ERR reason=bad_fields"
        if error := self._auth(fields):
            return error
        pin = self._pin(fields["pin"])
        try:
            self._release_pin(pin)
        except Exception:
            self._release_all("driver")
            return "RP1 ERR reason=driver_error"
        return f"RP1 OK pin={pin} released=1"
