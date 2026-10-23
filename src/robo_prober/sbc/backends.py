"""GPIO ownership, input bias sampling, edge watching, and 50 Hz outputs.

Hardware modules are imported only by hardware constructors. No constructor
drives a line. Release explicitly changes outputs to input before dropping the
kernel request: closing an output request alone can leave its level latched.
"""
from __future__ import annotations

import errno
import importlib
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path


class BackendError(RuntimeError):
    """An error with a ROBOPROBE reason safe to put on the wire."""


class GPIOBackend(ABC):
    name = "gpio"
    supports_pwm = False

    @abstractmethod
    def lines(self): ...

    def resolve_line(self, name):
        return int(name) if str(name).isdigit() else None

    def resolve_pin(self, record):
        offset = record.get("line_offset", record.get("gpio"))
        return self.resolve_line(offset) if isinstance(offset, str) else offset

    @abstractmethod
    def claimed(self, pin): ...

    @abstractmethod
    def sample(self, pin, bias): ...

    @abstractmethod
    def watch(self, pins): ...

    @abstractmethod
    def events(self): ...

    @abstractmethod
    def digital(self, pin, level): ...

    def servo(self, pin, pulse_us):
        raise BackendError("no_pwm_slot")

    @abstractmethod
    def release(self, pin): ...

    @abstractmethod
    def release_all(self): ...

    def check(self):
        """Surface asynchronous PWM errors to the protocol watchdog."""

    def close(self):
        self.release_all()


@dataclass
class PinTruth:
    kind: str = "floating"
    level: int = 0
    bus: int = 1
    address: int = 0x40


class EmulatedBackend(GPIOBackend):
    """Hidden wiring is observed only through the same input/output interface.

    Test fixtures can press buttons and inspect LED/servo observations. The
    protocol never reads _truth or uses the component kind to classify a pin.
    """
    name = "EMULATED"
    supports_pwm = True

    def __init__(self, pins=None):
        self._truth = {p: v if isinstance(v, PinTruth) else PinTruth(v)
                       for p, v in (pins if pins is not None else {
                           4: "servo", 17: "led", 27: "button", 22: "pulled_up",
                           23: "driven_high", 24: "claimed", 2: "i2c",
                       }).items()}
        self.outputs = {}
        self.write_log = []
        self.input_log = []
        self.observations = []
        self.watched = set()
        self._events = deque(maxlen=4096)
        self._samples = 0

    def lines(self):
        return sorted(self._truth)

    def claimed(self, pin):
        if pin not in self._truth:
            raise BackendError("bad_pin")
        return self._truth[pin].kind == "claimed"

    def sample(self, pin, bias):
        if self.claimed(pin):
            raise BackendError("pin_claimed")
        self.input_log.append((pin, bias))
        truth = self._truth[pin]
        if truth.kind.startswith("driven"):
            return int(truth.kind == "driven_high")
        if truth.kind == "button" and truth.level:
            return 0
        if bias != "disabled":
            return int(bias == "pull_up")
        if truth.kind in ("pulled_up", "button", "i2c"):
            return 1
        if truth.kind in ("pulled_down", "servo", "led"):
            return 0
        self._samples += 1
        return self._samples % 2

    def watch(self, pins):
        pins = set(pins)
        if any(self.claimed(p) for p in pins):
            raise BackendError("pin_claimed")
        self.watched = pins

    def events(self):
        result = list(self._events)
        self._events.clear()
        return result

    def press(self, pin, pressed=True):
        truth = self._truth[pin]
        if truth.kind != "button":
            raise ValueError("not a button")
        if truth.level != int(pressed) and pin in self.watched:
            self._events.append((pin, int(not pressed)))
        truth.level = int(pressed)

    def _write(self, pin, kind, value):
        if self.claimed(pin):
            raise BackendError("pin_claimed")
        self.outputs[pin] = (kind, value)
        self.write_log.append((pin, kind, value))
        if self._truth[pin].kind == kind:
            self.observations.append((pin, kind, value))
        if kind == "digital" and self._truth[pin].kind == "led":
            self.observations.append((pin, "led", value))

    def digital(self, pin, level):
        self._write(pin, "digital", level)

    def servo(self, pin, pulse_us):
        self._write(pin, "servo", pulse_us)

    def release(self, pin):
        self.outputs.pop(pin, None)
        self.watched.discard(pin)

    def release_all(self):
        self.outputs.clear()
        self.watched.clear()
        self._events.clear()

    def scan(self, bus):
        return sorted({t.address for t in self._truth.values()
                       if t.kind == "i2c" and t.bus == bus and 0x08 <= t.address < 0x78})


class GpiodBackend(GPIOBackend):
    name = "gpiod"

    def __init__(self, chip_labels=(), *, chip_path=None, module=None):
        self.gpiod = module or importlib.import_module("gpiod")
        if not hasattr(self.gpiod, "LineSettings"):
            raise RuntimeError("gpiod v2 is required")
        self.lock = threading.RLock()
        self.requests = {}
        self.watched = set()
        paths = [chip_path] if chip_path else sorted(Path("/dev").glob("gpiochip*"))
        candidates = []
        for path in paths:
            with self.gpiod.Chip(str(path)) as chip:
                label = chip.get_info().label
                if chip_path or not chip_labels or label in chip_labels:
                    candidates.append((str(path), label))
        if len(candidates) != 1:
            raise RuntimeError("GPIO chip label missing or ambiguous; specify --chip")
        self.chip_path, self.chip_label = candidates[0]
        self.chip = self.gpiod.Chip(self.chip_path)

    def lines(self):
        with self.lock:
            return range(self.chip.get_info().num_lines)

    def resolve_line(self, name):
        with self.lock:
            try:
                return self.chip.line_offset_from_id(name)
            except (ValueError, OSError):
                return None

    def resolve_pin(self, record):
        label = record.get("gpiochip")
        if label and label != self.chip_label:
            return None
        if record.get("line_name"):
            # Names survive Jetson kernel offset changes; never silently map a
            # missing name to a potentially different pin's old numeric offset.
            return self.resolve_line(record["line_name"])
        return super().resolve_pin(record)

    def claimed(self, pin):
        with self.lock:
            return pin not in self.requests and self.chip.get_line_info(pin).used

    def _settings(self, *, output=False, bias="disabled", watch=False, level=0):
        line = self.gpiod.line
        return self.gpiod.LineSettings(
            direction=line.Direction.OUTPUT if output else line.Direction.INPUT,
            bias={"disabled": line.Bias.DISABLED, "pull_up": line.Bias.PULL_UP,
                  "pull_down": line.Bias.PULL_DOWN}[bias],
            edge_detection=line.Edge.BOTH if watch else line.Edge.NONE,
            output_value=line.Value.ACTIVE if level else line.Value.INACTIVE,
        )

    def _request(self, pin, settings):
        if self.claimed(pin):
            raise BackendError("pin_claimed")
        try:
            request = self.chip.request_lines(consumer="robo-prober", config={pin: settings})
        except OSError as error:
            if error.errno == errno.EBUSY:
                raise BackendError("pin_claimed") from error
            raise
        self.requests[pin] = request
        return request

    def sample(self, pin, bias):
        with self.lock:
            if pin in self.requests:
                raise BackendError("pin_claimed")
            request = self._request(pin, self._settings(bias=bias))
            try:
                time.sleep(0.001)
                return int(request.get_value(pin) == self.gpiod.line.Value.ACTIVE)
            finally:
                self.release(pin)

    def watch(self, pins):
        with self.lock:
            pins = set(pins)
            for pin in self.watched - pins:
                self.release(pin)
            for pin in pins - self.watched:
                self._request(pin, self._settings(watch=True, bias="pull_up"))
                self.watched.add(pin)

    def events(self):
        with self.lock:
            events = []
            for pin in self.watched:
                request = self.requests[pin]
                if request.wait_edge_events(0):
                    for event in request.read_edge_events(max_events=128):
                        events.append((event.timestamp_ns, pin,
                                       int(event.event_type == event.Type.RISING_EDGE)))
            return [(pin, level) for _, pin, level in sorted(events)]

    def digital(self, pin, level):
        with self.lock:
            request = self.requests.get(pin)
            if request is None:
                self._request(pin, self._settings(output=True, level=level))
            elif pin in self.watched:
                raise BackendError("pin_claimed")
            else:
                request.set_value(pin, self.gpiod.line.Value.ACTIVE if level
                                  else self.gpiod.line.Value.INACTIVE)

    def release(self, pin):
        with self.lock:
            request = self.requests.get(pin)
            if request is not None:
                try:
                    request.reconfigure_lines({pin: self._settings()})
                finally:
                    request.release()
                    del self.requests[pin]
                    self.watched.discard(pin)

    def release_all(self):
        failure = None
        for pin in list(self.requests):
            try:
                self.release(pin)
            except Exception as error:
                failure = error
        if failure:
            raise failure

    def close(self):
        try:
            self.release_all()
        finally:
            self.chip.close()


class SoftwarePWMBackend(GpiodBackend):
    """Best effort userspace PWM. Linux scheduling can introduce pulse jitter."""
    name = "software"
    supports_pwm = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pwm = {}
        self._error = None

    def servo(self, pin, pulse_us):
        self.check()
        if pin in self._pwm:
            self._pwm[pin][2][0] = pulse_us
            return
        self.digital(pin, 0)
        stop = threading.Event()
        width = [pulse_us]

        def pulse():
            try:
                while not stop.is_set():
                    start = time.monotonic()
                    self.digital(pin, 1)
                    stop.wait(width[0] / 1_000_000)
                    self.digital(pin, 0)
                    stop.wait(max(0, 0.020 - (time.monotonic() - start)))
            except Exception as error:
                self._error = error
                try:
                    GpiodBackend.release(self, pin)
                except Exception:
                    pass  # check() reports the original failure to the core.

        thread = threading.Thread(target=pulse, name="sbc-pwm", daemon=True)
        self._pwm[pin] = (stop, thread, width)
        thread.start()

    def check(self):
        if self._error is not None:
            raise BackendError("driver_error") from self._error

    def release(self, pin):
        pwm = self._pwm.pop(pin, None)
        if pwm:
            pwm[0].set()
            pwm[1].join()
        super().release(pin)

    def release_all(self):
        failure = None
        for pin in set(self.requests) | set(self._pwm):
            try:
                self.release(pin)
            except Exception as error:
                failure = error
        if failure:
            raise failure


class LgpioBackend(GpiodBackend):
    """libgpiod inputs/edges with lgpio's tx_servo on the same selected chip."""
    name = "lgpio"
    supports_pwm = True

    def __init__(self, *args, lgpio_module=None, **kwargs):
        self.lgpio = lgpio_module or importlib.import_module("lgpio")
        super().__init__(*args, **kwargs)
        self._servos = set()
        try:
            self.handle = self.lgpio.gpiochip_open(int(Path(self.chip_path).name.removeprefix("gpiochip")))
        except Exception:
            self.chip.close()
            raise

    @staticmethod
    def _ok(result):
        if result < 0:
            raise BackendError("driver_error")

    def claimed(self, pin):
        return pin not in self._servos and super().claimed(pin)

    def servo(self, pin, pulse_us):
        with self.lock:
            if pin not in self._servos:
                if self.claimed(pin) or pin in self.requests:
                    raise BackendError("pin_claimed")
                self._ok(self.lgpio.gpio_claim_output(self.handle, pin, 0))
                self._servos.add(pin)
            self._ok(self.lgpio.tx_servo(self.handle, pin, pulse_us, servo_frequency=50))

    def release(self, pin):
        with self.lock:
            if pin in self._servos:
                try:
                    self._ok(self.lgpio.tx_servo(self.handle, pin, 0))
                finally:
                    # Reclaiming an owned line as input cancels the old output
                    # claim in lgpio; then free the input claim.
                    try:
                        self._ok(self.lgpio.gpio_claim_input(self.handle, pin))
                    finally:
                        self._ok(self.lgpio.gpio_free(self.handle, pin))
                        self._servos.discard(pin)
            super().release(pin)

    def release_all(self):
        failure = None
        for pin in set(self.requests) | self._servos:
            try:
                self.release(pin)
            except Exception as error:
                failure = error
        if failure:
            raise failure

    def close(self):
        try:
            super().close()
        finally:
            self.lgpio.gpiochip_close(self.handle)


class MultiChipBackend(GPIOBackend):
    """Route catalog pins across e.g. Jetson's main and always-on chips."""

    def __init__(self, chips):
        self.chips = chips
        self.name = "multichip"
        self.supports_pwm = all(chip.supports_pwm for chip in chips.values())

    def lines(self):
        return [(label, pin) for label, chip in self.chips.items() for pin in chip.lines()]

    def resolve_pin(self, record):
        label = record.get("gpiochip")
        if label not in self.chips:
            return None
        pin = self.chips[label].resolve_pin(record)
        return (label, pin) if pin is not None else None

    def claimed(self, pin):
        return self.chips[pin[0]].claimed(pin[1])

    def sample(self, pin, bias):
        return self.chips[pin[0]].sample(pin[1], bias)

    def watch(self, pins):
        pins = list(pins)
        for label, chip in self.chips.items():
            chip.watch(pin for name, pin in pins if name == label)

    def events(self):
        return [((label, pin), level) for label, chip in self.chips.items() for pin, level in chip.events()]

    def digital(self, pin, level):
        self.chips[pin[0]].digital(pin[1], level)

    def servo(self, pin, pulse_us):
        self.chips[pin[0]].servo(pin[1], pulse_us)

    def release(self, pin):
        self.chips[pin[0]].release(pin[1])

    def _each(self, operation):
        failure = None
        for chip in self.chips.values():
            try:
                getattr(chip, operation)()
            except Exception as error:
                failure = error
        if failure:
            raise failure

    def check(self):
        self._each("check")

    def release_all(self):
        self._each("release_all")

    def close(self):
        self._each("close")
