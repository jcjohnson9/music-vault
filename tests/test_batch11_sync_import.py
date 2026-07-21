from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

from music_vault.core.audio_inspection import AudioInspection
from music_vault.core.audio_quality_config import (
    BEST_ORIGINAL_PROFILE,
    MP3_320_COMPATIBILITY_PROFILE,
)
from music_vault.core.db import MusicVaultDB
from music_vault.core.importer import ImportSourceContext, import_file
from music_vault.core.multi_source_sync import MultiSourceSyncOrchestrator
from music_vault.core.sync_result import (
    PlaylistSnapshot,
    PlaylistSnapshotItem,
    SyncFailure,
    SyncImportItem,
    SyncResult,
    utc_now,
)
from music_vault.core.sync_sources import SyncSourceService


VIDEO_ID = "batch11test"


def _quality_facts(*, stored_bytes: int) -> dict[str, object]:
    return {
        "acquisition_profile": BEST_ORIGINAL_PROFILE,
        "source_format_id": "synthetic-opus",
        "source_extension": ".webm",
        "source_container": "webm",
        "source_codec": "opus",
        "source_bitrate_kbps": 160,
        "source_sample_rate_hz": 48_000,
        "source_channels": 2,
        "source_filesize_bytes": stored_bytes,
        "stored_extension": ".opus",
        "stored_container": "ogg",
        "stored_codec": "opus",
        "stored_bitrate_kbps": 160,
        "stored_sample_rate_hz": 48_000,
        "stored_channels": 2,
        "stored_filesize_bytes": stored_bytes,
        "transformation_kind": "source_preserved_remux",
        "inspection_state": "inspected",
        "provenance": "synthetic_yt_dlp_and_ffprobe",
        "inspected_at": utc_now(),
    }


class _QualitySyncer:
    def __init__(self, config, _report, calls: list[object]) -> None:
        self.config = config
        self.calls = calls

    def sync(self) -> SyncResult:
        self.calls.append(self.config)
        source_item_id = f"source-{self.config.saved_source_id}-item"
        snapshot = PlaylistSnapshot.completed(
            self.config.playlist_url.rsplit("=", 1)[-1],
            "Synthetic quality source",
            (
                PlaylistSnapshotItem(
                    source_item_id,
                    VIDEO_ID,
                    0,
                    "Synthetic item",
                ),
            ),
        )
        result = SyncResult(
            "complete",
            snapshot.playlist_id,
            snapshot.playlist_title,
            visible_item_count=1,
            saved_source_id=self.config.saved_source_id,
            snapshot=snapshot,
        )
        if VIDEO_ID in set(self.config.existing_video_ids):
            result.existing_count = 1
            result.successful_video_ids.add(VIDEO_ID)
            return result
        local_path = self.config.shared_download_index.get(VIDEO_ID)
        if local_path is not None:
            result.existing_count = 1
            result.import_items.append(
                SyncImportItem(
                    str(local_path),
                    VIDEO_ID,
                    source_item_ids=(source_item_id,),
                )
            )
            result.successful_video_ids.add(VIDEO_ID)
            return result

        target = self.config.source_destination_dir / f"Track [{VIDEO_ID}].opus"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic opus")
        facts = _quality_facts(stored_bytes=target.stat().st_size)
        result.new_item_count = 1
        result.downloaded_count = 1
        result.downloaded_paths.append(str(target))
        result.import_items.append(
            SyncImportItem(
                str(target),
                VIDEO_ID,
                source_item_ids=(source_item_id,),
                quality_facts=facts,
            )
        )
        result.successful_video_ids.add(VIDEO_ID)
        return result


def _synthetic_import(db: MusicVaultDB, item: SyncImportItem) -> int:
    return db.upsert_track(
        item.path,
        source_kind="youtube",
        source_video_id=item.video_id,
    )


def test_source_override_reuses_first_canonical_file_and_persists_quality(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3")
    sources = SyncSourceService(db)
    inherited = sources.create_source("PLbatch11AA", label="Inherited")
    compatibility = sources.create_source(
        "PLbatch11BB",
        label="Compatibility",
        download_quality_profile=MP3_320_COMPATIBILITY_PROFILE,
    )
    calls: list[object] = []
    transitions: list[dict] = []
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        download_quality_profile=BEST_ORIGINAL_PROFILE,
        compatibility_mp3_bitrate_kbps=320,
        source_service=sources,
        syncer_factory=lambda config, report: _QualitySyncer(config, report, calls),
        importer=_synthetic_import,
        transition_callback=transitions.append,
    )

    aggregate = orchestrator.sync_all_enabled()

    assert aggregate.status == "complete", [
        (outcome.status, [failure.reason for failure in outcome.failures])
        for outcome in aggregate.source_outcomes
    ]
    assert [call.download_quality_profile for call in calls] == [
        BEST_ORIGINAL_PROFILE,
        MP3_320_COMPATIBILITY_PROFILE,
    ]
    assert all(call.compatibility_mp3_bitrate_kbps == 320 for call in calls)
    assert aggregate.total_downloaded == 1
    assert aggregate.total_existing == 1
    assert aggregate.total_imported == 1
    assert aggregate.total_source_preserved == 0
    assert aggregate.total_source_preserved_remux == 1
    assert aggregate.total_mp3_compatibility_transcodes == 0
    assert aggregate.total_quality_failures == 0

    rows = list(db.conn.execute("SELECT id, path FROM tracks"))
    assert len(rows) == 1
    assert Path(rows[0]["path"]).is_file()
    assert len(list((tmp_path / "downloads").rglob("*.opus"))) == 1
    assert db.canonical_track_id("youtube", VIDEO_ID) == rows[0]["id"]
    quality = db.get_track_media_quality(rows[0]["id"])
    assert quality["acquisition_profile"] == BEST_ORIGINAL_PROFILE
    assert quality["source_codec"] == "opus"
    assert quality["stored_codec"] == "opus"
    assert quality["transformation_kind"] == "source_preserved_remux"

    run_rows = list(
        db.conn.execute(
            "SELECT source_id, source_preserved_count, "
            "source_preserved_remux_count, "
            "mp3_compatibility_transcode_count, quality_failure_count, "
            "total_stored_bytes FROM sync_source_runs ORDER BY source_id"
        )
    )
    assert tuple(run_rows[0]) == (
        inherited.id,
        0,
        1,
        0,
        0,
        len(b"synthetic opus"),
    )
    # Source B requests MP3 compatibility, but the shared canonical item is
    # not redownloaded. Its result reports the already stored Best Original
    # representation without double-counting an acquisition or stored bytes.
    assert tuple(run_rows[1]) == (compatibility.id, 0, 0, 0, 0, 0)
    assert aggregate.source_outcomes[1].reused_quality_profile_counts == {
        BEST_ORIGINAL_PROFILE: 1
    }
    assert aggregate.reused_quality_profile_counts == {
        BEST_ORIGINAL_PROFILE: 1
    }
    assert aggregate.reused_stored_codec_counts == {"opus": 1}
    assert transitions[-1]["last_sync_source_preserved_remux_count"] == 1
    assert transitions[-1]["last_sync_total_stored_bytes"] == len(
        b"synthetic opus"
    )
    db.close()


def test_verified_acquisition_facts_survive_a_later_source_import_retry(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3")
    sources = SyncSourceService(db)
    sources.create_source("PLbatch11RA", label="Initial import failure")
    sources.create_source("PLbatch11RB", label="Retry source")
    calls: list[object] = []
    import_attempts = 0

    def retrying_importer(target_db, item: SyncImportItem) -> int:
        nonlocal import_attempts
        import_attempts += 1
        if import_attempts == 1:
            raise RuntimeError("Synthetic first import failure")
        return _synthetic_import(target_db, item)

    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=sources,
        syncer_factory=lambda config, report: _QualitySyncer(
            config,
            report,
            calls,
        ),
        importer=retrying_importer,
    )

    aggregate = orchestrator.sync_all_enabled()

    assert aggregate.total_downloaded == 1
    assert aggregate.total_existing == 1
    assert aggregate.total_imported == 1
    assert aggregate.source_outcomes[0].status == "complete_with_issues"
    assert aggregate.source_outcomes[1].status == "complete"
    assert import_attempts == 2
    assert len(list((tmp_path / "downloads").rglob("*.opus"))) == 1
    track = db.conn.execute("SELECT id FROM tracks").fetchone()
    quality = db.get_track_media_quality(track["id"])
    assert quality["acquisition_profile"] == BEST_ORIGINAL_PROFILE
    assert quality["source_format_id"] == "synthetic-opus"
    assert quality["source_codec"] == quality["stored_codec"] == "opus"
    assert aggregate.total_source_preserved_remux == 1
    assert aggregate.total_stored_bytes == len(b"synthetic opus")
    db.close()


def test_quality_item_failure_is_persisted_in_source_run_metrics(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3")
    sources = SyncSourceService(db)
    source = sources.create_source("PLbatch11QA", label="Quality failure")
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=sources,
    )
    result = SyncResult(
        "complete",
        source.external_id,
        "Synthetic quality source",
    )
    result.add_failure(
        SyncFailure(
            VIDEO_ID,
            "Synthetic item",
            "Synthetic codec mismatch.",
            "quality",
        )
    )

    with db.conn:
        orchestrator._record_source_run(source.id, "batch-11-test", result)

    row = db.conn.execute(
        "SELECT status, failed_count, quality_failure_count "
        "FROM sync_source_runs WHERE source_id=?",
        (source.id,),
    ).fetchone()
    assert tuple(row) == ("complete_with_issues", 1, 1)
    db.close()


def _webm_inspection(
    path: Path,
    *,
    audio_stream_count: int | None,
    video_stream_count: int | None,
    codec: str | None = "opus",
) -> AudioInspection:
    return AudioInspection(
        path=path.resolve(),
        extension=".webm",
        container="webm",
        codec=codec,
        bitrate_kbps=160,
        sample_rate_hz=48_000,
        channels=2,
        duration_seconds=1.0,
        filesize_bytes=path.stat().st_size,
        audio_stream_count=audio_stream_count,
        video_stream_count=video_stream_count,
        inspection_method="ffprobe",
    )


def _patch_webm_import(monkeypatch, inspection: AudioInspection) -> None:
    from music_vault.core import importer

    monkeypatch.setattr(
        importer,
        "discover_ffmpeg",
        lambda: SimpleNamespace(ready=True, ffprobe_path=Path("ffprobe.exe")),
    )
    monkeypatch.setattr(
        importer,
        "is_verified_audio_only_webm",
        lambda *_args, **_kwargs: bool(
            inspection.audio_stream_count is not None
            and inspection.audio_stream_count >= 1
            and inspection.video_stream_count == 0
            and inspection.codec
            in {"opus", "aac", "vorbis", "mp3", "flac", "alac"}
        ),
    )
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: {
            "title": "Synthetic WebM",
            "artist": None,
            "album": None,
            "album_artist": None,
            "release_date": None,
            "year": None,
            "duration_seconds": 1.0,
            "title_provenance": "embedded",
        },
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)


def test_audio_only_webm_import_is_guarded_and_read_only(tmp_path, monkeypatch):
    media = tmp_path / "audio-only.webm"
    media.write_bytes(b"synthetic webm")
    before_bytes = media.read_bytes()
    before_mtime = media.stat().st_mtime_ns
    inspection = _webm_inspection(
        media,
        audio_stream_count=1,
        video_stream_count=0,
    )
    _patch_webm_import(monkeypatch, inspection)
    db = MusicVaultDB(tmp_path / "library.sqlite3")

    assert import_file(db, media) is True
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert media.read_bytes() == before_bytes
    assert media.stat().st_mtime_ns == before_mtime
    db.close()


@pytest.mark.parametrize(
    ("audio_stream_count", "video_stream_count", "codec"),
    (
        (1, 1, "opus"),
        (None, None, "opus"),
        (1, 0, "unsupported-codec"),
    ),
)
def test_webm_import_rejects_video_inconclusive_or_unsupported_media(
    tmp_path,
    monkeypatch,
    audio_stream_count,
    video_stream_count,
    codec,
):
    media = tmp_path / "rejected.webm"
    media.write_bytes(b"synthetic webm")
    before_bytes = media.read_bytes()
    before_mtime = media.stat().st_mtime_ns
    inspection = _webm_inspection(
        media,
        audio_stream_count=audio_stream_count,
        video_stream_count=video_stream_count,
        codec=codec,
    )
    _patch_webm_import(monkeypatch, inspection)
    db = MusicVaultDB(tmp_path / "library.sqlite3")

    assert import_file(db, media) is False
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    assert media.read_bytes() == before_bytes
    assert media.stat().st_mtime_ns == before_mtime
    db.close()


def test_native_import_keeps_a_private_thumbnail_without_replacing_valid_artwork(
    tmp_path,
    monkeypatch,
) -> None:
    from music_vault.core import importer

    media = tmp_path / f"Native [{VIDEO_ID}].opus"
    media.write_bytes(b"synthetic native audio")
    before_media = (media.read_bytes(), media.stat().st_mtime_ns)
    first_thumbnail = tmp_path / f"First [{VIDEO_ID}].png"
    second_thumbnail = tmp_path / f"Second [{VIDEO_ID}].png"
    for path, color in (
        (first_thumbnail, QColor("#336699")),
        (second_thumbnail, QColor("#993366")),
    ):
        image = QImage(3, 3, QImage.Format.Format_ARGB32)
        image.fill(color)
        assert image.save(str(path), "PNG")
    monkeypatch.setattr(importer, "covers_dir", lambda: tmp_path / "private-covers")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: {
            "title": "Synthetic Native",
            "artist": None,
            "album": None,
            "album_artist": None,
            "release_date": None,
            "year": None,
            "duration_seconds": 1.0,
            "title_provenance": "embedded",
        },
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    db = MusicVaultDB(tmp_path / "library.sqlite3")

    assert import_file(
        db,
        media,
        ImportSourceContext(
            "youtube",
            VIDEO_ID,
            private_cover_path=str(first_thumbnail),
        ),
    )
    track = db.conn.execute(
        "SELECT id, cover_path FROM tracks WHERE path=?",
        (str(media.resolve()),),
    ).fetchone()
    first_private_cover = Path(track["cover_path"])
    assert first_private_cover.is_file()
    assert first_private_cover.is_relative_to((tmp_path / "private-covers").resolve())

    assert import_file(
        db,
        media,
        ImportSourceContext(
            "youtube",
            VIDEO_ID,
            private_cover_path=str(second_thumbnail),
        ),
    )
    refreshed_cover = db.conn.execute(
        "SELECT cover_path FROM tracks WHERE id=?",
        (track["id"],),
    ).fetchone()[0]
    assert Path(refreshed_cover).resolve() == first_private_cover.resolve()
    assert (media.read_bytes(), media.stat().st_mtime_ns) == before_media
    db.close()


def test_optional_private_cover_storage_failure_does_not_fail_audio_import(
    tmp_path,
    monkeypatch,
) -> None:
    from music_vault.core import importer

    media = tmp_path / f"Native [{VIDEO_ID}].opus"
    media.write_bytes(b"synthetic native audio")
    thumbnail = tmp_path / f"Native [{VIDEO_ID}].png"
    image = QImage(3, 3, QImage.Format.Format_ARGB32)
    image.fill(QColor("#336699"))
    assert image.save(str(thumbnail), "PNG")
    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: {
            "title": "Synthetic Native",
            "artist": None,
            "album": None,
            "album_artist": None,
            "release_date": None,
            "year": None,
            "duration_seconds": 1.0,
            "title_provenance": "embedded",
        },
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)
    monkeypatch.setattr(
        importer,
        "_save_cover",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic")),
    )
    db = MusicVaultDB(tmp_path / "library.sqlite3")

    assert import_file(
        db,
        media,
        ImportSourceContext(
            "youtube",
            VIDEO_ID,
            private_cover_path=str(thumbnail),
        ),
    )
    assert db.conn.execute("SELECT cover_path FROM tracks").fetchone()[0] is None
    db.close()
