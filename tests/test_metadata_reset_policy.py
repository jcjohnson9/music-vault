"""Synthetic-only reset authority, atomicity, and complete Undo regressions."""
from dataclasses import replace

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.materializer import capture_metadata_state, materialize_proposal, user_write_intent
from music_vault.metadata.evidence import normalize_candidate
from music_vault.metadata.musicbrainz_enricher import MetadataCandidate
from music_vault.metadata.service import MetadataAction, MetadataService


@pytest.fixture
def library(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track = db.upsert_track(tmp_path / "synthetic.flac", title="Embedded title", artist="Embedded artist", album="Embedded album")
    try:
        yield db, track, MetadataService(db)
    finally:
        db.close()


def accepted_title(db, track, service, *, journal=True):
    if not journal:
        return service.apply_high_confidence_candidate(
            track, {"title": "Accepted title"}, recording_id="recording-a", release_id=None, confidence=98,
        )
    intent = replace(user_write_intent(track, service.metadata_state_fingerprint(track), mode="manual", selection={"test": "accept"}),
                     actor="metadata_intelligence")
    with materialize_proposal(db.conn, intent):
        db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='recording-a' WHERE id=?", (track,))
        service.record_source_observations(track, provider="musicbrainz_high_confidence", values={"title": "Accepted title"},
                                           provider_reference="recording-a", confidence=98, actor="metadata_intelligence", commit=False)
    return intent


@pytest.mark.parametrize("provider", ["musicbrainz", "discogs", "discogs_high_confidence", "musicbrainz_candidate", "unknown"])
def test_recorded_but_unaccepted_provider_does_not_win(library, provider):
    db, track, service = library
    service.apply_manual_patch(track, {"title": "Manual title"})
    with db.conn:
        db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='recording-a',discogs_release_id='release-a' WHERE id=?", (track,))
    service.record_source_observations(track, provider=provider, values={"title": "Rejected title"}, confidence=100,
                                       provider_reference="recording-a", apply_effective=False)
    service.reset_fields(track, ["title"])
    assert service.snapshot(track).value("title") == "Embedded title"


@pytest.mark.parametrize("journal", [False, True])
def test_accepted_compatible_title_resets_and_undo_restores_complete_state(library, journal):
    db, track, service = library
    accepted_title(db, track, service, journal=journal)
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title")})
    before = capture_metadata_state(db.conn, track)
    result = service.reset_fields(track, ["title"])
    assert result.changed
    state = result.after.fields["title"]
    assert (state.value, state.provenance, state.is_manual, state.is_locked) == ("Accepted title", "musicbrainz_high_confidence", False, False)
    assert service.preview_undo(track).change_group_id == result.change_group_id
    assert service.preview_undo(track).reason == "reset_to_automatic"
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before


@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize("change", ["recording", "version", "undone"])
def test_changed_identity_version_or_undone_acceptance_is_not_authority(library, journal, change):
    db, track, service = library
    accepted_title(db, track, service, journal=journal)
    if change == "undone":
        service.undo_last_change(track)
    elif change == "recording":
        with db.conn:
            db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='recording-other' WHERE id=?", (track,))
    else:
        service.apply_manual_patch(track, {"version_type": "live"})
    service.apply_manual_patch(track, {"title": "Manual title"})
    service.reset_fields(track, ["title"])
    assert service.snapshot(track).value("title") == "Embedded title"


def test_rollback_history_cannot_fabricate_acceptance(library):
    db, track, service = library
    with db.conn:
        db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='recording-a' WHERE id=?", (track,))
    service.record_source_observations(track, provider="musicbrainz_high_confidence", values={"title": "Rollback value"},
                                       provider_reference="recording-a", confidence=98, actor="remediation_rollback")
    service.apply_manual_patch(track, {"title": "Manual title"})
    assert service.best_automatic_value(track, "title").value == "Embedded title"


def test_same_transaction_version_change_cannot_authorize_old_provider_title(library):
    db, track, service = library
    accepted_title(db, track, service)
    service.apply_manual_patch(track, {"title": "Manual title"})
    service.apply_actions(track, {"title": MetadataAction.reset(), "version_type": MetadataAction.set("live")})
    assert service.snapshot(track).value("title") == "Embedded title"
    assert service.snapshot(track).value("version_type") == "live"


def test_provider_artist_is_not_flattened_from_scalar_observation(library):
    db, track, service = library
    service.apply_high_confidence_candidate(track, {"artist": "Catalogue artist"}, recording_id="recording-a", release_id=None, confidence=98)
    service.apply_manual_actions(track, {"artist": MetadataAction.set("Manual artist")})
    before = capture_metadata_state(db.conn, track)
    service.reset_fields(track, ["artist"])
    assert service.snapshot(track).value("artist") == "Embedded artist"
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before


def test_reset_does_not_unlock_unselected_fields(library):
    db, track, service = library
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title"), "album": MetadataAction.clear()})
    service.reset_fields(track, ["title"])
    album = service.snapshot(track).fields["album"]
    assert album.value is None and album.is_manual and album.is_locked


def test_source_upload_date_cannot_be_promoted_to_canonical_date(library):
    db, track, service = library
    service.record_source_observations(track, provider="youtube", values={"release_date": "2026", "original_release_date": "2026"})
    service.reset_fields(track, ["release_date", "original_release_date"])
    assert service.snapshot(track).value("release_date") is None
    assert service.snapshot(track).value("original_release_date") is None


def test_direct_reset_is_atomic_on_late_graph_failure(library, monkeypatch):
    db, track, service = library
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title"), "artist": MetadataAction.set("Manual artist")})
    before = tuple(db.conn.iterdump())
    def fail(*args, **kwargs):
        raise RuntimeError("injected final reconciliation failure")
    monkeypatch.setattr(service, "finalize_identity", fail)
    with pytest.raises(RuntimeError, match="injected"):
        service.apply_actions(track, {"title": MetadataAction.reset(), "artist": MetadataAction.reset()})
    assert tuple(db.conn.iterdump()) == before


def test_reset_preserves_outer_transaction_ownership(library):
    db, track, service = library
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title")})
    before = tuple(db.conn.iterdump())
    db.conn.execute("BEGIN")
    service.reset_fields(track, ["title"], commit=False)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert tuple(db.conn.iterdump()) == before


def test_repeated_reset_is_complete_noop(library):
    db, track, service = library
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title")})
    service.reset_fields(track, ["title"])
    before = tuple(db.conn.iterdump())
    assert not service.reset_fields(track, ["title"]).changed
    assert tuple(db.conn.iterdump()) == before


def test_recorded_evidence_bundle_is_not_an_acceptance_witness(library):
    db, track, service = library
    item = normalize_candidate(MetadataCandidate("Rejected title", "Rejected artist", None, None, "recording-a", None, 99))
    intent = replace(user_write_intent(track, service.metadata_state_fingerprint(track), mode="manual",
                                      selection={"test": "blocked-evidence"}, evidence=(item,)), actor="metadata_intelligence")
    with materialize_proposal(db.conn, intent):
        db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='recording-a' WHERE id=?", (track,))
        service.record_source_observations(track, provider="musicbrainz", values={"title": "Rejected title"},
                                           provider_reference="recording-a", confidence=99, apply_effective=False, commit=False)
    assert db.conn.execute("SELECT COUNT(*) FROM metadata_evidence_bundles").fetchone()[0] == 1
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title")})
    service.reset_fields(track, ["title"])
    assert service.snapshot(track).value("title") == "Embedded title"


def test_later_rejected_observation_does_not_displace_earlier_accepted_winner(library):
    db, track, service = library
    accepted_title(db, track, service)
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title")})
    service.record_source_observations(track, provider="musicbrainz_high_confidence", values={"title": "Later rejected title"},
                                       provider_reference="recording-a", confidence=100)
    service.reset_fields(track, ["title"])
    assert service.snapshot(track).value("title") == "Accepted title"


def test_changed_release_context_does_not_reuse_journal_authority(library):
    db, track, service = library
    accepted_title(db, track, service)
    with db.conn:
        db.conn.execute("INSERT INTO track_release_context(track_id,musicbrainz_release_group_id,updated_at) VALUES(?, 'other-family', 'synthetic-time')", (track,))
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title")})
    service.reset_fields(track, ["title"])
    assert service.snapshot(track).value("title") == "Embedded title"


def test_reset_preview_is_read_only(library):
    db, track, service = library
    accepted_title(db, track, service)
    service.apply_manual_actions(track, {"title": MetadataAction.set("Manual title")})
    before = tuple(db.conn.iterdump())
    assert service.best_automatic_value(track, "title").value == "Accepted title"
    assert tuple(db.conn.iterdump()) == before
    assert not db.conn.in_transaction


def test_stale_reset_fingerprint_rejects_all_writes(library):
    from music_vault.metadata.materializer import StaleMetadataProposal
    db, track, service = library
    fingerprint = service.metadata_state_fingerprint(track)
    service.apply_manual_actions(track, {"album": MetadataAction.clear()})
    before = tuple(db.conn.iterdump())
    with pytest.raises(StaleMetadataProposal):
        service.apply_manual_actions(track, {"title": MetadataAction.reset()}, expected_fingerprint=fingerprint)
    assert tuple(db.conn.iterdump()) == before


def test_rejected_artwork_is_not_reset_without_acceptance(library):
    db, track, service = library
    service.record_source_observations(track, provider="cover_art_archive", values={"artwork": "synthetic-rejected.png"},
                                       provider_reference="edition-a", confidence=99, apply_effective=False)
    service.apply_manual_actions(track, {"artwork": MetadataAction.set("synthetic-manual.png")})
    service.reset_fields(track, ["artwork"])
    assert service.snapshot(track).value("artwork") is None
