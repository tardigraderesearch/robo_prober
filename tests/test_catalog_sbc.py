import pytest
from robo_prober.catalog_sbc import (
    BOARD_PROFILES_SBC,
    SOURCES,
    assert_no_automatic_probe,
    candidate_gpio,
    detect,
    get_profile,
    list_board_ids,
)

# Power and ground positions on standard 40-pin SBC headers
POWER_GROUND_PINS = {1, 2, 4, 6, 9, 14, 17, 20, 25, 30, 34, 39}

# Dedicated ID EEPROM positions on standard 40-pin SBC headers
ID_EEPROM_PINS = {27, 28}


def test_header_has_40_pins():
    """Every SBC board profile must define a complete 40-pin header (pins 1..40)."""
    assert len(BOARD_PROFILES_SBC) > 0
    for board_id, profile in BOARD_PROFILES_SBC.items():
        pins = profile['pins']
        assert len(pins) == 40, f"{board_id} expected 40 pins, got {len(pins)}"
        assert set(pins.keys()) == set(range(1, 41)), f"{board_id} pins do not span 1..40"
        assert profile['pin_index'] == 'physical_header'


def test_power_ground_pins_never_usable():
    """Power and ground pins must never be marked usable or included in candidate GPIOs."""
    for board_id, profile in BOARD_PROFILES_SBC.items():
        cg = candidate_gpio(board_id)
        for pin_num in POWER_GROUND_PINS:
            pin = profile['pins'][pin_num]
            assert pin.get('usable') is False, f"{board_id} pin {pin_num} marked usable"
            assert pin.get('reserved') is True, f"{board_id} pin {pin_num} not reserved"
            assert pin.get('type') in ('3v3', '5v', 'gnd', 'power'), f"{board_id} pin {pin_num} wrong type"
            assert pin_num not in cg, f"{board_id} power/ground pin {pin_num} in candidate_gpio"


def test_id_eeprom_never_usable():
    """HAT/carrier ID EEPROM pins (GPIO 0/1 on Pi, pins 27/28) must never be marked usable."""
    for board_id, profile in BOARD_PROFILES_SBC.items():
        cg = candidate_gpio(board_id)
        for pin_num in ID_EEPROM_PINS:
            pin = profile['pins'][pin_num]
            assert pin.get('usable') is False, f"{board_id} pin {pin_num} marked usable"
            assert pin.get('reserved') is True, f"{board_id} pin {pin_num} not reserved"
            assert pin_num not in cg, f"{board_id} ID EEPROM pin {pin_num} in candidate_gpio"
            assert 'EEPROM' in pin.get('notes', '').upper() or 'ID' in pin.get('notes', '').upper()


def test_detect_maps_known_model_strings():
    """detect() must correctly map /proc/device-tree/model strings to profile IDs."""
    model_cases = [
        ("Raspberry Pi 5 Model B Rev 1.0\x00", "raspberry_pi_5"),
        ("Raspberry Pi 5 Model B Rev 1.0", "raspberry_pi_5"),
        ("Raspberry Pi 4 Model B Rev 1.4\n", "raspberry_pi_4"),
        ("Raspberry Pi 4 Model B Rev 1.1", "raspberry_pi_4"),
        ("Raspberry Pi Zero 2 W Rev 1.0", "raspberry_pi_zero_2w"),
        ("Raspberry Pi 3 Model B Plus Rev 1.3", "raspberry_pi_3b_plus"),
        ("NVIDIA Jetson Orin Nano Developer Kit", "jetson_orin_nano_devkit"),
        ("NVIDIA Jetson Orin NX Developer Kit", "jetson_orin_nx_devkit"),
        ("NVIDIA Jetson AGX Orin Developer Kit", "jetson_agx_orin_devkit"),
    ]
    for model_str, expected_id in model_cases:
        detected = detect(model_str)
        assert detected == expected_id, f"Expected {expected_id} for {model_str!r}, got {detected}"

    # Unknown, blank, or malformed strings should safely return None
    assert detect("") is None
    assert detect("   ") is None
    assert detect(None) is None
    assert detect("Generic x86_64 Desktop PC") is None


def test_every_usable_pin_has_gpio_line():
    """Every pin marked usable must have a valid non-negative integer GPIO line."""
    for board_id, profile in BOARD_PROFILES_SBC.items():
        cg = candidate_gpio(board_id)
        for pin_num, pin in profile['pins'].items():
            if pin.get('usable') is True:
                assert pin_num in cg, f"{board_id} usable pin {pin_num} missing from candidate_gpio"
                assert pin.get('gpio') is not None, f"{board_id} usable pin {pin_num} has None gpio"
                assert isinstance(pin['gpio'], int), f"{board_id} usable pin {pin_num} gpio is not int"
                assert pin['gpio'] >= 0, f"{board_id} usable pin {pin_num} gpio is negative"
                assert pin.get('type') == 'gpio', f"{board_id} usable pin {pin_num} type is not gpio"
                assert pin.get('reserved') is False, f"{board_id} usable pin {pin_num} marked reserved"
            else:
                assert pin_num not in cg, f"{board_id} non-usable pin {pin_num} present in candidate_gpio"


def test_raspberry_pi_profiles():
    """Verify Raspberry Pi specific requirements: RP1 gpiochip, I2C1 pullups, UART, and PWM."""
    pi5 = get_profile('raspberry_pi_5')
    assert pi5['gpiochip'] == 'pinctrl-rp1'
    for pin in pi5['pins'].values():
        if pin.get('role') == 'gpio' or pin.get('type') == 'gpio':
            assert pin.get('gpiochip') == 'pinctrl-rp1'

    for bid in ('raspberry_pi_4', 'raspberry_pi_5', 'raspberry_pi_zero_2w'):
        prof = get_profile(bid)
        # Default I2C1 pins (physical 3 = GPIO 2, physical 5 = GPIO 3)
        p3 = prof['pins'][3]
        p5 = prof['pins'][5]
        assert p3['gpio'] == 2 and p3['usable'] is True
        assert p5['gpio'] == 3 and p5['usable'] is True
        assert '1.8k' in p3['notes'] and 'pulled_up' in p3['notes']
        assert '1.8k' in p5['notes'] and 'pulled_up' in p5['notes']

        # UART pins (physical 8 = GPIO 14, physical 10 = GPIO 15)
        p8 = prof['pins'][8]
        p10 = prof['pins'][10]
        assert p8['gpio'] == 14 and 'UART' in p8['notes']
        assert p10['gpio'] == 15 and 'UART' in p10['notes']

        # Hardware PWM capable pins (physical 12, 32, 33, 35)
        for pwm_pin in (12, 32, 33, 35):
            assert prof['pins'][pwm_pin]['pwm_capable'] is True, f"{bid} pin {pwm_pin} not pwm_capable"


def test_jetson_profiles():
    """Verify Jetson Orin profiles: default GPIO restriction, gpiochip/line names, I2C buses, and PWM."""
    orin_nano = get_profile('jetson_orin_nano_devkit')
    assert orin_nano['gpiochip'] == 'tegra234-gpio'

    # Only pins that are GPIO by default in carrier pinmux (7, 15, 29, 31, 32, 33) are usable
    expected_usable = {7, 15, 29, 31, 32, 33}
    actual_usable = set(candidate_gpio('jetson_orin_nano_devkit').keys())
    assert actual_usable == expected_usable

    # Verify line names and gpiochip on usable pins
    assert orin_nano['pins'][7]['line_name'] == 'PAC.06'
    assert orin_nano['pins'][7]['gpio'] == 144
    assert orin_nano['pins'][15]['line_name'] == 'PN.01'
    assert orin_nano['pins'][15]['gpio'] == 85
    assert orin_nano['pins'][29]['line_name'] == 'PQ.05'
    assert orin_nano['pins'][29]['gpio'] == 105
    assert orin_nano['pins'][31]['line_name'] == 'PQ.06'
    assert orin_nano['pins'][31]['gpio'] == 106
    assert orin_nano['pins'][32]['line_name'] == 'PG.06'
    assert orin_nano['pins'][32]['gpio'] == 41
    assert orin_nano['pins'][33]['line_name'] == 'PH.00'
    assert orin_nano['pins'][33]['gpio'] == 43

    # PWM pins on Jetson Orin Nano / NX dev kit
    for pwm_pin in (15, 32, 33):
        assert orin_nano['pins'][pwm_pin]['pwm_capable'] is True
        assert orin_nano['pins'][pwm_pin]['pwm_chip'] is not None

    # I2C buses on Jetson Orin Nano
    assert orin_nano['pins'][3]['i2c_bus'] == 7
    assert orin_nano['pins'][5]['i2c_bus'] == 7
    assert orin_nano['pins'][27]['i2c_bus'] == 1
    assert orin_nano['pins'][28]['i2c_bus'] == 1


def test_sources_and_fail_closed_invariants():
    """Verify security assertions and documentation citations."""
    assert assert_no_automatic_probe() is True

    for board_id, profile in BOARD_PROFILES_SBC.items():
        assert profile.get('auto_probe') is False
        assert len(profile.get('sources', [])) > 0
        for src in profile['sources']:
            assert src.startswith('http://') or src.startswith('https://')
