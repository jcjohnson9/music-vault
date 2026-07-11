from __future__ import annotations

import json
from pathlib import Path

from music_vault.core import app_status
from music_vault.core.db import MusicVaultDB
from music_vault.core.playback_errors import playback_error_message
from music_vault.core.watchtower_status import write_watchtower_status
from music_vault.app import MusicVaultWindow


def test_app_status_schema_and_compatibility_alias(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(app_status, "project_root", lambda: tmp_path)
    monkeypatch.setattr(app_status, "data_dir", lambda: data)
    monkeypatch.setattr(app_status, "app_status_path", lambda: data / "music_vault_status.json")
    monkeypatch.setattr(app_status, "config_path", lambda: data / "config.json")
    monkeypatch.setattr(app_status, "youtube_api_key_path", lambda: data / "missing-key.txt")
    monkeypatch.setattr(app_status, "path_resolution_source", lambda: "synthetic")
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    path = app_status.write_app_status(
        db,
        {"download_folder": str(tmp_path / "downloads")},
        {"sync": {"last_sync_status": "complete_with_issues", "last_sync_failed_count": 2}},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["sync"]["last_sync_status"] == "complete_with_issues"
    assert payload["sync"]["last_sync_failed_count"] == 2
    assert "api_key" not in json.dumps(payload).lower()
    assert write_watchtower_status is app_status.write_app_status
    db.close()


def test_playback_error_message_hides_paths_and_control_characters():
    message = playback_error_message("Song\nName")
    assert "SongName" in message
    assert "C:\\" not in message


def test_queue_fifo_and_base_context_invariants_remain_in_source():
    source = Path("music_vault/app.py").read_text(encoding="utf-8")
    assert "self.manual_queue.append(track_id)" in source
    assert "queued_track_id = self.manual_queue.pop(0)" in source
    assert "capture_base_context=False" in source
    assert 'self.base_playback_context["current_track_id"] = track_id' in source


def test_acceptance_mode_skips_api_key_file_access(monkeypatch):
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")

    class NoFileAccess:
        def api_key_path(self):
            raise AssertionError("Acceptance mode must not inspect the API-key file.")

    assert MusicVaultWindow.read_saved_api_key(NoFileAccess()) == ""


def test_acceptance_status_mode_skips_api_key_file_access(monkeypatch):
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setattr(
        app_status,
        "youtube_api_key_path",
        lambda: (_ for _ in ()).throw(
            AssertionError("Acceptance status must not inspect the API-key file.")
        ),
    )

    assert app_status._api_ready() is False
