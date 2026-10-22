"""Fail-closed robot controller discovery, probing, range-of-motion and export tools.

The package defaults to discovery-only behavior. Motion commands require an
explicit protocol, per-channel permissions or limits, and manual arming.

Public API (see ``robo_prober.api`` for details)::

    discover, connect, Controller, load_map, save_map,
    RomChannel, RomSession, rom_range, get_rom, set_rom, set_last_positions, load_markers
"""

__version__ = "0.2.0"

from .api import Controller, connect, discover, load_map, save_map  # noqa: E402
from .markers import Markers, load_markers  # noqa: E402
from .rom import RomChannel, RomError, RomSession, get_rom, rom_range, set_last_positions, set_rom  # noqa: E402

__all__ = [
    "__version__",
    "Controller",
    "Markers",
    "RomChannel",
    "RomError",
    "RomSession",
    "connect",
    "discover",
    "get_rom",
    "load_map",
    "load_markers",
    "rom_range",
    "save_map",
    "set_last_positions",
    "set_rom",
]
