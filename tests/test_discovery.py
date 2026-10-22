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
