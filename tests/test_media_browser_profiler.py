from __future__ import annotations

import json
from pathlib import Path

from tools.dev import profile_media_browsers


def test_profiler_uses_required_scale_shapes_and_synthetic_small_run(qapp):
    assert profile_media_browsers._shape_for(300) == (100, 200)
    assert profile_media_browsers._shape_for(1000) == (300, 600)
    assert profile_media_browsers._shape_for(5000) == (1000, 2000)

    payload = profile_media_browsers.run_profile((30,))
    dataset = payload["datasets"][0]
    assert payload["synthetic_only"] is True
    assert payload["network_used"] is False
    assert dataset["tracks"] == 30
    assert dataset["actual_albums"] == 10
    assert dataset["actual_artists"] == 20
    assert dataset["albums"]["card_widget_count"] == 0
    assert dataset["artists"]["card_widget_count"] == 0
    assert dataset["albums"]["thumbnails"]["offscreen_sources_requested"] == 0
    assert dataset["albums"]["thumbnails"]["stats"]["decodes"] <= 10


def test_profiler_json_is_written_only_when_explicitly_requested(tmp_path: Path):
    destination = tmp_path / "profile.json"
    payload = {
        "synthetic_only": True,
        "network_used": False,
        "datasets": [],
    }
    profile_media_browsers._write_json(destination, payload)
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert not destination.with_suffix(".json.tmp").exists()
