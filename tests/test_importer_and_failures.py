from __future__ import annotations

from pathlib import Path

from music_vault.core.db import MusicVaultDB
from music_vault.core.importer import ImportSourceContext, import_file


def _metadata(year: str):
    return {
        "title": "Synthetic",
        "artist": "Tester",
        "album": "Fixture",
        "year": year,
        "duration_seconds": 12.0,
    }


def test_youtube_upload_date_does_not_become_release_year(tmp_path, monkeypatch):
    from music_vault.core import importer

    media = tmp_path / "Video [abcdefghijk].mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(importer, "read_audio_metadata", lambda _path: _metadata("20240302"))
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    assert import_file(
        db,
        media,
        ImportSourceContext("youtube", "abcdefghijk", "20240302"),
    )
    row = db.conn.execute("SELECT * FROM tracks").fetchone()
    assert row["year"] is None
    assert row["source_upload_date"] == "2024-03-02"
    assert row["source_kind"] == "youtube"
    assert row["source_video_id"] == "abcdefghijk"
    db.close()


def test_local_import_keeps_legitimate_release_year(tmp_path, monkeypatch):
    from music_vault.core import importer

    media = tmp_path / "Local.mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(importer, "read_audio_metadata", lambda _path: _metadata("1997"))
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    assert import_file(db, media)
    row = db.conn.execute("SELECT * FROM tracks").fetchone()
    assert row["year"] == "1997"
    assert row["source_kind"] is None
    db.close()


def test_failure_upsert_increments_attempt_and_resolves(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    kwargs = dict(
        playlist_id="playlist",
        playlist_title="Mix",
        video_id="abcdefghijk",
        title="Song",
        reason="Unavailable",
        error_category="download",
    )
    db.record_sync_failure(**kwargs)
    db.record_sync_failure(**{**kwargs, "reason": "Still unavailable"})
    row = db.list_sync_failures("unresolved")[0]
    assert row["attempt_count"] == 2
    assert row["reason"] == "Still unavailable"
    assert db.unresolved_failure_count() == 1
    db.resolve_sync_failure("abcdefghijk")
    assert db.unresolved_failure_count() == 0
    assert db.list_sync_failures("resolved")[0]["resolved_at"]
    db.close()


def test_legacy_failure_import_is_validated_and_one_time(tmp_path):
    legacy = tmp_path / "failed.txt"
    legacy.write_text("abcdefghijk\ninvalid\nabcdefghijk\n", encoding="utf-8")
    db = MusicVaultDB(tmp_path / "db.sqlite3", legacy_failure_file=legacy)
    assert db.unresolved_failure_count() == 1
    assert db.import_legacy_failures(legacy) == 0
    assert legacy.is_file()
    db.close()


def test_downloaded_view_uses_source_identity(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    custom = tmp_path / "custom-folder" / "Track.mp3"
    local = tmp_path / "youtube_downloads" / "Local.mp3"
    custom.parent.mkdir()
    local.parent.mkdir()
    custom.write_bytes(b"x")
    local.write_bytes(b"x")
    db.upsert_track(custom, source_kind="youtube", source_video_id="abcdefghijk")
    db.upsert_track(local)
    rows = db.list_downloaded_tracks()
    assert [Path(row["path"]).name for row in rows] == ["Track.mp3"]
    db.close()


def test_database_source_identity_requires_a_valid_local_file(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    media = tmp_path / "Song [abcdefghijk].mp3"
    media.write_bytes(b"synthetic")
    db.upsert_track(media, source_kind="youtube", source_video_id="abcdefghijk")
    assert db.existing_youtube_video_ids() == {"abcdefghijk"}
    media.unlink()
    assert db.existing_youtube_video_ids() == set()
    db.close()
