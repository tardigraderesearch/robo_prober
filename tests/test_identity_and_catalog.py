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
