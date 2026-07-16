from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.service import MetadataAction, MetadataService


def _track(db: MusicVaultDB, tmp_path: Path) -> tuple[int, MetadataService]:
    media = tmp_path / "track.synthetic"
    media.write_bytes(b"synthetic")
    track_id = db.upsert_track(
        media,
        title="Source Title",
        artist="Source Artist",
        album="Source Album",
        year="2001",
    )
    return track_id, MetadataService(db)


def test_manual_locked_precedence_and_observation_retention(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    manual = service.apply_manual_patch(track_id, {"title": "Approved Title"})
    before_timestamp = manual.after.metadata_updated_at
    automatic = service.record_source_observations(
        track_id,
        provider="embedded",
        values={"title": "Changed Tag"},
    )
    assert not automatic.changed
    snapshot = service.snapshot(track_id)
    assert snapshot.value("title") == "Approved Title"
    assert snapshot.metadata_updated_at == before_timestamp
    assert any(item.value == "Changed Tag" for item in service.observations(track_id, "title"))
    db.close()


def test_embedded_beats_youtube_and_empty_never_erases(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    media = tmp_path / "track.synthetic"
    media.write_bytes(b"synthetic")
    track_id = db.upsert_track(media, title="Upload", source_kind="youtube")
    service = MetadataService(db)
    service.record_source_observations(
        track_id, provider="embedded", values={"title": "Embedded"}
    )
    service.record_source_observations(
        track_id, provider="youtube", values={"title": "Weaker"}
    )
    service.record_source_observations(
        track_id, provider="embedded", values={"title": None}
    )
    assert service.snapshot(track_id).value("title") == "Embedded"
    db.close()


def test_multi_field_save_is_atomic_grouped_and_materialized(tmp_path, monkeypatch):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    result = service.apply_actions(
        track_id,
        {
            "title": MetadataAction.set("Manual"),
            "album": MetadataAction.clear(),
            "release_date": MetadataAction.set("2024-02-29"),
        },
    )
    assert result.changed_fields == {"title", "album", "release_date"}
    rows = db.conn.execute(
        "SELECT DISTINCT change_group_id FROM track_metadata_history WHERE change_group_id=?",
        (result.change_group_id,),
    ).fetchall()
    assert len(rows) == 1
    track = db.get_track(track_id)
    assert track["title"] == "Manual"
    assert track["album"] is None
    assert track["release_date"] == "2024-02-29"
    assert track["year"] == "2024"
    for name in result.changed_fields:
        state = result.after.fields[name]
        assert state.is_manual and state.is_locked

    before = service.snapshot(track_id)
    original = service._write_history

    def fail_history(**kwargs):
        original(**kwargs)
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(service, "_write_history", fail_history)
    with pytest.raises(RuntimeError):
        service.apply_manual_patch(track_id, {"artist": "Rolled Back"})
    after = service.snapshot(track_id)
    assert after.value("artist") == before.value("artist")
    db.close()


def test_no_change_save_changes_nothing(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    before = service.snapshot(track_id)
    history_count = db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0]
    result = service.apply_actions(track_id, {})
    assert not result.changed
    assert service.snapshot(track_id).metadata_updated_at == before.metadata_updated_at
    assert db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0] == history_count
    db.close()


def test_clear_unlock_and_reset_are_distinct(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    cleared = service.apply_actions(track_id, {"album": MetadataAction.clear()})
    state = cleared.after.fields["album"]
    assert state.value is None and state.is_manual and state.is_locked
    service.record_source_observations(
        track_id, provider="embedded", values={"album": "New Automatic"}
    )
    assert service.snapshot(track_id).value("album") is None

    unlocked = service.unlock_fields(track_id, ["album"])
    assert unlocked.after.value("album") is None
    assert not unlocked.after.fields["album"].is_locked
    service.record_source_observations(
        track_id, provider="embedded", values={"album": "New Automatic"}
    )
    assert service.snapshot(track_id).value("album") == "New Automatic"

    service.apply_manual_patch(track_id, {"album": "Another Manual"})
    reset = service.reset_fields(track_id, ["album"])
    assert reset.after.value("album") == "New Automatic"
    assert not reset.after.fields["album"].is_manual
    assert not reset.after.fields["album"].is_locked
    db.close()


def test_title_cannot_clear_and_upload_date_is_never_release(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    with pytest.raises(ValueError):
        service.apply_actions(track_id, {"title": MetadataAction.clear()})
    service.record_source_observations(
        track_id,
        provider="youtube",
        values={"source_upload_date": "2024-03-02", "release_date": "2024"},
    )
    assert service.snapshot(track_id).value("release_date") == "2001"
    db.close()


def test_candidate_selected_fields_lock_and_ids_persist(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    result = service.apply_confirmed_candidate(
        track_id,
        {"title": "Confirmed", "artist": "Confirmed Artist"},
        recording_id="recording",
        release_id="release",
        confidence=97,
    )
    assert result.changed_fields == {"title", "artist"}
    assert result.after.fields["title"].provenance == "musicbrainz_confirmed"
    assert result.after.fields["title"].is_locked
    track = db.get_track(track_id)
    assert track["musicbrainz_recording_id"] == "recording"
    assert track["musicbrainz_release_id"] == "release"
    db.close()


def test_history_and_undo_restore_complete_state_without_touching_playlist(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    playlist_id = db.create_playlist("Synthetic")
    db.add_track_to_playlist(playlist_id, track_id)
    before = service.snapshot(track_id)
    changed = service.apply_manual_patch(
        track_id,
        {"artist": "Manual Artist", "release_date": "2020-05"},
    )
    original_groups = service.history_groups(track_id)
    assert original_groups[0].change_group_id == changed.change_group_id
    undone = service.undo_last_change(track_id)
    assert undone.changed_fields == {"artist", "release_date"}
    assert undone.after.value("artist") == before.value("artist")
    assert undone.after.value("release_date") == before.value("release_date")
    assert len(service.history_groups(track_id)) == len(original_groups) + 1
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
    db.close()


def test_approved_snapshot_and_review_signal(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    assert not service.field_needs_review(track_id, "title")
    service.record_source_observations(
        track_id, provider="youtube", values={"artist": "Source Artist"}
    )
    approved = service.approved_snapshot(track_id)
    assert approved.title == "Source Title"
    assert approved.path.endswith("track.synthetic")
    assert approved.release_date == "2001"
    db.close()


def test_upsert_is_atomic_and_commit_false_remains_caller_owned(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    db = MusicVaultDB(db_path)
    media = tmp_path / "track.synthetic"
    media.write_bytes(b"synthetic")

    with pytest.raises(ValueError):
        db.upsert_track(media, title="Title", release_date="invalid")
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0

    track_id = db.upsert_track(media, commit=False)
    MetadataService(db).apply_manual_patch(track_id, {"title": "Uncommitted"})
    with sqlite3.connect(db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    db.conn.rollback()

    original = MetadataService.record_source_observations

    def fail_observation(self, *args, **kwargs):
        raise RuntimeError("synthetic metadata failure")

    monkeypatch.setattr(MetadataService, "record_source_observations", fail_observation)
    with pytest.raises(RuntimeError):
        db.upsert_track(media, title="Rolled Back")
    monkeypatch.setattr(MetadataService, "record_source_observations", original)
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    db.close()


def test_new_track_persists_every_effective_field_state(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    media = tmp_path / "track.synthetic"
    media.write_bytes(b"synthetic")
    track_id = db.upsert_track(media, title="Title")
    rows = db.conn.execute(
        "SELECT field_name FROM track_metadata_fields WHERE track_id=?",
        (track_id,),
    ).fetchall()
    assert {row["field_name"] for row in rows} == {
        "title",
        "artist",
        "album",
        "album_artist",
        "release_date",
        "original_release_date",
        "version_type",
        "version_label",
        "artwork",
    }
    db.close()


def test_duplicate_observation_preserves_effective_reference_and_confidence(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    service.record_source_observations(
        track_id,
        provider="musicbrainz",
        values={"title": "Source Title"},
        provider_reference="recording-id",
        confidence=95,
    )
    before = service.snapshot(track_id)
    history_count = db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0]
    observation_count = db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_observations"
    ).fetchone()[0]

    result = service.record_source_observations(
        track_id,
        provider="musicbrainz",
        values={"title": "Source Title"},
    )

    state = result.after.fields["title"]
    assert not result.changed
    assert state.provider_reference == "recording-id"
    assert state.confidence == 95
    assert result.after.metadata_updated_at == before.metadata_updated_at
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0] == history_count
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_observations"
    ).fetchone()[0] == observation_count
    db.close()


def test_undo_does_not_mistake_later_null_fill_for_initial_import(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    track_id, service = _track(db, tmp_path)
    changed = service.record_source_observations(
        track_id,
        provider="embedded",
        values={"album_artist": "Later Album Artist"},
        reason="embedded_reimport",
    )
    assert changed.changed_fields == {"album_artist"}
    assert service.preview_undo(track_id).change_group_id == changed.change_group_id

    undone = service.undo_last_change(track_id)
    assert undone.changed_fields == {"album_artist"}
    assert undone.after.value("album_artist") is None
    db.close()


def test_oldest_group_is_undoable_when_it_is_not_an_initial_import(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    media = tmp_path / "track.synthetic"
    media.write_bytes(b"synthetic")
    track_id = db.upsert_track(media)
    service = MetadataService(db)
    changed = service.record_source_observations(
        track_id,
        provider="embedded",
        values={"album": "Observed Later"},
        reason="embedded_reimport",
    )
    assert len(service.history_groups(track_id)) == 1
    assert service.preview_undo(track_id).change_group_id == changed.change_group_id
    assert service.undo_last_change(track_id).after.value("album") is None
    db.close()


def test_legacy_metadata_update_routes_effective_fields_through_service(tmp_path):
    db = MusicVaultDB(tmp_path / "db.sqlite3")
    media = tmp_path / "track.synthetic"
    media.write_bytes(b"synthetic")
    track_id = db.upsert_track(media, title="Title")

    db.update_track_metadata(
        track_id,
        year="2004-02-29",
        cover_path=str(tmp_path / "cover.png"),
        duration_seconds=123,
    )

    service = MetadataService(db)
    snapshot = service.snapshot(track_id)
    track = db.get_track(track_id)
    assert snapshot.value("release_date") == "2004-02-29"
    assert snapshot.value("artwork") == str(tmp_path / "cover.png")
    assert snapshot.fields["release_date"].provenance == "embedded"
    assert track["release_date"] == "2004-02-29" and track["year"] == "2004"
    assert track["cover_path"] == str(tmp_path / "cover.png")
    assert track["duration_seconds"] == 123
    assert track["metadata_updated_at"] is not None
    assert {entry.field_name for entry in service.preview_undo(track_id).entries} == {
        "release_date",
        "artwork",
    }
    with pytest.raises(ValueError, match="owned by MetadataService"):
        db.update_track_metadata(track_id, metadata_updated_at="caller-value")
    db.close()
