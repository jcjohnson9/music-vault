"""Automatic remediation never borrows explicit user override authority."""
import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService
from music_vault.metadata.materializer import StaleMetadataProposal, capture_metadata_state
from music_vault.metadata.service import MetadataAction, MetadataService


@pytest.fixture
def library(tmp_path):
    db = MusicVaultDB(tmp_path / "synthetic.sqlite3", backup_dir=tmp_path / "backups")
    track = db.upsert_track(tmp_path / "synthetic.flac", title="Before", artist="Before artist", album="Before album")
    try:
        yield db, track, MetadataService(db)
    finally:
        db.close()


def apply(service, track, values, **kwargs):
    return service.apply_high_confidence_candidate(track, values, recording_id=kwargs.pop("recording_id", "recording-a"),
        release_id=kwargs.pop("release_id", "edition-a"), confidence=98, **kwargs)


def prepare(service, track, values, **kwargs):
    return service.prepare_high_confidence_candidate(track, values, recording_id=kwargs.pop("recording_id", "recording-a"),
        release_id=kwargs.pop("release_id", "edition-a"), confidence=98, **kwargs)


@pytest.mark.parametrize("values,recording,release", [
    ({"title": "After"}, "recording-a", None),
    ({"artist": "After artist"}, "recording-a", None),
    ({"album": "After album"}, None, "edition-a"),
    ({"artwork": "synthetic-cover.png"}, None, None),
])
def test_preparation_is_pure_immutable_and_scopes_identity(library, values, recording, release):
    db, track, service = library
    before = tuple(db.conn.iterdump())
    proposal = prepare(service, track, values)
    assert dict(proposal.values) == values
    assert proposal.recording_id == recording and proposal.release_id == release
    assert proposal.effective_change
    with pytest.raises(TypeError):
        proposal.values["title"] = "Injected"
    assert tuple(db.conn.iterdump()) == before and not db.conn.in_transaction
    apply(service, track, values)
    row = db.get_track(track)
    assert row["musicbrainz_recording_id"] == recording
    assert row["musicbrainz_release_id"] == release


@pytest.mark.parametrize("provenance", ["manual", "musicbrainz_confirmed", "discogs_confirmed", "provider_confirmed"])
def test_all_protected_provenances_survive_unlock(library, provenance):
    db, track, service = library
    with db.conn:
        db.conn.execute("UPDATE track_metadata_fields SET provenance=?,is_locked=0,is_manual=? WHERE track_id=? AND field_name='title'",
                        (provenance, int(provenance == "manual"), track))
    before = tuple(db.conn.iterdump())
    assert not prepare(service, track, {"title": "Forbidden"}).values
    assert not apply(service, track, {"title": "Forbidden"}).changed
    assert tuple(db.conn.iterdump()) == before


@pytest.mark.parametrize("column,values", [("musicbrainz_recording_id", {"title": "After"}), ("musicbrainz_release_id", {"album": "After"})])
def test_known_identity_conflict_rejected_before_any_write(library, column, values):
    db, track, service = library
    with db.conn:
        db.conn.execute(f"UPDATE tracks SET {column}='known-other' WHERE id=?", (track,))
    before = tuple(db.conn.iterdump())
    with pytest.raises(ValueError, match="identity"):
        prepare(service, track, values)
    with pytest.raises(ValueError, match="identity"):
        apply(service, track, values)
    assert tuple(db.conn.iterdump()) == before


def test_new_edition_cannot_inherit_unknown_old_family(library):
    db, track, service = library
    with db.conn:
        db.conn.execute("INSERT INTO track_release_context(track_id,musicbrainz_release_group_id,updated_at) VALUES(?, 'old-family', 'synthetic')", (track,))
    with pytest.raises(ValueError, match="family"):
        prepare(service, track, {"album": "After album"})


def test_unproved_cross_catalogue_release_is_rejected(library):
    db, track, service = library
    with db.conn:
        db.conn.execute("UPDATE tracks SET discogs_release_id='discogs-existing' WHERE id=?", (track,))
    with pytest.raises(ValueError, match="cross-catalogue"):
        prepare(service, track, {"album": "After album"})


def test_structured_credits_are_not_flattened_even_unlocked(library):
    db, track, service = library
    ArtistCreditService(db).replace_track_credits(track, [ArtistCreditInput("Before artist", musicbrainz_artist_id="artist-existing")],
                                                provenance="musicbrainz", is_manual=False, is_locked=False)
    before = tuple(db.conn.iterdump())
    assert not prepare(service, track, {"artist": "Replacement scalar"}).values
    assert not apply(service, track, {"artist": "Replacement scalar"}).changed
    assert tuple(db.conn.iterdump()) == before


def test_atomic_graph_undo_and_automatic_retry_after_undo_is_refused(library):
    db, track, service = library
    before = capture_metadata_state(db.conn, track)
    fingerprint = service.metadata_state_fingerprint(track)
    values = {"title": "After", "artist": "After artist", "album": "After album"}
    result = apply(service, track, values, expected_fingerprint=fingerprint, proposal_revision="item-version-1", release_group_id="family-a")
    journal = db.conn.execute("SELECT * FROM metadata_materializations WHERE id=?", (result.change_group_id,)).fetchone()
    assert journal and journal["undone_at"] is None
    assert service.preview_undo(track).actor == "remediation"
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before
    state = tuple(db.conn.iterdump())
    with pytest.raises(StaleMetadataProposal):
        apply(service, track, values, expected_fingerprint=fingerprint, proposal_revision="item-version-1", release_group_id="family-a")
    assert tuple(db.conn.iterdump()) == state


def test_exact_retry_and_fresh_equivalent_apply_are_true_noops(library):
    db, track, service = library
    fingerprint = service.metadata_state_fingerprint(track)
    values = {"title": "After"}
    apply(service, track, values, expected_fingerprint=fingerprint, proposal_revision="item-version-1")
    before = tuple(db.conn.iterdump())
    assert not apply(service, track, values, expected_fingerprint=fingerprint, proposal_revision="item-version-1").changed
    assert tuple(db.conn.iterdump()) == before
    assert not prepare(service, track, values).effective_change
    assert not apply(service, track, values, proposal_revision="new-analysis").changed
    assert tuple(db.conn.iterdump()) == before


def test_stale_graph_rejects_even_with_equal_visible_title(library):
    db, track, service = library
    fingerprint = service.metadata_state_fingerprint(track)
    with db.conn:
        db.conn.execute("UPDATE track_artist_credits SET is_locked=1 WHERE track_id=?", (track,))
    before = tuple(db.conn.iterdump())
    with pytest.raises(StaleMetadataProposal):
        prepare(service, track, {"title": "After"}, expected_fingerprint=fingerprint)
    with pytest.raises(StaleMetadataProposal):
        apply(service, track, {"title": "After"}, expected_fingerprint=fingerprint)
    assert tuple(db.conn.iterdump()) == before


def test_final_reconciliation_runs_once_after_scoped_identity(library, monkeypatch):
    db, track, service = library
    seen = []
    original = service.finalize_identity
    def finalize(*args, **kwargs):
        seen.append(db.get_track(track)["musicbrainz_release_id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "finalize_identity", finalize)
    apply(service, track, {"album": "After album"}, release_group_id="family-a")
    assert seen == ["edition-a"]
    membership = db.conn.execute("SELECT album.musicbrainz_release_group_id FROM canonical_albums album JOIN track_album_memberships member ON member.canonical_album_id=album.id WHERE member.track_id=?", (track,)).fetchone()
    assert membership[0] == "family-a"


@pytest.mark.parametrize("shared", ["artist", "album"])
def test_shared_catalogue_mutation_rolls_back_complete_automatic_write(library, monkeypatch, shared):
    db, track, service = library
    other = db.upsert_track(db.db_path.parent / "other.flac", title="Other", artist="Other artist", album="Other album")
    table = "artists" if shared == "artist" else "canonical_albums"
    join = "track_artist_credits" if shared == "artist" else "track_album_memberships"
    key = "artist_id" if shared == "artist" else "canonical_album_id"
    identity = db.conn.execute(f"SELECT {key} FROM {join} WHERE track_id=?", (other,)).fetchone()[0]
    column = "display_name" if shared == "artist" else "title"
    original = service.finalize_identity
    def corrupt_shared(*args, **kwargs):
        original(*args, **kwargs)
        db.conn.execute(f"UPDATE {table} SET {column}='Forbidden shared mutation' WHERE id=?", (identity,))
    monkeypatch.setattr(service, "finalize_identity", corrupt_shared)
    before = tuple(db.conn.iterdump())
    with pytest.raises(ValueError, match="Shared"):
        apply(service, track, {"title": "After"})
    assert tuple(db.conn.iterdump()) == before


def test_caller_transaction_and_late_failure_are_atomic(library, monkeypatch):
    db, track, service = library
    before = tuple(db.conn.iterdump())
    db.conn.execute("BEGIN")
    apply(service, track, {"title": "After"}, commit=False)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert tuple(db.conn.iterdump()) == before
    def fail(*args, **kwargs):
        raise RuntimeError("injected finalization failure")
    monkeypatch.setattr(service, "finalize_identity", fail)
    with pytest.raises(RuntimeError, match="injected"):
        apply(service, track, {"title": "After"})
    assert tuple(db.conn.iterdump()) == before
