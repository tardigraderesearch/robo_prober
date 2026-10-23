"""Hardware adapter contract tests with injected modules; never open /dev."""
import errno
import sys
import time
from collections import deque
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from robo_prober.sbc import EmulatedBackend, SBCAgent
from robo_prober.sbc.backends import (
    BackendError, GpiodBackend, LgpioBackend, MultiChipBackend, SoftwarePWMBackend,
)
from robo_prober.sbc.boards import pin_map
from robo_prober.sbc.i2c import SMBusScanner


class Direction(Enum):
    INPUT = 1
    OUTPUT = 2


class Bias(Enum):
    DISABLED = 1
    PULL_UP = 2
    PULL_DOWN = 3


class Value(Enum):
    INACTIVE = 0
    ACTIVE = 1


class Edge(Enum):
    NONE = 0
    BOTH = 1


class FakeGpiod:
    line = SimpleNamespace(Direction=Direction, Bias=Bias, Value=Value, Edge=Edge)
    LineSettings = SimpleNamespace

    def __init__(self):
        self.labels = {"/dev/gpiochip0": "pinctrl-bcm2711", "/dev/gpiochip4": "pinctrl-rp1"}
        self.owners = {}
        self.external = set()
        self.operations = []
        self.edges = deque()
        self.race = False
        self.fail_reconfigure = set()

    def Chip(self, path):
        api = self

        class Chip:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

            def close(self):
                api.operations.append(("chip_close", path))

            def get_info(self):
                return SimpleNamespace(label=api.labels[path], num_lines=32)

            def get_line_info(self, pin):
                return SimpleNamespace(used=(path, pin) in api.owners or (path, pin) in api.external)

            def line_offset_from_id(self, name):
                if name.startswith("GPIO") and name[4:].isdigit():
                    return int(name[4:])
                if name == "PN.01":
                    return 12  # deliberately differs from old numeric metadata
                raise ValueError("unknown line name")

            def request_lines(self, *, consumer, config):
                pin, settings = next(iter(config.items()))
                if self.get_line_info(pin).used or api.race:
                    raise OSError(errno.EBUSY, "claimed")
                request = Request(pin, settings)
                api.owners[path, pin] = request
                api.operations.append(("request", pin, settings))
                return request

        class Request:
            def __init__(self, pin, settings):
                self.pin, self.settings = pin, settings

            def get_value(self, pin):
                assert self.settings.direction == Direction.INPUT
                return Value.ACTIVE if self.settings.bias == Bias.PULL_UP else Value.INACTIVE

            def set_value(self, pin, value):
                assert self.settings.direction == Direction.OUTPUT
                api.operations.append(("write", pin, value))

            def reconfigure_lines(self, config):
                if self.pin in api.fail_reconfigure:
                    raise OSError("injected release error")
                self.settings = config[self.pin]
                api.operations.append(("configure", self.pin, self.settings))

            def release(self):
                api.operations.append(("free", self.pin, self.settings.direction))
                api.owners.pop((path, self.pin))

            def wait_edge_events(self, timeout):
                assert timeout == 0
                return bool(api.edges)

            def read_edge_events(self, max_events):
                events = []
                while api.edges and len(events) < max_events:
                    events.append(api.edges.popleft())
                return events

        return Chip()


@pytest.fixture
def gpio(monkeypatch):
    api = FakeGpiod()
    monkeypatch.setattr(Path, "glob", lambda *_: [Path(p) for p in api.labels])
    return api


def test_chip_discovery_uses_pi5_label_not_chip_number(gpio):
    backend = GpiodBackend(("pinctrl-rp1",), module=gpio)
    assert backend.chip_path == "/dev/gpiochip4"
    assert not gpio.owners
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        GpiodBackend(module=gpio)
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        GpiodBackend(("missing",), module=gpio)
    backend.close()


def test_gpiod_survey_only_requests_inputs_and_checks_ownership(gpio):
    backend = GpiodBackend(("pinctrl-rp1",), module=gpio)
    for bias, expected in (("disabled", 0), ("pull_up", 1), ("pull_down", 0)):
        assert backend.sample(4, bias) == expected
    settings = [op[2] for op in gpio.operations if op[0] in ("request", "configure")]
    assert all(setting.direction == Direction.INPUT for setting in settings)
    assert not gpio.owners
    gpio.external.add((backend.chip_path, 5))
    for operation in (lambda: backend.sample(5, "disabled"), lambda: backend.digital(5, 1), lambda: backend.watch([5])):
        with pytest.raises(BackendError, match="pin_claimed"):
            operation()
    gpio.race = True
    with pytest.raises(BackendError, match="pin_claimed"):
        backend.digital(4, 1)
    backend.close()


def test_gpiod_edges_and_release_to_input(gpio):
    backend = GpiodBackend(("pinctrl-rp1",), module=gpio)
    backend.watch([4])
    request = gpio.owners[backend.chip_path, 4]
    assert request.settings.edge_detection == Edge.BOTH
    event_type = SimpleNamespace(RISING_EDGE=1, FALLING_EDGE=2)
    gpio.edges.extend([SimpleNamespace(timestamp_ns=i, event_type=level, Type=event_type) for i, level in ((1, 1), (2, 2))])
    assert backend.events() == [(4, 1), (4, 0)]
    backend.watch([])
    backend.digital(4, 1)
    backend.release(4)
    assert gpio.operations[-1] == ("free", 4, Direction.INPUT)
    assert not gpio.owners
    backend.close()


def test_release_failure_still_attempts_all_lines(gpio):
    backend = GpiodBackend(("pinctrl-rp1",), module=gpio)
    backend.digital(4, 1)
    backend.digital(5, 1)
    gpio.fail_reconfigure.add(4)
    with pytest.raises(OSError):
        backend.release_all()
    assert not gpio.owners
    assert ("free", 5, Direction.INPUT) in gpio.operations
    backend.close()


def test_software_pwm_stops_thread_before_releasing_input(gpio):
    backend = SoftwarePWMBackend(("pinctrl-rp1",), module=gpio)
    backend.servo(4, 1500)
    worker = backend._pwm[4][1]
    backend.servo(4, 1550)
    backend.release_all()
    assert not worker.is_alive() and not backend._pwm and not gpio.owners
    at_release = len(gpio.operations)
    time.sleep(0.025)
    assert len(gpio.operations) == at_release
    assert gpio.operations[-1] == ("free", 4, Direction.INPUT)
    backend.close()


def test_software_pwm_worker_failure_surfaces_and_releases(gpio, monkeypatch):
    backend = SoftwarePWMBackend(("pinctrl-rp1",), module=gpio)
    original = backend.digital
    def fail_high(pin, level):
        if level:
            raise OSError("injected write failure")
        original(pin, level)
    monkeypatch.setattr(backend, "digital", fail_high)
    backend.servo(4, 1500)
    backend._pwm[4][1].join(1)
    with pytest.raises(BackendError):
        backend.check()
    backend.release_all()
    assert not gpio.owners and not backend._pwm
    backend.close()


def test_lgpio_uses_selected_chip_servo_frequency_and_input_release(gpio):
    calls = []
    def call(name):
        def impl(*args, **kwargs):
            calls.append((name, args, kwargs))
            return 0
        return impl
    module = SimpleNamespace(**{name: call(name) for name in (
        "gpiochip_open", "gpiochip_close", "gpio_claim_output", "tx_servo", "gpio_claim_input", "gpio_free")})
    backend = LgpioBackend(("pinctrl-rp1",), module=gpio, lgpio_module=module)
    assert calls[0] == ("gpiochip_open", (4,), {})
    backend.servo(4, 1500)
    assert calls[-1] == ("tx_servo", (0, 4, 1500), {"servo_frequency": 50})
    backend.release_all()
    assert [name for name, _, _ in calls[-3:]] == ["tx_servo", "gpio_claim_input", "gpio_free"]
    assert calls[-3][1] == (0, 4, 0)
    gpio.external.add((backend.chip_path, 5))
    with pytest.raises(BackendError, match="pin_claimed"):
        backend.servo(5, 1500)
    backend.close()


def test_profile_resolves_names_and_never_confuses_different_chips(gpio):
    backend = GpiodBackend(("pinctrl-rp1",), module=gpio)
    record = {"gpio": 85, "line_name": "PN.01", "gpiochip": "pinctrl-rp1",
              "type": "gpio", "reserved": False, "exposed": True, "usable": True}
    known, pins = pin_map({"pins": {15: record}}, backend)
    assert known == {15} and pins == {15: 12}
    assert backend.resolve_pin(dict(record, gpiochip="tegra234-gpio-aon")) is None
    assert backend.resolve_pin(dict(record, line_name="missing")) is None
    backend.close()


def test_multichip_routes_same_offset_to_correct_chip():
    main, aon = EmulatedBackend({4: "servo"}), EmulatedBackend({4: "servo"})
    backend = MultiChipBackend({"main": main, "aon": aon})
    profile = {"pins": {pin: {"gpio": 4, "gpiochip": label, "type": "gpio",
                              "reserved": False, "exposed": True, "usable": True}
                         for pin, label in ((7, "main"), (16, "aon"))}}
    device = SBCAgent(backend, profile=profile)
    assert "pins=7,16" in device.handle("BOARD", 0)
    device.handle("ARM token=t cutoff=confirmed", 0)
    assert device.handle("PROBE pin=16 mode=servo start_us=1500 token=t", 0).startswith("RP1 OK")
    assert not main.outputs and aon.outputs == {4: ("servo", 1500)}
    device.handle("ESTOP", 0)
    assert not main.outputs and not aon.outputs
    device.close()


def test_read_only_gpio_backend_reports_no_pwm_slot(gpio):
    backend = GpiodBackend(("pinctrl-rp1",), module=gpio)
    device = SBCAgent(backend)
    device.handle("ARM token=t cutoff=confirmed", 0)
    assert device.handle("PROBE pin=4 mode=servo start_us=1500 token=t", 0) == "RP1 ERR reason=no_pwm_slot"
    assert not gpio.owners
    device.close()


def test_smbus_read_only_scan_excludes_reserved_and_claimed_addresses(monkeypatch):
    calls = []
    class Bus:
        def __init__(self, path):
            assert path == "/dev/i2c-7"
        def __enter__(self):
            return self
        def __exit__(self, *_):
            calls.append("closed")
        def read_byte(self, address):
            calls.append(address)
            if address == 0x41:
                raise OSError(errno.EBUSY, "kernel device")
            if address not in (0x40, 0x68):
                raise OSError(errno.EREMOTEIO, "NACK")
            return 0
    monkeypatch.setitem(sys.modules, "smbus2", SimpleNamespace(SMBus=Bus))
    assert SMBusScanner().scan(7) == [0x40, 0x68]
    assert calls == list(range(0x08, 0x78)) + ["closed"]


def test_unsupported_i2c_does_not_report_empty_success(monkeypatch):
    class Bus:
        def __init__(self, path):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read_byte(self, address):
            raise OSError(errno.EOPNOTSUPP, "unsupported")
    monkeypatch.setitem(sys.modules, "smbus2", SimpleNamespace(SMBus=Bus))
    device = SBCAgent(EmulatedBackend(), scanner=SMBusScanner())
    assert device.handle("I2C_SCAN bus=0", 0) == "RP1 ERR reason=driver_error"
