from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from music_vault.core import paths as runtime_paths
from music_vault.ui import review as ui_review


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _tool():
    path = PROJECT_ROOT / "tools" / "dev" / "run_batch10_5_packaged_smoke.py"
    spec = importlib.util.spec_from_file_location("run_batch10_5_packaged_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_packaged_smoke_prepare_is_schema7_synthetic_and_secret_free(tmp_path: Path):
    tool = _tool()
    project = tmp_path / "synthetic-project"
    executable = project / "dist" / "MusicVault" / "MusicVault.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable marker")
    # Keep this representative of the actual wrapper's short TEMP-root path;
    # pytest's nested Windows path can exceed legacy Qt/filesystem limits.
    runtime = Path(tempfile.gettempdir()) / f"{tool.RUNTIME_PREFIX}{uuid.uuid4().hex[:8]}"
    try:
        manifest = tool.prepare(runtime, project)

        assert manifest["manifest_format_version"] == tool.MANIFEST_FORMAT_VERSION
        assert manifest["database"]["counts"]["schema_version"] == 7
        assert manifest["database"]["counts"]["review_count"] == 0
        assert manifest["seed"]["orientation_repair_count"] == 1
        assert manifest["seed"]["remaining_review_count"] == 0
        assert manifest["seed"]["applied_count"] >= 1
        assert manifest["seed"]["applied_with_gaps_count"] >= 1
        assert manifest["seed"]["source_fallback_count"] >= 1
        assert manifest["artist_images"]
        connection = tool.sqlite3.connect(runtime / "data" / "music_vault.sqlite3")
        try:
            marker = connection.execute(
                "SELECT value FROM app_meta "
                "WHERE key='batch10_5_metadata_acceptance_repair_v1'"
            ).fetchone()
            legacy_marker = connection.execute(
                "SELECT value FROM app_meta WHERE key=?",
                (tool.LEGACY_FAILURE_IMPORT_MARKER,),
            ).fetchone()
        finally:
            connection.close()
        assert marker == ("synthetic_acceptance_complete",)
        assert legacy_marker == ("synthetic_no_legacy_failures",)
        assert not (runtime / "data" / "youtube_api_key.txt").exists()
        assert not (runtime / "data" / "discogs_token.txt").exists()
        plan = json.loads((runtime / tool.REVIEW_PLAN_NAME).read_text(encoding="utf-8"))
        assert plan["scenes"] == ["batch10_5_smoke"]
        assert plan["expected_capture_count"] == 1
    finally:
        if runtime.exists():
            shutil.rmtree(runtime)


def test_packaged_smoke_rejects_workspace_or_unprefixed_runtime():
    tool = _tool()
    with pytest.raises(tool.SmokeFailure, match="unsafe_temporary_runtime"):
        tool._safe_runtime(PROJECT_ROOT / "data")
    with pytest.raises(tool.SmokeFailure, match="unsafe_temporary_runtime"):
        tool._safe_runtime(Path(tool.tempfile.gettempdir()) / "unprefixed")


def test_packaged_review_manifest_requires_every_batch10_5_behavior(tmp_path: Path):
    tool = _tool()
    output = tmp_path / "review"
    output.mkdir()
    screenshot = output / "1280x720_batch10_5_smoke.png"
    screenshot.write_bytes(b"synthetic png evidence")
    behaviors = {name: True for name in tool.REQUIRED_BEHAVIOR_FIELDS}
    behaviors.update(
        {
            "packaged_process": True,
            "schema_version": 7,
            "network_attempt_count": 0,
            "artist_card_count": 4,
            "review_outcome_counts": {"applied": 1, "review": 0},
        }
    )
    manifest = output / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "runtime": "isolated_temporary",
                "requested_capture_count": 1,
                "capture_count": 1,
                "captures": [
                    {
                        "scene": "batch10_5_smoke",
                        "file": screenshot.name,
                        "sha256": tool._sha256(screenshot),
                    }
                ],
                "runtime_checks": {"batch10_5_behaviors": behaviors},
            }
        ),
        encoding="utf-8",
    )

    assert tool._review_evidence(manifest)["verified"] is True
    behaviors["global_spacebar_guarded"] = False
    manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "runtime": "isolated_temporary",
                "requested_capture_count": 1,
                "capture_count": 1,
                "captures": [
                    {
                        "scene": "batch10_5_smoke",
                        "file": screenshot.name,
                        "sha256": tool._sha256(screenshot),
                    }
                ],
                "runtime_checks": {"batch10_5_behaviors": behaviors},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(tool.SmokeFailure, match="evidence_invalid"):
        tool._review_evidence(manifest)


def test_packaged_wrapper_uses_official_exe_and_owned_temp_cleanup():
    wrapper = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_5_packaged_smoke.ps1"
    ).read_text(encoding="utf-8")
    assert "dist\\MusicVault\\MusicVault.exe" in wrapper
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper
    assert "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK" in wrapper
    assert "Get-NetTCPConnection" in wrapper
    assert "CloseMainWindow" in wrapper
    assert "MusicVault_Batch10_5_PackagedSmoke_" in wrapper
    assert "Remove-Item -LiteralPath $ResolvedRuntime" in wrapper


def test_packaged_batch10_5_hook_exercises_real_window_offline(
    monkeypatch,
    qapp,
):
    from tools.dev import run_batch10_5_review as review_tool

    runtime = review_tool._runtime_directory()
    output = runtime.root.with_name(runtime.root.name + "_Review")
    try:
        with review_tool._review_environment(runtime.root):
            review_tool._seed_batch10_5(runtime)
            request = runtime.root / "batch10_5-ui-review-plan.json"
            request.write_text(
                json.dumps(
                    {
                        "schema_version": ui_review.REVIEW_SCHEMA_VERSION,
                        "runtime_root": str(runtime.root),
                        "output_dir": str(output),
                        "sizes": [{"width": 1280, "height": 720}],
                        "scenes": ["batch10_5_smoke"],
                        "settle_ms": 100,
                        "expected_capture_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            plan = ui_review.load_review_plan(request)
            monkeypatch.setattr(runtime_paths, "_configured_data_directory", None)
            runtime_paths._resolved_project_root.cache_clear()
            monkeypatch.setattr(ui_review, "_REVIEW_NETWORK_GUARD_INSTALLED", True)
            monkeypatch.setattr(ui_review, "_REVIEW_NETWORK_EVENTS", [])

            from music_vault import app as app_module

            monkeypatch.setattr(
                app_module.MusicVaultWindow,
                "use_system_default_audio_output",
                lambda self: None,
            )
            monkeypatch.setattr(app_module, "apply_dark_title_bar", lambda _window: False)
            window = app_module.MusicVaultWindow()
            window.show()
            qapp.processEvents()
            try:
                assert ui_review.validate_review_runtime(plan)["schema_version"] == 7
                evidence = ui_review.validate_batch10_5_review_behaviors(window, plan)
                assert evidence["packaged_process"] is False
                assert evidence["ordinary_review_eliminated"] is True
                assert evidence["preferred_cached_portrait"] is True
                assert evidence["canonical_artist_sections_complete"] is True
                assert evidence["global_spacebar_guarded"] is True
                assert evidence["network_attempt_count"] == 0
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
                window.db.close()
                window.deleteLater()
                qapp.processEvents()
                runtime_paths._resolved_project_root.cache_clear()
    finally:
        if runtime.root.exists():
            review_tool.shutil.rmtree(review_tool._owned_runtime(runtime))
        if output.exists():
            review_tool.shutil.rmtree(output)
