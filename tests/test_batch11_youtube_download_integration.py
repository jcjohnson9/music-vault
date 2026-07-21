from __future__ import annotations

from pathlib import Path

import pytest

from music_vault.core.audio_inspection import AudioInspection, DeterministicFinalPathTracker
from music_vault.core.audio_quality_config import (
    BEST_ORIGINAL_PROFILE,
    MP3_320_COMPATIBILITY_PROFILE,
)
from music_vault.core.ffmpeg import FFmpegDiscoveryResult
from music_vault.core.youtube_sync import (
    AudioQualityDownloadError,
    AuthorizedYouTubePlaylistSyncer,
    YouTubeSyncConfig,
    scan_existing_downloads,
)


VIDEO_ID = "abcdefghijk"


def _ready_discovery(root: Path) -> FFmpegDiscoveryResult:
    return FFmpegDiscoveryResult(
        True,
        "configured",
        root,
        root / "ffmpeg.exe",
        root / "ffprobe.exe",
    )


class _UnexpectedExtensionYDL:
    def __init__(self, options: dict) -> None:
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool):
        if not download:
            return {
                "id": VIDEO_ID,
                "duration": 12.0,
                "formats": [
                    {
                        "format_id": "provider-ranked-audio",
                        "ext": "webm",
                        "container": "webm",
                        "acodec": "opus",
                        "vcodec": "none",
                        "abr": 160,
                        "duration": 12.0,
                    }
                ],
            }

        destination = Path(self.options["outtmpl"]).parent
        rejected = destination / f"Rejected [{VIDEO_ID}].m4a"
        rejected.write_bytes(b"new rejected attempt")
        event = {
            "status": "finished",
            "filename": str(rejected),
            "filepath": str(rejected),
            "info_dict": {"filepath": str(rejected)},
        }
        for hook in self.options.get("progress_hooks", ()):
            hook(event)
        for hook in self.options.get("postprocessor_hooks", ()):
            hook(event)
        return {
            "id": VIDEO_ID,
            "filepath": str(rejected),
            "upload_date": "20240102",
        }


def test_rejected_hook_reported_output_is_removed_and_cannot_be_reused(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: FFmpegDiscoveryResult(False, "none"),
    )
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.yt_dlp.YoutubeDL",
        _UnexpectedExtensionYDL,
    )
    config = YouTubeSyncConfig(
        playlist_url="https://www.youtube.com/playlist?list=PLbatch11",
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)

    with pytest.raises(AudioQualityDownloadError):
        syncer._download_one(VIDEO_ID, "PLbatch11", "Synthetic")

    assert scan_existing_downloads(config.output_dir) == {}
    assert [path for path in config.output_dir.rglob("*") if path.is_file()] == []


def test_rejected_attempt_cleanup_never_deletes_a_preexisting_file(tmp_path) -> None:
    destination = tmp_path / "managed-source"
    destination.mkdir()
    existing = destination / f"Existing [{VIDEO_ID}].opus"
    existing.write_bytes(b"preexisting personal media")
    tracker = DeterministicFinalPathTracker(destination, VIDEO_ID)
    tracker.record_path(existing)

    AuthorizedYouTubePlaylistSyncer._discard_rejected_attempt(
        tracker,
        destination,
        VIDEO_ID,
        frozenset({existing.resolve()}),
    )

    assert existing.read_bytes() == b"preexisting personal media"


def test_rejected_attempt_cleanup_never_deletes_a_nested_unowned_file(tmp_path) -> None:
    destination = tmp_path / "managed-source"
    nested = destination / "nested"
    nested.mkdir(parents=True)
    existing = nested / f"Existing [{VIDEO_ID}].opus"
    existing.write_bytes(b"nested personal media")
    tracker = DeterministicFinalPathTracker(destination, VIDEO_ID)
    tracker.record_path(existing)

    AuthorizedYouTubePlaylistSyncer._discard_rejected_attempt(
        tracker,
        destination,
        VIDEO_ID,
        frozenset(),
    )

    assert existing.read_bytes() == b"nested personal media"


def test_existing_download_scan_rejects_unverified_video_webm(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    audio = root / f"Audio [{VIDEO_ID}].webm"
    video = root / "Video [bcdefghijkl].webm"
    audio.write_bytes(b"audio webm")
    video.write_bytes(b"video webm")
    probe = tmp_path / "ffprobe.exe"
    probe.write_bytes(b"synthetic probe")
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: FFmpegDiscoveryResult(
            True,
            "configured",
            tmp_path,
            tmp_path / "ffmpeg.exe",
            probe,
        ),
    )
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.is_verified_reusable_audio",
        lambda path, **_kwargs: Path(path).name.startswith("Audio "),
    )

    found = scan_existing_downloads(root)

    assert found == {VIDEO_ID: audio.resolve()}


def test_existing_download_scan_fails_closed_when_probe_discovery_raises(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    candidate = root / f"Untrusted [{VIDEO_ID}].webm"
    candidate.write_bytes(b"synthetic")
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: (_ for _ in ()).throw(OSError("synthetic")),
    )

    assert scan_existing_downloads(root) == {}


def test_existing_download_scan_rejects_corrupt_native_audio(tmp_path, monkeypatch) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    (root / f"Corrupt [{VIDEO_ID}].opus").write_bytes(b"not audio")
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: FFmpegDiscoveryResult(False, "none"),
    )

    assert scan_existing_downloads(root) == {}


def test_existing_download_scan_skips_database_known_ids_without_reinspection(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    (root / f"Known [{VIDEO_ID}].opus").write_bytes(b"database-owned")
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.is_verified_reusable_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("database-known media must not be reinspected")
        ),
    )

    assert scan_existing_downloads(root, exclude_video_ids={VIDEO_ID}) == {}


def test_failed_unreported_postprocessor_output_is_removed(tmp_path, monkeypatch) -> None:
    class FailingYDL:
        def __init__(self, options: dict) -> None:
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            if not download:
                return {
                    "id": VIDEO_ID,
                    "duration": 12.0,
                    "formats": [
                        {
                            "format_id": "dynamic-opus",
                            "ext": "webm",
                            "acodec": "opus",
                            "vcodec": "none",
                            "duration": 12.0,
                        }
                    ],
                }
            destination = Path(self.options["outtmpl"]).parent
            (destination / f"Unreported [{VIDEO_ID}].opus").write_bytes(b"partial")
            raise RuntimeError("synthetic postprocessor failure")

    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: _ready_discovery(tmp_path),
    )
    monkeypatch.setattr("music_vault.core.youtube_sync.yt_dlp.YoutubeDL", FailingYDL)
    config = YouTubeSyncConfig(
        playlist_url="https://www.youtube.com/playlist?list=PLbatch11",
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )

    with pytest.raises(RuntimeError):
        AuthorizedYouTubePlaylistSyncer(config)._download_one(
            VIDEO_ID,
            "PLbatch11",
            "Synthetic",
        )

    assert not list(config.output_dir.rglob("*.opus"))


def test_preexisting_same_source_media_never_reaches_postprocessing(
    tmp_path,
    monkeypatch,
) -> None:
    download_calls = 0

    class GuardedYDL:
        def __init__(self, options: dict) -> None:
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            nonlocal download_calls
            if download:
                download_calls += 1
                raise AssertionError("postprocessing must not start")
            return {
                "id": VIDEO_ID,
                "duration": 12.0,
                "formats": [
                    {
                        "format_id": "dynamic-opus",
                        "ext": "webm",
                        "acodec": "opus",
                        "vcodec": "none",
                        "duration": 12.0,
                    }
                ],
            }

    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: _ready_discovery(tmp_path),
    )
    monkeypatch.setattr("music_vault.core.youtube_sync.yt_dlp.YoutubeDL", GuardedYDL)
    config = YouTubeSyncConfig(
        playlist_url="https://www.youtube.com/playlist?list=PLbatch11",
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)
    destination = syncer._download_destination("Synthetic", "PLbatch11")
    destination.mkdir(parents=True)
    existing = destination / f"Existing [{VIDEO_ID}].opus"
    existing.write_bytes(b"personal preexisting media")

    with pytest.raises(AudioQualityDownloadError, match="left it unchanged"):
        syncer._download_one(
            VIDEO_ID,
            "PLbatch11",
            "Synthetic",
        )

    assert download_calls == 0
    assert existing.read_bytes() == b"personal preexisting media"


def test_early_metadata_failure_preserves_preexisting_same_source_artwork(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: _ready_discovery(tmp_path),
    )
    config = YouTubeSyncConfig(
        playlist_url="https://www.youtube.com/playlist?list=PLbatch11",
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)
    destination = syncer._download_destination("Synthetic", "PLbatch11")
    destination.mkdir(parents=True)
    artwork = destination / f"Existing [{VIDEO_ID}].jpg"
    artwork.write_bytes(b"preexisting personal artwork")
    before = (artwork.read_bytes(), artwork.stat().st_mtime_ns)
    monkeypatch.setattr(
        syncer,
        "_attempt_path_baseline",
        lambda *_args: (_ for _ in ()).throw(
            AudioQualityDownloadError("synthetic baseline failure")
        ),
    )

    with pytest.raises(AudioQualityDownloadError, match="baseline failure"):
        syncer._download_one(VIDEO_ID, "PLbatch11", "Synthetic")

    assert (artwork.read_bytes(), artwork.stat().st_mtime_ns) == before


def test_preexisting_thumbnail_does_not_block_missing_media_acquisition(
    tmp_path,
    monkeypatch,
) -> None:
    download_calls = 0

    class ThumbnailSafeYDL:
        def __init__(self, _options: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            nonlocal download_calls
            if download:
                download_calls += 1
                raise RuntimeError("synthetic acquisition reached")
            return {
                "id": VIDEO_ID,
                "duration": 12.0,
                "formats": [
                    {
                        "format_id": "dynamic-opus",
                        "ext": "webm",
                        "acodec": "opus",
                        "vcodec": "none",
                        "duration": 12.0,
                    }
                ],
            }

    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: _ready_discovery(tmp_path),
    )
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.yt_dlp.YoutubeDL",
        ThumbnailSafeYDL,
    )
    config = YouTubeSyncConfig(
        playlist_url="https://www.youtube.com/playlist?list=PLbatch11",
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)
    destination = syncer._download_destination("Synthetic", "PLbatch11")
    destination.mkdir(parents=True)
    artwork = destination / f"Existing [{VIDEO_ID}].jpg"
    artwork.write_bytes(b"preexisting personal artwork")
    before = (artwork.read_bytes(), artwork.stat().st_mtime_ns)

    with pytest.raises(RuntimeError, match="acquisition reached"):
        syncer._download_one(VIDEO_ID, "PLbatch11", "Synthetic")

    assert download_calls == 1
    assert (artwork.read_bytes(), artwork.stat().st_mtime_ns) == before


def test_restart_reuse_recovers_only_one_unambiguous_private_thumbnail(tmp_path) -> None:
    destination = tmp_path / "downloads"
    destination.mkdir()
    media = destination / f"Recovered [{VIDEO_ID}].opus"
    media.write_bytes(b"synthetic")
    thumbnail = destination / f"Recovered [{VIDEO_ID}].jpg"
    thumbnail.write_bytes(b"synthetic thumbnail")

    assert (
        AuthorizedYouTubePlaylistSyncer._existing_private_cover_path(media, VIDEO_ID)
        == thumbnail.resolve()
    )
    (destination / f"Alternate [{VIDEO_ID}].png").write_bytes(b"ambiguous")
    assert (
        AuthorizedYouTubePlaylistSyncer._existing_private_cover_path(media, VIDEO_ID)
        is None
    )


@pytest.mark.parametrize(
    ("profile", "final_suffix", "final_codec", "expected_transform"),
    (
        (BEST_ORIGINAL_PROFILE, ".opus", "opus", "source_preserved_remux"),
        (
            MP3_320_COMPATIBILITY_PROFILE,
            ".mp3",
            "mp3",
            "lossy_transcode",
        ),
    ),
)
def test_download_success_path_returns_verified_quality_facts(
    tmp_path,
    monkeypatch,
    profile,
    final_suffix,
    final_codec,
    expected_transform,
) -> None:
    option_sets: list[dict] = []

    class SuccessfulYDL:
        def __init__(self, options: dict) -> None:
            self.options = options
            option_sets.append(options)

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            if not download:
                return {
                    "id": VIDEO_ID,
                    "duration": 12.0,
                    "formats": [
                        {
                            "format_id": "dynamic-provider-choice",
                            "ext": "webm",
                            "container": "webm",
                            "acodec": "opus",
                            "vcodec": "none",
                            "abr": 160,
                            "asr": 48_000,
                            "audio_channels": 2,
                            "duration": 12.0,
                        }
                    ],
                }
            destination = Path(self.options["outtmpl"]).parent
            final_path = destination / f"Verified [{VIDEO_ID}]{final_suffix}"
            final_path.write_bytes(b"verified synthetic audio")
            thumbnails = []
            if profile == BEST_ORIGINAL_PROFILE:
                thumbnail = destination / f"Verified [{VIDEO_ID}].jpg"
                thumbnail.write_bytes(b"synthetic private thumbnail")
                thumbnails.append({"filepath": str(thumbnail)})
            event = {
                "status": "finished",
                "filename": str(final_path),
                "filepath": str(final_path),
                "info_dict": {
                    "filepath": str(final_path),
                    "thumbnails": thumbnails,
                },
            }
            for hook in self.options.get("progress_hooks", ()):
                hook(event)
            for hook in self.options.get("postprocessor_hooks", ()):
                hook(event)
            return {
                "id": VIDEO_ID,
                "filepath": str(final_path),
                "upload_date": "20240102",
                "thumbnails": thumbnails,
            }

    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: _ready_discovery(tmp_path),
    )
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.yt_dlp.YoutubeDL",
        SuccessfulYDL,
    )

    def inspect(path, **_kwargs):
        resolved = Path(path).resolve()
        return AudioInspection(
            path=resolved,
            extension=final_suffix,
            container="ogg" if final_suffix == ".opus" else "mp3",
            codec=final_codec,
            bitrate_kbps=160 if final_codec == "opus" else 320,
            sample_rate_hz=48_000,
            channels=2,
            duration_seconds=12.0,
            filesize_bytes=resolved.stat().st_size,
            audio_stream_count=1,
            video_stream_count=0,
            inspection_method="ffprobe",
        )

    monkeypatch.setattr("music_vault.core.youtube_sync.inspect_audio_file", inspect)
    syncer = AuthorizedYouTubePlaylistSyncer(
        YouTubeSyncConfig(
            playlist_url="https://www.youtube.com/playlist?list=PLbatch11",
            output_dir=tmp_path / "downloads",
            archive_file=tmp_path / "archive.txt",
            download_quality_profile=profile,
        )
    )

    item = syncer._download_one(VIDEO_ID, "PLbatch11", "Synthetic")

    assert Path(item.path).suffix == final_suffix
    assert item.quality_facts is not None
    assert item.quality_facts["source_codec"] == "opus"
    assert item.quality_facts["stored_codec"] == final_codec
    assert item.quality_facts["transformation_kind"] == expected_transform
    download_options = option_sets[1]
    assert download_options["format"] == "dynamic-provider-choice"
    extract_options = download_options["postprocessors"][0]
    if profile == BEST_ORIGINAL_PROFILE:
        assert extract_options == {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "best",
        }
        assert download_options["writethumbnail"] is True
        assert download_options["embedthumbnail"] is False
        assert item.private_cover_path is not None
        assert Path(item.private_cover_path).is_file()
    else:
        assert extract_options["preferredcodec"] == "mp3"
        assert extract_options["preferredquality"] == "320"
        assert item.private_cover_path is None


def test_quality_failure_is_an_item_issue_and_is_not_archived(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.discover_ffmpeg",
        lambda _configured=None: FFmpegDiscoveryResult(False, "none"),
    )
    config = YouTubeSyncConfig(
        playlist_url="https://www.youtube.com/playlist?list=PLbatch11",
        output_dir=tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
    )
    syncer = AuthorizedYouTubePlaylistSyncer(config)
    monkeypatch.setattr(
        syncer,
        "_extract_playlist_entries_via_api",
        lambda: (
            "PLbatch11",
            "Synthetic",
            [
                {
                    "id": VIDEO_ID,
                    "source_item_id": "occurrence-1",
                    "position": 0,
                    "title": "Synthetic item",
                    "unavailable_reason": None,
                }
            ],
        ),
    )
    monkeypatch.setattr(
        syncer,
        "_download_one",
        lambda *_args: (_ for _ in ()).throw(
            AudioQualityDownloadError("Synthetic final codec mismatch.")
        ),
    )

    result = syncer.sync()

    assert result.status == "complete_with_issues"
    assert result.quality_failure_count == 1
    assert result.failures[0].error_category == "quality"
    assert result.import_items == []
    assert config.archive_file.read_text(encoding="utf-8") == ""
