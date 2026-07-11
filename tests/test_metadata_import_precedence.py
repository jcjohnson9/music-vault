from __future__ import annotations

from pathlib import Path

from music_vault.core.db import MusicVaultDB
from music_vault.core.importer import ImportSourceContext, import_file, refresh_covers_for_library
from music_vault.metadata.service import MetadataService


def _metadata(
    *,
    title="Imported",
    artist="Embedded Artist",
    album="Embedded Album",
    album_artist="Album Artist",
    release_date="1998-04-03",
):
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "release_date": release_date,
        "year": release_date,
        "duration_seconds": 180,
    }


def test_local_import_records_embedded_observations(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "local.mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(importer, "read_audio_metadata", lambda _path: _metadata())
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    assert import_file(db, media)
    service = MetadataService(db)
    row = db.conn.execute("SELECT id FROM tracks").fetchone()
    snapshot = service.snapshot(row["id"])
    assert snapshot.value("album_artist") == "Album Artist"
    assert snapshot.value("release_date") == "1998-04-03"
    assert db.get_track(row["id"])["year"] == "1998"
    assert {item.provider for item in service.observations(row["id"])} == {"embedded"}
    db.close()


def test_youtube_import_keeps_upload_date_source_only(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "source [abcdefghijk].mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: _metadata(release_date="20240302"),
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media, ImportSourceContext("youtube", "abcdefghijk", "20240302"))
    row = db.conn.execute("SELECT * FROM tracks").fetchone()
    assert row["source_upload_date"] == "2024-03-02"
    assert row["release_date"] is None and row["year"] is None
    fields = {
        (item.field_name, item.value)
        for item in MetadataService(db).observations(row["id"])
    }
    assert ("source_upload_date", "2024-03-02") in fields
    assert ("release_date", "20240302") not in fields
    db.close()


def test_reimport_preserves_all_locked_manual_fields(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "local.mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(importer, "read_audio_metadata", lambda _path: _metadata())
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media)
    row = db.conn.execute("SELECT id FROM tracks").fetchone()
    service = MetadataService(db)
    service.apply_manual_patch(
        row["id"],
        {
            "title": "Manual Title",
            "artist": "Manual Artist",
            "album": "Manual Album",
            "release_date": "1984",
            "artwork": str(tmp_path / "manual.png"),
        },
    )
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: _metadata(
            title="Changed",
            artist="Changed",
            album="Changed",
            release_date="2020",
        ),
    )
    import_file(db, media)
    snapshot = service.snapshot(row["id"])
    assert snapshot.value("title") == "Manual Title"
    assert snapshot.value("artist") == "Manual Artist"
    assert snapshot.value("album") == "Manual Album"
    assert snapshot.value("release_date") == "1984"
    assert snapshot.value("artwork") == str(tmp_path / "manual.png")
    db.close()


def test_embedded_reimport_upgrades_unlocked_youtube_fallback(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "source.mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: _metadata(title="Upload Title", release_date="20240101"),
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media, ImportSourceContext("youtube", "abcdefghijk", "20240101"))
    track_id = db.conn.execute("SELECT id FROM tracks").fetchone()[0]
    # Explicitly change source classification for this synthetic precedence check.
    db.conn.execute("UPDATE tracks SET source_kind='local' WHERE id=?", (track_id,))
    db.conn.commit()
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: _metadata(title="Embedded Upgrade", release_date="1990"),
    )
    import_file(db, media)
    snapshot = MetadataService(db).snapshot(track_id)
    assert snapshot.value("title") == "Embedded Upgrade"
    assert snapshot.value("release_date") == "1990"
    db.close()


def test_existing_youtube_reimport_without_context_stays_source_only(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "source.mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: _metadata(release_date="20240302"),
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media, ImportSourceContext("youtube", "abcdefghijk", "20240302"))
    import_file(db, media)
    row = db.conn.execute("SELECT * FROM tracks").fetchone()
    assert row["source_kind"] == "youtube"
    assert row["release_date"] is None and row["year"] is None
    db.close()


def test_duplicate_observation_and_locked_refresh_artwork(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "local.mp3"
    media.write_bytes(b"synthetic")
    embedded_art = tmp_path / "embedded.png"
    embedded_art.write_bytes(b"art")
    monkeypatch.setattr(importer, "read_audio_metadata", lambda _path: _metadata())
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: str(embedded_art))
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media)
    track_id = db.conn.execute("SELECT id FROM tracks").fetchone()[0]
    service = MetadataService(db)
    initial_observations = db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_observations"
    ).fetchone()[0]
    import_file(db, media)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_observations"
    ).fetchone()[0] == initial_observations
    manual_art = tmp_path / "manual.png"
    service.apply_manual_patch(track_id, {"artwork": str(manual_art)})
    assert refresh_covers_for_library(db) == 0
    assert service.snapshot(track_id).value("artwork") == str(manual_art)
    db.close()


def test_source_kind_is_normalized_before_date_classification(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "source.mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: _metadata(release_date="2024"),
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media, ImportSourceContext("  YOUTUBE  ", "abcdefghijk", "2024"))
    row = db.conn.execute("SELECT * FROM tracks").fetchone()
    assert row["source_kind"] == "youtube"
    assert row["source_upload_date"] == "2024"
    assert row["release_date"] is None and row["year"] is None
    db.close()


def test_filename_title_has_distinct_low_priority_provenance(monkeypatch, tmp_path):
    from music_vault.core import importer

    media = tmp_path / "Filename Title.mp3"
    media.write_bytes(b"synthetic")
    metadata = _metadata(title="Filename Title")
    metadata["title_provenance"] = "filename"
    monkeypatch.setattr(importer, "read_audio_metadata", lambda _path: metadata)
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media)
    track_id = db.conn.execute("SELECT id FROM tracks").fetchone()[0]
    service = MetadataService(db)
    assert service.snapshot(track_id).fields["title"].provenance == "filename"
    assert {item.provider for item in service.observations(track_id, "title")} == {
        "filename"
    }
    group_count = db.conn.execute(
        "SELECT COUNT(DISTINCT change_group_id) FROM track_metadata_history"
    ).fetchone()[0]
    assert group_count == 1
    db.close()


def test_invalid_embedded_release_date_is_observed_but_not_materialized(
    monkeypatch,
    tmp_path,
):
    from music_vault.core import importer

    media = tmp_path / "local.mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: _metadata(release_date="1999-00-00"),
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    import_file(db, media)
    row = db.conn.execute("SELECT * FROM tracks").fetchone()
    service = MetadataService(db)
    assert row["release_date"] is None and row["year"] is None
    assert any(
        item.provider == "embedded" and item.value == "1999-00-00"
        for item in service.observations(row["id"], "release_date")
    )
    assert service.best_automatic_value(row["id"], "release_date") is None
    db.close()
