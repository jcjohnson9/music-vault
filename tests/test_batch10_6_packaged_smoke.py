from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION
from music_vault.core import paths as runtime_paths
from music_vault.ui import review as ui_review


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _tool():
    path = PROJECT_ROOT / "tools" / "dev" / "run_batch10_6_packaged_smoke.py"
    spec = importlib.util.spec_from_file_location("run_batch10_6_packaged_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime(tool) -> Path:
    return Path(tempfile.gettempdir()) / f"{tool.RUNTIME_PREFIX}{uuid.uuid4().hex[:8]}"


def _synthetic_project(tmp_path: Path) -> Path:
    project = tmp_path / "synthetic-project"
    executable = project / "dist" / "MusicVault" / "MusicVault.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable marker")
    return project


def test_prepare_seeds_one_queued_dual_orientation_target(tmp_path: Path):
    tool = _tool()
    runtime = _runtime(tool)
    try:
        manifest = tool.prepare(runtime, _synthetic_project(tmp_path))
        assert (
            manifest["database"]["counts"]["schema_version"]
            == CURRENT_SCHEMA_VERSION
        )
        assert manifest["seed"] == {
            "orientation_target_count": 1,
            "automatic_queued_count": 1,
        }
        assert manifest["execution_policy"]["synthetic_injected_providers"] is True
        plan = json.loads((runtime / tool.REVIEW_PLAN_NAME).read_text(encoding="utf-8"))
        assert plan["scenes"] == ["batch10_6_smoke"]
        connection = tool.sqlite3.connect(runtime / "data" / "music_vault.sqlite3")
        try:
            queued, targets = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='queued'),"
                "(SELECT COUNT(*) FROM tracks WHERE source_video_id='b106smoke01')"
            ).fetchone()
        finally:
            connection.close()
        assert (queued, targets) == (1, 1)
        assert not (runtime / "data" / "youtube_api_key.txt").exists()
        assert not (runtime / "data" / "discogs_token.txt").exists()
    finally:
        if runtime.exists():
            shutil.rmtree(runtime)


def test_review_manifest_requires_every_batch10_6_behavior(tmp_path: Path):
    tool = _tool()
    output = tmp_path / "review"
    output.mkdir()
    screenshot = output / "1280x720_batch10_6_smoke.png"
    screenshot.write_bytes(b"synthetic png evidence")
    behaviors = {name: True for name in tool.REQUIRED_BEHAVIOR_FIELDS}
    behaviors.update(
        {
            "packaged_process": True,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "processed_count": 1,
            "discogs_query_count": 2,
            "musicbrainz_query_count": 1,
            "network_attempt_count": 0,
        }
    )

    def write() -> Path:
        path = output / "manifest.json"
        path.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "runtime": "isolated_temporary",
                    "capture_count": 1,
                    "captures": [
                        {
                            "scene": "batch10_6_smoke",
                            "file": screenshot.name,
                            "sha256": tool.base._sha256(screenshot),
                        }
                    ],
                    "runtime_checks": {"batch10_6_behaviors": behaviors},
                }
            ),
            encoding="utf-8",
        )
        return path

    manifest = write()
    assert tool._review_evidence(manifest)["verified"] is True
    behaviors["raw_source_preserved"] = False
    write()
    with pytest.raises(tool.SmokeFailure, match="evidence_invalid"):
        tool._review_evidence(manifest)


def test_wrapper_is_official_exe_owned_temp_and_never_provider_network():
    wrapper = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_6_packaged_smoke.ps1"
    ).read_text(encoding="utf-8")
    assert "dist\\MusicVault\\MusicVault.exe" in wrapper
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper
    assert "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK" in wrapper
    assert "Get-NetTCPConnection" in wrapper
    assert "CloseMainWindow" in wrapper
    assert "MusicVault_Batch10_6_PackagedSmoke_" in wrapper
    assert "Remove-Item -LiteralPath $ResolvedRuntime" in wrapper


def test_batch10_6_hook_runs_real_orchestration_with_injected_providers(
    tmp_path: Path,
    monkeypatch,
    qapp,
):
    tool = _tool()
    runtime = _runtime(tool)
    output = runtime.with_name(runtime.name + tool.REVIEW_OUTPUT_SUFFIX)
    try:
        tool.prepare(runtime, _synthetic_project(tmp_path))
        request = runtime / tool.REVIEW_PLAN_NAME
        monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
        monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
        monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")
        monkeypatch.setenv("MUSIC_VAULT_DISABLE_NETWORK", "1")
        monkeypatch.setenv(
            "MUSIC_VAULT_ACCEPTANCE_NETWORK_REPORT",
            str(runtime / "batch10_6-network-report.json"),
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
            evidence = ui_review.validate_batch10_6_review_behaviors(window, plan)
            assert evidence["exactly_one_target_processed"] is True
            assert evidence["discogs_query_count"] == 2
            assert evidence["musicbrainz_query_count"] <= 1
            assert evidence["reverse_orientation_selected"] is True
            assert evidence["raw_source_preserved"] is True
            assert evidence["ordinary_review_zero"] is True
            assert evidence["network_attempt_count"] == 0
        finally:
            for timer_name in ("audio_device_timer", "volume_save_timer", "_browser_reflow_timer"):
                timer = getattr(window, timer_name, None)
                if timer is not None:
                    timer.stop()
            window.close()
            window.db.close()
            window.deleteLater()
            qapp.processEvents()
            runtime_paths._resolved_project_root.cache_clear()
    finally:
        if runtime.exists():
            shutil.rmtree(runtime)
        if output.exists():
            shutil.rmtree(output)
