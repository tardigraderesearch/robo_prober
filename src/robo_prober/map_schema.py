from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
JOINT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# Joints are servos; everything else the find-servos step names goes in "devices".
# A "faulty_servo" moved one way only (broken feedback, or continuous rotation):
# it is never a joint, so nothing tries to hold a position with it.
DEVICE_KINDS = ("servo", "faulty_servo", "led", "button", "i2c", "sensor", "unknown")


class MapValidationError(ValueError):
    pass


def stable_json_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_classifications(
    classifications: list[dict[str, Any]], expected_joints: tuple[str, ...] | None = None
) -> None:
    """Validate operator joint assignments.

    Joint names are operator-chosen identifiers. When ``expected_joints`` is
    given, every listed joint must be present and no others are allowed.
    """
    if not classifications:
        raise MapValidationError("at least one confirmed joint classification is required")
    joints: set[str] = set()
    channels: set[int] = set()
    for item in classifications:
        joint = str(item.get("joint", ""))
        if not JOINT_NAME_RE.match(joint):
            raise MapValidationError(f"invalid or missing joint name {joint!r}")
        if expected_joints is not None and joint not in expected_joints:
            raise MapValidationError(f"unexpected joint {joint!r}")
        channel = int(item.get("channel"))
        if channel in channels:
            raise MapValidationError(f"duplicate channel {channel}")
        if joint in joints:
            raise MapValidationError(f"duplicate joint {joint}")
        if item.get("human_confirmed") is not True:
            raise MapValidationError(f"joint {joint} lacks human confirmation")
        if item.get("direction") not in ("positive", "negative", "unknown"):
            raise MapValidationError(f"joint {joint} has invalid direction")
        channels.add(channel)
        joints.add(joint)
    missing = set(expected_joints or ()) - joints
    if missing:
        raise MapValidationError("missing joint classifications: " + ", ".join(sorted(missing)))


def build_hardware_map(
    *,
    controller_identity: dict[str, Any],
    board_profile_id: str,
    permissions: dict[str, Any],
    observations: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    validate_classifications(classifications)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "software_verified_physical_unverified",
        "identities": {"controller": controller_identity, "board_profile_id": board_profile_id},
        "references": {
            "catalog": "robo_prober.catalog (reference metadata only; never a probing permission)",
            "protocol": "ROBOPROBE/1 reference protocol",
        },
        "probe_permissions": permissions,
        "permission_hash": stable_json_hash(permissions),
        "observations": observations,
        "classification_provenance": {
            "method": "marker motion summarized, then explicit human/operator confirmation required",
            "human_confirmed": True,
            "largest_displacement_not_sufficient": True,
        },
        "joints": {
            item["joint"]: {
                "channel": int(item["channel"]),
                "direction": item["direction"],
                "invert": bool(item.get("invert", False)),
                "neutral_us": int(item["neutral_us"]),
                "min_us": int(item["min_us"]),
                "max_us": int(item["max_us"]),
                "route": item["route"],
            }
            for item in classifications
        },
        "warnings": warnings or [],
    }


def validate_map(data: dict[str, Any]) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise MapValidationError("unsupported schema_version")
    identities = data.get("identities")
    if not isinstance(identities, dict) or not identities.get("board_profile_id"):
        raise MapValidationError("identities.board_profile_id is required")
    joints = data.get("joints")
    if not isinstance(joints, dict):
        raise MapValidationError("joints must be an object")
    validate_classifications(
        [
            {
                "joint": joint,
                "channel": values["channel"],
                "direction": values["direction"],
                "human_confirmed": True,
            }
            for joint, values in joints.items()
        ]
    )
    for joint, values in joints.items():
        if "rom" in values:
            validate_rom(joint, values["rom"])
        if "last_position" in values:
            last = values["last_position"]
            pulse = last.get("pulse_us") if isinstance(last, dict) else None
            if type(pulse) is not int or not 500 <= pulse <= 2500 or not last.get("at"):
                raise MapValidationError(f"joint {joint}: last_position needs pulse_us in 500..2500 and at")
    devices = data.get("devices", {})
    if not isinstance(devices, dict):
        raise MapValidationError("devices must be an object")
    for name, values in devices.items():
        if not JOINT_NAME_RE.match(str(name)):
            raise MapValidationError(f"invalid device name {name!r}")
        if not isinstance(values, dict) or values.get("kind") not in DEVICE_KINDS:
            raise MapValidationError(f"device {name}: kind must be one of {', '.join(DEVICE_KINDS)}")
    if data.get("permission_hash") != stable_json_hash(data.get("probe_permissions")):
        raise MapValidationError("permission_hash mismatch")


def validate_rom(joint: str, rom: Any) -> None:
    """Check a ``rom`` (range of motion) block on a joint."""
    if not isinstance(rom, dict):
        raise MapValidationError(f"joint {joint}: rom must be an object")
    values = [rom.get(key) for key in ("rom1_us", "rom2_us", "min_us", "max_us")]
    if any(type(value) is not int or not 500 <= value <= 2500 for value in values):
        raise MapValidationError(f"joint {joint}: rom pulses must be integers in 500..2500 us")
    rom1, rom2, low, high = values
    if rom1 == rom2 or low != min(rom1, rom2) or high != max(rom1, rom2):
        raise MapValidationError(f"joint {joint}: rom min_us/max_us must be min/max of two different marks")
    if not rom.get("set_by") or not rom.get("set_at"):
        raise MapValidationError(f"joint {joint}: rom needs set_by and set_at")


def export_atomic(data: dict[str, Any], path: Path, *, overwrite: bool = False) -> None:
    validate_map(data)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; pass --overwrite to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    validate_map(json.loads(path.read_text(encoding="utf-8")))


def load_map(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_map(data)
    return data
