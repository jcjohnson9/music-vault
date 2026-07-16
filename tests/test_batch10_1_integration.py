from __future__ import annotations

import json
from pathlib import Path

from music_vault.core import app_status, importer
from music_vault.core.db import MusicVaultDB
from music_vault.core.playback_state import config_for_persistence
from music_vault.metadata.intelligence_settings import (
    DiscogsTokenStore,
    normalize_metadata_intelligence_settings,
)
from tools.release.release_common import FORBIDDEN_EXACT_NAMES
from tools.security.pre_public_commit_check import _path_violations


def _synthetic_audio_metadata() -> dict:
    return {
        "title": "Synthetic Artist - Synthetic Song",
        "artist": "Synthetic Uploader",
        "album": None,
        "album_artist": None,
        "release_date": None,
        "duration_seconds": 123.0,
    }


def test_import_commits_then_enqueues_new_canonical_track_once(
    tmp_path: Path,
    monkeypatch,
):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    path = tmp_path / "track.mp3"
    path.write_bytes(b"synthetic-not-decoded")
    monkeypatch.setattr(importer, "read_audio_metadata", lambda _path: _synthetic_audio_metadata())
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)

    assert importer.import_file(db, path) is True
    assert importer.import_file(db, path) is True

    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM metadata_intelligence_items"
    ).fetchone()[0] == 1
    credit = db.conn.execute(
        """
        SELECT artist.display_name, credit.role
        FROM track_artist_credits credit
        JOIN artists artist ON artist.id=credit.artist_id
        """
    ).fetchone()
    assert tuple(credit) == ("Synthetic Uploader", "primary")
    db.close()


def test_discogs_token_is_local_only_and_config_aliases_are_removed(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", raising=False)
    token_file = tmp_path / "data" / "discogs_token.txt"
    store = DiscogsTokenStore(token_file)
    store.save("synthetic-personal-token")
    assert store.configured() is True
    persisted = config_for_persistence(
        {
            "metadata_intelligence_enabled": True,
            "discogs_token": "synthetic-personal-token",
            "discogs_personal_token": "synthetic-personal-token",
            "discogs_api_token": "synthetic-personal-token",
        }
    )
    assert persisted == {"metadata_intelligence_enabled": True}

    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    assert store.read() == ""
    assert store.configured() is False
    monkeypatch.delenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS")
    assert store.remove() is True
    assert not token_file.exists()


def test_metadata_settings_require_literal_boolean_and_versioned_consent():
    settings = normalize_metadata_intelligence_settings(
        {
            "metadata_intelligence_enabled": "true",
            "metadata_discogs_enabled": 1,
            "metadata_writeback_enabled": True,
            "metadata_fill_missing_artwork_enabled": True,
            "metadata_intelligence_consent_version": 0,
            "metadata_discogs_consent_version": 0,
        }
    )
    assert settings["metadata_intelligence_enabled"] is False
    assert settings["metadata_discogs_enabled"] is False
    assert settings["metadata_writeback_enabled"] is False
    assert settings["metadata_fill_missing_artwork_enabled"] is False


def test_app_status_exports_only_metadata_intelligence_aggregates(
    tmp_path: Path,
    monkeypatch,
):
    data = tmp_path / "data"
    monkeypatch.setattr(app_status, "project_root", lambda: tmp_path)
    monkeypatch.setattr(app_status, "data_dir", lambda: data)
    monkeypatch.setattr(app_status, "app_status_path", lambda: data / "music_vault_status.json")
    monkeypatch.setattr(app_status, "config_path", lambda: data / "config.json")
    monkeypatch.setattr(app_status, "youtube_api_key_path", lambda: data / "missing-youtube.txt")
    monkeypatch.setattr(app_status, "discogs_token_path", lambda: data / "missing-discogs.txt")
    monkeypatch.setattr(app_status, "path_resolution_source", lambda: "synthetic")
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_id = db.upsert_track(tmp_path / "track.mp3", title="Private Synthetic Title")
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore

    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    status_path = app_status.write_app_status(
        db,
        {
            "download_folder": str(tmp_path / "downloads"),
            "metadata_intelligence_enabled": True,
            "metadata_intelligence_consent_version": 1,
        },
    )
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["metadata_intelligence_enabled"] is True
    assert payload["metadata_intelligence_total"] == 1
    assert payload["metadata_intelligence_analyzed"] == 0
    assert payload["discogs_ready"] is False
    serialized = json.dumps(payload)
    assert "Private Synthetic Title" not in serialized
    for forbidden in ("discogs_release_id", "query", "uploader", "image_url", "token"):
        assert forbidden not in serialized.casefold()
    db.close()


def test_publication_and_release_gates_reject_discogs_token_file():
    violations = {rule for rule, _remediation in _path_violations("data/discogs_token.txt")}
    assert "private runtime data path" in violations
    assert "private Music Vault runtime file" in violations
    assert "discogs_token.txt" in FORBIDDEN_EXACT_NAMES


def test_acceptance_no_secrets_guard_blocks_automatic_provider_wake(monkeypatch):
    from music_vault.app import MusicVaultWindow

    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    # The guard must return before touching config, task runners, token stores,
    # or provider factories.  A bare object makes that boundary explicit.
    assert MusicVaultWindow.wake_metadata_intelligence(object()) is None
