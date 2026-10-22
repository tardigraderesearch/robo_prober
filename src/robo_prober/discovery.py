from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .catalog import match_usb


@dataclass(frozen=True)
class SerialDevice:
    device: str
    vid: int | None
    pid: int | None
    description: str
    serial_number: str | None = None
    location: str | None = None
    manufacturer: str | None = None
    product: str | None = None

    @property
    def node_id(self) -> str:
        return "usb:" + self.device


def discover_serial_ports() -> list[SerialDevice]:
    """Enumerate serial ports without opening them."""
    try:
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover - exercised through CLI error path
        raise RuntimeError("pyserial is required for serial discovery") from exc

    devices: list[SerialDevice] = []
    for port in list_ports.comports():
        devices.append(
            SerialDevice(
                device=port.device,
                vid=port.vid,
                pid=port.pid,
                description=port.description or "",
                serial_number=port.serial_number,
                location=port.location,
                manufacturer=getattr(port, "manufacturer", None),
                product=getattr(port, "product", None),
            )
        )
    return devices


def is_placeholder_port(dev: SerialDevice) -> bool:
    """Legacy on-board UART nodes (e.g. Linux /dev/ttyS*) with no device behind them."""
    return dev.vid is None and dev.pid is None and dev.description.strip().lower() in ("", "n/a")


def annotate_devices(devices: Iterable[SerialDevice]) -> list[dict]:
    """Attach USB interface hints while preserving unknown devices as unknown."""
    rows: list[dict] = []
    for dev in devices:
        hint = match_usb(dev.vid, dev.pid) if dev.vid is not None and dev.pid is not None else None
        row = {
            "node_id": dev.node_id,
            "device": dev.device,
            "vid": dev.vid,
            "pid": dev.pid,
            "description": dev.description,
            "serial_number": dev.serial_number,
            "location": dev.location,
            "manufacturer": dev.manufacturer,
            "product": dev.product,
            "transport": "usb_serial",
            "interface": hint["name"] if hint else dev.description,
            "status": "interface_recognized" if hint else "unknown_interface",
            "match_type": hint["match_type"] if hint else None,
            "candidate_profiles": hint["candidate_profiles"] if hint else [],
            "warning": "USB IDs identify the bridge/interface only, never the downstream board.",
        }
        rows.append(row)
    return rows
