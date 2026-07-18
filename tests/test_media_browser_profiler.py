from __future__ import annotations

import json
from pathlib import Path

from tools.dev import profile_media_browsers


def test_profiler_uses_required_scale_shapes_and_synthetic_small_run(qapp):
    assert profile_media_browsers.DEFAULT_TRACK_COUNTS == (300, 1000, 5000)
    assert profile_media_browsers._shape_for(300) == (100, 200)
    assert profile_media_browsers._shape_for(1000) == (300, 600)
    assert profile_media_browsers._shape_for(5000) == (1000, 2000)

    payload = profile_media_browsers.run_profile((30,))
    dataset = payload["datasets"][0]
    assert payload["synthetic_only"] is True
    assert payload["network_used"] is False
    assert payload["network_attempt_count"] == 0
    assert payload["credential_read_count"] == 0
    assert dataset["tracks"] == 30
    assert dataset["schema_version"] == 7
    assert dataset["integrity"] == "ok"
    assert dataset["actual_albums"] == 10
    assert dataset["actual_artists"] == 20
    assert dataset["canonical"]["canonical_albums"] == 10
    assert dataset["canonical"]["album_memberships"] == 30
    assert dataset["canonical"]["canonical_artists"] == 20
    assert dataset["canonical"]["artist_aliases"] > 0
    assert dataset["canonical"]["artist_relationships"] > 0
    assert dataset["canonical"]["edition_memberships"] > 0
    assert dataset["canonical"]["required_indexes_present"] is True
    assert dataset["canonical"]["album_membership_query_uses_index"] is True
    assert dataset["review_reclassification"]["scanned"] == 30
    assert dataset["review_reclassification"]["remaining"] == 0
    assert dataset["review_reclassification"]["batch_size"] == 250
    assert (
        dataset["review_reclassification"]["applied"]
        + dataset["review_reclassification"]["applied_with_gaps"]
        + dataset["review_reclassification"]["source_fallback"]
        + dataset["review_reclassification"]["needs_review"]
    ) == 30
    assert dataset["albums"]["card_widget_count"] == 0
    assert dataset["artists"]["card_widget_count"] == 0
    assert dataset["albums"]["summary_sql_statement_count"] <= 4
    assert dataset["artists"]["summary_sql_statement_count"] <= 3
    assert dataset["albums"]["track_query_sql_statement_count"] <= 2
    assert dataset["artists"]["section_query_sql_statement_count"] <= 3
    assert dataset["albums"]["thumbnails"]["offscreen_sources_requested"] == 0
    assert dataset["albums"]["thumbnails"]["stats"]["decodes"] <= 10


def test_profiler_wrapper_forces_offscreen_no_secret_mode():
    wrapper = (
        Path(profile_media_browsers.__file__).with_suffix(".ps1")
    ).read_text(encoding="utf-8")
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper
    assert "QT_QPA_PLATFORM" in wrapper
    assert ".venv\\Scripts\\python.exe" in wrapper


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
