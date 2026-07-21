from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from music_vault.core import app_status
from music_vault.core.audio_quality_config import (
    BEST_ORIGINAL_PROFILE,
    INHERIT_PROFILE,
    MP3_320_COMPATIBILITY_PROFILE,
)
from music_vault.core.db import MusicVaultDB
from music_vault.core.sync_sources import SyncSourceService
from music_vault.metadata.service import MetadataService
from music_vault.ui.metadata_editor import MetadataEditorDialog
from music_vault.ui.onboarding import FirstRunWizard, sanitized_onboarding_config
from music_vault.ui.sync_center import (
    SourceEditorDialog,
    SyncCenterWidget,
    aggregate_status_transition,
    multi_source_status_payload,
)


def _source_id() -> str:
    return "PL_SYNTHETIC_BATCH11_QUALITY_0001"


def test_saved_source_quality_override_round_trips_through_editor_and_service(
    tmp_path,
    qapp,
) -> None:
    db = MusicVaultDB(tmp_path / "synthetic.sqlite3")
    try:
        service = SyncSourceService(db)
        source = service.create_source(_source_id())
        dialog = SourceEditorDialog(source=source)
        assert dialog.download_quality_profile.currentData() == INHERIT_PROFILE
        assert [
            dialog.download_quality_profile.itemText(index)
            for index in range(dialog.download_quality_profile.count())
        ] == [
            "Use Global Setting",
            "Best Original",
            "MP3 320 Compatibility",
        ]

        dialog.download_quality_profile.setCurrentIndex(
            dialog.download_quality_profile.findData(BEST_ORIGINAL_PROFILE)
        )
        values = dialog.values()
        assert values.download_quality_profile == BEST_ORIGINAL_PROFILE
        assert "avoids lossy re-encoding" in dialog.quality_description.text()
        updated = service.update_source(
            source.id,
            download_quality_profile=values.download_quality_profile,
        )
        dialog.close()

        reopened = SourceEditorDialog(source=updated)
        assert reopened.download_quality_profile.currentData() == BEST_ORIGINAL_PROFILE
        widget = SyncCenterWidget()
        widget.set_sources([updated])
        widget.set_source_detail(updated)
        assert widget.detail_quality_profile.text() == (
            "Future Download Quality: Best Original"
        )
        reopened.close()
        widget.close()
    finally:
        db.close()


def test_onboarding_offers_profiles_and_keeps_legacy_result_compatibility(
    tmp_path,
    qapp,
) -> None:
    data = tmp_path / "data"
    wizard = FirstRunWizard(
        portable_folder=tmp_path,
        data_folder=data,
        download_folder=data / "youtube_downloads",
        config={"audio_quality": "256"},
        create_shortcut_default=False,
    )
    try:
        assert wizard.download_quality_profile.currentData() == BEST_ORIGINAL_PROFILE
        assert wizard.download_quality_profile.itemText(0) == (
            "Best Original — Recommended"
        )
        assert wizard.audio_quality.isHidden()

        wizard.download_quality_profile.setCurrentIndex(
            wizard.download_quality_profile.findData(
                MP3_320_COMPATIBILITY_PROFILE
            )
        )
        result = wizard.result_values()
        config = sanitized_onboarding_config({}, result)
        assert result.download_quality_profile == MP3_320_COMPATIBILITY_PROFILE
        assert result.audio_quality == "256"
        assert config["download_quality_profile"] == (
            MP3_320_COMPATIBILITY_PROFILE
        )
        assert config["compatibility_mp3_bitrate_kbps"] == 320
        assert "cannot improve source fidelity" in wizard.quality_description.text()
    finally:
        wizard.close()


def _quality_editor_context(tmp_path: Path, *, suffix: str = ".opus"):
    db = MusicVaultDB(tmp_path / f"quality-{suffix[1:]}.sqlite3")
    media = tmp_path / f"synthetic-audio{suffix}"
    media.write_bytes(b"synthetic audio fixture")
    track_id = db.upsert_track(
        media,
        title="Synthetic Track",
        artist="Synthetic Artist",
        source_kind="youtube",
        source_video_id="abcdefghijk",
    )
    return db, track_id


def test_track_quality_tab_shows_only_known_honest_facts(tmp_path, qapp) -> None:
    db, track_id = _quality_editor_context(tmp_path)
    try:
        db.upsert_track_media_quality(
            track_id,
            acquisition_profile=BEST_ORIGINAL_PROFILE,
            source_extension="webm",
            source_container="webm",
            source_codec="opus",
            source_bitrate_kbps=160,
            source_sample_rate_hz=48000,
            source_channels=2,
            stored_extension="opus",
            stored_container="ogg",
            stored_codec="opus",
            stored_bitrate_kbps=160,
            stored_sample_rate_hz=48000,
            stored_channels=2,
            stored_filesize_bytes=2048,
            transformation_kind="source_preserved_remux",
            inspection_state="inspected",
            provenance="synthetic_batch11_test",
        )
        dialog = MetadataEditorDialog(MetadataService(db), track_id)
        labels = dialog.quality_context_labels
        assert dialog.tabs.tabText(dialog.tabs.indexOf(dialog.quality_tab)) == "Quality"
        assert labels["acquisition_profile"].text() == "Best Original"
        assert labels["source_format"].text() == ".webm"
        assert labels["source_codec"].text() == "Opus"
        assert labels["source_bitrate"].text() == "Approximately 160 kbps"
        assert labels["stored_format"].text() == ".opus"
        assert labels["stored_codec"].text() == "Opus"
        assert labels["sample_rate"].text() == "48,000 Hz"
        assert labels["channels"].text() == "2 (stereo)"
        assert labels["transformation"].text() == (
            "Source codec retained; container-only remux"
        )
        assert "source_format_id" not in labels
        assert "provenance" not in labels
        dialog.close()
    finally:
        db.close()


def test_legacy_quality_tab_does_not_render_unknown_facts_as_zero(
    tmp_path,
    qapp,
) -> None:
    db, track_id = _quality_editor_context(tmp_path, suffix=".mp3")
    try:
        dialog = MetadataEditorDialog(MetadataService(db), track_id)
        labels = dialog.quality_context_labels
        assert labels["acquisition_profile"].text() == "Legacy YouTube MP3"
        assert labels["stored_format"].text() == ".mp3"
        assert "source_codec" not in labels
        assert "source_bitrate" not in labels
        assert "stored_bitrate" not in labels
        visible_text = " ".join(label.text() for label in labels.values())
        assert "0 kbps" not in visible_text
        assert "source quality was not recorded" in labels["transformation"].text()
        dialog.close()
    finally:
        db.close()


def test_quality_sync_payload_and_transition_use_canonical_aggregate_keys() -> None:
    result = SimpleNamespace(
        finished_at="2026-07-21T12:00:00Z",
        status="complete_with_issues",
        total_new=3,
        total_imported=3,
        total_visible=4,
        total_downloaded=3,
        total_existing=1,
        total_failed_items=1,
        selected_source_count=2,
        completed_source_count=1,
        issue_source_count=1,
        failed_source_count=0,
        total_source_preserved=1,
        total_source_preserved_remux=1,
        total_mp3_compatibility_transcodes=1,
        total_quality_failures=1,
        total_stored_bytes=4096,
    )
    payload = multi_source_status_payload(
        result,
        sync_source_count=2,
        enabled_sync_source_count=2,
    )
    assert payload["last_sync_source_preserved_count"] == 1
    assert payload["last_sync_source_preserved_remux_count"] == 1
    assert payload["last_sync_mp3_compatibility_transcode_count"] == 1
    assert payload["last_sync_quality_failure_count"] == 1
    assert payload["last_sync_total_stored_bytes"] == 4096

    safe = aggregate_status_transition(
        {
            **payload,
            "last_sync_quality_failure_count": "not-an-integer",
            "source_url": "PRIVATE_SOURCE_URL",
            "source_format_id": "PRIVATE_FORMAT_ID",
        }
    )
    assert safe["last_sync_quality_failure_count"] == 0
    assert "source_url" not in safe
    assert "source_format_id" not in safe


def test_app_status_exports_only_quality_aggregates(tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    monkeypatch.setattr(app_status, "project_root", lambda: tmp_path)
    monkeypatch.setattr(app_status, "data_dir", lambda: data)
    monkeypatch.setattr(app_status, "app_status_path", lambda: data / "status.json")
    monkeypatch.setattr(app_status, "config_path", lambda: data / "config.json")
    monkeypatch.setattr(app_status, "default_downloads_dir", lambda: data / "downloads")
    monkeypatch.setattr(app_status, "youtube_api_key_path", lambda: data / "missing-key")
    monkeypatch.setattr(app_status, "discogs_token_path", lambda: data / "missing-token")
    monkeypatch.setattr(app_status, "path_resolution_source", lambda: "synthetic")
    monkeypatch.setattr(app_status, "_api_ready", lambda: False)
    monkeypatch.setattr(app_status, "_ffmpeg_ready", lambda _config=None: False)

    db = MusicVaultDB(tmp_path / "status.sqlite3")
    try:
        for index, profile in enumerate(
            (BEST_ORIGINAL_PROFILE, MP3_320_COMPATIBILITY_PROFILE),
            start=1,
        ):
            media = tmp_path / f"status-{index}.mp3"
            media.write_bytes(b"synthetic")
            track_id = db.upsert_track(media, title=f"Synthetic {index}")
            db.upsert_track_media_quality(
                track_id,
                acquisition_profile=profile,
                stored_extension="mp3",
                stored_codec="mp3",
                stored_filesize_bytes=9,
                transformation_kind=(
                    "none" if profile == BEST_ORIGINAL_PROFILE else "lossy_transcode"
                ),
                inspection_state="inspected",
                provenance="synthetic_batch11_status_test",
            )

        path = app_status.write_app_status(
            db,
            {},
            {
                "sync": {
                    "last_sync_source_preserved_count": 1,
                    "last_sync_source_preserved_remux_count": 0,
                    "last_sync_mp3_compatibility_transcode_count": 1,
                    "last_sync_quality_failure_count": 0,
                    "last_sync_total_stored_bytes": 18,
                    "source_format_id": "PRIVATE_FORMAT_ID",
                    "track_path": "PRIVATE_TRACK_PATH",
                    "source_video_id": "PRIVATE_VIDEO_ID",
                }
            },
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        library = payload["library"]
        assert library["quality_best_original_count"] == 1
        assert library["quality_mp3_compatibility_count"] == 1
        assert library["quality_legacy_youtube_mp3_count"] == 0
        assert library["quality_local_original_count"] == 0
        assert library["quality_unknown_count"] == 0
        assert payload["sync"]["last_sync_source_preserved_count"] == 1
        assert payload["sync"]["last_sync_total_stored_bytes"] == 18
        serialized = json.dumps(payload)
        assert "PRIVATE_FORMAT_ID" not in serialized
        assert "PRIVATE_TRACK_PATH" not in serialized
        assert "PRIVATE_VIDEO_ID" not in serialized

        rewritten = json.loads(
            app_status.write_app_status(db, {}).read_text(encoding="utf-8")
        )
        assert rewritten["sync"]["last_sync_source_preserved_count"] == 1
        assert rewritten["sync"]["last_sync_total_stored_bytes"] == 18
        assert "PRIVATE_FORMAT_ID" not in json.dumps(rewritten)
    finally:
        db.close()
