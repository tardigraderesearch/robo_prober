"""Build the prebuilt robo-prober test firmware (maintainers only).

    python -m robo_prober.firmware_build [--pio PATH]

Builds every ESP32 environment in firmware/esp32_robo_prober with PlatformIO,
reads each env's flash images and offsets from ``pio project metadata`` (so
the bootloader offset comes from the build: 0x1000 on ESP32/S2, 0x0 on
S3/C3/C6), merges them with esptool merge_bin into one image that is written
at 0x0, and writes src/robo_prober/firmware/<chip>.bin plus manifest.json.
Users never need PlatformIO: the images ship as package data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
OUT_DIR = PACKAGE_DIR / "firmware"
REPO = PACKAGE_DIR.parent.parent
FW_DIR = REPO / "firmware" / "esp32_robo_prober"
DEFAULT_PIO = Path.home() / ".platformio" / "penv" / "bin" / "pio"

# PlatformIO env -> esptool chip name
ENV_CHIPS = {
    "esp32dev": "esp32",
    "esp32-s2-saola-1": "esp32s2",
    "esp32-s3-devkitc-1": "esp32s3",
    "esp32-c3-devkitm-1": "esp32c3",
    "esp32-c6-devkitc-1": "esp32c6",
}
FLASH_MODES = {0: "qio", 1: "qout", 2: "dio", 3: "dout"}
DEFAULT_APP_OFFSET = 0x10000


def firmware_version(fw_dir: Path = FW_DIR) -> str:
    text = (fw_dir / "src" / "rp_core.h").read_text()
    match = re.search(r'kFirmwareVersion\s*=\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError("kFirmwareVersion not found in rp_core.h")
    return match.group(1)


def firmware_caps(fw_dir: Path = FW_DIR) -> list[str]:
    text = (fw_dir / "src" / "rp_core.cpp").read_text()
    match = re.search(r'kCaps\s*=\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError("kCaps not found in rp_core.cpp")
    return match.group(1).split(",")


def flash_params(chip: str, bootloader: Path) -> dict[str, str]:
    """Flash mode/freq/size as the build wrote them into the bootloader header."""
    from esptool.bin_image import LoadFirmwareImage
    from esptool.targets import CHIP_DEFS

    image = LoadFirmwareImage(chip, str(bootloader))
    rom = CHIP_DEFS[chip]
    size_code = image.flash_size_freq & 0xF0
    freq_code = image.flash_size_freq & 0x0F
    size = next((k for k, v in rom.FLASH_SIZES.items() if v == size_code), hex(size_code))
    freq = next((k for k, v in rom.FLASH_FREQUENCY.items() if v == freq_code), hex(freq_code))
    return {"flash_mode": FLASH_MODES.get(image.flash_mode, str(image.flash_mode)),
            "flash_freq": freq, "flash_size": size}


def build_env(pio: Path, env: str) -> None:
    subprocess.run([str(pio), "run", "-e", env], cwd=FW_DIR, check=True)


def flash_images(pio: Path, env: str) -> list[tuple[int, str]]:
    out = subprocess.run([str(pio), "project", "metadata", "-e", env, "--json-output"],
                         cwd=FW_DIR, check=True, capture_output=True, text=True).stdout
    data = json.loads(out[out.index("{"):])
    extra = data[env]["extra"]
    images = [(int(item["offset"], 16), item["path"]) for item in extra["flash_images"]]
    app_offset = extra.get("application_offset")
    app_offset = int(app_offset, 16) if isinstance(app_offset, str) else DEFAULT_APP_OFFSET
    images.append((app_offset, str(FW_DIR / ".pio" / "build" / env / "firmware.bin")))
    return sorted(images)


def build_all(pio: Path = DEFAULT_PIO, out_dir: Path = OUT_DIR) -> dict:
    import esptool.cmds

    out_dir.mkdir(parents=True, exist_ok=True)
    version = firmware_version()
    manifest = {"fw_version": version, "proto": "ROBOPROBE/1", "caps": firmware_caps(),
                "write_offset": "0x0", "images": {}}
    for env, chip in ENV_CHIPS.items():
        build_env(pio, env)
        images = flash_images(pio, env)
        bootloader = next(path for _, path in images if path.endswith("bootloader.bin"))
        params = flash_params(chip, Path(bootloader))
        target = out_dir / f"{chip}.bin"
        esptool.cmds.merge_bin(images, chip, output=str(target))
        data = target.read_bytes()
        manifest["images"][chip] = {
            "chip": chip, "env": env, "file": target.name, "fw_version": version,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "bootloader_offset": hex(min(offset for offset, _ in images)),
            "offsets": {Path(path).name: hex(offset) for offset, path in images},
            **params,
        }
        print(f"{chip}: {target} ({len(data)} bytes)")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pio", type=Path, default=DEFAULT_PIO)
    args = parser.parse_args(argv)
    build_all(args.pio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
