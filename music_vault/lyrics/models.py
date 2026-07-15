"""Immutable, provider-neutral lyric models.

Lyric payload fields are deliberately excluded from dataclass ``repr`` output so
routine diagnostics cannot accidentally disclose copyrighted lyric text.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class LyricsSource(str, Enum):
    MANUAL = "manual"
    SIDECAR_SYNCED = "sidecar_synced"
    EMBEDDED_SYNCED = "embedded_synced"
    CACHE_SYNCED = "cache_synced"
    SIDECAR_PLAIN = "sidecar_plain"
    EMBEDDED_PLAIN = "embedded_plain"
    CACHE_PLAIN = "cache_plain"
    PROVIDER = "provider"


class LyricsStatus(str, Enum):
    AVAILABLE = "available"
    INSTRUMENTAL = "instrumental"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    TEMPORARY_ERROR = "temporary_error"
    DISABLED = "disabled"


class LookupState(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"


def normalize_metadata(value: object) -> str:
    """Normalize metadata conservatively for cache identity and strict matching."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class TrackLyricsIdentity:
    track_id: str | int
    title: str
    artist: str
    album: str = ""
    duration_ms: int | None = None
    media_path: Path | str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", " ".join(str(self.title or "").split()))
        object.__setattr__(self, "artist", " ".join(str(self.artist or "").split()))
        object.__setattr__(self, "album", " ".join(str(self.album or "").split()))
        try:
            duration = int(self.duration_ms) if self.duration_ms is not None else None
        except (TypeError, ValueError, OverflowError):
            duration = None
        object.__setattr__(self, "duration_ms", duration if duration and duration > 0 else None)
        if self.media_path is not None:
            object.__setattr__(self, "media_path", Path(self.media_path))

    @property
    def stable_id(self) -> str:
        return str(self.track_id)

    @property
    def metadata_fingerprint(self) -> str:
        # Whole seconds deliberately ignore insignificant decoder-duration jitter.
        payload = {
            "artist": normalize_metadata(self.artist),
            "duration_seconds": round((self.duration_ms or 0) / 1000),
            "title": normalize_metadata(self.title),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


LyricsTrack = TrackLyricsIdentity


@dataclass(frozen=True, order=True)
class LyricLine:
    timestamp_ms: int
    text: str = field(compare=True, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_ms", max(0, int(self.timestamp_ms)))
        object.__setattr__(self, "text", str(self.text).replace("\x00", ""))


@dataclass(frozen=True)
class ParsedLyrics:
    lines: tuple[LyricLine, ...] = field(default_factory=tuple, repr=False)
    plain_text: str | None = field(default=None, repr=False)
    offset_ms: int = 0

    @property
    def synchronized(self) -> bool:
        return bool(self.lines)

    @property
    def empty(self) -> bool:
        return not self.lines and not self.plain_text


@dataclass(frozen=True)
class LyricsResult:
    status: LyricsStatus
    identity: TrackLyricsIdentity
    source: LyricsSource | None = None
    synced_lines: tuple[LyricLine, ...] = field(default_factory=tuple, repr=False)
    plain_text: str | None = field(default=None, repr=False)
    provider: str | None = None
    provider_result_id: str | None = None
    provider_duration_ms: int | None = None
    attribution: str | None = None
    confidence: float | None = None
    fetched_at: str | None = None
    retry_after: str | None = None
    error_code: str | None = None
    from_cache: bool = False

    @property
    def available(self) -> bool:
        return self.status in {LyricsStatus.AVAILABLE, LyricsStatus.INSTRUMENTAL}

    @property
    def synchronized(self) -> bool:
        return self.status is LyricsStatus.AVAILABLE and bool(self.synced_lines)

    @property
    def instrumental(self) -> bool:
        return self.status is LyricsStatus.INSTRUMENTAL


@dataclass(frozen=True)
class LyricsQuery:
    identity: TrackLyricsIdentity

    @property
    def title(self) -> str:
        return self.identity.title

    @property
    def artist(self) -> str:
        return self.identity.artist

    @property
    def album(self) -> str:
        return self.identity.album

    @property
    def duration_ms(self) -> int | None:
        return self.identity.duration_ms


@dataclass(frozen=True)
class ProviderMatch:
    result_id: str | None
    title: str
    artist: str
    album: str
    duration_ms: int | None
    instrumental: bool
    synced_text: str | None = field(default=None, repr=False)
    plain_text: str | None = field(default=None, repr=False)
    score: float = 0.0


@dataclass(frozen=True)
class CacheRecord:
    track_id: str
    metadata_fingerprint: str
    status: LyricsStatus
    source: LyricsSource | None
    provider: str | None
    provider_result_id: str | None
    content_hash: str | None
    fetched_at: str
    retry_after: str | None = None


_SAFE_ERROR_RE = re.compile(r"[^a-z0-9_.-]+")


def safe_error_code(value: object, fallback: str = "lyrics_error") -> str:
    text = str(value or "").strip().casefold()
    if not text or len(text) > 64 or _SAFE_ERROR_RE.search(text):
        return fallback
    return text


def result_from_mapping(identity: TrackLyricsIdentity, payload: Mapping[str, Any]) -> LyricsResult:
    """Narrow helper used by cache validation; never accepts arbitrary text logs."""
    try:
        status = LyricsStatus(str(payload["status"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("invalid lyrics result status") from exc
    source_value = payload.get("source")
    try:
        source = LyricsSource(str(source_value)) if source_value else None
    except ValueError as exc:
        raise ValueError("invalid lyrics result source") from exc
    return LyricsResult(status=status, identity=identity, source=source)
