from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field

from .reference_device import ReferenceDevice


class ProtocolError(RuntimeError):
    pass


class TransportTimeout(ProtocolError):
    pass


class FirmwareOutdated(ProtocolError):
    """The controller speaks ROBOPROBE/1 but lacks a capability this host needs."""


OK_RE = re.compile(r"^RP1 OK(?: (?P<fields>.*))?$")
ERR_RE = re.compile(r"^RP1 ERR(?: (?P<fields>.*))?$")
FIELD_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)=([^ ]+)")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def parse_response(line: str) -> tuple[str, dict[str, str]]:
    stripped = line.strip()
    match = OK_RE.match(stripped)
    status = "OK"
    if match is None:
        match = ERR_RE.match(stripped)
        status = "ERR"
    if match is None:
        raise ProtocolError(f"malformed response: {stripped!r}")
    fields_text = match.group("fields") or ""
    fields: dict[str, str] = {}
    cursor = 0
    for field_match in FIELD_RE.finditer(fields_text):
        if field_match.start() != cursor and fields_text[cursor:field_match.start()].strip():
            raise ProtocolError(f"malformed field block: {fields_text!r}")
        fields[field_match.group(1)] = field_match.group(2)
        cursor = field_match.end()
        if cursor < len(fields_text) and fields_text[cursor] == " ":
            cursor += 1
    if fields_text[cursor:].strip():
        raise ProtocolError(f"malformed field block: {fields_text!r}")
    return status, fields


def validate_ack(command: str, line: str, expected: dict[str, str] | None = None) -> dict[str, str]:
    status, fields = parse_response(line)
    if status != "OK":
        raise ProtocolError(f"{command} returned error: {fields}")
    for key, value in (expected or {}).items():
        if fields.get(key) != value:
            raise ProtocolError(f"{command} acknowledgement mismatch for {key}: {fields.get(key)!r} != {value!r}")
    return fields


def _int_field(fields: dict[str, str], key: str) -> int:
    value = fields.get(key, "")
    if not value.isdigit():
        raise ProtocolError(f"expected integer field {key!r}, got {value!r}")
    return int(value)


@dataclass
class SerialProtocolTransport:
    """Host side of ROBOPROBE/1.

    Every command waits for exactly one reply line; a lock makes that safe
    when a heartbeat thread shares the port. Optional features (limits,
    status, heartbeat, session token, e-stop) are used only when the device
    advertises them in its HELLO ``caps`` field, so v0.1 firmware still works
    for plain probing.
    """

    port: str
    baudrate: int = 115200
    timeout_s: float = 1.0
    connection: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._serial = None
        self._seq = 0
        self._lock = threading.RLock()
        self._token: str | None = None
        self.caps: frozenset[str] = frozenset()
        self.watchdog_ms: int | None = None
        self.device_info: dict[str, str] = {}

    def open(self) -> None:
        if self.connection is not None:
            self._serial = self.connection
            return
        try:
            import serial
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyserial is required for serial transport") from exc
        self._serial = serial.Serial(
            self.port,
            self.baudrate,
            timeout=self.timeout_s,
            write_timeout=self.timeout_s,
            dsrdtr=True,
            rtscts=False,
        )

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def __enter__(self) -> "SerialProtocolTransport":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self):
        if self._serial is None:
            raise ProtocolError("transport is not open")
        return self._serial

    def transact(self, command: str, expected: dict[str, str] | None = None) -> dict[str, str]:
        verb = command.split(" ")[0]
        with self._lock:
            ser = self._ensure_open()
            payload = (command.strip() + "\n").encode("ascii")
            ser.write(payload)
            ser.flush()
            raw = ser.readline()
        if not raw:
            raise TransportTimeout(f"timeout waiting for {verb}")
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("non-ASCII response") from exc
        return validate_ack(verb, line, expected)

    def has_caps(self, *names: str) -> bool:
        return all(name in self.caps for name in names)

    def require_caps(self, *names: str) -> None:
        missing = [name for name in names if name not in self.caps]
        if missing:
            raise FirmwareOutdated(
                "controller firmware does not support " + ", ".join(missing)
                + " (HELLO caps); install the current robo-prober test firmware"
                " (python robo_prober.py firmware install)"
            )

    def hello(self) -> dict[str, str]:
        fields = self.transact("HELLO", {"proto": "ROBOPROBE/1"})
        self.device_info = dict(fields)
        self.caps = frozenset(filter(None, fields.get("caps", "").split(",")))
        watchdog = fields.get("watchdog_ms", "")
        self.watchdog_ms = int(watchdog) if watchdog.isdigit() and int(watchdog) > 0 else None
        return fields

    def arm(self, token: str, *, cutoff_confirmed: bool) -> dict[str, str]:
        if not token:
            raise ProtocolError("arm token is required")
        if not TOKEN_RE.match(token):
            raise ProtocolError("arm token must be 1-32 characters from A-Z a-z 0-9 _ . -")
        if not cutoff_confirmed:
            raise ProtocolError("physical power cutoff confirmation is required")
        fields = self.transact(f"ARM token={token} cutoff=confirmed", {"armed": "1"})
        self._token = token
        return fields

    def disarm(self) -> None:
        self._token = None
        self.transact("DISARM")

    def stop(self) -> None:
        self._token = None
        self.transact("STOP")

    def estop(self) -> None:
        self._token = None
        self.transact("ESTOP", {"estop": "1"})

    def estop_clear(self, *, cutoff_confirmed: bool) -> None:
        if not cutoff_confirmed:
            raise ProtocolError("physical power cutoff confirmation is required")
        self.transact("ESTOP_CLEAR cutoff=confirmed", {"estop": "0"})

    def limit(self, channel: int, min_us: int, neutral_us: int, max_us: int, slew_us_s: int) -> dict[str, str]:
        expected = {
            "channel": str(channel),
            "min_us": str(min_us),
            "neutral_us": str(neutral_us),
            "max_us": str(max_us),
            "slew_us_s": str(slew_us_s),
        }
        return self.transact(
            f"LIMIT channel={channel} min_us={min_us} neutral_us={neutral_us} max_us={max_us} slew_us_s={slew_us_s}",
            expected,
        )

    def start(self, channel: int, pulse_us: int) -> dict[str, str]:
        """First PWM on a channel, at an explicitly chosen pulse (v0.2 firmware)."""
        if self._token is None:
            raise ProtocolError("START requires an armed session")
        return self.transact(f"START channel={channel} pulse_us={pulse_us} token={self._token}",
                             {"channel": str(channel), "started_us": str(pulse_us)})

    # -- discovery (v0.3 firmware: caps survey, watch, i2c, probe, blink) -------
    def board(self) -> dict[str, str]:
        return self.transact("BOARD")

    def survey(self, pin: int) -> dict[str, str]:
        """Read-only: classify one pin (floating, pulled_*, driven_*, reserved)."""
        return self.transact(f"SURVEY pin={pin}", {"pin": str(pin)})

    def watch(self, on: bool) -> None:
        self.transact(f"WATCH state={'on' if on else 'off'}", {"watch": "1" if on else "0"})

    def events(self) -> list[tuple[int, int]]:
        """Level changes queued since the last call, as (pin, level)."""
        text = self.transact("EVENTS").get("events", "none")
        if text == "none":
            return []
        return [(int(pin), int(level)) for pin, level in (item.split(":") for item in text.split(","))]

    def i2c_scan(self, sda: int, scl: int) -> tuple[list[str], list[str]]:
        """Addresses that answer on this pin pair, and the ones that look like a PCA9685."""
        fields = self.transact(f"I2C_SCAN sda={sda} scl={scl}")
        split = lambda text: [] if text in (None, "none") else text.split(",")  # noqa: E731
        return split(fields.get("devices")), split(fields.get("pca9685"))

    def _session_token(self, what: str) -> str:
        if self._token is None:
            raise ProtocolError(f"{what} requires an armed session")
        return self._token

    def probe(self, pin: int, start_us: int) -> int:
        """Servo PWM on one GPIO at an explicit start pulse; returns its channel number."""
        token = self._session_token("PROBE")
        fields = self.transact(f"PROBE pin={pin} mode=servo start_us={start_us} token={token}", {"pin": str(pin)})
        return _int_field(fields, "channel")

    def blink(self, pin: int, count: int) -> None:
        token = self._session_token("BLINK")
        self.transact(f"BLINK pin={pin} count={count} token={token}", {"pin": str(pin)})

    def release(self, pin: int) -> None:
        token = self._session_token("RELEASE")
        self.transact(f"RELEASE pin={pin} token={token}", {"pin": str(pin), "released": "1"})

    def heartbeat(self) -> None:
        token = self._token
        if token is None:
            raise ProtocolError("heartbeat requires an armed session")
        self.transact(f"HB token={token}", {"hb": "1"})

    def status(self, channel: int) -> dict[str, int]:
        fields = self.transact(f"STATUS channel={channel}", {"channel": str(channel)})
        return {key: _int_field(fields, key) for key in (
            "active", "current_us", "target_us", "min_us", "neutral_us", "max_us", "slew_us_s", "armed")}

    def move_us(self, channel: int, target_us: int) -> dict[str, str]:
        self._seq += 1
        seq = str(self._seq)
        command = f"MOVE channel={channel} target_us={target_us} seq={seq}"
        if self._token is not None and "token" in self.caps:
            command += f" token={self._token}"
        return self.transact(command, {"seq": seq, "channel": str(channel), "target_us": str(target_us)})

    def wait_settled(self, channel: int, target_us: int, *, timeout_s: float, poll_s: float = 0.02) -> dict[str, int]:
        """Poll STATUS until the slew-limited output reaches ``target_us``."""
        deadline = time.monotonic() + timeout_s
        while True:
            state = self.status(channel)
            if not state["armed"] or not state["active"]:
                raise ProtocolError(f"channel {channel} was released while moving (watchdog, stop or fault)")
            if state["target_us"] != target_us:
                raise ProtocolError(f"channel {channel} target mismatch: {state['target_us']} != {target_us}")
            if state["current_us"] == target_us:
                return state
            if time.monotonic() >= deadline:
                raise ProtocolError(f"channel {channel} did not settle at {target_us} us within {timeout_s:.1f} s")
            time.sleep(poll_s)


def settle_timeout_s(distance_us: int, slew_us_s: int, margin_s: float = 1.0) -> float:
    return abs(distance_us) / max(slew_us_s, 1) + margin_s


class Heartbeat:
    """Background HB sender that keeps the firmware watchdog fed.

    If the host process hangs or dies, heartbeats stop and the firmware
    releases every output. A failed heartbeat is recorded and re-raised by
    ``check()`` so the main flow stops instead of carrying on blind.
    """

    def __init__(self, transport: SerialProtocolTransport, interval_s: float):
        self.transport = transport
        self.interval_s = interval_s
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @classmethod
    def for_transport(cls, transport: SerialProtocolTransport) -> "Heartbeat | None":
        if transport.watchdog_ms is None or not transport.has_caps("hb", "token"):
            return None
        return cls(transport, max(transport.watchdog_ms / 4000.0, 0.01))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.transport.heartbeat()
            except BaseException as error:  # noqa: BLE001 - surfaced through check()
                if not self._stop.is_set():
                    self.error = error
                return

    def start(self) -> "Heartbeat":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.interval_s * 2 + self.transport.timeout_s + 1.0)

    def check(self) -> None:
        if self.error is not None:
            raise ProtocolError(f"heartbeat failed: {self.error}")


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class InProcessEmulatorConnection:
    """A serial-like object backed by a ReferenceDevice in this process.

    Works on every OS (no pseudo terminal needed). It implements the part of
    the pyserial API the transport uses: write, flush, readline and close.
    """

    def __init__(self, device: ReferenceDevice | None = None, *, clock=_now_ms):
        self.device = device or ReferenceDevice()
        self._clock = clock
        self.device.begin(self._clock())
        self._replies: list[bytes] = []
        self._lock = threading.Lock()
        self.closed = False

    def write(self, data: bytes) -> int:
        with self._lock:
            for raw in data.splitlines():
                line = raw.decode("ascii", errors="replace")
                self._replies.append((self.device.handle(line, self._clock()) + "\n").encode("ascii"))
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        with self._lock:
            return self._replies.pop(0) if self._replies else b""

    def close(self) -> None:
        self.closed = True


class PtyReferenceEmulator:
    """ROBOPROBE/1 emulator behind a real pseudo terminal (Linux and macOS).

    The device logic is ReferenceDevice, so slew, limits, the watchdog and the
    e-stop behave like the v0.2 firmware. On Windows, use
    InProcessEmulatorConnection instead.
    """

    def __init__(self, *, malformed_after: int | None = None, delay_s: float = 0.0,
                 device: ReferenceDevice | None = None, legacy: bool = False):
        self.master_fd: int | None = None
        self._slave_fd: int | None = None
        self.slave_path: str | None = None
        self.device = device or ReferenceDevice(legacy=legacy)
        self.malformed_after = malformed_after
        self.delay_s = delay_s
        self.command_count = 0
        self._pending = b""

    def open(self) -> str:
        import pty
        import tty

        self.master_fd, self._slave_fd = pty.openpty()
        # Raw mode: no echo or newline translation (macOS and Linux defaults differ).
        tty.setraw(self._slave_fd)
        self.slave_path = os.ttyname(self._slave_fd)
        self.device.begin(_now_ms())
        return self.slave_path

    def close(self) -> None:
        if self.master_fd is not None:
            os.close(self.master_fd)
            self.master_fd = None
        if self._slave_fd is not None:
            os.close(self._slave_fd)
            self._slave_fd = None

    def serve_once(self, timeout_s: float = 2.0) -> bool:
        import select

        fd = self.master_fd
        if fd is None:
            raise RuntimeError("emulator is not open")
        ready, _, _ = select.select([fd], [], [], timeout_s)
        self.device.tick(_now_ms())
        if not ready:
            return False
        data = os.read(fd, 4096)
        if not data:
            return False
        self._pending += data
        while b"\n" in self._pending:
            raw_line, self._pending = self._pending.split(b"\n", 1)
            line = raw_line.decode("ascii", errors="replace").strip("\r")
            response = self._handle(line)
            if self.delay_s:
                time.sleep(self.delay_s)
            os.write(fd, (response + "\n").encode("ascii"))
        return True

    def _handle(self, line: str) -> str:
        self.command_count += 1
        if self.malformed_after is not None and self.command_count > self.malformed_after:
            return "NOT_A_VALID_ACK"
        return self.device.handle(line, _now_ms())


def serve_forever(emulator: PtyReferenceEmulator, poll_s: float = 0.01) -> None:
    """Serve an emulator until it is closed (run this in a thread)."""
    while emulator.master_fd is not None:
        try:
            emulator.serve_once(timeout_s=poll_s)
        except (OSError, TypeError, ValueError):
            return


def pty_available() -> bool:
    if os.name != "posix":
        return False
    try:
        import pty  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        return False
    return True
