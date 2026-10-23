"""flasher: backup / install / restore with esptool mocked. No real port is ever opened."""
import datetime as dt
import hashlib
import json

import pytest

from robo_prober import flasher
from robo_prober.flasher import FirmwareStatus, FlashError

MAC = "aa:bb:cc:dd:ee:01"
NOW = dt.datetime(2026, 10, 22, 12, 0, 0)
CURRENT = flasher.load_manifest()


def hello(fw=None, caps=None, **extra):
    info = {"proto": "ROBOPROBE/1", "id": "esp32_devkitc", "fw": fw or CURRENT["fw_version"],
            "caps": ",".join(CURRENT["caps"] if caps is None else caps)}
    info.update(extra)
    return info


class FakeSession:
    """Stands in for EsptoolSession: an in-memory flash."""

    def __init__(self, chip_name="ESP32-S3", mac=MAC, size=1024 * 1024, short_read=False):
        self.chip_name = chip_name
        self._mac = mac
        self.flash = bytearray(b"\x5a" * size)
        self.short_read = short_read
        self.writes = []
        self.resets = 0
        self.closed = False

    def mac(self):
        return self._mac

    def flash_size(self):
        return len(self.flash)

    def read(self, size, progress):
        progress(size // 2, size)
        progress(size, size)
        return bytes(self.flash[: size - 1 if self.short_read else size])

    def write(self, data, progress):
        self.writes.append(bytes(data))
        self.flash[: len(data)] = data
        progress(len(data), len(data))

    def md5(self, size):
        return hashlib.md5(bytes(self.flash[:size])).hexdigest()

    def hard_reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


def opener(session):
    return lambda port, *a, **k: session


# ---------------------------------------------------------------- manifest
def test_manifest_images_match_sha_and_cover_every_chip():
    assert set(CURRENT["images"]) == {"esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6"}
    assert CURRENT["proto"] == "ROBOPROBE/1"
    for chip, entry in CURRENT["images"].items():
        data = flasher.firmware_image(chip)
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert entry["bootloader_offset"] == ("0x1000" if chip in ("esp32", "esp32s2") else "0x0")


def test_manifest_matches_host_emulator_version_and_caps():
    from robo_prober import reference_device
    assert CURRENT["fw_version"] == reference_device.FIRMWARE_VERSION
    assert set(CURRENT["caps"]) == set(reference_device.CAPS.split(","))


def test_corrupt_image_is_refused(tmp_path):
    manifest = json.loads(json.dumps(CURRENT))
    (tmp_path / "esp32s3.bin").write_bytes(b"not the image")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FlashError, match="does not match its manifest"):
        flasher.firmware_image("esp32s3", tmp_path)


# ------------------------------------------------------------- HELLO check
def test_classify_current_foreign_outdated_and_sbc():
    assert flasher.classify_hello(hello()).state == "current"
    assert flasher.classify_hello(None).state == "foreign"
    assert flasher.classify_hello({"proto": "OTHER"}).state == "foreign"
    old = flasher.classify_hello(hello(fw="0.2.0"))
    assert old.state == "outdated" and not old.needs_install and old.update_available  # same caps: works as is, no popup
    sbc = flasher.classify_hello(hello(fw="0.0.1", id="raspberry_pi_5"))
    assert sbc.state == "sbc" and not sbc.needs_install and "python -m robo_prober.sbc" in sbc.message


def test_same_version_but_missing_caps_is_outdated():
    status = flasher.classify_hello(hello(caps=["limit", "status", "hb", "token", "estop", "start"]))
    assert status.state == "outdated"
    assert "survey" in status.missing_caps


def test_older_robo_prober_firmware_triggers_install(tmp_path):
    """0.2.0 robo-prober firmware (no survey cap) is backed up and replaced, like foreign firmware."""
    session = FakeSession()
    calls = []

    def check(port, manifest=None):
        calls.append(port)
        if len(calls) == 1:
            return flasher.classify_hello(hello(fw="0.2.0", caps=["limit", "status", "hb", "token"]))
        return flasher.classify_hello(hello())

    result = flasher.install("PORT", out_dir=tmp_path, check=check, open_session=opener(session), verify_wait_s=0)
    assert not result.skipped and result.backup.exists()
    assert session.writes == [flasher.firmware_image("esp32s3")]


def test_check_firmware_retries_then_reports_foreign():
    attempts = []

    def fail(port, timeout):
        attempts.append(port)
        raise TimeoutError

    assert flasher.check_firmware("PORT", attempts=2, hello_fn=fail).state == "foreign"
    assert len(attempts) == 2


# ------------------------------------------------------------------- backup
def test_backup_naming_sidecar_and_no_overwrite(tmp_path):
    first = flasher.backup("PORT", tmp_path, open_session=opener(FakeSession()), now=NOW)
    second = flasher.backup("PORT", tmp_path, open_session=opener(FakeSession()), now=NOW)
    assert first.name == "esp32s3-aabbccddee01-20261022-120000.bin"
    assert second != first and second.exists() and first.exists()
    meta = json.loads(first.with_suffix(".json").read_text())
    assert meta["mac"] == MAC and meta["chip"] == "esp32s3" and meta["flash_size"] == 1024 * 1024
    assert meta["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert flasher.newest_backup(MAC, tmp_path) in (first, second)
    assert set(flasher.backups_for(MAC, tmp_path)) == {first, second}


def test_short_backup_aborts_before_flashing(tmp_path):
    session = FakeSession(short_read=True)
    with pytest.raises(FlashError, match="expected"):
        flasher.install("PORT", out_dir=tmp_path, check=lambda p, manifest=None: FirmwareStatus("foreign", "x"),
                        open_session=opener(session), verify_wait_s=0)
    assert session.writes == []
    assert list(tmp_path.glob("*.bin")) == []


def test_unsupported_chip_is_refused(tmp_path):
    with pytest.raises(FlashError, match="unsupported chip"):
        flasher.backup("PORT", tmp_path, open_session=opener(FakeSession(chip_name="ESP8266")))


# ------------------------------------------------------------------ install
def test_install_skips_when_current_firmware_answers(tmp_path):
    def no_session(*a, **k):
        raise AssertionError("must not open esptool when firmware is current")

    result = flasher.install("PORT", out_dir=tmp_path, check=lambda p, manifest=None: flasher.classify_hello(hello()),
                             open_session=no_session)
    assert result.skipped and "nothing to install" in result.message
    assert list(tmp_path.iterdir()) == []


def test_install_skips_sbc_boards(tmp_path):
    status = flasher.classify_hello(hello(id="jetson_orin_nano"))
    result = flasher.install("PORT", out_dir=tmp_path, check=lambda p, manifest=None: status,
                             open_session=lambda *a: pytest.fail("never flash an SBC"))
    assert result.skipped and "Raspberry Pi / Jetson" in result.message


def test_install_backs_up_flashes_verifies(tmp_path):
    session = FakeSession(chip_name="ESP32")
    original = bytes(session.flash)
    answers = iter([FirmwareStatus("foreign", "no"), flasher.classify_hello(hello())])
    stages = []
    result = flasher.install("PORT", out_dir=tmp_path, check=lambda p, manifest=None: next(answers),
                             open_session=opener(session), progress=lambda s, d, t: stages.append(s),
                             verify_wait_s=0)
    assert result.backup.read_bytes() == original
    assert session.writes == [flasher.firmware_image("esp32")] and session.resets == 1 and session.closed
    assert any(s.startswith("Backing up") for s in stages) and any(s.startswith("Verifying") for s in stages)


def test_install_fails_when_hello_does_not_come_back(tmp_path):
    with pytest.raises(FlashError, match="did not answer HELLO"):
        flasher.install("PORT", out_dir=tmp_path, check=lambda p, manifest=None: FirmwareStatus("foreign", "no"),
                        open_session=opener(FakeSession()), verify_wait_s=0)


def test_flash_verify_mismatch_raises(tmp_path):
    session = FakeSession()
    session.md5 = lambda size: "0" * 32
    with pytest.raises(FlashError, match="verify failed"):
        flasher.flash_test_firmware("PORT", open_session=opener(session))
    assert session.resets == 0


# ------------------------------------------------------------------ restore
def test_restore_refuses_other_mac(tmp_path):
    path = flasher.backup("PORT", tmp_path, open_session=opener(FakeSession()), now=NOW)
    other = FakeSession(mac="11:22:33:44:55:66")
    with pytest.raises(FlashError, match="refusing to restore it to a different board"):
        flasher.restore("PORT", path, open_session=opener(other))
    assert other.writes == []


def test_restore_writes_backup_back_and_verifies(tmp_path):
    board = FakeSession()
    original = bytes(board.flash)
    path = flasher.backup("PORT", tmp_path, open_session=opener(board), now=NOW)
    board.flash[:4] = b"test"
    flasher.restore("PORT", path, open_session=opener(board))
    assert bytes(board.flash) == original and board.resets == 1


def test_restore_refuses_tampered_backup(tmp_path):
    path = flasher.backup("PORT", tmp_path, open_session=opener(FakeSession()), now=NOW)
    path.write_bytes(b"\x00" * (1024 * 1024))
    with pytest.raises(FlashError, match="sha256"):
        flasher.restore("PORT", path, open_session=opener(FakeSession()))


def test_outdated_caps_error_names_no_version():
    from robo_prober.transport import FirmwareOutdated, SerialProtocolTransport

    transport = SerialProtocolTransport("unused")
    with pytest.raises(FirmwareOutdated) as info:
        transport.require_caps("survey")
    assert "v0." not in str(info.value) and "firmware install" in str(info.value)


# ---------------------------------------------------------------------- CLI
def test_cli_install_asks_and_cancel_flashes_nothing(monkeypatch, capsys):
    from robo_prober import cli

    monkeypatch.setattr(flasher, "check_firmware", lambda port: FirmwareStatus("foreign", "Not robo-prober."))
    monkeypatch.setattr(flasher, "install", lambda *a, **k: pytest.fail("must not flash without a yes"))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["firmware", "install", "--port", "FAKE"]) == 1
    assert "Nothing done" in capsys.readouterr().out


def test_cli_install_yes_flag(monkeypatch, tmp_path):
    from robo_prober import cli

    seen = {}
    monkeypatch.setattr(flasher, "check_firmware", lambda port: FirmwareStatus("foreign", "Not robo-prober."))

    def fake_install(port, **kwargs):
        seen.update(kwargs, port=port)
        return flasher.InstallResult(False, "Installed.")

    monkeypatch.setattr(flasher, "install", fake_install)
    assert cli.main(["firmware", "install", "--port", "FAKE", "-y", "--backup-dir", str(tmp_path)]) == 0
    assert seen["port"] == "FAKE" and seen["out_dir"] == tmp_path


def test_install_can_skip_the_backup(tmp_path):
    import inspect
    assert inspect.signature(flasher.install).parameters["backup"].default is True
