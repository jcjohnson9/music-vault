"""Saved review selections are explicit, revision-bound, atomic scalar edits."""
from dataclasses import FrozenInstanceError
import json
import sqlite3

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.materializer import StaleMetadataProposal, capture_metadata_state
from music_vault.metadata.service import MetadataAction, MetadataService


@pytest.fixture
def review(tmp_path):
    db = MusicVaultDB(tmp_path / "synthetic.sqlite3", backup_dir=tmp_path / "backups")
    track = db.upsert_track(tmp_path / "synthetic.flac", title="Current title", artist="Current artist", album="Current album")
    playlist = db.create_playlist("Synthetic playlist")
    db.add_track_to_playlist(playlist, track)
    db.listening.set_favorite(track, True)
    store = MetadataIntelligenceJobStore(db)
    item = store.enqueue_track(track)
    store.mark_item(item.id, "review", field_proposal={
        "title": "Proposed title", "artist": "Proposed artist", "album": "Proposed album", "release_date": "1994",
        "_current": {"title": "Obsolete saved title"},
        "musicbrainz_recording_id": "hidden-recording", "musicbrainz_release_id": "hidden-edition",
        "discogs_release_id": "hidden-discogs", "artist_credits": [{"artist_id": "hidden-artist"}],
    }, musicbrainz_recording_id="hidden-recording", musicbrainz_release_id="hidden-edition")
    try:
        yield db, track, item.id, MetadataIntelligenceService(db, {})
    finally:
        db.close()


def dump(db):
    return tuple(db.conn.iterdump())


def item_row(db, item):
    return db.conn.execute("SELECT * FROM metadata_intelligence_items WHERE id=?", (item,)).fetchone()


def test_prepare_is_immutable_read_only_and_uses_fresh_current_values(review):
    db, track, item, intelligence = review
    before = dump(db)
    selection = intelligence.prepare_review_fields(item)
    assert selection.track_id == track
    assert selection.current_values["title"] == "Current title"
    assert selection.values == {"title": "Proposed title", "artist": "Proposed artist", "album": "Proposed album", "release_date": "1994"}
    with pytest.raises(TypeError):
        selection.values["title"] = "Injected"
    with pytest.raises(TypeError):
        selection.current_values["title"] = "Injected"
    with pytest.raises(FrozenInstanceError):
        selection.track_id = 999
    assert dump(db) == before
    assert not db.conn.in_transaction


@pytest.mark.parametrize("change", ["credit", "context", "lock", "item", "job"])
def test_stale_graph_or_saved_decision_rejects_without_writes(review, change):
    db, track, item, intelligence = review
    selection = intelligence.prepare_review_fields(item)
    with db.conn:
        if change == "credit":
            db.conn.execute("UPDATE track_artist_credits SET is_locked=1 WHERE track_id=?", (track,))
        elif change == "context":
            db.conn.execute("INSERT INTO track_release_context(track_id,musicbrainz_release_group_id,updated_at) VALUES(?, 'new-family', 'synthetic')", (track,))
        elif change == "lock":
            db.conn.execute("UPDATE track_metadata_fields SET is_locked=1 WHERE track_id=? AND field_name='title'", (track,))
        elif change == "item":
            db.conn.execute("UPDATE metadata_intelligence_items SET field_proposal=? WHERE id=?", ('{"title":"Different proposal"}', item))
        else:
            db.conn.execute("UPDATE metadata_intelligence_jobs SET updated_at='changed-job-revision' WHERE id=?", (item_row(db, item)["job_id"],))
    before = dump(db)
    with pytest.raises(StaleMetadataProposal):
        intelligence.apply_review_fields(item, ["title"], expected_revision=selection.revision)
    assert dump(db) == before


def test_exact_selection_is_manual_locked_and_never_imports_hidden_provider_ids(review):
    db, track, item, intelligence = review
    selection = intelligence.prepare_review_fields(item)
    before = capture_metadata_state(db.conn, track)
    result = intelligence.apply_review_fields(item, ["title", "title"], expected_revision=selection.revision)
    assert result.changed_fields == {"title"}
    state = result.after.fields["title"]
    assert (state.value, state.provenance, state.is_manual, state.is_locked) == ("Proposed title", "manual", True, True)
    after = capture_metadata_state(db.conn, track)
    for name in ("musicbrainz_recording_id", "musicbrainz_release_id", "discogs_release_id", "discogs_master_id"):
        assert after["track"][name] == before["track"][name]
    for table in ("track_artist_credits", "track_release_context", "track_album_memberships", "artists", "canonical_albums"):
        assert after[table] == before[table]
    assert item_row(db, item)["applied_history_group"] == result.change_group_id
    assert item_row(db, item)["state"] == "applied"


@pytest.mark.parametrize("fields", [[], ["musicbrainz_recording_id"], ["title", "unavailable"], ["artist_credits"]])
def test_invalid_or_hidden_selected_fields_fail_atomically(review, fields):
    db, track, item, intelligence = review
    before = dump(db)
    with pytest.raises(ValueError):
        intelligence.apply_review_fields(item, fields)
    assert dump(db) == before


def test_artist_album_review_has_full_structured_public_undo(review):
    db, track, item, intelligence = review
    metadata = MetadataService(db)
    before = capture_metadata_state(db.conn, track)
    memberships = [tuple(row) for row in db.conn.execute("SELECT * FROM playlist_tracks")]
    result = intelligence.apply_review_fields(item, ["title", "artist", "album"])
    assert metadata.preview_undo(track).change_group_id == result.change_group_id
    assert metadata.preview_undo(track).reason == "metadata_intelligence_review_selection"
    metadata.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM playlist_tracks")] == memberships
    assert db.listening.is_favorite(track)


@pytest.mark.parametrize("stage", ["item", "job"])
def test_late_item_or_job_failure_rolls_back_fields_graph_history_and_journal(review, monkeypatch, stage):
    db, track, item, intelligence = review
    if stage == "item":
        db.conn.execute("CREATE TEMP TRIGGER reject_review_update BEFORE UPDATE ON metadata_intelligence_items BEGIN SELECT RAISE(ABORT, 'injected item failure'); END")
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("injected job failure")
        monkeypatch.setattr(MetadataIntelligenceJobStore, "_refresh_job", fail)
    before = dump(db)
    with pytest.raises((RuntimeError, sqlite3.IntegrityError), match="injected"):
        intelligence.apply_review_fields(item, ["artist", "album"])
    assert dump(db) == before


def test_caller_owned_transaction_stays_open_and_can_rollback_everything(review):
    db, track, item, intelligence = review
    before = dump(db)
    db.conn.execute("BEGIN")
    selection = intelligence.prepare_review_fields(item)
    assert db.conn.in_transaction
    intelligence.apply_review_fields(item, ["artist"], expected_revision=selection.revision)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert dump(db) == before


def test_failed_nested_review_preserves_earlier_caller_work(review, monkeypatch):
    db, track, item, intelligence = review
    db.conn.execute("BEGIN")
    db.conn.execute("UPDATE tracks SET title='Caller pending title' WHERE id=?", (track,))
    before = dump(db)
    def fail(*args, **kwargs):
        raise RuntimeError("injected refresh failure")
    monkeypatch.setattr(MetadataIntelligenceJobStore, "_refresh_job", fail)
    with pytest.raises(RuntimeError, match="injected"):
        intelligence.apply_review_fields(item, ["artist"])
    assert db.conn.in_transaction and dump(db) == before
    db.conn.rollback()


def test_noop_completion_does_not_claim_old_group_or_churn_metadata(review):
    db, track, item, intelligence = review
    metadata = MetadataService(db)
    prior = metadata.apply_manual_actions(track, {"title": MetadataAction.set("Proposed title")})
    with db.conn:
        db.conn.execute("UPDATE metadata_intelligence_items SET applied_history_group=? WHERE id=?", (prior.change_group_id, item))
    before = capture_metadata_state(db.conn, track)
    tables = ("track_metadata_history", "metadata_materializations", "metadata_evidence_bundles", "track_metadata_observations")
    rows = {table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")] for table in tables}
    result = intelligence.apply_review_fields(item, ["title"])
    assert not result.changed and result.change_group_id is None
    assert capture_metadata_state(db.conn, track) == before
    assert {table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")] for table in tables} == rows
    saved = item_row(db, item)
    assert saved["state"] == "applied" and saved["applied_history_group"] is None
    assert json.loads(saved["field_proposal"])["_review_previous_applied_history_groups"] == [prior.change_group_id]


def test_invalid_saved_values_are_not_offered_as_choices(review):
    db, track, item, intelligence = review
    with db.conn:
        db.conn.execute("UPDATE metadata_intelligence_items SET field_proposal=? WHERE id=?", (json.dumps({
            "title": "  Valid title  ", "artist": {"nested": "invalid"}, "album": True,
            "release_date": "not-a-date", "version_type": "not-a-version", "album_artist": ["nested"],
        }), item))
    assert intelligence.prepare_review_fields(item).values == {"title": "Valid title"}


def test_legacy_saved_proposal_uses_fresh_explicit_review_not_stored_baseline(review):
    db, track, item, intelligence = review
    MetadataService(db).apply_manual_actions(track, {"title": MetadataAction.set("Fresh manual title")})
    selection = intelligence.prepare_review_fields(item)
    assert selection.current_values["title"] == "Fresh manual title"
    assert selection.values["title"] == "Proposed title"
    result = intelligence.apply_review_fields(item, ["title"], expected_revision=selection.revision)
    assert result.after.value("title") == "Proposed title"
    assert db.get_track(track)["musicbrainz_recording_id"] is None
