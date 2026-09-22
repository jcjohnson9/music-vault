from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace, MethodType

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
        {
            "sync": {
                "last_sync_status": "complete_with_issues",
                "last_sync_failed_count": 2,
            },
            "party_mode_active": True,
            "party_mode_preset": "aurora",
            "audio_reactivity_available": True,
        },
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["app_version"] == "1.1.0"
    assert payload["release_channel"] == "development"
    assert payload["sync"]["last_sync_status"] == "complete_with_issues"
    assert payload["sync"]["last_sync_failed_count"] == 2
    assert payload["party_mode_active"] is True
    assert payload["party_mode_preset"] == "aurora"
    assert payload["audio_reactivity_available"] is True
    assert "api_key" not in json.dumps(payload).lower()
    assert not {"pcm", "frequency", "samples", "monitor"} & payload.keys()
    assert write_watchtower_status is app_status.write_app_status
    db.close()


def test_playback_error_message_hides_paths_and_control_characters():
    message = playback_error_message("Song\nName")
    assert "SongName" in message
    assert "C:\\" not in message


def test_queue_fifo_and_base_context_invariants_execute_through_host_editor():
    context = {"track_ids": [10, 20], "current_track_id": 10}
    played = []
    host = SimpleNamespace(
        manual_queue=[], base_playback_context=context,
        db=SimpleNamespace(get_track=lambda track_id: {"id": track_id, "title": "Synthetic", "artist": "Example"}),
        update_queue_label=lambda: None, write_app_status=lambda: None,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *_: None),
        play_track_by_id=lambda track_id, **kwargs: played.append((track_id, kwargs)) or True,
    )
    host._manual_queue_editor = MethodType(MusicVaultWindow._manual_queue_editor, host)
    queue = host.manual_queue
    for track_id in (30, 40, 30):
        MusicVaultWindow.queue_track_by_id(host, track_id)
    assert host.manual_queue is queue and queue == [30, 40, 30]
    for _ in range(3):
        assert MusicVaultWindow.play_next_from_manual_queue(host)
    assert [track_id for track_id, _ in played] == [30, 40, 30]
    assert all(options == {"capture_base_context": False, "show_missing_warning": False} for _, options in played)
    assert queue == [] and host.base_playback_context is context
    assert context == {"track_ids": [10, 20], "current_track_id": 10}


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
