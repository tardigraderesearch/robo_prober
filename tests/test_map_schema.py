import pytest

from robo_prober.cli import demo_confirmations, demo_fixture_path, demo_permissions
from robo_prober.map_schema import MapValidationError, export_atomic, load_map, validate_classifications
from robo_prober.permissions import load_permissions
from robo_prober.session import build_map_from_session


def test_export_reload_round_trip(tmp_path):
    permissions = load_permissions(demo_permissions())
    fixture = demo_fixture_path()
    hardware_map = build_map_from_session(
        controller_identity={"status": "simulated"},
        permissions=permissions,
        observation_paths=[fixture],
        confirmations=demo_confirmations(),
        probe_events=[{"event": "hello"}],
        warnings=["fixture only"],
    )
    out = tmp_path / "map.json"
    export_atomic(hardware_map, out)
    reloaded = load_map(out)
    assert sorted(reloaded["joints"]) == ["base_yaw", "elbow", "shoulder"]
    with pytest.raises(FileExistsError):
        export_atomic(hardware_map, out)


def test_map_rejects_missing_human_confirmation(tmp_path):
    permissions = load_permissions(demo_permissions())
    fixture = demo_fixture_path()
    confirmations = demo_confirmations()
    confirmations[0]["human_confirmed"] = False
    with pytest.raises(MapValidationError):
        build_map_from_session(
            controller_identity={"status": "simulated"},
            permissions=permissions,
            observation_paths=[fixture],
            confirmations=confirmations,
            probe_events=[],
        )


def _row(joint, channel):
    return {"joint": joint, "channel": channel, "direction": "positive", "human_confirmed": True}


def test_joint_names_are_operator_defined():
    validate_classifications([_row("gripper", 0), _row("wrist_roll", 1)])


@pytest.mark.parametrize("rows", [[], [_row("../etc", 0)], [_row("", 0)], [_row("a", 0), _row("a", 1)], [_row("a", 0), _row("b", 0)]])
def test_invalid_joint_classifications_rejected(rows):
    with pytest.raises(MapValidationError):
        validate_classifications(rows)


def test_expected_joints_enforced_when_requested():
    with pytest.raises(MapValidationError, match="missing"):
        validate_classifications([_row("base_yaw", 0)], expected_joints=("base_yaw", "shoulder"))
