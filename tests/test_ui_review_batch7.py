from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QWidget

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.ui import review
from music_vault.ui.review import (
    REMEDIATION_REVIEW_SCENES,
    REVIEW_SCHEMA_VERSION,
    ReviewPlan,
    ReviewSize,
    SCENE_LABELS,
    load_review_plan,
    prepare_review_scene,
    remediation_review_metrics,
    review_scene_ready,
    validate_remediation_review_behaviors,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _capture_module():
    path = PROJECT_ROOT / "tools" / "dev" / "capture_ui_review.py"
    spec = importlib.util.spec_from_file_location("batch7_capture_ui_review", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch7_review_scene_contract_is_bounded_complete_and_unique():
    assert len(REMEDIATION_REVIEW_SCENES) == 16
    assert len(set(REMEDIATION_REVIEW_SCENES)) == 16
    assert all(scene in SCENE_LABELS for scene in REMEDIATION_REVIEW_SCENES)
    assert {
        "remediation_empty",
        "remediation_analyzing",
        "remediation_paused",
        "remediation_mixed_ready",
        "remediation_high_confirmation",
        "remediation_insufficient_disk",
        "remediation_needs_review",
        "remediation_ambiguous",
        "remediation_no_match",
        "remediation_artwork_comparison",
        "remediation_apply_progress",
        "remediation_complete_issues",
        "remediation_failed",
        "remediation_rollback_confirmation",
        "remediation_rolled_back",
        "remediation_long_values",
    } == set(REMEDIATION_REVIEW_SCENES)


def test_review_plan_keeps_batch7_matrix_within_twenty_captures(tmp_path):
    runtime = tmp_path / "runtime"
    output = tmp_path / "output"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    request = runtime / "plan.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "runtime_root": str(runtime),
                "output_dir": str(output),
                "sizes": [
                    {"width": 1440, "height": 900},
                ],
                "scenes": list(REMEDIATION_REVIEW_SCENES),
                "settle_ms": 100,
                "expected_capture_count": 16,
            }
        ),
        encoding="utf-8",
    )
    plan = load_review_plan(request)
    assert plan.capture_count == 16
    assert plan.capture_count <= 20
    assert plan.scenes == REMEDIATION_REVIEW_SCENES
    assert ReviewSize(1100, 720).width == 1100
    assert ReviewSize(1920, 1080).height == 1080


class _Pages:
    def setCurrentWidget(self, page):
        self.current = page


class _ReviewWindow(QWidget):
    def __init__(self, db_path: Path):
        super().__init__()
        self.resize(1200, 800)
        self.pages = _Pages()
        self.library_page = QWidget(self)
        self.db = SimpleNamespace(db_path=db_path, backup_dir=db_path.parent / "backups")


def test_each_remediation_scene_builds_fake_only_dashboard_state(tmp_path, qapp):
    window = _ReviewWindow(tmp_path / "synthetic.sqlite3")
    window.show()
    try:
        for scene in REMEDIATION_REVIEW_SCENES:
            prepare_review_scene(window, scene)
            qapp.processEvents()
            assert review_scene_ready(window, scene)
            metrics = remediation_review_metrics(window, scene)
            assert metrics is not None
            assert metrics["dialog_visible"] is True
            assert metrics["metric_card_count"] == 10
            assert metrics["control_count"] >= 12
            assert metrics["synthetic_provider_active"] is True
            assert metrics["public_provider_call_count"] == 0
            assert metrics["private_path_visible"] is False
            assert metrics["review_geometry_widget_count"] >= 20
            assert metrics["review_geometry_overlap_count"] == 0
            assert metrics["review_geometry_clipped_count"] == 0
            assert metrics["review_group_clipped_count"] == 0
            assert metrics["job_present"] is (scene != "remediation_empty")
            if scene in {
                "remediation_high_confirmation",
                "remediation_insufficient_disk",
                "remediation_rollback_confirmation",
            }:
                assert metrics["confirmation_visible"] is True
            if scene in {
                "remediation_needs_review",
                "remediation_artwork_comparison",
            }:
                assert metrics["release_choice_count"] >= 1
                assert metrics["release_identity_complete"] is True
        review._close_review_remediation_dialog(window)
    finally:
        window.close()


def test_1100x720_long_review_has_no_overlap_or_clipping(tmp_path, qapp):
    window = _ReviewWindow(tmp_path / "synthetic.sqlite3")
    window.resize(1100, 720)
    window.show()
    try:
        prepare_review_scene(window, "remediation_long_values")
        qapp.processEvents()
        dialog = window._review_remediation_dialog
        dialog.layout().activate()
        qapp.processEvents()
        metrics = remediation_review_metrics(window, "remediation_long_values")
        assert metrics["review_geometry_widget_count"] >= 20
        assert metrics["review_geometry_overlap_count"] == 0
        assert metrics["review_geometry_clipped_count"] == 0
        assert metrics["review_group_clipped_count"] == 0
        assert metrics["review_group_height"] >= 210
        assert len({card.y() for card in dialog.metric_cards.values()}) == 1
    finally:
        review._close_review_remediation_dialog(window)
        window.close()


def test_artwork_review_metrics_prove_real_comparison_without_preapproval_or_paths(
    tmp_path, qapp
):
    window = _ReviewWindow(tmp_path / "synthetic.sqlite3")
    window.show()
    try:
        prepare_review_scene(window, "remediation_artwork_comparison")
        qapp.processEvents()
        metrics = remediation_review_metrics(window, "remediation_artwork_comparison")
        assert metrics["selected_row_count"] == 1
        assert metrics["artwork_field_selected"] is False
        assert metrics["current_artwork_rendered"] is True
        assert metrics["candidate_artwork_rendered"] is True
        assert metrics["release_choice_count"] == 1
        assert metrics["release_identity_complete"] is True
        assert "Synthetic" not in json.dumps(metrics)
        assert str(tmp_path) not in json.dumps(metrics)
    finally:
        review._close_review_remediation_dialog(window)
        window.close()


def test_capture_seed_is_schema4_has_valid_mp3_and_no_key(tmp_path):
    tool = _capture_module()
    runtime = tmp_path / "synthetic_runtime"
    (runtime / "data").mkdir(parents=True)
    (runtime / "profile").mkdir()
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")

    result = tool.seed_synthetic_runtime(PROJECT_ROOT, runtime)
    database = runtime / "data" / "music_vault.sqlite3"
    db = MusicVaultDB(database, backup_dir=runtime / "data" / "backups")
    try:
        first = db.conn.execute("SELECT path FROM tracks ORDER BY id LIMIT 1").fetchone()
        assert first is not None
        mp3 = Path(str(first["path"]))
        from music_vault.metadata.tag_writer import inspect_mp3

        assert inspect_mp3(mp3).audio_payload_sha256
        assert (
            db.conn.execute("PRAGMA user_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
    assert result["track_count"] == 300
    assert result["synthetic_mp3_count"] == 1
    assert not (runtime / "data" / "youtube_api_key.txt").exists()


def test_functional_remediation_validation_is_synthetic_and_repeatable(
    tmp_path,
    monkeypatch,
):
    tool = _capture_module()
    runtime = tmp_path / "synthetic_runtime"
    output = tmp_path / "output"
    (runtime / "data").mkdir(parents=True)
    (runtime / "profile").mkdir()
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    tool.seed_synthetic_runtime(PROJECT_ROOT, runtime)
    (runtime / "data" / "music_vault_status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "app": "Music Vault",
                "health": {"ok": True},
                "library": {"track_count": 300},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    monkeypatch.setenv("HOME", str(runtime / "profile"))
    monkeypatch.setenv("USERPROFILE", str(runtime / "profile"))
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    from music_vault.core import paths

    paths._resolved_project_root.cache_clear()
    db = MusicVaultDB(
        runtime / "data" / "music_vault.sqlite3",
        backup_dir=runtime / "data" / "backups",
    )
    plan = ReviewPlan(
        request_path=runtime / "plan.json",
        runtime_root=runtime.resolve(),
        output_dir=output.resolve(),
        sizes=(ReviewSize(1100, 720),),
        scenes=("remediation_mixed_ready",),
        settle_ms=100,
    )
    window = SimpleNamespace(
        db=db,
        manual_queue=[2, 3],
        base_playback_context={"track_ids": [1, 2, 3], "current_track_id": 1},
    )
    try:
        evidence = validate_remediation_review_behaviors(window, plan)
        assert evidence and all(evidence.values())
        assert evidence["non_destructive_analysis"] is True
        assert evidence["exact_media_backup"] is True
        assert evidence["audio_payload_unchanged"] is True
        assert evidence["rollback_exact"] is True
        assert evidence["resumable_after_restart"] is True
        # A second packaged scene reuses aggregate evidence rather than
        # repeating provider/application work.
        assert validate_remediation_review_behaviors(window, plan) == evidence
    finally:
        db.close()
        paths._resolved_project_root.cache_clear()


def test_restart_checkpoint_survives_fresh_database_and_service(
    tmp_path,
    monkeypatch,
):
    tool = _capture_module()
    runtime = tmp_path / "synthetic_runtime"
    output = tmp_path / "output"
    (runtime / "data").mkdir(parents=True)
    (runtime / "profile").mkdir()
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    tool.seed_synthetic_runtime(PROJECT_ROOT, runtime)

    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    monkeypatch.setenv("HOME", str(runtime / "profile"))
    monkeypatch.setenv("USERPROFILE", str(runtime / "profile"))
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    from music_vault.core import paths

    paths._resolved_project_root.cache_clear()
    plan = ReviewPlan(
        request_path=runtime / "plan.json",
        runtime_root=runtime.resolve(),
        output_dir=output.resolve(),
        sizes=(ReviewSize(1100, 720),),
        scenes=("remediation_paused",),
        settle_ms=100,
    )
    first_db = MusicVaultDB(
        runtime / "data" / "music_vault.sqlite3",
        backup_dir=runtime / "data" / "backups",
    )
    try:
        monkeypatch.setenv(review.REMEDIATION_RESTART_PHASE_ENV, "prepare")
        prepared = validate_remediation_review_behaviors(
            SimpleNamespace(db=first_db),
            plan,
        )
        assert prepared["restart_checkpoint_created"] is True
        aggregate = tool.validate_remediation_restart_checkpoint(
            runtime,
            packaged=False,
        )
        assert aggregate["partial_analyzed"] == 1
        assert "job_id" not in aggregate
    finally:
        first_db.close()

    checkpoint_path = runtime / "data" / tool.REMEDIATION_RESTART_CHECKPOINT
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    creator_pid = int(checkpoint["creator_pid"])
    monkeypatch.delenv(review.REMEDIATION_RESTART_PHASE_ENV)
    monkeypatch.setenv(review.REMEDIATION_RESTART_REQUIRED_ENV, "source")
    monkeypatch.setattr(review.os, "getpid", lambda: creator_pid + 1)

    second_db = MusicVaultDB(
        runtime / "data" / "music_vault.sqlite3",
        backup_dir=runtime / "data" / "backups",
    )
    try:
        from music_vault.metadata.remediation import RemediationService

        rows = [
            dict(row)
            for row in second_db.conn.execute(
                "SELECT * FROM tracks ORDER BY id LIMIT 4"
            ).fetchall()
        ]
        provider = review._SyntheticRemediationProvider(rows)
        service = RemediationService(
            second_db,
            provider=provider,
            cover_provider=review._SyntheticRemediationCoverProvider(),
            reports_root=runtime / "data" / "metadata_reports",
            backups_root=runtime / "data" / "backups" / "metadata_jobs",
            sleep=lambda _seconds: None,
        )
        resumed, packaged = review._resume_remediation_restart_checkpoint(
            service,
            provider,
            plan,
        )
        assert resumed is True
        assert packaged is False
        summary = service.status(str(checkpoint["job_id"]))
        assert summary.status == "ready"
        assert summary.analyzed == summary.total
        assert provider.calls
    finally:
        second_db.close()
        paths._resolved_project_root.cache_clear()
