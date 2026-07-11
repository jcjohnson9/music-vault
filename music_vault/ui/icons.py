from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Final

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from music_vault.core.paths import assets_dir
from music_vault.ui.theme import COLORS


REQUIRED_ICONS: Final[tuple[str, ...]] = (
    "play",
    "pause",
    "previous",
    "next",
    "shuffle",
    "repeat",
    "repeat-one",
    "volume",
    "volume-low",
    "volume-muted",
    "search",
    "library",
    "recently-added",
    "downloaded",
    "albums",
    "artists",
    "playlists",
    "sync",
    "settings",
    "import",
    "add",
    "queue-next",
    "more",
    "refresh",
    "remove",
    "metadata",
    "enrich",
    "folder",
    "warning",
    "error",
)

OPTIONAL_ICONS: Final[tuple[str, ...]] = (
    "autoplay",
    "chevron-down",
    "music-note",
    "check",
)

_ICON_FILES: Final[dict[str, str]] = {
    name: f"{name}.svg" for name in REQUIRED_ICONS + OPTIONAL_ICONS
}


def _icon_name(name: str) -> str:
    normalized = str(name).strip().lower().replace("_", "-")
    if normalized not in _ICON_FILES:
        raise KeyError(f"Unknown Music Vault UI icon: {name!r}")
    return normalized


def icon_path(name: str) -> Path:
    """Resolve a registered icon in source and packaged application layouts."""

    normalized = _icon_name(name)
    root = (assets_dir() / "icons" / "ui").resolve()
    candidate = (root / _ICON_FILES[normalized]).resolve()
    if candidate.parent != root:
        raise ValueError("UI icon paths must remain inside the icon asset directory.")
    return candidate


def _normalized_color(value: str | QColor) -> str:
    color = QColor(value)
    if not color.isValid():
        raise ValueError(f"Invalid icon color: {value!r}")
    return color.name(QColor.NameFormat.HexArgb)


def _normalized_dpr(value: float) -> float:
    try:
        dpr = float(value)
    except (TypeError, ValueError, OverflowError):
        dpr = 1.0
    if not math.isfinite(dpr) or dpr <= 0:
        dpr = 1.0
    return round(min(dpr, 4.0), 2)


@lru_cache(maxsize=1024)
def _render_icon_pixmap_cached(
    name: str,
    size: int,
    color_name: str,
    dpr: float,
) -> QPixmap:
    renderer = QSvgRenderer(str(icon_path(name)))
    if not renderer.isValid():
        return QPixmap()

    physical_size = max(1, int(math.ceil(size * dpr)))
    pixmap = QPixmap(physical_size, physical_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(dpr)

    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0.0, 0.0, float(size), float(size)))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(QRectF(0.0, 0.0, float(size), float(size)), QColor(color_name))
    painter.end()
    return pixmap


def render_icon_pixmap(
    name: str,
    size: int,
    color: str | QColor,
    dpr: float | None = None,
) -> QPixmap:
    """Render and tint one registered SVG at a logical, high-DPI-safe size."""

    logical_size = int(size)
    if logical_size <= 0:
        raise ValueError("Icon size must be a positive integer.")
    resolved_dpr = _application_dpr() if dpr is None else dpr
    return _render_icon_pixmap_cached(
        _icon_name(name),
        logical_size,
        _normalized_color(color),
        _normalized_dpr(resolved_dpr),
    )


def _application_dpr() -> float:
    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    return _normalized_dpr(screen.devicePixelRatio() if screen is not None else 1.0)


@lru_cache(maxsize=512)
def _ui_icon_cached(
    name: str,
    size: int,
    normal_color: str,
    disabled_color: str,
    active_color: str,
    dpr: float,
) -> QIcon:
    icon = QIcon()
    icon.addPixmap(
        render_icon_pixmap(name, size, normal_color, dpr),
        QIcon.Mode.Normal,
        QIcon.State.Off,
    )
    icon.addPixmap(
        render_icon_pixmap(name, size, active_color, dpr),
        QIcon.Mode.Active,
        QIcon.State.Off,
    )
    icon.addPixmap(
        render_icon_pixmap(name, size, disabled_color, dpr),
        QIcon.Mode.Disabled,
        QIcon.State.Off,
    )
    icon.addPixmap(
        render_icon_pixmap(name, size, active_color, dpr),
        QIcon.Mode.Selected,
        QIcon.State.On,
    )
    return icon


def ui_icon(
    name: str,
    size: int = 20,
    color: str | QColor = COLORS["text_secondary"],
    disabled_color: str | QColor = COLORS["disabled_text"],
    active_color: str | QColor = COLORS["accent_hover"],
) -> QIcon:
    """Create a cached QIcon with coherent normal, active, and disabled modes."""

    logical_size = int(size)
    if logical_size <= 0:
        raise ValueError("Icon size must be a positive integer.")
    return _ui_icon_cached(
        _icon_name(name),
        logical_size,
        _normalized_color(color),
        _normalized_color(disabled_color),
        _normalized_color(active_color),
        _application_dpr(),
    )


def clear_icon_cache() -> None:
    _render_icon_pixmap_cached.cache_clear()
    _ui_icon_cached.cache_clear()


def icon_cache_info():
    return _render_icon_pixmap_cached.cache_info()


def icon_size(value: int) -> QSize:
    """Return a square logical QSize for callers configuring buttons."""

    size = max(1, int(value))
    return QSize(size, size)
