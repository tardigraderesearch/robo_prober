"""Firmware-in-the-loop bench: the real firmware core behind a pseudo terminal.

``FirmwareBench`` compiles ``firmware/esp32_robo_prober/src/rp_core.cpp`` with
``test/native/bench_main.cpp`` into a desktop program, starts it, and gives
you the path of a pseudo terminal that behaves like the controller's USB
serial port. The robo-prober host (or any serial client) connects to it the
same way it connects to an ESP32, and every PWM output the firmware drives is
reported back, timestamped, so a test or a simulated robot can follow it::

    with FirmwareBench() as bench:
        with connect(bench.port) as ctl:
            ctl.arm(power_cutoff_confirmed=True)
            ctl.start(0, 1500)
            print(bench.outputs[0])            # 1500
        bench.wait_released()                  # closing disarmed it

Unlike the Python device model (``reference_device``), this runs the C++
code that ships on the board, with its own millisecond clock, so watchdog
and slew timing are the firmware's. Needs a C++17 compiler and a POSIX
pseudo terminal (Linux, macOS). It is a test tool; it never touches real
hardware.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

CHANNELS = 16
REPO_FIRMWARE = Path(__file__).resolve().parents[2] / "firmware" / "esp32_robo_prober"
PwmCallback = Callable[[float, int, "int | None"], None]


class BenchError(RuntimeError):
    pass


def bench_available() -> bool:
    return os.name == "posix" and sys.platform != "cygwin" and _compiler() is not None and REPO_FIRMWARE.is_dir()


def _compiler() -> str | None:
    for name in (os.environ.get("CXX"), "c++", "g++", "clang++"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


def build_bench(firmware_dir: Path | None = None, cache_dir: Path | None = None) -> Path:
    """Compile the bench (cached by source hash) and return the executable."""
    firmware_dir = Path(firmware_dir or REPO_FIRMWARE)
    sources = [firmware_dir / "src" / "rp_core.cpp", firmware_dir / "test" / "native" / "bench_main.cpp"]
    headers = [firmware_dir / "src" / "rp_core.h"]
    missing = [str(p) for p in sources + headers if not p.is_file()]
    if missing:
        raise BenchError("firmware sources not found (use a source checkout): " + ", ".join(missing))
    compiler = _compiler()
    if compiler is None:
        raise BenchError("no C++ compiler found (set CXX)")
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in sources + headers)).hexdigest()[:16]
    base = Path(cache_dir or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "robo-prober" / "bench")
    exe = base / digest / "rp_bench"
    if exe.is_file():
        return exe
    exe.parent.mkdir(parents=True, exist_ok=True)
    tmp = exe.with_name(f"rp_bench.{os.getpid()}.tmp")
    result = subprocess.run(
        [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(firmware_dir / "src"),
         *map(str, sources), "-o", str(tmp)],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BenchError("firmware bench failed to compile:\n" + result.stderr)
    os.replace(tmp, exe)  # atomic: parallel builders never see a half-written binary
    return exe


class FirmwareBench:
    """Run the native firmware core behind a pseudo terminal.

    ``outputs`` holds the pulse the firmware's output driver last wrote on
    each channel (None = released). ``on_pwm(t_s, channel, pulse_or_None)``
    is called from a reader thread for every change; ``t_s`` is the
    firmware clock in seconds.
    """

    def __init__(self, *, on_pwm: PwmCallback | None = None, firmware_dir: Path | None = None,
                 cache_dir: Path | None = None):
        self.exe = build_bench(firmware_dir, cache_dir)
        self.on_pwm = on_pwm
        self.outputs: dict[int, int | None] = {ch: None for ch in range(CHANNELS)}
        self.events: list[tuple[float, int, int | None]] = []
        self.port: str | None = None
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Condition()
        self._acks: list[str] = []

    def open(self) -> str:
        self._proc = subprocess.Popen([str(self.exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      text=True, bufsize=1)
        first = self._proc.stdout.readline().strip()
        if not first.startswith("PORT "):
            self.close()
            raise BenchError(f"bench did not start: {first!r}")
        self.port = first.split(" ", 1)[1]
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        return self.port

    def __enter__(self) -> "FirmwareBench":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read(self) -> None:
        for line in self._proc.stdout:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "PWM":
                t, channel = int(parts[1]) / 1000.0, int(parts[2])
                pulse = None if parts[3] == "off" else int(parts[3])
                with self._lock:
                    self.outputs[channel] = pulse
                    self.events.append((t, channel, pulse))
                    self._lock.notify_all()
                if self.on_pwm is not None:
                    self.on_pwm(t, channel, pulse)
            elif parts and parts[0] == "ACK":
                with self._lock:
                    self._acks.append(line[4:].strip())
                    self._lock.notify_all()

    def _control(self, command: str, timeout_s: float = 2.0) -> None:
        if self._proc is None or self._proc.poll() is not None:
            raise BenchError("bench is not running")
        self._proc.stdin.write(command + "\n")
        self._proc.stdin.flush()
        with self._lock:
            if not self._lock.wait_for(lambda: command in self._acks, timeout_s):
                raise BenchError(f"bench did not acknowledge {command}")
            self._acks.remove(command)

    def estop_pin(self) -> None:
        """Trip the hardware e-stop input."""
        self._control("ESTOP_PIN")

    def driver_fail(self, fail: bool = True) -> None:
        self._control("DRIVER fail" if fail else "DRIVER ok")

    def active_channels(self) -> dict[int, int]:
        with self._lock:
            return {ch: p for ch, p in self.outputs.items() if p is not None}

    def wait_released(self, timeout_s: float = 2.0) -> float:
        """Wait until every output is released; returns the seconds waited."""
        start = time.monotonic()
        with self._lock:
            if not self._lock.wait_for(lambda: all(p is None for p in self.outputs.values()), timeout_s):
                raise BenchError(f"outputs still driven after {timeout_s} s: {self.active_channels()}")
        return time.monotonic() - start

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.stdin.write("QUIT\n")
                proc.stdin.flush()
            proc.wait(timeout=2)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        if self._reader is not None:
            self._reader.join(timeout=2)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
