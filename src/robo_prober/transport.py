from __future__ import annotations

import os
import re
import select
import time
from dataclasses import dataclass


class ProtocolError(RuntimeError):
    pass


class TransportTimeout(ProtocolError):
    pass


OK_RE = re.compile(r"^RP1 OK(?: (?P<fields>.*))?$")
ERR_RE = re.compile(r"^RP1 ERR(?: (?P<fields>.*))?$")
FIELD_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)=([^ ]+)")


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
    for field in FIELD_RE.finditer(fields_text):
        if field.start() != cursor and fields_text[cursor:field.start()].strip():
            raise ProtocolError(f"malformed field block: {fields_text!r}")
        fields[field.group(1)] = field.group(2)
        cursor = field.end()
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


@dataclass
class SerialProtocolTransport:
    port: str
    baudrate: int = 115200
    timeout_s: float = 1.0

    def __post_init__(self) -> None:
        self._serial = None
        self._seq = 0

    def open(self) -> None:
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
        ser = self._ensure_open()
        payload = (command.strip() + "\n").encode("ascii")
        ser.write(payload)
        ser.flush()
        raw = ser.readline()
        if not raw:
            raise TransportTimeout(f"timeout waiting for {command}")
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("non-ASCII response") from exc
        return validate_ack(command, line, expected)

    def hello(self) -> dict[str, str]:
        return self.transact("HELLO", {"proto": "ROBOPROBE/1"})

    def arm(self, token: str, *, cutoff_confirmed: bool) -> dict[str, str]:
        if not token:
            raise ProtocolError("arm token is required")
        if not cutoff_confirmed:
            raise ProtocolError("physical power cutoff confirmation is required")
        return self.transact(f"ARM token={token} cutoff=confirmed", {"armed": "1"})

    def disarm(self) -> None:
        self.transact("DISARM")

    def stop(self) -> None:
        self.transact("STOP")

    def move_us(self, channel: int, target_us: int) -> dict[str, str]:
        self._seq += 1
        seq = str(self._seq)
        return self.transact(
            f"MOVE channel={channel} target_us={target_us} seq={seq}",
            {"seq": seq, "channel": str(channel), "target_us": str(target_us)},
        )


class PtyReferenceEmulator:
    """Small ROBOPROBE/1 emulator backed by a real pseudo terminal."""

    def __init__(self, *, malformed_after: int | None = None, delay_s: float = 0.0):
        self.master_fd: int | None = None
        self._slave_fd: int | None = None
        self.slave_path: str | None = None
        self._armed = False
        self._stop = False
        self.malformed_after = malformed_after
        self.delay_s = delay_s
        self.command_count = 0

    def open(self) -> str:
        import pty

        self.master_fd, self._slave_fd = pty.openpty()
        self.slave_path = os.ttyname(self._slave_fd)
        return self.slave_path

    def close(self) -> None:
        self._stop = True
        if self.master_fd is not None:
            os.close(self.master_fd)
            self.master_fd = None
        if self._slave_fd is not None:
            os.close(self._slave_fd)
            self._slave_fd = None

    def serve_once(self, timeout_s: float = 2.0) -> bool:
        fd = self.master_fd
        if fd is None:
            raise RuntimeError("emulator is not open")
        ready, _, _ = select.select([fd], [], [], timeout_s)
        if not ready:
            return False
        data = os.read(fd, 4096)
        if not data:
            return False
        for raw_line in data.splitlines():
            line = raw_line.decode("ascii", errors="replace").strip()
            response = self._handle(line)
            if self.delay_s:
                time.sleep(self.delay_s)
            os.write(fd, (response + "\n").encode("ascii"))
        return True

    def _handle(self, line: str) -> str:
        self.command_count += 1
        if self.malformed_after is not None and self.command_count > self.malformed_after:
            return "NOT_A_VALID_ACK"
        if line == "HELLO":
            return "RP1 OK proto=ROBOPROBE/1 mode=disarmed id=reference-emulator"
        if line.startswith("ARM "):
            if "cutoff=confirmed" in line and "token=" in line:
                self._armed = True
                return "RP1 OK armed=1"
            return "RP1 ERR reason=missing_arm_fields"
        if line == "DISARM":
            self._armed = False
            return "RP1 OK armed=0"
        if line == "STOP":
            self._armed = False
            return "RP1 OK stopped=1"
        if line.startswith("MOVE "):
            fields = dict(FIELD_RE.findall(line.removeprefix("MOVE ")))
            if not self._armed:
                return "RP1 ERR reason=disarmed"
            required = {"channel", "target_us", "seq"}
            if set(fields) != required:
                return "RP1 ERR reason=bad_move_fields"
            if not fields["channel"].isdigit() or not fields["target_us"].isdigit() or not fields["seq"].isdigit():
                return "RP1 ERR reason=bad_move_values"
            return f"RP1 OK seq={fields['seq']} channel={fields['channel']} target_us={fields['target_us']}"
        return "RP1 ERR reason=unknown_command"
