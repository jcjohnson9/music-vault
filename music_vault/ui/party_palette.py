"""Deterministic artwork palettes for Party Mode visuals.

The extractor deliberately works from a small, bounded image sample.  It is
safe to call from UI code: malformed or missing artwork returns the same
high-contrast fallback palette instead of raising an image-decoding error.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
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


def _finite(value: object, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if math.isfinite(result) else fallback


def rgb_to_oklch(color: RGB) -> tuple[float, float, float]:
    """sRGB to perceptual lightness/chroma/hue (hue in degrees)."""
    r, g, b = (_linear_channel(value) for value in normalize_color(color))
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    lightness = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    b_axis = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    chroma = math.hypot(a, b_axis)
    return lightness, chroma, math.degrees(math.atan2(b_axis, a)) % 360 if chroma > 0.0005 else 0.0


def oklch_to_rgb(lightness: float, chroma: float, hue: float) -> RGB:
    """Map OKLCH into sRGB by reducing chroma, never clipping the hue."""
    lightness = max(0.0, min(1.0, _finite(lightness)))
    chroma = max(0.0, min(0.4, _finite(chroma)))
    radians = math.radians(_finite(hue) % 360)

    def linear(c: float) -> tuple[float, float, float]:
        a, b = c * math.cos(radians), c * math.sin(radians)
        l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
        return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

    channels = linear(chroma)
    if not all(-1e-8 <= value <= 1.0 + 1e-8 for value in channels):
        low, high = 0.0, chroma
        for _ in range(18):
            middle = (low + high) / 2
            if all(-1e-8 <= value <= 1.0 + 1e-8 for value in linear(middle)):
                low = middle
            else:
                high = middle
        channels = linear(low)
    return tuple(_channel(round(255 * (12.92 * value if value <= 0.0031308
                                       else 1.055 * value ** (1 / 2.4) - 0.055)))
                 for value in channels)  # type: ignore[return-value]


def interpolate_perceptual_color(start: RGB, end: RGB, amount: float) -> RGB:
    """Short-arc OKLCH fade; neutral endpoints borrow the chromatic hue."""
    ratio = max(0.0, min(1.0, _finite(amount)))
    if ratio == 0:
        return normalize_color(start)
    if ratio == 1:
        return normalize_color(end)
    l1, c1, h1 = rgb_to_oklch(start)
    l2, c2, h2 = rgb_to_oklch(end)
    if c1 < 0.0005:
        h1 = h2
    if c2 < 0.0005:
        h2 = h1
    # Exactly opposite hues always take the same positive half-circle.
    delta = (h2 - h1 + 180) % 360 - 180
    if abs(delta + 180) < 1e-8:
        delta = 180.0
    return oklch_to_rgb(l1 + (l2 - l1) * ratio, c1 + (c2 - c1) * ratio, h1 + delta * ratio)


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
            background=(8, 12, 22),
            surface=(18, 23, 35),
            primary=(128, 224, 218),
            secondary=(168, 156, 240),
            accent=(241, 226, 200),
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
    """Perceptual track transition; retain the six-role colour contract."""

    return ArtworkPalette(
        background=interpolate_perceptual_color(start.background, end.background, amount),
        surface=interpolate_perceptual_color(start.surface, end.surface, amount),
        primary=interpolate_perceptual_color(start.primary, end.primary, amount),
        secondary=interpolate_perceptual_color(start.secondary, end.secondary, amount),
        accent=interpolate_perceptual_color(start.accent, end.accent, amount),
        foreground=interpolate_perceptual_color(start.foreground, end.foreground, amount),
    )


@lru_cache(maxsize=128)
def palette_for_preset(palette: ArtworkPalette, preset: str) -> ArtworkPalette:
    """Apply the chosen A-orbs/B-fireworks art direction, retaining cover hue."""
    if preset == "orb_cluster":
        targets = ((128, 224, 218), (168, 156, 240), (241, 226, 200))
    elif preset == "fireworks":
        targets = ((102, 179, 255), (246, 189, 103), (232, 140, 170))
    else:
        return palette
    lights = [ensure_contrast(interpolate_perceptual_color(source, target, strength), palette.background, 3.0)
              for source, target, strength in zip((palette.primary, palette.secondary, palette.accent), targets, (0.72, 0.80, 0.82))]
    return ArtworkPalette(palette.background, palette.surface, *lights, palette.foreground)


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
        candidates = [(weight, color, rgb_to_oklch(color)) for weight, color in ranked]
        candidates = [item for item in candidates if item[2][1] >= 0.035 and 0.20 <= item[2][0] <= 0.94]
        candidates.sort(key=lambda item: (-item[2][1] * math.sqrt(item[0]), -item[0], item[1]))
        if candidates:
            _, _, (_, chroma, hue) = candidates[0]
            chroma = max(0.10, min(0.16, chroma))
            secondary_hue = next((lch[2] for _, _, lch in candidates[1:]
                                  if abs((lch[2] - hue + 180) % 360 - 180) >= 55), (hue + 85) % 360)
            accent_hue = (hue + 155) % 360
        else:
            # Achromatic and near-black covers have no trustworthy hue. Use a
            # deliberate light family rather than amplifying brown/gray noise.
            hue, secondary_hue, accent_hue, chroma = 185.0, 300.0, 85.0, 0.12
        primary = oklch_to_rgb(0.79, chroma, hue)
        secondary = oklch_to_rgb(0.76, 0.12, secondary_hue)
        accent = oklch_to_rgb(0.85, 0.075, accent_hue)
        # Artwork colours the lights, not the stage: almost-neutral ink remains
        # dark even for white, skin-tone or highly saturated covers.
        background = oklch_to_rgb(0.14, 0.008, 265.0)
        surface = oklch_to_rgb(0.205, 0.012, 265.0)
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
    "interpolate_perceptual_color",
    "oklch_to_rgb",
    "rgb_to_oklch",
    "palette_for_preset",
    "relative_luminance",
]
