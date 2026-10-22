from __future__ import annotations

import argparse
import json
import sys
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .catalog import get_profile, list_board_ids
from .discovery import annotate_devices, discover_serial_ports, is_placeholder_port
from .identity import chip_profile_id, identify_esp32_with_consent, matching_boards_for_chip, normalize_chip_name
from .map_schema import export_atomic, load_map
from .permissions import load_permissions
from .session import build_map_from_session, run_bounded_probe
from .transport import PtyReferenceEmulator, SerialProtocolTransport


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def cmd_discover(args: argparse.Namespace) -> int:
    ports = discover_serial_ports()
    if not args.all:
        ports = [port for port in ports if not is_placeholder_port(port)]
    devices = annotate_devices(ports)
    if args.json:
        print_json({"devices": devices})
    else:
        if not devices:
            print("No serial devices found.")
        for index, dev in enumerate(devices, start=1):
            vidpid = "unknown VID:PID"
            if dev["vid"] is not None and dev["pid"] is not None:
                vidpid = f"{dev['vid']:04x}:{dev['pid']:04x}"
            print(f"{index}. {dev['device']} {vidpid} {dev['status']} - {dev['interface']}")
            print("   USB ID note: bridge/interface hint only; exact board must be selected manually.")
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    chip, error = identify_esp32_with_consent(
        args.port,
        consent_reset=args.consent_controller_identification == "YES_RESET_ALLOWED",
        timeout_s=args.timeout,
    )
    if error:
        print_json({"status": "identification_failed", "error": error, "port": args.port})
        return 2
    print_json(
        {
            "status": "chip_identified",
            "port": args.port,
            "chip": chip,
            "chip_profile_id": chip_profile_id(chip),
            "matching_board_profiles": matching_boards_for_chip(chip),
            "warning": "Chip identity is not exact board identity; select the physical board explicitly.",
        }
    )
    return 0


def cmd_boards(args: argparse.Namespace) -> int:
    board_ids = matching_boards_for_chip(args.chip) if args.chip else list_board_ids()
    if args.json:
        print_json({"chip": normalize_chip_name(args.chip), "boards": board_ids})
    else:
        for board_id in board_ids:
            profile = get_profile(board_id)
            print(f"{board_id}: {profile['name']} ({len(profile['pins'])} pin records)")
    return 0


def cmd_validate_permissions(args: argparse.Namespace) -> int:
    permissions = load_permissions(load_json(args.permissions))
    print_json({"status": "valid", "board_profile_id": permissions.board_profile_id, "channels": sorted(permissions.channels)})
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    if args.protocol != "ROBOPROBE/1":
        raise SystemExit("Only explicit protocol ROBOPROBE/1 is supported")
    permissions = load_permissions(load_json(args.permissions))
    channels = args.channels if args.channels else sorted(permissions.channels)
    confirmations = load_json(args.confirmations)
    controller_identity = load_json(args.controller_identity)
    with SerialProtocolTransport(args.port, args.baudrate, args.timeout) as transport:
        events = run_bounded_probe(
            transport=transport,
            permissions=permissions,
            channels=channels,
            arm_token=args.arm_token,
            cutoff_confirmed=args.power_cutoff_confirmed,
        )
    hardware_map = build_map_from_session(
        controller_identity=controller_identity,
        permissions=permissions,
        observation_paths=args.observations,
        confirmations=confirmations,
        probe_events=events,
        warnings=["Physical probing requires manual arming by the operator, known circuit selection, and a power cutoff."],
    )
    export_atomic(hardware_map, args.out, overwrite=args.overwrite)
    print_json({"status": "exported", "out": str(args.out), "joints": sorted(hardware_map["joints"])})
    return 0


def demo_permissions() -> dict[str, Any]:
    return {
        "controller_node_id": "usb:/dev/pts/reference-emulator",
        "board_profile_id": "esp32_devkitc_v4_wroom32",
        "physical_power_cutoff_required": True,
        "notes": "Demo fixture only. These are not physical servo approvals.",
        "channels": [
            {"channel": 0, "route": "pca9685_servo", "neutral_us": 1500, "min_us": 1460, "max_us": 1540, "step_us": 20, "approved_by": "demo-operator", "approved_at": "2026-10-13T00:00:00Z"},
            {"channel": 1, "route": "pca9685_servo", "neutral_us": 1490, "min_us": 1450, "max_us": 1530, "step_us": 20, "approved_by": "demo-operator", "approved_at": "2026-10-13T00:00:00Z"},
            {"channel": 2, "route": "pca9685_servo", "neutral_us": 1510, "min_us": 1470, "max_us": 1550, "step_us": 20, "approved_by": "demo-operator", "approved_at": "2026-10-13T00:00:00Z"},
        ],
    }


def demo_confirmations() -> list[dict[str, Any]]:
    return [
        {"channel": 0, "joint": "base_yaw", "direction": "positive", "invert": False, "human_confirmed": True},
        {"channel": 1, "joint": "shoulder", "direction": "negative", "invert": True, "human_confirmed": True},
        {"channel": 2, "joint": "elbow", "direction": "positive", "invert": False, "human_confirmed": True},
    ]


def _serve_emulator(emulator: PtyReferenceEmulator) -> None:
    while emulator.master_fd is not None:
        try:
            emulator.serve_once(timeout_s=0.1)
        except OSError:
            return


def demo_fixture_path() -> Path:
    """Path to the synthetic observation fixture bundled with the package."""
    return Path(str(resources.files("robo_prober") / "data" / "synthetic_observation.json"))


def cmd_run_demo(args: argparse.Namespace) -> int:
    fixture = args.fixture or demo_fixture_path()
    emulator = PtyReferenceEmulator()
    port = emulator.open()
    thread = threading.Thread(target=_serve_emulator, args=(emulator,), daemon=True)
    thread.start()
    permissions_data = demo_permissions()
    permissions_data["controller_node_id"] = "usb:" + port
    permissions = load_permissions(permissions_data)
    controller_identity = {
        "status": "simulated_reference_protocol",
        "port": port,
        "chip": "ESP32",
        "board_selection": permissions.board_profile_id,
        "physical_compatibility": "not verified",
    }
    try:
        with SerialProtocolTransport(port, timeout_s=1.0) as transport:
            events = run_bounded_probe(
                transport=transport,
                permissions=permissions,
                channels=[0, 1, 2],
                arm_token="demo-arm",
                cutoff_confirmed=True,
            )
    finally:
        emulator.close()
        thread.join(timeout=1.0)
    hardware_map = build_map_from_session(
        controller_identity=controller_identity,
        permissions=permissions,
        observation_paths=[fixture],
        confirmations=demo_confirmations(),
        probe_events=events,
        warnings=[
            "Demo used a PTY reference emulator and synthetic observation fixture.",
            "No physical servo motion, firmware compatibility, or live camera evidence was verified.",
        ],
    )
    export_atomic(hardware_map, args.out, overwrite=args.overwrite)
    reloaded = load_map(args.out)
    print_json({"status": "demo_complete", "out": str(args.out), "reloaded_joints": sorted(reloaded["joints"])})
    return 0


def cmd_validate_map(args: argparse.Namespace) -> int:
    data = load_map(args.map)
    print_json({"status": "valid", "map": str(args.map), "joints": sorted(data["joints"])})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robo-prober",
        description="Fail-closed robot controller discovery, opt-in servo probing, and hardware map export.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="Enumerate serial devices without opening ports")
    discover.add_argument("--json", action="store_true")
    discover.add_argument("--all", action="store_true", help="Include placeholder ports with no USB or device info")
    discover.set_defaults(func=cmd_discover)

    identify = sub.add_parser("identify", help="Run explicit ESP32 chip identification")
    identify.add_argument("--port", required=True)
    identify.add_argument("--consent-controller-identification", required=True, choices=["NO", "YES_RESET_ALLOWED"])
    identify.add_argument("--timeout", type=float, default=25.0)
    identify.set_defaults(func=cmd_identify)

    boards = sub.add_parser("boards", help="List exact board profiles")
    boards.add_argument("--chip")
    boards.add_argument("--json", action="store_true")
    boards.set_defaults(func=cmd_boards)

    validate_permissions = sub.add_parser("validate-permissions", help="Validate a per-channel permission file")
    validate_permissions.add_argument("permissions", type=Path)
    validate_permissions.set_defaults(func=cmd_validate_permissions)

    probe = sub.add_parser("probe", help="Run bounded opt-in probing over ROBOPROBE/1")
    probe.add_argument("--port", required=True)
    probe.add_argument("--protocol", required=True)
    probe.add_argument("--permissions", required=True, type=Path)
    probe.add_argument("--controller-identity", required=True, type=Path)
    probe.add_argument("--confirmations", required=True, type=Path)
    probe.add_argument("--observations", nargs="+", required=True, type=Path)
    probe.add_argument("--out", required=True, type=Path)
    probe.add_argument("--overwrite", action="store_true")
    probe.add_argument("--channels", nargs="*", type=int)
    probe.add_argument("--arm-token", required=True)
    probe.add_argument("--power-cutoff-confirmed", action="store_true")
    probe.add_argument("--baudrate", type=int, default=115200)
    probe.add_argument("--timeout", type=float, default=1.0)
    probe.set_defaults(func=cmd_probe)

    demo = sub.add_parser("run-demo", help="Exercise CLI with a PTY emulator and synthetic observation fixture")
    demo.add_argument("--out", required=True, type=Path)
    demo.add_argument("--overwrite", action="store_true")
    demo.add_argument("--fixture", type=Path)
    demo.set_defaults(func=cmd_run_demo)

    validate_map = sub.add_parser("validate-map", help="Validate and reload an exported hardware map")
    validate_map.add_argument("map", type=Path)
    validate_map.set_defaults(func=cmd_validate_map)

    from .sensing_cli import register
    register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
