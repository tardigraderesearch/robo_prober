"""Read a markers.json tag manifest (the format aruco-tag-maker writes).

Only the fields robo-prober needs are read:

    {"dictionary": "DICT_4X4_100", "tag_size_mm": 30.0,
     "tags": {"base": {"id": 0}, "gripper": {"id": 1}}}

This is plain JSON parsing; no tag-making package is imported or required.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DICT_RE = re.compile(r"^DICT_[A-Z0-9_]+$")


class MarkersError(ValueError):
    pass


@dataclass(frozen=True)
class Markers:
    dictionary: str
    tags: dict[str, int] = field(default_factory=dict)
    tag_size_mm: float | None = None

    def id_for(self, tag: str | int) -> int:
        """Marker ID for a tag name (an int is returned unchanged if it is a known ID)."""
        if isinstance(tag, bool):
            raise MarkersError(f"invalid tag {tag!r}")
        if isinstance(tag, int):
            if tag not in self.tags.values():
                raise MarkersError(f"marker id {tag} is not in the tag manifest")
            return tag
        if tag not in self.tags:
            raise MarkersError(f"unknown tag name {tag!r}; known: {', '.join(sorted(self.tags)) or 'none'}")
        return self.tags[tag]

    def name_for(self, marker_id: int) -> str | None:
        for name, value in self.tags.items():
            if value == marker_id:
                return name
        return None


def parse_markers(data: Any) -> Markers:
    if not isinstance(data, dict):
        raise MarkersError("markers.json must contain a JSON object")
    dictionary = data.get("dictionary")
    if not isinstance(dictionary, str) or not _DICT_RE.match(dictionary):
        raise MarkersError("markers.json needs a dictionary such as DICT_4X4_100")
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, dict) or not raw_tags:
        raise MarkersError("markers.json needs a non-empty tags object {name: {\"id\": n}}")
    tags: dict[str, int] = {}
    for name, entry in raw_tags.items():
        marker_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or isinstance(marker_id, bool) or not isinstance(marker_id, int) or marker_id < 0:
            raise MarkersError(f"tag {name!r} needs a non-negative integer id")
        tags[name] = marker_id
    if len(set(tags.values())) != len(tags):
        raise MarkersError("marker ids in markers.json must be unique")
    size = data.get("tag_size_mm")
    if size is not None and (isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0):
        raise MarkersError("tag_size_mm must be a positive number")
    return Markers(dictionary=dictionary, tags=tags, tag_size_mm=float(size) if size is not None else None)


def load_markers(path: str | Path) -> Markers:
    return parse_markers(json.loads(Path(path).read_text(encoding="utf-8")))
