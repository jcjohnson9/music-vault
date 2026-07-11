from __future__ import annotations

import hashlib

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from music_vault.metadata.artwork import (
    MAX_EMBEDDED_ARTWORK_DIMENSION,
    TARGET_EMBEDDED_ARTWORK_BYTES,
    normalize_artwork_for_embedding,
    prepare_artwork_bytes,
)


def _encoded(image: QImage, format_name: str, quality: int = -1) -> bytes:
    output = QByteArray()
    buffer = QBuffer(output)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, format_name, quality)
    buffer.close()
    return bytes(output)


def test_large_opaque_artwork_scales_to_bounded_efficient_jpeg():
    image = QImage(2400, 1200, QImage.Format.Format_RGB32)
    image.fill(0xFF285078)
    prepared = prepare_artwork_bytes(_encoded(image, "PNG"), "image/png")

    normalized = normalize_artwork_for_embedding(prepared)

    assert normalized.mime_type == "image/jpeg"
    assert (normalized.width, normalized.height) == (
        MAX_EMBEDDED_ARTWORK_DIMENSION,
        MAX_EMBEDDED_ARTWORK_DIMENSION // 2,
    )
    assert len(normalized.data) <= TARGET_EMBEDDED_ARTWORK_BYTES
    assert normalized.sha256 == hashlib.sha256(normalized.data).hexdigest()
    assert prepare_artwork_bytes(normalized.data, normalized.mime_type) == normalized


def test_transparency_uses_png_and_preserves_aspect_ratio_and_alpha():
    image = QImage(1600, 800, QImage.Format.Format_RGBA8888)
    image.fill(0x00000000)
    image.setPixelColor(800, 400, 0x8040A0E0)
    prepared = prepare_artwork_bytes(_encoded(image, "PNG"), "image/png")

    normalized = normalize_artwork_for_embedding(prepared)

    assert normalized.mime_type == "image/png"
    assert (normalized.width, normalized.height) == (1200, 600)
    decoded = QImage.fromData(normalized.data)
    assert not decoded.isNull()
    assert decoded.hasAlphaChannel()
    assert decoded.pixelColor(0, 0).alpha() == 0


def test_small_matching_representation_is_preserved_without_enlargement():
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(0xFF305070)
    payload = _encoded(image, "JPEG", 88)
    prepared = prepare_artwork_bytes(payload, "image/jpeg")

    normalized = normalize_artwork_for_embedding(prepared)

    assert normalized == prepared
    assert (normalized.width, normalized.height) == (320, 180)


def test_normalization_is_deterministic_and_can_reduce_for_a_tight_target():
    width = 700
    height = 700
    pixels = bytes((index * 73 + index // 19) % 256 for index in range(width * height * 3))
    image = QImage(pixels, width, height, width * 3, QImage.Format.Format_RGB888).copy()
    prepared = prepare_artwork_bytes(_encoded(image, "PNG"), "image/png")

    first = normalize_artwork_for_embedding(prepared, target_bytes=45_000)
    second = normalize_artwork_for_embedding(prepared, target_bytes=45_000)

    assert first == second
    assert first.mime_type == "image/jpeg"
    assert len(first.data) <= 45_000
    assert first.width <= width and first.height <= height


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_dimension": 0}, "max_dimension"),
        ({"target_bytes": 0}, "target_bytes"),
    ],
)
def test_normalization_rejects_invalid_limits(kwargs, message):
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0xFF000000)
    prepared = prepare_artwork_bytes(_encoded(image, "PNG"), "image/png")

    with pytest.raises(ValueError, match=message):
        normalize_artwork_for_embedding(prepared, **kwargs)
