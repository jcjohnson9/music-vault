"""Strict read-only LRCLIB provider implementation."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from music_vault.core.runtime_policy import runtime_policy_for
from music_vault.version import APP_NAME, APP_VERSION, PROJECT_URL

from ..models import (
    LyricsQuery,
    LyricsResult,
    LyricsSource,
    LyricsStatus,
    ProviderMatch,
    safe_error_code,
)
from ..parser import MAX_LYRIC_BYTES, LyricsParseError, parse_lrc, parse_plain_text
from .base import (
    LyricsContentError,
    LyricsTemporaryError,
    LyricsUnavailableError,
    UnsafeLyricsUrlError,
)


LRCLIB_PROVIDER_NAME = "LRCLIB"
LRCLIB_ATTRIBUTION = "Lyrics via LRCLIB"
LRCLIB_BASE_URL = "https://lrclib.net/api"
LRCLIB_USER_AGENT = f"MusicVault/{APP_VERSION} ({APP_NAME}; +{PROJECT_URL})"
LRCLIB_ALLOWED_HOSTS = frozenset({"lrclib.net"})
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 500
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 2

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_VERSION_RE = re.compile(
    r"\b(live|remix(?:ed)?|cover|edit|acoustic|instrumental|karaoke|demo|"
    r"radio|extended|sped\s*up|slowed|nightcore)\b",
    re.IGNORECASE,
)


def _is_global_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def validate_lrclib_url(
    url: object,
    *,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> str:
    """Allow only official HTTPS LRCLIB API destinations with public DNS."""
    text = str(url or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeLyricsUrlError("invalid_url") from exc
    host = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme.casefold() != "https":
        raise UnsafeLyricsUrlError("https_required")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeLyricsUrlError("userinfo_rejected")
    if port not in (None, 443):
        raise UnsafeLyricsUrlError("port_rejected")
    if host not in LRCLIB_ALLOWED_HOSTS:
        raise UnsafeLyricsUrlError("domain_rejected")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeLyricsUrlError("ip_literal_rejected")
    validated = urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    if not resolve_dns:
        return validated
    try:
        answers = resolver(host, 443, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise LyricsTemporaryError("dns_unavailable") from exc
    addresses: list[str] = []
    for answer in answers:
        try:
            addresses.append(str(answer[4][0]).split("%", 1)[0])
        except (IndexError, TypeError) as exc:
            raise UnsafeLyricsUrlError("invalid_dns_answer") from exc
    if not addresses or any(not _is_global_address(value) for value in addresses):
        raise UnsafeLyricsUrlError("private_address_rejected")
    return validated


class SafeLyricsTransport:
    """Bounded requests transport with redirect and destination revalidation."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    ) -> None:
        self.session = session
        if self.session is not None:
            self.session.trust_env = False
        self.resolver = resolver

    def _network_session(self) -> requests.Session:
        if not runtime_policy_for().allows_provider_construction(token_backed=False):
            raise LyricsUnavailableError("provider_work_deferred")
        if self.session is None:
            self.session = requests.Session()
            self.session.trust_env = False
        return self.session

    @staticmethod
    def _read_limited(response: Any) -> bytes:
        length = response.headers.get("Content-Length")
        if length:
            try:
                parsed = int(length)
            except ValueError as exc:
                raise LyricsContentError("invalid_content_length") from exc
            if parsed < 0 or parsed > MAX_JSON_BYTES:
                raise LyricsContentError("response_too_large")
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    body.extend(chunk)
                    if len(body) > MAX_JSON_BYTES:
                        raise LyricsContentError("response_too_large")
        except requests.RequestException as exc:
            raise LyricsTemporaryError("network_unavailable") from exc
        return bytes(body)

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        headers = {
            "User-Agent": LRCLIB_USER_AGENT,
            "Accept": "application/json",
        }
        session = self._network_session()
        prepared = session.prepare_request(requests.Request("GET", url, params=params, headers=headers))
        current_url = str(prepared.url)
        for redirect_count in range(MAX_REDIRECTS + 1):
            current_url = validate_lrclib_url(current_url, resolver=self.resolver)
            prepared.url = current_url
            try:
                response = session.send(
                    prepared,
                    allow_redirects=False,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise LyricsTemporaryError("network_unavailable") from exc
            except requests.RequestException as exc:
                raise LyricsUnavailableError("request_failed") from exc

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                if not location or redirect_count >= MAX_REDIRECTS:
                    raise LyricsUnavailableError("redirect_rejected")
                current_url = urljoin(current_url, location)
                prepared = session.prepare_request(requests.Request("GET", current_url, headers=headers))
                continue
            if response.status_code == 404 and allow_not_found:
                response.close()
                return None
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                response.close()
                raise LyricsTemporaryError(f"http_{response.status_code}")
            if not 200 <= response.status_code <= 299:
                response.close()
                raise LyricsUnavailableError(f"http_{response.status_code}")
            try:
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
                if mime != "application/json" and not mime.endswith("+json"):
                    raise LyricsContentError("json_content_type_rejected")
                body = self._read_limited(response)
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise LyricsContentError("invalid_json") from exc
            finally:
                response.close()
        raise LyricsUnavailableError("redirect_rejected")


def _normalized_words(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(_WORD_RE.sub(" ", text).split())


def _version_qualifiers(value: object) -> frozenset[str]:
    normalized: set[str] = set()
    for match in _VERSION_RE.finditer(str(value or "")):
        qualifier = " ".join(match.group(1).casefold().split())
        if qualifier.startswith("remix"):
            qualifier = "remix"
        normalized.add(qualifier)
    return frozenset(normalized)


def _duration_ms(value: object) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 86_400:
        return None
    return round(seconds * 1000)


def _candidate(payload: Mapping[str, Any]) -> ProviderMatch | None:
    title = str(payload.get("trackName") or payload.get("name") or "").strip()
    artist = str(payload.get("artistName") or "").strip()
    album = str(payload.get("albumName") or "").strip()
    synced = payload.get("syncedLyrics")
    plain = payload.get("plainLyrics")
    if synced is not None and not isinstance(synced, str):
        return None
    if plain is not None and not isinstance(plain, str):
        return None
    if synced and len(synced.encode("utf-8")) > MAX_LYRIC_BYTES:
        return None
    if plain and len(plain.encode("utf-8")) > MAX_LYRIC_BYTES:
        return None
    instrumental = payload.get("instrumental") is True
    if (
        not title
        or not artist
        or len(title) > 1024
        or len(artist) > 1024
        or len(album) > 1024
        or (not synced and not plain and not instrumental)
    ):
        return None
    result_id = payload.get("id")
    if isinstance(result_id, (str, int)) and len(str(result_id)) > 256:
        return None
    return ProviderMatch(
        str(result_id) if isinstance(result_id, (str, int)) else None,
        title,
        artist,
        album,
        _duration_ms(payload.get("duration")),
        instrumental,
        synced or None,
        plain or None,
    )


def score_lrclib_candidate(query: LyricsQuery, candidate: ProviderMatch) -> float | None:
    """Score only exact, unambiguous metadata identities."""
    if _normalized_words(candidate.title) != _normalized_words(query.title):
        return None
    if _normalized_words(candidate.artist) != _normalized_words(query.artist):
        return None
    if _version_qualifiers(candidate.title) != _version_qualifiers(query.title):
        return None
    if query.duration_ms and candidate.duration_ms:
        tolerance = max(3000, round(query.duration_ms * 0.03))
        if abs(candidate.duration_ms - query.duration_ms) > tolerance:
            return None
    score = 100.0
    if query.duration_ms and candidate.duration_ms:
        score += 8.0
    if query.album and candidate.album and _normalized_words(query.album) == _normalized_words(candidate.album):
        score += 3.0
    if candidate.synced_text:
        score += 4.0
    elif candidate.plain_text:
        score += 1.0
    return score


def _result_from_match(query: LyricsQuery, match: ProviderMatch, score: float) -> LyricsResult | None:
    if match.instrumental:
        return LyricsResult(
            LyricsStatus.INSTRUMENTAL,
            query.identity,
            LyricsSource.PROVIDER,
            provider=LRCLIB_PROVIDER_NAME,
            provider_result_id=match.result_id,
            provider_duration_ms=match.duration_ms,
            attribution=LRCLIB_ATTRIBUTION,
            confidence=min(1.0, score / 115.0),
        )
    if match.synced_text:
        try:
            parsed = parse_lrc(match.synced_text)
        except LyricsParseError:
            parsed = None
        if parsed is not None and parsed.synchronized:
            return LyricsResult(
                LyricsStatus.AVAILABLE,
                query.identity,
                LyricsSource.PROVIDER,
                parsed.lines,
                None,
                LRCLIB_PROVIDER_NAME,
                match.result_id,
                match.duration_ms,
                LRCLIB_ATTRIBUTION,
                min(1.0, score / 115.0),
            )
    if match.plain_text:
        try:
            parsed = parse_plain_text(match.plain_text)
        except LyricsParseError:
            parsed = None
        if parsed is not None and parsed.plain_text:
            return LyricsResult(
                LyricsStatus.AVAILABLE,
                query.identity,
                LyricsSource.PROVIDER,
                (),
                parsed.plain_text,
                LRCLIB_PROVIDER_NAME,
                match.result_id,
                match.duration_ms,
                LRCLIB_ATTRIBUTION,
                min(1.0, score / 115.0),
            )
    return None


def choose_lrclib_result(
    query: LyricsQuery,
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    exact: bool = False,
) -> LyricsResult | None:
    if isinstance(payload, Mapping):
        candidates: Sequence[Mapping[str, Any]] = [payload]
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        if len(payload) > MAX_RESULTS:
            raise LyricsContentError("too_many_results")
        candidates = payload
    else:
        raise LyricsContentError("invalid_json_structure")
    ranked_by_key: dict[str, tuple[float, ProviderMatch]] = {}
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        candidate = _candidate(item)
        if candidate is None:
            continue
        score = score_lrclib_candidate(query, candidate)
        if score is not None:
            key = (
                f"id:{candidate.result_id}"
                if candidate.result_id is not None
                else f"anonymous:{len(ranked_by_key)}"
            )
            previous = ranked_by_key.get(key)
            if previous is None or score > previous[0]:
                ranked_by_key[key] = (score, candidate)
    ranked = list(ranked_by_key.values())
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not exact and len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 3.0:
        return None
    return _result_from_match(query, ranked[0][1], ranked[0][0])


class LRCLIBProvider:
    """Official LRCLIB machine-API client with exact-then-search matching."""

    name = LRCLIB_PROVIDER_NAME

    def __init__(self, transport: SafeLyricsTransport | None = None) -> None:
        self.transport = transport or SafeLyricsTransport()

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def lookup(
        self,
        query: LyricsQuery,
        cancel_event: threading.Event | None = None,
    ) -> LyricsResult:
        if not query.title or not query.artist:
            return LyricsResult(LyricsStatus.NO_MATCH, query.identity)
        if self._cancelled(cancel_event):
            return LyricsResult(LyricsStatus.TEMPORARY_ERROR, query.identity, error_code="cancelled")
        exact_params: dict[str, Any] = {
            "track_name": query.title,
            "artist_name": query.artist,
        }
        if query.album:
            exact_params["album_name"] = query.album
        if query.duration_ms:
            exact_params["duration"] = round(query.duration_ms / 1000)
        try:
            payload = self.transport.get_json(
                f"{LRCLIB_BASE_URL}/get",
                params=exact_params,
                allow_not_found=True,
            )
            if payload is not None:
                if not isinstance(payload, Mapping):
                    raise LyricsContentError("invalid_json_structure")
                result = choose_lrclib_result(query, payload, exact=True)
                if result is not None:
                    return result
            if self._cancelled(cancel_event):
                return LyricsResult(LyricsStatus.TEMPORARY_ERROR, query.identity, error_code="cancelled")
            search_params: dict[str, Any] = {
                "track_name": query.title,
                "artist_name": query.artist,
            }
            if query.album:
                search_params["album_name"] = query.album
            search = self.transport.get_json(
                f"{LRCLIB_BASE_URL}/search",
                params=search_params,
            )
            if not isinstance(search, Sequence) or isinstance(search, (str, bytes)):
                raise LyricsContentError("invalid_json_structure")
            result = choose_lrclib_result(query, search, exact=False)
            return result or LyricsResult(LyricsStatus.NO_MATCH, query.identity)
        except LyricsTemporaryError as exc:
            return LyricsResult(
                LyricsStatus.TEMPORARY_ERROR,
                query.identity,
                error_code=safe_error_code(exc, "provider_temporary"),
            )
        except (LyricsUnavailableError, LyricsContentError, UnsafeLyricsUrlError) as exc:
            return LyricsResult(
                LyricsStatus.TEMPORARY_ERROR,
                query.identity,
                error_code=safe_error_code(exc, "provider_unavailable"),
            )
        except Exception:
            return LyricsResult(
                LyricsStatus.TEMPORARY_ERROR,
                query.identity,
                error_code="provider_error",
            )
