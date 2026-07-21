from __future__ import annotations

import json
from pathlib import Path

from music_vault.core import paths as runtime_paths
from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.ui import review as ui_review
from tools.dev import run_batch10_3_source_migration_proof as source_proof


def _review_plan(runtime: Path, output: Path) -> ui_review.ReviewPlan:
    request = runtime / "batch10_3-review-plan.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": ui_review.REVIEW_SCHEMA_VERSION,
                "runtime_root": str(runtime),
                "output_dir": str(output),
                "sizes": [{"width": 1280, "height": 720}],
                "scenes": list(ui_review.BATCH10_3_REVIEW_SCENES),
                "settle_ms": 100,
                "expected_capture_count": 1,
            }
        ),
        encoding="utf-8",
    )
    return ui_review.load_review_plan(request)


def test_batch10_3_source_ui_smoke_uses_real_window_handlers_offline(
    tmp_path: Path,
    monkeypatch,
    qapp,
) -> None:
    runtime = (tmp_path / "synthetic-runtime").resolve()
    output = (tmp_path / "review-output").resolve()
    data = runtime / "data"
    downloads = data / "youtube_downloads"
    backups = data / "backups"
    downloads.mkdir(parents=True)
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# synthetic review marker\n", encoding="utf-8")
    (data / "music_vault_config.json").write_text(
        json.dumps(
            {
                "download_folder": str(downloads),
                "audio_quality": "320",
                "volume_percent": 23,
                "artist_image_fetch_enabled": False,
                "onboarding_completed": True,
                "metadata_intelligence_enabled": False,
                "metadata_discogs_enabled": False,
                "metadata_writeback_enabled": False,
                "metadata_fill_missing_artwork_enabled": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plan = _review_plan(runtime, output)

    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_DISABLE_NETWORK", "1")
    monkeypatch.setenv("MUSIC_VAULT_UI_REVIEW", str(plan.request_path))
    monkeypatch.setenv("MUSIC_VAULT_ARTIST_IMAGE_PROVIDER", "synthetic")
    monkeypatch.setattr(runtime_paths, "_configured_data_directory", None)
    runtime_paths._resolved_project_root.cache_clear()

    database = data / "music_vault.sqlite3"
    source_proof._create_synthetic_schema6(database, backups, runtime)
    migrated = MusicVaultDB(database, backup_dir=backups)
    migrated.close()

    from music_vault import app as app_module

    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "use_system_default_audio_output",
        lambda self: None,
    )
    monkeypatch.setattr(app_module, "apply_dark_title_bar", lambda _window: False)
    monkeypatch.setattr(
        ui_review,
        "_REVIEW_NETWORK_GUARD_INSTALLED",
        True,
    )
    monkeypatch.setattr(ui_review, "_REVIEW_NETWORK_EVENTS", [])

    window = app_module.MusicVaultWindow()
    window.show()
    qapp.processEvents()
    try:
        runtime_checks = ui_review.validate_review_runtime(plan)
        evidence = ui_review.validate_batch10_3_review_behaviors(window, plan)

        assert runtime_checks["resolver_isolated"] is True
        assert runtime_checks["schema_version"] == CURRENT_SCHEMA_VERSION
        assert evidence["packaged_process"] is False
        assert evidence["schema_version"] == CURRENT_SCHEMA_VERSION
        assert evidence["canonical_multi_edition_card_count"] >= 1
        assert evidence["soundtrack_card_count"] >= 1
        assert evidence["score_card_count"] >= 1
        assert evidence["corrected_version_alias_count"] >= 1
        assert evidence["review_outcome_counts"] == {
            "applied_with_gaps": 2,
            "source_fallback": 1,
        }
        assert evidence["network_attempt_count"] == 0
        assert all(
            value is True
            for name, value in evidence.items()
            if isinstance(value, bool) and name != "packaged_process"
        )
        encoded = json.dumps(evidence, sort_keys=True)
        assert "Fixture" not in encoded
        assert str(runtime) not in encoded
        assert not (runtime / "dist" / "MusicVault" / "data").exists()
        assert not (data / "youtube_api_key.txt").exists()
        assert not (data / "discogs_token.txt").exists()
    finally:
        for timer_name in (
            "audio_device_timer",
            "volume_save_timer",
            "_browser_reflow_timer",
        ):
            timer = getattr(window, timer_name, None)
            if timer is not None:
                timer.stop()
        window.close()
        window.deleteLater()
        qapp.processEvents()
        runtime_paths._resolved_project_root.cache_clear()


def test_batch10_3_packaged_scene_is_one_bounded_explicit_capture(tmp_path: Path) -> None:
    runtime = (tmp_path / "runtime").resolve()
    output = (tmp_path / "output").resolve()
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    plan = _review_plan(runtime, output)

    assert ui_review.BATCH10_3_REVIEW_SCENES == ("batch10_3_smoke",)
    assert plan.scenes == ui_review.BATCH10_3_REVIEW_SCENES
    assert plan.capture_count == 1
    assert ui_review._review_browser_kind("batch10_3_smoke") == "artists"
