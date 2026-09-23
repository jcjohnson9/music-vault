"""Synthetic transaction acceptance for the reviewed metadata write boundary."""
from __future__ import annotations

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService
from music_vault.metadata.ensemble import FieldAction, build_metadata_ensemble
from music_vault.metadata.evidence import normalize_candidate
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.materializer import (
    StaleMetadataProposal, capture_metadata_state, materialize_proposal, state_fingerprint,
    undo_materialization,
)
from music_vault.metadata.musicbrainz_enricher import MetadataCandidate
from music_vault.metadata.providers import ProviderArtistCredit
from music_vault.metadata.resolver import resolve_metadata
from music_vault.metadata.schema import EDITABLE_METADATA_FIELDS
from music_vault.metadata.service import AutomaticMetadataField, MetadataService


def _dump(conn):
    return tuple(conn.iterdump())


def _rows(conn, table):
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.fixture
def library(tmp_path):
    db = MusicVaultDB(tmp_path / "synthetic.sqlite3", backup_dir=tmp_path / "backups")
    track = db.upsert_track(tmp_path / "one.flac", title="Before", artist="Before Artist", duration_seconds=200)
    other = db.upsert_track(tmp_path / "other.flac", title="Other", artist="Other Artist")
    playlist = db.create_playlist("Synthetic playlist")
    db.add_track_to_playlist(playlist, track)
    db.add_track_to_playlist(playlist, other)
    db.listening.set_favorite(track, True)
    db.listening.save_event(dict(
        event_id="synthetic-event", run_id="synthetic-run", track_id=track, recorded_track_id=track,
        title_at_start="Before", artist_at_start="Before Artist", album_at_start=None,
        started_at="2026-01-01T00:00:00Z", last_observed_at="2026-01-01T00:00:01Z",
        ended_at=None, qualified_at=None, listened_ms=1000, duration_ms=200000,
        end_reason=None, playback_origin="manual", context_kind="playlist", context_playlist_id=playlist,
        context_label="Synthetic playlist", update_sequence=1,
    ))
    try:
        yield db, track, other
    finally:
        db.close()


def _candidate():
    return MetadataCandidate(
        "Accepted Song", "Stage Alias", "Accepted Album", "1992", "recording-1", "release-1", 99,
        release_group_id="family-1", duration_seconds=200, album_artist="Stage Alias",
        artist_credits=(ProviderArtistCredit(
            "Stage Alias", artist_id="artist-1", provider="musicbrainz",
            canonical_name="Canonical Artist", credited_as="Stage Alias", entity_type="person",
        ),),
    )


def _analyze(db, track, candidate=None):
    candidate = candidate or _candidate()
    metadata = MetadataService(db)
    snapshot = metadata.snapshot(track)
    before = capture_metadata_state(db.conn, track)
    current = {name: field.value for name, field in snapshot.fields.items()}
    current.update(before["track"])
    if before["track_release_context"]:
        current.update(before["track_release_context"][0])
    locked = frozenset(name for name, value in snapshot.fields.items() if value.is_manual or value.is_locked)
    ensemble = build_metadata_ensemble(current=current, musicbrainz_candidates=(candidate,), locked_fields=locked)
    proposal = resolve_metadata(
        track_id=track, expected_fingerprint=state_fingerprint(before), current_values=current,
        locked_fields=locked, protected_credits=any(row["is_manual"] or row["is_locked"] for row in before["track_artist_credits"]),
        ensemble=ensemble, evidence=(normalize_candidate(candidate),),
    )
    automatic = {
        field.field_name: AutomaticMetadataField(value=field.value, confidence=field.score, provider=field.source,
                                                provider_reference=field.provider_reference, conflict=field.conflict)
        for field in ensemble.fields if field.field_name in EDITABLE_METADATA_FIELDS and field.field_name != "artwork"
        and field.value not in (None, "") and field.action is not FieldAction.REVIEW
    }
    return metadata, proposal, ensemble, automatic


def _apply(db, track, analysis):
    # This particular pure write method needs no initialized provider/token objects.
    service = object.__new__(MetadataIntelligenceService)
    return service._materialize_analysis(db, track, *analysis)


def _unrelated(db):
    names = ("playlists", "playlist_tracks", "playlist_track_origins", "track_favorites", "listening_events", "source_track_identities")
    return {name: _rows(db.conn, name) for name in names}


def test_materialization_preserves_memberships_and_maps_musicbrainz_identity_only(library):
    db, track, _other = library
    unrelated = _unrelated(db)
    result = _apply(db, track, _analyze(db, track))
    row = db.get_track(track)
    assert result.changed
    assert row["title"] == "Accepted Song"
    assert row["musicbrainz_recording_id"] == "recording-1"
    assert row["musicbrainz_release_id"] == "release-1"
    assert row["discogs_release_id"] is None
    credits = ArtistCreditService(db).track_credits(track)
    assert len(credits) == 1
    assert credits[0].artist.musicbrainz_artist_id == "artist-1"
    assert credits[0].artist.discogs_artist_id is None
    assert credits[0].credited_as == "Stage Alias"
    assert credits[0].artist.display_name == "Canonical Artist"
    assert _unrelated(db) == unrelated
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("field", ["artist", "album", "album_artist"])
def test_new_manual_blank_after_analysis_rejects_before_any_write(library, field):
    db, track, _other = library
    analysis = _analyze(db, track)
    MetadataService(db).apply_manual_patch(track, {field: None})
    before = _dump(db.conn)
    with pytest.raises(StaleMetadataProposal, match="changed_since_analysis"):
        _apply(db, track, analysis)
    assert _dump(db.conn) == before
    assert MetadataService(db).snapshot(track).fields[field].is_manual


@pytest.mark.parametrize("stage", ["fields", "identity", "credits", "album"])
def test_injected_failure_rolls_back_every_table_and_evidence_journal(library, monkeypatch, stage):
    db, track, _other = library
    analysis = _analyze(db, track)
    before = _dump(db.conn)
    owner, name = {
        "fields": (MetadataService, "apply_automatic_fields"),
        "identity": (MetadataIntelligenceService, "_apply_musicbrainz_identity"),
        "credits": (ArtistCreditService, "replace_track_credits"),
        "album": (MetadataService, "finalize_identity"),
    }[stage]
    original = getattr(owner, name)
    def fail_after(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected stage failure")
    monkeypatch.setattr(owner, name, fail_after)
    with pytest.raises(RuntimeError, match="injected stage"):
        _apply(db, track, analysis)
    assert _dump(db.conn) == before
    assert not db.conn.in_transaction


def test_identical_proposal_retry_is_exact_noop_including_all_timestamps(library):
    db, track, _other = library
    analysis = _analyze(db, track)
    _apply(db, track, analysis)
    before = _dump(db.conn)
    assert not _apply(db, track, analysis).changed
    assert _dump(db.conn) == before
    assert db.conn.execute("SELECT COUNT(*) FROM metadata_materializations").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM metadata_evidence_bundles").fetchone()[0] == 1


def test_caller_owned_outer_transaction_is_not_committed(library):
    db, track, _other = library
    analysis = _analyze(db, track)
    before = _dump(db.conn)
    db.conn.execute("BEGIN IMMEDIATE")
    db.conn.execute("INSERT INTO app_meta(key,value) VALUES ('synthetic_owner','pending')")
    _apply(db, track, analysis)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert _dump(db.conn) == before


def test_failed_nested_materialization_preserves_callers_earlier_write(library, monkeypatch):
    db, track, _other = library
    analysis = _analyze(db, track)
    db.conn.execute("BEGIN IMMEDIATE")
    db.conn.execute("INSERT INTO app_meta(key,value) VALUES ('synthetic_owner','pending')")
    before = _dump(db.conn)
    monkeypatch.setattr(MetadataService, "finalize_identity", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        _apply(db, track, analysis)
    assert db.conn.in_transaction
    assert _dump(db.conn) == before
    db.conn.rollback()


def test_credited_alias_does_not_rename_shared_artist_entity(library):
    db, track, other = library
    ArtistCreditService(db).replace_track_credits(other, [ArtistCreditInput("Canonical Artist", musicbrainz_artist_id="artist-1", entity_type="person")], provenance="musicbrainz")
    shared_before = tuple(db.conn.execute("SELECT * FROM artists WHERE musicbrainz_artist_id='artist-1'").fetchone())
    other_before = capture_metadata_state(db.conn, other)
    _apply(db, track, _analyze(db, track))
    assert tuple(db.conn.execute("SELECT * FROM artists WHERE musicbrainz_artist_id='artist-1'").fetchone()) == shared_before
    assert capture_metadata_state(db.conn, other) == other_before
    assert db.get_track(track)["artist"] == "Stage Alias"


def test_final_album_only_after_provider_identity_and_no_intermediate_fallbacks(library, monkeypatch):
    from music_vault.metadata import canonical_albums
    db, track, _other = library
    before_artist_ids = {row[0] for row in db.conn.execute("SELECT id FROM artists")}
    before_album_ids = {row[0] for row in db.conn.execute("SELECT id FROM canonical_albums")}
    observations = []
    original = canonical_albums.upsert_track_canonical_album
    def observe(conn, track_id):
        observations.append((conn.execute("SELECT musicbrainz_release_id FROM tracks WHERE id=?", (track_id,)).fetchone()[0],
                             tuple(credit.artist.musicbrainz_artist_id for credit in ArtistCreditService(conn).track_credits(track_id))))
        return original(conn, track_id)
    monkeypatch.setattr(canonical_albums, "upsert_track_canonical_album", observe)
    _apply(db, track, _analyze(db, track))
    assert observations == [("release-1", ("artist-1",))]
    new_artists = [row for row in db.conn.execute("SELECT * FROM artists") if row["id"] not in before_artist_ids]
    new_albums = [row for row in db.conn.execute("SELECT * FROM canonical_albums") if row["id"] not in before_album_ids]
    assert len(new_artists) == 1 and new_artists[0]["musicbrainz_artist_id"] == "artist-1"
    assert len(new_albums) == 1


def test_structured_undo_restores_exact_track_graph_preserves_history_and_memberships(library):
    db, track, _other = library
    before = capture_metadata_state(db.conn, track)
    unrelated = _unrelated(db)
    result = _apply(db, track, _analyze(db, track))
    history = _rows(db.conn, "track_metadata_history")
    assert undo_materialization(db.conn, result.change_group_id)
    assert capture_metadata_state(db.conn, track) == before
    assert _unrelated(db) == unrelated
    assert _rows(db.conn, "track_metadata_history") == history
    assert not undo_materialization(db.conn, result.change_group_id)
    assert db.conn.execute("SELECT undone_at FROM metadata_materializations").fetchone()[0]


def test_structured_undo_refuses_newer_edit_without_changing_any_rows(library):
    db, track, _other = library
    result = _apply(db, track, _analyze(db, track))
    MetadataService(db).apply_manual_patch(track, {"title": "Newer manual title"})
    before = _dump(db.conn)
    with pytest.raises(StaleMetadataProposal, match="changed_since_materialization"):
        undo_materialization(db.conn, result.change_group_id)
    assert _dump(db.conn) == before


def test_undone_proposal_cannot_silently_reapply(library):
    db, track, _other = library
    analysis = _analyze(db, track)
    result = _apply(db, track, analysis)
    assert undo_materialization(db.conn, result.change_group_id)
    before = _dump(db.conn)
    with pytest.raises(StaleMetadataProposal, match="already_changed"):
        _apply(db, track, analysis)
    assert _dump(db.conn) == before


def test_public_undo_routes_to_whole_structured_graph(library):
    db, track, _other = library
    metadata = MetadataService(db)
    before = capture_metadata_state(db.conn, track)
    result = _apply(db, track, _analyze(db, track))
    assert metadata.preview_undo(track).change_group_id == result.change_group_id
    assert metadata.undo_last_change(track).changed
    assert capture_metadata_state(db.conn, track) == before
    next_undo = metadata.preview_undo(track)
    assert next_undo is None or next_undo.change_group_id != result.change_group_id


def test_identity_only_acceptance_is_reported_and_available_to_public_undo(library):
    db, track, _other = library
    metadata = MetadataService(db)
    metadata.apply_manual_patch(track, {
        "title": "Accepted Song", "artist": "Stage Alias", "album": "Accepted Album",
        "album_artist": "Stage Alias", "release_date": "1992",
    })
    before = capture_metadata_state(db.conn, track)
    result = _apply(db, track, _analyze(db, track))
    assert db.get_track(track)["musicbrainz_recording_id"] == "recording-1"
    assert result.change_group_id is not None, "Structured-only acceptance needs an accessible undo group"
    assert metadata.preview_undo(track).change_group_id == result.change_group_id
    metadata.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before


def _seed_exclusive_unknown_artist(db, track):
    ArtistCreditService(db).replace_track_credits(
        track, [ArtistCreditInput("Canonical Artist", musicbrainz_artist_id="artist-1")],
        provenance="musicbrainz",
    )


def test_undo_restores_exclusive_catalogue_entity_promotion(library):
    db, track, _other = library
    _seed_exclusive_unknown_artist(db, track)
    before = capture_metadata_state(db.conn, track)
    result = _apply(db, track, _analyze(db, track))
    assert ArtistCreditService(db).track_credits(track)[0].artist.entity_type == "person"
    assert undo_materialization(db.conn, result.change_group_id)
    assert capture_metadata_state(db.conn, track) == before


def test_undo_refuses_promoted_entity_newly_shared_by_second_track(library):
    db, track, other = library
    _seed_exclusive_unknown_artist(db, track)
    result = _apply(db, track, _analyze(db, track))
    ArtistCreditService(db).replace_track_credits(
        other, [ArtistCreditInput("Canonical Artist", entity_type="person", musicbrainz_artist_id="artist-1")],
        provenance="musicbrainz",
    )
    before = _dump(db.conn)
    with pytest.raises(StaleMetadataProposal, match="shared_identity_changed"):
        undo_materialization(db.conn, result.change_group_id)
    assert _dump(db.conn) == before


def test_all_blocked_providers_do_not_repair_membership_or_change_effective_rows(library):
    db, track, _other = library
    with db.conn:
        db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='different-recording' WHERE id=?", (track,))
        db.conn.execute("DELETE FROM track_album_memberships WHERE track_id=?", (track,))
    before = capture_metadata_state(db.conn, track)
    analysis = _analyze(db, track)
    assert analysis[1].blocked_providers == ("musicbrainz",)
    result = _apply(db, track, analysis)
    assert not result.changed
    assert capture_metadata_state(db.conn, track) == before
    assert not db.conn.execute("SELECT 1 FROM track_album_memberships WHERE track_id=?", (track,)).fetchone()


def test_fresh_same_candidate_analysis_does_not_churn_effective_state_or_timestamps(library):
    db, track, _other = library
    _apply(db, track, _analyze(db, track))
    before = capture_metadata_state(db.conn, track)
    history = _rows(db.conn, "track_metadata_history")
    result = _apply(db, track, _analyze(db, track))
    assert not result.changed
    assert capture_metadata_state(db.conn, track) == before
    assert _rows(db.conn, "track_metadata_history") == history


def test_recording_group_only_delta_is_available_to_public_undo(library):
    db, track, _other = library
    metadata, proposal, _ensemble, _automatic = _analyze(db, track)
    before = capture_metadata_state(db.conn, track)
    with materialize_proposal(db.conn, proposal) as transaction:
        db.conn.execute("UPDATE tracks SET recording_group_key='rg1_synthetic' WHERE id=?", (track,))
    assert transaction.changed
    assert metadata.preview_undo(track).change_group_id == transaction.identifier
    metadata.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before
