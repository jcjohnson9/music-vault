from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse
import re
import time

import requests
import yt_dlp

from .paths import youtube_api_key_path


ProgressCallback = Callable[[str], None]


@dataclass
class YouTubeSyncConfig:
    playlist_url: str
    output_dir: Path
    archive_file: Path
    audio_format: str = "mp3"
    audio_quality: str = "320"


class AuthorizedYouTubePlaylistSyncer:
    AUDIO_SUFFIXES = {".mp3", ".m4a", ".webm", ".opus", ".flac", ".wav", ".ogg", ".aac"}

    def __init__(self, config: YouTubeSyncConfig, progress: Optional[ProgressCallback] = None) -> None:
        self.config = config
        self.progress = progress or (lambda message: None)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.config.archive_file.parent.mkdir(parents=True, exist_ok=True)
        self.failed_file = self.config.archive_file.with_name("youtube_failed_ids.txt")

    def _playlist_id(self) -> str:
        parsed = urlparse(self.config.playlist_url)
        query = parse_qs(parsed.query)
        playlist_id = (query.get("list") or [""])[0].strip()

        if not playlist_id:
            raise RuntimeError("Could not find playlist ID in URL. Use a YouTube playlist URL containing list=...")

        return playlist_id

    def _api_key(self) -> str:
        key_path = youtube_api_key_path()

        if not key_path.exists():
            raise RuntimeError("Missing data/youtube_api_key.txt. Save your YouTube Data API key there first.")

        key = key_path.read_text(encoding="utf-8", errors="ignore").strip()

        if not key:
            raise RuntimeError("data/youtube_api_key.txt is empty.")

        return key

    def _ffmpeg_location(self) -> str | None:
        tools_root = Path.home() / "Documents" / "MusicVaultTools" / "ffmpeg"

        if tools_root.exists():
            for bin_dir in tools_root.glob("*/bin"):
                if (bin_dir / "ffmpeg.exe").exists():
                    return str(bin_dir)

        return None

    def _archive_ids(self) -> set[str]:
        if not self.config.archive_file.exists():
            return set()

        ids: set[str] = set()

        for line in self.config.archive_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.strip().split()
            if parts:
                ids.add(parts[-1])

        return ids

    def _failed_ids(self) -> set[str]:
        if not self.failed_file.exists():
            return set()

        return set(
            line.strip()
            for line in self.failed_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip()
        )

    def _mark_failed(self, video_id: str) -> None:
        failed = self._failed_ids()

        if video_id not in failed:
            with self.failed_file.open("a", encoding="utf-8") as f:
                f.write(video_id + "\n")

    def _existing_download_ids(self) -> set[str]:
        ids: set[str] = set()

        if not self.config.output_dir.exists():
            return ids

        for path in self.config.output_dir.rglob("*"):
            if not path.is_file():
                continue

            if path.suffix.lower() not in self.AUDIO_SUFFIXES:
                continue

            match = re.search(r"\[([A-Za-z0-9_-]{11})\]$", path.stem)

            if match:
                ids.add(match.group(1))

        return ids

    def _repair_archive_for_missing_files(self) -> None:
        if not self.config.archive_file.exists():
            return

        existing_ids = self._existing_download_ids()
        old_lines = self.config.archive_file.read_text(encoding="utf-8", errors="ignore").splitlines()

        kept_lines: list[str] = []
        removed_count = 0

        for line in old_lines:
            parts = line.strip().split()
            video_id = parts[-1] if parts else ""

            if video_id and video_id not in existing_ids:
                removed_count += 1
                continue

            kept_lines.append(line)

        self.config.archive_file.write_text(
            "\n".join(kept_lines) + ("\n" if kept_lines else ""),
            encoding="utf-8"
        )

        if removed_count:
            self.progress(f"Archive repair: removed {removed_count} entries for missing/deleted files.")

    def _get_playlist_title(self, playlist_id: str, api_key: str) -> str:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/playlists",
            params={
                "part": "snippet",
                "id": playlist_id,
                "key": api_key,
            },
            timeout=30,
        )

        if response.status_code != 200:
            return "YouTube Playlist"

        data = response.json()
        items = data.get("items") or []

        if not items:
            return "YouTube Playlist"

        snippet = items[0].get("snippet") or {}
        return snippet.get("title") or "YouTube Playlist"

    def _extract_playlist_entries_via_api(self) -> tuple[str, list[dict]]:
        api_key = self._api_key()
        playlist_id = self._playlist_id()

        self.progress("Reading playlist using: YouTube Data API")

        playlist_title = self._get_playlist_title(playlist_id, api_key)
        entries: list[dict] = []
        page_token = None
        total_results = None

        while True:
            params = {
                "part": "snippet,contentDetails,status",
                "playlistId": playlist_id,
                "maxResults": 50,
                "key": api_key,
            }

            if page_token:
                params["pageToken"] = page_token

            response = requests.get(
                "https://www.googleapis.com/youtube/v3/playlistItems",
                params=params,
                timeout=30,
            )

            if response.status_code != 200:
                raise RuntimeError(f"YouTube Data API error {response.status_code}: {response.text[:800]}")

            data = response.json()

            if total_results is None:
                total_results = data.get("pageInfo", {}).get("totalResults")
                self.progress(f"YouTube Data API reported totalResults: {total_results}")

            for item in data.get("items", []):
                snippet = item.get("snippet") or {}
                content = item.get("contentDetails") or {}

                video_id = content.get("videoId")
                title = snippet.get("title") or video_id

                # YouTube sometimes returns placeholder/deleted/private entries.
                if not video_id:
                    continue

                if title in {"Deleted video", "Private video"}:
                    self.progress(f"Skipping hidden playlist item: {title} [{video_id}]")
                    continue

                entries.append({
                    "id": video_id,
                    "title": title or video_id,
                })

            self.progress(f"API entries collected so far: {len(entries)}")

            page_token = data.get("nextPageToken")

            if not page_token:
                break

        self.progress(f"Playlist entries visible to Music Vault: {len(entries)}")
        return playlist_title, entries

    def _cookie_modes(self) -> list[tuple[str, dict]]:
        return [
            ("Firefox cookies", {"cookiesfrombrowser": ("firefox",)}),
            ("Chrome cookies", {"cookiesfrombrowser": ("chrome",)}),
            ("Edge cookies", {"cookiesfrombrowser": ("edge",)}),
            ("No browser cookies", {}),
        ]

    def _download_one(self, video_id: str, playlist_title: str) -> bool:
        ffmpeg_location = self._ffmpeg_location()
        safe_playlist_title = re.sub(r'[<>:"/\\\\|?*]', "_", playlist_title).strip() or "YouTube Playlist"

        base_opts = {
            "format": "bestaudio/best",
            "ignoreerrors": True,
            "noplaylist": True,
            "download_archive": str(self.config.archive_file),
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
            "outtmpl": str(
                self.config.output_dir
                / safe_playlist_title
                / "%(title).180s [%(id)s].%(ext)s"
            ),
            "progress_hooks": [self._hook],
            "quiet": True,
            "no_warnings": False,
            "restrictfilenames": False,
        }

        if ffmpeg_location:
            base_opts["ffmpeg_location"] = ffmpeg_location

        url = f"https://www.youtube.com/watch?v={video_id}"

        last_error = None

        for cookie_label, cookie_opts in self._cookie_modes():
            opts = dict(base_opts)
            opts.update(cookie_opts)

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    result = ydl.extract_info(url, download=True)

                if result is not None:
                    return True

            except Exception as exc:
                last_error = exc
                self.progress(f"{cookie_label} failed for {video_id}: {exc}")

        self.progress(f"Skipped {video_id}. Last error: {last_error}")
        self._mark_failed(video_id)
        return False

    def _hook(self, status: dict) -> None:
        if status.get("status") == "downloading":
            filename = Path(status.get("filename", "")).name
            percent = status.get("_percent_str", "").strip()
            speed = status.get("_speed_str", "").strip()
            self.progress(f"Downloading {filename} {percent} {speed}".strip())

        elif status.get("status") == "finished":
            filename = Path(status.get("filename", "")).name
            self.progress(f"Downloaded {filename}. Converting/tagging...")

        elif status.get("status") == "error":
            self.progress("Download error. Continuing if possible...")

    def sync(self) -> dict:
        self._repair_archive_for_missing_files()

        playlist_title, entries = self._extract_playlist_entries_via_api()

        existing_ids = self._existing_download_ids()
        archive_ids = self._archive_ids()
        failed_ids = self._failed_ids()

        already_have = existing_ids | archive_ids

        to_download = [
            item for item in entries
            if item["id"] not in already_have and item["id"] not in failed_ids
        ]

        self.progress(f"Playlist title: {playlist_title}")
        self.progress(f"Already downloaded/archive IDs: {len(already_have)}")
        self.progress(f"Previously failed/unavailable IDs skipped: {len(failed_ids)}")
        self.progress(f"Missing/new playlist items to download now: {len(to_download)}")

        started_at = time.time()
        before_all = self._archive_ids()

        downloaded = 0
        skipped = 0

        for index, item in enumerate(to_download, start=1):
            video_id = item["id"]
            title = item.get("title") or video_id

            self.progress(f"[{index}/{len(to_download)}] {title}")

            ok = self._download_one(video_id, playlist_title)

            if ok:
                downloaded += 1
            else:
                skipped += 1

        after_all = self._archive_ids()
        actual_new = len(after_all - before_all)
        elapsed = round(time.time() - started_at, 1)

        result = {
            "playlist_title": playlist_title,
            "playlist_count": len(entries),
            "new_items": actual_new,
            "downloaded_attempts": downloaded,
            "skipped_items": skipped,
            "elapsed_seconds": elapsed,
            "output_dir": str(self.config.output_dir.resolve()),
            "archive_file": str(self.config.archive_file.resolve()),
        }

        self.progress(f"API sync complete. Total new downloads archived: {actual_new}. Skipped now: {skipped}.")
        return result
