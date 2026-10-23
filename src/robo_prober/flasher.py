"""Back up a microcontroller, install the robo-prober test firmware, restore it.

robo-prober never relies on whatever firmware is already on a board. On a
serial ESP32 board the flow is:

1. HELLO: if the board already runs the current robo-prober firmware (same
   fw version and every required capability), nothing is flashed.
2. identify: chip type, MAC and flash size, via esptool.
3. backup: the ENTIRE flash, read to ~/robo_prober_backups/<chip>-<mac>-<time>.bin
   with a .json beside it. Never overwrites; a wrong-size read aborts.
4. install: the prebuilt merged image for that chip, written at 0x0 and
   checked (MD5 of the flash against the image), then a hard reset.
5. verify: HELLO must answer proto=ROBOPROBE/1 with the expected version.

restore() writes a backup back, only to the board whose MAC it came from.
Raspberry Pi / Jetson boards run ``python -m robo_prober.sbc`` and are never
flashed.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "ROBOPROBE/1"
FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"
BACKUP_DIR = Path.home() / "robo_prober_backups"
FAST_BAUD = 921600
SBC_ID_PREFIXES = ("raspberry_pi", "jetson", "linux_")
SBC_NOTE = ("This is a Raspberry Pi / Jetson board: nothing is flashed. "
            "It runs the agent itself: python -m robo_prober.sbc")
INSTALL_HINT = "install the robo-prober test firmware (python robo_prober.py firmware install)"

# esptool's CHIP_NAME -> our image name
CHIP_KEYS = {"ESP32": "esp32", "ESP32-S2": "esp32s2", "ESP32-S3": "esp32s3",
             "ESP32-C3": "esp32c3", "ESP32-C6": "esp32c6"}

Progress = Callable[[str, int, int], None]


class FlashError(RuntimeError):
    pass


def _noop(stage: str, done: int, total: int) -> None:
    pass


# ------------------------------------------------------------------ manifest
def load_manifest(firmware_dir: Path = FIRMWARE_DIR) -> dict[str, Any]:
    path = firmware_dir / "manifest.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise FlashError(f"prebuilt firmware manifest missing or unreadable: {path} ({error})") from error


def firmware_image(chip: str, firmware_dir: Path = FIRMWARE_DIR,
                   manifest: dict[str, Any] | None = None) -> bytes:
    """The merged image for ``chip``, checked against the manifest's size and sha256."""
    manifest = manifest or load_manifest(firmware_dir)
    entry = manifest.get("images", {}).get(chip)
    if entry is None:
        raise FlashError(f"no robo-prober firmware for chip {chip!r} "
                         f"(have: {', '.join(sorted(manifest.get('images', {})))})")
    data = (firmware_dir / entry["file"]).read_bytes()
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise FlashError(f"firmware image {entry['file']} does not match its manifest (corrupt install?)")
    return data


def required_caps(manifest: dict[str, Any] | None = None) -> set[str]:
    return set((manifest or load_manifest()).get("caps", []))


# ------------------------------------------------------------- HELLO check
@dataclass
class FirmwareStatus:
    state: str  # current | outdated | foreign | sbc
    message: str
    info: dict[str, str] = field(default_factory=dict)
    missing_caps: list[str] = field(default_factory=list)

    @property
    def current(self) -> bool:
        return self.state in ("current", "sbc")

    @property
    def needs_install(self) -> bool:
        """Only when the app can't work with what's on the board: foreign firmware, or missing caps."""
        return self.state == "foreign" or (self.state == "outdated" and bool(self.missing_caps))

    @property
    def update_available(self) -> bool:
        return self.state == "outdated" and not self.missing_caps


def classify_hello(info: dict[str, str] | None, manifest: dict[str, Any] | None = None) -> FirmwareStatus:
    """Decide from a HELLO reply (or None: no answer) whether to install."""
    manifest = manifest or load_manifest()
    want = manifest["fw_version"]
    if not info or info.get("proto") != PROTOCOL:
        return FirmwareStatus("foreign", "This board isn't running robo-prober.", dict(info or {}))
    if str(info.get("id", "")).startswith(SBC_ID_PREFIXES):
        return FirmwareStatus("sbc", SBC_NOTE, dict(info))
    have = info.get("fw", "?")
    caps = set(filter(None, info.get("caps", "").split(",")))
    missing = sorted(required_caps(manifest) - caps)
    if have == want and not missing:
        return FirmwareStatus("current", f"Board already runs robo-prober firmware {have}; nothing to install.",
                              dict(info))
    why = f"firmware {have}" + (f" lacks {', '.join(missing)}" if missing else f", not {want}")
    return FirmwareStatus("outdated", f"This board runs older robo-prober firmware ({why}).", dict(info), missing)


BOOT_WAIT_S = 8.0  # opening the port resets most ESP32 boards; boot + pin survey takes a few seconds


def _serial_hello(port: str, timeout_s: float) -> dict[str, str]:
    """Open the port ONCE and keep saying HELLO until the board has booted.

    Reopening per attempt would reset the board each time, so it would never
    get far enough to answer.
    """
    import serial

    from .transport import parse_response

    deadline = time.monotonic() + max(timeout_s, BOOT_WAIT_S)
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, 115200, 0.3
    ser.dtr = ser.rts = False  # don't hold the board in reset / download mode while we listen
    reset_at = time.monotonic() + 3.0
    with ser:
        buffer = b""
        while time.monotonic() < deadline:
            if reset_at and time.monotonic() > reset_at:
                # Silent for 3 s: probably parked in the bootloader (e.g. after esptool).
                # Pulse EN through RTS once, the same way esptool's hard reset does.
                reset_at = 0
                ser.rts = True
                time.sleep(0.1)
                ser.rts = False
            ser.write(b"HELLO\n")
            end = time.monotonic() + 0.6
            while time.monotonic() < end:
                buffer += ser.read(256)
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("ascii", errors="replace").strip()
                    if line.startswith("RP1 "):
                        try:
                            status, fields = parse_response(line)
                        except Exception:  # noqa: BLE001 - boot noise
                            continue
                        if status == "OK" and "proto" in fields:
                            return fields
    raise TimeoutError("no ROBOPROBE answer")


def check_firmware(port: str, *, attempts: int = 1, timeout_s: float = BOOT_WAIT_S,
                   hello_fn: Callable[[str, float], dict[str, str]] = _serial_hello,
                   manifest: dict[str, Any] | None = None) -> FirmwareStatus:
    """Say HELLO (read-only; nothing is armed). Retries cover the boot after a port-open reset."""
    info = None
    for _ in range(max(1, attempts)):
        try:
            info = hello_fn(port, timeout_s)
            break
        except Exception:  # noqa: BLE001 - any failure means "not our firmware (yet)"
            time.sleep(0.3)
    return classify_hello(info, manifest)


# --------------------------------------------------------------- esptool
class EsptoolSession:
    """One esptool connection (stub loaded, fast baud, flash attached)."""

    def __init__(self, port: str, baud: int = FAST_BAUD):
        import esptool.cmds as cmds

        self._cmds = cmds
        self.esp = cmds.detect_chip(port, 115200)
        try:
            self.esp = cmds.run_stub(self.esp)
            if baud != 115200:
                with contextlib.suppress(Exception):  # native USB ports ignore the baud rate
                    self.esp.change_baud(baud)
            cmds.attach_flash(self.esp)
        except Exception:
            self.close()
            raise

    @property
    def chip_name(self) -> str:
        return self.esp.CHIP_NAME

    def mac(self) -> str:
        return ":".join(f"{b:02x}" for b in self.esp.read_mac("BASE_MAC"))

    def flash_size(self) -> int:
        from esptool.util import flash_size_bytes

        text = self._cmds.detect_flash_size(self.esp)
        if not text:
            raise FlashError("could not detect the flash size")
        return flash_size_bytes(text)

    def read(self, size: int, progress: Callable[[int, int], None]) -> bytes:
        return self.esp.read_flash(0, size, progress_fn=progress)

    def write(self, data: bytes, progress: Callable[[int, int], None]) -> None:
        from esptool.logger import log

        original = log.progress_bar
        log.progress_bar = lambda cur, total, *a, **k: progress(cur, total)
        try:
            self._cmds.write_flash(self.esp, [(0, data)], flash_freq="keep", flash_mode="keep", flash_size="keep")
        finally:
            log.progress_bar = original

    def md5(self, size: int) -> str:
        digest = self.esp.flash_md5sum(0, size)
        return digest.hex() if isinstance(digest, bytes) else str(digest).lower()

    def hard_reset(self) -> None:
        self._cmds.reset_chip(self.esp, "hard-reset")

    def close(self) -> None:
        # Always leave the board running its firmware, never parked in the bootloader.
        with contextlib.suppress(Exception):
            self.esp.hard_reset()
        with contextlib.suppress(Exception):
            self.esp._port.close()


def _open_session(port: str, baud: int = FAST_BAUD) -> EsptoolSession:
    try:
        return EsptoolSession(port, baud)
    except ImportError as error:
        raise FlashError("esptool is not installed: pip install 'robo-prober[identify]'") from error


@dataclass
class ChipInfo:
    chip: str  # esptool image name, e.g. esp32s3
    name: str  # e.g. ESP32-S3
    mac: str
    flash_size: int


def _identify(session) -> ChipInfo:
    name = session.chip_name
    chip = CHIP_KEYS.get(name.split(" ")[0].upper())
    if chip is None:
        raise FlashError(f"unsupported chip {name!r}; robo-prober has firmware for {', '.join(CHIP_KEYS)}")
    return ChipInfo(chip, name, session.mac(), session.flash_size())


def identify(port: str, *, open_session=_open_session) -> ChipInfo:
    session = open_session(port)
    try:
        return _identify(session)
    finally:
        session.close()


# ------------------------------------------------------------------ backup
def backup_name(info: ChipInfo, when: _dt.datetime) -> str:
    return f"{info.chip}-{info.mac.replace(':', '').lower()}-{when:%Y%m%d-%H%M%S}"


def _exclusive_path(out_dir: Path, stem: str) -> Path:
    """A new .bin path that does not exist yet (never overwrite an earlier backup)."""
    for n in range(1000):
        path = out_dir / (f"{stem}.bin" if n == 0 else f"{stem}-{n}.bin")
        if not path.exists() and not path.with_suffix(".json").exists():
            return path
    raise FlashError(f"too many backups named {stem} in {out_dir}")


def _backup_with(session, out_dir: Path, progress: Progress, now: _dt.datetime | None) -> tuple[Path, ChipInfo]:
    info = _identify(session)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _exclusive_path(out_dir, backup_name(info, now or _dt.datetime.now()))
    progress(f"Backing up {info.flash_size // 1024} KB of flash to {path}", 0, info.flash_size)
    data = session.read(info.flash_size, lambda done, total: progress("Backing up", done, total))
    if data is None or len(data) != info.flash_size:
        got = 0 if data is None else len(data)
        raise FlashError(f"backup read {got} bytes, expected {info.flash_size}; nothing was flashed")
    with open(path, "xb") as handle:  # "x": never overwrite
        handle.write(data)
    meta = {"chip": info.chip, "chip_name": info.name, "mac": info.mac, "flash_size": info.flash_size,
            "sha256": hashlib.sha256(data).hexdigest(), "date": (now or _dt.datetime.now()).isoformat(),
            "file": path.name}
    with open(path.with_suffix(".json"), "x") as handle:
        json.dump(meta, handle, indent=2)
    if path.stat().st_size != info.flash_size:
        raise FlashError(f"backup file {path} has the wrong size; nothing was flashed")
    return path, info


def backup(port: str, out_dir: Path = BACKUP_DIR, *, progress: Progress = _noop,
           open_session=_open_session, now: _dt.datetime | None = None) -> Path:
    session = open_session(port)
    try:
        return _backup_with(session, Path(out_dir), progress, now)[0]
    finally:
        session.close()


def backups_for(mac: str, out_dir: Path = BACKUP_DIR) -> list[Path]:
    """Backups of the board with this MAC, newest first."""
    key = mac.replace(":", "").lower()
    found = []
    for meta_path in Path(out_dir).glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if str(meta.get("mac", "")).replace(":", "").lower() == key and meta_path.with_suffix(".bin").exists():
            found.append((meta.get("date", ""), meta_path.with_suffix(".bin")))
    return [path for _, path in sorted(found, reverse=True)]


def newest_backup(mac: str | None = None, out_dir: Path = BACKUP_DIR) -> Path | None:
    if mac:
        paths = backups_for(mac, out_dir)
        return paths[0] if paths else None
    paths = sorted(Path(out_dir).glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


# ------------------------------------------------------------------- flash
def _write_verified(session, data: bytes, progress: Progress, what: str) -> None:
    progress(f"Writing {what}", 0, len(data))
    session.write(data, lambda done, total: progress(f"Writing {what}", done, total))
    progress(f"Verifying {what}", len(data), len(data))
    if session.md5(len(data)) != hashlib.md5(data).hexdigest():
        raise FlashError(f"verify failed: the flash does not match {what}")
    session.hard_reset()


def flash_test_firmware(port: str, chip: str | None = None, *, progress: Progress = _noop,
                        open_session=_open_session, firmware_dir: Path = FIRMWARE_DIR) -> ChipInfo:
    session = open_session(port)
    try:
        info = _identify(session)
        if chip is not None and chip != info.chip:
            raise FlashError(f"board is {info.name}, not {chip}; nothing was flashed")
        _flash_with(session, info, progress, firmware_dir)
        return info
    finally:
        session.close()


def _flash_with(session, info: ChipInfo, progress: Progress, firmware_dir: Path) -> None:
    data = firmware_image(info.chip, firmware_dir)
    if len(data) > info.flash_size:
        raise FlashError("firmware image is larger than the board's flash")
    _write_verified(session, data, progress, "robo-prober test firmware")


def verify_test_firmware(port: str, *, wait_s: float = 12.0, check=check_firmware,
                         manifest: dict[str, Any] | None = None) -> FirmwareStatus:
    """After a reset, wait for HELLO to answer with the expected version and caps."""
    deadline = time.monotonic() + wait_s
    status = FirmwareStatus("foreign", "no answer")
    while True:
        try:
            status = check(port, manifest=manifest)
        except Exception as error:  # noqa: BLE001 - port may re-enumerate after reset
            status = FirmwareStatus("foreign", str(error))
        if status.state == "current" or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if status.state != "current":
        raise FlashError(f"installed, but the board did not answer HELLO as robo-prober "
                         f"{(manifest or load_manifest())['fw_version']}: {status.message}")
    return status


@dataclass
class InstallResult:
    skipped: bool
    message: str
    backup: Path | None = None
    chip: ChipInfo | None = None
    status: FirmwareStatus | None = None


def install(port: str, *, out_dir: Path = BACKUP_DIR, progress: Progress = _noop, force: bool = False,
            open_session=_open_session, check=check_firmware, firmware_dir: Path = FIRMWARE_DIR,
            verify_wait_s: float = 12.0, backup: bool = True) -> InstallResult:
    """HELLO; if not current: identify, back up all flash, flash, verify. Refuses on a failed backup."""
    manifest = load_manifest(firmware_dir)
    status = check(port, manifest=manifest)
    if status.state == "sbc":
        return InstallResult(True, SBC_NOTE, status=status)
    if status.state == "current" and not force:
        return InstallResult(True, status.message, status=status)
    progress("Identifying the chip", 0, 1)
    session = open_session(port)
    try:
        if backup:
            path, info = _backup_with(session, Path(out_dir), progress, None)
        else:  # the operator chose to skip it
            path, info = None, _identify(session)
        _flash_with(session, info, progress, firmware_dir)
    finally:
        session.close()
    progress("Waiting for the board to answer HELLO", 0, 1)
    final = verify_test_firmware(port, wait_s=verify_wait_s, check=check, manifest=manifest)
    return InstallResult(False, f"Installed robo-prober firmware {manifest['fw_version']} on {info.name} "
                                f"({info.mac}). " + (f"Original firmware backed up to {path}." if path else "No backup was made (skipped)."),
                         path, info, final)


def read_backup_meta(backup_path: Path) -> dict[str, Any]:
    meta_path = Path(backup_path).with_suffix(".json")
    try:
        return json.loads(meta_path.read_text())
    except (OSError, ValueError) as error:
        raise FlashError(f"backup info {meta_path} is missing or unreadable; refusing to restore") from error


def restore(port: str, backup_path: Path, *, progress: Progress = _noop, open_session=_open_session) -> ChipInfo:
    """Write a backup back to the board it came from (MAC must match), then verify."""
    backup_path = Path(backup_path)
    meta = read_backup_meta(backup_path)
    data = backup_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
        raise FlashError(f"{backup_path} does not match its sha256; refusing to restore")
    session = open_session(port)
    try:
        info = _identify(session)
        want = str(meta.get("mac", "")).replace(":", "").lower()
        if info.mac.replace(":", "").lower() != want:
            raise FlashError(f"this board's MAC is {info.mac}, the backup came from {meta.get('mac')}; "
                             "refusing to restore it to a different board")
        if len(data) != info.flash_size:
            raise FlashError(f"backup is {len(data)} bytes but the flash is {info.flash_size}; refusing")
        _write_verified(session, data, progress, "original firmware")
        return info
    finally:
        session.close()
