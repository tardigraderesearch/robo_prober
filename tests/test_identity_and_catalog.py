from robo_prober.catalog import BOARD_PROFILES, assert_no_automatic_probe
from robo_prober.identity import chip_profile_id, matching_boards_for_chip, normalize_chip_name, parse_esp32_chip_id_output


def test_esp32_c3_name_normalized_and_profiled():
    assert normalize_chip_name("ESP32_C3") == "ESP32-C3"
    assert chip_profile_id("ESP32_C3") == "esp32_c3_chip_reference"
    assert parse_esp32_chip_id_output("Chip type: ESP32_C3\n") == "ESP32-C3"


def test_usb_catalog_remains_fail_closed():
    assert assert_no_automatic_probe() is True
    for profile in BOARD_PROFILES.values():
        assert profile["auto_probe"] is False


def test_matching_boards_are_exact_board_profiles_only():
    matches = matching_boards_for_chip("ESP32-S3")
    assert "esp32_s3_devkitc_1" in matches
    assert "esp32_s3_chip_reference" not in matches


def test_firmware_pin_tables_are_generated_from_the_catalog():
    from robo_prober.firmware_pins import HEADER, chip_flags, render_header
    assert HEADER.read_text(encoding="utf-8") == render_header(), "run: python -m robo_prober.firmware_pins"
    s3 = chip_flags("esp32s3")
    assert s3[19] == s3[20] == s3[43] == s3[44] == 0          # USB and the host UART: never touched
    assert all(s3[pin] == 0 for pin in range(26, 38))          # flash / PSRAM
    assert s3[0] & 2 and s3[46] & 4 and s3[1] & 8              # strapping, input only, ADC
