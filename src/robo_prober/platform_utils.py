"""Small OS-specific helpers for the desktop window."""
from __future__ import annotations

import sys


def enable_hidpi() -> None:
    """Ask Windows for per-monitor DPI awareness so Tk is not blurry. No-op elsewhere."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - purely cosmetic
        pass


def tkinter_fix_hint() -> str:
    """What to install when tkinter is missing, for the current platform."""
    if sys.platform.startswith("win"):
        return ("Reinstall Python from https://www.python.org/downloads/ and keep the "
                "'tcl/tk and IDLE' option ticked.")
    if sys.platform == "darwin":
        return ("Install Python from https://www.python.org/downloads/ (includes Tk), "
                "or with Homebrew: brew install python-tk")
    return ("Install Tk for your Python:\n"
            "  Debian/Ubuntu: sudo apt install python3-tk\n"
            "  Fedora:        sudo dnf install python3-tkinter\n"
            "  Arch:          sudo pacman -S tk\n"
            "  openSUSE:      sudo zypper install python3-tk\n"
            "  Alpine:        sudo apk add py3-tkinter")


def serial_port_hint() -> str:
    """Typical serial port names on this platform."""
    if sys.platform.startswith("win"):
        return "COM3"
    if sys.platform == "darwin":
        return "/dev/cu.usbserial-0001"
    return "/dev/ttyUSB0"
