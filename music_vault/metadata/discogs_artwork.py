"""Gap-only Discogs release-artwork retrieval and private runtime caching.

Discogs images are restricted provider content.  This module therefore has a
deliberately narrow boundary: it accepts only the front image attached to the
already accepted release candidate, never replaces valid/manual/locked art,
never embeds an image in media, and stores only private runtime cache files
plus the attribution needed to display them responsibly.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from music_vault.core.paths import discogs_covers_dir
from music_vault.metadata.artwork import (
    MAX_ARTWORK_BYTES,
    MAX_ARTWORK_PIXELS,
    ArtworkError,
    PreparedArtwork,
    prepare_artwork_bytes,
    prepare_local_artwork,
)
from music_vault.metadata.providers import ProviderArtworkCandidate
from music_vault.metadata.providers.discogs import (
    DISCOGS_ATTRIBUTION_TEXT,
    DISCOGS_USER_AGENT,
)


DISCOGS_ARTWORK_CACHE_SCHEMA_VERSION = 1
DISCOGS_IMAGE_HOSTS = frozenset({"i.discogs.com", "api-img.discogs.com"})
DISCOGS_PAGE_HOSTS = frozenset({"discogs.com", "www.discogs.com"})
DISCOGS_ARTWORK_MAX_AGE = timedelta(hours=6)
MAX_ARTWORK_EDGE = 10_000
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
MINIMUM_ACCEPTED_SCORE = 85.0
ATTRIBUTION_STATE = "required_visible"

_RELEASE_ID_RE = re.compile(r"^[1-9]\d{0,17}$")


class DiscogsArtworkError(RuntimeError):
    """A sanitized Discogs-image failure safe for logs and UI summaries."""


@dataclass(frozen=True)
class DiscogsArtworkRecord:
    """Validated runtime art and the attribution required to display it."""

    path: Path
    release_id: str
    sha256: str
    mime_type: str
    width: int
    height: int
    source_url: str
    delivery_url: str
    provider_page_url: str
    fetched_at: str
    last_validated_at: str
    attribution_text: str = DISCOGS_ATTRIBUTION_TEXT
    attribution_state: str = ATTRIBUTION_STATE
    from_cache: bool = False

    @property
    def attribution_url(self) -> str:
        return self.provider_page_url


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


def _global_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def validate_discogs_image_url(
    value: object,
    *,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> str:
    """Accept only HTTPS Discogs image-delivery hosts and public DNS answers."""

    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise DiscogsArtworkError("discogs_artwork_url_invalid") from exc
    host = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme.casefold() != "https":
        raise DiscogsArtworkError("discogs_artwork_https_required")
    if parsed.username is not None or parsed.password is not None:
        raise DiscogsArtworkError("discogs_artwork_userinfo_rejected")
    if port not in (None, 443):
        raise DiscogsArtworkError("discogs_artwork_port_rejected")
    if host not in DISCOGS_IMAGE_HOSTS:
        raise DiscogsArtworkError("discogs_artwork_host_rejected")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DiscogsArtworkError("discogs_artwork_ip_literal_rejected")
    normalized = urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    if not resolve_dns:
        return normalized
    try:
        answers = resolver(host, 443, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise DiscogsArtworkError("discogs_artwork_dns_unavailable") from exc
    addresses: list[str] = []
    for answer in answers:
        try:
            addresses.append(str(answer[4][0]).split("%", 1)[0])
        except (IndexError, TypeError) as exc:
            raise DiscogsArtworkError("discogs_artwork_dns_invalid") from exc
    if not addresses or any(not _global_address(address) for address in addresses):
        raise DiscogsArtworkError("discogs_artwork_private_address_rejected")
    return normalized


def validate_discogs_release_url(value: object, release_id: object) -> str:
    """Validate a normal, user-visible Discogs release attribution page."""

    identity = str(release_id or "").strip()
    if not _RELEASE_ID_RE.fullmatch(identity):
        raise DiscogsArtworkError("discogs_artwork_release_invalid")
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise DiscogsArtworkError("discogs_artwork_attribution_invalid") from exc
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or host not in DISCOGS_PAGE_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not re.fullmatch(rf"/release/{re.escape(identity)}(?:-[^/?#]+)?/?", parsed.path)
    ):
        raise DiscogsArtworkError("discogs_artwork_attribution_invalid")
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def is_true_artwork_gap(
    cover_path: str | Path | None,
    *,
    placeholder: bool = False,
    manual: bool = False,
    locked: bool = False,
) -> bool:
    """Return whether restricted Discogs art may fill this exact cover state.

    Manual and locked artwork remain authoritative even if the caller's path
    has subsequently become unavailable.  Other absent, missing, explicitly
    placeholder, or undecodable references are true gaps.
    """

    if manual or locked:
        return False
    if placeholder:
        return True
    text = str(cover_path or "").strip()
    if not text:
        return True
    try:
        prepare_local_artwork(Path(text))
    except ArtworkError:
        return True
    return False


class DiscogsArtworkCache:
    """Private content-addressed cache for accepted release-front images."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        session: requests.Session | None = None,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        clock: Callable[[], datetime] = _utc_now,
        max_age: timedelta = DISCOGS_ARTWORK_MAX_AGE,
    ) -> None:
        self.root = Path(root if root is not None else discogs_covers_dir()).expanduser().resolve()
        self.index_path = (self.root / "index.json").resolve()
        if self.index_path.parent != self.root:
            raise ValueError("Discogs artwork index must remain inside its cache root.")
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.resolver = resolver
        self.clock = clock
        self.max_age = min(max_age, DISCOGS_ARTWORK_MAX_AGE)
        if self.max_age <= timedelta(0):
            raise ValueError("Discogs artwork max_age must be positive.")
        self._manifest: dict[str, Any] | None = None
        self._lock = threading.RLock()

    def _empty_manifest(self) -> dict[str, Any]:
        return {"schema_version": DISCOGS_ARTWORK_CACHE_SCHEMA_VERSION, "entries": {}}

    def _load(self) -> dict[str, Any]:
        if self._manifest is not None:
            return self._manifest
        try:
            if self.index_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("oversized manifest")
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != DISCOGS_ARTWORK_CACHE_SCHEMA_VERSION
                or not isinstance(payload.get("entries"), dict)
            ):
                raise ValueError("unsupported manifest")
            self._manifest = payload
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
        except OSError as exc:
            raise DiscogsArtworkError("discogs_artwork_cache_unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _safe_cache_path(self, value: object) -> Path | None:
        name = str(value or "")
        if not name or Path(name).name != name or name in {".", "..", "index.json"}:
            return None
        path = (self.root / name).resolve()
        return path if path.parent == self.root else None

    @staticmethod
    def _validated_prepared(payload: bytes, content_type: str) -> PreparedArtwork:
        try:
            prepared = prepare_artwork_bytes(
                payload,
                content_type,
                max_bytes=MAX_ARTWORK_BYTES,
                max_pixels=MAX_ARTWORK_PIXELS,
            )
        except ArtworkError as exc:
            raise DiscogsArtworkError(str(exc)) from exc
        if prepared.width > MAX_ARTWORK_EDGE or prepared.height > MAX_ARTWORK_EDGE:
            raise DiscogsArtworkError("artwork_dimensions_rejected")
        return prepared

    def _record_from_manifest(
        self,
        release_id: str,
        raw: Mapping[str, Any],
        *,
        require_fresh: bool,
    ) -> DiscogsArtworkRecord | None:
        fetched = _parse_iso(raw.get("fetched_at"))
        validated_at = _parse_iso(raw.get("last_validated_at"))
        if fetched is None or validated_at is None:
            return None
        now = self.clock().astimezone(timezone.utc)
        if require_fresh and now - validated_at > self.max_age:
            return None
        path = self._safe_cache_path(raw.get("cache_file"))
        if path is None or not path.is_file() or path.is_symlink():
            return None
        source_url = validate_discogs_image_url(raw.get("source_url"), resolve_dns=False)
        delivery_url = validate_discogs_image_url(
            raw.get("delivery_url", raw.get("source_url")), resolve_dns=False
        )
        provider_page_url = validate_discogs_release_url(
            raw.get("provider_page_url"), release_id
        )
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        prepared = self._validated_prepared(payload, str(raw.get("mime_type") or ""))
        if prepared.sha256 != str(raw.get("sha256") or "") or path.name != (
            f"{prepared.sha256}{prepared.extension}"
        ):
            return None
        return DiscogsArtworkRecord(
            path=path,
            release_id=release_id,
            sha256=prepared.sha256,
            mime_type=prepared.mime_type,
            width=prepared.width,
            height=prepared.height,
            source_url=source_url,
            delivery_url=delivery_url,
            provider_page_url=provider_page_url,
            fetched_at=_iso(fetched),
            last_validated_at=_iso(validated_at),
            attribution_text=DISCOGS_ATTRIBUTION_TEXT,
            attribution_state=ATTRIBUTION_STATE,
            from_cache=True,
        )

    def lookup(
        self,
        candidate: ProviderArtworkCandidate,
        *,
        require_fresh: bool = True,
    ) -> DiscogsArtworkRecord | None:
        """Return a validated matching cache entry, never unrelated release art."""

        release_id = str(candidate.release_id or "").strip()
        with self._lock:
            raw = self._load()["entries"].get(release_id)
            if not isinstance(raw, Mapping):
                return None
            try:
                record = self._record_from_manifest(
                    release_id, raw, require_fresh=require_fresh
                )
            except DiscogsArtworkError:
                return None
            if record is None:
                return None
            try:
                candidate_source = validate_discogs_image_url(
                    candidate.source_url, resolve_dns=False
                )
                candidate_page = validate_discogs_release_url(
                    candidate.provider_page_url, release_id
                )
            except DiscogsArtworkError:
                return None
            if record.source_url != candidate_source or record.provider_page_url != candidate_page:
                return None
            return record

    @staticmethod
    def _read_limited(response: Any) -> bytes:
        length = response.headers.get("Content-Length")
        if length:
            try:
                parsed_length = int(length)
            except (TypeError, ValueError) as exc:
                raise DiscogsArtworkError("discogs_artwork_length_invalid") from exc
            if parsed_length < 0:
                raise DiscogsArtworkError("discogs_artwork_length_invalid")
            if parsed_length > MAX_ARTWORK_BYTES:
                raise DiscogsArtworkError("discogs_artwork_response_too_large")
        payload = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    payload.extend(chunk)
                    if len(payload) > MAX_ARTWORK_BYTES:
                        raise DiscogsArtworkError("discogs_artwork_response_too_large")
        except DiscogsArtworkError:
            raise
        except requests.RequestException as exc:
            raise DiscogsArtworkError("discogs_artwork_request_failed") from exc
        return bytes(payload)

    def _download(self, source_url: str) -> tuple[PreparedArtwork, str]:
        current_url = source_url
        for redirect_count in range(MAX_REDIRECTS + 1):
            current_url = validate_discogs_image_url(
                current_url, resolver=self.resolver
            )
            try:
                response = self.session.get(
                    current_url,
                    headers={"User-Agent": DISCOGS_USER_AGENT, "Accept": "image/*"},
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    stream=True,
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise DiscogsArtworkError("discogs_artwork_unavailable") from exc
            except requests.RequestException as exc:
                raise DiscogsArtworkError("discogs_artwork_request_failed") from exc
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location or redirect_count >= MAX_REDIRECTS:
                        raise DiscogsArtworkError("discogs_artwork_redirect_rejected")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code == 404:
                    raise DiscogsArtworkError("discogs_artwork_not_found")
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    raise DiscogsArtworkError("discogs_artwork_unavailable")
                if not 200 <= response.status_code <= 299:
                    raise DiscogsArtworkError("discogs_artwork_request_rejected")
                content_type = str(response.headers.get("Content-Type") or "")
                payload = self._read_limited(response)
                return self._validated_prepared(payload, content_type), current_url
            finally:
                response.close()
        raise DiscogsArtworkError("discogs_artwork_redirect_rejected")

    def _store(
        self,
        candidate: ProviderArtworkCandidate,
        prepared: PreparedArtwork,
        *,
        validated_source_url: str,
    ) -> DiscogsArtworkRecord:
        release_id = str(candidate.release_id).strip()
        source_url = validate_discogs_image_url(candidate.source_url, resolve_dns=False)
        provider_page_url = validate_discogs_release_url(
            candidate.provider_page_url, release_id
        )
        self.root.mkdir(parents=True, exist_ok=True)
        destination = (self.root / f"{prepared.sha256}{prepared.extension}").resolve()
        if destination.parent != self.root:
            raise DiscogsArtworkError("discogs_artwork_cache_unavailable")
        if destination.exists():
            if destination.is_symlink():
                raise DiscogsArtworkError("discogs_artwork_cache_unavailable")
            try:
                if destination.read_bytes() != prepared.data:
                    raise DiscogsArtworkError("discogs_artwork_hash_collision")
            except OSError as exc:
                raise DiscogsArtworkError("discogs_artwork_cache_unavailable") from exc
        else:
            temporary = self.root / f".{prepared.sha256}-{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as stream:
                    stream.write(prepared.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            except OSError as exc:
                raise DiscogsArtworkError("discogs_artwork_cache_unavailable") from exc
            finally:
                temporary.unlink(missing_ok=True)

        now = _iso(self.clock())
        raw = {
            "release_id": release_id,
            "cache_file": destination.name,
            "sha256": prepared.sha256,
            "mime_type": prepared.mime_type,
            "width": prepared.width,
            "height": prepared.height,
            "source_url": source_url,
            "delivery_url": validated_source_url,
            "provider_page_url": provider_page_url,
            "fetched_at": now,
            "last_validated_at": now,
            "attribution_text": DISCOGS_ATTRIBUTION_TEXT,
            "attribution_state": ATTRIBUTION_STATE,
        }
        self._load()["entries"][release_id] = raw
        self._write_manifest()
        return DiscogsArtworkRecord(
            destination,
            release_id,
            prepared.sha256,
            prepared.mime_type,
            prepared.width,
            prepared.height,
            source_url,
            validated_source_url,
            provider_page_url,
            now,
            now,
        )

    def fetch_for_gap(
        self,
        candidate: ProviderArtworkCandidate,
        *,
        accepted_release_id: object,
        provider_score: float,
        current_cover_path: str | Path | None,
        placeholder: bool = False,
        manual: bool = False,
        locked: bool = False,
    ) -> DiscogsArtworkRecord | None:
        """Fetch/store the accepted front image only when a true gap exists."""

        if not is_true_artwork_gap(
            current_cover_path,
            placeholder=placeholder,
            manual=manual,
            locked=locked,
        ):
            return None
        accepted_id = str(accepted_release_id or "").strip()
        if (
            not _RELEASE_ID_RE.fullmatch(accepted_id)
            or str(candidate.release_id or "").strip() != accepted_id
        ):
            raise DiscogsArtworkError("discogs_artwork_release_mismatch")
        try:
            score = float(provider_score)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DiscogsArtworkError("discogs_artwork_match_not_confident") from exc
        if not math.isfinite(score) or score < MINIMUM_ACCEPTED_SCORE:
            raise DiscogsArtworkError("discogs_artwork_match_not_confident")
        if not candidate.catalogue_image:
            raise DiscogsArtworkError("discogs_artwork_catalogue_image_required")
        if not candidate.is_front:
            raise DiscogsArtworkError("discogs_artwork_front_required")
        if candidate.width and candidate.height:
            if (
                candidate.width > MAX_ARTWORK_EDGE
                or candidate.height > MAX_ARTWORK_EDGE
                or candidate.width * candidate.height > MAX_ARTWORK_PIXELS
            ):
                raise DiscogsArtworkError("artwork_dimensions_rejected")
        validate_discogs_release_url(candidate.provider_page_url, accepted_id)
        validate_discogs_image_url(candidate.source_url, resolve_dns=False)
        source_url = validate_discogs_image_url(
            candidate.source_url, resolver=self.resolver
        )
        with self._lock:
            cached = self.lookup(candidate)
            if cached is not None:
                return cached
            prepared, final_url = self._download(source_url)
            return self._store(
                candidate, prepared, validated_source_url=final_url
            )

    def attribution_for_release(self, release_id: object) -> tuple[str, str] | None:
        """Return the normal Discogs attribution label/link for cached art."""

        identity = str(release_id or "").strip()
        with self._lock:
            raw = self._load()["entries"].get(identity)
            if not isinstance(raw, Mapping):
                return None
            try:
                url = validate_discogs_release_url(raw.get("provider_page_url"), identity)
            except DiscogsArtworkError:
                return None
            return DISCOGS_ATTRIBUTION_TEXT, url


__all__ = [
    "ATTRIBUTION_STATE",
    "DISCOGS_ARTWORK_CACHE_SCHEMA_VERSION",
    "DISCOGS_IMAGE_HOSTS",
    "DiscogsArtworkCache",
    "DiscogsArtworkError",
    "DiscogsArtworkRecord",
    "is_true_artwork_gap",
    "validate_discogs_image_url",
    "validate_discogs_release_url",
]
