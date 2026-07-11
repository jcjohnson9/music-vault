from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import requests

from music_vault.metadata.artist_images import validate_public_url
from music_vault.metadata.schema import normalize_release_date


MUSICBRAINZ_ENDPOINT = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_USER_AGENT = "MusicVault/1.0.0 (https://github.com/jcjohnson9/music-vault)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
LOW_CONFIDENCE_SCORE = 80


class MetadataProviderError(RuntimeError):
    """A concise provider failure safe to display without private details."""


@dataclass(frozen=True)
class MetadataCandidate:
    title: str
    artist: str
    album: str | None
    release_date: str | None
    recording_id: str | None
    release_id: str | None
    score: int
    duration_seconds: float | None = None
    album_artist: str | None = None
    country: str | None = None
    release_status: str | None = None
    artwork_available: bool | None = None
    provider: str = "MusicBrainz"
    provider_order: int = 0

    @property
    def year(self) -> str | None:
        return self.release_date[:4] if self.release_date else None

    @property
    def low_confidence(self) -> bool:
        return self.score < LOW_CONFIDENCE_SCORE


class _RateLimiter:
    def __init__(self, minimum_interval: float = 1.0) -> None:
        self.minimum_interval = minimum_interval
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self, cancel_event: threading.Event | None = None) -> None:
        with self._lock:
            remaining = self.minimum_interval - (time.monotonic() - self._last_request)
            while remaining > 0:
                if cancel_event is not None and cancel_event.wait(min(remaining, 0.05)):
                    raise MetadataProviderError("search_cancelled")
                if cancel_event is None:
                    time.sleep(min(remaining, 0.05))
                remaining = self.minimum_interval - (time.monotonic() - self._last_request)
            self._last_request = time.monotonic()


_RATE_LIMITER = _RateLimiter()


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _artist_credit(recording: Mapping[str, Any], fallback: str | None) -> str:
    parts: list[str] = []
    for entry in recording.get("artist-credit", []) or []:
        if isinstance(entry, str):
            parts.append(entry)
            continue
        if not isinstance(entry, Mapping):
            continue
        artist = entry.get("artist")
        name = entry.get("name")
        if not name and isinstance(artist, Mapping):
            name = artist.get("name")
        if name:
            parts.append(str(name))
        join = entry.get("joinphrase")
        if join:
            parts.append(str(join))
    return "".join(parts).strip() or str(fallback or "").strip()


def _release_date(value: object) -> str | None:
    try:
        return normalize_release_date(value)
    except ValueError:
        return None


def _duration_seconds(value: object) -> float | None:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return round(milliseconds / 1000.0, 3)


class MusicBrainzProvider:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        rate_limiter: _RateLimiter = _RATE_LIMITER,
    ) -> None:
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.resolver = resolver
        self.rate_limiter = rate_limiter

    @staticmethod
    def _query(title: str, artist: str | None) -> str:
        def quoted(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"')

        query = f'recording:"{quoted(title)}"'
        if artist:
            query += f' AND artist:"{quoted(artist)}"'
        return query

    def _payload(
        self,
        title: str,
        artist: str | None,
        cancel_event: threading.Event | None,
    ) -> Mapping[str, Any]:
        if cancel_event is not None and cancel_event.is_set():
            raise MetadataProviderError("search_cancelled")
        self.rate_limiter.wait(cancel_event)
        endpoint = validate_public_url(
            MUSICBRAINZ_ENDPOINT,
            allowed_hosts=frozenset({"musicbrainz.org"}),
            resolver=self.resolver,
        )
        try:
            response = self.session.get(
                endpoint,
                params={"query": self._query(title, artist), "fmt": "json", "limit": 10},
                headers={
                    "User-Agent": MUSICBRAINZ_USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=(5, 15),
                allow_redirects=False,
                stream=True,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise MetadataProviderError("musicbrainz_unavailable") from exc
        except requests.RequestException as exc:
            raise MetadataProviderError("musicbrainz_request_failed") from exc
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                raise MetadataProviderError("musicbrainz_redirect_rejected")
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                raise MetadataProviderError("musicbrainz_unavailable")
            if not 200 <= response.status_code <= 299:
                raise MetadataProviderError("musicbrainz_request_rejected")
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
            if mime != "application/json" and not mime.endswith("+json"):
                raise MetadataProviderError("musicbrainz_response_rejected")
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise MetadataProviderError("musicbrainz_response_too_large")
                except ValueError as exc:
                    raise MetadataProviderError("musicbrainz_response_rejected") from exc
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise MetadataProviderError("musicbrainz_response_too_large")
            except MetadataProviderError:
                raise
            except requests.RequestException as exc:
                raise MetadataProviderError("musicbrainz_request_failed") from exc
            try:
                payload = json.loads(bytes(body).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise MetadataProviderError("musicbrainz_response_invalid") from exc
        finally:
            response.close()
        if not isinstance(payload, Mapping):
            raise MetadataProviderError("musicbrainz_response_invalid")
        return payload

    def search(
        self,
        title: str,
        artist: str | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> list[MetadataCandidate]:
        clean_title = str(title or "").strip()
        clean_artist = _clean(artist)
        if not clean_title:
            raise ValueError("A title is required for MusicBrainz search.")
        payload = self._payload(clean_title, clean_artist, cancel_event)
        candidates: list[MetadataCandidate] = []
        provider_order = 0
        for recording in payload.get("recordings", []) or []:
            if not isinstance(recording, Mapping):
                continue
            try:
                score = max(0, min(100, int(recording.get("score", 0) or 0)))
            except (TypeError, ValueError, OverflowError) as exc:
                raise MetadataProviderError("musicbrainz_response_invalid") from exc
            recording_id = _clean(recording.get("id"))
            recording_title = _clean(recording.get("title")) or clean_title
            artist_credit = _artist_credit(recording, clean_artist)
            duration_seconds = _duration_seconds(recording.get("length"))
            releases = recording.get("releases", []) or []
            if not releases:
                releases = [None]
            for release in releases:
                release_map = release if isinstance(release, Mapping) else {}
                cover_info = release_map.get("cover-art-archive")
                artwork_available = None
                if isinstance(cover_info, Mapping):
                    artwork_available = bool(cover_info.get("front"))
                candidates.append(
                    MetadataCandidate(
                        title=recording_title,
                        artist=artist_credit,
                        album=_clean(release_map.get("title")),
                        release_date=_release_date(release_map.get("date")),
                        recording_id=recording_id,
                        release_id=_clean(release_map.get("id")),
                        score=score,
                        duration_seconds=duration_seconds,
                        album_artist=_artist_credit(release_map, artist_credit),
                        country=_clean(release_map.get("country")),
                        release_status=_clean(release_map.get("status")),
                        artwork_available=artwork_available,
                        provider_order=provider_order,
                    )
                )
                provider_order += 1
        return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.provider_order))


def configure_musicbrainz() -> None:
    """Compatibility no-op; provider configuration is explicit and local."""


def search_recording(title: str, artist: str | None = None) -> list[MetadataCandidate]:
    return MusicBrainzProvider().search(title, artist)
