from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class MarkerPose:
    marker_id: int
    x: float
    y: float
    angle_deg: float = 0.0
    visible: bool = True


def load_marker_frame(data: dict[str, Any]) -> dict[int, MarkerPose]:
    markers = data.get("markers")
    if not isinstance(markers, list):
        raise ObservationError("observation frame must contain a markers list")
    result: dict[int, MarkerPose] = {}
    for item in markers:
        marker = MarkerPose(
            marker_id=int(item["id"]),
            x=float(item["x"]),
            y=float(item["y"]),
            angle_deg=float(item.get("angle_deg", 0.0)),
            visible=bool(item.get("visible", True)),
        )
        result[marker.marker_id] = marker
    return result


def relative_frame(frame: dict[int, MarkerPose], reference_id: int) -> dict[int, MarkerPose]:
    reference = frame.get(reference_id)
    if reference is None or not reference.visible:
        raise ObservationError(f"reference marker {reference_id} is not visible")
    rel: dict[int, MarkerPose] = {}
    for marker_id, pose in frame.items():
        if not pose.visible:
            continue
        rel[marker_id] = MarkerPose(
            marker_id=marker_id,
            x=pose.x - reference.x,
            y=pose.y - reference.y,
            angle_deg=(pose.angle_deg - reference.angle_deg + 180) % 360 - 180,
            visible=True,
        )
    return rel


def calculate_marker_motion(
    before: dict[int, MarkerPose], after: dict[int, MarkerPose], *, reference_id: int
) -> dict[int, dict[str, float | bool]]:
    before_rel = relative_frame(before, reference_id)
    after_rel = relative_frame(after, reference_id)
    shared = sorted(set(before_rel) & set(after_rel) - {reference_id})
    if not shared:
        raise ObservationError("no shared visible moving markers")
    motion: dict[int, dict[str, float | bool]] = {}
    for marker_id in shared:
        dx = after_rel[marker_id].x - before_rel[marker_id].x
        dy = after_rel[marker_id].y - before_rel[marker_id].y
        da = (after_rel[marker_id].angle_deg - before_rel[marker_id].angle_deg + 180) % 360 - 180
        motion[marker_id] = {
            "dx": dx,
            "dy": dy,
            "d_angle_deg": da,
            "distance": math.hypot(dx, dy),
            "ambiguous": False,
        }
    return motion


def summarize_motion(motion: dict[int, dict[str, float | bool]], threshold: float, *, angle_threshold_deg: float | None = None) -> dict[str, Any]:
    if angle_threshold_deg is None:
        angle_threshold_deg = threshold  # legacy fixture compatibility
    if any(not math.isfinite(v) or v < 0 for v in (threshold, angle_threshold_deg)):
        raise ObservationError("thresholds must be finite and nonnegative")
    moved = [
        marker_id
        for marker_id, values in motion.items()
        if float(values["distance"]) >= threshold or abs(float(values["d_angle_deg"])) >= angle_threshold_deg
    ]
    return {
        "status": "observed_motion" if moved else "ambiguous",
        "moving_marker_ids": moved,
        "reason": "human confirmation required; marker motion alone is not a joint assignment",
    }


def load_observation_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") == "unavailable":
        raise ObservationError(data.get("reason", "comparison unavailable"))
    if data.get("status") == "incomplete":
        raise ObservationError("Recorded observation is incomplete: reference/tags missing or unassigned")
    reference_id = int(data["reference_marker_id"])
    before = load_marker_frame(data["before"])
    after = load_marker_frame(data["after"])
    motion = calculate_marker_motion(before, after, reference_id=reference_id)
    return {
        "source": str(path),
        "provenance": data.get("provenance"),
        "units": data.get("units"),
        "kind": data.get("kind", "fixture"),
        "reference_marker_id": reference_id,
        "marker_link_map": data.get("marker_link_map", {}),
        "motion": motion,
        "summary": summarize_motion(motion, float(data.get("pixel_threshold", data.get("threshold", 1.0))), angle_threshold_deg=float(data.get("angle_threshold_deg", data.get("threshold", 1.0)))),
        "warnings": [
            "Synthetic or recorded observations are evidence for software behavior only unless captured from the physical setup."
        ],
    }


def detect_aruco_from_image(path: Path, *, dictionary_name: str = "DICT_4X4_50") -> dict[int, MarkerPose]:
    """Optional OpenCV ArUco detector for recorded images."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ObservationError("OpenCV with aruco support is not installed; install robo-prober[vision]") from exc

    dictionary_id = getattr(cv2.aruco, dictionary_name, None)
    if dictionary_id is None:
        raise ObservationError(f"unknown ArUco dictionary {dictionary_name}")
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ObservationError(f"could not read image {path}")
    dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
    corners, ids, _ = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters()).detectMarkers(image)
    if ids is None:
        return {}
    result: dict[int, MarkerPose] = {}
    for marker_id, corner in zip(ids.flatten(), corners):
        points = np.asarray(corner[0], dtype=float)
        center = points.mean(axis=0)
        dx, dy = points[1] - points[0]
        angle = math.degrees(math.atan2(float(dy), float(dx)))
        result[int(marker_id)] = MarkerPose(int(marker_id), float(center[0]), float(center[1]), angle)
    return result
