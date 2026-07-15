"""Premium, privacy-preserving lyrics presentation for Party Mode.

This module owns presentation and request coordination only.  Parsing, source
priority, provider matching, and private cache persistence remain in the
focused :mod:`music_vault.lyrics` package.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from music_vault.lyrics.cache import LyricsCache
from music_vault.lyrics.models import (
    LyricLine,
    LyricsResult,
    LyricsStatus,
    TrackLyricsIdentity,
)
from music_vault.lyrics.providers.lrclib import LRCLIBProvider
from music_vault.lyrics.service import LyricsService
from music_vault.ui.theme import COLORS


LYRICS_CONSENT_VERSION = 1
LYRICS_CACHE_SCHEMA_VERSION = 1

LYRICS_DEFAULTS: dict[str, object] = {
    "party_mode_lyrics_enabled": False,
    "lyrics_online_lookup_enabled": False,
    "lyrics_lookup_consent_version": 0,
    "lyrics_cache_schema_version": LYRICS_CACHE_SCHEMA_VERSION,
}


def _strict_bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _bounded_version(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(10_000, parsed))


def normalize_lyrics_settings(config: object) -> dict[str, object]:
    """Return strict lyric settings without silently opting into networking."""

    source = config if isinstance(config, Mapping) else {}
    consent_version = _bounded_version(
        source.get("lyrics_lookup_consent_version"), 0
    )
    online_requested = _strict_bool(
        source.get("lyrics_online_lookup_enabled"), False
    )
    return {
        "party_mode_lyrics_enabled": _strict_bool(
            source.get("party_mode_lyrics_enabled"), False
        ),
        "lyrics_online_lookup_enabled": (
            online_requested and consent_version >= LYRICS_CONSENT_VERSION
        ),
        "lyrics_lookup_consent_version": consent_version,
        "lyrics_cache_schema_version": LYRICS_CACHE_SCHEMA_VERSION,
    }


class LyricsTimeline:
    """Binary-searchable line timeline driven by QMediaPlayer position."""

    def __init__(self, lines: tuple[LyricLine, ...] = ()) -> None:
        self.set_lines(lines)

    def set_lines(self, lines: tuple[LyricLine, ...]) -> None:
        self.lines = tuple(sorted(lines, key=lambda line: line.timestamp_ms))
        self._timestamps = tuple(line.timestamp_ms for line in self.lines)

    def index_at(self, position_ms: int) -> int:
        if not self.lines:
            return -1
        return max(0, bisect_right(self._timestamps, max(0, int(position_ms))) - 1)

    def context_at(self, position_ms: int) -> tuple[str, str, str, int]:
        index = self.index_at(position_ms)
        if index < 0:
            return "", "", "", -1
        previous = self.lines[index - 1].text if index > 0 else ""
        current = self.lines[index].text
        following = self.lines[index + 1].text if index + 1 < len(self.lines) else ""
        return previous, current, following, index


class PartyLyricsPanel(QFrame):
    """Independent lyrics overlay positioned above the existing player bar."""

    presentation_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PartyLyricsPanel")
        self.setAccessibleName("Party Mode lyrics")
        self.setMaximumWidth(1_040)
        self.setMinimumWidth(420)
        self._timeline = LyricsTimeline()
        self._timeline_index = -1
        self._available = False
        self._synchronized = False
        self._reduced_motion = False
        self._mode = "hidden"
        self._line_context = ("", "", "")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 18, 30, 18)
        layout.setSpacing(6)
        self._layout = layout
        self._compact = False

        self.state_label = QLabel()
        self.state_label.setObjectName("PartyLyricsState")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        self.state_label.setWordWrap(True)

        self.previous_label = QLabel()
        self.previous_label.setObjectName("PartyLyricsPrevious")
        self.current_label = QLabel()
        self.current_label.setObjectName("PartyLyricsCurrent")
        self.next_label = QLabel()
        self.next_label.setObjectName("PartyLyricsNext")
        for label in (self.previous_label, self.current_label, self.next_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setMaximumWidth(980)
            label.setMinimumWidth(0)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Preferred,
            )

        self.unsynced_label = QLabel("Unsynced Lyrics")
        self.unsynced_label.setObjectName("PartyLyricsUnsyncedHeading")
        self.unsynced_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plain_view = QTextEdit()
        self.plain_view.setObjectName("PartyLyricsPlain")
        self.plain_view.setAccessibleName("Unsynchronized lyrics")
        self.plain_view.setReadOnly(True)
        self.plain_view.setFrameShape(QFrame.Shape.NoFrame)
        self.plain_view.setMinimumHeight(120)
        self.plain_view.setMaximumHeight(220)

        self.attribution_label = QLabel()
        self.attribution_label.setObjectName("PartyLyricsAttribution")
        self.attribution_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.attribution_label.setTextFormat(Qt.TextFormat.PlainText)

        layout.addWidget(self.state_label)
        layout.addWidget(self.previous_label)
        layout.addWidget(self.current_label)
        layout.addWidget(self.next_label)
        layout.addWidget(self.unsynced_label)
        layout.addWidget(self.plain_view)
        layout.addWidget(self.attribution_label)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity)
        self._line_animation = QPropertyAnimation(self._opacity, b"opacity", self)
        self._line_animation.setDuration(220)
        self._line_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.setStyleSheet(
            f"""
            QFrame#PartyLyricsPanel {{
                background: rgba(8, 13, 21, 178);
                border: 1px solid rgba(255, 255, 255, 28);
                border-radius: 22px;
            }}
            QFrame#PartyLyricsPanel[compact="true"] {{
                background: rgba(8, 13, 21, 150);
                border-radius: 12px;
            }}
            QLabel#PartyLyricsPrevious, QLabel#PartyLyricsNext {{
                color: rgba(214, 224, 236, 118); font-size: 15px;
            }}
            QLabel#PartyLyricsCurrent {{
                color: #F7FAFD; font-size: 23px; font-weight: 750;
            }}
            QLabel#PartyLyricsCurrent[compact="true"],
            QLabel#PartyLyricsState[compact="true"] {{
                font-size: 15px;
            }}
            QLabel#PartyLyricsPrevious[compact="true"],
            QLabel#PartyLyricsNext[compact="true"] {{
                font-size: 11px;
            }}
            QLabel#PartyLyricsState {{
                color: #D5DEE9; font-size: 18px; font-weight: 600;
            }}
            QLabel#PartyLyricsUnsyncedHeading {{
                color: {COLORS['accent']}; font-size: 12px; font-weight: 700;
                letter-spacing: 1px;
            }}
            QTextEdit#PartyLyricsPlain {{
                color: #EEF3F9; background: rgba(255,255,255,7);
                border-radius: 14px; padding: 10px; font-size: 16px;
                selection-background-color: {COLORS['accent']};
            }}
            QLabel#PartyLyricsAttribution {{
                color: rgba(197, 209, 223, 112); font-size: 10px;
            }}
            """
        )
        self._show_only("hidden")

    @property
    def lyrics_available(self) -> bool:
        return self._available

    @property
    def lyrics_synchronized(self) -> bool:
        return self._synchronized

    @property
    def presentation_mode(self) -> str:
        return self._mode

    def set_reduced_motion(self, reduced: bool) -> None:
        self._reduced_motion = bool(reduced)
        self._line_animation.setDuration(180 if reduced else 240)

    def set_compact(self, compact: bool) -> None:
        normalized = bool(compact)
        if normalized == self._compact:
            return
        self._compact = normalized
        self.setProperty("compact", normalized)
        if normalized:
            self._layout.setContentsMargins(14, 2, 14, 2)
            self._layout.setSpacing(1)
        else:
            self._layout.setContentsMargins(30, 18, 30, 18)
            self._layout.setSpacing(6)
        self.plain_view.setMinimumHeight(96 if normalized else 120)
        self.plain_view.setMaximumHeight(120 if normalized else 220)
        for widget in (
            self,
            self.state_label,
            self.previous_label,
            self.current_label,
            self.next_label,
        ):
            widget.setProperty("compact", normalized)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._show_only(self._mode, emit_change=False)
        self._render_line_context()

    def reset_for_track(self) -> None:
        self._line_animation.stop()
        self._timeline.set_lines(())
        self._timeline_index = -1
        self._line_context = ("", "", "")
        self.plain_view.clear()
        self.plain_view.verticalScrollBar().setValue(0)
        self._available = False
        self._synchronized = False
        self._show_only("hidden")

    def show_state(self, text: str) -> None:
        self._available = False
        self._synchronized = False
        self.state_label.setText(str(text))
        self.attribution_label.clear()
        self._show_only("state")

    def show_result(self, result: LyricsResult) -> None:
        if result.instrumental:
            self._available = True
            self._synchronized = False
            self.state_label.setText("Instrumental")
            self.attribution_label.setText(result.attribution or "")
            self._show_only("state")
            return
        if result.synchronized:
            self._available = True
            self._synchronized = True
            self._timeline.set_lines(result.synced_lines)
            self._timeline_index = -1
            self.attribution_label.setText(result.attribution or "")
            self._show_only("synchronized")
            self.set_position(0, force=True)
            return
        if result.available and result.plain_text:
            self._available = True
            self._synchronized = False
            # setPlainText is intentional: provider markup is never interpreted.
            self.plain_view.setPlainText(result.plain_text)
            self.plain_view.verticalScrollBar().setValue(0)
            self.attribution_label.setText(result.attribution or "")
            self._show_only("plain")
            return
        self.show_state("No lyrics available")

    def set_position(self, position_ms: int, *, force: bool = False) -> int:
        if self._mode != "synchronized":
            return -1
        previous, current, following, index = self._timeline.context_at(position_ms)
        if not force and index == self._timeline_index:
            return index
        self._timeline_index = index
        self._line_context = (previous, current, following)
        self._render_line_context()
        self._animate_line_change()
        return index

    def page_scroll(self, direction: int) -> bool:
        if self._mode != "plain":
            return False
        bar = self.plain_view.verticalScrollBar()
        delta = max(1, bar.pageStep()) * (1 if int(direction) > 0 else -1)
        bar.setValue(max(bar.minimum(), min(bar.maximum(), bar.value() + delta)))
        return True

    def _animate_line_change(self) -> None:
        self._line_animation.stop()
        self._line_animation.setStartValue(0.58 if self._reduced_motion else 0.42)
        self._line_animation.setEndValue(1.0)
        self._line_animation.start()

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._render_line_context()

    def _render_line_context(self) -> None:
        widths = (
            max(48, self.width() // 4),
            max(96, self.width() // 2),
            max(48, self.width() // 4),
        )
        for label, raw, width in zip(
            (self.previous_label, self.current_label, self.next_label),
            self._line_context,
            widths,
        ):
            displayed = raw
            if self._compact and self._mode == "synchronized":
                displayed = label.fontMetrics().elidedText(
                    raw,
                    Qt.TextElideMode.ElideRight,
                    max(20, width - 18),
                )
            label.setText(displayed)
            label.setToolTip(raw if displayed != raw else "")

    def _show_only(self, mode: str, *, emit_change: bool = True) -> None:
        self._mode = mode
        state = mode == "state"
        synced = mode == "synchronized"
        plain = mode == "plain"
        self._layout.setDirection(
            QBoxLayout.Direction.LeftToRight
            if synced and self._compact
            else QBoxLayout.Direction.TopToBottom
        )
        self._layout.setStretch(1, 1)
        self._layout.setStretch(2, 2)
        self._layout.setStretch(3, 1)
        self.state_label.setVisible(state)
        self.previous_label.setVisible(synced)
        self.current_label.setVisible(synced)
        self.next_label.setVisible(synced)
        self.unsynced_label.setVisible(plain)
        self.plain_view.setVisible(plain)
        attribution = self.attribution_label.text()
        self.attribution_label.setVisible(
            not self._compact and (synced or plain or state) and bool(attribution)
        )
        self.setToolTip(attribution if self._compact else "")
        self.setVisible(mode != "hidden")
        self.adjustSize()
        self._render_line_context()
        if emit_change:
            self.presentation_changed.emit()


def create_default_lyrics_service() -> LyricsService:
    """Construct the one-provider service without performing a network request."""

    return LyricsService(provider=LRCLIBProvider(), cache=LyricsCache(), max_workers=1)


class PartyLyricsController(QObject):
    """Coordinate one current-track lookup and reject every stale result."""

    result_arrived = Signal(int, object)
    state_changed = Signal(bool, bool)
    consent_required = Signal(object)

    def __init__(
        self,
        panel: PartyLyricsPanel,
        *,
        service: LyricsService | None = None,
        service_factory: Callable[[], LyricsService] = create_default_lyrics_service,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.panel = panel
        self._service = service
        self._service_factory = service_factory
        self._identity: TrackLyricsIdentity | None = None
        self._generation = 0
        self._enabled = False
        self._online_enabled = False
        self._consent_version = 0
        self._consent_prompted_for: str | None = None
        self._suspended = False
        self.result_arrived.connect(self._accept_async_result)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def lyrics_available(self) -> bool:
        return self.panel.lyrics_available

    @property
    def lyrics_synchronized(self) -> bool:
        return self.panel.lyrics_synchronized

    @property
    def pending_count(self) -> int:
        return int(getattr(self._service, "pending_count", 0)) if self._service else 0

    def apply_settings(self, settings: object) -> None:
        normalized = normalize_lyrics_settings(settings)
        changed = (
            self._enabled != bool(normalized["party_mode_lyrics_enabled"])
            or self._online_enabled
            != bool(normalized["lyrics_online_lookup_enabled"])
            or self._consent_version
            != int(normalized["lyrics_lookup_consent_version"])
        )
        self._enabled = bool(normalized["party_mode_lyrics_enabled"])
        self._online_enabled = bool(normalized["lyrics_online_lookup_enabled"])
        self._consent_version = int(normalized["lyrics_lookup_consent_version"])
        if not self._enabled:
            self.suspend()
            self.panel.reset_for_track()
            self._emit_state()
        elif changed and self._identity is not None:
            self._load_current()

    def set_track(self, identity: TrackLyricsIdentity | None) -> None:
        if self._same_identity(identity, self._identity):
            return
        self.suspend()
        self._identity = identity
        self._consent_prompted_for = None
        self.panel.reset_for_track()
        self._emit_state()
        if self._enabled and identity is not None:
            self._load_current()

    def set_position(self, position_ms: int) -> int:
        return self.panel.set_position(position_ms)

    def refresh(self) -> None:
        if self._enabled and self._identity is not None:
            self._load_current(force_refresh=True)

    def import_manual(self, path: str | Path) -> LyricsResult | None:
        if self._identity is None:
            return None
        try:
            result = self._get_service().import_manual(self._identity, Path(path))
        except Exception:
            self.panel.show_state("Lyrics temporarily unavailable")
            self._emit_state()
            return None
        self._present(result)
        return result

    def clear_automatic(self) -> None:
        if self._identity is None:
            return
        try:
            self._get_service().clear_automatic(self._identity)
        except Exception:
            self.panel.show_state("Lyrics temporarily unavailable")
            self._emit_state()
            return
        self._load_current(allow_online=False)

    def reload_local_only(self) -> None:
        """Refresh local sources after a global clear without refetching."""

        if self._enabled and self._identity is not None:
            self._load_current(allow_online=False)

    def suspend(self) -> None:
        self._suspended = True
        self._generation += 1
        if self._service is not None:
            self._service.cancel()

    def resume(self) -> None:
        """Reload the current identity after a hidden-window cancellation."""

        if self._suspended and self._enabled and self._identity is not None:
            self._load_current()

    def close(self) -> None:
        self.suspend()
        if self._service is not None:
            self._service.close()
            self._service = None

    def _load_current(
        self,
        *,
        force_refresh: bool = False,
        allow_online: bool = True,
    ) -> None:
        identity = self._identity
        if not self._enabled or identity is None:
            return
        self.suspend()
        self._suspended = False
        try:
            local = self._get_service().resolve(
                identity,
                online_enabled=False,
                force_refresh=force_refresh,
            )
        except Exception:
            self.panel.show_state("Lyrics temporarily unavailable")
            self._emit_state()
            return
        if local.available:
            self._present(local)
            return
        if self._online_enabled and allow_online:
            self.panel.show_state("Finding lyrics…")
            self._emit_state()
            try:
                self._generation = self._get_service().request(
                    identity,
                    self._async_callback,
                    online_enabled=True,
                    force_refresh=force_refresh,
                )
            except Exception:
                self.panel.show_state("Lyrics temporarily unavailable")
                self._emit_state()
            return
        self.panel.show_state("No local lyrics available")
        self._emit_state()
        if not allow_online:
            return
        fingerprint = identity.metadata_fingerprint
        if (
            self._consent_version < LYRICS_CONSENT_VERSION
            and self._consent_prompted_for != fingerprint
        ):
            self._consent_prompted_for = fingerprint
            self.consent_required.emit(identity)

    def _async_callback(self, generation: int, result: LyricsResult) -> None:
        # Signal delivery crosses safely to the controller's Qt thread.
        self.result_arrived.emit(int(generation), result)

    def _accept_async_result(self, generation: int, result: object) -> None:
        if generation != self._generation or not isinstance(result, LyricsResult):
            return
        if not self._enabled or not self._same_identity(result.identity, self._identity):
            return
        self._present(result)

    def _present(self, result: LyricsResult) -> None:
        if result.available:
            self.panel.show_result(result)
        elif result.status in {LyricsStatus.NO_MATCH, LyricsStatus.AMBIGUOUS}:
            self.panel.show_state("No lyrics available")
        elif result.status is LyricsStatus.TEMPORARY_ERROR:
            self.panel.show_state("Lyrics temporarily unavailable")
        else:
            self.panel.show_state("No lyrics available")
        self._emit_state()

    def _emit_state(self) -> None:
        self.state_changed.emit(
            self.panel.lyrics_available,
            self.panel.lyrics_synchronized,
        )

    def _get_service(self) -> LyricsService:
        if self._service is None:
            self._service = self._service_factory()
        return self._service

    @staticmethod
    def _same_identity(
        left: TrackLyricsIdentity | None,
        right: TrackLyricsIdentity | None,
    ) -> bool:
        if left is None or right is None:
            return left is right
        return (
            left.stable_id == right.stable_id
            and left.metadata_fingerprint == right.metadata_fingerprint
        )


def request_online_lyrics_consent(
    parent: QWidget,
    identity: TrackLyricsIdentity,
) -> bool:
    """Show the one-time, explicit metadata disclosure for online lookup."""

    duration = (
        f"{round(identity.duration_ms / 1000)} seconds"
        if identity.duration_ms
        else "not available"
    )
    details = [
        f"Title: {identity.title}",
        f"Artist: {identity.artist}",
    ]
    if identity.album:
        details.append(f"Album: {identity.album}")
    details.append(f"Duration: {duration}")

    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Information)
    dialog.setWindowTitle("Online Lyrics")
    dialog.setText(
        "Music Vault can request lyrics from LRCLIB only after local, embedded, "
        "sidecar, and cached lyrics are unavailable."
    )
    dialog.setInformativeText(
        "The following metadata is sent over HTTPS and successful results are "
        "cached privately on this computer:\n\n" + "\n".join(details)
    )
    enable = dialog.addButton("Enable Online Lyrics", QMessageBox.ButtonRole.AcceptRole)
    dialog.addButton("Keep Local Only", QMessageBox.ButtonRole.RejectRole)
    dialog.setDefaultButton(enable)
    dialog.exec()
    return dialog.clickedButton() is enable


__all__ = [
    "LYRICS_CACHE_SCHEMA_VERSION",
    "LYRICS_CONSENT_VERSION",
    "LYRICS_DEFAULTS",
    "LyricsTimeline",
    "PartyLyricsController",
    "PartyLyricsPanel",
    "create_default_lyrics_service",
    "normalize_lyrics_settings",
    "request_online_lyrics_consent",
]
