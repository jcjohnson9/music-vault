"""Bounded Discogs catalogue provider for Music Vault metadata intelligence.

Only a user-supplied personal token is accepted.  It is sent in the
``Authorization`` header exclusively to ``https://api.discogs.com``.  Raw
responses may live briefly in this provider's in-memory cache, but are never
written to disk or included in normalized candidates.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import requests

from music_vault.metadata.artist_images import validate_public_url
from music_vault.metadata.matching import text_similarity
from music_vault.metadata.schema import normalize_release_date
from music_vault.metadata.title_parser import classify_version_hint
from music_vault.metadata.ensemble import versions_compatible
from music_vault.version import APP_VERSION, PROJECT_URL

from . import (
    ProviderArtistCredit,
    ProviderArtworkCandidate,
    ProviderQuery,
    ProviderReleaseCandidate,
)


DISCOGS_API_ROOT = "https://api.discogs.com"
DISCOGS_API_HOSTS = frozenset({"api.discogs.com"})
DISCOGS_USER_AGENT = f"MusicVault/{APP_VERSION} +{PROJECT_URL}"
DISCOGS_GENERAL_NOTICE = (
    "This application uses Discogs\u2019 API but is not affiliated with, sponsored "
    "or endorsed by Discogs. \u201cDiscogs\u201d is a trademark of Zink Media, LLC."
)
DISCOGS_ATTRIBUTION_TEXT = "Data provided by Discogs"

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 3
MAX_PAGE_SIZE = 50
MAX_SEARCH_RESULTS = 50
MAX_RELEASE_CANDIDATES = 10
MAX_RETRIES = 2
MAX_BACKOFF_SECONDS = 60.0
RAW_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60
RAW_CACHE_MAX_ENTRIES = 128
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 20

_ALLOWED_PATH_RE = re.compile(
    r"/(?:database/search|oauth/identity|releases/[1-9]\d*|masters/[1-9]\d*|artists/[1-9]\d*)"
)
_DISCOGS_NAME_SUFFIX_RE = re.compile(r"\s+\(\d+\)\s*$")
_COMPILATION_MARKERS = frozenset({"compilation", "sampler", "greatest hits"})
_UNOFFICIAL_MARKERS = frozenset({"unofficial release", "bootleg", "unofficial"})


class DiscogsProviderError(RuntimeError):
    """A deliberately sanitized provider failure suitable for UI display."""


def _clean(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def _bounded_id(value: object, kind: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9]\d{0,17}", text):
        raise ValueError(f"A valid Discogs {kind} ID is required.")
    return text


def _safe_date(value: object) -> str | None:
    text = _clean(value)
    if not text:
        return None
    # Discogs occasionally represents unknown components as zero.  Preserve
    # only the known precision rather than inventing a month or day.
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
    if not match:
        return None
    year, month, day = match.groups()
    if month == "00":
        month = None
        day = None
    if day == "00":
        day = None
    normalized = year + (f"-{month}" if month else "") + (f"-{day}" if day else "")
    try:
        return normalize_release_date(normalized)
    except ValueError:
        return None


def _duration_seconds(value: object) -> float | None:
    text = _clean(value)
    if not text:
        return None
    parts = text.split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    seconds = numbers[-1] + numbers[-2] * 60
    if len(numbers) == 3:
        seconds += numbers[0] * 3600
    return float(seconds) if seconds > 0 else None


def _entry_name(entry: Mapping[str, Any]) -> str | None:
    name = _clean(entry.get("anv")) or _clean(entry.get("name"))
    return _DISCOGS_NAME_SUFFIX_RE.sub("", name).strip() if name else None


def parse_discogs_artist_credits(
    value: object,
) -> tuple[ProviderArtistCredit, ...]:
    """Normalize provider-structured credits without punctuation guessing."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    credits: list[ProviderArtistCredit] = []
    next_role = "primary"
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        name = _entry_name(raw)
        if not name:
            continue
        artist_id = _clean(raw.get("id"))
        join_phrase = str(raw.get("join") or "")
        role_text = _clean(raw.get("role")) or ""
        role = next_role
        if re.search(r"\bremix", role_text, re.IGNORECASE):
            role = "remixer"
        elif re.search(r"\bperform", role_text, re.IGNORECASE):
            role = "performer"
        reference = (
            f"https://www.discogs.com/artist/{artist_id}" if artist_id else None
        )
        credits.append(
            ProviderArtistCredit(
                name=name,
                role=role,
                artist_id=artist_id,
                join_phrase=join_phrase,
                entity_type="unknown",
                provider_reference=reference,
            )
        )
        if re.search(r"\b(?:feat\.?|ft\.?|featuring)\b", join_phrase, re.IGNORECASE):
            next_role = "featured"
        elif re.search(r"(?:&|\bwith\b|\band\b|\bx\b)", join_phrase, re.IGNORECASE):
            next_role = "collaborator"
        else:
            next_role = "primary"
    return tuple(credits)


def format_artist_credits(credits: Sequence[ProviderArtistCredit]) -> str:
    return "".join(credit.name + credit.join_phrase for credit in credits).strip()


def _format_descriptions(payload: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    formats = payload.get("formats", ())
    if not isinstance(formats, Sequence) or isinstance(formats, (str, bytes, bytearray)):
        return ()
    for raw in formats:
        if not isinstance(raw, Mapping):
            continue
        name = _clean(raw.get("name"))
        if name:
            values.append(name)
        descriptions = raw.get("descriptions", ())
        if isinstance(descriptions, Sequence) and not isinstance(
            descriptions, (str, bytes, bytearray)
        ):
            values.extend(text for item in descriptions if (text := _clean(item)))
    return tuple(values)


def _release_label(payload: Mapping[str, Any]) -> str | None:
    labels = payload.get("labels", ())
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes, bytearray)):
        return None
    for item in labels:
        if isinstance(item, Mapping):
            name = _clean(item.get("name"))
            if name:
                return name
    return None


def _front_artwork(
    payload: Mapping[str, Any], release_id: str, provider_page_url: str
) -> ProviderArtworkCandidate | None:
    images = payload.get("images", ())
    if not isinstance(images, Sequence) or isinstance(images, (str, bytes, bytearray)):
        return None
    ordered = sorted(
        (item for item in images if isinstance(item, Mapping)),
        key=lambda item: 0 if str(item.get("type", "")).casefold() == "primary" else 1,
    )
    for item in ordered:
        if str(item.get("type", "")).casefold() != "primary":
            continue
        source_url = _clean(item.get("uri"))
        if not source_url:
            continue
        try:
            width = int(item.get("width")) if item.get("width") is not None else None
            height = int(item.get("height")) if item.get("height") is not None else None
        except (TypeError, ValueError, OverflowError):
            width = height = None
        return ProviderArtworkCandidate(
            source_url=source_url,
            provider_page_url=provider_page_url,
            release_id=release_id,
            image_type="front",
            width=width if width and width > 0 else None,
            height=height if height and height > 0 else None,
            catalogue_image=True,
        )
    return None


def _track_score(
    query: ProviderQuery,
    track: Mapping[str, Any],
    release_credits: tuple[ProviderArtistCredit, ...],
) -> tuple[float, tuple[str, ...], tuple[ProviderArtistCredit, ...]]:
    title = _clean(track.get("title")) or ""
    credits = parse_discogs_artist_credits(track.get("artists")) or release_credits
    artist = format_artist_credits(credits)
    title_score = text_similarity(query.title, title, title=True)
    artist_score = text_similarity(query.artist, artist) if query.artist else 100.0
    duration = _duration_seconds(track.get("duration"))
    if query.duration_seconds is None or duration is None:
        duration_score = 72.0
        duration_delta = None
    else:
        duration_delta = abs(float(query.duration_seconds) - duration)
        duration_score = max(0.0, 100.0 - duration_delta * 5.0)
    candidate_version, _label = classify_version_hint(title)
    version_ok = versions_compatible(query.version_type or "unknown", candidate_version)

    score = title_score * 0.55 + artist_score * 0.35 + duration_score * 0.10
    reasons: list[str] = []
    if title_score == 100.0:
        reasons.append("exact_tracklist_title")
    elif title_score < 80.0:
        reasons.append("track_title_mismatch")
    if query.artist and artist_score < 75.0:
        score = min(score, 55.0)
        reasons.append("artist_mismatch")
    elif query.artist and artist_score == 100.0:
        reasons.append("exact_artist_credit")
    if duration_delta is not None:
        if duration_delta <= 5.0:
            reasons.append("duration_plausible")
        elif duration_delta > 15.0:
            score -= min(20.0, duration_delta / 2.0)
            reasons.append("duration_mismatch")
    if not version_ok:
        score = min(score, 59.0)
        reasons.append("version_conflict")
    return max(0.0, min(100.0, score)), tuple(reasons), credits


def parse_discogs_release(
    payload: Mapping[str, Any],
    query: ProviderQuery,
    *,
    master_payload: Mapping[str, Any] | None = None,
    provider_order: int = 0,
) -> ProviderReleaseCandidate | None:
    """Reduce one release response to its best matching normalized track."""

    release_id = _clean(payload.get("id"))
    if not release_id or not release_id.isdigit():
        return None
    release_credits = parse_discogs_artist_credits(payload.get("artists"))
    release_artist = format_artist_credits(release_credits)
    tracklist = payload.get("tracklist", ())
    if not isinstance(tracklist, Sequence) or isinstance(tracklist, (str, bytes, bytearray)):
        return None

    best_track: Mapping[str, Any] | None = None
    best_credits: tuple[ProviderArtistCredit, ...] = ()
    best_score = -1.0
    best_reasons: tuple[str, ...] = ()
    best_index = 0
    for index, item in enumerate(tracklist):
        if not isinstance(item, Mapping):
            continue
        score, reasons, credits = _track_score(query, item, release_credits)
        if score > best_score:
            best_track = item
            best_credits = credits
            best_score = score
            best_reasons = reasons
            best_index = index
    if best_track is None:
        return None

    title = _clean(best_track.get("title")) or query.title
    artist = format_artist_credits(best_credits) or release_artist or (query.artist or "")
    format_values = _format_descriptions(payload)
    format_text = ", ".join(format_values)
    version_type, version_label = classify_version_hint(title, format_text)
    lowered_formats = {value.casefold() for value in format_values}
    is_compilation = bool(lowered_formats & _COMPILATION_MARKERS)
    is_official = not bool(lowered_formats & _UNOFFICIAL_MARKERS)
    if version_type == "unknown" and is_official:
        version_type = "studio"

    release_date = _safe_date(payload.get("released")) or _safe_date(payload.get("year"))
    master_id = _clean(payload.get("master_id"))
    original_date = None
    if master_payload:
        original_date = _safe_date(master_payload.get("year"))
    if original_date is None:
        original_date = _safe_date(payload.get("master_year"))
    if version_type == "studio" and original_date:
        # A late reissue must not become the canonical original studio year.
        release_date = original_date
    if version_type == "live" and not is_official:
        release_date = None

    reasons = list(best_reasons)
    if is_compilation:
        reasons.extend(("compilation_release", "release_ambiguous"))
    if not is_official:
        reasons.append("unofficial_release")
    album = _clean(payload.get("title"))
    if is_compilation or (version_type == "live" and not is_official):
        album = None

    provider_page_url = f"https://www.discogs.com/release/{release_id}"
    score = best_score
    if is_compilation:
        score = max(0.0, score - 12.0)
    if not is_official:
        score = max(0.0, score - 8.0)
    album_score: float | None = None
    if query.album and album:
        album_score = text_similarity(query.album, album, title=True)
        if album_score >= 95.0:
            score = min(100.0, score + 4.0)
            reasons.append("album_context_match")
        elif album_score < 70.0:
            score = max(0.0, score - 10.0)
            reasons.append("album_context_mismatch")
    if "exact_tracklist_title" in reasons and "exact_artist_credit" in reasons:
        score = min(100.0, score + 4.0)
    if "version_conflict" in reasons:
        # Later release-context bonuses must never lift a conflicting version
        # back into automatic-application territory.
        score = min(score, 59.0)
    duration = _duration_seconds(best_track.get("duration"))
    field_scores = {
        "title": min(100.0, score + 2.0),
        "artist": min(100.0, score + 2.0),
        "artist_credits": min(100.0, score + 2.0),
        "album": (
            min(score, album_score)
            if album and album_score is not None
            else score if album else 0.0
        ),
        "album_artist": score,
        "release_date": score if release_date else 0.0,
        "original_release_date": score if original_date else 0.0,
        "version_type": score,
        "version_label": score,
        "discogs_release_id": score,
        "discogs_master_id": score,
        "discogs_track_position": score,
        "artwork": score,
    }
    return ProviderReleaseCandidate(
        provider="Discogs",
        title=title,
        artist=artist,
        artist_credits=best_credits,
        album=album,
        album_artist=release_artist or artist,
        release_date=release_date,
        original_release_date=original_date,
        version_type=version_type,
        version_label=version_label,
        duration_seconds=duration,
        provider_score=round(score, 3),
        release_id=release_id,
        master_id=master_id,
        track_position=_clean(best_track.get("position")) or str(best_index + 1),
        label=_release_label(payload),
        country=_clean(payload.get("country")),
        release_format=format_text or None,
        provider_reference=provider_page_url,
        artwork=_front_artwork(payload, release_id, provider_page_url),
        is_compilation=is_compilation,
        is_official=is_official,
        provider_order=provider_order,
        reasons=tuple(dict.fromkeys(reasons)),
        field_scores=field_scores,
    )


def rank_discogs_candidates(
    candidates: Sequence[ProviderReleaseCandidate],
) -> list[ProviderReleaseCandidate]:
    """Prefer a coherent original context without blindly choosing a reissue."""

    if not candidates:
        return []

    def year(candidate: ProviderReleaseCandidate) -> int:
        value = candidate.original_release_date or candidate.release_date
        try:
            return int(str(value)[:4])
        except (TypeError, ValueError):
            return 9999

    ranked = sorted(
        candidates,
        key=lambda item: (
            -item.provider_score,
            item.is_compilation,
            not item.is_official,
            year(item),
            item.provider_order,
        ),
    )
    leader = ranked[0]
    coherent = [
        item
        for item in ranked
        if item.provider_score >= leader.provider_score - 4.0
        and not item.is_compilation
        and item.is_official
    ]
    if coherent:
        earliest = min(coherent, key=lambda item: (year(item), item.provider_order))
        ranked.remove(earliest)
        ranked.insert(0, earliest)

    if len(ranked) > 1:
        first, second = ranked[:2]
        identities_differ = (
            first.release_id != second.release_id
            and (first.album or "").casefold() != (second.album or "").casefold()
        )
        if identities_differ and abs(first.provider_score - second.provider_score) < 3.0:
            ranked[0] = replace(
                first,
                album=None,
                reasons=tuple(dict.fromkeys(first.reasons + ("release_ambiguous",))),
                field_scores={**first.field_scores, "album": 0.0},
            )
    return ranked


class _MemoryResponseCache:
    """Bounded six-hour in-process duplicate suppression; never persisted."""

    def __init__(
        self,
        max_age_seconds: float = RAW_CACHE_MAX_AGE_SECONDS,
        max_entries: int = RAW_CACHE_MAX_ENTRIES,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_age_seconds = min(float(max_age_seconds), RAW_CACHE_MAX_AGE_SECONDS)
        self.max_entries = max(1, min(int(max_entries), RAW_CACHE_MAX_ENTRIES))
        self.clock = clock
        self._values: OrderedDict[tuple[Any, ...], tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[Any, ...]) -> Mapping[str, Any] | None:
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            created, encoded = item
            if self.clock() - created > self.max_age_seconds:
                self._values.pop(key, None)
                return None
            self._values.move_to_end(key)
        decoded = json.loads(encoded.decode("utf-8"))
        return decoded if isinstance(decoded, Mapping) else None

    def put(self, key: tuple[Any, ...], payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            return
        with self._lock:
            self._values[key] = (self.clock(), encoded)
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)


class DiscogsRateLimiter:
    """Sequential pacing with bounded cancellation-aware backoff."""

    def __init__(
        self,
        minimum_interval: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.minimum_interval = max(0.0, float(minimum_interval))
        self.clock = clock
        self.sleeper = sleeper
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.remaining: int | None = None
        self.limit: int | None = None

    def wait(self, cancel_event: threading.Event | None = None) -> None:
        with self._lock:
            remaining = self.minimum_interval - (self.clock() - self._last_request)
            while remaining > 0:
                step = min(remaining, 0.1)
                if cancel_event is not None:
                    if cancel_event.wait(step):
                        raise DiscogsProviderError("discogs_search_cancelled")
                else:
                    self.sleeper(step)
                remaining = self.minimum_interval - (self.clock() - self._last_request)
            self._last_request = self.clock()

    def observe(self, headers: Mapping[str, Any]) -> None:
        try:
            self.limit = int(headers.get("X-Discogs-Ratelimit"))
        except (TypeError, ValueError):
            pass
        try:
            self.remaining = int(headers.get("X-Discogs-Ratelimit-Remaining"))
        except (TypeError, ValueError):
            pass

    def backoff(self, seconds: float, cancel_event: threading.Event | None = None) -> None:
        remaining = max(0.0, min(float(seconds), MAX_BACKOFF_SECONDS))
        while remaining > 0:
            step = min(remaining, 0.1)
            if cancel_event is not None:
                if cancel_event.wait(step):
                    raise DiscogsProviderError("discogs_search_cancelled")
            else:
                self.sleeper(step)
            remaining -= step


_DEFAULT_RATE_LIMITER = DiscogsRateLimiter()


class DiscogsProvider:
    """Authenticated Discogs provider with an exact network boundary."""

    def __init__(
        self,
        token: object,
        session: requests.Session | None = None,
        *,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        rate_limiter: DiscogsRateLimiter = _DEFAULT_RATE_LIMITER,
        cache: _MemoryResponseCache | None = None,
    ) -> None:
        self._token = str(token or "").strip()
        if not self._token:
            raise DiscogsProviderError("discogs_token_required")
        if len(self._token) > 512 or "\r" in self._token or "\n" in self._token:
            raise DiscogsProviderError("discogs_token_invalid")
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.resolver = resolver
        self.rate_limiter = rate_limiter
        self.cache = cache or _MemoryResponseCache()

    @staticmethod
    def _cache_key(path: str, params: Mapping[str, Any] | None) -> tuple[Any, ...]:
        return (path, tuple(sorted((str(key), str(value)) for key, value in (params or {}).items())))

    @staticmethod
    def _check_cancelled(
        cancel_event: threading.Event | None,
        stale_check: Callable[[], bool] | None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DiscogsProviderError("discogs_search_cancelled")
        if stale_check is not None and stale_check():
            raise DiscogsProviderError("discogs_result_stale")

    @staticmethod
    def _read_limited(response: Any) -> bytes:
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_RESPONSE_BYTES:
                    raise DiscogsProviderError("discogs_response_too_large")
            except ValueError as exc:
                raise DiscogsProviderError("discogs_response_rejected") from exc
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise DiscogsProviderError("discogs_response_too_large")
        except DiscogsProviderError:
            raise
        except requests.RequestException as exc:
            raise DiscogsProviderError("discogs_request_failed") from exc
        return bytes(body)

    def _request_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        stale_check: Callable[[], bool] | None = None,
        use_cache: bool = True,
    ) -> Mapping[str, Any]:
        if not _ALLOWED_PATH_RE.fullmatch(path):
            raise DiscogsProviderError("discogs_endpoint_rejected")
        bounded_params: dict[str, Any] = {}
        for key, value in (params or {}).items():
            text = str(value)
            if len(str(key)) > 32 or len(text) > 300:
                raise DiscogsProviderError("discogs_query_rejected")
            bounded_params[str(key)] = value
        key = self._cache_key(path, bounded_params)
        if use_cache and (cached := self.cache.get(key)) is not None:
            self._check_cancelled(cancel_event, stale_check)
            return cached

        self._check_cancelled(cancel_event, stale_check)
        try:
            endpoint = validate_public_url(
                f"{DISCOGS_API_ROOT}{path}",
                allowed_hosts=DISCOGS_API_HOSTS,
                resolver=self.resolver,
            )
        except Exception as exc:
            raise DiscogsProviderError("discogs_endpoint_unavailable") from exc

        for attempt in range(MAX_RETRIES + 1):
            self._check_cancelled(cancel_event, stale_check)
            self.rate_limiter.wait(cancel_event)
            try:
                response = self.session.get(
                    endpoint,
                    params=bounded_params or None,
                    headers={
                        "User-Agent": DISCOGS_USER_AGENT,
                        "Accept": "application/vnd.discogs.v2.discogs+json",
                        "Authorization": f"Discogs token={self._token}",
                    },
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    allow_redirects=False,
                    stream=True,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise DiscogsProviderError("discogs_unavailable") from exc
            except requests.RequestException as exc:
                raise DiscogsProviderError("discogs_request_failed") from exc
            try:
                self.rate_limiter.observe(response.headers)
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise DiscogsProviderError("discogs_redirect_rejected")
                if response.status_code == 429:
                    if attempt >= MAX_RETRIES:
                        raise DiscogsProviderError("discogs_rate_limited")
                    try:
                        retry_after = float(response.headers.get("Retry-After", 1.0))
                    except (TypeError, ValueError):
                        retry_after = 1.0
                    retry = retry_after
                elif 500 <= response.status_code <= 599:
                    if attempt >= MAX_RETRIES:
                        raise DiscogsProviderError("discogs_unavailable")
                    retry = min(2.0**attempt, MAX_BACKOFF_SECONDS)
                else:
                    retry = None
                if retry is not None:
                    continue_retry = retry
                elif response.status_code in {401, 403}:
                    raise DiscogsProviderError("discogs_auth_rejected")
                elif response.status_code == 404:
                    raise DiscogsProviderError("discogs_not_found")
                elif not 200 <= response.status_code <= 299:
                    raise DiscogsProviderError("discogs_request_rejected")
                else:
                    mime = str(response.headers.get("Content-Type", "")).split(";", 1)[0].casefold()
                    if mime != "application/json" and not mime.endswith("+json"):
                        raise DiscogsProviderError("discogs_response_rejected")
                    body = self._read_limited(response)
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise DiscogsProviderError("discogs_response_invalid") from exc
                    if not isinstance(payload, Mapping):
                        raise DiscogsProviderError("discogs_response_invalid")
                    self._check_cancelled(cancel_event, stale_check)
                    if use_cache:
                        self.cache.put(key, payload)
                    return payload
            finally:
                response.close()
            self.rate_limiter.backoff(continue_retry, cancel_event)
        raise DiscogsProviderError("discogs_unavailable")

    def test_connection(
        self, *, cancel_event: threading.Event | None = None
    ) -> bool:
        payload = self._request_json(
            "/oauth/identity", cancel_event=cancel_event, use_cache=False
        )
        return bool(_clean(payload.get("username")) or _clean(payload.get("id")))

    def get_release(
        self,
        release_id: object,
        *,
        cancel_event: threading.Event | None = None,
        stale_check: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        identity = _bounded_id(release_id, "release")
        return self._request_json(
            f"/releases/{identity}", cancel_event=cancel_event, stale_check=stale_check
        )

    def get_master(
        self,
        master_id: object,
        *,
        cancel_event: threading.Event | None = None,
        stale_check: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        identity = _bounded_id(master_id, "master")
        return self._request_json(
            f"/masters/{identity}", cancel_event=cancel_event, stale_check=stale_check
        )

    def get_artist(
        self,
        artist_id: object,
        *,
        cancel_event: threading.Event | None = None,
        stale_check: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        identity = _bounded_id(artist_id, "artist")
        return self._request_json(
            f"/artists/{identity}", cancel_event=cancel_event, stale_check=stale_check
        )

    def search_catalogue(
        self,
        query: ProviderQuery,
        *,
        search_type: str = "release",
        max_pages: int = 1,
        max_results: int = MAX_SEARCH_RESULTS,
        cancel_event: threading.Event | None = None,
        stale_check: Callable[[], bool] | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        title = _clean(query.title)
        if not title:
            raise ValueError("A title is required for Discogs search.")
        if search_type not in {"release", "master", "artist"}:
            raise ValueError("Unsupported Discogs search type.")
        pages = max(1, min(int(max_pages), MAX_PAGES))
        limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
        results: list[Mapping[str, Any]] = []
        for page in range(1, pages + 1):
            params: dict[str, Any] = {
                "type": search_type,
                "page": page,
                "per_page": min(MAX_PAGE_SIZE, limit - len(results)),
            }
            if search_type == "artist":
                params["artist"] = _clean(query.artist) or title
            else:
                params["track"] = title
                if query.artist:
                    params["artist"] = _clean(query.artist)
                if query.album:
                    params["release_title"] = _clean(query.album)
            payload = self._request_json(
                "/database/search",
                params=params,
                cancel_event=cancel_event,
                stale_check=stale_check,
            )
            raw_results = payload.get("results", ())
            if not isinstance(raw_results, Sequence) or isinstance(
                raw_results, (str, bytes, bytearray)
            ):
                raise DiscogsProviderError("discogs_response_invalid")
            for item in raw_results:
                if not isinstance(item, Mapping):
                    raise DiscogsProviderError("discogs_response_invalid")
                results.append(dict(item))
                if len(results) >= limit:
                    break
            pagination = payload.get("pagination", {})
            if not isinstance(pagination, Mapping):
                raise DiscogsProviderError("discogs_response_invalid")
            try:
                total_pages = int(pagination.get("pages", page))
            except (TypeError, ValueError, OverflowError) as exc:
                raise DiscogsProviderError("discogs_response_invalid") from exc
            if len(results) >= limit or page >= total_pages or not raw_results:
                break
        return tuple(results)

    def search_releases(
        self,
        query: ProviderQuery,
        *,
        max_pages: int = 1,
        max_candidates: int = 6,
        cancel_event: threading.Event | None = None,
        stale_check: Callable[[], bool] | None = None,
    ) -> list[ProviderReleaseCandidate]:
        maximum = max(1, min(int(max_candidates), MAX_RELEASE_CANDIDATES))
        results = self.search_catalogue(
            query,
            search_type="release",
            max_pages=max_pages,
            max_results=maximum,
            cancel_event=cancel_event,
            stale_check=stale_check,
        )
        payloads: list[Mapping[str, Any]] = []
        candidates: list[ProviderReleaseCandidate] = []
        for index, result in enumerate(results):
            identity = result.get("id")
            try:
                payload = self.get_release(
                    identity, cancel_event=cancel_event, stale_check=stale_check
                )
            except ValueError:
                continue
            payloads.append(payload)
            candidate = parse_discogs_release(payload, query, provider_order=index)
            if candidate is not None:
                candidates.append(candidate)
        ranked = rank_discogs_candidates(candidates)
        if ranked and ranked[0].master_id:
            try:
                master = self.get_master(
                    ranked[0].master_id,
                    cancel_event=cancel_event,
                    stale_check=stale_check,
                )
            except DiscogsProviderError as exc:
                if str(exc) not in {"discogs_not_found", "discogs_unavailable"}:
                    raise
            else:
                release_payload = next(
                    (
                        item
                        for item in payloads
                        if _clean(item.get("id")) == ranked[0].release_id
                    ),
                    None,
                )
                if release_payload is not None:
                    enriched = parse_discogs_release(
                        release_payload,
                        query,
                        master_payload=master,
                        provider_order=ranked[0].provider_order,
                    )
                    if enriched is not None:
                        ranked[0] = enriched
                        ranked = rank_discogs_candidates(ranked)
        return ranked

    def search_masters(self, query: ProviderQuery, **kwargs: Any) -> tuple[Mapping[str, Any], ...]:
        return self.search_catalogue(query, search_type="master", **kwargs)

    def search_artists(self, query: ProviderQuery, **kwargs: Any) -> tuple[Mapping[str, Any], ...]:
        return self.search_catalogue(query, search_type="artist", **kwargs)

    search = search_releases


__all__ = [
    "DISCOGS_API_ROOT",
    "DISCOGS_ATTRIBUTION_TEXT",
    "DISCOGS_GENERAL_NOTICE",
    "DISCOGS_USER_AGENT",
    "DiscogsProvider",
    "DiscogsProviderError",
    "DiscogsRateLimiter",
    "format_artist_credits",
    "parse_discogs_artist_credits",
    "parse_discogs_release",
    "rank_discogs_candidates",
]
