from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import contextmanager
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
from .transport import (
    InProcessEmulatorConnection,
    PtyReferenceEmulator,
    SerialProtocolTransport,
    pty_available,
    serve_forever,
)


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
            slew_us_s=args.slew_us_s,
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


@contextmanager
def emulator_transport(timeout_s: float = 1.0):
    """Open a transport to the built-in ROBOPROBE/1 emulator.

    Uses a real pseudo terminal on Linux and macOS (exercising the serial
    stack) and an in-process connection on Windows.
    """
    if pty_available():
        emulator = PtyReferenceEmulator()
        port = emulator.open()
        thread = threading.Thread(target=serve_forever, args=(emulator,), daemon=True)
        thread.start()
        try:
            with SerialProtocolTransport(port, timeout_s=timeout_s) as transport:
                yield transport, port
        finally:
            emulator.close()
            thread.join(timeout=1.0)
    else:
        port = "emulator://in-process"
        with SerialProtocolTransport(port, timeout_s=timeout_s, connection=InProcessEmulatorConnection()) as transport:
            yield transport, port


def demo_fixture_path() -> Path:
    """Path to the synthetic observation fixture bundled with the package."""
    return Path(str(resources.files("robo_prober") / "data" / "synthetic_observation.json"))


def cmd_run_demo(args: argparse.Namespace) -> int:
    fixture = args.fixture or demo_fixture_path()
    with emulator_transport() as (transport, port):
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
        events = run_bounded_probe(
            transport=transport,
            permissions=permissions,
            channels=[0, 1, 2],
            arm_token="demo-arm",
            cutoff_confirmed=True,
            slew_us_s=1000,
        )
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


def cmd_rom(args: argparse.Namespace) -> int:
    """Record a range of motion measured elsewhere; optionally push it to the firmware."""
    from .api import connect
    from .rom import get_rom, rom_range, set_rom

    if args.port and args.emulator:
        raise SystemExit("pass at most one of --port or --emulator")
    hardware_map = load_map(args.map)
    out = args.out or args.map
    if out != args.map and out.exists() and not args.overwrite:
        raise FileExistsError(f"{out} exists; pass --overwrite to replace it")
    low, high = rom_range(args.rom1, args.rom2)
    updated = set_rom(hardware_map, args.channel, args.rom1, args.rom2, set_by=args.operator)
    pushed = None
    if args.port or args.emulator:
        joint = next(values for values in updated["joints"].values() if int(values["channel"]) == args.channel)
        neutral = min(max(int(joint["neutral_us"]), low), high)
        with connect(args.port, emulator=args.emulator) as controller:
            pushed = controller.set_limits(args.channel, low, high, neutral_us=neutral, slew_us_s=args.slew_us_s)
    export_atomic(updated, out, overwrite=True)
    print_json({"status": "rom_saved", "out": str(out), "channel": args.channel, "rom": get_rom(updated, args.channel),
                "firmware_limit": pushed})
    return 0


def cmd_rom_gui(args: argparse.Namespace) -> int:
    try:
        import tkinter
    except ImportError:
        from .platform_utils import tkinter_fix_hint

        print("tkinter is not available, so the window cannot open.\n" + tkinter_fix_hint(), file=sys.stderr)
        print("The scripting path still works: robo-prober rom --map ... --channel N --rom1 US --rom2 US",
              file=sys.stderr)
        return 3
    from .rom_gui import run

    try:
        return run(map_path=args.map, out_path=args.out, port="emulator" if args.emulator else args.port,
                   channels=args.channels, operator=args.operator or "", band_half_us=args.band_us,
                   host_slew_us_s=args.slew_us_s)
    except tkinter.TclError as exc:
        print(f"Could not open a window: {exc}\nRun this from a desktop session, or script it with "
              "'robo-prober rom'.", file=sys.stderr)
        return 3


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what this installation can do."""
    import importlib.util
    import platform

    print(f"robo-prober {__version__} on Python {platform.python_version()} ({sys.platform})")
    ok = True
    for module, why, required in (("serial", "serial ports", True), ("PIL", "window theme", True),
                                  ("tkinter", "range-of-motion window", False),
                                  ("esptool", "identify", False), ("cv2", "vision commands", False),
                                  ("trimesh", "model commands", False)):
        found = importlib.util.find_spec(module) is not None
        ok = ok and (found or not required)
        print(f"  {'ok' if found else '--'}  {module:<8} {why}{'' if found or not required else ' (REQUIRED)'}")
    try:
        ports = [port.device for port in discover_serial_ports() if not is_placeholder_port(port)]
        print(f"  ok  serial ports: {', '.join(ports) if ports else 'none found'}")
    except Exception as error:  # noqa: BLE001
        print(f"  --  serial port listing failed: {error}")
    return 0 if ok else 1


def cmd_estop(args: argparse.Namespace) -> int:
    with SerialProtocolTransport(args.port, args.baudrate, args.timeout) as transport:
        if args.clear:
            if not args.power_cutoff_confirmed:
                raise SystemExit("--clear requires --power-cutoff-confirmed")
            transport.estop_clear(cutoff_confirmed=True)
            print_json({"status": "estop_cleared", "port": args.port, "armed": False})
        else:
            transport.estop()
            print_json({"status": "estop_latched", "port": args.port})
    return 0


def _firmware_port(args: argparse.Namespace) -> str:
    if args.port:
        return args.port
    found = [port for port in discover_serial_ports() if not is_placeholder_port(port)]
    ports = [row["device"] for row in annotate_devices(found)]
    if len(ports) != 1:
        raise SystemExit("pick a port with --port (found: " + (", ".join(ports) or "none") + ")")
    return ports[0]


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(question + " [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


def _cli_progress():
    state = {"stage": None}

    def progress(stage: str, done: int, total: int) -> None:
        if stage != state["stage"]:
            if state["stage"] is not None:
                print(file=sys.stderr)
            state["stage"] = stage
        pct = int(100 * done / total) if total else 0
        print(f"\r{stage}: {pct}%", end="", file=sys.stderr, flush=True)

    return progress


def cmd_firmware(args: argparse.Namespace) -> int:
    from . import flasher

    port = _firmware_port(args)
    progress = _cli_progress()
    out_dir = Path(args.backup_dir) if args.backup_dir else flasher.BACKUP_DIR
    if args.action == "backup":
        if not _confirm(f"Read the whole flash of the board on {port} into {out_dir}? "
                        "It resets the board; nothing is written to it.", args.yes):
            print("Nothing done.")
            return 1
        path = flasher.backup(port, out_dir, progress=progress)
        print(f"\nBacked up to {path}")
        return 0
    if args.action == "install":
        status = flasher.check_firmware(port)
        if status.state == "sbc" or (status.state == "current" and not args.force):
            print(status.message)
            return 0
        print(status.message)
        if not _confirm(f"Back up everything on it to {out_dir} and install the robo-prober test firmware? "
                        "Unplug servo power first.", args.yes):
            print("Nothing done.")
            return 1
        # Already checked above, so install does not skip; it still refuses on a failed backup.
        result = flasher.install(port, out_dir=out_dir, progress=progress, force=True)
        print("\n" + result.message)
        return 0
    # restore
    backup_path = Path(args.backup) if args.backup else None
    if backup_path is None:
        info = flasher.identify(port)
        backup_path = flasher.newest_backup(info.mac, out_dir)
        if backup_path is None:
            raise SystemExit(f"no backup for {info.mac} in {out_dir}; pass --backup FILE")
    if not _confirm(f"Write {backup_path} back to the board on {port}? This replaces the robo-prober "
                    "firmware with the original. Unplug servo power first.", args.yes):
        print("Nothing done.")
        return 1
    info = flasher.restore(port, backup_path, progress=progress)
    print(f"\nRestored {backup_path.name} to {info.name} ({info.mac}).")
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
    probe.add_argument("--slew-us-s", type=int, default=250, help="Firmware slew limit for probed channels (v0.2 firmware)")
    probe.set_defaults(func=cmd_probe)

    rom = sub.add_parser(
        "rom",
        help="Record a channel's range of motion (two marks, any order) in a hardware map",
        description="Stores [min(rom1, rom2), max(rom1, rom2)] on the channel's joint. With --port or --emulator "
        "the range is also sent to the firmware as that channel's band (no motion, no arming).",
    )
    rom.add_argument("--map", required=True, type=Path)
    rom.add_argument("--out", type=Path, help="Write here instead of updating --map in place")
    rom.add_argument("--overwrite", action="store_true")
    rom.add_argument("--channel", required=True, type=int)
    rom.add_argument("--rom1", required=True, type=int, help="First mark, us")
    rom.add_argument("--rom2", required=True, type=int, help="Second mark, us")
    rom.add_argument("--operator", required=True, help="Name recorded as set_by")
    rom.add_argument("--port", help="Also push the range to this controller (COM3, /dev/ttyUSB0, ...)")
    rom.add_argument("--emulator", action="store_true", help="Push to the built-in emulator instead")
    rom.add_argument("--slew-us-s", type=int, default=250)
    rom.set_defaults(func=cmd_rom)

    rom_gui = sub.add_parser("rom-gui", help="Open the range-of-motion window (sliders, Set ROM 1 / Set ROM 2, E-STOP)")
    rom_gui.add_argument("--map", type=Path, help="Hardware map whose channels to show and save into")
    rom_gui.add_argument("--out", type=Path, help="Save here instead of updating --map in place")
    rom_gui.add_argument("--port", help="Preselect a serial port")
    rom_gui.add_argument("--emulator", action="store_true", help="Preselect the built-in emulator")
    rom_gui.add_argument("--channels", nargs="*", type=int, help="Only these channels")
    rom_gui.add_argument("--operator", help="Name recorded as set_by")
    rom_gui.add_argument("--band-us", type=int, default=300, help="Start band half-width before ROM is set (default 300)")
    rom_gui.add_argument("--slew-us-s", type=int, default=200, help="Host slew limit for slider motion (default 200)")
    rom_gui.set_defaults(func=cmd_rom_gui)

    doctor = sub.add_parser("doctor", help="Check Python, optional extras, Tk and serial ports")
    doctor.set_defaults(func=cmd_doctor)

    estop = sub.add_parser("estop", help="Latch the controller e-stop (releases all outputs), or clear it")
    estop.add_argument("--port", required=True)
    estop.add_argument("--clear", action="store_true", help="Clear a latched e-stop (does not arm)")
    estop.add_argument("--power-cutoff-confirmed", action="store_true")
    estop.add_argument("--baudrate", type=int, default=115200)
    estop.add_argument("--timeout", type=float, default=1.0)
    estop.set_defaults(func=cmd_estop)

    demo = sub.add_parser("run-demo", help="Exercise CLI with a PTY emulator and synthetic observation fixture")
    demo.add_argument("--out", required=True, type=Path)
    demo.add_argument("--overwrite", action="store_true")
    demo.add_argument("--fixture", type=Path)
    demo.set_defaults(func=cmd_run_demo)

    validate_map = sub.add_parser("validate-map", help="Validate and reload an exported hardware map")
    validate_map.add_argument("map", type=Path)
    validate_map.set_defaults(func=cmd_validate_map)

    firmware = sub.add_parser("firmware", help="Back up the board, install the robo-prober test firmware, or restore")
    firmware.add_argument("action", choices=["backup", "install", "restore"])
    firmware.add_argument("--port", help="Serial port (default: the only one connected)")
    firmware.add_argument("--backup", help="restore: backup .bin to write (default: newest for this board's MAC)")
    firmware.add_argument("--backup-dir", help="Where backups live (default: ~/robo_prober_backups)")
    firmware.add_argument("--force", action="store_true", help="install even if the current firmware answers")
    firmware.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")
    firmware.set_defaults(func=cmd_firmware)

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
