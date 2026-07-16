from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox

from music_vault.core import paths
from music_vault.core.db import MusicVaultDB
from music_vault.app import MusicVaultWindow
from music_vault.metadata.intelligence import (
    AUTOMATIC_IMPORT_JOB_ID,
    IntelligenceRunResult,
    MetadataIntelligenceService,
)
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.service import MetadataService
from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog


@pytest.fixture
def intelligence_review_context(tmp_path, monkeypatch, qapp):
    runtime = tmp_path / "runtime"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    paths._resolved_project_root.cache_clear()
    db = MusicVaultDB(runtime / "data" / "music_vault.sqlite3")

    def add_track(index: int) -> int:
        media = runtime / f"synthetic-review-{index}.mp3"
        media.write_bytes(b"synthetic media fixture")
        return db.upsert_track(
            media,
            title=f"Current Title {index}",
            artist="Current Artist",
            album="Current Album",
            duration_seconds=241.5,
            source_kind="youtube",
            source_video_id=f"syn{index:08d}",
            source_upload_date="2026-01-02",
        )

    yield db, runtime, add_track
    db.close()
    paths._resolved_project_root.cache_clear()


def _proposal(*, title: str = "Proposed Title") -> dict[str, object]:
    return {
        "_current": {
            "artist": "Current Artist",
            "title": "Current Title",
            "album": "Current Album",
            "duration_seconds": 241.5,
            "artwork": True,
        },
        "_discogs": {
            "artist": "Discogs Artist",
            "title": "Discogs Title",
            "album": "Discogs Release",
            "duration_seconds": 242,
            "score": 96,
        },
        "_musicbrainz": {
            "artist": "MusicBrainz Artist",
            "title": "MusicBrainz Title",
            "album": "MusicBrainz Release",
            "duration_seconds": 240,
            "score": 92,
        },
        "_artwork": {
            "candidate_available": True,
            "result": "download ready",
        },
        "title": title,
        "album": "Proposed Album",
        "original_release_date": "1984",
        "version_type": "live",
        "artist_credits": [
            {"display_name": "Normalized Credit", "role": "primary"}
        ],
    }


def _create_job(
    db: MusicVaultDB,
    track_ids: list[int],
    specifications: list[Mapping[str, object]],
) -> tuple[str, list[int]]:
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job(track_ids)
    item_ids: list[int] = []
    for specification in specifications:
        item = store.claim_next_item(job_id)
        assert item is not None
        item_ids.append(item.id)
        store.mark_item(
            item.id,
            str(specification["state"]),
            parsed_hints=specification.get("parsed_hints"),
            discogs_release_id=specification.get("discogs_release_id"),
            discogs_master_id=specification.get("discogs_master_id"),
            field_proposal=specification.get("field_proposal"),
            field_confidence=specification.get("field_confidence"),
            provider_agreement=str(
                specification.get("provider_agreement", "unknown")
            ),
            review_reason=specification.get("review_reason"),
            error=specification.get("error"),
        )
    return job_id, item_ids


def test_review_filters_cover_states_and_every_ambiguity_reason(
    intelligence_review_context,
):
    db, _runtime, add_track = intelligence_review_context
    specifications = [
        {"state": "applied"},
        {"state": "review", "review_reason": "provider_disagreement"},
        {"state": "review", "review_reason": "version_conflict"},
        {"state": "review", "review_reason": "album_ambiguity"},
        {"state": "review", "review_reason": "date_ambiguity"},
        {"state": "review", "review_reason": "artist_ambiguity"},
        {"state": "review", "review_reason": "youtube_exclusive"},
        {"state": "ready"},
        {"state": "no_match"},
        {"state": "failed", "error": "synthetic provider outage"},
    ]
    track_ids = [add_track(index) for index in range(len(specifications))]
    _create_job(db, track_ids, specifications)
    dialog = MetadataIntelligenceDialog(db)

    expected_counts = {
        None: 10,
        "applied": 1,
        "needs_review": 7,
        "provider_disagreement": 1,
        "version_conflict": 1,
        "album_ambiguity": 1,
        "date_ambiguity": 1,
        "artist_ambiguity": 1,
        "youtube_exclusive": 1,
        "no_match": 1,
        "failed": 1,
    }
    for filter_value, expected_count in expected_counts.items():
        index = dialog.filter_combo.findData(filter_value)
        assert index >= 0, filter_value
        dialog.filter_combo.setCurrentIndex(index)
        assert dialog.table.rowCount() == expected_count, filter_value
        if filter_value in {
            "provider_disagreement",
            "version_conflict",
            "album_ambiguity",
            "date_ambiguity",
            "artist_ambiguity",
            "youtube_exclusive",
        }:
            assert dialog.table.item(0, 11).text() == filter_value

    dialog.filter_combo.setCurrentIndex(
        dialog.filter_combo.findData("needs_review")
    )
    assert {
        dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount())
    } == {"Review", "Ready"}


def test_normalized_proposal_renders_provider_facts_without_raw_json(
    intelligence_review_context,
):
    db, _runtime, add_track = intelligence_review_context
    track_id = add_track(20)
    hints = {
        "raw_title": "Synthetic Upload Title (Live)",
        "uploader": "Synthetic Uploader",
        "version_type": "live",
    }
    _create_job(
        db,
        [track_id],
        [
            {
                "state": "review",
                "review_reason": "provider_disagreement",
                "provider_agreement": "conflict",
                "parsed_hints": hints,
                "field_proposal": _proposal(),
                "field_confidence": {"title": 96, "album": 92},
            }
        ],
    )
    dialog = MetadataIntelligenceDialog(db)

    assert dialog.table.rowCount() == 1
    cells = [dialog.table.item(0, column).text() for column in range(12)]
    assert dialog.table.columnWidth(1) >= 180
    assert dialog.table.columnWidth(4) >= 180
    assert all(
        dialog.table.item(0, column).toolTip() == cells[column]
        for column in range(12)
    )
    assert "Current Artist" in cells[1]
    assert "Current Title" in cells[1]
    assert "Current Album" in cells[1]
    assert cells[2] == hints["raw_title"]
    assert cells[3] == hints["uploader"]
    assert "Discogs Artist" in cells[4]
    assert "Discogs Title" in cells[4]
    assert "score 96" in cells[4]
    assert "MusicBrainz Artist" in cells[5]
    assert "conflict" in cells[5]
    assert "Local: 241.5s" in cells[6]
    assert "Discogs: 242s" in cells[6]
    assert "MusicBrainz: 240s" in cells[6]
    assert cells[7] == "live"
    assert cells[8] == "Discogs Release"
    assert "Current: Present" in cells[9]
    assert "candidate available" in cells[9]
    assert "download ready" in cells[9]
    assert {"Title", "Album", "Original Release Date", "Version Type"} <= set(
        cells[10].split(", ")
    )
    assert cells[11] == "provider_disagreement"
    assert not any(
        marker in " ".join(cells)
        for marker in ("_current", "_discogs", "_musicbrainz", "artist_credits")
    )


def test_field_checkboxes_apply_only_selected_scalars_with_locks_and_history(
    intelligence_review_context,
    qapp,
):
    db, _runtime, add_track = intelligence_review_context
    track_id = add_track(30)
    _job_id, item_ids = _create_job(
        db,
        [track_id],
        [
            {
                "state": "review",
                "review_reason": "version_conflict",
                "provider_agreement": "conflict",
                "field_proposal": _proposal(title="Manually Confirmed Title"),
            }
        ],
    )
    service = MetadataIntelligenceService(
        db,
        {"metadata_intelligence_enabled": True},
    )
    dialog = MetadataIntelligenceDialog(db, service=service)
    emitted: list[int] = []
    dialog.review_applied.connect(emitted.append)

    dialog.table.selectRow(0)
    qapp.processEvents()
    assert set(dialog.field_checks) == {
        "title",
        "album",
        "original_release_date",
        "version_type",
    }
    assert all(checkbox.isChecked() for checkbox in dialog.field_checks.values())
    assert dialog.apply_fields_button.isEnabled()
    dialog.field_checks["album"].setChecked(False)
    dialog._apply_selected_fields()

    snapshot = MetadataService(db).snapshot(track_id)
    selected_fields = {"title", "original_release_date", "version_type"}
    assert snapshot.value("title") == "Manually Confirmed Title"
    assert snapshot.value("album") == "Current Album"
    assert snapshot.value("original_release_date") == "1984"
    assert snapshot.value("version_type") == "live"
    assert all(
        snapshot.fields[field].is_manual and snapshot.fields[field].is_locked
        for field in selected_fields
    )
    history = db.conn.execute(
        "SELECT field_name,change_group_id,actor,reason,new_is_manual,new_is_locked "
        "FROM track_metadata_history WHERE track_id=? "
        "AND reason='metadata_intelligence_review_selection' ORDER BY field_name",
        (track_id,),
    ).fetchall()
    assert {str(row["field_name"]) for row in history} == selected_fields
    assert len({str(row["change_group_id"]) for row in history}) == 1
    assert all(
        row["actor"] == "user"
        and int(row["new_is_manual"]) == 1
        and int(row["new_is_locked"]) == 1
        for row in history
    )
    item = db.conn.execute(
        "SELECT state,review_reason,applied_history_group "
        "FROM metadata_intelligence_items WHERE id=?",
        (item_ids[0],),
    ).fetchone()
    assert item is not None
    assert item["state"] == "applied"
    assert item["review_reason"] is None
    assert item["applied_history_group"] == history[0]["change_group_id"]
    assert emitted == [track_id]


def test_resume_signal_emits_only_after_a_successful_persisted_resume(
    intelligence_review_context,
    monkeypatch,
):
    db, _runtime, add_track = intelligence_review_context
    track_id = add_track(40)
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    store.pause(job_id)
    service = MetadataIntelligenceService(
        db,
        {"metadata_intelligence_enabled": True},
    )
    dialog = MetadataIntelligenceDialog(db, service=service)
    emitted: list[tuple[str, str]] = []
    warnings: list[str] = []
    dialog.resume_requested.connect(
        lambda persisted_id, persisted_kind: emitted.append(
            (persisted_id, persisted_kind)
        )
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(str(message)),
    )

    dialog.resume_job()

    status = db.conn.execute(
        "SELECT status FROM metadata_intelligence_jobs WHERE id=?",
        (job_id,),
    ).fetchone()[0]
    assert status == "analyzing"
    assert emitted == [(job_id, "existing_library")]
    assert warnings == []

    dialog.resume_job()

    assert emitted == [(job_id, "existing_library")]
    assert len(warnings) == 1
    assert "paused" in warnings[0].casefold()


def test_failed_automatic_job_resume_emits_persisted_kind_and_id(
    intelligence_review_context,
):
    db, _runtime, add_track = intelligence_review_context
    store = MetadataIntelligenceJobStore(db)
    item = store.enqueue_track(add_track(41))
    claimed = store.claim_next_item(AUTOMATIC_IMPORT_JOB_ID)
    assert claimed is not None and claimed.id == item.id
    store.mark_item(claimed.id, "failed", error="synthetic provider outage")
    service = MetadataIntelligenceService(
        db,
        {"metadata_intelligence_enabled": True},
    )
    dialog = MetadataIntelligenceDialog(db, service=service)
    emitted: list[tuple[str, str]] = []
    dialog.resume_requested.connect(
        lambda persisted_id, persisted_kind: emitted.append(
            (persisted_id, persisted_kind)
        )
    )

    dialog.resume_job()

    row = db.conn.execute(
        "SELECT job_kind,status FROM metadata_intelligence_jobs WHERE id=?",
        (AUTOMATIC_IMPORT_JOB_ID,),
    ).fetchone()
    item_state = db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?",
        (item.id,),
    ).fetchone()[0]
    assert tuple(row) == ("new_import", "analyzing")
    assert item_state == "queued"
    assert emitted == [(AUTOMATIC_IMPORT_JOB_ID, "new_import")]


def test_app_resume_routes_each_persisted_job_kind_to_its_exact_worker():
    class Tasks:
        pending_count = 0

        def __init__(self):
            self.submissions: list[tuple[str, object]] = []

        def submit(self, kind, work):
            self.submissions.append((kind, work))

    class Service:
        def __init__(self):
            self.calls: list[tuple[str, str, object]] = []

        def process_automatic_queue(self, *, job_id, cancel_event):
            self.calls.append(("automatic", job_id, cancel_event))

        def analyze_existing_library(self, *, job_id, cancel_event):
            self.calls.append(("existing", job_id, cancel_event))

    service = Service()
    tasks = Tasks()
    window = SimpleNamespace(
        metadata_intelligence_tasks=tasks,
        metadata_intelligence_service=service,
    )

    MusicVaultWindow.resume_metadata_intelligence_job(
        window, AUTOMATIC_IMPORT_JOB_ID, "new_import"
    )
    automatic_kind, automatic_work = tasks.submissions.pop(0)
    automatic_cancel = object()
    automatic_work(automatic_cancel)

    existing_id = "persisted-existing-job"
    MusicVaultWindow.resume_metadata_intelligence_job(
        window, existing_id, "existing_library"
    )
    existing_kind, existing_work = tasks.submissions.pop(0)
    existing_cancel = object()
    existing_work(existing_cancel)

    assert automatic_kind == "metadata_automatic_imports"
    assert existing_kind == "metadata_existing_library"
    assert service.calls == [
        ("automatic", AUTOMATIC_IMPORT_JOB_ID, automatic_cancel),
        ("existing", existing_id, existing_cancel),
    ]

    MusicVaultWindow.resume_metadata_intelligence_job(
        window, "wrong-automatic-id", "new_import"
    )
    MusicVaultWindow.resume_metadata_intelligence_job(
        window, "unknown-job", "unsupported"
    )
    assert tasks.submissions == []


def test_failed_existing_retry_targets_requested_job_not_newer_latest(
    intelligence_review_context,
    monkeypatch,
):
    db, _runtime, add_track = intelligence_review_context
    store = MetadataIntelligenceJobStore(db)
    requested_id = store.create_existing_library_job([add_track(42)])
    requested_item = store.claim_next_item(requested_id)
    assert requested_item is not None
    store.mark_item(
        requested_item.id,
        "failed",
        error="synthetic retryable provider outage",
    )
    newer_id = store.create_existing_library_job([add_track(43)])
    newer_before = tuple(
        db.conn.execute(
            "SELECT status,updated_at FROM metadata_intelligence_jobs WHERE id=?",
            (newer_id,),
        ).fetchone()
    )
    store.enqueue_track(add_track(44))
    service = MetadataIntelligenceService(
        db,
        {
            "metadata_intelligence_enabled": True,
            "metadata_intelligence_consent_version": 1,
            "metadata_musicbrainz_secondary_enabled": False,
        },
    )
    service.resume_job(requested_id)
    processed: list[str] = []

    def run_exact(_worker_db, exact_job_id, *, cancel_event=None):
        processed.append(exact_job_id)
        return IntelligenceRunResult(exact_job_id, 0, 0, 0, 0, 0)

    monkeypatch.setattr(service, "_run_job", run_exact)

    result = service.analyze_existing_library(job_id=requested_id)

    assert result.job_id == requested_id
    assert processed == [requested_id]
    assert requested_id != newer_id
    requested = db.conn.execute(
        "SELECT status FROM metadata_intelligence_jobs WHERE id=?",
        (requested_id,),
    ).fetchone()
    newer = tuple(db.conn.execute(
        "SELECT status,updated_at FROM metadata_intelligence_jobs WHERE id=?",
        (newer_id,),
    ).fetchone())
    assert requested[0] in {"created", "analyzing"}
    assert newer == newer_before
    with pytest.raises(ValueError, match="job_kind_invalid"):
        service.analyze_existing_library(job_id=AUTOMATIC_IMPORT_JOB_ID)
    with pytest.raises(ValueError, match="automatic_metadata_job_id_invalid"):
        service.process_automatic_queue(job_id="wrong-automatic-id")


def test_discogs_attribution_tracks_selected_row_and_rejects_raw_urls(
    intelligence_review_context,
    qapp,
):
    db, _runtime, add_track = intelligence_review_context

    def proposal(reference: str, **identity: object) -> dict[str, object]:
        return {"_discogs": {"provider_reference": reference, **identity}}

    specifications = [
        {
            "state": "review",
            "discogs_release_id": "101",
            "field_proposal": proposal(
                "https://www.discogs.com/release/101-safe-slug"
            ),
        },
        {
            "state": "review",
            "discogs_master_id": "202",
            "field_proposal": proposal("https://discogs.com/master/202"),
        },
        {
            "state": "review",
            "field_proposal": proposal(
                "https://www.discogs.com/artist/303",
                artist_id="303",
            ),
        },
        {
            "state": "review",
            "discogs_release_id": "404",
            "field_proposal": proposal(
                "https://untrusted.invalid/release/404?credential=private"
            ),
        },
        {
            "state": "review",
            "field_proposal": proposal("javascript:alert('unsafe')"),
        },
    ]
    track_ids = [add_track(50 + index) for index in range(len(specifications))]
    _create_job(db, track_ids, specifications)
    dialog = MetadataIntelligenceDialog(db)

    assert isinstance(dialog.discogs_attribution_label, QLabel)
    assert dialog.discogs_attribution_label.openExternalLinks()
    assert (
        dialog.discogs_attribution_label.textInteractionFlags()
        & Qt.TextInteractionFlag.TextBrowserInteraction
    )
    expected = (
        "https://www.discogs.com/release/101",
        "https://www.discogs.com/master/202",
        "https://www.discogs.com/artist/303",
        "https://www.discogs.com/release/404",
        "https://www.discogs.com/",
    )
    for row, url in enumerate(expected):
        dialog.table.selectRow(row)
        qapp.processEvents()
        text = dialog.discogs_attribution_label.text()
        assert url in text
        assert "untrusted.invalid" not in text
        assert "credential" not in text
        assert "javascript:" not in text
