from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp

from .ffmpeg import FFmpegDiscoveryResult, discover_ffmpeg
from .paths import youtube_api_key_path
from .safety import (
    extract_source_video_id,
    normalize_source_upload_date,
    playlist_output_directory,
    sanitize_error_text,
)
from .sync_result import SyncFailure, SyncImportItem, SyncResult, utc_now


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


class AuthorizedYouTubePlaylistSyncer:
    """Synchronize an authorized public/unlisted playlist without browser cookies."""

    AUDIO_SUFFIXES = {".mp3", ".m4a", ".webm", ".opus", ".flac", ".wav", ".ogg", ".aac"}

    def __init__(
        self,
        config: YouTubeSyncConfig,
        progress: Optional[ProgressCallback] = None,
    ) -> None:
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

    def _existing_downloads(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        if not self.config.output_dir.exists():
            return found
        for path in self.config.output_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() in self.AUDIO_SUFFIXES:
                video_id = extract_source_video_id(path)
                if video_id:
                    found.setdefault(video_id, path.resolve())
        return found

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
                snippet = item.get("snippet") or {}
                content = item.get("contentDetails") or {}
                video_id = str(content.get("videoId") or "").strip()
                title = str(snippet.get("title") or video_id or "Unavailable item")
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
        destination = playlist_output_directory(
            self.config.output_dir, playlist_title, playlist_id
        )
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

    def sync(self) -> SyncResult:
        started_at = utc_now()
        try:
            # Resolve once for the whole worker. In particular, a configured
            # but invalid location must fail before yt-dlp can search elsewhere.
            self._resolve_ffmpeg_once()
            playlist_id, playlist_title, entries = self._extract_playlist_entries_via_api()
        except Exception as exc:
            return SyncResult.failed_result(exc, started_at=started_at)

        result = SyncResult(
            status="complete",
            playlist_id=playlist_id,
            playlist_title=playlist_title,
            visible_item_count=len(entries),
            started_at=started_at,
        )
        local_files = self._existing_downloads()
        database_ids = set(self.config.existing_video_ids)
        reliable_ids = database_ids | set(local_files)
        stale_archive_ids = self._archive_ids() - reliable_ids
        if stale_archive_ids:
            self._report(
                f"Archive reconciliation found {len(stale_archive_ids)} stale entr"
                f"{'y' if len(stale_archive_ids) == 1 else 'ies'}; they will not suppress downloads."
            )

        for entry in entries:
            video_id = entry["id"]
            title = entry["title"]
            if entry["unavailable_reason"]:
                result.add_failure(
                    SyncFailure(video_id, title, entry["unavailable_reason"], "unavailable")
                )
                continue
            if video_id in reliable_ids:
                result.existing_count += 1
                result.successful_video_ids.add(video_id)
                if video_id in local_files and video_id not in database_ids:
                    result.import_items.append(
                        SyncImportItem(str(local_files[video_id]), video_id)
                    )
                continue

            result.new_item_count += 1
            self._report(f"Downloading: {title}")
            try:
                item = self._download_one(video_id, playlist_id, playlist_title)
            except Exception as exc:
                result.add_failure(
                    SyncFailure(video_id, title, sanitize_error_text(exc), "download")
                )
                continue
            result.downloaded_count += 1
            result.downloaded_paths.append(item.path)
            result.import_items.append(item)
            result.successful_video_ids.add(video_id)
            reliable_ids.add(video_id)

        # The archive is compatibility history only. Rewrite it atomically from
        # evidence backed by an existing DB/file source ID or this run's success.
        self._write_archive_ids_atomic(reliable_ids)
        result.finished_at = utc_now()
        result.refresh_status()
        self._report(
            f"Sync {result.status}: {result.downloaded_count} downloaded, "
            f"{result.existing_count} existing, {result.failed_count} failed."
        )
        return result
