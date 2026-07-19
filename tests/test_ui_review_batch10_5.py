from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from music_vault.ui import review as packaged_review


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _tool():
    path = PROJECT_ROOT / "tools" / "dev" / "run_batch10_5_review.py"
    spec = importlib.util.spec_from_file_location("run_batch10_5_review", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_batch10_5_review_matrix_is_bounded_complete_and_scaled():
    tool = _tool()
    names = [scene.name for scene in tool.SCENES]
    assert len(names) == len(set(names)) == 8
    assert set(names) == {
        "canonical_artist_grid",
        "preferred_cached_portrait",
        "canonical_artist_tracks",
        "artist_featured_on",
        "artist_collaborations",
        "artist_group_appearances",
        "metadata_zero_review",
        "singles_uncatalogued_150",
    }
    assert {(scene.width, scene.height) for scene in tool.SCENES} == {
        (1280, 720),
        (1920, 1080),
    }
    assert [scene.name for scene in tool.SCENES if scene.scale == 1.5] == [
        "singles_uncatalogued_150"
    ]


def test_batch10_5_review_output_fails_closed_and_owner_marker_controls_cleanup():
    tool = _tool()
    with pytest.raises(ValueError, match="TEMP or .ui-review"):
        tool._output_directory(PROJECT_ROOT / "review-output-must-not-exist")

    requested = Path(tempfile.gettempdir()) / f"{tool.OUTPUT_PREFIX}pytest_owner"
    if requested.exists():
        tool.shutil.rmtree(requested)
    output, token = tool._output_directory(requested)
    assert tool._owned_output(output, token) == output.resolve()
    (output / tool.OWNER_MARKER).write_text(
        '{"schema_version": 1, "token": "wrong"}\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="does not match"):
        tool._owned_output(output, token)
    tool.shutil.rmtree(output)


def test_batch10_5_offscreen_review_uses_production_surfaces_and_cleans_temp():
    tool = _tool()
    temp = Path(tempfile.gettempdir())
    output = temp / f"{tool.OUTPUT_PREFIX}pytest_production_surfaces"
    if output.exists():
        tool.shutil.rmtree(output)
    before_runtimes = set(temp.glob(f"{tool.RUNTIME_PREFIX}*"))

    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(PROJECT_ROOT / "tools" / "dev" / "run_batch10_5_review.py"),
            "--offscreen",
            "--output",
            str(output),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["capture_count"] == 8
    assert payload["review_queue_count"] == 0
    assert payload["backwards_orientation_repaired"] is True
    assert payload["preferred_cached_portrait_preserved"] is True
    assert payload["cover_paths_unchanged"] is True
    assert payload["network_attempt_count"] == 0
    assert payload["credential_read_count"] == 0
    assert payload["provider_request_count"] == 0
    assert {capture["production_surface"] for capture in payload["captures"]} == {
        "MusicVaultWindow",
        "MetadataIntelligenceDialog",
    }
    assert not output.exists()
    assert set(temp.glob(f"{tool.RUNTIME_PREFIX}*")) == before_runtimes


def test_batch10_5_packaged_hook_is_inert_without_explicit_review(monkeypatch):
    monkeypatch.delenv(packaged_review.REVIEW_ENV, raising=False)
    assert packaged_review.schedule_ui_review(object(), object()) is False
    assert packaged_review.BATCH10_5_REVIEW_SCENES == ("batch10_5_smoke",)
    assert packaged_review._review_browser_kind("batch10_5_smoke") == "artists"


def test_batch10_5_review_contract_blocks_network_secrets_and_retains_no_capture():
    tool_text = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_5_review.py"
    ).read_text(encoding="utf-8")
    wrapper_text = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_5_review.ps1"
    ).read_text(encoding="utf-8")
    assert '"network_attempt_count": 0' in tool_text
    assert '"credential_read_count": 0' in tool_text
    assert '"provider_request_count": 0' in tool_text
    assert "if not args.keep_captures" in tool_text
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper_text
    assert "--offscreen" in wrapper_text
