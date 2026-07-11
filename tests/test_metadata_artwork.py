from __future__ import annotations

from pathlib import Path

import pytest
import requests
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from music_vault.core import paths
from music_vault.metadata.artwork import (
    ArtworkError,
    CoverArtArchiveProvider,
    prepare_artwork_bytes,
    prepare_local_artwork,
    store_prepared_artwork,
    validate_cover_art_url,
)


RELEASE_ID = "12345678-1234-4234-9234-123456789abc"


def _image_bytes(fmt: str = "PNG", width: int = 24, height: int = 24) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0xFF1DB954)
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt)
    buffer.close()
    return bytes(data)


@pytest.mark.parametrize(
    ("fmt", "mime", "extension"),
    [("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg")],
)
def test_valid_artwork_formats_are_prepared(fmt, mime, extension):
    result = prepare_artwork_bytes(_image_bytes(fmt), mime)
    assert result.extension == extension
    assert result.width == 24 and result.height == 24


def test_webp_is_accepted_when_qt_supports_it():
    payload = _image_bytes("WEBP")
    if not payload:
        pytest.skip("Qt WebP support is unavailable")
    assert prepare_artwork_bytes(payload, "image/webp").extension == ".webp"


def test_invalid_oversized_and_pixel_heavy_artwork_is_rejected():
    with pytest.raises(ArtworkError):
        prepare_artwork_bytes(b"not an image")
    with pytest.raises(ArtworkError):
        prepare_artwork_bytes(_image_bytes(), "image/jpeg")
    with pytest.raises(ArtworkError):
        prepare_artwork_bytes(_image_bytes(), max_bytes=10)
    with pytest.raises(ArtworkError):
        prepare_artwork_bytes(_image_bytes(width=20, height=20), max_pixels=399)


def test_local_artwork_hash_dedupe_and_atomic_store(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    paths._resolved_project_root.cache_clear()
    source = tmp_path / "chosen.png"
    source.write_bytes(_image_bytes())
    prepared = prepare_local_artwork(source)
    first = store_prepared_artwork(prepared)
    second = store_prepared_artwork(prepared)
    assert first == second
    assert first.name == f"{prepared.sha256}.png"
    assert first.parent == runtime / "data" / "covers" / "manual"
    assert list(first.parent.glob("*.tmp")) == []
    paths._resolved_project_root.cache_clear()


class _Response:
    def __init__(self, status=200, *, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=0):
        yield self._body

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _resolver(*_args):
    return [(None, None, None, None, ("93.184.216.34", 443))]


def _private_resolver(*_args):
    return [(None, None, None, None, ("127.0.0.1", 443))]


def _multicast_resolver(*_args):
    return [(None, None, None, None, ("224.0.0.1", 443))]


def test_cover_provider_validates_and_returns_image():
    payload = _image_bytes()
    session = _Session(
        [_Response(headers={"Content-Type": "image/png", "Content-Length": str(len(payload))}, body=payload)]
    )
    provider = CoverArtArchiveProvider(session=session, resolver=_resolver)
    result = provider.fetch(RELEASE_ID)
    assert result and result.extension == ".png"
    assert session.trust_env is False
    assert session.calls[0][1]["allow_redirects"] is False


def test_cover_provider_missing_art_returns_none():
    provider = CoverArtArchiveProvider(session=_Session([_Response(status=404)]), resolver=_resolver)
    assert provider.fetch(RELEASE_ID) is None


def test_cover_provider_rejects_redirect_domain_and_bad_content():
    redirect = _Response(status=302, headers={"Location": "https://example.com/image.png"})
    provider = CoverArtArchiveProvider(session=_Session([redirect]), resolver=_resolver)
    with pytest.raises(ArtworkError, match="domain_rejected"):
        provider.fetch(RELEASE_ID)

    provider = CoverArtArchiveProvider(
        session=_Session([_Response(headers={"Content-Type": "text/html"}, body=b"html")]),
        resolver=_resolver,
    )
    with pytest.raises(ArtworkError):
        provider.fetch(RELEASE_ID)


def test_cover_urls_reject_non_https_unknown_and_private_destinations():
    with pytest.raises(ArtworkError, match="https_required"):
        validate_cover_art_url("http://coverartarchive.org/a", resolve_dns=False)
    with pytest.raises(ArtworkError, match="domain_rejected"):
        validate_cover_art_url("https://example.com/a", resolve_dns=False)
    with pytest.raises(ArtworkError, match="private_address"):
        validate_cover_art_url(
            "https://coverartarchive.org/a",
            resolver=_private_resolver,
        )
    with pytest.raises(ArtworkError, match="private_address"):
        validate_cover_art_url(
            "https://coverartarchive.org/a",
            resolver=_multicast_resolver,
        )


def test_cover_provider_sanitizes_stream_failures():
    response = _Response(headers={"Content-Type": "image/png"})

    def broken_stream(*_args, **_kwargs):
        raise requests.exceptions.ChunkedEncodingError("C:/private/path query=secret")

    response.iter_content = broken_stream
    provider = CoverArtArchiveProvider(session=_Session([response]), resolver=_resolver)

    with pytest.raises(ArtworkError, match="^artwork_request_failed$"):
        provider.fetch(RELEASE_ID)
