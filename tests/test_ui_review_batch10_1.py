from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "dev" / "run_batch10_1_review.py"


def _tool():
    spec = importlib.util.spec_from_file_location("batch10_1_review_tool", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_review_matrix_is_bounded_complete_and_unique():
    tool = _tool()
    names = [scene.name for scene in tool.SCENES]
    assert names == [
        "discogs_settings",
        "metadata_consent",
        "job_summary",
        "provider_agreement",
        "provider_disagreement",
        "structured_credits",
        "unofficial_live",
        "youtube_exclusive",
        "missing_art",
        "artist_featured_on",
    ]
    assert len(names) == len(set(names)) == 10
    assert all(1100 <= scene.width <= 1920 for scene in tool.SCENES)
    assert all(700 <= scene.height <= 1080 for scene in tool.SCENES)


def test_review_output_fails_closed_outside_temp_or_ignored_review_root():
    tool = _tool()
    with pytest.raises(ValueError, match="TEMP or .ui-review"):
        tool._output_directory(PROJECT_ROOT / "unsafe-review-output")


def test_review_owner_marker_controls_cleanup(tmp_path):
    tool = _tool()
    requested = Path(tool.tempfile.gettempdir()) / f"{tool.OUTPUT_PREFIX}pytest_owner"
    if requested.exists():
        shutil.rmtree(requested)
    output, token = tool._output_directory(requested)
    try:
        assert tool._owned_output(output, token) == output.resolve()
        with pytest.raises(RuntimeError, match="does not match"):
            tool._owned_output(output, "wrong-token")
    finally:
        shutil.rmtree(output)


def test_review_and_documentation_preserve_discogs_notice_and_offline_contract():
    exact_notice = (
        "This application uses Discogs’ API but is not affiliated with, sponsored or\n"
        "endorsed by Discogs. “Discogs” is a trademark of Zink Media, LLC."
    )
    notice_compact = exact_notice.replace("\n", " ")
    tool_text = TOOL_PATH.read_text(encoding="utf-8")
    discogs_doc = (PROJECT_ROOT / "docs" / "DISCOGS_METADATA.md").read_text(
        encoding="utf-8"
    )
    notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert _tool().DISCOGS_NOTICE == notice_compact
    assert exact_notice in discogs_doc
    assert exact_notice in notices
    assert "music_vault.metadata.providers" not in tool_text
    assert 'sqlite3.connect(":memory:")' in tool_text
    assert '"discogs_token_read": False' in tool_text
    assert '"network_attempt_count": 0' in tool_text


def test_general_capture_reports_the_current_synthetic_schema():
    text = (PROJECT_ROOT / "tools" / "dev" / "capture_ui_review.py").read_text(
        encoding="utf-8"
    )
    assert "from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB" in text
    assert 'if schema != CURRENT_SCHEMA_VERSION or integrity != "ok":' in text
    assert '"schema_version": dataset["schema_version"]' in text
    assert '"schema_version": 5' not in text


def test_review_job_rows_use_normalized_dashboard_summaries():
    db = _tool()._synthetic_job_db()
    rows = db.conn.execute(
        "SELECT field_proposal,provider_agreement FROM metadata_intelligence_items "
        "ORDER BY id"
    ).fetchall()
    try:
        assert {str(row["provider_agreement"]) for row in rows} == {
            "agreed",
            "conflict",
            "partial",
            "no_match",
        }
        proposals = [json.loads(str(row["field_proposal"])) for row in rows]
        assert all(
            {"_current", "_discogs", "_musicbrainz", "_artwork"} <= set(proposal)
            for proposal in proposals
        )
        assert not any("raw_response" in proposal for proposal in proposals)
    finally:
        db.conn.close()
