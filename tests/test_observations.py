import pytest

from robo_prober.cli import demo_fixture_path
from robo_prober.observations import MarkerPose, calculate_marker_motion, load_observation_fixture, summarize_motion


def test_synthetic_fixture_motion_is_reference_relative():
    data = load_observation_fixture(demo_fixture_path())
    motion = data["motion"]
    assert motion[1]["dx"] == 4.0
    assert motion[1]["dy"] == 3.0
    assert data["summary"]["status"] == "observed_motion"
    assert "human confirmation required" in data["summary"]["reason"]


def test_angle_wrap_and_independent_thresholds():
    before = {0: MarkerPose(0, 0, 0), 1: MarkerPose(1, 20, 20, 179)}
    after = {0: MarkerPose(0, 0, 0), 1: MarkerPose(1, 20, 20, -179)}
    motion = calculate_marker_motion(before, after, reference_id=0)
    assert motion[1]["d_angle_deg"] == 2
    assert summarize_motion(motion, 0.5, angle_threshold_deg=5)["status"] == "ambiguous"
    assert summarize_motion(motion, 100, angle_threshold_deg=1)["moving_marker_ids"] == [1]


def test_detect_aruco_from_recorded_image(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from robo_prober.observations import detect_aruco_from_image

    image = np.full((360, 640), 255, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for i, marker_id in enumerate((0, 1)):
        image[100:220, 50 + i * 260:170 + i * 260] = cv2.aruco.generateImageMarker(dictionary, marker_id, 120)
    path = tmp_path / "synthetic.png"
    cv2.imwrite(str(path), image)
    assert sorted(detect_aruco_from_image(path)) == [0, 1]
