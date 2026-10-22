import pytest

from robo_prober.permissions import PermissionError, load_permissions


def base_permission():
    return {
        "controller_node_id": "usb:/dev/ttySIM",
        "board_profile_id": "esp32_devkitc_v4_wroom32",
        "physical_power_cutoff_required": True,
        "channels": [
            {
                "channel": 0,
                "route": "pca9685_servo",
                "neutral_us": 1500,
                "min_us": 1450,
                "max_us": 1550,
                "step_us": 20,
                "approved_by": "tester",
                "approved_at": "2026-10-13T00:00:00Z",
            }
        ],
    }


def test_permission_accepts_explicit_servo_route():
    permissions = load_permissions(base_permission())
    assert permissions.channels[0].target_us(1) == 1520


def test_permission_denies_generic_gpio_route():
    data = base_permission()
    data["channels"][0]["route"] = "gpio_pwm"
    with pytest.raises(PermissionError, match="generic GPIO"):
        load_permissions(data)


def test_permission_denies_board_pin_authorization():
    data = base_permission()
    data["channels"][0]["board_pin"] = "GPIO4"
    with pytest.raises(PermissionError, match="board pin metadata"):
        load_permissions(data)


def test_permission_denies_step_outside_bounds():
    data = base_permission()
    data["channels"][0]["step_us"] = 80
    with pytest.raises(PermissionError, match="neutral"):
        load_permissions(data)
