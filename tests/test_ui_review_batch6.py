from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from music_vault.ui.review import (
    METADATA_REVIEW_SCENES,
    SCENE_LABELS,
    REVIEW_SCHEMA_VERSION,
    load_review_plan,
    metadata_review_metrics,
)


def test_batch6_metadata_review_matrix_is_complete_and_unique():
    assert len(METADATA_REVIEW_SCENES) == 17
    assert len(set(METADATA_REVIEW_SCENES)) == 17
    assert all(scene in SCENE_LABELS for scene in METADATA_REVIEW_SCENES)


def test_review_plan_accepts_every_metadata_scene(tmp_path):
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
                "sizes": [{"width": 1100, "height": 720}],
                "scenes": list(METADATA_REVIEW_SCENES),
                "settle_ms": 100,
                "expected_capture_count": len(METADATA_REVIEW_SCENES),
            }
        ),
        encoding="utf-8",
    )
    plan = load_review_plan(request)
    assert plan.capture_count == 17


def test_metadata_metrics_are_path_free_and_prove_fake_only_state():
    # The production metric intentionally requires the internal fake type. Use
    # a real lightweight instance without invoking any provider method.
    from music_vault.ui import review

    provider = review._SyntheticMetadataProvider()
    cover_provider = review._SyntheticCoverProvider()
    dialog = SimpleNamespace(
        field_editors={name: object() for name in ("title", "artist", "album", "album_artist", "release_date")},
        candidates=[],
        history_table=SimpleNamespace(rowCount=lambda: 2),
        file_writeback_note=SimpleNamespace(
            text=lambda: "Changes stay in Music Vault; audio files are unchanged."
        ),
        _review_metadata_provider=provider,
        _review_cover_provider=cover_provider,
    )
    window = SimpleNamespace(_review_metadata_dialog=dialog)
    metrics = metadata_review_metrics(window, "metadata_editor")
    assert metrics == {
        "state": "metadata_editor",
        "editable_field_count": 6,
        "candidate_count": 0,
        "history_group_count": 2,
        "source_upload_date_is_read_only": True,
        "database_only_message_present": True,
        "synthetic_provider_active": True,
        "synthetic_provider_call_count": 0,
        "public_provider_call_count": 0,
        "manual_artwork_staged": False,
        "artwork_effective_present": False,
        "artwork_editor_visible": False,
        "undo_confirmation_visible": False,
    }
