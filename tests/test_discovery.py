from robo_prober.discovery import SerialDevice, annotate_devices, is_placeholder_port


def test_annotate_devices_keeps_unknown_as_unknown():
    rows = annotate_devices([SerialDevice("/dev/ttyTEST0", None, None, "mystery")])
    assert rows[0]["status"] == "unknown_interface"
    assert rows[0]["candidate_profiles"] == []


def test_annotate_devices_reports_bridge_warning():
    rows = annotate_devices([SerialDevice("/dev/ttyUSB0", 0x10C4, 0xEA60, "CP210x")])
    assert rows[0]["status"] == "interface_recognized"
    assert rows[0]["match_type"] == "usb_bridge_only"
    assert "bridge/interface" in rows[0]["warning"]


def test_placeholder_ports_are_identified():
    assert is_placeholder_port(SerialDevice("/dev/ttyS0", None, None, "n/a"))
    assert not is_placeholder_port(SerialDevice("/dev/ttyUSB0", 0x10C4, 0xEA60, "CP210x"))
    assert not is_placeholder_port(SerialDevice("/dev/ttyACM0", None, None, "custom adapter"))


class FakePort:
    def __init__(self, device, vid, pid, description):
        self.device, self.vid, self.pid, self.description = device, vid, pid, description
        self.serial_number = self.location = self.manufacturer = self.product = None


def test_discovery_handles_windows_macos_and_linux_port_names(monkeypatch):
    from serial.tools import list_ports

    from robo_prober.discovery import discover_serial_ports

    ports = [
        FakePort("COM3", 0x10C4, 0xEA60, "Silicon Labs CP210x USB to UART Bridge (COM3)"),
        FakePort("COM1", None, None, "Communications Port (COM1)"),
        FakePort("/dev/cu.usbserial-0001", 0x1A86, 0x7523, "USB Serial"),
        FakePort("/dev/cu.Bluetooth-Incoming-Port", None, None, "n/a"),
        FakePort("/dev/ttyACM0", 0x303A, 0x1001, "USB JTAG/serial debug unit"),
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    devices = discover_serial_ports()
    visible = [dev for dev in devices if not is_placeholder_port(dev)]
    assert [dev.device for dev in visible] == ["COM3", "COM1", "/dev/cu.usbserial-0001", "/dev/ttyACM0"]
    rows = annotate_devices(visible)
    assert rows[0]["node_id"] == "usb:COM3" and rows[0]["status"] == "interface_recognized"
    assert rows[2]["status"] == "interface_recognized" and rows[3]["status"] == "interface_recognized"
    assert rows[1]["status"] == "unknown_interface"
