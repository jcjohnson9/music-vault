from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QWizard

from music_vault.ui.onboarding import (
    FirstRunWizard,
    OnboardingResult,
    RuntimeEvidence,
    inspect_runtime_evidence,
    sanitized_onboarding_config,
    should_show_first_run,
    validate_writable_folder,
)


def _runtime_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "config_file": tmp_path / "data" / "music_vault_config.json",
        "database_file": tmp_path / "data" / "music_vault.sqlite3",
        "api_key_file": tmp_path / "data" / "youtube_api_key.txt",
        "status_file": tmp_path / "data" / "music_vault_status.json",
    }


def _wizard(tmp_path: Path, qapp) -> FirstRunWizard:
    portable = tmp_path / "portable"
    data = portable / "data"
    downloads = data / "youtube_downloads"
    for folder in (portable, data, downloads):
        folder.mkdir(parents=True, exist_ok=True)
    wizard = FirstRunWizard(
        portable_folder=portable,
        data_folder=data,
        download_folder=downloads,
        ffmpeg_ready=False,
        create_shortcut_default=False,
    )
    qapp.processEvents()
    return wizard


def test_blank_runtime_triggers_first_run_but_completed_config_does_not(tmp_path):
    evidence = inspect_runtime_evidence(**_runtime_paths(tmp_path))

    assert evidence == RuntimeEvidence(established=False)
    assert should_show_first_run(None, evidence)
    assert should_show_first_run({}, evidence)
    assert not should_show_first_run({"onboarding_completed": True}, evidence)


def test_any_established_runtime_evidence_suppresses_first_run(tmp_path):
    paths = _runtime_paths(tmp_path)
    paths["config_file"].parent.mkdir(parents=True)

    paths["config_file"].write_text(
        json.dumps({"audio_quality": "320"}), encoding="utf-8"
    )
    evidence = inspect_runtime_evidence(**paths)
    assert evidence.config_exists
    assert evidence.established
    assert not should_show_first_run({}, evidence)

    paths["config_file"].write_text("{}", encoding="utf-8")
    paths["status_file"].write_text("{}", encoding="utf-8")
    evidence = inspect_runtime_evidence(**paths)
    assert not evidence.config_exists
    assert evidence.status_exists
    assert not should_show_first_run({}, evidence)

    paths["status_file"].unlink()
    paths["api_key_file"].write_bytes(b"synthetic-nonempty-secret-evidence")
    evidence = inspect_runtime_evidence(**paths)
    assert evidence.api_key_exists
    assert not should_show_first_run({}, evidence)


def test_nonempty_library_is_inspected_read_only_and_never_treated_as_blank(tmp_path):
    paths = _runtime_paths(tmp_path)
    database = paths["database_file"]
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE playlists (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO tracks(title) VALUES ('Synthetic One'), ('Synthetic Two');
        INSERT INTO playlists(name) VALUES ('Synthetic List');
        """
    )
    connection.commit()
    connection.close()
    before_bytes = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns

    evidence = inspect_runtime_evidence(**paths)

    assert evidence.library_rows == 3
    assert evidence.established
    assert not should_show_first_run({}, evidence)
    assert database.read_bytes() == before_bytes
    assert database.stat().st_mtime_ns == before_mtime
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_unclassifiable_existing_database_fails_closed_as_established(tmp_path):
    paths = _runtime_paths(tmp_path)
    paths["database_file"].parent.mkdir(parents=True)
    paths["database_file"].write_bytes(b"not a sqlite database")

    evidence = inspect_runtime_evidence(**paths)

    assert evidence.library_rows == 1
    assert evidence.established
    assert not should_show_first_run({}, evidence)


def test_malformed_nonempty_config_is_preserved_as_runtime_evidence(tmp_path):
    paths = _runtime_paths(tmp_path)
    paths["config_file"].parent.mkdir(parents=True)
    paths["config_file"].write_text("{incomplete", encoding="utf-8")

    evidence = inspect_runtime_evidence(**paths)

    assert evidence.config_exists
    assert evidence.established
    assert not should_show_first_run({}, evidence)


def test_writable_folder_validation_uses_and_removes_a_probe(tmp_path):
    selected = tmp_path / "new runtime" / "data"

    ok, error = validate_writable_folder(selected)

    assert ok
    assert error is None
    assert selected.is_dir()
    assert list(selected.glob(".music-vault-write-test-*.tmp")) == []


def test_unwritable_folder_validation_returns_a_sanitized_error(tmp_path, monkeypatch):
    selected = tmp_path / "unwritable"

    def deny_mkdir(_self, *args, **kwargs):
        raise PermissionError("synthetic private operating-system detail")

    monkeypatch.setattr(Path, "mkdir", deny_mkdir)
    ok, error = validate_writable_folder(selected)

    assert not ok
    assert error == "Music Vault cannot write to the selected folder (PermissionError)."
    assert str(selected) not in error
    assert "synthetic private" not in error


def test_sanitized_config_persists_choices_without_any_api_key(tmp_path):
    existing = {
        "theme": "dark",
        "youtube_api_key": "stale-secret",
        "API_KEY_BACKUP": "another-secret",
    }
    original = dict(existing)
    result = OnboardingResult(
        data_folder=tmp_path / "data",
        download_folder=tmp_path / "downloads",
        local_import_folder=tmp_path / "local library",
        api_key="new-secret",
        authorized_use_acknowledged=True,
        audio_quality="256",
        ffmpeg_location=str(tmp_path / "tools" / "ffmpeg.exe"),
        create_shortcut=True,
    )

    config = sanitized_onboarding_config(existing, result)
    serialized = json.dumps(config)

    assert existing == original
    assert config["theme"] == "dark"
    assert config["download_folder"] == str(result.download_folder.resolve())
    assert config["audio_quality"] == "256"
    assert config["onboarding_completed"] is True
    assert config["authorized_use_acknowledged"] is True
    assert config["ffmpeg_location"] == result.ffmpeg_location
    assert all("api_key" not in str(key).casefold() for key in config)
    assert "stale-secret" not in serialized
    assert "another-secret" not in serialized
    assert "new-secret" not in serialized


def test_authorization_acknowledgement_gates_sync_but_not_local_import(tmp_path, qapp):
    wizard = _wizard(tmp_path, qapp)
    local_folder = tmp_path / "local library"
    try:
        wizard.local_import_folder.setText(str(local_folder))
        wizard.api_key.setText("synthetic-secret")

        assert not wizard.configure_youtube.isEnabled()
        assert not wizard.api_key.isEnabled()
        assert wizard.local_import_folder.isEnabled()

        wizard.authorized_ack.setChecked(True)
        assert wizard.configure_youtube.isEnabled()
        wizard.configure_youtube.setChecked(True)
        assert wizard.api_key.isEnabled()
        assert wizard.result_values().api_key == "synthetic-secret"

        wizard.authorized_ack.setChecked(False)
        assert not wizard.configure_youtube.isChecked()
        assert not wizard.configure_youtube.isEnabled()
        assert not wizard.api_key.isEnabled()
        result = wizard.result_values()
        assert result.api_key == ""
        assert result.local_import_folder == local_folder.resolve()
    finally:
        wizard.close()


def test_local_only_result_allows_no_api_key_or_ffmpeg_and_persists_quality(
    tmp_path, qapp
):
    wizard = _wizard(tmp_path, qapp)
    try:
        wizard.audio_quality.setCurrentText("192")
        result = wizard.result_values()
        config = sanitized_onboarding_config({}, result)

        assert result.api_key == ""
        assert result.ffmpeg_location is None
        assert not result.authorized_use_acknowledged
        assert result.audio_quality == "192"
        assert config["audio_quality"] == "192"
        assert config["onboarding_completed"] is True
    finally:
        wizard.close()


def test_skip_setup_uses_validated_local_defaults_without_sync_or_shortcut(
    tmp_path, qapp
):
    wizard = _wizard(tmp_path, qapp)
    try:
        wizard.authorized_ack.setChecked(True)
        wizard.configure_youtube.setChecked(True)
        wizard.api_key.setText("synthetic-skipped-secret")
        wizard.create_shortcut.setChecked(True)

        wizard._custom_button_clicked(QWizard.WizardButton.CustomButton1.value)

        result = wizard.result_values()
        assert wizard.result() == QDialog.Accepted
        assert result.skipped
        assert result.api_key == ""
        assert result.local_import_folder is None
        assert not result.create_shortcut
    finally:
        wizard.close()


def test_result_keeps_explicit_download_location_and_rebases_untouched_default(
    tmp_path, qapp
):
    wizard = _wizard(tmp_path, qapp)
    changed_data = tmp_path / "chosen data"
    explicit_downloads = tmp_path / "chosen downloads"
    try:
        wizard.data_folder.setText(str(changed_data))
        rebased = wizard.result_values()
        assert rebased.data_folder == changed_data.resolve()
        assert rebased.download_folder == (changed_data / "youtube_downloads").resolve()

        wizard.download_folder.setText(str(explicit_downloads))
        explicit = wizard.result_values()
        assert explicit.download_folder == explicit_downloads.resolve()
    finally:
        wizard.close()


def test_prepare_first_run_persists_config_and_secret_separately_before_window(
    tmp_path, monkeypatch, qapp
):
    from music_vault import app as app_module

    root = tmp_path / "blank portable"
    data = root / "data"
    config_file = data / "music_vault_config.json"
    database_file = data / "music_vault.sqlite3"
    key_file = data / "youtube_api_key.txt"
    status_file = data / "music_vault_status.json"
    result = OnboardingResult(
        data_folder=data,
        download_folder=data / "youtube_downloads",
        local_import_folder=None,
        api_key="synthetic-onboarding-secret",
        authorized_use_acknowledged=True,
        audio_quality="256",
        ffmpeg_location=None,
        create_shortcut=False,
    )

    class FakeWizard:
        def __init__(self, **_kwargs):
            pass

        def setStyleSheet(self, _stylesheet):
            pass

        def exec(self):
            return QDialog.Accepted

        def result_values(self):
            return result

    monkeypatch.setattr(app_module, "config_path", lambda: config_file)
    monkeypatch.setattr(app_module, "database_path", lambda: database_file)
    monkeypatch.setattr(app_module, "youtube_api_key_path", lambda: key_file)
    monkeypatch.setattr(app_module, "app_status_path", lambda: status_file)
    monkeypatch.setattr(app_module, "data_dir", lambda: data)
    monkeypatch.setattr(app_module, "default_downloads_dir", lambda: data / "youtube_downloads")
    monkeypatch.setattr(app_module, "project_root", lambda: root)
    monkeypatch.setattr(app_module, "portable_root", lambda: None)
    monkeypatch.setattr(app_module, "FirstRunWizard", FakeWizard)
    monkeypatch.setattr(
        app_module,
        "discover_ffmpeg",
        lambda **_kwargs: SimpleNamespace(ready=False),
    )
    monkeypatch.setattr(
        app_module,
        "configure_data_dir",
        lambda *_args, **_kwargs: SimpleNamespace(
            configured=True, persisted=False, error=None
        ),
    )

    proceed, selected = app_module.prepare_first_run(qapp)

    assert proceed
    assert selected == result
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["onboarding_completed"] is True
    assert saved["authorized_use_acknowledged"] is True
    assert saved["download_folder"] == str(result.download_folder.resolve())
    assert key_file.read_text(encoding="utf-8") == "synthetic-onboarding-secret"
    assert "synthetic-onboarding-secret" not in config_file.read_text(encoding="utf-8")
    assert all("api_key" not in key.casefold() for key in saved)
    assert not database_file.exists()


def test_existing_completion_inference_never_overwrites_malformed_config(
    tmp_path,
):
    from music_vault.app import _config_supports_completion_inference

    missing = tmp_path / "missing.json"
    valid = tmp_path / "valid.json"
    malformed = tmp_path / "malformed.json"
    valid.write_text(json.dumps({"audio_quality": "320"}), encoding="utf-8")
    malformed.write_text("{unfinished", encoding="utf-8")

    assert _config_supports_completion_inference(missing)
    assert _config_supports_completion_inference(valid)
    assert not _config_supports_completion_inference(malformed)
    assert malformed.read_text(encoding="utf-8") == "{unfinished"
