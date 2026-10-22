from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PermissionError(ValueError):
    pass


SUPPORTED_ROUTES = {"pca9685_servo"}


@dataclass(frozen=True)
class ChannelPermission:
    channel: int
    route: str
    neutral_us: int
    min_us: int
    max_us: int
    step_us: int
    approved_by: str
    approved_at: str
    board_pin: str | None = None

    def validate(self) -> None:
        if self.route not in SUPPORTED_ROUTES:
            raise PermissionError(f"unsupported route {self.route!r}; generic GPIO probing is denied")
        if not 0 <= self.channel <= 15:
            raise PermissionError("PCA9685 servo channel must be in 0..15")
        if self.board_pin:
            raise PermissionError("board pin metadata cannot authorize servo probing")
        if self.min_us < 500 or self.max_us > 2500 or self.min_us >= self.max_us:
            raise PermissionError("servo pulse bounds must be within 500..2500 us and ordered")
        if not self.min_us <= self.neutral_us <= self.max_us:
            raise PermissionError("neutral_us must be inside approved bounds")
        if self.step_us <= 0:
            raise PermissionError("step_us must be positive")
        for target in (self.neutral_us - self.step_us, self.neutral_us + self.step_us):
            if not self.min_us <= target <= self.max_us:
                raise PermissionError("neutral +/- step_us must stay inside approved bounds")
        if not self.approved_by or not self.approved_at:
            raise PermissionError("approval identity and timestamp are required")

    def target_us(self, direction: int) -> int:
        if direction not in (-1, 1):
            raise PermissionError("probe direction must be -1 or 1")
        target = self.neutral_us + direction * self.step_us
        if not self.min_us <= target <= self.max_us:
            raise PermissionError("target pulse is outside approved bounds")
        return target


@dataclass(frozen=True)
class ProbePermissions:
    controller_node_id: str
    board_profile_id: str
    physical_power_cutoff_required: bool
    channels: dict[int, ChannelPermission]
    notes: str = ""

    def validate(self) -> None:
        if self.physical_power_cutoff_required is not True:
            raise PermissionError("a physical power cutoff must be required for powered workflows")
        if not self.controller_node_id:
            raise PermissionError("controller_node_id is required")
        if not self.board_profile_id:
            raise PermissionError("board_profile_id is required")
        seen: set[int] = set()
        for channel, permission in self.channels.items():
            if channel in seen:
                raise PermissionError("duplicate channel permission")
            if channel != permission.channel:
                raise PermissionError("channel key must match permission.channel")
            permission.validate()
            seen.add(channel)


def load_permissions(data: dict[str, Any]) -> ProbePermissions:
    raw_channels = data.get("channels")
    if not isinstance(raw_channels, list):
        raise PermissionError("channels must be a list")
    channels: dict[int, ChannelPermission] = {}
    for item in raw_channels:
        if not isinstance(item, dict):
            raise PermissionError("channel entries must be objects")
        permission = ChannelPermission(
            channel=int(item["channel"]),
            route=str(item["route"]),
            neutral_us=int(item["neutral_us"]),
            min_us=int(item["min_us"]),
            max_us=int(item["max_us"]),
            step_us=int(item["step_us"]),
            approved_by=str(item["approved_by"]),
            approved_at=str(item["approved_at"]),
            board_pin=item.get("board_pin"),
        )
        if permission.channel in channels:
            raise PermissionError("duplicate channel permission")
        channels[permission.channel] = permission
    permissions = ProbePermissions(
        controller_node_id=str(data.get("controller_node_id", "")),
        board_profile_id=str(data.get("board_profile_id", "")),
        physical_power_cutoff_required=bool(data.get("physical_power_cutoff_required")),
        channels=channels,
        notes=str(data.get("notes", "")),
    )
    permissions.validate()
    return permissions
