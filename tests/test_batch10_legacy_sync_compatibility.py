from __future__ import annotations

from types import SimpleNamespace

import pytest

from music_vault.app import MusicVaultWindow
from music_vault.core.db import MusicVaultDB
from music_vault.core.sync_sources import SyncSourceService


def _legacy_migration_harness(db: MusicVaultDB, config: dict[str, object]):
    return SimpleNamespace(
        config=config,
        sync_source_service=SyncSourceService(db),
    )


def _run_legacy_migration(harness) -> None:
    MusicVaultWindow._migrate_legacy_sync_source_from_config(harness)


def _table_count(db: MusicVaultDB, table: str) -> int:
    return int(db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.parametrize(
    "config_key",
    (
        "youtube_playlist_url",
        "youtube_sync_playlist_url",
        "playlist_url",
    ),
)
def test_valid_persisted_legacy_url_creates_exactly_one_enabled_library_source(
    tmp_path, config_key
):
    db = MusicVaultDB(tmp_path / "legacy-valid.sqlite3")
    try:
        external_id = "PLlegacyValid01"
        harness = _legacy_migration_harness(
            db,
            {
                config_key: (
                    "https://www.youtube.com/playlist?list="
                    f"{external_id}&feature=share"
                )
            },
        )

        _run_legacy_migration(harness)
        _run_legacy_migration(harness)

        sources = harness.sync_source_service.list_active()
        assert len(sources) == 1
        assert sources[0].external_id == external_id
        assert sources[0].enabled is True
        assert sources[0].destination_kind == "library"
        assert sources[0].destination_playlist_id is None
        assert _table_count(db, "sync_source_items") == 0
        assert _table_count(db, "sync_source_runs") == 0
    finally:
        db.close()


def test_missing_legacy_url_creates_no_source(tmp_path):
    db = MusicVaultDB(tmp_path / "legacy-missing.sqlite3")
    try:
        harness = _legacy_migration_harness(
            db,
            {
                "download_folder": str(tmp_path / "downloads"),
                "audio_quality": "320",
            },
        )

        _run_legacy_migration(harness)

        assert _table_count(db, "sync_sources") == 0
        assert _table_count(db, "sync_source_items") == 0
        assert _table_count(db, "sync_source_runs") == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    "invalid_value",
    (
        "https://example.test/playlist?list=PLlegacyInvalid",
        "https://www.youtube.com/watch?v=abcdefghijk",
        r"C:\private\playlist",
    ),
)
def test_invalid_legacy_url_creates_no_source(tmp_path, invalid_value):
    db = MusicVaultDB(tmp_path / "legacy-invalid.sqlite3")
    try:
        harness = _legacy_migration_harness(
            db, {"youtube_playlist_url": invalid_value}
        )

        _run_legacy_migration(harness)

        assert _table_count(db, "sync_sources") == 0
        assert _table_count(db, "sync_source_items") == 0
        assert _table_count(db, "sync_source_runs") == 0
    finally:
        db.close()


def test_existing_youtube_tracks_do_not_invent_source_membership(tmp_path):
    db = MusicVaultDB(tmp_path / "legacy-existing-track.sqlite3")
    try:
        media = tmp_path / "synthetic-downloaded-track.media"
        media.write_bytes(b"synthetic")
        track_id = db.upsert_track(
            media,
            source_kind="youtube",
            source_video_id="abcdefghijk",
        )
        harness = _legacy_migration_harness(db, {})

        _run_legacy_migration(harness)

        assert db.canonical_track_id("youtube", "abcdefghijk") == track_id
        assert _table_count(db, "sync_sources") == 0
        assert _table_count(db, "sync_source_items") == 0
        assert _table_count(db, "sync_source_runs") == 0
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM playlist_track_origins "
                "WHERE origin_kind='sync_source'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()


def test_legacy_failures_attach_only_to_exact_matching_playlist_id(tmp_path):
    db = MusicVaultDB(tmp_path / "legacy-failures.sqlite3")
    try:
        service = SyncSourceService(db)
        other_source = service.create_source("PLlegacyOther01")
        target_external_id = "PLlegacyTarget1"
        attempted_at = "2026-01-01T00:00:00Z"

        for playlist_id, video_id, source_id in (
            (target_external_id, "abcdefghijk", None),
            ("PLlegacyDifferent", "bcdefghijkl", None),
            ("unassigned", "cdefghijklm", None),
            (target_external_id, "defghijklmn", other_source.id),
        ):
            db.record_sync_failure(
                playlist_id=playlist_id,
                playlist_title=None,
                video_id=video_id,
                title=None,
                reason="Synthetic failure",
                error_category="download",
                attempted_at=attempted_at,
                sync_source_id=source_id,
            )

        target = service.create_source(target_external_id)
        assignments = {
            str(row["video_id"]): row["sync_source_id"]
            for row in db.conn.execute(
                "SELECT video_id, sync_source_id FROM sync_failures"
            )
        }

        assert assignments == {
            "abcdefghijk": target.id,
            "bcdefghijkl": None,
            "cdefghijklm": None,
            "defghijklmn": other_source.id,
        }
    finally:
        db.close()
