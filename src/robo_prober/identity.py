from __future__ import annotations

import re
import subprocess
import sys

from .catalog import BOARD_PROFILES

CHIP_PROFILE_IDS = {
    "ESP32": "esp32_chip_reference",
    "ESP32-S2": "esp32_s2_chip_reference",
    "ESP32-S3": "esp32_s3_chip_reference",
    "ESP32-C3": "esp32_c3_chip_reference",
    "ESP32-C6": "esp32_c6_chip_reference",
}


def normalize_chip_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().upper().replace("_", "-")
    aliases = {
        "ESP32C3": "ESP32-C3",
        "ESP32S2": "ESP32-S2",
        "ESP32S3": "ESP32-S3",
        "ESP32C6": "ESP32-C6",
    }
    return aliases.get(cleaned, cleaned)


def parse_esp32_chip_id_output(output: str) -> str | None:
    match = re.search(r"Chip type:\s*(ESP32(?:[-_][A-Z0-9]+)?)", output, re.IGNORECASE)
    return normalize_chip_name(match.group(1)) if match else None


def chip_profile_id(chip: str | None) -> str | None:
    return CHIP_PROFILE_IDS.get(normalize_chip_name(chip))


def matching_boards_for_chip(chip: str | None) -> list[str]:
    normalized = normalize_chip_name(chip)
    if normalized is None:
        return []
    return sorted(
        board_id
        for board_id, profile in BOARD_PROFILES.items()
        if profile.get("platform") == normalized and profile.get("board_pinout_verified") is True
    )


def identify_esp32_with_consent(port: str, *, consent_reset: bool, timeout_s: float = 25.0) -> tuple[str | None, str | None]:
    """Run esptool chip-id only after explicit reset/identification consent."""
    if not consent_reset:
        return None, "controller identification was not run because reset/open consent was not provided"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "esptool", "--port", port, "chip-id"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "controller identification timed out"
    except OSError as error:
        return None, str(error)

    output = result.stdout + "\n" + result.stderr
    if result.returncode != 0:
        lowered = output.lower()
        if "permission denied" in lowered:
            return None, "serial port permission denied; check OS serial permissions"
        if "could not open" in lowered:
            return None, "could not open serial port"
        return None, "esp32 identification failed: " + output[-500:]

    chip = parse_esp32_chip_id_output(output)
    if chip is None:
        return None, "no ESP32 chip type was reported"
    return chip, None
