"""Synthetic accepted-cover authority, retention and whole-graph reversal."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest
from PySide6.QtGui import QImage

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artwork import prepare_local_artwork
from music_vault.metadata.discogs_artwork import DiscogsArtworkCache, DiscogsArtworkError, DiscogsArtworkRecord
from music_vault.metadata.ensemble import FieldAction, build_metadata_ensemble
from music_vault.metadata.evidence import normalize_candidate
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.materializer import StaleMetadataProposal, capture_metadata_state, state_fingerprint
from music_vault.metadata.presentation_art import (
    accepted_release_witness, apply_artwork_upgrade, current_asset_info,
    prepare_artwork_upgrade, validate_staged_artwork,
)
from music_vault.metadata.providers import ProviderArtworkCandidate, ProviderArtistCredit, ProviderReleaseCandidate
from music_vault.metadata.resolver import resolve_metadata
from music_vault.metadata.schema import EDITABLE_METADATA_FIELDS
from music_vault.metadata.service import AutomaticMetadataField, MetadataAction, MetadataService


def image(path, width=600, height=600, color=0xFF884422):
    picture = QImage(width, height, QImage.Format.Format_RGB32)
    picture.fill(color)
    assert picture.save(str(path), "PNG")
    return path


def candidate(**kwargs):
    values = dict(
        provider="discogs", title="Synthetic track", artist="Synthetic artist", album="Synthetic album",
        album_artist="Synthetic artist", version_type="studio", release_date="1990",
        original_release_date="1990", release_id="202", master_id="303", duration_seconds=240,
        provider_score=97, provider_reference="https://www.discogs.com/release/202",
        artist_credits=(ProviderArtistCredit("Synthetic artist", artist_id="101", provider="discogs"),),
        artwork=ProviderArtworkCandidate("https://i.discogs.com/accepted.jpeg", "https://www.discogs.com/release/202", "202"),
    )
    values.update(kwargs)
    return ProviderReleaseCandidate(**values)


def accept(db, track, provider_candidate=None):
    accepted = provider_candidate or candidate()
    metadata = MetadataService(db)
    state = capture_metadata_state(db.conn, track)
    snapshot = metadata.snapshot(track)
    values = {name: field.value for name, field in snapshot.fields.items()}
    values.update(state["track"])
    if state["track_release_context"]:
        values.update(state["track_release_context"][0])
    locked = frozenset(name for name, field in snapshot.fields.items() if field.is_manual or field.is_locked)
    ensemble = build_metadata_ensemble(current=values, discogs_candidates=(accepted,), locked_fields=locked)
    proposal = resolve_metadata(
        track_id=track, expected_fingerprint=state_fingerprint(state), current_values=values,
        locked_fields=locked, protected_credits=False, ensemble=ensemble, evidence=(normalize_candidate(accepted),),
    )
    automatic = {
        field.field_name: AutomaticMetadataField(field.value, field.score, field.source, field.provider_reference, field.conflict)
        for field in ensemble.fields if field.field_name in EDITABLE_METADATA_FIELDS
        and field.field_name != "artwork" and field.value not in (None, "") and field.action is not FieldAction.REVIEW
    }
    service = object.__new__(MetadataIntelligenceService)
    return service._materialize_analysis(db, track, metadata, proposal, ensemble, automatic)


def staged(tmp_path, width=600, height=600, **changes):
    source = image(tmp_path / "candidate.png", width, height)
    prepared = prepare_local_artwork(source)
    destination = tmp_path / (prepared.sha256 + prepared.extension)
    destination.write_bytes(prepared.data)
    record = DiscogsArtworkRecord(
        destination, "202", prepared.sha256, prepared.mime_type, width, height,
        "https://i.discogs.com/accepted.jpeg", "https://i.discogs.com/accepted.jpeg",
        "https://www.discogs.com/release/202", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
    )
    return replace(record, **changes)


@pytest.fixture
def library(tmp_path):
    db = MusicVaultDB(tmp_path / "synthetic.sqlite3", backup_dir=tmp_path / "backups")
    old = image(tmp_path / "source-thumbnail.png", 640, 360, 0xFF112244)
    track = db.upsert_track(tmp_path / "synthetic.mp3", title="Upload", artist="Uploader",
                            source_kind="youtube", source_video_id="synthetic01", cover_path=str(old), duration_seconds=240)
    original = accept(db, track)
    assert original.changed
    try:
        yield db, track, old
    finally:
        db.close()


def decision(db, track):
    state = capture_metadata_state(db.conn, track)
    art = MetadataService(db).snapshot(track).fields["artwork"]
    return prepare_artwork_upgrade(state, accepted_release_witness(db.conn, track, "202"), current_asset_info(art.value))


def dump(db):
    return tuple(db.conn.iterdump())


def test_matching_accepted_cover_changes_only_art_and_public_undo_restores_full_graph(library, tmp_path):
    db, track, old = library
    before = capture_metadata_state(db.conn, track)
    old_bytes = old.read_bytes()
    cover = staged(tmp_path)
    prepared = decision(db, track)
    assert prepared.allowed and prepared.replacement_class == "source_thumbnail"
    result = apply_artwork_upgrade(db, prepared, cover)
    assert result.changed_fields == {"artwork"}
    assert result.change_group_id
    after = capture_metadata_state(db.conn, track)
    for table in ("artists", "canonical_albums", "track_artist_credits", "track_release_context", "track_album_memberships"):
        assert after[table] == before[table]
    assert db.get_track(track)["cover_path"] == str(cover.path)
    assert MetadataService(db).undo_last_change(track).changed_fields == {"artwork"}
    assert capture_metadata_state(db.conn, track) == before
    assert old.read_bytes() == old_bytes
    assert cover.path.is_file()
    assert not (tmp_path / "synthetic.mp3").exists()


def test_identical_retry_is_exact_database_noop(library, tmp_path):
    db, track, _old = library
    prepared, cover = decision(db, track), staged(tmp_path)
    apply_artwork_upgrade(db, prepared, cover)
    before = dump(db)
    assert not apply_artwork_upgrade(db, prepared, cover).changed
    assert dump(db) == before


@pytest.mark.parametrize("authority", ["manual", "locked", "confirmed", "manual_blank"])
def test_manual_and_confirmed_authority_wins_even_when_blank(library, authority):
    db, track, old = library
    if authority.startswith("manual"):
        MetadataService(db).apply_manual_actions(track, {"artwork": MetadataAction.set(str(old)) if authority == "manual" else MetadataAction.clear()})
    else:
        db.conn.execute("UPDATE track_metadata_fields SET is_locked=?,provenance=? WHERE track_id=? AND field_name='artwork'",
                        (int(authority == "locked"), "provider_confirmed" if authority == "confirmed" else "youtube_thumbnail", track))
        db.conn.commit()
    assert decision(db, track).reason == "preserved_manual_artwork"


@pytest.mark.parametrize("provenance", ["embedded", "unknown", "discogs_high_confidence", "cover_art_archive"])
def test_valid_non_thumbnail_is_not_replaced(library, provenance):
    db, track, _ = library
    db.conn.execute("UPDATE track_metadata_fields SET provenance=? WHERE track_id=? AND field_name='artwork'", (provenance, track))
    db.conn.commit()
    assert decision(db, track).reason == "preserved_existing_artwork"


def test_missing_source_is_a_proven_gap_but_unwitnessed_legacy_identity_is_not(library):
    db, track, old = library
    old.unlink()
    assert decision(db, track).replacement_class == "gap"
    db.conn.execute("DELETE FROM metadata_materializations")
    db.conn.commit()
    assert decision(db, track).reason == "accepted_release_witness_unavailable"


@pytest.mark.parametrize("mutation", ["edition", "family", "version", "undone", "evidence"])
def test_changed_or_rejected_identity_witness_is_refused(library, mutation):
    db, track, _old = library
    if mutation in {"edition", "family", "version"}:
        column, value = {"edition": ("discogs_release_id", "999"), "family": ("discogs_master_id", "999"), "version": ("version_type", "live")}[mutation]
        db.conn.execute(f"UPDATE tracks SET {column}=? WHERE id=?", (value, track))
    elif mutation == "undone":
        db.conn.execute("UPDATE metadata_materializations SET undone_at='synthetic'")
    else:
        db.conn.execute("DELETE FROM metadata_evidence_bundles")
    db.conn.commit()
    assert not decision(db, track).allowed


@pytest.mark.parametrize("width,height", [(64, 64), (2000, 400)])
def test_actual_decoded_front_quality_is_required(library, tmp_path, width, height):
    db, track, _ = library
    before = dump(db)
    with pytest.raises(ValueError, match="quality_insufficient"):
        apply_artwork_upgrade(db, decision(db, track), staged(tmp_path, width, height))
    assert dump(db) == before


@pytest.mark.parametrize("race", ["source", "staged", "graph", "manual", "witness"])
def test_stale_assets_graph_and_authority_reject_atomically(library, tmp_path, race):
    db, track, old = library
    prepared, cover = decision(db, track), staged(tmp_path)
    if race in {"source", "staged"}:
        image(old if race == "source" else cover.path, color=0xFFABCDEF)
    elif race == "graph":
        db.conn.execute("UPDATE tracks SET title='Changed' WHERE id=?", (track,))
        db.conn.commit()
    elif race == "manual":
        MetadataService(db).apply_manual_actions(track, {"artwork": MetadataAction.clear()})
    else:
        db.conn.execute("UPDATE metadata_materializations SET undone_at='synthetic'")
        db.conn.commit()
    before = dump(db)
    with pytest.raises((ValueError, StaleMetadataProposal)):
        apply_artwork_upgrade(db, prepared, cover)
    assert dump(db) == before


def test_later_credit_change_prevents_undo(library, tmp_path):
    db, track, _ = library
    apply_artwork_upgrade(db, decision(db, track), staged(tmp_path))
    db.conn.execute("UPDATE track_artist_credits SET credited_as='Changed alias' WHERE track_id=?", (track,))
    db.conn.commit()
    before = dump(db)
    with pytest.raises(StaleMetadataProposal):
        MetadataService(db).undo_last_change(track)
    assert dump(db) == before


def test_failed_history_write_rolls_back_every_database_write(library, tmp_path):
    db, track, _ = library
    db.conn.execute("CREATE TRIGGER reject_art BEFORE INSERT ON track_metadata_history WHEN NEW.field_name='artwork' BEGIN SELECT RAISE(ABORT,'synthetic rejection'); END")
    db.conn.commit()
    before = dump(db)
    with pytest.raises(Exception, match="synthetic rejection"):
        apply_artwork_upgrade(db, decision(db, track), staged(tmp_path))
    assert dump(db) == before


def test_caller_transaction_remains_owned_and_can_rollback(library, tmp_path):
    db, track, _ = library
    before = dump(db)
    db.conn.execute("BEGIN")
    assert apply_artwork_upgrade(db, decision(db, track), staged(tmp_path)).changed
    assert db.conn.in_transaction
    db.conn.rollback()
    assert dump(db) == before


def test_identical_image_bytes_dont_relabel_or_create_a_journal(library, tmp_path):
    db, track, old = library
    image(old)
    before = dump(db)
    assert not apply_artwork_upgrade(db, decision(db, track), staged(tmp_path)).changed
    assert dump(db) == before


@pytest.mark.parametrize("change", [dict(image_type="back"), dict(catalogue_image=False), dict(release_id="999")])
def test_staging_reuses_front_and_release_guards_without_network(tmp_path, change):
    cache = DiscogsArtworkCache(tmp_path / "cache")
    with pytest.raises(DiscogsArtworkError):
        cache.stage_accepted_front(replace(candidate().artwork, **change), accepted_release_id="202", provider_score=97)
    assert not (tmp_path / "cache").exists()


def test_saved_evidence_without_accepted_identity_transition_is_not_authority(library):
    db, track, _ = library
    row = db.conn.execute("SELECT id,after_json FROM metadata_materializations ORDER BY rowid DESC LIMIT 1").fetchone()
    db.conn.execute("UPDATE metadata_materializations SET before_json=? WHERE id=?", (row[1], row[0]))
    db.conn.commit()
    assert accepted_release_witness(db.conn, track, "202") is None


def test_disagreeing_effective_artwork_column_is_not_silently_replaced(library):
    db, track, _ = library
    db.conn.execute("UPDATE tracks SET cover_path='unrecognized.png' WHERE id=?", (track,))
    db.conn.commit()
    assert decision(db, track).reason == "artwork_state_inconsistent"


class _StagingStore:
    def __init__(self, record):
        self.record = record
        self.staged = 0

    def fetch_for_gap(self, *args, **kwargs):
        return None

    def stage_accepted_front(self, *args, **kwargs):
        self.staged += 1
        return self.record


def worker(db, art, *, enabled=True, writeback=False, tag_writer=None):
    from test_batch10_1_acceptance import TokenStore, FakeDiscogs, config
    return MetadataIntelligenceService(
        db, config(metadata_fill_missing_artwork_enabled=enabled, metadata_writeback_enabled=writeback),
        token_store=TokenStore(), discogs_provider_factory=lambda _token: FakeDiscogs((candidate(),)),
        artwork_store_factory=lambda _token: art,
        tag_writer=tag_writer,
    )


def test_existing_opt_in_worker_upgrades_cover_and_retains_both_history_links(library, tmp_path):
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    db, track, old = library
    before = capture_metadata_state(db.conn, track)
    cover = staged(tmp_path)
    art = _StagingStore(cover)
    store = MetadataIntelligenceJobStore(db)
    item = store.enqueue_track(track)
    result = worker(db, art).process_automatic_queue()
    assert result.failed == 0
    assert art.staged == 1
    assert db.get_track(track)["cover_path"] == str(cover.path)
    saved = db.conn.execute("SELECT * FROM metadata_intelligence_items WHERE id=?", (item.id,)).fetchone()
    proposal = json.loads(saved["field_proposal"])
    assert saved["artwork_result"] == "upgraded_source_thumbnail"
    assert saved["applied_history_group"] == proposal["_presentation_art_history_group"]
    # This scan made no new scalar/identity change; retain the earlier accepted
    # edition journal explicitly without inventing a new metadata change group.
    witness_id = proposal["_presentation_art_release_witness"]
    assert witness_id != saved["applied_history_group"]
    assert db.conn.execute("SELECT undone_at FROM metadata_materializations WHERE id=?", (witness_id,)).fetchone()[0] is None
    assert proposal["_artwork"]["result"] == "upgraded_source_thumbnail"
    MetadataService(db).undo_last_change(track)
    assert db.get_track(track)["cover_path"] == str(old)
    after = capture_metadata_state(db.conn, track)
    for key in ("artists", "canonical_albums", "track_artist_credits", "track_release_context", "track_album_memberships"):
        assert after[key] == before[key]


def test_worker_opt_out_never_stages(library, tmp_path):
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    db, track, old = library
    art = _StagingStore(staged(tmp_path))
    MetadataIntelligenceJobStore(db).enqueue_track(track)
    assert worker(db, art, enabled=False).process_automatic_queue().failed == 0
    assert art.staged == 0
    assert db.get_track(track)["cover_path"] == str(old)


def test_worker_item_persistence_failure_rolls_back_both_journals(library, tmp_path):
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    db, track, old = library
    art = _StagingStore(staged(tmp_path))
    store = MetadataIntelligenceJobStore(db)
    item = store.enqueue_track(track)
    claimed = store.claim_next_item(item.job_id)
    db.conn.execute("CREATE TRIGGER reject_completed BEFORE UPDATE ON metadata_intelligence_items WHEN NEW.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'synthetic item failure'); END")
    db.conn.commit()
    before = dump(db)
    service = worker(db, art)
    with pytest.raises(Exception, match="synthetic item failure"):
        service._process_item(db, store, claimed, service._settings(), None)
    assert dump(db) == before
    assert db.get_track(track)["cover_path"] == str(old)


def test_undo_then_identical_automatic_retry_is_true_noop(library, tmp_path):
    db, track, _ = library
    prepared, cover = decision(db, track), staged(tmp_path)
    apply_artwork_upgrade(db, prepared, cover)
    MetadataService(db).undo_last_change(track)
    before = dump(db)
    assert not apply_artwork_upgrade(db, decision(db, track), cover).changed
    assert dump(db) == before
    db.conn.execute("UPDATE tracks SET title='Concurrent edit' WHERE id=?", (track,))
    db.conn.commit()
    before = dump(db)
    with pytest.raises(StaleMetadataProposal, match="undone_proposal_changed"):
        apply_artwork_upgrade(db, prepared, cover)
    assert dump(db) == before


def test_worker_rescan_after_art_undo_keeps_thumbnail_and_does_not_fail(library, tmp_path):
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    db, track, old = library
    art = _StagingStore(staged(tmp_path))
    store = MetadataIntelligenceJobStore(db)
    item = store.enqueue_track(track)
    service = worker(db, art)
    assert service.process_automatic_queue().failed == 0
    MetadataService(db).undo_last_change(track)
    history = db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0]
    journals = db.conn.execute("SELECT COUNT(*) FROM metadata_materializations WHERE proposal_key LIKE 'presentation-art-v1:%'").fetchone()[0]
    db.conn.execute("UPDATE metadata_intelligence_items SET state='queued',completed_at=NULL WHERE id=?", (item.id,))
    db.conn.execute("UPDATE metadata_intelligence_jobs SET status='created',completed_at=NULL WHERE id=?", (item.job_id,))
    db.conn.commit()
    result = service.process_automatic_queue()
    assert result.processed == 1 and result.failed == 0
    assert db.get_track(track)["cover_path"] == str(old)
    assert db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0] == history
    assert db.conn.execute("SELECT COUNT(*) FROM metadata_materializations WHERE proposal_key LIKE 'presentation-art-v1:%'").fetchone()[0] == journals


def test_presentation_only_upgrade_never_enters_tag_writer_even_with_opt_in(library, tmp_path):
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    db, track, _ = library
    class NeverWrite:
        def supports(self, *_args):
            pytest.fail("Presentation art cannot activate embedded tag writeback")
    MetadataIntelligenceJobStore(db).enqueue_track(track)
    art = _StagingStore(staged(tmp_path))
    assert worker(db, art, writeback=True, tag_writer=NeverWrite()).process_automatic_queue().failed == 0
    assert art.staged == 1


def test_noop_rescan_retains_previous_art_and_release_witness_journal_links(library, tmp_path):
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    db, track, _ = library
    art = _StagingStore(staged(tmp_path))
    item = MetadataIntelligenceJobStore(db).enqueue_track(track)
    service = worker(db, art)
    assert service.process_automatic_queue().failed == 0
    saved = db.conn.execute("SELECT applied_history_group,field_proposal FROM metadata_intelligence_items WHERE id=?", (item.id,)).fetchone()
    original_art_group = saved["applied_history_group"]
    witness = json.loads(saved["field_proposal"])["_presentation_art_release_witness"]
    db.conn.execute("UPDATE metadata_intelligence_items SET state='queued',completed_at=NULL WHERE id=?", (item.id,))
    db.conn.execute("UPDATE metadata_intelligence_jobs SET status='created',completed_at=NULL WHERE id=?", (item.job_id,))
    db.conn.commit()
    result = service.process_automatic_queue()
    assert result.processed == 1 and result.failed == 0
    saved = db.conn.execute("SELECT applied_history_group,field_proposal FROM metadata_intelligence_items WHERE id=?", (item.id,)).fetchone()
    assert saved["applied_history_group"] is None  # No invented acceptance.
    history = json.loads(saved["field_proposal"])["_review_previous_applied_history_groups"]
    assert original_art_group in history and witness in history
    assert len(history) == len(set(history))
    assert art.staged == 1  # An already-selected catalogue cover is preserved.


@pytest.mark.parametrize("changes", [dict(release_id="999"), dict(attribution_text=""), dict(sha256="0" * 64)])
def test_staged_record_identity_attribution_and_digest_are_rechecked(library, tmp_path, changes):
    db, track, _ = library
    before = dump(db)
    with pytest.raises(ValueError):
        apply_artwork_upgrade(db, decision(db, track), staged(tmp_path, **changes))
    assert dump(db) == before
