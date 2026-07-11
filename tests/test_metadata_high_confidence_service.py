from __future__ import annotations

from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.service import MetadataService


def _track(
    tmp_path: Path,
    *,
    source_kind: str | None = "youtube",
) -> tuple[MusicVaultDB, int, MetadataService]:
    database = MusicVaultDB(tmp_path / "library.sqlite3")
    media = tmp_path / "synthetic.media"
    media.write_bytes(b"synthetic")
    track_id = database.upsert_track(
        media,
        title="Source Title",
        artist="Source Artist",
        album="Source Album",
        album_artist="Source Album Artist",
        release_date="2001",
        source_kind=source_kind,
        source_upload_date="2025-06-07" if source_kind == "youtube" else None,
    )
    return database, track_id, MetadataService(database)


def _state_tuple(state) -> tuple[object, ...]:
    return (
        state.value,
        state.provenance,
        state.provider_reference,
        state.confidence,
        state.is_manual,
        state.is_locked,
    )


def _private_snapshot(snapshot) -> dict[str, object]:
    return {
        "musicbrainz_recording_id": snapshot.musicbrainz_recording_id,
        "musicbrainz_release_id": snapshot.musicbrainz_release_id,
        "fields": {
            name: {
                "value": state.value,
                "provenance": state.provenance,
                "provider_reference": state.provider_reference,
                "confidence": state.confidence,
                "is_manual": state.is_manual,
                "is_locked": state.is_locked,
            }
            for name, state in snapshot.fields.items()
        },
    }


def test_high_confidence_precedence_is_unlocked_editable_and_resettable(tmp_path):
    database, track_id, service = _track(tmp_path)
    service.record_source_observations(
        track_id,
        provider="embedded",
        values={"title": "Embedded Title"},
    )
    applied = service.apply_high_confidence_candidate(
        track_id,
        {"title": "Canonical Title"},
        recording_id="recording-high",
        release_id=None,
        confidence=98,
    )
    high = applied.after.fields["title"]
    assert _state_tuple(high) == (
        "Canonical Title",
        "musicbrainz_high_confidence",
        "recording-high",
        98,
        False,
        False,
    )

    service.record_source_observations(
        track_id,
        provider="embedded",
        values={"title": "Later Embedded"},
    )
    service.record_source_observations(
        track_id,
        provider="youtube",
        values={"title": "Later Upload"},
    )
    assert service.snapshot(track_id).value("title") == "Canonical Title"

    manual = service.apply_manual_patch(track_id, {"title": "Manual Title"})
    assert manual.after.fields["title"].is_locked
    blocked = service.apply_high_confidence_candidate(
        track_id,
        {"title": "Should Not Replace Manual"},
        recording_id="other-recording",
        release_id=None,
        confidence=100,
    )
    assert "title" not in blocked.changed_fields
    assert blocked.after.value("title") == "Manual Title"

    reset = service.reset_fields(track_id, ["title"])
    assert _state_tuple(reset.after.fields["title"]) == _state_tuple(high)
    edited = service.apply_manual_patch(track_id, {"title": "Edited Again"})
    assert edited.after.value("title") == "Edited Again"
    assert edited.after.fields["title"].provenance == "manual"
    database.close()


def test_manual_confirmed_and_other_locked_fields_block_high_confidence(tmp_path):
    database, track_id, service = _track(tmp_path)
    service.apply_manual_patch(track_id, {"album": "Manual Album"})
    confirmed = service.apply_confirmed_candidate(
        track_id,
        {
            "artist": "Confirmed Artist",
            "album_artist": "Confirmed Album Artist",
        },
        recording_id="confirmed-recording",
        release_id="confirmed-release",
        confidence=99,
    )
    assert confirmed.after.fields["album_artist"].provider_reference == "confirmed-release"
    service.lock_fields(track_id, ["release_date"])

    before = service.snapshot(track_id)
    blocked = service.apply_high_confidence_candidate(
        track_id,
        {
            "artist": "Automatic Artist",
            "album": "Automatic Album",
            "release_date": "2020",
        },
        recording_id="automatic-recording",
        release_id="automatic-release",
        confidence=100,
    )
    after = blocked.after
    assert not blocked.changed
    for field_name in ("artist", "album", "release_date"):
        assert _state_tuple(after.fields[field_name]) == _state_tuple(
            before.fields[field_name]
        )
    assert after.musicbrainz_recording_id == confirmed.after.musicbrainz_recording_id
    assert after.musicbrainz_release_id == confirmed.after.musicbrainz_release_id
    database.close()


def test_high_confidence_ids_field_confidence_and_history_are_one_atomic_group(
    tmp_path,
    monkeypatch,
):
    database, track_id, service = _track(tmp_path, source_kind=None)
    values = {
        "title": "Canonical Title",
        "artist": "Canonical Artist",
        "album": "Canonical Album",
        "album_artist": "Canonical Album Artist",
        "release_date": "1998-03-04",
    }
    result = service.apply_high_confidence_candidate(
        track_id,
        values,
        recording_id="recording-id",
        release_id="release-id",
        confidence=97.5,
    )

    assert result.changed_fields == frozenset(values)
    assert result.change_group_id
    for field_name in values:
        state = result.after.fields[field_name]
        assert state.confidence == 97.5
        assert state.provenance == "musicbrainz_high_confidence"
        assert not state.is_locked and not state.is_manual
        expected_reference = (
            "release-id"
            if field_name in {"album", "album_artist", "release_date"}
            else "recording-id"
        )
        assert state.provider_reference == expected_reference
    assert result.after.musicbrainz_recording_id == "recording-id"
    assert result.after.musicbrainz_release_id == "release-id"
    rows = database.conn.execute(
        """
        SELECT change_group_id, actor, reason, new_confidence, new_is_locked
        FROM track_metadata_history WHERE change_group_id=?
        """,
        (result.change_group_id,),
    ).fetchall()
    assert len(rows) == len(values)
    assert {tuple(row) for row in rows} == {
        (result.change_group_id, "remediation", "musicbrainz_high_confidence", 97.5, 0)
    }

    before_failure = service.snapshot(track_id)
    history_count = database.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0]
    original_write_history = service._write_history

    def fail_history(**kwargs):
        original_write_history(**kwargs)
        raise RuntimeError("synthetic history failure")

    monkeypatch.setattr(service, "_write_history", fail_history)
    with pytest.raises(RuntimeError, match="synthetic history failure"):
        service.apply_high_confidence_candidate(
            track_id,
            {"title": "Must Roll Back"},
            recording_id="must-not-persist",
            release_id="must-not-persist",
            confidence=99,
        )
    after_failure = service.snapshot(track_id)
    assert _state_tuple(after_failure.fields["title"]) == _state_tuple(
        before_failure.fields["title"]
    )
    assert after_failure.musicbrainz_recording_id == "recording-id"
    assert after_failure.musicbrainz_release_id == "release-id"
    assert database.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0] == history_count
    database.close()


def test_source_upload_date_never_becomes_release_date_during_high_confidence_flow(
    tmp_path,
):
    database, track_id, service = _track(tmp_path, source_kind=None)
    before = service.snapshot(track_id)
    service.record_source_observations(
        track_id,
        provider="youtube",
        values={
            "source_upload_date": "2026-01-02",
            "release_date": "2026-01-02",
        },
    )
    after_observation = service.snapshot(track_id)
    assert after_observation.source_upload_date == before.source_upload_date
    assert after_observation.value("release_date") == "2001"

    with pytest.raises(ValueError, match="Unsupported metadata field"):
        service.apply_high_confidence_candidate(
            track_id,
            {
                "title": "Must Roll Back",
                "source_upload_date": "2026-01-02",
            },
            recording_id="must-not-persist",
            release_id="must-not-persist",
            confidence=99,
        )
    after_failure = service.snapshot(track_id)
    assert after_failure.value("title") == before.value("title")
    assert after_failure.value("release_date") == "2001"
    assert after_failure.musicbrainz_recording_id is None
    database.close()


def test_restore_remediation_snapshot_restores_ids_provenance_confidence_and_locks(
    tmp_path,
):
    database, track_id, service = _track(tmp_path, source_kind=None)
    service.apply_manual_patch(track_id, {"album": "Locked Manual Album"})
    service.apply_confirmed_candidate(
        track_id,
        {"artist": "Locked Confirmed Artist"},
        recording_id="original-recording",
        release_id="original-release",
        confidence=99,
    )
    before = service.snapshot(track_id)
    private_snapshot = _private_snapshot(before)

    service.unlock_fields(track_id, ["album"])
    service.apply_manual_patch(track_id, {"artist": "Temporary Manual Artist"})
    service.apply_high_confidence_candidate(
        track_id,
        {"title": "Temporary Canonical Title", "release_date": "2010-11-12"},
        recording_id="temporary-recording",
        release_id="temporary-release",
        confidence=98,
    )
    changed = service.snapshot(track_id)
    assert changed.musicbrainz_recording_id == "temporary-recording"
    assert changed.musicbrainz_release_id == "temporary-release"

    restored = service.restore_remediation_snapshot(track_id, private_snapshot)
    assert restored.change_group_id
    after = restored.after
    for field_name, state in before.fields.items():
        assert _state_tuple(after.fields[field_name]) == _state_tuple(state)
    assert after.musicbrainz_recording_id == "original-recording"
    assert after.musicbrainz_release_id == "original-release"
    assert database.get_track(track_id)["title"] == before.value("title")
    group = service.history_groups(track_id)[0]
    assert group.change_group_id == restored.change_group_id
    assert group.actor == "remediation_rollback"
    assert group.reason == "remediation_rollback"
    assert {entry.field_name for entry in group.entries} == restored.changed_fields
    database.close()
