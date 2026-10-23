"""Board identity and catalog adapter. Pin numbers are catalog keys."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Board:
    id: str
    model: str
    chip_labels: tuple[str, ...] = ()


def detect_board(model_path="/proc/device-tree/model",
                 tegra_path="/etc/nv_tegra_release") -> Board:
    try:
        model = Path(model_path).read_text().strip("\0\n ")
    except OSError:
        model = "Unknown Linux SBC"
    lower = model.lower()
    if "raspberry pi 5" in lower:
        return Board("raspberry_pi_5", model, ("pinctrl-rp1",))
    if "raspberry pi 4" in lower:
        return Board("raspberry_pi_4", model, ("pinctrl-bcm2711",))
    if "raspberry pi zero 2" in lower:
        return Board("raspberry_pi_zero_2w", model, ("pinctrl-bcm2835", "pinctrl-bcm2837"))
    for name, profile in (("orin nano", "jetson_orin_nano_devkit"),
                          ("orin nx", "jetson_orin_nx_devkit"),
                          ("agx orin", "jetson_agx_orin_devkit")):
        if name in lower:
            return Board(profile, model, ("tegra234-gpio",))
    if "orin" in lower:
        return Board("jetson_orin_unknown", model, ("tegra234-gpio",))
    if Path(tegra_path).exists():
        # A Tegra release file alone does not identify an Orin carrier/header.
        return Board("jetson_unknown", model, ("tegra234-gpio", "tegra-gpio"))
    return Board("linux_unknown", model)


def get_profile(board_id):
    """Use lane C when installed, otherwise the public catalog API."""
    from ..catalog import get_profile as catalog_profile
    try:
        return catalog_profile(board_id)
    except KeyError:
        try:
            from ..catalog_sbc import get_profile as sbc_profile
        except ModuleNotFoundError as error:
            if error.name == "robo_prober.catalog_sbc":
                raise KeyError(board_id) from None
            raise
        return sbc_profile(board_id)


def pin_map(profile, backend):
    """Return (known pin IDs, usable ID -> line offsets).

    Catalog safe_* flags are intentionally false and are not runtime grants.
    Exposed, nonreserved GPIO records define candidates; ARM and survey grant
    runtime access. Unknown profiles use unclaimed lines on the selected chip.
    Unresolved line names/offsets in a known profile remain reserved.
    """
    lines = set(backend.lines())
    if profile is None:
        # Ownership is live state: BOARD and every operation filter it afresh.
        return lines, {p: p for p in lines}
    known, usable = set(), {}
    for key, record in profile.get("pins", {}).items():
        try:
            pin = int(key)
        except (TypeError, ValueError):
            continue
        known.add(pin)
        if (record.get("reserved", True) or record.get("exposed") is not True
                or record.get("type") != "gpio" or record.get("usable") is False):
            continue
        offset = backend.resolve_pin(record)
        if offset is not None and offset in lines:
            usable[pin] = offset
    return known, usable
