"""Optional markers.json (tag manifest) support."""
import json

import pytest

from robo_prober.markers import MarkersError, load_markers, parse_markers

MANIFEST = {"generator": "any", "dictionary": "DICT_4X4_100", "tag_size_mm": 24.0,
            "tags": {"base": {"id": 10}, "upper_arm": {"id": 11}, "forearm": {"id": 12}}}


def test_manifest_parsed_and_names_resolved(tmp_path):
    path = tmp_path / "markers.json"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    markers = load_markers(path)
    assert markers.dictionary == "DICT_4X4_100" and markers.tag_size_mm == 24.0
    assert markers.id_for("forearm") == 12 and markers.id_for(11) == 11 and markers.name_for(10) == "base"
    with pytest.raises(MarkersError):
        markers.id_for("gripper")
    with pytest.raises(MarkersError):
        markers.id_for(99)


@pytest.mark.parametrize("mutate", [
    lambda m: m.pop("dictionary"),
    lambda m: m.update(dictionary="4x4"),
    lambda m: m.update(tags={}),
    lambda m: m["tags"].update(dup={"id": 10}),
    lambda m: m["tags"].update(bad={"id": -1}),
    lambda m: m["tags"].update(bad={"id": True}),
    lambda m: m.update(tag_size_mm=0),
])
def test_bad_manifests_rejected(mutate):
    data = json.loads(json.dumps(MANIFEST))
    mutate(data)
    with pytest.raises(MarkersError):
        parse_markers(data)


def test_vision_config_accepts_tag_names(tmp_path):
    pytest.importorskip("cv2")
    from robo_prober import vision_pipeline as v

    (tmp_path / "markers.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    config = {"markers_file": "markers.json", "dictionary_confirmed": True, "reference_tag": "base",
              "markers": [{"tag": "base", "link": "base_link"}, {"tag": "forearm", "link": "forearm_link"}],
              "pixel_threshold": 3.0, "angle_threshold_deg": 3.0, "calibration": None}
    (tmp_path / "vision.json").write_text(json.dumps(config), encoding="utf-8")
    cfg = v.load_config(tmp_path / "vision.json")
    assert cfg["dictionary"] == "DICT_4X4_100" and cfg["reference_id"] == 10
    assert [m["id"] for m in cfg["markers"]] == [10, 12] and "markers_file" not in cfg
    # A recorded session stores the resolved config, which reloads without the manifest.
    (tmp_path / "saved.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert v.load_config(tmp_path / "saved.json")["markers"] == cfg["markers"]

    config["dictionary"] = "DICT_5X5_100"
    (tmp_path / "vision.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        v.load_config(tmp_path / "vision.json")
    del config["markers_file"], config["dictionary"]
    (tmp_path / "vision.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="markers_file"):
        v.load_config(tmp_path / "vision.json")
