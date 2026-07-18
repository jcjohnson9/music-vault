from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _tool():
    path = PROJECT_ROOT / "tools" / "dev" / "run_batch10_3_review.py"
    spec = importlib.util.spec_from_file_location("run_batch10_3_review", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_review_matrix_is_bounded_complete_and_scaled():
    tool = _tool()
    names = [scene.name for scene in tool.SCENES]
    assert len(names) == len(set(names)) == 10
    assert {
        "album_grouping",
        "canonical_album_editions",
        "canonical_artist_tracks",
        "artist_featured_on",
        "artist_collaborations",
        "artist_group_appearances",
        "review_outcomes",
        "soundtrack_state",
        "malformed_artist_repair",
        "missing_portrait_150",
    } == set(names)
    assert {(scene.width, scene.height) for scene in tool.SCENES} == {
        (1280, 720),
        (1920, 1080),
    }
    assert [scene.name for scene in tool.SCENES if scene.scale == 1.5] == [
        "missing_portrait_150"
    ]


def test_review_output_fails_closed_outside_temp_or_ignored_review_root():
    tool = _tool()
    with pytest.raises(ValueError, match="TEMP or .ui-review"):
        tool._output_directory(PROJECT_ROOT / "review-output-must-not-exist")


def test_review_owner_marker_controls_cleanup():
    tool = _tool()
    requested = Path(tool.tempfile.gettempdir()) / f"{tool.OUTPUT_PREFIX}pytest_owner"
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


def test_review_uses_production_surfaces_instead_of_bespoke_mock_builders():
    tool_text = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_3_review.py"
    ).read_text(encoding="utf-8")
    assert "from music_vault.app import MusicVaultWindow" in tool_text
    assert "from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog" in tool_text
    assert "window.show_album_browser()" in tool_text
    assert "window.show_artist_browser()" in tool_text
    assert "_BUILDERS" not in tool_text
    assert "def _shell(" not in tool_text


def test_offscreen_review_renders_production_window_and_dialog_then_cleans_temp():
    tool = _tool()
    temp = Path(tempfile.gettempdir())
    output = temp / f"{tool.OUTPUT_PREFIX}pytest_production_surfaces"
    if output.exists():
        tool.shutil.rmtree(output)
    before_runtimes = set(temp.glob(f"{tool.RUNTIME_PREFIX}*"))

    completed = subprocess.run(
        [
            str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
            "-B",
            str(PROJECT_ROOT / "tools" / "dev" / "run_batch10_3_review.py"),
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
    assert payload["capture_count"] == 10
    assert payload["production_window_used"] is True
    assert payload["production_metadata_dialog_used"] is True
    assert payload["temporary_synthetic_database"] is True
    assert payload["network_attempt_count"] == 0
    assert payload["credential_read_count"] == 0
    assert payload["runtime_database_read_count"] == 0
    assert payload["provider_request_count"] == 0
    assert {capture["production_surface"] for capture in payload["captures"]} == {
        "MusicVaultWindow",
        "MetadataIntelligenceDialog",
    }
    assert all(capture["semantic_check_count"] == 1 for capture in payload["captures"])
    assert not output.exists()
    assert set(temp.glob(f"{tool.RUNTIME_PREFIX}*")) == before_runtimes


def test_review_contract_is_network_and_secret_free():
    tool_text = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_3_review.py"
    ).read_text(encoding="utf-8")
    wrapper_text = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_3_review.ps1"
    ).read_text(encoding="utf-8")
    assert '"network_attempt_count": 0' in tool_text
    assert '"credential_read_count": 0' in tool_text
    assert '"runtime_database_read_count": 0' in tool_text
    assert '"provider_request_count": 0' in tool_text
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper_text
    assert "--offscreen" in wrapper_text
