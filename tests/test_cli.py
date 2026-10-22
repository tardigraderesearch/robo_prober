import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "robo_prober.cli", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_boards_accepts_underscore_chip_typo():
    result = run_cli("boards", "--chip", "ESP32_C3", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["chip"] == "ESP32-C3"


def test_cli_no_device_error_path():
    result = run_cli("identify", "--port", "/dev/does-not-exist", "--consent-controller-identification", "NO")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "identification_failed"


def test_cli_demo_generates_reloadable_map(tmp_path):
    out = tmp_path / "demo-map.json"
    result = run_cli("run-demo", "--out", str(out))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "demo_complete"
    validate = run_cli("validate-map", str(out))
    assert validate.returncode == 0, validate.stderr
