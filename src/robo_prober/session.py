from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .map_schema import build_hardware_map
from .observations import load_observation_fixture
from .permissions import ProbePermissions
from .transport import SerialProtocolTransport


class ProbeSessionError(RuntimeError):
    pass


def classifications_from_confirmations(
    permissions: ProbePermissions, confirmations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in confirmations:
        channel = int(item["channel"])
        permission = permissions.channels.get(channel)
        if permission is None:
            raise ProbeSessionError(f"no permission for confirmed channel {channel}")
        rows.append(
            {
                "joint": str(item["joint"]),
                "channel": channel,
                "direction": str(item.get("direction", "unknown")),
                "invert": bool(item.get("invert", False)),
                "human_confirmed": bool(item.get("human_confirmed")),
                "neutral_us": permission.neutral_us,
                "min_us": permission.min_us,
                "max_us": permission.max_us,
                "route": permission.route,
            }
        )
    return rows


def run_bounded_probe(
    *,
    transport: SerialProtocolTransport,
    permissions: ProbePermissions,
    channels: list[int],
    arm_token: str,
    cutoff_confirmed: bool,
) -> list[dict[str, Any]]:
    permissions.validate()
    events: list[dict[str, Any]] = []
    armed = False
    try:
        hello = transport.hello()
        events.append({"event": "hello", "response": hello})
        transport.arm(arm_token, cutoff_confirmed=cutoff_confirmed)
        armed = True
        for channel in channels:
            permission = permissions.channels.get(channel)
            if permission is None:
                raise ProbeSessionError(f"channel {channel} lacks explicit permission")
            target = permission.target_us(1)
            response = transport.move_us(channel, target)
            events.append({"event": "move", "channel": channel, "target_us": target, "response": response})
            neutral = transport.move_us(channel, permission.neutral_us)
            events.append({"event": "return_to_approved_neutral", "channel": channel, "target_us": permission.neutral_us, "response": neutral})
        transport.disarm()
        armed = False
        events.append({"event": "disarm"})
        return events
    except Exception:
        if armed:
            try:
                transport.stop()
            except Exception:
                pass
        raise


def permissions_to_json(permissions: ProbePermissions) -> dict[str, Any]:
    return {
        "controller_node_id": permissions.controller_node_id,
        "board_profile_id": permissions.board_profile_id,
        "physical_power_cutoff_required": permissions.physical_power_cutoff_required,
        "notes": permissions.notes,
        "channels": [asdict(value) for _, value in sorted(permissions.channels.items())],
    }


def build_map_from_session(
    *,
    controller_identity: dict[str, Any],
    permissions: ProbePermissions,
    observation_paths,
    confirmations: list[dict[str, Any]],
    probe_events: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    observations = [load_observation_fixture(path) for path in observation_paths]
    observations.append({"kind": "transport_events", "events": probe_events})
    classifications = classifications_from_confirmations(permissions, confirmations)
    return build_hardware_map(
        controller_identity=controller_identity,
        board_profile_id=permissions.board_profile_id,
        permissions=permissions_to_json(permissions),
        observations=observations,
        classifications=classifications,
        warnings=warnings or [],
    )
