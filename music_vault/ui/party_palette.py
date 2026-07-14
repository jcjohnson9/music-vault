"""Deterministic artwork palettes for Party Mode visuals.

The extractor deliberately works from a small, bounded image sample.  It is
safe to call from UI code: malformed or missing artwork returns the same
high-contrast fallback palette instead of raising an image-decoding error.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from threading import RLock
from typing import TypeAlias

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage, QImageReader


RGB: TypeAlias = tuple[int, int, int]
_SAMPLE_EDGE = 64
_MAX_INLINE_ARTWORK_BYTES = 32 * 1024 * 1024


def _channel(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(255, number))


def normalize_color(color: tuple[object, object, object] | QColor) -> RGB:
    """Return a bounded RGB tuple for a tuple or ``QColor`` value."""

    if isinstance(color, QColor):
        return color.red(), color.green(), color.blue()
    return _channel(color[0]), _channel(color[1]), _channel(color[2])


def color_hex(color: RGB) -> str:
    red, green, blue = normalize_color(color)
    return f"#{red:02x}{green:02x}{blue:02x}"


def interpolate_color(start: RGB, end: RGB, amount: float) -> RGB:
    """Linearly interpolate two colours, clamping ``amount`` to ``0..1``."""

    try:
        ratio = float(amount)
    except (TypeError, ValueError, OverflowError):
        ratio = 0.0
    if not math.isfinite(ratio):
        ratio = 0.0
    ratio = max(0.0, min(1.0, ratio))
    left = normalize_color(start)
    right = normalize_color(end)
    return tuple(
        _channel(round(a + ((b - a) * ratio))) for a, b in zip(left, right)
    )  # type: ignore[return-value]


def _linear_channel(value: int) -> float:
    component = value / 255.0
    if component <= 0.04045:
        return component / 12.92
    return ((component + 0.055) / 1.055) ** 2.4


def relative_luminance(color: RGB) -> float:
    red, green, blue = normalize_color(color)
    return (
        (0.2126 * _linear_channel(red))
        + (0.7152 * _linear_channel(green))
        + (0.0722 * _linear_channel(blue))
    )


def contrast_ratio(first: RGB, second: RGB) -> float:
    """Return the WCAG contrast ratio between two RGB colours."""

    high, low = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True
    )
    return (high + 0.05) / (low + 0.05)


def ensure_contrast(foreground: RGB, background: RGB, minimum: float = 4.5) -> RGB:
    """Move a foreground toward black or white until contrast is sufficient."""

    foreground = normalize_color(foreground)
    background = normalize_color(background)
    minimum = max(1.0, min(21.0, float(minimum)))
    if contrast_ratio(foreground, background) >= minimum:
        return foreground

    targets = ((248, 250, 255), (4, 6, 10))
    target = max(targets, key=lambda candidate: contrast_ratio(candidate, background))
    for step in range(1, 21):
        candidate = interpolate_color(foreground, target, step / 20.0)
        if contrast_ratio(candidate, background) >= minimum:
            return candidate
    return target


@dataclass(frozen=True, slots=True)
class ArtworkPalette:
    """A compact colour contract shared by the Party Mode renderers."""

    background: RGB
    surface: RGB
    primary: RGB
    secondary: RGB
    accent: RGB
    foreground: RGB

    def __post_init__(self) -> None:
        for field_name in (
            "background",
            "surface",
            "primary",
            "secondary",
            "accent",
            "foreground",
        ):
            object.__setattr__(self, field_name, normalize_color(getattr(self, field_name)))

    @classmethod
    def fallback(cls) -> "ArtworkPalette":
        return cls(
            background=(6, 9, 16),
            surface=(16, 24, 38),
            primary=(29, 185, 84),
            secondary=(58, 111, 196),
            accent=(139, 92, 246),
            foreground=(248, 249, 255),
        )

    def interpolated(self, other: "ArtworkPalette", amount: float) -> "ArtworkPalette":
        return interpolate_palette(self, other, amount)

    def as_hex(self) -> dict[str, str]:
        return {
            field_name: color_hex(getattr(self, field_name))
            for field_name in (
                "background",
                "surface",
                "primary",
                "secondary",
                "accent",
                "foreground",
            )
        }


DEFAULT_PARTY_PALETTE = ArtworkPalette.fallback()


def interpolate_palette(
    start: ArtworkPalette, end: ArtworkPalette, amount: float
) -> ArtworkPalette:
    """Interpolate every palette role with deterministic integer rounding."""

    return ArtworkPalette(
        background=interpolate_color(start.background, end.background, amount),
        surface=interpolate_color(start.surface, end.surface, amount),
        primary=interpolate_color(start.primary, end.primary, amount),
        secondary=interpolate_color(start.secondary, end.secondary, amount),
        accent=interpolate_color(start.accent, end.accent, amount),
        foreground=interpolate_color(start.foreground, end.foreground, amount),
    )


def _saturation(color: RGB) -> float:
    high = max(color)
    low = min(color)
    return 0.0 if high == 0 else (high - low) / high


def _distance(first: RGB, second: RGB) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first, second)))


def _fit_size(size: QSize) -> QSize:
    width = max(1, size.width())
    height = max(1, size.height())
    scale = min(_SAMPLE_EDGE / width, _SAMPLE_EDGE / height, 1.0)
    return QSize(max(1, round(width * scale)), max(1, round(height * scale)))


class PaletteExtractor:
    """Extract and LRU-cache stable palettes from artwork.

    Paths are keyed by resolved name, byte size, and nanosecond modification
    time. Byte payloads are keyed by SHA-256. Invalid input is intentionally
    cached as the immutable fallback palette.
    """

    def __init__(self, max_cache_entries: int = 128) -> None:
        self.max_cache_entries = max(1, min(1024, int(max_cache_entries)))
        self._cache: OrderedDict[tuple[object, ...], ArtworkPalette] = OrderedDict()
        self._lock = RLock()

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def extract(
        self, source: str | Path | bytes | bytearray | memoryview | QImage | None
    ) -> ArtworkPalette:
        if isinstance(source, (str, Path)):
            key, resolved = self._path_identity(source)
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
            image = self._read_path_sample(resolved) if resolved is not None else None
        else:
            key, image = self._load_sample(source)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

        palette = self._extract_image(image) if image is not None else DEFAULT_PARTY_PALETTE
        with self._lock:
            self._cache[key] = palette
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
        return palette

    def _load_sample(
        self, source: str | Path | bytes | bytearray | memoryview | QImage | None
    ) -> tuple[tuple[object, ...], QImage | None]:
        try:
            if source is None:
                return ("none",), None
            if isinstance(source, QImage):
                image = self._sampled(source)
                return ("image", self._image_digest(image)), image
            if isinstance(source, (bytes, bytearray, memoryview)):
                payload = bytes(source)
                digest = hashlib.sha256(payload).hexdigest()
                if not payload or len(payload) > _MAX_INLINE_ARTWORK_BYTES:
                    return ("bytes-invalid", digest, len(payload)), None
                return ("bytes", digest, len(payload)), self._sampled(QImage.fromData(payload))

            key, resolved = self._path_identity(source)
            return key, self._read_path_sample(resolved) if resolved is not None else None
        except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
            return ("invalid", type(source).__name__), None

    @staticmethod
    def _path_identity(source: str | Path) -> tuple[tuple[object, ...], Path | None]:
        path = Path(source).expanduser()
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except (OSError, RuntimeError):
            return ("missing", str(path)), None
        if not resolved.is_file():
            return ("not-file", str(resolved)), None
        return ("path", str(resolved), stat.st_size, stat.st_mtime_ns), resolved

    @classmethod
    def _read_path_sample(cls, resolved: Path) -> QImage | None:
        reader = QImageReader(str(resolved))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(_fit_size(size))
        return cls._sampled(reader.read())

    @staticmethod
    def _sampled(image: QImage) -> QImage | None:
        if image.isNull():
            return None
        if image.width() > _SAMPLE_EDGE or image.height() > _SAMPLE_EDGE:
            image = image.scaled(
                _fit_size(image.size()),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return image.convertToFormat(QImage.Format.Format_RGBA8888)

    @staticmethod
    def _image_digest(image: QImage | None) -> str:
        if image is None:
            return "null"
        digest = hashlib.sha256()
        digest.update(f"{image.width()}x{image.height()}".encode("ascii"))
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                digest.update(bytes((color.red(), color.green(), color.blue(), color.alpha())))
        return digest.hexdigest()

    @staticmethod
    def _extract_image(image: QImage) -> ArtworkPalette:
        buckets: dict[tuple[int, int, int], list[int]] = {}
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                alpha = color.alpha()
                if alpha < 32:
                    continue
                key = (color.red() >> 4, color.green() >> 4, color.blue() >> 4)
                entry = buckets.setdefault(key, [0, 0, 0, 0])
                entry[0] += alpha
                entry[1] += color.red() * alpha
                entry[2] += color.green() * alpha
                entry[3] += color.blue() * alpha

        if not buckets:
            return DEFAULT_PARTY_PALETTE

        ranked: list[tuple[int, RGB]] = []
        for key, (weight, red, green, blue) in buckets.items():
            del key
            ranked.append(
                (weight, (_channel(red // weight), _channel(green // weight), _channel(blue // weight)))
            )
        ranked.sort(key=lambda item: (-item[0], item[1]))
        dominant = ranked[0][1]

        candidates = [
            (weight, color)
            for weight, color in ranked
            if _saturation(color) >= 0.12 and 0.07 <= relative_luminance(color) <= 0.86
        ] or ranked
        candidates.sort(
            key=lambda item: (
                -(_saturation(item[1]) * math.sqrt(item[0])),
                -item[0],
                item[1],
            )
        )

        primary = candidates[0][1]
        secondary = next(
            (color for _, color in candidates[1:] if _distance(color, primary) >= 72.0),
            interpolate_color(primary, (35, 214, 191), 0.58),
        )
        accent = next(
            (
                color
                for _, color in candidates[1:]
                if _distance(color, primary) >= 88.0 and _distance(color, secondary) >= 64.0
            ),
            interpolate_color(primary, (255, 75, 151), 0.62),
        )

        background = interpolate_color(dominant, (5, 7, 14), 0.78)
        if relative_luminance(background) > 0.10:
            background = interpolate_color(background, (3, 5, 10), 0.52)
        surface = interpolate_color(background, dominant, 0.24)
        primary = ensure_contrast(primary, background, 3.0)
        secondary = ensure_contrast(secondary, background, 3.0)
        accent = ensure_contrast(accent, background, 3.0)
        foreground = ensure_contrast((247, 249, 255), background, 7.0)

        return ArtworkPalette(
            background=background,
            surface=surface,
            primary=primary,
            secondary=secondary,
            accent=accent,
            foreground=foreground,
        )


__all__ = [
    "ArtworkPalette",
    "DEFAULT_PARTY_PALETTE",
    "PaletteExtractor",
    "RGB",
    "color_hex",
    "contrast_ratio",
    "ensure_contrast",
    "interpolate_color",
    "interpolate_palette",
    "relative_luminance",
]
