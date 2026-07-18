from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
import unicodedata
import uuid
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Signal, Slot
from PySide6.QtGui import QColor, QImage, QImageReader, QLinearGradient, QPainter

from music_vault.core.paths import (
    artist_image_files_dir,
    artist_image_index_path,
    artist_images_dir,
)
from music_vault.core.acceptance_network import (
    record_provider_factory_invocation,
    record_provider_task_dispatch,
)
from music_vault.core.runtime_policy import RuntimePolicy, runtime_policy_for
from music_vault.version import user_agent


ARTIST_IMAGE_CACHE_SCHEMA_VERSION = 1
ARTIST_IMAGE_USER_AGENT = user_agent()
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 3
NEGATIVE_CACHE_TTL = timedelta(days=30)
TEMPORARY_CACHE_TTL = timedelta(hours=6)

PUBLIC_API_HOSTS = frozenset(
    {
        "musicbrainz.org",
        "www.wikidata.org",
        "en.wikipedia.org",
        "commons.wikimedia.org",
        "upload.wikimedia.org",
    }
)
DISCOGS_IMAGE_HOSTS = frozenset({"i.discogs.com", "api-img.discogs.com"})
DISCOGS_SOURCE_HOSTS = frozenset({"discogs.com", "www.discogs.com"})
PUBLIC_SOURCE_HOSTS = frozenset(
    {
        "musicbrainz.org",
        "www.wikidata.org",
        "en.wikipedia.org",
        "commons.wikimedia.org",
        *DISCOGS_SOURCE_HOSTS,
    }
)
PUBLIC_IMAGE_HOSTS = frozenset({"upload.wikimedia.org", *DISCOGS_IMAGE_HOSTS})
IMAGE_CONTENT_TYPES = {
    "image/jpeg": ("jpg", frozenset({"jpg", "jpeg"})),
    "image/png": ("png", frozenset({"png"})),
    "image/webp": ("webp", frozenset({"webp"})),
}
_MUSICBRAINZ_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_WIKIDATA_ID_RE = re.compile(r"^Q[1-9][0-9]*$")
_SAFE_ERROR_CODE_RE = re.compile(r"[^a-z0-9_.-]+")


class ArtistImageStatus(str, Enum):
    RESOLVED = "resolved"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    TEMPORARY_ERROR = "temporary_error"
    DISABLED = "disabled"


@dataclass(frozen=True)
class ArtistIdentity:
    display_name: str
    normalized_key: str
    discogs_artist_id: str | None = None
    musicbrainz_artist_id: str | None = None

    @classmethod
    def from_display_name(
        cls,
        value: object,
        *,
        discogs_artist_id: object = None,
        musicbrainz_artist_id: object = None,
    ) -> "ArtistIdentity":
        display_name = _display_artist_name(value)
        discogs_id = str(discogs_artist_id or "").strip() or None
        if discogs_id is not None and not re.fullmatch(r"[1-9]\d{0,17}", discogs_id):
            discogs_id = None
        musicbrainz_id = str(musicbrainz_artist_id or "").strip() or None
        if musicbrainz_id is not None and not _MUSICBRAINZ_ID_RE.fullmatch(musicbrainz_id):
            musicbrainz_id = None
        return cls(
            display_name,
            normalize_artist_identity(display_name),
            discogs_id,
            musicbrainz_id,
        )

    @property
    def cache_identity(self) -> str:
        if self.discogs_artist_id:
            return f"discogs:{self.discogs_artist_id}"
        if self.musicbrainz_artist_id:
            return f"musicbrainz:{self.musicbrainz_artist_id.casefold()}"
        return f"name:{self.normalized_key}"


@dataclass(frozen=True)
class ArtistImageResult:
    status: ArtistImageStatus
    identity: ArtistIdentity
    matched_artist_name: str | None = None
    musicbrainz_artist_id: str | None = None
    discogs_artist_id: str | None = None
    match_score: int | None = None
    image_provider: str | None = None
    attribution_text: str | None = None
    source_page_url: str | None = None
    image_url: str | None = None
    cache_file: Path | None = None
    fetched_at: str | None = None
    retry_after: str | None = None
    error_code: str | None = None
    content_type: str | None = None
    image_bytes: bytes | None = field(default=None, repr=False, compare=False)
    from_cache: bool = False

    @property
    def resolved(self) -> bool:
        return self.status is ArtistImageStatus.RESOLVED and self.cache_file is not None


class ArtistImageProvider(Protocol):
    def resolve(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None = None,
    ) -> ArtistImageResult:
        """Resolve an artist image without touching the Music Vault database."""


class DisabledArtistImageProvider:
    """Inert provider used when process-local policy forbids external work."""

    def resolve(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None = None,
    ) -> ArtistImageResult:
        del cancel_event
        return ArtistImageResult(ArtistImageStatus.DISABLED, identity)


class ArtistImageError(RuntimeError):
    """Base class for sanitized artist-image failures."""


class UnsafeArtistImageUrlError(ArtistImageError):
    pass


class ArtistImageTemporaryError(ArtistImageError):
    pass


class ArtistImageUnavailableError(ArtistImageError):
    pass


class ArtistImageContentError(ArtistImageError):
    pass


@dataclass(frozen=True)
class ValidatedImage:
    payload: bytes
    content_type: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True)
class MusicBrainzMatch:
    artist_id: str
    name: str
    score: int


def _display_artist_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split())


def normalize_artist_identity(value: object) -> str:
    """Normalize conservatively without splitting or reinterpreting credits."""
    return _display_artist_name(value).casefold()


def choose_musicbrainz_artist(
    candidates: Sequence[Mapping[str, Any]],
    identity: ArtistIdentity,
    *,
    minimum_score: int = 95,
) -> tuple[ArtistImageStatus, MusicBrainzMatch | None]:
    """Choose only one unique, high-confidence, exact normalized-name match."""
    matches: dict[str, MusicBrainzMatch] = {}
    for candidate in candidates:
        name = _display_artist_name(candidate.get("name"))
        artist_id = str(candidate.get("id") or "").strip()
        try:
            score = int(candidate.get("score", candidate.get("ext:score", 0)))
        except (TypeError, ValueError, OverflowError):
            score = 0
        if (
            artist_id
            and _MUSICBRAINZ_ID_RE.fullmatch(artist_id)
            and score >= minimum_score
            and normalize_artist_identity(name) == identity.normalized_key
        ):
            existing = matches.get(artist_id.casefold())
            if existing is None or score > existing.score:
                matches[artist_id.casefold()] = MusicBrainzMatch(artist_id, name, score)

    if not matches:
        return ArtistImageStatus.NO_MATCH, None
    if len(matches) != 1:
        return ArtistImageStatus.AMBIGUOUS, None
    return ArtistImageStatus.RESOLVED, next(iter(matches.values()))


def _safe_error_code(value: object, fallback: str = "provider_error") -> str:
    text = str(value or "").strip().casefold()
    if not text or len(text) > 64 or _SAFE_ERROR_CODE_RE.search(text):
        return fallback
    return text


def _is_global_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return bool(
            address.is_global
            and not address.is_multicast
            and not address.is_unspecified
            and not address.is_reserved
        )
    except ValueError:
        return False


def _validate_url_syntax(url: object, allowed_hosts: frozenset[str]) -> str:
    text = str(url or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeArtistImageUrlError("invalid_url") from exc

    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme.casefold() != "https":
        raise UnsafeArtistImageUrlError("https_required")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeArtistImageUrlError("userinfo_rejected")
    if port not in (None, 443):
        raise UnsafeArtistImageUrlError("port_rejected")
    if hostname not in allowed_hosts:
        raise UnsafeArtistImageUrlError("domain_rejected")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise UnsafeArtistImageUrlError("ip_literal_rejected")

    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def validate_public_url(
    url: object,
    *,
    allowed_hosts: frozenset[str] = PUBLIC_API_HOSTS,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> str:
    """Validate an HTTPS provider URL and reject non-public destinations."""
    validated = _validate_url_syntax(url, allowed_hosts)
    if not resolve_dns:
        return validated

    hostname = (urlsplit(validated).hostname or "").rstrip(".").casefold()
    try:
        answers = resolver(hostname, 443, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise ArtistImageTemporaryError("dns_unavailable") from exc
    if not answers:
        raise ArtistImageTemporaryError("dns_unavailable")

    addresses: list[str] = []
    for answer in answers:
        try:
            addresses.append(str(answer[4][0]).split("%", 1)[0])
        except (IndexError, TypeError):
            raise UnsafeArtistImageUrlError("invalid_dns_answer")
    if not addresses or any(not _is_global_address(address) for address in addresses):
        raise UnsafeArtistImageUrlError("private_address_rejected")
    return validated


def is_safe_artist_source_url(url: object) -> bool:
    """Return whether a provenance page is safe to open after a user action."""
    try:
        validated = _validate_url_syntax(url, PUBLIC_SOURCE_HOSTS)
    except UnsafeArtistImageUrlError:
        return False
    parsed = urlsplit(validated)
    host = (parsed.hostname or "").casefold()
    path = parsed.path
    if host == "musicbrainz.org":
        return bool(re.fullmatch(r"/artist/[0-9a-fA-F-]{36}", path))
    if host == "www.wikidata.org":
        return bool(re.fullmatch(r"/wiki/Q[1-9][0-9]*", path))
    if host == "en.wikipedia.org":
        return path.startswith("/wiki/") and len(path) > len("/wiki/")
    if host == "commons.wikimedia.org":
        return path.startswith("/wiki/File:") and len(path) > len("/wiki/File:")
    if host in DISCOGS_SOURCE_HOSTS:
        return bool(re.fullmatch(r"/artist/[1-9]\d{0,17}(?:-[^/?#]+)?/?", path))
    return False


def _safe_image_provenance_url(url: object) -> str | None:
    try:
        return _validate_url_syntax(url, PUBLIC_IMAGE_HOSTS)
    except UnsafeArtistImageUrlError:
        return None


def validate_image_payload(
    payload: bytes,
    content_type: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> ValidatedImage:
    """Validate encoded size, MIME, actual format, dimensions, and decodability."""
    mime = str(content_type or "").split(";", 1)[0].strip().casefold()
    format_info = IMAGE_CONTENT_TYPES.get(mime)
    if format_info is None:
        raise ArtistImageContentError("unsupported_image_type")
    if not payload or len(payload) > max_bytes:
        raise ArtistImageContentError("image_size_rejected")

    byte_array = QByteArray(payload)
    buffer = QBuffer()
    buffer.setData(byte_array)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ArtistImageContentError("image_decode_failed")
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        raise ArtistImageContentError("image_dimensions_invalid")
    if size.width() * size.height() > max_pixels:
        raise ArtistImageContentError("image_dimensions_rejected")
    detected = bytes(reader.format()).decode("ascii", errors="ignore").casefold()
    extension, accepted_formats = format_info
    if detected not in accepted_formats:
        raise ArtistImageContentError("image_type_mismatch")
    image = reader.read()
    buffer.close()
    if image.isNull():
        raise ArtistImageContentError("image_decode_failed")
    return ValidatedImage(payload, mime, extension, image.width(), image.height())


class SafeArtistImageTransport:
    """Small requests transport with strict public-destination and body limits."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        allowed_hosts: frozenset[str] = PUBLIC_API_HOSTS,
    ) -> None:
        self.session = session
        if self.session is not None:
            self.session.trust_env = False
        self.resolver = resolver
        self.allowed_hosts = allowed_hosts

    def _network_session(self) -> requests.Session:
        if not runtime_policy_for().allows_provider_construction(token_backed=False):
            raise ArtistImageUnavailableError("provider_work_deferred")
        if self.session is None:
            self.session = requests.Session()
            self.session.trust_env = False
        return self.session

    def _read_limited(self, response: Any, maximum: int) -> bytes:
        length = response.headers.get("Content-Length")
        if length:
            try:
                parsed_length = int(length)
                if parsed_length < 0:
                    raise ArtistImageContentError("invalid_content_length")
                if parsed_length > maximum:
                    raise ArtistImageContentError("response_too_large")
            except ValueError as exc:
                raise ArtistImageContentError("invalid_content_length") from exc
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                body.extend(chunk)
                if len(body) > maximum:
                    raise ArtistImageContentError("response_too_large")
        return bytes(body)

    def _request(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        # Policy must be checked before request preparation or URL validation
        # can construct a transport or reach DNS.
        session = self._network_session()
        request = requests.Request(
            "GET",
            url,
            params=params,
            headers={
                "User-Agent": ARTIST_IMAGE_USER_AGENT,
                "Accept": "application/json, image/jpeg, image/png, image/webp",
            },
        )
        prepared = session.prepare_request(request)
        current_url = str(prepared.url)

        for redirect_count in range(MAX_REDIRECTS + 1):
            current_url = validate_public_url(
                current_url,
                allowed_hosts=self.allowed_hosts,
                resolver=self.resolver,
            )
            prepared.url = current_url
            try:
                response = session.send(
                    prepared,
                    allow_redirects=False,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise ArtistImageTemporaryError("network_unavailable") from exc
            except requests.RequestException as exc:
                raise ArtistImageUnavailableError("request_failed") from exc

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                if redirect_count >= MAX_REDIRECTS or not location:
                    raise ArtistImageUnavailableError("redirect_rejected")
                current_url = urljoin(current_url, location)
                prepared = session.prepare_request(
                    requests.Request(
                        "GET",
                        current_url,
                        headers=dict(request.headers or {}),
                    )
                )
                continue

            if response.status_code == 429 or 500 <= response.status_code <= 599:
                response.close()
                raise ArtistImageTemporaryError(f"http_{response.status_code}")
            if not 200 <= response.status_code <= 299:
                response.close()
                raise ArtistImageUnavailableError(f"http_{response.status_code}")
            return response

        raise ArtistImageUnavailableError("redirect_rejected")

    def get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        response = self._request(url, params=params)
        try:
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
            if mime != "application/json" and not mime.endswith("+json"):
                raise ArtistImageContentError("json_content_type_rejected")
            body = self._read_limited(response, MAX_JSON_BYTES)
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ArtistImageContentError("invalid_json") from exc
        finally:
            response.close()

    def get_image(self, url: str) -> ValidatedImage:
        response = self._request(url)
        try:
            content_type = response.headers.get("Content-Type", "")
            body = self._read_limited(response, MAX_IMAGE_BYTES)
            return validate_image_payload(body, content_type)
        finally:
            response.close()


class _RateLimiter:
    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self, cancel_event: threading.Event | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.minimum_interval_seconds
        if delay:
            if cancel_event is not None and cancel_event.wait(delay):
                raise CancelledError()
            time.sleep(0 if cancel_event is not None else delay)


class MusicBrainzWikimediaProvider:
    """No-key provider using MusicBrainz identity and Wikimedia APIs."""

    def __init__(self, transport: SafeArtistImageTransport | None = None) -> None:
        self.transport = transport or SafeArtistImageTransport()
        self._musicbrainz_rate = _RateLimiter(1.05)

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()

    def _musicbrainz_json(
        self,
        url: str,
        params: Mapping[str, Any],
        cancel_event: threading.Event | None,
    ) -> Any:
        self._cancelled(cancel_event)
        self._musicbrainz_rate.wait(cancel_event)
        return self.transport.get_json(url, params=params)

    @staticmethod
    def _relation_targets(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
        wikidata_ids: set[str] = set()
        wikipedia_titles: set[str] = set()
        for relation in payload.get("relations", []) or []:
            if not isinstance(relation, Mapping):
                continue
            resource = str((relation.get("url") or {}).get("resource") or "")
            relation_type = str(relation.get("type") or "").casefold()
            try:
                parsed = urlsplit(resource)
            except ValueError:
                continue
            host = (parsed.hostname or "").rstrip(".").casefold()
            if relation_type == "wikidata" and host == "www.wikidata.org":
                candidate = parsed.path.rsplit("/", 1)[-1]
                if _WIKIDATA_ID_RE.fullmatch(candidate):
                    wikidata_ids.add(candidate)
            elif relation_type == "wikipedia" and host == "en.wikipedia.org":
                prefix = "/wiki/"
                if parsed.path.startswith(prefix) and len(parsed.path) > len(prefix):
                    wikipedia_titles.add(parsed.path[len(prefix) :])
        wikidata_id = next(iter(wikidata_ids)) if len(wikidata_ids) == 1 else None
        wikipedia_title = (
            next(iter(wikipedia_titles)) if len(wikipedia_titles) == 1 else None
        )
        return wikidata_id, wikipedia_title

    @staticmethod
    def _wikidata_claim_values(
        entity: Mapping[str, Any], property_id: str
    ) -> set[str]:
        values: set[str] = set()
        claim_groups = entity.get("claims") or {}
        if not isinstance(claim_groups, Mapping):
            return values
        claims = claim_groups.get(property_id) or []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            mainsnak = claim.get("mainsnak") or {}
            if not isinstance(mainsnak, Mapping):
                continue
            datavalue = mainsnak.get("datavalue") or {}
            if not isinstance(datavalue, Mapping):
                continue
            value = datavalue.get("value")
            if isinstance(value, (str, int)):
                text = str(value).strip()
                if text:
                    values.add(text)
        return values

    @classmethod
    def _wikidata_entity_matches_provider(
        cls,
        entity: Mapping[str, Any],
        *,
        musicbrainz_artist_id: str | None,
        discogs_artist_id: str | None,
    ) -> bool:
        # MusicBrainz is authoritative in this provider. Discogs remains a
        # useful strict disambiguator when no MusicBrainz identity is known.
        if musicbrainz_artist_id:
            expected = musicbrainz_artist_id.casefold()
            return expected in {
                value.casefold()
                for value in cls._wikidata_claim_values(entity, "P434")
            }
        if discogs_artist_id:
            return discogs_artist_id in cls._wikidata_claim_values(entity, "P1953")
        return True

    @staticmethod
    def _wikidata_image_filename(entity: Mapping[str, Any]) -> str | None:
        claim_groups = entity.get("claims") or {}
        if not isinstance(claim_groups, Mapping):
            return None
        claims = claim_groups.get("P18") or []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            mainsnak = claim.get("mainsnak") or {}
            if not isinstance(mainsnak, Mapping):
                continue
            datavalue = mainsnak.get("datavalue") or {}
            if not isinstance(datavalue, Mapping):
                continue
            filename = str(datavalue.get("value") or "").strip()
            if filename:
                return filename
        return None

    def _commons_image(
        self,
        filename: str,
        cancel_event: threading.Event | None,
    ) -> tuple[ValidatedImage, str, str] | None:
        self._cancelled(cancel_event)
        payload = self.transport.get_json(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": "640",
                "titles": f"File:{filename}",
            },
        )
        pages = ((payload or {}).get("query") or {}).get("pages") or []
        if not pages or not isinstance(pages[0], Mapping):
            return None
        image_info = pages[0].get("imageinfo") or []
        if not image_info or not isinstance(image_info[0], Mapping):
            return None
        info = image_info[0]
        image_url = str(info.get("thumburl") or info.get("url") or "")
        if not image_url:
            return None
        validated = self.transport.get_image(image_url)
        source = str(info.get("descriptionurl") or "")
        if not is_safe_artist_source_url(source):
            source = "https://commons.wikimedia.org/wiki/File:" + quote(filename, safe="_()-.~")
        return validated, source, image_url

    def _wikidata_image(
        self,
        wikidata_id: str,
        cancel_event: threading.Event | None,
    ) -> tuple[ValidatedImage, str, str] | None:
        self._cancelled(cancel_event)
        payload = self.transport.get_json(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "format": "json",
                "ids": wikidata_id,
                "props": "claims",
            },
        )
        entity = ((payload or {}).get("entities") or {}).get(wikidata_id) or {}
        if not isinstance(entity, Mapping):
            return None
        filename = self._wikidata_image_filename(entity)
        if filename:
            return self._commons_image(filename, cancel_event)
        return None

    def _wikipedia_image(
        self,
        page_title: str,
        cancel_event: threading.Event | None,
        *,
        expected_identity: ArtistIdentity | None = None,
        expected_wikidata_id: str | None = None,
        musicbrainz_artist_id: str | None = None,
        discogs_artist_id: str | None = None,
    ) -> tuple[ValidatedImage, str, str] | None:
        self._cancelled(cancel_event)
        payload = self.transport.get_json(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "pageimages|info|pageprops",
                "piprop": "thumbnail|original",
                "pithumbsize": "640",
                "inprop": "url",
                "titles": page_title.replace("_", " "),
            },
        )
        pages = ((payload or {}).get("query") or {}).get("pages") or []
        if not pages or not isinstance(pages[0], Mapping):
            return None
        page = pages[0]
        if expected_identity is not None:
            pageprops = page.get("pageprops") or {}
            if not isinstance(pageprops, Mapping):
                return None
            if (
                "missing" in page
                or "invalid" in page
                or normalize_artist_identity(page.get("title"))
                != expected_identity.normalized_key
                or "disambiguation" in pageprops
            ):
                return None
            wikidata_id = str(pageprops.get("wikibase_item") or "").strip()
            if expected_wikidata_id:
                if wikidata_id != expected_wikidata_id:
                    return None
            elif musicbrainz_artist_id or discogs_artist_id:
                if not _WIKIDATA_ID_RE.fullmatch(wikidata_id):
                    return None
                entity_payload = self.transport.get_json(
                    "https://www.wikidata.org/w/api.php",
                    params={
                        "action": "wbgetentities",
                        "format": "json",
                        "ids": wikidata_id,
                        "props": "claims",
                    },
                )
                entity = (
                    ((entity_payload or {}).get("entities") or {}).get(wikidata_id)
                    or {}
                )
                if not isinstance(entity, Mapping) or not self._wikidata_entity_matches_provider(
                    entity,
                    musicbrainz_artist_id=musicbrainz_artist_id,
                    discogs_artist_id=discogs_artist_id,
                ):
                    return None
        image_url = str(
            (page.get("thumbnail") or {}).get("source")
            or (page.get("original") or {}).get("source")
            or ""
        )
        if not image_url:
            return None
        validated = self.transport.get_image(image_url)
        source = str(page.get("fullurl") or "")
        if not is_safe_artist_source_url(source):
            source = "https://en.wikipedia.org/wiki/" + quote(page_title, safe="_()-.~")
        return validated, source, image_url

    def _direct_wikimedia_image(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None,
        *,
        musicbrainz_artist_id: str | None,
    ) -> tuple[ValidatedImage, str, str] | None:
        """Resolve only an exact canonical label with provider-ID confirmation."""

        self._cancelled(cancel_event)
        search = self.transport.get_json(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbsearchentities",
                "format": "json",
                "language": "en",
                "uselang": "en",
                "type": "item",
                "limit": "5",
                "search": identity.display_name,
            },
        )
        exact_ids: set[str] = set()
        search_results = (
            (search or {}).get("search") if isinstance(search, Mapping) else ()
        ) or ()
        for candidate in search_results:
            if not isinstance(candidate, Mapping):
                continue
            wikidata_id = str(candidate.get("id") or "").strip()
            label = _display_artist_name(candidate.get("label"))
            if (
                _WIKIDATA_ID_RE.fullmatch(wikidata_id)
                and normalize_artist_identity(label) == identity.normalized_key
            ):
                exact_ids.add(wikidata_id)

        chosen_id: str | None = None
        chosen_entity: Mapping[str, Any] | None = None
        if exact_ids:
            entities_payload = self.transport.get_json(
                "https://www.wikidata.org/w/api.php",
                params={
                    "action": "wbgetentities",
                    "format": "json",
                    "ids": "|".join(sorted(exact_ids)),
                    "props": "claims|sitelinks",
                },
            )
            entities = (
                (entities_payload or {}).get("entities")
                if isinstance(entities_payload, Mapping)
                else {}
            ) or {}
            if not isinstance(entities, Mapping):
                return None
            matches: list[tuple[str, Mapping[str, Any]]] = []
            for wikidata_id in sorted(exact_ids):
                entity = entities.get(wikidata_id) or {}
                if isinstance(entity, Mapping) and self._wikidata_entity_matches_provider(
                    entity,
                    musicbrainz_artist_id=musicbrainz_artist_id,
                    discogs_artist_id=identity.discogs_artist_id,
                ):
                    matches.append((wikidata_id, entity))
            # A provider-free lookup is safe only when the exact label itself
            # has one result. Provider-backed identities may disambiguate an
            # otherwise duplicated label, but still require one unique match.
            if len(matches) != 1 or (
                not musicbrainz_artist_id
                and not identity.discogs_artist_id
                and len(exact_ids) != 1
            ):
                return None
            chosen_id, chosen_entity = matches[0]

            filename = self._wikidata_image_filename(chosen_entity)
            if filename:
                resolved = self._commons_image(filename, cancel_event)
                if resolved is not None:
                    return resolved

            enwiki = (chosen_entity.get("sitelinks") or {}).get("enwiki") or {}
            title = _display_artist_name(enwiki.get("title"))
            if title and normalize_artist_identity(title) == identity.normalized_key:
                resolved = self._wikipedia_image(
                    title,
                    cancel_event,
                    expected_identity=identity,
                    expected_wikidata_id=chosen_id,
                )
                if resolved is not None:
                    return resolved

        # If Wikidata returned an exact but conflicting identity, do not let a
        # name-only Wikipedia query bypass that conflict.
        if exact_ids and chosen_entity is None:
            return None
        return self._wikipedia_image(
            identity.display_name,
            cancel_event,
            expected_identity=identity,
            musicbrainz_artist_id=musicbrainz_artist_id,
            discogs_artist_id=identity.discogs_artist_id,
        )

    def resolve(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None = None,
    ) -> ArtistImageResult:
        if not identity.normalized_key:
            return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)
        try:
            validated_musicbrainz_id = (
                identity.musicbrainz_artist_id
                if identity.musicbrainz_artist_id
                and _MUSICBRAINZ_ID_RE.fullmatch(identity.musicbrainz_artist_id)
                else None
            )
            if validated_musicbrainz_id:
                match = MusicBrainzMatch(
                    validated_musicbrainz_id,
                    identity.display_name,
                    100,
                )
            else:
                escaped_name = identity.display_name.replace("\\", "\\\\").replace('"', '\\"')
                search = self._musicbrainz_json(
                    "https://musicbrainz.org/ws/2/artist/",
                    {"query": f'artist:"{escaped_name}"', "limit": "5", "fmt": "json"},
                    cancel_event,
                )
                candidates = (search or {}).get("artists") or []
                status, match = choose_musicbrainz_artist(candidates, identity)
                if match is None:
                    return ArtistImageResult(status, identity)

            details = self._musicbrainz_json(
                f"https://musicbrainz.org/ws/2/artist/{match.artist_id}",
                {"inc": "url-rels", "fmt": "json"},
                cancel_event,
            )
            wikidata_id, wikipedia_title = self._relation_targets(details or {})
            resolved: tuple[ValidatedImage, str, str] | None = None
            if wikidata_id:
                resolved = self._wikidata_image(wikidata_id, cancel_event)
            if resolved is None and wikipedia_title:
                resolved = self._wikipedia_image(wikipedia_title, cancel_event)
            if resolved is None:
                resolved = self._direct_wikimedia_image(
                    identity,
                    cancel_event,
                    musicbrainz_artist_id=match.artist_id,
                )
            if resolved is None:
                return ArtistImageResult(
                    ArtistImageStatus.NO_MATCH,
                    identity,
                    matched_artist_name=match.name,
                    musicbrainz_artist_id=match.artist_id,
                    match_score=match.score,
                )

            image, source_page, image_url = resolved
            return ArtistImageResult(
                ArtistImageStatus.RESOLVED,
                identity,
                matched_artist_name=match.name,
                musicbrainz_artist_id=match.artist_id,
                match_score=match.score,
                image_provider="Wikimedia Commons",
                source_page_url=source_page,
                image_url=image_url,
                content_type=image.content_type,
                image_bytes=image.payload,
            )
        except CancelledError:
            raise
        except ArtistImageTemporaryError as exc:
            return ArtistImageResult(
                ArtistImageStatus.TEMPORARY_ERROR,
                identity,
                error_code=_safe_error_code(exc),
            )
        except (ArtistImageUnavailableError, ArtistImageContentError, UnsafeArtistImageUrlError) as exc:
            return ArtistImageResult(
                ArtistImageStatus.UNAVAILABLE,
                identity,
                error_code=_safe_error_code(exc),
            )
        except Exception:
            return ArtistImageResult(
                ArtistImageStatus.TEMPORARY_ERROR,
                identity,
                error_code="provider_error",
            )


class DiscogsArtistImageProvider:
    """Private-token Discogs artist portrait provider.

    Catalogue responses remain in the existing provider's bounded in-memory
    cache. Only a validated artist image is returned to ``ArtistImageCache``;
    release artwork and raw JSON are never persisted here.
    """

    def __init__(
        self,
        token: object = None,
        *,
        catalogue_provider: object | None = None,
        transport: SafeArtistImageTransport | None = None,
    ) -> None:
        if catalogue_provider is None:
            from .providers.discogs import DiscogsProvider

            catalogue_provider = DiscogsProvider(token=token)
        self.catalogue_provider = catalogue_provider
        self.transport = transport or SafeArtistImageTransport(
            allowed_hosts=DISCOGS_IMAGE_HOSTS
        )

    @staticmethod
    def _clean_discogs_name(value: object) -> str:
        name = _display_artist_name(value)
        return re.sub(r"\s+\([1-9]\d*\)\s*$", "", name).strip()

    def _resolve_artist_id(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None,
    ) -> str | None:
        if identity.discogs_artist_id:
            return identity.discogs_artist_id
        from .providers import ProviderQuery

        results = self.catalogue_provider.search_artists(
            ProviderQuery(title=identity.display_name, artist=identity.display_name),
            max_pages=1,
            max_results=10,
            cancel_event=cancel_event,
        )
        candidates: set[str] = set()
        for result in results:
            if not isinstance(result, Mapping):
                continue
            candidate_id = str(result.get("id") or "").strip()
            candidate_name = self._clean_discogs_name(
                result.get("title") or result.get("name")
            )
            if (
                re.fullmatch(r"[1-9]\d{0,17}", candidate_id)
                and normalize_artist_identity(candidate_name) == identity.normalized_key
            ):
                candidates.add(candidate_id)
        return next(iter(candidates)) if len(candidates) == 1 else None

    @staticmethod
    def _artist_image(payload: Mapping[str, Any]) -> str | None:
        values = payload.get("images") or ()
        if not isinstance(values, Sequence) or isinstance(
            values, (str, bytes, bytearray)
        ):
            return None
        ranked: list[tuple[int, int, str]] = []
        for order, value in enumerate(values):
            if not isinstance(value, Mapping):
                continue
            image_type = str(value.get("type") or "").strip().casefold()
            if image_type not in {"primary", "secondary"}:
                continue
            image_url = str(
                value.get("uri150")
                or value.get("uri")
                or value.get("resource_url")
                or ""
            ).strip()
            try:
                width = int(value.get("width") or 0)
                height = int(value.get("height") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if width < 0 or height < 0 or (
                width and height and width * height > MAX_IMAGE_PIXELS
            ):
                continue
            if image_url:
                ranked.append((0 if image_type == "primary" else 1, order, image_url))
        return min(ranked)[2] if ranked else None

    def resolve(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None = None,
    ) -> ArtistImageResult:
        if not identity.normalized_key:
            return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            artist_id = self._resolve_artist_id(identity, cancel_event)
            if not artist_id:
                return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)
            payload = self.catalogue_provider.get_artist(
                artist_id, cancel_event=cancel_event
            )
            if not isinstance(payload, Mapping):
                return ArtistImageResult(ArtistImageStatus.UNAVAILABLE, identity)
            matched_name = self._clean_discogs_name(payload.get("name"))
            if normalize_artist_identity(matched_name) != identity.normalized_key:
                return ArtistImageResult(ArtistImageStatus.AMBIGUOUS, identity)
            image_url = self._artist_image(payload)
            if not image_url:
                return ArtistImageResult(
                    ArtistImageStatus.NO_MATCH,
                    identity,
                    matched_artist_name=matched_name,
                    discogs_artist_id=artist_id,
                    match_score=100,
                )
            validated = self.transport.get_image(image_url)
            source_page = f"https://www.discogs.com/artist/{artist_id}"
            return ArtistImageResult(
                ArtistImageStatus.RESOLVED,
                identity,
                matched_artist_name=matched_name,
                discogs_artist_id=artist_id,
                match_score=100,
                image_provider="Discogs",
                attribution_text="Data provided by Discogs",
                source_page_url=source_page,
                image_url=image_url,
                content_type=validated.content_type,
                image_bytes=validated.payload,
            )
        except CancelledError:
            raise
        except ArtistImageTemporaryError as exc:
            return ArtistImageResult(
                ArtistImageStatus.TEMPORARY_ERROR,
                identity,
                error_code=_safe_error_code(exc),
            )
        except (ArtistImageUnavailableError, ArtistImageContentError, UnsafeArtistImageUrlError) as exc:
            return ArtistImageResult(
                ArtistImageStatus.UNAVAILABLE,
                identity,
                error_code=_safe_error_code(exc),
            )
        except Exception as exc:
            # Discogs provider exceptions are deliberately sanitized at their
            # boundary. Never reflect tokens, URLs, or response content here.
            code = str(exc).strip().casefold()
            status = (
                ArtistImageStatus.TEMPORARY_ERROR
                if any(value in code for value in ("unavailable", "rate_limited", "cancelled"))
                else ArtistImageStatus.UNAVAILABLE
            )
            return ArtistImageResult(status, identity, error_code="discogs_artist_image_failed")


class ChainedArtistImageProvider:
    """Try ordered portrait providers until one resolves a validated image."""

    def __init__(self, providers: Sequence[ArtistImageProvider]) -> None:
        self.providers = tuple(providers)

    def resolve(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None = None,
    ) -> ArtistImageResult:
        last = ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)
        retryable: ArtistImageResult | None = None
        for provider in self.providers:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            result = provider.resolve(identity, cancel_event)
            if result.status is ArtistImageStatus.RESOLVED:
                return result
            if result.status is ArtistImageStatus.TEMPORARY_ERROR:
                retryable = result
            last = result
        # A later definitive no-match must not turn a transient upstream
        # failure into a long-lived negative cache entry.
        return retryable or last


class SyntheticArtistImageProvider:
    """Deterministic no-network provider for explicitly isolated tests/review."""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = max(0.0, delay_seconds)
        self.calls: list[str] = []

    @staticmethod
    def _portrait(identity: ArtistIdentity) -> bytes:
        digest = hashlib.sha256(identity.normalized_key.encode("utf-8")).digest()
        image = QImage(512, 512, QImage.Format.Format_ARGB32)
        image.fill(QColor(16 + digest[0] % 20, 25 + digest[1] % 22, 30 + digest[2] % 25))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0, 0, 512, 512)
        gradient.setColorAt(0, QColor(32, 203, 118, 220))
        gradient.setColorAt(1, QColor(36 + digest[3] % 60, 74, 98 + digest[4] % 60, 220))
        painter.setBrush(gradient)
        painter.setPen(QColor(255, 255, 255, 45))
        painter.drawEllipse(142, 72, 228, 228)
        painter.drawRoundedRect(82, 298, 348, 250, 120, 120)
        painter.setBrush(QColor(255, 255, 255, 34))
        painter.setPen(QColor(255, 255, 255, 28))
        painter.drawEllipse(40 + digest[5] % 80, 36 + digest[6] % 70, 72, 72)
        painter.end()
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise ArtistImageContentError("synthetic_image_failed")
        buffer.close()
        return bytes(payload)

    def resolve(
        self,
        identity: ArtistIdentity,
        cancel_event: threading.Event | None = None,
    ) -> ArtistImageResult:
        self.calls.append(identity.normalized_key)
        if self.delay_seconds and cancel_event is not None:
            if cancel_event.wait(self.delay_seconds):
                raise CancelledError()
        elif self.delay_seconds:
            time.sleep(self.delay_seconds)
        key = identity.normalized_key
        if "ambiguous" in key:
            return ArtistImageResult(ArtistImageStatus.AMBIGUOUS, identity)
        if "no match" in key or "unknown" in key:
            return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)
        if "temporary" in key:
            return ArtistImageResult(
                ArtistImageStatus.TEMPORARY_ERROR,
                identity,
                error_code="synthetic_temporary",
            )
        if "corrupt" in key:
            return ArtistImageResult(
                ArtistImageStatus.RESOLVED,
                identity,
                matched_artist_name=identity.display_name,
                match_score=100,
                image_provider="Synthetic review provider",
                content_type="image/png",
                image_bytes=b"not-an-image",
            )
        return ArtistImageResult(
            ArtistImageStatus.RESOLVED,
            identity,
            matched_artist_name=identity.display_name,
            musicbrainz_artist_id=None,
            match_score=100,
            image_provider="Synthetic review provider",
            content_type="image/png",
            image_bytes=self._portrait(identity),
        )


def create_artist_image_provider(
    *,
    runtime_policy: RuntimePolicy | None = None,
) -> ArtistImageProvider:
    """Create production provider unless an explicit isolated review requests fake data."""
    record_provider_factory_invocation()
    policy = runtime_policy or RuntimePolicy.from_environment()
    if not policy.provider_construction_allowed:
        # Check policy before interpreting a requested mode, constructing a
        # transport, or reading the private Discogs token.
        return DisabledArtistImageProvider()
    requested = os.environ.get("MUSIC_VAULT_ARTIST_IMAGE_PROVIDER", "").strip().casefold()
    if requested in {"synthetic", "fake"}:
        if not (
            os.environ.get("MUSIC_VAULT_UI_REVIEW", "").strip()
            and os.environ.get("MUSIC_VAULT_PROJECT_ROOT", "").strip()
        ):
            raise RuntimeError("Synthetic artist provider requires isolated UI review mode.")
        return SyntheticArtistImageProvider()
    if requested not in {"", "production", "public"}:
        raise RuntimeError("Unknown artist-image provider mode.")
    public_provider = MusicBrainzWikimediaProvider()
    from .intelligence_settings import DiscogsTokenStore

    token = DiscogsTokenStore().read()
    if not token:
        return public_provider
    return ChainedArtistImageProvider(
        (DiscogsArtistImageProvider(token=token), public_provider)
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ArtistImageCache:
    """Versioned runtime cache with atomic metadata and content-addressed files."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.root = Path(root) if root is not None else artist_images_dir()
        self.root = self.root.expanduser().resolve()
        if root is None:
            self.files_dir = artist_image_files_dir().resolve()
            self.index_path = artist_image_index_path().resolve()
        else:
            self.files_dir = (self.root / "files").resolve()
            self.index_path = (self.root / "index.json").resolve()
        if self.files_dir.parent != self.root or self.index_path.parent != self.root:
            raise ValueError("Artist-image cache paths must remain inside the cache root.")
        self.clock = clock
        self._lock = threading.RLock()
        self._manifest: dict[str, Any] | None = None

    @staticmethod
    def _entry_key(identity: ArtistIdentity) -> str:
        return hashlib.sha256(identity.cache_identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _legacy_entry_key(identity: ArtistIdentity) -> str:
        return hashlib.sha256(identity.normalized_key.encode("utf-8")).hexdigest()

    def _empty_manifest(self) -> dict[str, Any]:
        return {"schema_version": ARTIST_IMAGE_CACHE_SCHEMA_VERSION, "entries": {}}

    def _load(self) -> dict[str, Any]:
        if self._manifest is not None:
            return self._manifest
        try:
            loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
            if (
                not isinstance(loaded, dict)
                or loaded.get("schema_version") != ARTIST_IMAGE_CACHE_SCHEMA_VERSION
                or not isinstance(loaded.get("entries"), dict)
            ):
                raise ValueError("unsupported cache manifest")
            self._manifest = loaded
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            self._manifest = self._empty_manifest()
        return self._manifest

    def _write_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".index-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._load(), stream, indent=2, sort_keys=True, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _safe_cached_path(self, relative_value: object) -> Path | None:
        text = str(relative_value or "").replace("\\", "/")
        relative = PurePosixPath(text)
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "files":
            return None
        if relative.parts[1] in {"", ".", ".."}:
            return None
        candidate = (self.root / Path(*relative.parts)).resolve()
        if candidate.parent != self.files_dir:
            return None
        return candidate

    def _drop_broken_entry(self, entry_key: str, file_path: Path | None = None) -> None:
        entries = self._load()["entries"]
        entries.pop(entry_key, None)
        if file_path is not None and file_path.parent == self.files_dir:
            file_path.unlink(missing_ok=True)
        self._write_manifest()

    def lookup(
        self,
        identity: ArtistIdentity,
        *,
        repair: bool = True,
    ) -> ArtistImageResult | None:
        """Return a cached result.

        ``repair=False`` is used by quiescent/acceptance paths. It validates
        existing cache data but deliberately avoids index or file mutation
        when a malformed entry is encountered.
        """
        with self._lock:
            entry_key = self._entry_key(identity)
            record = self._load()["entries"].get(entry_key)
            if (
                not isinstance(record, dict)
                and identity.discogs_artist_id is None
                and identity.musicbrainz_artist_id is None
            ):
                # Schema-1 name-only cache entries remain useful and are never
                # shared across newly distinguished provider-backed entities.
                legacy_key = self._legacy_entry_key(identity)
                legacy_record = self._load()["entries"].get(legacy_key)
                if isinstance(legacy_record, dict):
                    entry_key, record = legacy_key, legacy_record
            if not isinstance(record, dict):
                return None
            if record.get("normalized_key") != identity.normalized_key:
                if repair:
                    self._drop_broken_entry(entry_key)
                return None
            try:
                status = ArtistImageStatus(str(record.get("status")))
            except ValueError:
                if repair:
                    self._drop_broken_entry(entry_key)
                return None

            if status is ArtistImageStatus.RESOLVED:
                cache_file = self._safe_cached_path(record.get("cache_file"))
                if cache_file is None or not cache_file.is_file():
                    if repair:
                        self._drop_broken_entry(entry_key, cache_file)
                    return None
                try:
                    if cache_file.stat().st_size > MAX_IMAGE_BYTES:
                        raise ArtistImageContentError("image_size_rejected")
                    validate_image_payload(
                        cache_file.read_bytes(),
                        str(record.get("content_type") or ""),
                    )
                except (OSError, ArtistImageContentError):
                    if repair:
                        self._drop_broken_entry(entry_key, cache_file)
                    return None
            else:
                cache_file = None
                retry_at = _parse_iso(record.get("retry_after"))
                if retry_at is None or self.clock().astimezone(timezone.utc) >= retry_at:
                    return None

            return ArtistImageResult(
                status=status,
                identity=identity,
                matched_artist_name=record.get("matched_artist_name"),
                musicbrainz_artist_id=record.get("musicbrainz_artist_id"),
                discogs_artist_id=record.get("discogs_artist_id"),
                match_score=record.get("match_score"),
                image_provider=record.get("image_provider"),
                attribution_text=record.get("attribution_text"),
                source_page_url=(
                    str(record.get("source_page_url"))
                    if is_safe_artist_source_url(record.get("source_page_url"))
                    else None
                ),
                image_url=_safe_image_provenance_url(record.get("image_url")),
                cache_file=cache_file,
                fetched_at=record.get("fetched_at"),
                retry_after=record.get("retry_after"),
                error_code=record.get("error_code"),
                content_type=record.get("content_type"),
                from_cache=True,
            )

    def store(self, result: ArtistImageResult) -> ArtistImageResult:
        if result.status is ArtistImageStatus.DISABLED:
            return result
        now = self.clock().astimezone(timezone.utc)
        with self._lock:
            cache_file: Path | None = None
            relative_file: str | None = None
            content_type = result.content_type
            if result.status is ArtistImageStatus.RESOLVED:
                if result.image_bytes is None:
                    raise ArtistImageContentError("resolved_image_missing")
                validated = validate_image_payload(result.image_bytes, result.content_type or "")
                digest = hashlib.sha256(validated.payload).hexdigest()
                self.files_dir.mkdir(parents=True, exist_ok=True)
                cache_file = self.files_dir / f"{digest}.{validated.extension}"
                if not cache_file.exists():
                    temporary = self.files_dir / f".{digest}-{uuid.uuid4().hex}.tmp"
                    try:
                        with temporary.open("wb") as stream:
                            stream.write(validated.payload)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, cache_file)
                    finally:
                        temporary.unlink(missing_ok=True)
                relative_file = cache_file.relative_to(self.root).as_posix()
                content_type = validated.content_type
                retry_after = None
            else:
                ttl = (
                    TEMPORARY_CACHE_TTL
                    if result.status is ArtistImageStatus.TEMPORARY_ERROR
                    else NEGATIVE_CACHE_TTL
                )
                retry_after = _iso(now + ttl)

            musicbrainz_id = str(result.musicbrainz_artist_id or "").strip()
            if not _MUSICBRAINZ_ID_RE.fullmatch(musicbrainz_id):
                musicbrainz_id = None
            source_page_url = (
                str(result.source_page_url)
                if is_safe_artist_source_url(result.source_page_url)
                else None
            )
            image_url = _safe_image_provenance_url(result.image_url)
            record = {
                "status": result.status.value,
                "requested_display_name": result.identity.display_name,
                "normalized_key": result.identity.normalized_key,
                "identity_key": result.identity.cache_identity,
                "matched_artist_name": result.matched_artist_name,
                "musicbrainz_artist_id": musicbrainz_id,
                "discogs_artist_id": (
                    str(result.discogs_artist_id)
                    if re.fullmatch(r"[1-9]\d{0,17}", str(result.discogs_artist_id or ""))
                    else None
                ),
                "match_score": result.match_score,
                "image_provider": result.image_provider,
                "attribution_text": (
                    str(result.attribution_text)[:160]
                    if result.attribution_text
                    else None
                ),
                "source_page_url": source_page_url,
                "image_url": image_url,
                "cache_file": relative_file,
                "content_type": content_type,
                "fetched_at": _iso(now),
                "retry_after": retry_after,
                "error_code": _safe_error_code(result.error_code) if result.error_code else None,
            }
            self._load()["entries"][self._entry_key(result.identity)] = record
            self._write_manifest()
            return replace(
                result,
                musicbrainz_artist_id=musicbrainz_id,
                source_page_url=source_page_url,
                image_url=image_url,
                cache_file=cache_file,
                image_bytes=None,
                fetched_at=record["fetched_at"],
                retry_after=retry_after,
                error_code=record["error_code"],
                content_type=content_type,
            )

    def clear(self, identity: ArtistIdentity | None = None) -> None:
        """Clear only this cache's manifest/files, never neighboring runtime data."""
        with self._lock:
            if identity is None:
                if self.files_dir.is_symlink():
                    self.files_dir.unlink(missing_ok=True)
                elif self.files_dir.exists():
                    shutil.rmtree(self.files_dir)
                self.index_path.unlink(missing_ok=True)
                for temporary in self.root.glob(".index-*.tmp") if self.root.exists() else ():
                    temporary.unlink(missing_ok=True)
                self._manifest = self._empty_manifest()
                return

            records = [
                self._load()["entries"].pop(entry_key, None)
                for entry_key in {
                    self._entry_key(identity),
                    self._legacy_entry_key(identity),
                }
            ]
            remaining = {
                candidate.get("cache_file")
                for candidate in self._load()["entries"].values()
                if isinstance(candidate, dict)
            }
            for record in records:
                if not isinstance(record, dict):
                    continue
                relative_file = record.get("cache_file")
                file_path = self._safe_cached_path(relative_file)
                if file_path is not None and relative_file not in remaining:
                    file_path.unlink(missing_ok=True)
            self._write_manifest()

    def rekey(self, old_identity: ArtistIdentity, new_identity: ArtistIdentity) -> bool:
        """Preserve valid portraits and discard stale failures after a safe merge."""

        with self._lock:
            entries = self._load()["entries"]
            # Provider-backed identities must never consume an unrelated
            # same-name legacy entry. The caller may explicitly issue a
            # separate name-only rekey after exact provider probes fail.
            old_keys = (self._entry_key(old_identity),)
            source_key = next(
                (key for key in old_keys if isinstance(entries.get(key), dict)), None
            )
            if source_key is None:
                return False
            record = dict(entries.pop(source_key))
            target_key = self._entry_key(new_identity)
            if record.get("status") == ArtistImageStatus.RESOLVED.value:
                record["requested_display_name"] = new_identity.display_name
                record["normalized_key"] = new_identity.normalized_key
                record["identity_key"] = new_identity.cache_identity
                target = entries.get(target_key)
                if (
                    not isinstance(target, dict)
                    or target.get("status") != ArtistImageStatus.RESOLVED.value
                ):
                    entries[target_key] = record
            # Negative malformed-name cache entries intentionally disappear so
            # an explicitly enabled refresh may retry the canonical identity.
            self._write_manifest()
            return True

    def statistics(self) -> dict[str, int]:
        with self._lock:
            entries = self._load()["entries"]
            total_bytes = 0
            file_count = 0
            if self.files_dir.exists():
                for path in self.files_dir.iterdir():
                    if path.is_file() and not path.is_symlink():
                        file_count += 1
                        total_bytes += path.stat().st_size
            return {
                "entry_count": len(entries),
                "file_count": file_count,
                "total_bytes": total_bytes,
            }


@dataclass
class _PendingRequest:
    future: Future[ArtistImageResult]
    cancel_event: threading.Event
    callbacks: list[Callable[[ArtistImageResult], None]]
    generation: int


class ArtistImageService(QObject):
    """Capped, coalescing background resolver with GUI-thread delivery."""

    result_ready = Signal(str, object)
    _completed = Signal(object, object, int)

    def __init__(
        self,
        provider: ArtistImageProvider | None,
        cache: ArtistImageCache,
        *,
        provider_factory: Callable[[], ArtistImageProvider] | None = None,
        max_workers: int = 2,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if provider is None and provider_factory is None:
            raise ValueError("An artist-image provider or provider factory is required.")
        self.provider = provider
        self._provider_factory = provider_factory
        self.cache = cache
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(int(max_workers), 4)),
            thread_name_prefix="artist-images",
        )
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, bool, bool], _PendingRequest] = {}
        self._generation = 0
        self._closed = False
        self._completed.connect(self._deliver)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _resolve_job(
        self,
        identity: ArtistIdentity,
        *,
        force: bool,
        network_enabled: bool,
        cancel_event: threading.Event,
        generation: int,
    ) -> ArtistImageResult:
        cached = (
            None
            if force and network_enabled
            else self.cache.lookup(identity, repair=network_enabled)
        )
        if cached is not None:
            return cached
        if not network_enabled:
            return ArtistImageResult(ArtistImageStatus.DISABLED, identity)
        if cancel_event.is_set():
            raise CancelledError()
        provider = self.provider
        if provider is None:
            with self._lock:
                provider = self.provider
                if provider is None:
                    factory = self._provider_factory
                    if factory is None:
                        return ArtistImageResult(ArtistImageStatus.DISABLED, identity)
                    provider = factory()
                    self.provider = provider
        result = provider.resolve(identity, cancel_event)
        with self._lock:
            if cancel_event.is_set() or generation != self._generation or self._closed:
                raise CancelledError()
            try:
                return self.cache.store(result)
            except ArtistImageContentError as exc:
                return self.cache.store(
                    ArtistImageResult(
                        ArtistImageStatus.UNAVAILABLE,
                        identity,
                        error_code=_safe_error_code(exc),
                    )
                )

    def request(
        self,
        display_name: object,
        callback: Callable[[ArtistImageResult], None] | None = None,
        *,
        force: bool = False,
        network_enabled: bool = True,
        discogs_artist_id: object = None,
        musicbrainz_artist_id: object = None,
    ) -> bool:
        """Queue a lookup; return False only when coalesced with an existing job."""
        identity = (
            display_name
            if isinstance(display_name, ArtistIdentity)
            else ArtistIdentity.from_display_name(
                display_name,
                discogs_artist_id=discogs_artist_id,
                musicbrainz_artist_id=musicbrainz_artist_id,
            )
        )
        key = (identity.cache_identity, bool(force), bool(network_enabled))
        with self._lock:
            if self._closed:
                return False
            existing = self._pending.get(key)
            if existing is not None:
                if callback is not None:
                    existing.callbacks.append(callback)
                return False
            cancel_event = threading.Event()
            generation = self._generation
            if network_enabled:
                record_provider_task_dispatch()
            future = self._executor.submit(
                self._resolve_job,
                identity,
                force=bool(force),
                network_enabled=bool(network_enabled),
                cancel_event=cancel_event,
                generation=generation,
            )
            pending = _PendingRequest(
                future,
                cancel_event,
                [callback] if callback is not None else [],
                generation,
            )
            self._pending[key] = pending

        def completed(completed_future: Future[ArtistImageResult]) -> None:
            try:
                result = completed_future.result()
            except CancelledError:
                return
            except Exception:
                result = ArtistImageResult(
                    ArtistImageStatus.TEMPORARY_ERROR,
                    identity,
                    error_code="service_error",
                )
            try:
                self._completed.emit(key, result, generation)
            except RuntimeError:
                pass

        future.add_done_callback(completed)
        return True

    @Slot(object, object, int)
    def _deliver(
        self,
        key: tuple[str, bool, bool],
        result: ArtistImageResult,
        generation: int,
    ) -> None:
        with self._lock:
            pending = self._pending.pop(key, None)
            if (
                pending is None
                or self._closed
                or generation != self._generation
                or generation != pending.generation
            ):
                return
            callbacks = tuple(pending.callbacks)
        self.result_ready.emit(result.identity.normalized_key, result)
        for callback in callbacks:
            try:
                callback(result)
            except Exception:
                pass

    def cancel_all(self) -> None:
        with self._lock:
            self._generation += 1
            pending = tuple(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.cancel_event.set()
            request.future.cancel()

    def clear_cache(self, identity: ArtistIdentity | None = None) -> None:
        self.cancel_all()
        self.cache.clear(identity)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.cancel_all()
        self._executor.shutdown(wait=False, cancel_futures=True)
