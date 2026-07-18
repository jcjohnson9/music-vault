from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp

from .ffmpeg import FFmpegDiscoveryResult, discover_ffmpeg
from .paths import youtube_api_key_path
from .runtime_policy import runtime_policy_for
from .safety import (
    extract_source_video_id,
    normalize_source_upload_date,
    playlist_output_directory,
    sanitize_error_text,
)
from .sync_result import (
    PlaylistSnapshot,
    PlaylistSnapshotItem,
    SyncFailure,
    SyncImportItem,
    SyncResult,
    utc_now,
)


ProgressCallback = Callable[[str], None]
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class _SanitizedYDLLogger:
    def __init__(self, report: ProgressCallback) -> None:
        self.report = report

    def debug(self, message: str) -> None:
        # yt-dlp sends ordinary informational messages through debug().
        if not str(message).startswith("[debug]"):
            self.report(sanitize_error_text(message))

    def warning(self, message: str) -> None:
        self.report(f"Warning: {sanitize_error_text(message)}")

    def error(self, message: str) -> None:
        self.report(f"Error: {sanitize_error_text(message)}")


@dataclass(frozen=True)
class YouTubeSyncConfig:
    playlist_url: str
    output_dir: Path
    archive_file: Path
    audio_format: str = "mp3"
    audio_quality: str = "320"
    existing_video_ids: frozenset[str] = field(default_factory=frozenset)
    ffmpeg_location: str | Path | None = None
    source_destination_dir: Path | None = None
    saved_source_id: int | None = None
    source_label: str | None = None
    # None preserves the legacy self-scan. An explicitly supplied tuple,
    # including an empty tuple, is an already-built batch-wide index.
    known_downloads: tuple[tuple[str, str], ...] | None = None
    # A multi-source batch supplies one prevalidated, read-only view over its
    # mutable media index. The view is borrowed rather than copied: the
    # sequential orchestrator may add a completed download between sources,
    # while an individual provider only performs keyed reads.
    shared_download_index: Mapping[str, Path] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def scan_existing_downloads(output_root: str | Path) -> dict[str, Path]:
    """Build one bounded source-ID index for a complete configured download tree."""

    root = Path(output_root).expanduser().resolve()
    found: dict[str, Path] = {}
    if not root.exists():
        return found
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in AuthorizedYouTubePlaylistSyncer.AUDIO_SUFFIXES:
            video_id = extract_source_video_id(path)
            if video_id:
                found.setdefault(video_id, path.resolve())
    return found


class AuthorizedYouTubePlaylistSyncer:
    """Synchronize an authorized public/unlisted playlist without browser cookies."""

    AUDIO_SUFFIXES = {".mp3", ".m4a", ".webm", ".opus", ".flac", ".wav", ".ogg", ".aac"}

    def __init__(
        self,
        config: YouTubeSyncConfig,
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        policy = runtime_policy_for()
        if not policy.network_allowed:
            raise RuntimeError("youtube_sync_deferred_acceptance_no_network")
        if not policy.secrets_allowed:
            raise RuntimeError("youtube_sync_deferred_acceptance_no_secrets")
        self.config = config
        self.progress = progress or (lambda message: None)
        self._ffmpeg_discovery: FFmpegDiscoveryResult | None = None
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.config.archive_file.parent.mkdir(parents=True, exist_ok=True)

    def _report(self, message: object) -> None:
        self.progress(sanitize_error_text(message))

    def _playlist_id(self) -> str:
        parsed = urlparse(self.config.playlist_url)
        playlist_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
        if not playlist_id:
            raise RuntimeError(
                "Could not find playlist ID in URL. Use a YouTube playlist URL containing list=."
            )
        return playlist_id

    def _api_key(self) -> str:
        if not runtime_policy_for().secrets_allowed:
            raise RuntimeError("YouTube synchronization is unavailable in no-secret mode.")
        key_path = youtube_api_key_path()
        if not key_path.exists():
            raise RuntimeError("The YouTube Data API key file is missing.")
        key = key_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not key:
            raise RuntimeError("The YouTube Data API key file is empty.")
        return key

    def _resolve_ffmpeg_once(self) -> FFmpegDiscoveryResult:
        if self._ffmpeg_discovery is None:
            self._ffmpeg_discovery = discover_ffmpeg(self.config.ffmpeg_location)
        result = self._ffmpeg_discovery
        configured = self.config.ffmpeg_location
        if configured is not None and str(configured).strip() and not result.ready:
            detail = result.error or "Both ffmpeg.exe and ffprobe.exe are required."
            raise RuntimeError(f"Configured FFmpeg is not ready: {detail}")
        return result

    def _ffmpeg_location(self) -> str | None:
        result = self._resolve_ffmpeg_once()
        return result.yt_dlp_location

    def _archive_ids(self) -> set[str]:
        if not self.config.archive_file.is_file():
            return set()
        ids: set[str] = set()
        for line in self.config.archive_file.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            candidate = line.strip().split()[-1] if line.strip() else ""
            if _VIDEO_ID_RE.fullmatch(candidate):
                ids.add(candidate)
        return ids

    def _write_archive_ids_atomic(self, ids: set[str]) -> None:
        target = self.config.archive_file
        temporary = target.with_name(f"{target.name}.tmp")
        body = "".join(f"youtube {video_id}\n" for video_id in sorted(ids))
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, target)

    def _existing_downloads(self) -> Mapping[str, Path]:
        if self.config.shared_download_index is not None:
            # The orchestrator built and validated this index once for the
            # whole batch. Do not reconstruct it or repeat filesystem stats
            # for every source.
            return self.config.shared_download_index
        found = {
            video_id: Path(path).expanduser().resolve()
            for video_id, path in (self.config.known_downloads or ())
            if _VIDEO_ID_RE.fullmatch(str(video_id)) and Path(path).is_file()
        }
        if self.config.known_downloads is not None:
            return found
        found.update(scan_existing_downloads(self.config.output_dir))
        return found

    def _download_destination(self, playlist_title: str, playlist_id: str) -> Path:
        configured = self.config.source_destination_dir
        if configured is None:
            return playlist_output_directory(
                self.config.output_dir, playlist_title, playlist_id
            )
        root = self.config.output_dir.expanduser().resolve()
        destination = Path(configured).expanduser().resolve()
        if not destination.is_relative_to(root):
            raise RuntimeError(
                "The saved source download folder must remain inside the configured download root."
            )
        return destination

    def _api_json(self, endpoint: str, params: dict) -> dict:
        try:
            response = requests.get(endpoint, params=params, timeout=30)
        except Exception as exc:
            raise RuntimeError(sanitize_error_text(exc)) from None
        if response.status_code != 200:
            detail = sanitize_error_text(response.text[:800])
            raise RuntimeError(f"YouTube Data API error {response.status_code}: {detail}")
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"YouTube Data API returned invalid JSON: {sanitize_error_text(exc)}") from None

    def _get_playlist_title(self, playlist_id: str, api_key: str) -> str:
        data = self._api_json(
            "https://www.googleapis.com/youtube/v3/playlists",
            {"part": "snippet", "id": playlist_id, "key": api_key},
        )
        items = data.get("items") or []
        if not items:
            raise RuntimeError("The playlist is unavailable through the public/unlisted API workflow.")
        return (items[0].get("snippet") or {}).get("title") or "YouTube Playlist"

    def _extract_playlist_entries_via_api(self) -> tuple[str, str, list[dict]]:
        api_key = self._api_key()
        playlist_id = self._playlist_id()
        self._report("Reading playlist using the YouTube Data API (public/unlisted mode).")
        playlist_title = self._get_playlist_title(playlist_id, api_key)
        entries: list[dict] = []
        page_token = None

        while True:
            params = {
                "part": "snippet,contentDetails,status",
                "playlistId": playlist_id,
                "maxResults": 50,
                "key": api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._api_json(
                "https://www.googleapis.com/youtube/v3/playlistItems", params
            )
            for item in data.get("items", []):
                source_item_id = str(item.get("id") or "").strip()
                if not source_item_id:
                    raise RuntimeError(
                        "YouTube returned a playlist item without a stable occurrence ID."
                    )
                snippet = item.get("snippet") or {}
                content = item.get("contentDetails") or {}
                video_id = str(content.get("videoId") or "").strip()
                title = str(snippet.get("title") or video_id or "Unavailable item")
                raw_position = snippet.get("position")
                try:
                    position = int(raw_position)
                    if position < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    position = len(entries)
                unavailable_reason = None
                if not _VIDEO_ID_RE.fullmatch(video_id):
                    unavailable_reason = "Playlist item has no usable video ID."
                elif title in {"Deleted video", "Private video"}:
                    unavailable_reason = (
                        f"{title} is unavailable through the supported public/unlisted workflow."
                    )
                entries.append(
                    {
                        "id": video_id or None,
                        "source_item_id": source_item_id,
                        "position": position,
                        "title": title,
                        "unavailable_reason": unavailable_reason,
                    }
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        self._report(f"Playlist items visible to Music Vault: {len(entries)}")
        return playlist_id, playlist_title, entries

    def _download_one(
        self, video_id: str, playlist_id: str, playlist_title: str
    ) -> SyncImportItem:
        destination = self._download_destination(playlist_title, playlist_id)
        destination.mkdir(parents=True, exist_ok=True)
        opts = {
            "format": "bestaudio/best",
            "ignoreerrors": False,
            "noplaylist": True,
            "writethumbnail": True,
            "embedthumbnail": True,
            "addmetadata": True,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 10,
            "socket_timeout": 30,
            "continuedl": True,
            "overwrites": False,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": self.config.audio_format,
                    "preferredquality": self.config.audio_quality,
                },
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            "outtmpl": str(destination / "%(title).180s [%(id)s].%(ext)s"),
            "progress_hooks": [self._hook],
            "logger": _SanitizedYDLLogger(self._report),
            "quiet": True,
            "no_warnings": False,
            "restrictfilenames": False,
        }
        ffmpeg_location = self._ffmpeg_location()
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", download=True
                )
        except Exception as exc:
            raise RuntimeError(sanitize_error_text(exc)) from None
        if not info:
            raise RuntimeError(
                "The item is unavailable through the supported public/unlisted workflow."
            )

        matches = [
            path.resolve()
            for path in destination.iterdir()
            if path.is_file()
            and path.suffix.lower() in self.AUDIO_SUFFIXES
            and extract_source_video_id(path) == video_id
        ]
        if not matches:
            raise RuntimeError("Download completed but the resulting audio file was not found.")
        return SyncImportItem(
            path=str(matches[0]),
            video_id=video_id,
            source_upload_date=normalize_source_upload_date(info.get("upload_date")),
        )

    def _hook(self, status: dict) -> None:
        state = status.get("status")
        if state == "downloading":
            filename = Path(status.get("filename", "")).name
            percent = status.get("_percent_str", "").strip()
            self._report(f"Downloading {filename} {percent}".strip())
        elif state == "finished":
            self._report("Download finished. Converting and tagging audio.")
        elif state == "error":
            self._report("Download error. The item will be recorded for retry.")

    @staticmethod
    def _snapshot_from_entries(
        playlist_id: str,
        playlist_title: str,
        entries: list[dict],
    ) -> PlaylistSnapshot:
        items: list[PlaylistSnapshotItem] = []
        for index, entry in enumerate(entries):
            # The fallback keeps old test/provider subclasses compatible. The
            # production API path rejects missing top-level playlist-item IDs.
            source_item_id = str(entry.get("source_item_id") or "").strip()
            if not source_item_id:
                source_item_id = f"legacy-occurrence-{index}"
            raw_position = entry.get("position", index)
            try:
                position = int(raw_position)
                if position < 0:
                    raise ValueError
            except (TypeError, ValueError):
                position = index
            video_id = str(entry.get("id") or "").strip() or None
            items.append(
                PlaylistSnapshotItem(
                    source_item_id=source_item_id,
                    video_id=video_id,
                    source_position=position,
                    title=str(entry.get("title") or "").strip() or None,
                    availability_reason=(
                        str(entry.get("unavailable_reason") or "").strip() or None
                    ),
                )
            )
        return PlaylistSnapshot.completed(playlist_id, playlist_title, items)

    def sync(self) -> SyncResult:
        started_at = utc_now()
        playlist_id: str | None = None
        try:
            # Resolve once for the whole worker. In particular, a configured
            # but invalid location must fail before yt-dlp can search elsewhere.
            self._resolve_ffmpeg_once()
            playlist_id = self._playlist_id()
            playlist_id, playlist_title, entries = self._extract_playlist_entries_via_api()
            snapshot = self._snapshot_from_entries(playlist_id, playlist_title, entries)
        except Exception as exc:
            failed_snapshot = PlaylistSnapshot.failed(exc, playlist_id=playlist_id)
            return SyncResult.failed_result(
                exc,
                playlist_id=playlist_id,
                started_at=started_at,
                saved_source_id=self.config.saved_source_id,
                source_label=self.config.source_label,
                snapshot=failed_snapshot,
            )

        result = SyncResult(
            status="complete",
            playlist_id=playlist_id,
            playlist_title=playlist_title,
            visible_item_count=len(entries),
            started_at=started_at,
            saved_source_id=self.config.saved_source_id,
            source_label=self.config.source_label,
            snapshot=snapshot,
            duplicate_occurrence_count=snapshot.duplicate_occurrence_count,
        )
        local_files = self._existing_downloads()
        database_ids = set(self.config.existing_video_ids)
        archive_ids = self._archive_ids()
        if self.config.shared_download_index is None:
            # Preserve the legacy standalone/tuple behavior exactly. Those
            # paths were already materialized locally, so collecting their IDs
            # for the compatibility archive adds no new batch-wide cost.
            reliable_archive_ids = database_ids | set(local_files)
            stale_archive_ids = archive_ids - reliable_archive_ids
        else:
            stale_archive_ids = {
                video_id
                for video_id in archive_ids
                if video_id not in database_ids and local_files.get(video_id) is None
            }
            reliable_archive_ids = archive_ids - stale_archive_ids
        if stale_archive_ids:
            self._report(
                f"Archive reconciliation found {len(stale_archive_ids)} stale entr"
                f"{'y' if len(stale_archive_ids) == 1 else 'ies'}; they will not suppress downloads."
            )

        processed_video_ids: set[str] = set()
        occurrence_ids: dict[str, list[str]] = {}
        for item in snapshot.items:
            if item.video_id:
                occurrence_ids.setdefault(item.video_id, []).append(item.source_item_id)

        for item in snapshot.items:
            video_id = item.video_id
            title = item.title
            if item.availability_reason:
                result.add_failure(
                    SyncFailure(
                        video_id,
                        title,
                        item.availability_reason,
                        "unavailable",
                        item.source_item_id,
                    )
                )
                continue
            if not video_id:
                result.add_failure(
                    SyncFailure(
                        None,
                        title,
                        "Playlist item has no usable video ID.",
                        "unavailable",
                        item.source_item_id,
                    )
                )
                continue
            if video_id in processed_video_ids:
                continue
            processed_video_ids.add(video_id)
            local_path = local_files.get(video_id)
            if video_id in database_ids or local_path is not None:
                result.existing_count += 1
                result.successful_video_ids.add(video_id)
                reliable_archive_ids.add(video_id)
                if local_path is not None and video_id not in database_ids:
                    result.import_items.append(
                        SyncImportItem(
                            str(local_path),
                            video_id,
                            source_item_ids=tuple(occurrence_ids[video_id]),
                        )
                    )
                continue

            result.new_item_count += 1
            self._report(f"Downloading: {title}")
            try:
                import_item = self._download_one(video_id, playlist_id, playlist_title)
            except Exception as exc:
                result.add_failure(
                    SyncFailure(
                        video_id,
                        title,
                        sanitize_error_text(exc),
                        "download",
                        item.source_item_id,
                    )
                )
                continue
            import_item = SyncImportItem(
                import_item.path,
                import_item.video_id,
                import_item.source_upload_date,
                tuple(occurrence_ids[video_id]),
            )
            result.downloaded_count += 1
            result.downloaded_paths.append(import_item.path)
            result.import_items.append(import_item)
            result.successful_video_ids.add(video_id)
            reliable_archive_ids.add(video_id)

        # The archive is compatibility history only. Rewrite it atomically from
        # prior entries still backed by a DB/file source ID plus this source's
        # observed successes. The shared media index is intentionally not
        # copied or traversed in full for each source.
        self._write_archive_ids_atomic(reliable_archive_ids)
        result.finished_at = utc_now()
        result.refresh_status()
        self._report(
            f"Sync {result.status}: {result.downloaded_count} downloaded, "
            f"{result.existing_count} existing, {result.failed_count} failed."
        )
        return result
