from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader, QImageWriter

from music_vault.core.paths import cover_art_archive_dir, manual_covers_dir


MAX_ARTWORK_BYTES = 8 * 1024 * 1024
MAX_ARTWORK_PIXELS = 25_000_000
MAX_EMBEDDED_ARTWORK_DIMENSION = 1200
TARGET_EMBEDDED_ARTWORK_BYTES = 1_500_000
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
MUSIC_VAULT_USER_AGENT = "MusicVault/1.0.0 (https://github.com/jcjohnson9/music-vault)"

_MIME_FORMATS = {
    "image/png": (".png", frozenset({"png"})),
    "image/jpeg": (".jpg", frozenset({"jpeg", "jpg"})),
    "image/webp": (".webp", frozenset({"webp"})),
}
_RELEASE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


class ArtworkError(RuntimeError):
    """A deliberately sanitized artwork validation/provider failure."""


@dataclass(frozen=True)
class PreparedArtwork:
    data: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    sha256: str


def _decode_prepared_artwork(artwork: PreparedArtwork) -> QImage:
    byte_array = QByteArray(artwork.data)
    buffer = QBuffer()
    buffer.setData(byte_array)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ArtworkError("artwork_decode_failed")
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    image = reader.read()
    buffer.close()
    if image.isNull():
        raise ArtworkError("artwork_decode_failed")
    return image


def _has_visible_transparency(image: QImage) -> bool:
    """Return whether any decoded pixel is not fully opaque."""

    if not image.hasAlphaChannel():
        return False
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = rgba.constBits()
    width = rgba.width()
    stride = rgba.bytesPerLine()
    for row in range(rgba.height()):
        start = row * stride + 3
        alpha = bytes(bits[start : start + width * 4 : 4])
        if alpha.count(255) != width:
            return True
    return False


def _encode_image(image: QImage, mime_type: str, *, quality: int = 88) -> bytes:
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise ArtworkError("artwork_encode_failed")
    format_name = b"PNG" if mime_type == "image/png" else b"JPEG"
    writer = QImageWriter(buffer, format_name)
    if mime_type == "image/png":
        # Qt's PNG plugin uses its generic 0-100 compression scale rather
        # than zlib's 0-9 scale. The maximum is deterministic and compact.
        writer.setCompression(100)
    else:
        writer.setQuality(max(1, min(100, int(quality))))
        writer.setOptimizedWrite(True)
    if not writer.write(image):
        buffer.close()
        raise ArtworkError("artwork_encode_failed")
    buffer.close()
    return bytes(encoded)


def _scale_down(image: QImage, max_dimension: int) -> QImage:
    if max(image.width(), image.height()) <= max_dimension:
        return image
    return image.scaled(
        max_dimension,
        max_dimension,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def normalize_artwork_for_embedding(
    artwork: PreparedArtwork,
    *,
    max_dimension: int = MAX_EMBEDDED_ARTWORK_DIMENSION,
    target_bytes: int = TARGET_EMBEDDED_ARTWORK_BYTES,
) -> PreparedArtwork:
    """Create deterministic, bounded artwork suitable for media-file tags.

    Transparent images remain lossless PNG. Opaque images use an efficient
    JPEG representation. Images are never enlarged, and exceptionally dense
    images are reduced further when encoding quality alone cannot meet the
    requested byte target.
    """

    if isinstance(max_dimension, bool) or int(max_dimension) <= 0:
        raise ValueError("max_dimension must be a positive integer.")
    if isinstance(target_bytes, bool) or int(target_bytes) <= 0:
        raise ValueError("target_bytes must be a positive integer.")
    max_dimension = int(max_dimension)
    target_bytes = int(target_bytes)

    # Do not trust a caller-constructed dataclass. Revalidate the bytes and
    # content type before decoding or preserving the original representation.
    validated = prepare_artwork_bytes(artwork.data, artwork.mime_type)
    image = _scale_down(_decode_prepared_artwork(validated), max_dimension)
    transparent = _has_visible_transparency(image)
    output_mime = "image/png" if transparent else "image/jpeg"

    if (
        validated.mime_type == output_mime
        and validated.width == image.width()
        and validated.height == image.height()
        and len(validated.data) <= target_bytes
    ):
        return validated

    if output_mime == "image/png":
        working = image.convertToFormat(QImage.Format.Format_RGBA8888)
        payload = _encode_image(working, output_mime)
    else:
        working = image.convertToFormat(QImage.Format.Format_RGB888)
        payload = b""
        for quality in (88, 82, 76, 70, 64, 58, 52, 46, 40):
            payload = _encode_image(working, output_mime, quality=quality)
            if len(payload) <= target_bytes:
                break

    # PNG cannot trade quality for bytes, and unusually dense JPEGs may still
    # miss a very small caller-supplied target. Reduce dimensions gradually,
    # always preserving aspect ratio and never enlarging.
    attempts = 0
    while len(payload) > target_bytes and max(working.width(), working.height()) > 96:
        attempts += 1
        if attempts > 8:
            break
        ratio = max(0.5, min(0.9, (target_bytes / len(payload)) ** 0.5 * 0.95))
        next_edge = max(96, int(max(working.width(), working.height()) * ratio))
        if next_edge >= max(working.width(), working.height()):
            next_edge = max(96, max(working.width(), working.height()) - 1)
        working = _scale_down(working, next_edge)
        if output_mime == "image/png":
            payload = _encode_image(working, output_mime)
        else:
            payload = _encode_image(working, output_mime, quality=40)

    # Re-run the public validation boundary so dimensions, MIME/magic, decode,
    # and the returned content hash describe the exact embedded payload.
    return prepare_artwork_bytes(payload, output_mime)


def _mime_from_payload(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def prepare_artwork_bytes(
    payload: bytes,
    content_type: str | None = None,
    *,
    max_bytes: int = MAX_ARTWORK_BYTES,
    max_pixels: int = MAX_ARTWORK_PIXELS,
) -> PreparedArtwork:
    if not payload or len(payload) > max_bytes:
        raise ArtworkError("artwork_size_rejected")
    detected_mime = _mime_from_payload(payload)
    supplied_mime = str(content_type or "").split(";", 1)[0].strip().casefold()
    mime = supplied_mime or detected_mime
    if mime not in _MIME_FORMATS or detected_mime is None:
        raise ArtworkError("artwork_type_rejected")
    if supplied_mime and supplied_mime != detected_mime:
        raise ArtworkError("artwork_type_mismatch")

    byte_array = QByteArray(payload)
    buffer = QBuffer()
    buffer.setData(byte_array)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ArtworkError("artwork_decode_failed")
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        buffer.close()
        raise ArtworkError("artwork_dimensions_invalid")
    if size.width() * size.height() > max_pixels:
        buffer.close()
        raise ArtworkError("artwork_dimensions_rejected")
    extension, accepted_formats = _MIME_FORMATS[mime]
    detected_format = bytes(reader.format()).decode("ascii", errors="ignore").casefold()
    if detected_format not in accepted_formats:
        buffer.close()
        raise ArtworkError("artwork_type_mismatch")
    image = reader.read()
    buffer.close()
    if image.isNull():
        raise ArtworkError("artwork_decode_failed")
    return PreparedArtwork(
        data=bytes(payload),
        mime_type=mime,
        extension=extension,
        width=image.width(),
        height=image.height(),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def prepare_local_artwork(path: str | Path) -> PreparedArtwork:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ArtworkError("artwork_unavailable") from exc
    if size <= 0 or size > MAX_ARTWORK_BYTES:
        raise ArtworkError("artwork_size_rejected")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ArtworkError("artwork_unavailable") from exc
    return prepare_artwork_bytes(payload)


def store_prepared_artwork(
    artwork: PreparedArtwork,
    *,
    provider: str = "manual",
) -> Path:
    if provider == "manual":
        folder = manual_covers_dir()
    elif provider == "cover_art_archive":
        folder = cover_art_archive_dir()
    else:
        raise ArtworkError("artwork_provider_rejected")
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{artwork.sha256}{artwork.extension}"
    if destination.exists():
        try:
            if destination.read_bytes() == artwork.data:
                return destination.resolve()
        except OSError as exc:
            raise ArtworkError("artwork_cache_unavailable") from exc
        raise ArtworkError("artwork_hash_collision")
    temporary = folder / f".{artwork.sha256}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(artwork.data)
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ArtworkError("artwork_cache_unavailable") from exc
    return destination.resolve()


def _global_address(value: str) -> bool:
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


def _allowed_cover_host(hostname: str) -> bool:
    host = hostname.rstrip(".").casefold()
    return host in {"coverartarchive.org", "archive.org"} or host.endswith(".archive.org")


def validate_cover_art_url(
    value: object,
    *,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ArtworkError("artwork_url_invalid") from exc
    host = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme.casefold() != "https":
        raise ArtworkError("artwork_https_required")
    if parsed.username is not None or parsed.password is not None:
        raise ArtworkError("artwork_userinfo_rejected")
    if port not in (None, 443):
        raise ArtworkError("artwork_port_rejected")
    if not _allowed_cover_host(host):
        raise ArtworkError("artwork_domain_rejected")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ArtworkError("artwork_ip_literal_rejected")
    validated = urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    if not resolve_dns:
        return validated
    try:
        answers = resolver(host, 443, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise ArtworkError("artwork_dns_unavailable") from exc
    addresses = []
    for answer in answers:
        try:
            addresses.append(str(answer[4][0]).split("%", 1)[0])
        except (IndexError, TypeError) as exc:
            raise ArtworkError("artwork_dns_invalid") from exc
    if not addresses or any(not _global_address(address) for address in addresses):
        raise ArtworkError("artwork_private_address_rejected")
    return validated


class CoverArtArchiveProvider:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    ) -> None:
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.resolver = resolver

    @staticmethod
    def _release_id(value: object) -> str:
        release_id = str(value or "").strip()
        if not _RELEASE_ID_RE.fullmatch(release_id):
            raise ArtworkError("release_id_invalid")
        return release_id.casefold()

    @staticmethod
    def _read_limited(response: Any) -> bytes:
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_ARTWORK_BYTES:
                    raise ArtworkError("artwork_response_too_large")
            except ValueError as exc:
                raise ArtworkError("artwork_content_length_invalid") from exc
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                body.extend(chunk)
                if len(body) > MAX_ARTWORK_BYTES:
                    raise ArtworkError("artwork_response_too_large")
        return bytes(body)

    def fetch(self, release_id: str) -> PreparedArtwork | None:
        release_id = self._release_id(release_id)
        current_url = f"https://coverartarchive.org/release/{release_id}/front-500"
        for redirect_count in range(MAX_REDIRECTS + 1):
            current_url = validate_cover_art_url(current_url, resolver=self.resolver)
            try:
                response = self.session.get(
                    current_url,
                    headers={"User-Agent": MUSIC_VAULT_USER_AGENT, "Accept": "image/*"},
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    stream=True,
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise ArtworkError("artwork_network_unavailable") from exc
            except requests.RequestException as exc:
                raise ArtworkError("artwork_request_failed") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                if not location or redirect_count >= MAX_REDIRECTS:
                    raise ArtworkError("artwork_redirect_rejected")
                current_url = urljoin(current_url, location)
                continue
            if response.status_code == 404:
                response.close()
                return None
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                response.close()
                raise ArtworkError("artwork_provider_unavailable")
            if not 200 <= response.status_code <= 299:
                response.close()
                raise ArtworkError("artwork_request_rejected")
            try:
                content_type = response.headers.get("Content-Type", "")
                try:
                    payload = self._read_limited(response)
                except ArtworkError:
                    raise
                except requests.RequestException as exc:
                    raise ArtworkError("artwork_request_failed") from exc
            finally:
                response.close()
            return prepare_artwork_bytes(payload, content_type)
        raise ArtworkError("artwork_redirect_rejected")

    def fetch_and_store(self, release_id: str) -> Path | None:
        prepared = self.fetch(release_id)
        if prepared is None:
            return None
        return store_prepared_artwork(prepared, provider="cover_art_archive")
