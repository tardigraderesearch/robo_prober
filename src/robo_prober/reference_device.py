"""Python model of the ROBOPROBE/1 v0.2 firmware core.

This mirrors ``firmware/esp32_robo_prober/src/rp_core.cpp`` command for
command, so the host can be tested end to end with no hardware: band limits,
slew limiting, the session token, the heartbeat watchdog and the latching
e-stop all behave like the firmware. A conformance test runs the same command
script through this model and through the compiled C++ core and requires
identical replies.

The pins behind the firmware are an ``EmulatedBoard``: each GPIO is open or a
source (a voltage behind a resistance), with the same arithmetic as the C++
simulator's ``fake_board.h``, and an emulated I2C bus. Tests can wire other
hidden truth onto it (servos, LEDs, buttons, other chips' outputs) and check
what the discovery cascade finds.

``legacy=True`` emulates the v0.1 reference firmware (no capabilities, no
token, no limits) so the host's compatibility paths can be tested too.

Time is passed in explicitly as milliseconds; nothing here sleeps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PROTOCOL = "ROBOPROBE/1"
FIRMWARE_VERSION = "0.3.0"
CHANNELS = 16          # PCA9685 outputs: channel 0..15
GPIO_BASE = 100        # a probed GPIO N is channel 100+N
GPIO_COUNT = 49
SLOTS = CHANNELS + GPIO_COUNT
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
CAPS = "limit,status,hb,token,estop,start,survey,watch,i2c,probe,blink"
BLINK_HALF_MS = 250
WATCH_IDLE_MS = 5000
PULL_PROOF_MV = 150
MAX_EVENTS = 32
MAX_PAIRS = 64
PIN_USABLE, PIN_LAST, PIN_INPUT_ONLY, PIN_ADC = 1, 2, 4, 8
PULL_OHMS = 45000
RAIL_MV = 3300
MAX_PWM = 8

# Default wiring (same as firmware/.../test/native/fake_board.h): gpio -> (mV, ohms).
DEFAULT_SOURCES = {5: (0, 40000), 6: (RAIL_MV, 10000), 7: (0, 30), 8: (RAIL_MV, 4700), 9: (RAIL_MV, 4700)}
# What is really there, for tests (the firmware never sees this).
DEFAULT_PARTS = {4: "servo", 5: "led", 6: "button", 7: "driven", 8: "i2c", 9: "i2c"}
DEFAULT_I2C = {"sda": 8, "scl": 9, "devices": {0x40: "pca9685", 0x70: None}}
PCA_REGS = {0x01: 0x04, 0x05: 0xE0, 0xFE: 0x1E}

_VERB_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_KEY_RE = re.compile(r"^[a-z_]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_UINT_RE = re.compile(r"^[0-9]{1,10}$")


class EmulatedBoard:
    """The pins of an emulated ESP32 and its I2C bus.

    ``sources`` maps a GPIO to (millivolts, ohms): what drives it from outside
    (a pull-up resistor, an LED to GND, another chip's output). Anything not
    listed is open (a servo signal input, nothing). ``parts`` says what is
    really attached, for tests to judge "did it move / light up"; the firmware
    model never reads it.
    """

    def __init__(self, *, chip: str = "esp32s3", sources: dict[int, tuple[int, int]] | None = None,
                 parts: dict[int, str] | None = None, i2c: dict | None = None, in_use: set[int] | None = None):
        from .catalog import BOARD_PROFILES, FIRMWARE_CHIPS
        from .firmware_pins import chip_flags

        self.profile_id = FIRMWARE_CHIPS[chip]
        self.model = BOARD_PROFILES[self.profile_id]["platform"]
        self.flags = chip_flags(chip)
        self.sources = dict(DEFAULT_SOURCES if sources is None else sources)
        self.parts = dict(DEFAULT_PARTS if parts is None else parts)
        self.i2c = DEFAULT_I2C if i2c is None else i2c
        self.in_use = set(in_use or ())
        self.pull: dict[int, str] = {}
        self.bus: tuple[int, int] | None = None
        self.pca_writes = 0

    # -- wiring (tests) ---------------------------------------------------
    def set_source(self, gpio: int, mv: int, ohms: int) -> None:
        self.sources[gpio] = (mv, ohms)

    def open_pin(self, gpio: int) -> None:
        self.sources.pop(gpio, None)

    # -- electrical model -------------------------------------------------
    def voltage(self, gpio: int, pull: str) -> int:
        rail = RAIL_MV if pull == "up" else 0
        source = self.sources.get(gpio)
        if source is None:
            return 0 if pull == "none" else rail
        mv, ohms = source
        if pull == "none" or ohms == 0:
            return mv
        return (mv * PULL_OHMS + rail * ohms) // (ohms + PULL_OHMS)

    # -- Board interface (rp_core.h) ---------------------------------------
    def pin_flags(self, gpio: int) -> int:
        return self.flags[gpio]

    def pin_in_use(self, gpio: int) -> bool:
        return gpio in self.in_use

    def set_input(self, gpio: int, pull: str) -> None:
        self.pull[gpio] = pull

    def level(self, gpio: int) -> int:
        return 1 if self.voltage(gpio, self.pull.get(gpio, "none")) >= RAIL_MV // 2 else 0

    def read_mv(self, gpio: int, pull: str) -> int:
        self.pull[gpio] = pull
        if not self.flags[gpio] & PIN_ADC:
            return -1
        return self.voltage(gpio, pull)

    # -- I2cBus interface ---------------------------------------------------
    def _on_bus(self) -> bool:
        return self.bus == (self.i2c.get("sda"), self.i2c.get("scl"))

    def i2c_begin(self, sda: int, scl: int) -> bool:
        self.bus = (sda, scl)
        return True

    def i2c_probe(self, address: int) -> int:
        return 0 if self._on_bus() and address in self.i2c.get("devices", {}) else 1

    def i2c_read(self, address: int, reg: int) -> int | None:
        if not self._on_bus() or self.i2c.get("devices", {}).get(address) != "pca9685":
            return None
        return PCA_REGS.get(reg, 0)

    def i2c_write(self, address: int, reg: int, value: int) -> bool:
        if not self._on_bus() or self.i2c.get("devices", {}).get(address) != "pca9685":
            return False
        self.pca_writes += 1
        return True

    def i2c_end(self) -> None:
        self.bus = None


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
    blinking: bool = False
    blink_left: int = 0
    blink_high: bool = False
    blink_next_ms: int = 0


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


def _slot(text: str | None) -> tuple[int, int] | None:
    """Channel 0..15 -> slot 0..15 (PCA9685); 100+N -> slot 16+N (GPIO N)."""
    channel = _uint(text, GPIO_BASE + GPIO_COUNT - 1)
    if channel is None:
        return None
    if channel < CHANNELS:
        return channel, channel
    if channel >= GPIO_BASE:
        return channel, CHANNELS + channel - GPIO_BASE
    return None


def _append(items: str, item: str, limit: int) -> str | None:
    """Comma list that starts as "none"; None when the item would not fit (C buffer size)."""
    used = "" if items == "none" else items
    text = used + ("," if used else "") + item
    return text if len(text) + 1 <= limit else None


CLASS_NAMES = ("unknown", "floating", "pulled_up", "pulled_down", "driven_high", "driven_low", "reserved")


class ReferenceDevice:
    """Firmware state machine plus a record of what the outputs are doing."""

    def __init__(self, *, device_id: str = "reference-emulator", outputs: str = "emulated",
                 watchdog_ms: int = DEFAULT_WATCHDOG_MS, legacy: bool = False, board: EmulatedBoard | None = None):
        self.device_id = device_id
        self.outputs_name = outputs
        self.watchdog_ms = watchdog_ms
        self.legacy = legacy
        self.board = board or EmulatedBoard()
        self.driver_available = True
        self.driver_fails = False
        self.outputs: dict[int, int | str | None] = {}
        self.pwm: set[int] = set()
        self.write_log: list[tuple[int, int]] = []  # every (channel, pulse_us) written
        self.level_log: list[tuple[int, bool]] = []  # every (channel, high) BLINK wrote
        self.i2c_sda = self.i2c_scl = -1
        self.pca_address = 0
        self.i2c_text = self.pca_text = "none"
        self.begin(0)
        self.find_i2c()

    # -- output driver ----------------------------------------------------
    @staticmethod
    def _channel_of(slot: int) -> int:
        return slot if slot < CHANNELS else GPIO_BASE + slot - CHANNELS

    def _start_problem(self, slot: int) -> str | None:
        if slot < CHANNELS or slot in self.pwm:
            return None
        return "no_pwm_slot" if len(self.pwm) >= MAX_PWM else None

    def _driver_write(self, slot: int, pulse_us: int) -> bool:
        if self.driver_fails:
            return False
        channel = self._channel_of(slot)
        self.outputs[channel] = pulse_us
        if slot >= CHANNELS:
            self.pwm.add(slot)
        self.write_log.append((channel, pulse_us))
        return True

    def _driver_level(self, slot: int, high: bool) -> bool:
        if self.driver_fails:
            return False
        channel = self._channel_of(slot)
        self.outputs[channel] = "high" if high else "low"
        self.level_log.append((channel, high))
        return True

    def _driver_release(self, slot: int) -> bool:
        self.outputs[self._channel_of(slot)] = None
        self.pwm.discard(slot)
        return not self.driver_fails

    def _driver_release_all(self) -> bool:
        self.outputs = {self._channel_of(slot): None for slot in range(SLOTS)}
        self.pwm.clear()
        return not self.driver_fails

    def output(self, channel: int) -> int | str | None:
        """What a channel outputs: a pulse in us, "high"/"low" while blinking, or None when off."""
        return self.outputs.get(channel)

    # -- core -------------------------------------------------------------
    def begin(self, now_ms: int) -> None:
        self.channels = [ChannelState() for _ in range(SLOTS)]
        self.armed = False
        self.estop = False
        self.fault = "none"
        self.token = ""
        self.last_feed_ms = now_ms
        self._driver_release_all()
        self.watching = False
        self.watched: set[int] = set()
        self.watch_level: dict[int, int] = {}
        self.watch_poll_ms = 0
        self.events: list[tuple[int, int]] = []
        self.classes = ["unknown"] * GPIO_COUNT
        for last in (False, True):
            for gpio in range(GPIO_COUNT):
                if bool(self.board.pin_flags(gpio) & PIN_LAST) == last:
                    self._survey_pin(gpio)

    def _bus_pin(self, gpio: int) -> bool:
        return gpio in (self.i2c_sda, self.i2c_scl)

    def _survey_pin(self, gpio: int) -> tuple[str, int]:
        board = self.board
        flags = board.pin_flags(gpio)
        mv = -1
        if not flags & PIN_USABLE or board.pin_in_use(gpio) or self._bus_pin(gpio):
            cls = "reserved"
        else:
            board.set_input(gpio, "up")
            up = board.level(gpio)
            board.set_input(gpio, "down")
            down = board.level(gpio)
            if up and not down:
                cls = "floating"
            elif up == down:
                high = bool(up)
                plain = board.read_mv(gpio, "none")
                against = board.read_mv(gpio, "down" if high else "up")
                shift = plain - against if high else against - plain
                resistive = plain >= 0 and against >= 0 and shift >= PULL_PROOF_MV
                cls = ("pulled_up" if resistive else "driven_high") if high else \
                    ("pulled_down" if resistive else "driven_low")
            else:
                cls = "driven_high"
            board.set_input(gpio, "none")
            if flags & PIN_ADC:
                mv = board.read_mv(gpio, "none")
        self.classes[gpio] = cls
        return cls, mv

    def _scan_pair(self, sda: int, scl: int) -> tuple[str, str, int]:
        board = self.board
        devices = pcas = "none"
        first = 0
        board.i2c_end()
        board.i2c_begin(sda, scl)
        acked = []
        for address in range(0x08, 0x78):
            result = board.i2c_probe(address)
            if result == 2:
                break
            if result == 0:
                acked.append(address)
        for address in acked:
            item = f"0x{address:02x}"
            devices = _append(devices, item, 96) or devices
            if address < 0x40 or address == 0x70:
                continue
            mode2, allcall, prescale = (board.i2c_read(address, reg) for reg in (0x01, 0x05, 0xFE))
            if None not in (mode2, allcall, prescale) and not mode2 & 0xE0 and not allcall & 0x01 and prescale >= 3:
                board.i2c_write(address, 0xFD, 0x10)
                pcas = _append(pcas, item, 48) or pcas
                first = first or address
        board.i2c_end()
        if self.pca_address:
            board.i2c_begin(self.i2c_sda, self.i2c_scl)
        return devices, pcas, first

    def find_i2c(self, sda: int = -1, scl: int = -1, fixed: bool = False) -> None:
        self.i2c_sda = self.i2c_scl = -1
        self.pca_address = 0
        self.i2c_text = self.pca_text = "none"
        if fixed:
            pairs = [(sda, scl)]
        else:
            high = [gpio for last in (False, True) for gpio in range(GPIO_COUNT)
                    if bool(self.board.pin_flags(gpio) & PIN_LAST) == last
                    and not self.board.pin_flags(gpio) & PIN_INPUT_ONLY
                    and self.classes[gpio] in ("pulled_up", "driven_high")]
            pairs = [(a, b) for a in high for b in high if a != b][:MAX_PAIRS]
        for a, b in pairs:
            _, pcas, first = self._scan_pair(a, b)
            if first:
                self.i2c_sda, self.i2c_scl, self.pca_address = a, b, first
                self.i2c_text, self.pca_text = f"{a},{b}", pcas
                self.classes[a] = self.classes[b] = "reserved"
                self.board.i2c_begin(a, b)
                break

    def _reset_channel(self, state: ChannelState) -> None:
        state.__init__()

    def _release_all(self, reason: str | None) -> None:
        ok = self._driver_release_all()
        self.armed = False
        self.token = ""
        for state in self.channels:
            # A band lives for one armed session (matches the firmware).
            self._reset_channel(state)
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

    def _watch_off(self) -> None:
        if not self.watching:
            return
        for gpio in self.watched:
            if not self.channels[CHANNELS + gpio].active:
                self.board.set_input(gpio, "none")
        self.watched = set()
        self.watching = False
        self.events = []

    def _drivable(self, gpio: int) -> str | None:
        flags = self.board.pin_flags(gpio)
        if not flags & PIN_USABLE or flags & PIN_INPUT_ONLY:
            return "pin_reserved"
        if self.board.pin_in_use(gpio) or self._bus_pin(gpio) or self.channels[CHANNELS + gpio].active:
            return "pin_claimed"
        self.watched.discard(gpio)
        cls, _ = self._survey_pin(gpio)
        if cls.startswith("driven"):
            return "pin_driven"
        if cls == "reserved":
            return "pin_reserved"
        return None

    def tick(self, now_ms: int) -> None:
        if self.armed and now_ms - self.last_feed_ms > self.watchdog_ms:
            self._release_all("watchdog")
            return
        for index, state in enumerate(self.channels):
            if not state.active:
                continue
            if state.blinking:
                if now_ms - state.blink_next_ms < 0:
                    continue
                if state.blink_left == 0:
                    ok = self._driver_release(index)
                    self._reset_channel(state)
                else:
                    state.blink_high = not state.blink_high
                    state.blink_left -= 1
                    state.blink_next_ms += BLINK_HALF_MS
                    ok = self._driver_level(index, state.blink_high)
                if not ok:
                    self._release_all("driver")
                    return
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
        if self.watching:
            if now_ms - self.watch_poll_ms > WATCH_IDLE_MS:
                self._watch_off()
                return
            for gpio in sorted(self.watched):
                level = self.board.level(gpio)
                if level == self.watch_level[gpio]:
                    continue
                self.watch_level[gpio] = level
                if len(self.events) < MAX_EVENTS:
                    self.events.append((gpio, level))

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
                f"outputs={self.outputs_name} i2c={self.i2c_text} pca9685={self.pca_text} "
                f"watchdog_ms={self.watchdog_ms} caps={CAPS} fault={self.fault}")

    def _cmd_board(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        pins = [str(g) for g in range(GPIO_COUNT) if self.board.pin_flags(g) & PIN_USABLE
                and not self.board.pin_in_use(g) and not self._bus_pin(g)]
        return (f"RP1 OK board={self.board.profile_id} chip={self.board.model} "
                f"pins={','.join(pins) or 'none'}")

    def _cmd_survey(self, fields, now_ms):
        if not _fields_ok(fields, ("pin",), 1):
            return "RP1 ERR reason=bad_fields"
        gpio = _uint(fields["pin"], GPIO_COUNT - 1)
        if gpio is None:
            return "RP1 ERR reason=bad_pin"
        if self.channels[CHANNELS + gpio].active:
            return "RP1 ERR reason=pin_claimed"
        cls, mv = self._survey_pin(gpio)
        if gpio in self.watched:
            self.board.set_input(gpio, "up" if cls == "floating" else "none")
            self.watch_level[gpio] = self.board.level(gpio)
        return f"RP1 OK pin={gpio} class={cls} adc={mv if mv >= 0 else '-'}"

    def _cmd_watch(self, fields, now_ms):
        if not _fields_ok(fields, ("state",), 1):
            return "RP1 ERR reason=bad_fields"
        if fields["state"] == "off":
            self._watch_off()
            return "RP1 OK watch=0"
        if fields["state"] != "on":
            return "RP1 ERR reason=bad_fields"
        if not self.watching:
            self.watched = set()
            for gpio in range(GPIO_COUNT):
                cls = self.classes[gpio]
                if cls in ("reserved", "unknown") or self.channels[CHANNELS + gpio].active:
                    continue
                self.watched.add(gpio)
                self.board.set_input(gpio, "up" if cls == "floating" else "none")
                self.watch_level[gpio] = self.board.level(gpio)
            self.events = []
            self.watching = True
        self.watch_poll_ms = now_ms
        return "RP1 OK watch=1"

    def _cmd_events(self, fields, now_ms):
        if fields:
            return "RP1 ERR reason=bad_fields"
        self.watch_poll_ms = now_ms
        text, taken = "none", 0
        for gpio, level in self.events:
            longer = _append(text, f"{gpio}:{level}", 200)
            if longer is None:
                break
            text, taken = longer, taken + 1
        del self.events[:taken]
        return f"RP1 OK events={text}"

    def _cmd_i2c_scan(self, fields, now_ms):
        if not _fields_ok(fields, ("sda", "scl"), 2):
            return "RP1 ERR reason=bad_fields"
        sda, scl = _uint(fields["sda"], GPIO_COUNT - 1), _uint(fields["scl"], GPIO_COUNT - 1)
        if sda is None or scl is None or sda == scl:
            return "RP1 ERR reason=bad_pin"
        driver_bus = {sda, scl} == {self.i2c_sda, self.i2c_scl}
        for pin in (sda, scl):
            flags = self.board.pin_flags(pin)
            if not flags & PIN_USABLE or flags & PIN_INPUT_ONLY:
                return "RP1 ERR reason=pin_reserved"
            if self.board.pin_in_use(pin) or self.channels[CHANNELS + pin].active or \
                    (self._bus_pin(pin) and not driver_bus):
                return "RP1 ERR reason=pin_claimed"
        devices, pcas, _ = self._scan_pair(sda, scl)
        for pin in (sda, scl):
            if pin in self.watched:
                self.board.set_input(pin, "up" if self.classes[pin] == "floating" else "none")
        return f"RP1 OK devices={devices} pca9685={pcas}"

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
        parsed = _slot(fields["channel"])
        if parsed is None:
            return "RP1 ERR reason=bad_channel"
        channel, slot = parsed
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
        state = self.channels[slot]
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
        parsed = _slot(fields["channel"])
        if parsed is None:
            return "RP1 ERR reason=bad_channel"
        _, slot = parsed
        target = _uint(fields["target_us"], ABS_MAX_US)
        seq = _uint(fields["seq"], 0xFFFFFFFF)
        if target is None or seq is None:
            return "RP1 ERR reason=bad_move_values"
        state = self.channels[slot]
        if not state.active:
            return "RP1 ERR reason=not_started"
        if state.blinking:
            return "RP1 ERR reason=pin_claimed"
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
        parsed = _slot(fields["channel"])
        if parsed is None or parsed[1] >= CHANNELS:
            return "RP1 ERR reason=bad_channel"
        _, slot = parsed
        pulse = _uint(fields["pulse_us"], ABS_MAX_US)
        if pulse is None:
            return "RP1 ERR reason=bad_start_values"
        state = self.channels[slot]
        if state.active:
            return "RP1 ERR reason=already_started"
        if not state.min_us <= pulse <= state.max_us:
            return "RP1 ERR reason=out_of_limits"
        why = self._start_problem(slot)
        if why:
            return f"RP1 ERR reason={why}"
        state.active = True
        state.current_us = state.target_us = pulse
        state.slew_acc = 0
        state.last_ms = now_ms
        if not self._write_channel(slot):
            self._release_all("driver")
            return "RP1 ERR reason=driver_error"
        self.last_feed_ms = now_ms
        return f"RP1 OK channel={fields['channel']} started_us={fields['pulse_us']}"

    def _probe_or_blink(self, fields, now_ms, probe: bool):
        keys = ("pin", "mode", "start_us", "token") if probe else ("pin", "count", "token")
        if not _fields_ok(fields, keys, len(keys)):
            return "RP1 ERR reason=bad_fields"
        if self.estop:
            return "RP1 ERR reason=estop_latched"
        if not self.armed:
            return "RP1 ERR reason=not_armed"
        if fields["token"] != self.token:
            return "RP1 ERR reason=bad_token"
        gpio = _uint(fields["pin"], GPIO_COUNT - 1)
        if gpio is None:
            return "RP1 ERR reason=bad_pin"
        slot = CHANNELS + gpio
        state = self.channels[slot]
        if probe:
            if fields["mode"] != "servo":
                return "RP1 ERR reason=bad_fields"
            value = _uint(fields["start_us"], ABS_MAX_US)
            if value is None:
                return "RP1 ERR reason=bad_start_values"
            if not state.active and not state.min_us <= value <= state.max_us:
                return "RP1 ERR reason=out_of_limits"
        else:
            value = _uint(fields["count"], 10)
            if not value:
                return "RP1 ERR reason=bad_fields"
        why = self._drivable(gpio)
        if not why and probe:
            why = self._start_problem(slot)
        if why:
            return f"RP1 ERR reason={why}"
        state.active = True
        if probe:
            state.current_us = state.target_us = value
            state.slew_acc = 0
            state.last_ms = now_ms
            ok = self._write_channel(slot)
        else:
            state.blinking = True
            state.blink_high = True
            state.blink_left = value * 2 - 1
            state.blink_next_ms = now_ms + BLINK_HALF_MS
            ok = self._driver_level(slot, True)
        if not ok:
            self._release_all("driver")
            return "RP1 ERR reason=driver_error"
        self.last_feed_ms = now_ms
        return f"RP1 OK pin={gpio} channel={GPIO_BASE + gpio}" if probe else f"RP1 OK pin={gpio} blinked={value}"

    def _cmd_probe(self, fields, now_ms):
        return self._probe_or_blink(fields, now_ms, True)

    def _cmd_blink(self, fields, now_ms):
        return self._probe_or_blink(fields, now_ms, False)

    def _cmd_release(self, fields, now_ms):
        if not _fields_ok(fields, ("pin", "token"), 2):
            return "RP1 ERR reason=bad_fields"
        if not self.armed:
            return "RP1 ERR reason=not_armed"
        if fields["token"] != self.token:
            return "RP1 ERR reason=bad_token"
        gpio = _uint(fields["pin"], GPIO_COUNT - 1)
        if gpio is None:
            return "RP1 ERR reason=bad_pin"
        state = self.channels[CHANNELS + gpio]
        if state.active:
            ok = self._driver_release(CHANNELS + gpio)
            self._reset_channel(state)
            if not ok:
                self._release_all("driver")
                return "RP1 ERR reason=driver_error"
        self.last_feed_ms = now_ms
        return f"RP1 OK pin={gpio} released=1"

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
        parsed = _slot(fields["channel"])
        if parsed is None:
            return "RP1 ERR reason=bad_channel"
        channel, slot = parsed
        s = self.channels[slot]
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
