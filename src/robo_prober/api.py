"""Importable Python API.

Everything the CLI and the ROM window do is available from Python::

    import robo_prober as rp

    print(rp.discover())                      # serial ports, never opened
    with rp.connect("COM3") as ctl:           # or "/dev/ttyUSB0", or rp.connect(emulator=True)
        ctl.set_limits(0, 1300, 1700, neutral_us=1500)
        ctl.arm(power_cutoff_confirmed=True)  # session token generated for you
        ctl.start(0, 1500)                    # first pulse: explicit, never a firmware default
        ctl.start(104, 1500)                  # channel 100+N: a servo straight on GPIO N
        ctl.set_pulse(0, 1550)                # slew-limited on the host and in the firmware
        ctl.disarm()

    hw = rp.load_map("robot.map.json")
    hw = rp.set_rom(hw, channel=0, rom1_us=1720, rom2_us=1280, set_by="me")
    rp.save_map(hw, "robot.map.json", overwrite=True)

The same safety rules apply as on the command line: nothing moves until
``arm(power_cutoff_confirmed=True)``, the firmware enforces its band, slew
and watchdog, and closing the controller disarms it.
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from .discovery import annotate_devices, discover_serial_ports, is_placeholder_port
from .map_schema import export_atomic, load_map as _load_map
from .reference_device import ReferenceDevice
from .transport import (
    Heartbeat,
    InProcessEmulatorConnection,
    ProtocolError,
    SerialProtocolTransport,
    settle_timeout_s,
)

EMULATOR_PORT = "emulator"
GPIO_BASE = 100  # channel 100+N is GPIO N driven directly by the controller
DEFAULT_HOST_SLEW_US_S = 200


def discover(*, include_placeholders: bool = False) -> list[dict[str, Any]]:
    """Serial ports with USB bridge hints. Ports are listed, never opened."""
    ports = discover_serial_ports()
    if not include_placeholders:
        ports = [port for port in ports if not is_placeholder_port(port)]
    return annotate_devices(ports)


def connect(port: str | None = None, *, emulator: bool = False, baudrate: int = 115200,
            timeout_s: float = 1.0, device: ReferenceDevice | None = None) -> "Controller":
    """Open a controller and say HELLO. Nothing is armed and nothing moves.

    ``emulator=True`` (or ``port="emulator"``) uses the built-in firmware
    model in this process, on any OS, with no hardware.
    """
    if emulator or port == EMULATOR_PORT:
        transport = SerialProtocolTransport(EMULATOR_PORT, timeout_s=timeout_s,
                                            connection=InProcessEmulatorConnection(device))
    elif port:
        transport = SerialProtocolTransport(port, baudrate, timeout_s)
    else:
        raise ValueError("pass a serial port (e.g. COM3, /dev/ttyUSB0) or emulator=True")
    controller = Controller(transport)
    controller.open()
    return controller


class Controller:
    """A ROBOPROBE/1 controller session."""

    def __init__(self, transport: SerialProtocolTransport):
        self.transport = transport
        self.info: dict[str, str] = {}
        self.token: str | None = None
        self._heartbeat: Heartbeat | None = None
        self._positions: dict[int, int] = {}

    # -- lifecycle --------------------------------------------------------
    def open(self) -> "Controller":
        self.transport.open()
        try:
            self.info = self.transport.hello()
        except Exception:
            self.transport.close()
            raise
        return self

    def close(self) -> None:
        """Disarm (releasing every output) if armed, then close the port."""
        try:
            if self.armed:
                self.disarm()
        finally:
            self._stop_heartbeat()
            self.transport.close()

    def __enter__(self) -> "Controller":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None and self.armed:
            try:
                self.stop()
            except Exception:
                pass
        self.close()

    @property
    def caps(self) -> frozenset[str]:
        return self.transport.caps

    @property
    def armed(self) -> bool:
        return self.token is not None

    def hello(self) -> dict[str, str]:
        self.info = self.transport.hello()
        return self.info

    # -- arming and stopping ----------------------------------------------
    def arm(self, *, power_cutoff_confirmed: bool, token: str | None = None, heartbeat: str = "thread") -> str:
        """Arm the controller. Returns the session token (generated if not given).

        ``heartbeat="thread"`` keeps the firmware watchdog fed from a background
        thread while this process lives. ``heartbeat="manual"`` leaves it to
        the caller: call ``heartbeat()`` (or move) more often than the
        watchdog period, so a frozen caller also releases the outputs.
        """
        if heartbeat not in ("thread", "manual"):
            raise ValueError("heartbeat must be 'thread' or 'manual'")
        if self.armed:
            raise ProtocolError("already armed")
        token = token or secrets.token_hex(8)
        self.transport.arm(token, cutoff_confirmed=power_cutoff_confirmed)
        self.token = token
        self._positions.clear()
        if heartbeat == "thread":
            self._heartbeat = Heartbeat.for_transport(self.transport)
            if self._heartbeat is not None:
                self._heartbeat.start()
        return token

    def heartbeat(self) -> None:
        """Feed the firmware watchdog once (for heartbeat="manual" sessions)."""
        if not self.armed:
            raise ProtocolError("not armed")
        if self._heartbeat is not None:
            self._heartbeat.check()
        if self.transport.has_caps("hb"):
            self.transport.heartbeat()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat = None

    def _end_session(self) -> None:
        self._stop_heartbeat()
        self.token = None
        self._positions.clear()

    def disarm(self) -> None:
        """Release every output and disarm."""
        self._stop_heartbeat()
        try:
            self.transport.disarm()
        finally:
            self._end_session()

    def stop(self) -> None:
        """Release every output and disarm (non-latching)."""
        self._stop_heartbeat()
        try:
            self.transport.stop()
        finally:
            self._end_session()

    def estop(self) -> None:
        """Release every output and latch the e-stop until clear_estop()."""
        self._stop_heartbeat()
        try:
            self.transport.estop()
        finally:
            self._end_session()

    def clear_estop(self, *, power_cutoff_confirmed: bool) -> None:
        self.transport.estop_clear(cutoff_confirmed=power_cutoff_confirmed)

    # -- channels ---------------------------------------------------------
    def set_limits(self, channel: int, min_us: int, max_us: int, *, neutral_us: int | None = None,
                   slew_us_s: int = 250) -> dict[str, str]:
        """Tell the firmware the band (and slew limit) for a channel."""
        self.transport.require_caps("limit")
        if neutral_us is None:
            position = self._positions.get(channel)
            neutral_us = position if position is not None and min_us <= position <= max_us else (min_us + max_us) // 2
        return self.transport.limit(channel, min_us, neutral_us, max_us, slew_us_s)

    def status(self, channel: int) -> dict[str, int]:
        self.transport.require_caps("status")
        return self.transport.status(channel)

    def position(self, channel: int) -> int | None:
        """Last pulse commanded on a channel in this session, or None if never driven."""
        return self._positions.get(channel)

    # -- discovery (read-only until armed) ---------------------------------
    def board(self) -> dict[str, str]:
        """Board profile id, chip model and the pins the firmware may survey."""
        self.transport.require_caps("survey")
        return self.transport.board()

    def survey(self, pin: int) -> dict[str, str]:
        self.transport.require_caps("survey")
        return self.transport.survey(pin)

    def watch(self, on: bool) -> None:
        self.transport.require_caps("watch")
        self.transport.watch(on)

    def events(self) -> list[tuple[int, int]]:
        return self.transport.events()

    def i2c_scan(self, sda: int, scl: int) -> tuple[list[str], list[str]]:
        self.transport.require_caps("i2c")
        return self.transport.i2c_scan(sda, scl)

    def blink(self, pin: int, count: int = 3) -> None:
        """Slow on/off pulses on one GPIO (LEDs, buzzers, relays); the firmware releases it after."""
        if not self.armed:
            raise ProtocolError("not armed")
        self.transport.require_caps("blink")
        self.transport.blink(pin, count)

    def release_pin(self, pin: int) -> None:
        """Stop driving a probed GPIO (it goes back to an input)."""
        if not self.armed:
            raise ProtocolError("not armed")
        self.transport.release(pin)
        self._positions.pop(GPIO_BASE + pin, None)

    def start(self, channel: int, pulse_us: int) -> None:
        """Begin output on a channel at an explicitly chosen pulse.

        Hobby servos report no position, so the servo goes to this pulse from
        wherever it is. Pick the pulse it is most likely already at (for
        example the last position saved in the hardware map) and make sure
        the joint is clear.
        """
        if not self.armed:
            raise ProtocolError("not armed")
        if channel in self._positions:
            raise ProtocolError(f"channel {channel} is already started")
        if self._heartbeat is not None:
            self._heartbeat.check()
        if channel >= GPIO_BASE:  # a GPIO pin driven directly (PROBE)
            self.transport.require_caps("probe")
            if self.transport.probe(channel - GPIO_BASE, int(pulse_us)) != channel:
                raise ProtocolError(f"firmware put GPIO {channel - GPIO_BASE} on another channel")
        elif "start" in self.caps:
            self.transport.start(channel, int(pulse_us))
        else:  # v0.1 firmware has no START; its first MOVE plays the same role
            self.transport.move_us(channel, int(pulse_us))
        self._positions[channel] = int(pulse_us)

    def positions(self) -> dict[int, int]:
        """Last commanded pulse of every started channel in this session."""
        return dict(self._positions)

    def move(self, channel: int, pulse_us: int) -> None:
        """Send one MOVE. The firmware slews toward it at its own limit."""
        if not self.armed:
            raise ProtocolError("not armed")
        if channel not in self._positions:
            raise ProtocolError(f"channel {channel} is not started; call start(channel, pulse_us) first")
        if self._heartbeat is not None:
            self._heartbeat.check()
        self.transport.move_us(channel, int(pulse_us))
        self._positions[channel] = int(pulse_us)

    def step_toward(self, channel: int, target_us: int, max_step_us: int) -> int:
        """Move at most ``max_step_us`` from the current position toward ``target_us``."""
        position = self._positions.get(channel)
        if position is None:
            raise ProtocolError(f"channel {channel} is not started; call start(channel, pulse_us) first")
        delta = max(-max_step_us, min(max_step_us, int(target_us) - position))
        new = position + delta
        self.move(channel, new)
        return new

    def set_pulse(self, channel: int, pulse_us: int, *, slew_us_s: int = DEFAULT_HOST_SLEW_US_S,
                  tick_s: float = 0.05, wait: bool = True) -> int:
        """Ramp a channel to ``pulse_us`` at no more than ``slew_us_s`` (blocking)."""
        if not 1 <= slew_us_s <= 2000:
            raise ValueError("slew_us_s must be 1..2000")
        max_step = max(1, int(round(slew_us_s * tick_s)))
        while self._positions.get(channel) != pulse_us:
            self.step_toward(channel, pulse_us, max_step)
            if self._positions.get(channel) != pulse_us:
                time.sleep(tick_s)
        if wait and "status" in self.caps:
            state = self.transport.status(channel)
            timeout = settle_timeout_s(pulse_us - state["current_us"], state["slew_us_s"])
            self.transport.wait_settled(channel, pulse_us, timeout_s=timeout)
        return pulse_us


def load_map(path: str | Path) -> dict[str, Any]:
    """Load and validate a hardware map."""
    return _load_map(Path(path))


def save_map(hardware_map: dict[str, Any], path: str | Path, *, overwrite: bool = False) -> Path:
    """Validate and write a hardware map atomically."""
    export_atomic(hardware_map, Path(path), overwrite=overwrite)
    return Path(path)
