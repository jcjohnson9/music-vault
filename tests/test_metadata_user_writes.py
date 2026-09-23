"""Explicit user authority is scoped, revision checked, and fully reversible."""
from dataclasses import replace

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_credits import ArtistCreditInput, ArtistCreditService
from music_vault.metadata.materializer import StaleMetadataProposal, capture_metadata_state
from music_vault.metadata.musicbrainz_enricher import MetadataCandidate
from music_vault.metadata.providers import ProviderArtistCredit
from music_vault.metadata.service import MetadataAction, MetadataService


@pytest.fixture
def library(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track = db.upsert_track(tmp_path / "missing-synthetic.flac", title="Before", artist="Before Artist")
    playlist = db.create_playlist("Synthetic playlist")
    db.add_track_to_playlist(playlist, track)
    db.listening.set_favorite(track, True)
    try:
        yield db, track, MetadataService(db)
    finally:
        db.close()


def candidate():
    return MetadataCandidate(
        "Confirmed Title", "Credited Alias", "Confirmed Album", "1998",
        "recording-new", "release-new", 92, release_group_id="family-new",
        album_artist="Credited Alias", artist_credits=(ProviderArtistCredit(
            "Credited Alias", artist_id="artist-new", provider="musicbrainz",
            canonical_name="Canonical Artist", credited_as="Credited Alias", entity_type="person",
        ),),
    )


def confirm(service, track, values, *, supplied=None, **kwargs):
    selected = supplied or candidate()
    return service.apply_confirmed_candidate(
        track, values, recording_id=selected.recording_id, release_id=selected.release_id,
        release_group_id=selected.release_group_id, confidence=selected.score, candidate=selected, **kwargs,
    )


def dump(db):
    return tuple(db.conn.iterdump())


@pytest.mark.parametrize("values,recording,release,family", [
    ({"artwork": "synthetic-art.png"}, None, None, None),
    ({"title": "Confirmed Title"}, "recording-new", None, None),
    ({"artist": "Credited Alias"}, "recording-new", None, None),
    ({"album": "Confirmed Album"}, None, "release-new", "family-new"),
    ({"release_date": "1998"}, None, "release-new", "family-new"),
    ({"title": "Confirmed Title", "album": "Confirmed Album"}, "recording-new", "release-new", "family-new"),
])
def test_confirmation_applies_only_selected_identity_scope(library, values, recording, release, family):
    db, track, service = library
    credits = [tuple(row) for row in db.conn.execute("SELECT * FROM track_artist_credits")]
    confirm(service, track, values)
    row = db.get_track(track)
    assert row["musicbrainz_recording_id"] == recording
    assert row["musicbrainz_release_id"] == release
    context = db.conn.execute("SELECT musicbrainz_release_group_id FROM track_release_context WHERE track_id=?", (track,)).fetchone()
    assert (context[0] if context else None) == family
    if "artist" not in values:
        assert [tuple(row) for row in db.conn.execute("SELECT * FROM track_artist_credits")] == credits


def test_explicit_confirmation_overrides_selected_lock_and_identity_not_unselected(library):
    db, track, service = library
    service.apply_manual_patch(track, {"artist": "Prior manual", "album": None})
    with db.conn:
        db.conn.execute("UPDATE tracks SET musicbrainz_recording_id='old-recording',musicbrainz_release_id='old-release' WHERE id=?", (track,))
    before = capture_metadata_state(db.conn, track)
    result = confirm(service, track, {"artist": "Credited Alias"})
    current = service.snapshot(track)
    assert current.fields["artist"].provenance == "musicbrainz_confirmed"
    assert current.fields["artist"].is_locked and not current.fields["artist"].is_manual
    assert current.fields["album"].is_manual and current.value("album") is None
    assert db.get_track(track)["musicbrainz_release_id"] == "old-release"
    credit = ArtistCreditService(db).track_credits(track)[0]
    assert credit.artist.musicbrainz_artist_id == "artist-new"
    assert credit.credited_as == "Credited Alias"
    assert credit.provenance == "musicbrainz_confirmed"
    assert credit.is_locked and not credit.is_manual
    assert service.preview_undo(track).change_group_id == result.change_group_id
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before


@pytest.mark.parametrize("manual", [False, True])
def test_stale_revision_rejects_before_any_write(library, manual):
    db, track, service = library
    revision = service.metadata_state_fingerprint(track)
    service.apply_manual_patch(track, {"album": None})
    before = dump(db)
    with pytest.raises(StaleMetadataProposal):
        if manual:
            service.apply_manual_actions(track, {"title": MetadataAction.set("Late")}, expected_fingerprint=revision)
        else:
            confirm(service, track, {"title": "Confirmed Title"}, expected_fingerprint=revision)
    assert dump(db) == before


def test_manual_credit_only_edit_has_complete_undo_and_preserves_nonmetadata(library):
    db, track, service = library
    before = capture_metadata_state(db.conn, track)
    other = {table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")]
             for table in ("playlist_tracks", "playlist_track_origins", "track_favorites", "listening_events")}
    result = service.apply_manual_actions(track, {}, credit_inputs=(ArtistCreditInput(
        "Canonical Artist", credited_as="Before Artist", musicbrainz_artist_id="artist-new",
    ),))
    assert result.changed_fields == frozenset({"artist"})
    assert result.change_group_id
    assert service.snapshot(track).value("artist") == "Before Artist"
    assert ArtistCreditService(db).track_credits(track)[0].artist.display_name == "Canonical Artist"
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before
    assert other == {table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")] for table in other}


def test_manual_same_intent_retry_and_fresh_equivalent_edit_are_effective_noops(library):
    db, track, service = library
    actions = {"artist": MetadataAction.set("A & B")}
    revision = service.metadata_state_fingerprint(track)
    service.apply_manual_actions(track, actions, expected_fingerprint=revision)
    accepted = dump(db)
    assert not service.apply_manual_actions(track, actions, expected_fingerprint=revision).changed
    assert dump(db) == accepted
    before = capture_metadata_state(db.conn, track)
    whole_before = dump(db)
    assert not service.apply_manual_actions(track, actions).changed
    assert capture_metadata_state(db.conn, track) == before
    assert dump(db) == whole_before
    credits = ArtistCreditService(db).track_credits(track)
    assert len(credits) == 1 and credits[0].artist.display_name == "A & B"


def test_confirmed_fresh_equivalent_edit_preserves_effective_timestamps(library):
    db, track, service = library
    values = {"title": "Confirmed Title", "artist": "Credited Alias", "album": "Confirmed Album"}
    revision = service.metadata_state_fingerprint(track)
    confirm(service, track, values, expected_fingerprint=revision)
    accepted = dump(db)
    assert not confirm(service, track, values, expected_fingerprint=revision).changed
    assert dump(db) == accepted
    before = capture_metadata_state(db.conn, track)
    whole_before = dump(db)
    assert not confirm(service, track, values).changed
    assert capture_metadata_state(db.conn, track) == before
    assert dump(db) == whole_before


@pytest.mark.parametrize("manual", [False, True])
def test_user_edit_respects_outer_transaction_and_caller_rollback(library, manual):
    db, track, service = library
    before = dump(db)
    db.conn.execute("BEGIN IMMEDIATE")
    if manual:
        service.apply_manual_actions(track, {"title": MetadataAction.set("New")}, commit=False)
    else:
        confirm(service, track, {"title": "Confirmed Title", "artist": "Credited Alias"}, commit=False)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert dump(db) == before


@pytest.mark.parametrize("manual", [False, True])
def test_failed_final_reconciliation_rolls_back_complete_edit(library, monkeypatch, manual):
    db, track, service = library
    before = dump(db)
    def fail(*_a, **_k):
        raise RuntimeError("injected finalize failure")
    monkeypatch.setattr(service, "finalize_identity", fail)
    with pytest.raises(RuntimeError, match="injected"):
        if manual:
            service.apply_manual_actions(track, {"title": MetadataAction.set("New")}, credit_inputs=(ArtistCreditInput("New Artist"),))
        else:
            confirm(service, track, {"title": "Confirmed Title", "artist": "Credited Alias", "album": "Confirmed Album"})
    assert dump(db) == before


def test_confirmed_artist_and_album_reconcile_once_after_final_identities(library, monkeypatch):
    from music_vault.metadata import canonical_albums
    db, track, service = library
    calls = []
    old_artist_ids = {row[0] for row in db.conn.execute("SELECT id FROM artists")}
    old_album_ids = {row[0] for row in db.conn.execute("SELECT id FROM canonical_albums")}
    original = canonical_albums.upsert_track_canonical_album
    def observe(conn, track_id):
        calls.append((db.get_track(track_id)["musicbrainz_release_id"], ArtistCreditService(db).track_credits(track_id)[0].artist.musicbrainz_artist_id))
        return original(conn, track_id)
    monkeypatch.setattr(canonical_albums, "upsert_track_canonical_album", observe)
    confirm(service, track, {"artist": "Credited Alias", "album": "Confirmed Album"})
    assert calls == [("release-new", "artist-new")]
    assert len({row[0] for row in db.conn.execute("SELECT id FROM artists")} - old_artist_ids) == 1
    assert len({row[0] for row in db.conn.execute("SELECT id FROM canonical_albums")} - old_album_ids) == 1


def test_candidate_id_mismatch_and_unqualified_override_are_rejected(library):
    db, track, service = library
    before = dump(db)
    with pytest.raises(ValueError, match="differs"):
        service.apply_confirmed_candidate(track, {"title": "Title"}, recording_id="wrong", release_id=None, confidence=99, candidate=candidate())
    with pytest.raises(ValueError, match="confirmed provenance"):
        ArtistCreditService(db).replace_track_credits(track, [ArtistCreditInput("Name")], provenance="youtube", is_locked=True, confirmed_override=True)
    assert dump(db) == before


def test_empty_manual_save_is_exact_noop(library):
    db, track, service = library
    before = dump(db)
    assert not service.apply_manual_actions(track, {}).changed
    assert dump(db) == before


def test_manual_credit_only_audit_identifies_user_authority(library):
    db, track, service = library
    service.apply_manual_actions(track, {}, credit_inputs=(ArtistCreditInput("Before Artist", musicbrainz_artist_id="artist-new"),))
    group = service.preview_undo(track)
    assert group.actor == "user"
    assert group.reason in {"manual_metadata_edit", "manual_artist_credit_edit", "manual_edit"}


@pytest.mark.parametrize("manual", [False, True])
def test_track_scoped_user_edit_cannot_mutate_shared_catalogue_artist(library, tmp_path, manual):
    db, track, service = library
    other = db.upsert_track(tmp_path / "other.flac", title="Other", artist="Canonical Artist")
    ArtistCreditService(db).replace_track_credits(
        other, [ArtistCreditInput("Canonical Artist", musicbrainz_artist_id="artist-new")], provenance="musicbrainz",
    )
    before = dump(db)
    with pytest.raises(ValueError, match="Shared artist facts"):
        if manual:
            service.apply_manual_actions(track, {}, credit_inputs=(ArtistCreditInput(
                "Canonical Artist", entity_type="person", musicbrainz_artist_id="artist-new", credited_as="Alias",
            ),))
        else:
            confirm(service, track, {"artist": "Credited Alias"})
    assert dump(db) == before


def test_shared_catalogue_artist_allows_track_only_credited_alias(library, tmp_path):
    db, track, service = library
    other = db.upsert_track(tmp_path / "other.flac", title="Other", artist="Canonical Artist")
    ArtistCreditService(db).replace_track_credits(other, [ArtistCreditInput(
        "Canonical Artist", musicbrainz_artist_id="artist-new", entity_type="person",
    )], provenance="musicbrainz")
    before = capture_metadata_state(db.conn, track)
    other_before = capture_metadata_state(db.conn, other)
    confirm(service, track, {"artist": "Credited Alias"})
    assert capture_metadata_state(db.conn, other) == other_before
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before


@pytest.mark.parametrize("new_family", ["family-second", None])
def test_confirmed_new_release_rebinds_only_target_membership_and_undo_restores(library, new_family):
    db, track, service = library
    confirm(service, track, {"artist": "Credited Alias", "album": "Confirmed Album"})
    before = capture_metadata_state(db.conn, track)
    original_membership = before["track_album_memberships"][0]["canonical_album_id"]
    second = replace(candidate(), album="Second Album", release_id="release-second", release_group_id=new_family)
    confirm(service, track, {"album": "Second Album"}, supplied=second)
    current = capture_metadata_state(db.conn, track)
    assert current["track"]["musicbrainz_release_id"] == "release-second"
    assert current["track_release_context"][0]["musicbrainz_release_group_id"] == new_family
    assert current["track_album_memberships"][0]["canonical_album_id"] != original_membership
    assert db.conn.execute("SELECT 1 FROM canonical_albums WHERE id=?", (original_membership,)).fetchone()
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before


def test_confirmed_title_only_does_not_rebind_existing_release(library):
    db, track, service = library
    confirm(service, track, {"artist": "Credited Alias", "album": "Confirmed Album"})
    before = capture_metadata_state(db.conn, track)
    second = replace(candidate(), title="Second Title", recording_id="recording-second", release_id="release-second", release_group_id="family-second")
    confirm(service, track, {"title": "Second Title"}, supplied=second)
    current = capture_metadata_state(db.conn, track)
    assert current["track"]["musicbrainz_recording_id"] == "recording-second"
    assert current["track"]["musicbrainz_release_id"] == "release-new"
    assert current["track_release_context"] == before["track_release_context"]
    assert current["track_album_memberships"] == before["track_album_memberships"]
    assert current["canonical_albums"] == before["canonical_albums"]


@pytest.mark.parametrize("manual", [False, True])
def test_fresh_deliberate_user_edit_after_undo_is_not_a_stale_automatic_retry(library, manual):
    db, track, service = library
    def edit():
        if manual:
            return service.apply_manual_actions(track, {"title": MetadataAction.set("Again")})
        return confirm(service, track, {"title": "Confirmed Title"})
    first = edit()
    service.undo_last_change(track)
    second = edit()
    assert second.changed and second.change_group_id != first.change_group_id
    assert db.get_track(track)["title"] == ("Again" if manual else "Confirmed Title")


@pytest.mark.parametrize("column", ["discogs_release_id", "discogs_master_id", "provider_release_family_id"])
def test_unproven_cross_catalogue_release_rebind_is_refused_atomically(library, column):
    db, track, service = library
    if column == "provider_release_family_id":
        with db.conn:
            db.conn.execute("INSERT INTO track_release_context(track_id,provider_release_family_id,updated_at) VALUES(?, 'other:family', '2026-01-01')", (track,))
    else:
        with db.conn:
            db.conn.execute(f"UPDATE tracks SET {column}='other-catalogue' WHERE id=?", (track,))
    before = dump(db)
    with pytest.raises(ValueError, match="cross-catalogue"):
        confirm(service, track, {"album": "Confirmed Album"})
    assert dump(db) == before


def _other_catalogue_album(db, tmp_path):
    other = db.upsert_track(tmp_path / "shared-album-other.flac", title="Other", artist="Other")
    confirm(MetadataService(db), other, {"artist": "Credited Alias", "album": "Confirmed Album"})
    return other


@pytest.mark.parametrize("field,value", [("album", "Renamed Shared Album"), ("original_release_date", "1980")])
def test_manual_shared_album_title_or_date_mutation_is_refused_atomically(library, tmp_path, field, value):
    db, track, service = library
    other = _other_catalogue_album(db, tmp_path)
    confirm(service, track, {"artist": "Credited Alias", "album": "Confirmed Album"})
    assert db.conn.execute("SELECT COUNT(DISTINCT canonical_album_id) FROM track_album_memberships WHERE track_id IN (?,?)", (track, other)).fetchone()[0] == 1
    before = dump(db)
    with pytest.raises(ValueError, match="Shared album facts"):
        service.apply_manual_actions(track, {field: MetadataAction.set(value)})
    assert dump(db) == before


@pytest.mark.parametrize("already_joined", [False, True])
def test_confirmed_shared_current_or_destination_album_change_is_refused(library, tmp_path, already_joined):
    db, track, service = library
    _other_catalogue_album(db, tmp_path)
    if already_joined:
        confirm(service, track, {"artist": "Credited Alias", "album": "Confirmed Album"})
    before = dump(db)
    # First-time attachment may retain the prior shared title, but an earlier
    # original date can still enrich that destination row before attachment.
    altered = replace(candidate(), album="Different Shared Presentation", original_release_date="1980")
    with pytest.raises(ValueError, match="Shared album facts"):
        confirm(service, track, {"artist": "Credited Alias", "album": altered.album,
                                 "original_release_date": "1980"}, supplied=altered)
    assert dump(db) == before


def test_confirmed_unchanged_existing_album_join_is_allowed_and_undoable(library, tmp_path):
    db, track, service = library
    other = _other_catalogue_album(db, tmp_path)
    before = capture_metadata_state(db.conn, track)
    shared_before = capture_metadata_state(db.conn, other)
    result = confirm(service, track, {"artist": "Credited Alias", "album": "Confirmed Album"})
    assert result.changed
    assert capture_metadata_state(db.conn, other) == shared_before
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before
    assert capture_metadata_state(db.conn, other) == shared_before


def test_manual_album_correction_to_independent_new_fallback_remains_allowed(library):
    db, track, service = library
    before = capture_metadata_state(db.conn, track)
    result = service.apply_manual_actions(track, {"album": MetadataAction.set("New Independent Album")})
    assert result.changed
    service.undo_last_change(track)
    assert capture_metadata_state(db.conn, track) == before
