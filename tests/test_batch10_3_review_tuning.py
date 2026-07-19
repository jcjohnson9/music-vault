from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.intelligence import MetadataIntelligenceService
from music_vault.metadata.review_policy import (
    ReviewOutcome,
    classify_ensemble_outcome,
    classify_stored_review_evidence,
)
from music_vault.metadata.review_reclassification import MetadataReviewReclassifier
from music_vault.metadata.soundtrack import SoundtrackKind, classify_soundtrack
from music_vault.metadata.title_parser import (
    parse_youtube_title,
    split_artist_version_suffix,
)
from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog


def _evidence(
    *,
    current: dict[str, object] | None = None,
    proposal: dict[str, object] | None = None,
    hints: dict[str, object] | None = None,
    confidence: dict[str, float] | None = None,
    agreement: str = "discogs_only",
    reason: str | None = "album_ambiguity",
):
    values: dict[str, object] = {
        "_current": current
        or {"title": "Synthetic Theme", "artist": "Synthetic Ensemble"},
        "_discogs": {"title": "Synthetic Theme", "score": 96}
        if agreement != "none"
        else {},
        "_musicbrainz": {},
        "_artwork": {"candidate_available": False},
        "_reasons": {},
    }
    values.update(proposal or {})
    return {
        "parsed_hints": hints or {},
        "field_proposal": values,
        "field_confidence": confidence or {},
        "provider_agreement": agreement,
        "review_reason": reason,
    }


def test_secondary_only_uncertainty_becomes_applied_with_gaps():
    decision = classify_stored_review_evidence(**_evidence())

    assert decision.outcome is ReviewOutcome.APPLIED_WITH_GAPS
    assert {"album", "release_date", "artwork"} <= set(decision.secondary_gaps)
    assert not decision.critical_conflicts


def test_critical_version_conflict_remains_needs_review():
    evidence = _evidence(reason="version_conflict")
    evidence["field_proposal"]["_reasons"] = {
        "version_type": ["version_identity_conflict"]
    }

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.NEEDS_REVIEW
    assert "version_type" in decision.critical_conflicts


def test_unknown_legacy_review_reason_remains_fail_closed():
    decision = classify_stored_review_evidence(
        **_evidence(reason="legacy_unclassified_conflict")
    )

    assert decision.outcome is ReviewOutcome.NEEDS_REVIEW
    assert "unclassified_legacy_reason" in decision.critical_conflicts


@pytest.mark.parametrize("field_name", ("field_proposal", "field_confidence"))
def test_malformed_stored_review_json_remains_fail_closed(field_name: str):
    evidence = _evidence(reason="album_ambiguity")
    evidence[field_name] = "{not-valid-json"

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.NEEDS_REVIEW
    assert decision.reason == "malformed_stored_review_evidence"
    assert decision.critical_conflicts == ("malformed_stored_evidence",)


@pytest.mark.parametrize(
    "nested_key",
    ("_current", "_reasons", "_discogs", "_musicbrainz", "_sources", "_artwork"),
)
def test_malformed_nested_review_evidence_remains_fail_closed(nested_key: str):
    evidence = _evidence(
        reason="strong_source_fallback",
        agreement="none",
        hints={
            "title": "Synthetic Theme",
            "artist": "Synthetic Ensemble",
            "pattern": "artist_dash_title",
        },
    )
    evidence["field_proposal"][nested_key] = ["corrupt-nested-evidence"]

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.NEEDS_REVIEW
    assert decision.reason == "malformed_stored_review_evidence"
    assert decision.critical_conflicts == ("malformed_stored_evidence",)


def test_unknown_stored_title_pattern_cannot_terminalize_source_fallback():
    evidence = _evidence(
        reason="strong_source_fallback",
        agreement="none",
        hints={
            "title": "Synthetic Theme",
            "artist": "Synthetic Ensemble",
            "pattern": "corrupt_future_pattern",
        },
    )

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.NEEDS_REVIEW
    assert decision.reason == "unrecognized_source_title_pattern"
    assert decision.critical_conflicts == ("unrecognized_source_title_pattern",)


def test_missing_critical_identity_never_auto_approves():
    decision = classify_stored_review_evidence(
        **_evidence(current={"title": "Synthetic Theme"}, reason="album_ambiguity")
    )

    assert decision.outcome is ReviewOutcome.NEEDS_REVIEW
    assert "missing_primary_artist_identity" in decision.critical_conflicts


def test_secondary_provider_disagreement_does_not_force_review():
    evidence = _evidence(reason="provider_disagreement", agreement="conflict")
    evidence["field_proposal"]["_reasons"] = {
        "album": ["provider_value_conflict", "release_context_ambiguous"]
    }

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.APPLIED_WITH_GAPS
    assert "exact_edition" in decision.secondary_gaps


def test_unknown_scope_provider_conflict_remains_needs_review():
    evidence = _evidence(reason=None, agreement="conflict")

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.NEEDS_REVIEW
    assert "provider_disagreement" in decision.critical_conflicts


def test_strong_source_title_without_provider_is_accepted_fallback():
    evidence = _evidence(
        agreement="none",
        reason="youtube_exclusive",
        hints={
            "title": "Synthetic Theme",
            "artist": "Synthetic Ensemble",
            "pattern": "artist_dash_title",
            "version_type": "unknown",
            "orientation": {
                "evaluated_count": 2,
                "selected": "left_is_artist",
                "provider_confirmed": False,
                "requires_provider_adjudication": True,
                "reasons": ["provisional_conventional_orientation"],
            },
        },
    )

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.SOURCE_FALLBACK
    assert decision.reason == "strong_source_fallback"


def test_provider_outage_does_not_become_terminal_source_fallback():
    ensemble = SimpleNamespace(
        fields=(),
        reasons=(),
        discogs_candidate=None,
        musicbrainz_candidate=None,
    )
    decision = classify_ensemble_outcome(
        ensemble,
        current={"title": "Synthetic Theme", "artist": "Synthetic Ensemble"},
        parsed_hints={
            "title": "Synthetic Theme",
            "artist": "Synthetic Ensemble",
            "pattern": "artist_dash_title",
        },
        changed=False,
        youtube_exclusive=True,
        provider_failures=("provider_unavailable",),
    )

    assert decision.outcome is ReviewOutcome.FAILED
    assert decision.reason == "provider_unavailable"


def test_soundtrack_edition_gap_accepts_critical_identity():
    evidence = _evidence(
        reason="soundtrack_edition_ambiguity",
        current={
            "title": "Synthetic Theme",
            "artist": "Synthetic Composer",
            "album": "Synthetic Film Original Motion Picture Soundtrack",
            "version_type": "soundtrack",
        },
        hints={
            "raw_title": "Synthetic Composer - Synthetic Theme (Original Score)",
            "title": "Synthetic Theme",
            "artist": "Synthetic Composer",
            "pattern": "artist_dash_title",
            "version_type": "soundtrack",
        },
    )

    decision = classify_stored_review_evidence(**evidence)

    assert decision.outcome is ReviewOutcome.APPLIED_WITH_GAPS
    assert "exact_edition" in decision.secondary_gaps
    assert decision.soundtrack is not None and decision.soundtrack.is_soundtrack


def test_soundtrack_classifier_distinguishes_score_cast_and_franchise_entry():
    game = classify_soundtrack(album="Synthetic Quest II - Video Game Soundtrack")
    score = classify_soundtrack(album="Synthetic Film - Original Motion Picture Score")
    stage = classify_soundtrack(album="Synthetic Musical - Original Broadway Cast Recording")
    film = classify_soundtrack(album="Synthetic Musical - Motion Picture Cast")

    assert game.kind is SoundtrackKind.GAME_SOUNDTRACK
    assert score.kind is SoundtrackKind.SCORE
    assert score.album_kind == "score"
    assert stage.kind is SoundtrackKind.STAGE_CAST
    assert film.kind is SoundtrackKind.FILM_CAST
    assert stage.kind is not film.kind


def test_various_artists_is_release_context_not_performer_identity():
    result = classify_soundtrack(
        album="Synthetic Film Soundtrack",
        album_artist="Various Artists",
        provider_credits=({"name": "Synthetic Performer"},),
    )

    assert result.is_soundtrack
    assert result.various_artists_release_context


def test_artist_live_at_suffix_moves_to_version_hint_without_splitting_band():
    assert split_artist_version_suffix("Synthetic Artist Live at Test Venue") == (
        "Synthetic Artist",
        "live",
        "Live at Test Venue",
    )
    assert split_artist_version_suffix("Synthetic & Ensemble") is None

    parsed = parse_youtube_title(
        "Synthetic Artist Live at Test Venue - Example Song"
    )
    assert parsed.artist_hint == "Synthetic Artist"
    assert parsed.title_hint == "Example Song"
    assert parsed.version_type == "live"
    assert parsed.version_label == "Live at Test Venue"


def test_artist_session_suffix_preserves_specific_version_taxonomy():
    assert split_artist_version_suffix("Synthetic Artist Acoustic Session") == (
        "Synthetic Artist",
        "acoustic",
        "Acoustic Session",
    )
    assert split_artist_version_suffix("Synthetic Artist Radio Session") == (
        "Synthetic Artist",
        "session",
        "Radio Session",
    )
    assert split_artist_version_suffix("Synthetic Artist Studio Session") == (
        "Synthetic Artist",
        "session",
        "Studio Session",
    )
    assert split_artist_version_suffix("Synthetic Artist Tiny Desk Concert") == (
        "Synthetic Artist",
        "live",
        "Tiny Desk Concert",
    )


def _add_track(db: MusicVaultDB, root: Path, index: int, *, title: str | None) -> int:
    media = root / f"synthetic-review-{index}.mp3"
    media.write_bytes(b"synthetic fixture")
    return db.upsert_track(
        media,
        title=title,
        artist="Synthetic Ensemble",
        source_kind="youtube",
        source_video_id=f"rev{index:08d}",
    )


def test_new_strong_youtube_source_becomes_fallback_without_provider_call(tmp_path):
    db = MusicVaultDB(tmp_path / "automatic.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _add_track(
        db,
        tmp_path,
        50,
        title="Synthetic Performer - Uncatalogued Theme",
    )
    MetadataIntelligenceJobStore(db).enqueue_track(track_id)
    service = MetadataIntelligenceService(
        db,
        {
            "metadata_intelligence_enabled": True,
            "metadata_discogs_enabled": False,
            "metadata_musicbrainz_secondary_enabled": False,
            "metadata_writeback_enabled": False,
            "metadata_fill_missing_artwork_enabled": False,
            "metadata_intelligence_consent_version": 1,
            "metadata_discogs_consent_version": 1,
        },
    )

    result = service.process_automatic_queue()

    assert result.source_fallback == 1
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE track_id=?", (track_id,)
    ).fetchone()[0] == "source_fallback"
    db.close()


def _mark_review(
    store: MetadataIntelligenceJobStore,
    job_id: str,
    evidence: dict[str, object],
) -> int:
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(item.id, "review", **evidence)
    return item.id


def test_reclassification_is_resumable_network_free_and_reconciles_counts(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3", backup_dir=tmp_path / "backups")
    track_ids = [
        _add_track(db, tmp_path, 1, title=None),
        _add_track(db, tmp_path, 2, title="Existing Source Title"),
        _add_track(db, tmp_path, 3, title="Critical Conflict"),
    ]
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job(track_ids)
    gap = _evidence(
        current={"artist": "Synthetic Ensemble"},
        proposal={
            "title": "Recovered Synthetic Title",
            "_sources": {"title": "discogs"},
        },
        confidence={"title": 96},
    )
    fallback = _evidence(
        agreement="none",
        reason="youtube_exclusive",
        hints={
            "title": "Source Title",
            "artist": "Source Artist",
            "pattern": "artist_dash_title",
            "orientation": {
                "evaluated_count": 2,
                "selected": "left_is_artist",
                "provider_confirmed": False,
                "requires_provider_adjudication": True,
                "reasons": ["provisional_conventional_orientation"],
            },
        },
    )
    conflict = _evidence(reason="version_conflict")
    conflict["field_proposal"]["_reasons"] = {
        "version_type": ["version_identity_conflict"]
    }
    item_ids = [
        _mark_review(store, job_id, gap),
        _mark_review(store, job_id, fallback),
        _mark_review(store, job_id, conflict),
    ]
    reclassifier = MetadataReviewReclassifier(db)

    first = reclassifier.reclassify(job_id=job_id, limit=2, apply=False)
    assert first.scanned == 2
    assert first.remaining == 1
    assert first.changed == 0
    assert store.aggregate_counts(job_id)["review"] == 3

    applied = reclassifier.reclassify(job_id=job_id, limit=20, apply=True)
    assert applied.scanned == 3
    assert applied.applied_with_gaps == 2
    assert applied.source_fallback == 1
    assert applied.needs_review == 0
    assert applied.safe_fields_applied == 2
    states = {
        int(row["id"]): str(row["state"])
        for row in db.conn.execute(
            "SELECT id,state FROM metadata_intelligence_items WHERE job_id=?", (job_id,)
        )
    }
    assert states[item_ids[0]] == "applied_with_gaps"
    assert states[item_ids[1]] == "source_fallback"
    assert states[item_ids[2]] == "applied_with_gaps"
    summary = store.job_summary(job_id)
    assert summary.review_items == 0
    assert summary.applied_with_gaps_items == 2
    assert summary.source_fallback_items == 1
    assert db.get_track(track_ids[0])["title"] == "Synthetic Theme"

    history = db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history WHERE track_id=? AND field_name='title'",
        (track_ids[0],),
    ).fetchone()[0]
    assert history == 1
    before_job = db.conn.execute(
        "SELECT updated_at FROM metadata_intelligence_jobs WHERE id=?", (job_id,)
    ).fetchone()[0]
    rerun = reclassifier.reclassify(job_id=job_id, apply=True)
    assert rerun.scanned == 0
    assert rerun.changed == 0
    assert db.conn.execute(
        "SELECT updated_at FROM metadata_intelligence_jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == before_job
    db.close()


def test_outcome_dashboard_defaults_to_all_and_keeps_legacy_pending_auditable(
    tmp_path, qapp
):
    db = MusicVaultDB(tmp_path / "ui.sqlite3", backup_dir=tmp_path / "backups")
    track_ids = [
        _add_track(db, tmp_path, 10, title="Applied Gap"),
        _add_track(db, tmp_path, 11, title="Fallback"),
        _add_track(db, tmp_path, 12, title="Conflict"),
    ]
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job(track_ids)
    claimed = [store.claim_next_item(job_id) for _ in track_ids]
    store.mark_item(claimed[0].id, "applied_with_gaps", **_evidence())
    store.mark_item(
        claimed[1].id,
        "source_fallback",
        **_evidence(
            agreement="none",
            reason="strong_source_fallback",
            hints={
                "title": "Fallback",
                "artist": "Synthetic Ensemble",
                "pattern": "artist_dash_title",
                "orientation": {
                    "evaluated_count": 2,
                    "selected": "left_is_artist",
                    "provider_confirmed": False,
                    "requires_provider_adjudication": True,
                    "reasons": ["provisional_conventional_orientation"],
                },
            },
        ),
    )
    store.mark_item(claimed[2].id, "review", **_evidence(reason="version_conflict"))

    dialog = MetadataIntelligenceDialog(db)
    assert dialog.filter_combo.currentData() is None
    assert dialog.filter_combo.findData("needs_review") == -1
    assert dialog.table.rowCount() == 3
    assert {dialog.table.item(row, 0).text() for row in range(3)} == {
        "Legacy Pending",
        "Applied with Gaps",
        "Accepted Source Fallback",
    }
    assert "Pending: 1" in dialog.summary.text()
    assert "Applied with Gaps: 1" in dialog.summary.text()
    assert "Source Fallback: 1" in dialog.summary.text()

    dialog.show_incomplete_checkbox.setChecked(True)
    assert dialog.table.rowCount() == 3
    dialog.filter_combo.setCurrentIndex(dialog.filter_combo.findData("source_fallback"))
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).text() == "Accepted Source Fallback"
    dialog.close()
    db.close()


def test_reclassification_uses_savepoint_inside_schema_migration_transaction(tmp_path):
    db = MusicVaultDB(tmp_path / "atomic.sqlite3", backup_dir=tmp_path / "backups")
    track_id = _add_track(db, tmp_path, 90, title="Atomic Fixture")
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item_id = _mark_review(store, job_id, _evidence())

    db.conn.execute("BEGIN")
    report = MetadataReviewReclassifier(db).reclassify(job_id=job_id, apply=True)
    assert report.changed == 1
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item_id,)
    ).fetchone()[0] == "applied_with_gaps"
    db.conn.rollback()

    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item_id,)
    ).fetchone()[0] == "review"
    db.close()
