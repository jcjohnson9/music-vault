from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QImageReader, QKeyEvent, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFrame,
    QFileDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
    QStackedLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from music_vault.core.audio_analysis import AudioAnalyzer, AudioFeatures
from music_vault.lyrics.models import TrackLyricsIdentity
from music_vault.ui.components import ElidedLabel, IconButton
from music_vault.ui.icons import render_icon_pixmap, ui_icon
from music_vault.ui.party_lyrics import (
    LYRICS_CONSENT_VERSION,
    LYRICS_DEFAULTS,
    PartyLyricsController,
    PartyLyricsPanel,
    normalize_lyrics_settings,
    request_online_lyrics_consent,
)
from music_vault.ui.party_palette import (
    DEFAULT_PARTY_PALETTE,
    ArtworkPalette,
    PaletteExtractor,
    interpolate_palette,
)
from music_vault.ui.party_visuals import (
    PRESETS,
    PartyCanvas,
    center_artwork_rect,
    center_content_bottom,
)
from music_vault.ui.theme import COLORS


PARTY_MODE_CONFIG_VERSION = 2
PARTY_MODE_DEFAULTS: dict[str, object] = {
    "party_mode_config_version": PARTY_MODE_CONFIG_VERSION,
    "party_mode_preset": "static",
    "party_mode_quality": "auto",
    "party_mode_frame_rate": "auto",
    "party_mode_reduced_motion": False,
    "party_mode_show_artwork": True,
    "party_mode_auto_hide_overlay": True,
    "party_mode_overlay_timeout_seconds": 3,
}
PARTY_PRESETS = tuple(PRESETS)
PARTY_PRESET_LABELS = {
    "static": "Static",
    "starfield": "Starfield",
    "aurora": "Aurora",
    "orb_cluster": "Orb Cluster",
    "fireworks": "Fireworks",
    "pulse": "Pulse",
}
PARTY_QUALITIES = ("auto", "low", "medium", "high")
PARTY_FRAME_RATES = ("auto", "30", "60")


def _choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    normalized = str(value).strip().lower()
    return normalized if normalized in allowed else default


def _strict_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _overlay_timeout(value: object) -> int:
    if isinstance(value, bool):
        return int(PARTY_MODE_DEFAULTS["party_mode_overlay_timeout_seconds"])
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        parsed = int(PARTY_MODE_DEFAULTS["party_mode_overlay_timeout_seconds"])
    return max(1, min(10, parsed))


def normalize_party_mode_settings(config: Mapping[str, object] | None) -> dict[str, object]:
    """Return safe Party Mode values without discarding unrelated config."""

    source = config if isinstance(config, Mapping) else {}
    configured_version = source.get("party_mode_config_version")
    try:
        version = int(configured_version) if not isinstance(configured_version, bool) else 0
    except (TypeError, ValueError, OverflowError):
        version = 0
    requested_preset = _choice(
        source.get("party_mode_preset"),
        PARTY_PRESETS,
        str(PARTY_MODE_DEFAULTS["party_mode_preset"]),
    )
    # Batch 9 shipped Pulse as an automatic default.  A missing config version
    # therefore means a stored Pulse was not an intentional v2 selection.
    if version < PARTY_MODE_CONFIG_VERSION and requested_preset == "pulse":
        requested_preset = "static"
    return {
        "party_mode_config_version": PARTY_MODE_CONFIG_VERSION,
        "party_mode_preset": requested_preset,
        "party_mode_quality": _choice(
            source.get("party_mode_quality"),
            PARTY_QUALITIES,
            str(PARTY_MODE_DEFAULTS["party_mode_quality"]),
        ),
        "party_mode_frame_rate": _choice(
            source.get("party_mode_frame_rate"),
            PARTY_FRAME_RATES,
            str(PARTY_MODE_DEFAULTS["party_mode_frame_rate"]),
        ),
        "party_mode_reduced_motion": _strict_bool(
            source.get("party_mode_reduced_motion"),
            bool(PARTY_MODE_DEFAULTS["party_mode_reduced_motion"]),
        ),
        "party_mode_show_artwork": _strict_bool(
            source.get("party_mode_show_artwork"),
            bool(PARTY_MODE_DEFAULTS["party_mode_show_artwork"]),
        ),
        "party_mode_auto_hide_overlay": _strict_bool(
            source.get("party_mode_auto_hide_overlay"),
            bool(PARTY_MODE_DEFAULTS["party_mode_auto_hide_overlay"]),
        ),
        "party_mode_overlay_timeout_seconds": _overlay_timeout(
            source.get("party_mode_overlay_timeout_seconds")
        ),
    }


def party_preset_label(value: object) -> str:
    return PARTY_PRESET_LABELS.get(str(value), PARTY_PRESET_LABELS["static"])


def party_preset_value(label: object) -> str:
    normalized = str(label).strip().casefold()
    for value, friendly in PARTY_PRESET_LABELS.items():
        if normalized in {value.casefold(), friendly.casefold()}:
            return value
    return "static"


class PartyAudioAnalysisThread(QThread):
    """One-worker/latest-buffer bridge from Qt decoded audio to pure analysis."""

    features_ready = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.analyzer = AudioAnalyzer()
        self._wake = threading.Event()
        self._stop_requested = threading.Event()

    @property
    def pending_count(self) -> int:
        return int(self.analyzer.slot.has_pending)

    @property
    def dropped_buffer_count(self) -> int:
        return self.analyzer.dropped_buffer_count

    def submit(
        self,
        data: bytes,
        sample_format: object,
        channels: int,
        sample_rate: int,
        timestamp_ms: int,
    ) -> bool:
        if self._stop_requested.is_set():
            return False
        accepted = self.analyzer.submit_latest(
            data,
            sample_format,
            channels,
            sample_rate,
            timestamp_ms,
        )
        self._wake.set()
        return accepted

    def run(self) -> None:
        minimum_interval = 1.0 / 30.0
        last_analysis_at = 0.0
        while not self._stop_requested.is_set():
            self._wake.wait(0.1)
            self._wake.clear()
            if self._stop_requested.is_set():
                break
            remaining = minimum_interval - (time.monotonic() - last_analysis_at)
            if remaining > 0.0 and self._stop_requested.wait(remaining):
                break
            features = self.analyzer.process_latest()
            if features is not None:
                last_analysis_at = time.monotonic()
                self.features_ready.emit(features)
            if self.analyzer.slot.has_pending:
                self._wake.set()

    def shutdown(self, timeout_ms: int = 1500) -> bool:
        self._stop_requested.set()
        self._wake.set()
        if self.isRunning():
            self.wait(max(1, int(timeout_ms)))
        stopped = not self.isRunning()
        if stopped:
            self.analyzer.reset()
        return stopped


class PartyModeWindow(QMainWindow):
    """Full-screen visual surface driven by an existing MusicVaultWindow."""

    party_closed = Signal()
    preset_changed = Signal(str)
    audio_reactivity_changed = Signal(bool)
    lyrics_status_changed = Signal(bool, bool, bool)

    def __init__(self, host: QMainWindow) -> None:
        super().__init__(None)
        self._host_ref = weakref.ref(host)
        self.setObjectName("PartyModeWindow")
        self.setWindowTitle("Music Vault Party Mode")
        self.setWindowFlags(Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self._settings = normalize_party_mode_settings(getattr(host, "config", {}))
        self._lyrics_settings = normalize_lyrics_settings(getattr(host, "config", {}))
        self._closed_notified = False
        self._player_signals_connected = False
        self._current_track_id: int | None = None
        self._lyrics_identity: TrackLyricsIdentity | None = None
        self._current_cover_identity = ""
        self._overlay_visible = True
        self._overlay_hovered = False
        self._help_visible = False
        self._audio_reactivity_available = False
        self._last_audio_feature_at = 0.0
        self._latest_audio_features: AudioFeatures | None = None
        self._last_nonzero_volume = int(getattr(host, "volume_percent", 70) or 70)
        self._palette_extractor = PaletteExtractor()
        self._palette = DEFAULT_PARTY_PALETTE
        self._palette_from = self._palette
        self._palette_to = self._palette
        self._palette_started_at = 0.0
        self._build_ui()
        self.lyrics_controller = PartyLyricsController(
            self.lyrics_panel,
            parent=self,
        )
        self.lyrics_controller.state_changed.connect(self._on_lyrics_state_changed)
        self.lyrics_controller.consent_required.connect(self._request_lyrics_consent)
        self.lyrics_panel.presentation_changed.connect(self._position_lyrics_panel)

        self.overlay_timer = QTimer(self)
        self.overlay_timer.setSingleShot(True)
        self.overlay_timer.timeout.connect(self._hide_overlay_if_idle)
        self.state_timer = QTimer(self)
        self.state_timer.setInterval(250)
        self.state_timer.timeout.connect(self.refresh_from_host)
        self.fallback_timer = QTimer(self)
        self.fallback_timer.setInterval(500)
        self.fallback_timer.timeout.connect(self._update_audio_capability)
        self.palette_timer = QTimer(self)
        self.palette_timer.setInterval(30)
        self.palette_timer.timeout.connect(self._advance_palette)

        for child in self.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)
        self.apply_settings({**self._settings, **self._lyrics_settings})
        self.refresh_from_host(force=True)

    @property
    def audio_reactivity_available(self) -> bool:
        return self._audio_reactivity_available

    @property
    def overlay_visible(self) -> bool:
        return self._overlay_visible

    @property
    def rendering_active(self) -> bool:
        return bool(getattr(self.canvas, "rendering_active", False))

    @property
    def current_preset(self) -> str:
        return str(self._settings["party_mode_preset"])

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("PartyRoot")
        stack = QStackedLayout(root)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setStackingMode(QStackedLayout.StackingMode.StackAll)

        self.canvas = PartyCanvas(root)
        self.canvas.setObjectName("PartyCanvas")
        stack.addWidget(self.canvas)

        self.overlay = QFrame(root)
        self.overlay.setObjectName("PartyOverlay")
        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(36, 28, 36, 30)
        overlay_layout.setSpacing(18)

        top_row = QHBoxLayout()
        self.party_brand = QLabel("MUSIC VAULT  ·  PARTY MODE")
        self.party_brand.setObjectName("PartyEyebrow")
        top_row.addWidget(self.party_brand)
        top_row.addStretch(1)
        self.lyrics_button = QPushButton("Lyrics")
        self.lyrics_button.setObjectName("PartyGlassButton")
        self.lyrics_button.setIcon(ui_icon("lyrics", 18, COLORS["text_primary"]))
        self.lyrics_button.setToolTip("Lyrics (L)")
        self.lyrics_button.setAccessibleName("Toggle Lyrics")
        self.lyrics_button.clicked.connect(self.toggle_lyrics)
        self.lyrics_button.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.lyrics_menu = QMenu(self.lyrics_button)
        self.lyrics_menu.addAction("Refresh Lyrics", self.refresh_lyrics)
        self.lyrics_menu.addAction("Import Lyrics…", self.import_lyrics)
        self.lyrics_menu.addAction(
            "Clear Automatic Lyrics", self.clear_automatic_lyrics
        )
        self.lyrics_menu.addSeparator()
        self.lyrics_menu.addAction("Open Lyrics Settings", self.open_lyrics_settings)
        self.lyrics_button.customContextMenuRequested.connect(
            lambda point: self.lyrics_menu.exec(
                self.lyrics_button.mapToGlobal(point)
            )
        )

        self.preset_button = QPushButton("Static")
        self.preset_button.setObjectName("PartyGlassButton")
        self.preset_button.setIcon(ui_icon("visual-preset", 18, COLORS["text_primary"]))
        self.preset_button.setToolTip("Cycle visual preset (V)")
        self.preset_button.setAccessibleName("Cycle Party Mode visual preset")
        self.preset_button.clicked.connect(self.cycle_preset)
        self.help_button = QPushButton("Shortcuts")
        self.help_button.setObjectName("PartyGlassButton")
        self.help_button.setIcon(ui_icon("overlay-help", 18, COLORS["text_primary"]))
        self.help_button.setToolTip("Show Party Mode shortcuts (?)")
        self.help_button.clicked.connect(self.toggle_help)
        self.exit_button = QPushButton("Exit Full Screen")
        self.exit_button.setObjectName("PartyExitButton")
        self.exit_button.setIcon(ui_icon("exit-fullscreen", 18, COLORS["text_primary"]))
        self.exit_button.setToolTip("Exit Party Mode (Esc or F11)")
        self.exit_button.setAccessibleName("Exit full screen Party Mode")
        self.exit_button.clicked.connect(self.close)
        top_row.addWidget(self.lyrics_button)
        top_row.addWidget(self.preset_button)
        top_row.addWidget(self.help_button)
        top_row.addWidget(self.exit_button)
        overlay_layout.addLayout(top_row)
        overlay_layout.addStretch(1)

        self.controls_panel = QFrame(self.overlay)
        self.controls_panel.setObjectName("PartyControlsPanel")
        controls_layout = QHBoxLayout(self.controls_panel)
        controls_layout.setContentsMargins(20, 18, 20, 18)
        controls_layout.setSpacing(18)

        self.overlay_artwork = QLabel()
        self.overlay_artwork.setObjectName("PartyArtwork")
        self.overlay_artwork.setFixedSize(88, 88)
        self.overlay_artwork.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_artwork.setPixmap(
            render_icon_pixmap("music-note", 34, COLORS["text_primary"])
        )
        controls_layout.addWidget(self.overlay_artwork)

        information = QVBoxLayout()
        information.setSpacing(3)
        self.title_label = ElidedLabel("Choose a song to begin")
        self.title_label.setObjectName("PartyTitle")
        self.artist_label = ElidedLabel("Music Vault is ready")
        self.artist_label.setObjectName("PartyArtist")
        self.album_label = ElidedLabel("")
        self.album_label.setObjectName("PartyAlbum")
        information.addWidget(self.title_label)
        information.addWidget(self.artist_label)
        information.addWidget(self.album_label)

        progress_row = QHBoxLayout()
        self.elapsed_label = QLabel("0:00")
        self.elapsed_label.setObjectName("PartyTime")
        self.progress_slider = QSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setObjectName("PartyProgress")
        self.progress_slider.setAccessibleName("Party Mode playback position")
        self.progress_slider.setRange(0, 0)
        self.progress_slider.sliderReleased.connect(self._seek_from_slider)
        self.duration_label = QLabel("0:00")
        self.duration_label.setObjectName("PartyTime")
        progress_row.addWidget(self.elapsed_label)
        progress_row.addWidget(self.progress_slider, 1)
        progress_row.addWidget(self.duration_label)
        information.addLayout(progress_row)
        controls_layout.addLayout(information, 1)

        transport = QHBoxLayout()
        transport.setSpacing(8)
        self.previous_button = IconButton(
            "previous", "Previous track (Ctrl+Left)", size=22, variant="circle"
        )
        self.play_button = IconButton(
            "play", "Play or pause (Space)", size=24, variant="primary"
        )
        self.next_button = IconButton(
            "next", "Next track (Ctrl+Right)", size=22, variant="circle"
        )
        self.previous_button.clicked.connect(self._previous)
        self.play_button.clicked.connect(self._play_pause)
        self.next_button.clicked.connect(self._next)
        transport.addWidget(self.previous_button)
        transport.addWidget(self.play_button)
        transport.addWidget(self.next_button)
        controls_layout.addLayout(transport)

        modes = QVBoxLayout()
        modes.setSpacing(6)
        mode_row = QHBoxLayout()
        self.auto_button = QPushButton("Auto")
        self.auto_button.setObjectName("PartyModeButton")
        self.auto_button.setToolTip("Toggle Auto (A)")
        self.auto_button.clicked.connect(self._toggle_auto)
        self.shuffle_button = QPushButton()
        self.shuffle_button.setObjectName("PartyModeButton")
        self.shuffle_button.setIcon(ui_icon("shuffle", 17))
        self.shuffle_button.setToolTip("Toggle Shuffle (S)")
        self.shuffle_button.clicked.connect(self._toggle_shuffle)
        self.repeat_button = QPushButton()
        self.repeat_button.setObjectName("PartyModeButton")
        self.repeat_button.setIcon(ui_icon("repeat", 17))
        self.repeat_button.setToolTip("Cycle Repeat (R)")
        self.repeat_button.clicked.connect(self._cycle_repeat)
        self.queue_label = QLabel("Q: 0")
        self.queue_label.setObjectName("PartyQueue")
        mode_row.addWidget(self.auto_button)
        mode_row.addWidget(self.shuffle_button)
        mode_row.addWidget(self.repeat_button)
        mode_row.addWidget(self.queue_label)
        modes.addLayout(mode_row)
        volume_row = QHBoxLayout()
        self.volume_label = QLabel()
        self.volume_label.setPixmap(render_icon_pixmap("volume", 18, COLORS["text_primary"]))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setObjectName("PartyVolume")
        self.volume_slider.setAccessibleName("Party Mode playback volume")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.valueChanged.connect(self._volume_changed)
        volume_row.addWidget(self.volume_label)
        volume_row.addWidget(self.volume_slider, 1)
        modes.addLayout(volume_row)
        controls_layout.addLayout(modes)
        overlay_layout.addWidget(self.controls_panel)

        effect = QGraphicsOpacityEffect(self.overlay)
        effect.setOpacity(1.0)
        self.overlay.setGraphicsEffect(effect)
        self.overlay_effect = effect
        self.overlay_animation = QPropertyAnimation(effect, b"opacity", self)
        self.overlay_animation.setDuration(180)
        self.overlay_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        stack.addWidget(self.overlay)

        self.help_panel = QFrame(root)
        self.help_panel.setObjectName("PartyHelpPanel")
        help_layout = QVBoxLayout(self.help_panel)
        help_layout.setContentsMargins(28, 24, 28, 24)
        heading = QLabel("Party Mode shortcuts")
        heading.setObjectName("PartyHelpTitle")
        shortcuts = QLabel(
            "Space  Play / Pause     ← / →  Seek 10 seconds\n"
            "Ctrl+← / Ctrl+→  Previous / Next     ↑ / ↓  Volume\n"
            "M  Mute     V  Visual preset     H  Overlay     L  Lyrics\n"
            "Page Up / Page Down  Scroll unsynchronized lyrics\n"
            "A  Auto     S  Shuffle     R  Repeat\n"
            "Esc or F11  Exit full screen"
        )
        shortcuts.setObjectName("PartyHelpText")
        shortcuts.setWordWrap(True)
        close_help = QPushButton("Close shortcuts")
        close_help.setObjectName("PartyGlassButton")
        close_help.clicked.connect(self.toggle_help)
        help_layout.addWidget(heading)
        help_layout.addWidget(shortcuts)
        help_layout.addWidget(close_help, alignment=Qt.AlignmentFlag.AlignRight)
        self.help_panel.setMaximumWidth(620)
        self.help_panel.hide()
        stack.addWidget(self.help_panel)
        stack.setAlignment(self.help_panel, Qt.AlignmentFlag.AlignCenter)
        stack.setCurrentWidget(self.overlay)
        self.root_stack = stack

        # Lyrics are deliberately independent of the control overlay.  The
        # panel is manually positioned above the existing player bar, so
        # hiding controls never hides lyrics and no approved geometry moves.
        self.lyrics_panel = PartyLyricsPanel(root)
        self.lyrics_panel.hide()
        self.lyrics_panel.raise_()

        self.setCentralWidget(root)
        self.setStyleSheet(self._stylesheet())

    @staticmethod
    def _stylesheet() -> str:
        return f"""
        QWidget#PartyRoot {{ background: #05080D; color: {COLORS['text_primary']}; }}
        QFrame#PartyOverlay {{ background: transparent; }}
        QLabel#PartyEyebrow {{ color: #A9B7C8; font-size: 12px; font-weight: 700; letter-spacing: 2px; }}
        QFrame#PartyControlsPanel, QFrame#PartyHelpPanel {{
            background: rgba(9, 14, 22, 222); border: 1px solid rgba(255,255,255,38);
            border-radius: 22px;
        }}
        QLabel#PartyArtwork {{ background: rgba(255,255,255,12); border: 1px solid rgba(255,255,255,30); border-radius: 14px; }}
        QLabel#PartyTitle {{ color: #F5F8FC; font-size: 21px; font-weight: 700; }}
        QLabel#PartyArtist {{ color: #C3CEDB; font-size: 14px; }}
        QLabel#PartyAlbum, QLabel#PartyTime, QLabel#PartyQueue {{ color: #8997A8; font-size: 11px; }}
        QLabel#PartyHelpTitle {{ color: #F5F8FC; font-size: 22px; font-weight: 700; }}
        QLabel#PartyHelpText {{ color: #C3CEDB; font-size: 14px; line-height: 1.5; }}
        QPushButton#PartyGlassButton, QPushButton#PartyExitButton, QPushButton#PartyModeButton {{
            color: #E9EFF7; background: rgba(255,255,255,16); border: 1px solid rgba(255,255,255,32);
            border-radius: 10px; padding: 8px 12px; min-height: 20px;
        }}
        QPushButton#PartyGlassButton:hover, QPushButton#PartyExitButton:hover, QPushButton#PartyModeButton:hover {{
            background: rgba(255,255,255,30); border-color: rgba(255,255,255,62);
        }}
        QPushButton#PartyGlassButton[active="true"] {{
            color: {COLORS['accent']}; border-color: {COLORS['accent']};
            background: rgba(78, 205, 196, 18);
        }}
        QPushButton#PartyModeButton[active="true"] {{ color: {COLORS['accent']}; border-color: {COLORS['accent']}; }}
        QPushButton#IconButton {{
            min-width: 42px; max-width: 42px; min-height: 42px; max-height: 42px;
            padding: 0; border-radius: 21px; background: rgba(255,255,255,16);
            border: 1px solid rgba(255,255,255,32);
        }}
        QPushButton#IconButton:hover {{
            background: rgba(255,255,255,30); border-color: rgba(255,255,255,62);
        }}
        QPushButton#IconButton:pressed {{ background: rgba(255,255,255,42); }}
        QPushButton#IconButton[variant="primary"] {{
            min-width: 48px; max-width: 48px; min-height: 48px; max-height: 48px;
            border-radius: 24px; background: {COLORS['accent']}; border: none;
        }}
        QPushButton#IconButton[variant="primary"]:hover {{ background: {COLORS['accent_hover']}; }}
        QSlider#PartyProgress::groove:horizontal, QSlider#PartyVolume::groove:horizontal {{
            height: 4px; background: rgba(255,255,255,30); border-radius: 2px;
        }}
        QSlider#PartyProgress::sub-page:horizontal, QSlider#PartyVolume::sub-page:horizontal {{
            background: {COLORS['accent']}; border-radius: 2px;
        }}
        QSlider#PartyProgress::handle:horizontal, QSlider#PartyVolume::handle:horizontal {{
            width: 12px; margin: -4px 0; border-radius: 6px; background: #F5F8FC;
        }}
        """

    def apply_settings(self, settings: Mapping[str, object] | None) -> None:
        merged = dict(self._settings)
        lyrics_merged = dict(self._lyrics_settings)
        if isinstance(settings, Mapping):
            merged.update(settings)
            lyrics_merged.update(settings)
        self._settings = normalize_party_mode_settings(merged)
        self._lyrics_settings = normalize_lyrics_settings(lyrics_merged)
        self.canvas.set_preset(str(self._settings["party_mode_preset"]))
        self._apply_canvas_audio_features()
        self.canvas.set_quality(str(self._settings["party_mode_quality"]))
        self.canvas.set_reduced_motion(bool(self._settings["party_mode_reduced_motion"]))
        if hasattr(self.canvas, "set_show_artwork"):
            self.canvas.set_show_artwork(bool(self._settings["party_mode_show_artwork"]))
        frame_rate = str(self._settings["party_mode_frame_rate"])
        if hasattr(self.canvas, "set_frame_rate"):
            self.canvas.set_frame_rate(frame_rate)
        self.preset_button.setText(
            party_preset_label(self._settings["party_mode_preset"])
        )
        self.lyrics_panel.set_reduced_motion(
            bool(self._settings["party_mode_reduced_motion"])
        )
        self.lyrics_controller.apply_settings(self._lyrics_settings)
        self._update_lyrics_button()
        self.refresh_from_host(force=True)
        self.lyrics_controller.resume()
        self._position_lyrics_panel()
        self._restart_overlay_timer()

    def show_on_screen(self, screen: object | None) -> None:
        self._closed_notified = False
        self._overlay_hovered = False
        self._connect_player_signals()
        self.refresh_from_host(force=True)
        if screen is not None:
            try:
                self.setGeometry(screen.geometry())
                self.winId()
                handle = self.windowHandle()
                if handle is not None:
                    handle.setScreen(screen)
            except (AttributeError, RuntimeError):
                pass
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.canvas.start_rendering()
        self.state_timer.start()
        self.fallback_timer.start()
        self.show_overlay()
        self._position_lyrics_panel()

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if self._route_key(event):
                return True
        if event.type() == QEvent.Type.MouseMove:
            self.show_overlay()
        elif event.type() == QEvent.Type.Enter and isinstance(watched, QWidget):
            if self._is_overlay_control(watched):
                self._overlay_hovered = True
                self.show_overlay()
        elif event.type() == QEvent.Type.Leave and isinstance(watched, QWidget):
            if self._is_overlay_control(watched):
                self._overlay_hovered = False
                self._restart_overlay_timer()
        return super().eventFilter(watched, event)

    def _is_overlay_control(self, watched: QWidget) -> bool:
        """Return whether ``watched`` belongs to an interactive overlay area."""

        return any(
            control is watched or control.isAncestorOf(watched)
            for control in (
                self.controls_panel,
                self.lyrics_button,
                self.preset_button,
                self.help_button,
                self.exit_button,
            )
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._route_key(event):
            return
        super().keyPressEvent(event)

    def _route_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_F11):
            self.close()
            return True
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit, QComboBox, QAbstractSpinBox)):
            return False
        modifiers = event.modifiers()
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if key != Qt.Key.Key_H:
            self.show_overlay()
        if key == Qt.Key.Key_Space:
            self._play_pause()
        elif key == Qt.Key.Key_Left and control:
            self._previous()
        elif key == Qt.Key.Key_Right and control:
            self._next()
        elif key == Qt.Key.Key_Left:
            self.seek_relative(-10_000)
        elif key == Qt.Key.Key_Right:
            self.seek_relative(10_000)
        elif key == Qt.Key.Key_Up:
            self.adjust_volume(5)
        elif key == Qt.Key.Key_Down:
            self.adjust_volume(-5)
        elif key == Qt.Key.Key_M:
            self.toggle_mute()
        elif key == Qt.Key.Key_V:
            self.cycle_preset()
        elif key == Qt.Key.Key_L:
            self.toggle_lyrics()
        elif key == Qt.Key.Key_PageUp:
            if not self.lyrics_panel.page_scroll(-1):
                return False
        elif key == Qt.Key.Key_PageDown:
            if not self.lyrics_panel.page_scroll(1):
                return False
        elif key == Qt.Key.Key_H:
            self.toggle_overlay()
        elif key == Qt.Key.Key_S:
            self._toggle_shuffle()
        elif key == Qt.Key.Key_A:
            self._toggle_auto()
        elif key == Qt.Key.Key_R:
            self._cycle_repeat()
        elif event.text() == "?" or key == Qt.Key.Key_Question:
            self.toggle_help()
        else:
            return False
        return True

    def show_overlay(self) -> None:
        if not self._help_visible:
            self.root_stack.setCurrentWidget(self.overlay)
        self._overlay_visible = True
        self.overlay.show()
        self.overlay_animation.stop()
        if bool(self._settings["party_mode_reduced_motion"]):
            self.overlay_effect.setOpacity(1.0)
        else:
            self.overlay_animation.setStartValue(self.overlay_effect.opacity())
            self.overlay_animation.setEndValue(1.0)
            self.overlay_animation.start()
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self._restart_overlay_timer()

    def hide_overlay(self) -> None:
        if self._help_visible:
            return
        self._overlay_visible = False
        self.overlay_animation.stop()
        if bool(self._settings["party_mode_reduced_motion"]):
            self.overlay_effect.setOpacity(0.0)
        else:
            self.overlay_animation.setStartValue(self.overlay_effect.opacity())
            self.overlay_animation.setEndValue(0.0)
            self.overlay_animation.start()
        self.setCursor(QCursor(Qt.CursorShape.BlankCursor))
        self.overlay_timer.stop()

    def toggle_overlay(self) -> None:
        if self._overlay_visible:
            self.hide_overlay()
        else:
            self.show_overlay()

    def _restart_overlay_timer(self) -> None:
        self.overlay_timer.stop()
        if (
            self.isVisible()
            and bool(self._settings["party_mode_auto_hide_overlay"])
            and self._overlay_visible
            and not self._help_visible
        ):
            seconds = int(self._settings["party_mode_overlay_timeout_seconds"])
            self.overlay_timer.start(seconds * 1_000)

    def _hide_overlay_if_idle(self) -> None:
        if self._overlay_hovered:
            self.overlay_timer.start(500)
        else:
            self.hide_overlay()

    def toggle_help(self) -> None:
        self._help_visible = not self._help_visible
        self.help_panel.setVisible(self._help_visible)
        if self._help_visible:
            self.lyrics_panel.hide()
            self._update_firework_lyrics_protection()
            self.root_stack.setCurrentWidget(self.help_panel)
            self.show_overlay()
            self.overlay_timer.stop()
        else:
            self.root_stack.setCurrentWidget(self.overlay)
            self._position_lyrics_panel()
            self._restart_overlay_timer()

    def cycle_preset(self) -> str:
        current = str(self._settings["party_mode_preset"])
        index = PARTY_PRESETS.index(current) if current in PARTY_PRESETS else 0
        preset = PARTY_PRESETS[(index + 1) % len(PARTY_PRESETS)]
        self._settings["party_mode_preset"] = preset
        self.canvas.set_preset(preset)
        self._apply_canvas_audio_features()
        self.preset_button.setText(party_preset_label(preset))
        host = self._host()
        if host is not None:
            host.config["party_mode_preset"] = preset
            host.config["party_mode_config_version"] = PARTY_MODE_CONFIG_VERSION
            host.save_config()
        self.preset_changed.emit(preset)
        return preset

    def toggle_lyrics(self) -> bool:
        enabled = not bool(self._lyrics_settings["party_mode_lyrics_enabled"])
        self._lyrics_settings["party_mode_lyrics_enabled"] = enabled
        host = self._host()
        if host is not None:
            host.config.update(self._lyrics_settings)
            host.save_config()
        self.lyrics_controller.apply_settings(self._lyrics_settings)
        self._update_lyrics_button()
        self._on_lyrics_state_changed(
            self.lyrics_controller.lyrics_available,
            self.lyrics_controller.lyrics_synchronized,
        )
        return enabled

    def refresh_lyrics(self) -> None:
        if not bool(self._lyrics_settings["party_mode_lyrics_enabled"]):
            self.toggle_lyrics()
        self.lyrics_controller.refresh()

    def import_lyrics(self) -> None:
        if self._lyrics_identity is None:
            return
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Import Lyrics",
            str(Path(self._lyrics_identity.media_path or "").parent),
            "Lyrics (*.lrc *.txt)",
        )
        if not path:
            return
        if not bool(self._lyrics_settings["party_mode_lyrics_enabled"]):
            self.toggle_lyrics()
        self.lyrics_controller.import_manual(path)

    def clear_automatic_lyrics(self) -> None:
        self.lyrics_controller.clear_automatic()

    def prepare_lyrics_cache_clear(self) -> None:
        self.lyrics_controller.suspend()

    def lyrics_cache_cleared(self) -> None:
        self.lyrics_controller.reload_local_only()

    def open_lyrics_settings(self) -> None:
        host = self._host()
        self.close()
        if host is not None:
            pages = getattr(host, "pages", None)
            if pages is not None:
                pages.setCurrentIndex(2)
            host.show()
            host.raise_()
            host.activateWindow()

    def _request_lyrics_consent(self, identity: object) -> None:
        if not isinstance(identity, TrackLyricsIdentity):
            return
        enabled = request_online_lyrics_consent(self, identity)
        self._lyrics_settings["lyrics_lookup_consent_version"] = (
            LYRICS_CONSENT_VERSION
        )
        self._lyrics_settings["lyrics_online_lookup_enabled"] = enabled
        host = self._host()
        if host is not None:
            host.config.update(self._lyrics_settings)
            host.save_config()
            refresh = getattr(host, "refresh_settings_status", None)
            if callable(refresh):
                refresh()
        self.lyrics_controller.apply_settings(self._lyrics_settings)

    def _update_lyrics_button(self) -> None:
        enabled = bool(self._lyrics_settings["party_mode_lyrics_enabled"])
        self.lyrics_button.setProperty("active", enabled)
        self.lyrics_button.setText("Lyrics")
        self.lyrics_button.setToolTip("Lyrics (L)")
        self.lyrics_button.style().unpolish(self.lyrics_button)
        self.lyrics_button.style().polish(self.lyrics_button)

    def _on_lyrics_state_changed(
        self,
        available: bool,
        synchronized: bool,
    ) -> None:
        enabled = bool(self._lyrics_settings["party_mode_lyrics_enabled"])
        self._position_lyrics_panel()
        self.lyrics_status_changed.emit(
            enabled,
            bool(available),
            bool(synchronized),
        )

    def on_audio_features(self, features: AudioFeatures) -> None:
        if not isinstance(features, AudioFeatures):
            return
        host = self._host()
        player = getattr(host, "player", None) if host is not None else None
        playing = bool(
            player is not None
            and player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        available = (
            playing and features.sample_count > 0 and features.sample_rate > 0
        )
        if available:
            self._last_audio_feature_at = time.monotonic()
            self._latest_audio_features = features
        else:
            self._latest_audio_features = None
        self._set_audio_reactivity_available(available)
        self._apply_canvas_audio_features()

    def _apply_canvas_audio_features(self) -> None:
        """Keep Static genuinely idle while retaining the latest audio state."""

        if self.current_preset == "static":
            return
        self.canvas.set_features(self._latest_audio_features)

    def _update_audio_capability(self) -> None:
        if self._last_audio_feature_at <= 0.0:
            self._set_audio_reactivity_available(False)
            return
        if time.monotonic() - self._last_audio_feature_at > 1.5:
            self._set_audio_reactivity_available(False)

    def _set_audio_reactivity_available(self, available: bool) -> None:
        normalized = bool(available)
        if not normalized:
            self._latest_audio_features = None
        if normalized == self._audio_reactivity_available:
            return
        self._audio_reactivity_available = normalized
        if hasattr(self.canvas, "set_audio_reactivity_available"):
            self.canvas.set_audio_reactivity_available(normalized)
        self.audio_reactivity_changed.emit(normalized)

    def refresh_from_host(self, force: bool = False) -> None:
        host = self._host()
        if host is None:
            return
        track_id = getattr(host, "current_track_id", None)
        if force or track_id != self._current_track_id:
            self._current_track_id = track_id
            track = host.db.get_track(track_id) if track_id else None
            self._set_track(track)
        self._sync_modes()
        player = getattr(host, "player", None)
        if player is not None:
            self._set_position(player.position())
            self._set_duration(player.duration())
            self._set_playback_state(player.playbackState())
        volume = max(0, min(100, int(getattr(host, "volume_percent", 0))))
        with QSignalBlocker(self.volume_slider):
            self.volume_slider.setValue(volume)

    def _set_track(self, track: object | None) -> None:
        if track is None:
            self._lyrics_identity = None
            self.lyrics_controller.set_track(None)
            self.title_label.setText("Choose a song to begin")
            self.artist_label.setText("Music Vault is ready")
            self.album_label.setText("")
            self.overlay_artwork.setPixmap(
                render_icon_pixmap("music-note", 34, COLORS["text_primary"])
            )
            self.canvas.set_artwork(None)
            self.canvas.set_track_text("Choose a song to begin", "", "")
            self._transition_palette(DEFAULT_PARTY_PALETTE)
            if hasattr(self.canvas, "set_playback_state"):
                self.canvas.set_playback_state(False, False)
            return
        try:
            path = Path(str(track["path"]))
            title = str(track["title"] or path.stem)
            artist = str(track["artist"] or "Unknown Artist")
            album = str(track["album"] or "")
            cover_path = str(track["cover_path"] or "")
        except (KeyError, TypeError, IndexError):
            return
        host = self._host()
        player = getattr(host, "player", None) if host is not None else None
        duration = max(0, int(player.duration())) if player is not None else 0
        self._lyrics_identity = TrackLyricsIdentity(
            track_id=self._current_track_id or "current",
            title=title,
            artist=artist,
            album=album,
            duration_ms=duration or None,
            media_path=path,
        )
        self.lyrics_controller.set_track(self._lyrics_identity)
        self.title_label.setText(title)
        self.artist_label.setText(artist)
        self.album_label.setText(album)
        artwork = self._load_bounded_artwork(cover_path)
        show_artwork = bool(self._settings["party_mode_show_artwork"])
        if show_artwork and not artwork.isNull():
            self.overlay_artwork.setPixmap(
                artwork.scaled(
                    84,
                    84,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.canvas.set_artwork(artwork)
            self._transition_palette(self._palette_extractor.extract(cover_path))
        else:
            self.overlay_artwork.setPixmap(
                render_icon_pixmap("music-note", 34, COLORS["text_primary"])
            )
            self.canvas.set_artwork(None)
            self._transition_palette(DEFAULT_PARTY_PALETTE)
        self.canvas.set_track_text(title, artist, album)

    @staticmethod
    def _load_bounded_artwork(cover_path: str, edge: int = 1_024) -> QPixmap:
        if not cover_path or not Path(cover_path).is_file():
            return QPixmap()
        reader = QImageReader(cover_path)
        reader.setAutoTransform(True)
        size = reader.size()
        maximum = max(64, min(2_048, int(edge)))
        if size.isValid() and max(size.width(), size.height()) > maximum:
            scale = maximum / max(size.width(), size.height())
            reader.setScaledSize(
                QSize(
                    max(1, round(size.width() * scale)),
                    max(1, round(size.height() * scale)),
                )
            )
        image = reader.read()
        return QPixmap.fromImage(image) if not image.isNull() else QPixmap()

    def _transition_palette(self, target: ArtworkPalette) -> None:
        if target == self._palette:
            return
        if bool(self._settings["party_mode_reduced_motion"]):
            self.palette_timer.stop()
            self._palette = target
            self.canvas.set_palette(target)
            return
        self._palette_from = self._palette
        self._palette_to = target
        self._palette_started_at = time.monotonic()
        self.palette_timer.start()

    def _advance_palette(self) -> None:
        elapsed = max(0.0, time.monotonic() - self._palette_started_at)
        amount = min(1.0, elapsed / 0.7)
        eased = amount * amount * (3.0 - (2.0 * amount))
        self._palette = interpolate_palette(
            self._palette_from, self._palette_to, eased
        )
        self.canvas.set_palette(self._palette)
        if amount >= 1.0:
            self.palette_timer.stop()

    def _sync_modes(self) -> None:
        host = self._host()
        if host is None:
            return
        autoplay = bool(getattr(host, "autoplay_enabled", False))
        shuffle = bool(getattr(host, "shuffle_enabled", False))
        repeat = str(getattr(host, "repeat_mode", "off"))
        self.auto_button.setProperty("active", autoplay)
        self.shuffle_button.setProperty("active", shuffle)
        self.repeat_button.setProperty("active", repeat != "off")
        self.auto_button.style().unpolish(self.auto_button)
        self.auto_button.style().polish(self.auto_button)
        self.shuffle_button.style().unpolish(self.shuffle_button)
        self.shuffle_button.style().polish(self.shuffle_button)
        self.repeat_button.style().unpolish(self.repeat_button)
        self.repeat_button.style().polish(self.repeat_button)
        repeat_icon = "repeat-one" if repeat == "one" else "repeat"
        self.repeat_button.setIcon(ui_icon(repeat_icon, 17))
        self.repeat_button.setToolTip(f"Repeat {repeat} (R)")
        self.queue_label.setText(f"Q: {len(getattr(host, 'manual_queue', []))}")

    def _set_position(self, position: int) -> None:
        value = max(0, int(position))
        if not self.progress_slider.isSliderDown():
            with QSignalBlocker(self.progress_slider):
                self.progress_slider.setValue(value)
        self.elapsed_label.setText(self._format_time(value))
        self.lyrics_controller.set_position(value)

    def _set_duration(self, duration: int) -> None:
        value = max(0, int(duration))
        self.progress_slider.setRange(0, value)
        self.duration_label.setText(self._format_time(value))
        identity = self._lyrics_identity
        if (
            identity is not None
            and value > 0
            and (
                identity.duration_ms is None
                or abs(identity.duration_ms - value) > 1_000
            )
        ):
            self._lyrics_identity = TrackLyricsIdentity(
                track_id=identity.track_id,
                title=identity.title,
                artist=identity.artist,
                album=identity.album,
                duration_ms=value,
                media_path=identity.media_path,
            )
            self.lyrics_controller.set_track(self._lyrics_identity)

    def _set_playback_state(self, state: object) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        if not playing:
            self._set_audio_reactivity_available(False)
        self.play_button.setIcon(ui_icon("pause" if playing else "play", 24))
        self.play_button.setToolTip("Pause (Space)" if playing else "Play (Space)")
        if hasattr(self.canvas, "set_playback_state"):
            self.canvas.set_playback_state(playing, self._current_track_id is not None)

    @staticmethod
    def _format_time(milliseconds: int) -> str:
        seconds = max(0, int(milliseconds) // 1_000)
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _seek_from_slider(self) -> None:
        host = self._host()
        player = getattr(host, "player", None) if host is not None else None
        if player is not None and player.isSeekable():
            player.setPosition(self.progress_slider.value())

    def seek_relative(self, milliseconds: int) -> int:
        host = self._host()
        player = getattr(host, "player", None) if host is not None else None
        if player is None or not player.isSeekable():
            return player.position() if player is not None else 0
        destination = max(0, min(player.duration(), player.position() + int(milliseconds)))
        player.setPosition(destination)
        return destination

    def adjust_volume(self, delta: int) -> int:
        host = self._host()
        if host is None:
            return 0
        value = max(0, min(100, int(getattr(host, "volume_percent", 0)) + int(delta)))
        host.volume_slider.setValue(value)
        return value

    def _volume_changed(self, value: int) -> None:
        host = self._host()
        slider = getattr(host, "volume_slider", None) if host is not None else None
        if slider is not None:
            slider.setValue(max(0, min(100, int(value))))

    def toggle_mute(self) -> bool:
        host = self._host()
        output = getattr(host, "audio_output", None) if host is not None else None
        if output is None:
            return False
        muted = bool(output.isMuted())
        if not muted:
            self._last_nonzero_volume = max(1, int(getattr(host, "volume_percent", 70)))
        output.setMuted(not muted)
        return not muted

    def _play_pause(self) -> None:
        host = self._host()
        if host is not None:
            host.toggle_play()

    def _previous(self) -> None:
        host = self._host()
        if host is not None:
            host.play_previous()

    def _next(self) -> None:
        host = self._host()
        if host is not None:
            host.play_next()

    def _toggle_auto(self) -> None:
        host = self._host()
        if host is not None:
            host.toggle_autoplay()
            self._sync_modes()

    def _toggle_shuffle(self) -> None:
        host = self._host()
        if host is not None:
            host.toggle_shuffle()
            self._sync_modes()

    def _cycle_repeat(self) -> None:
        host = self._host()
        if host is not None:
            host.cycle_repeat()
            self._sync_modes()

    def performance_metrics(self) -> dict[str, object]:
        metrics = dict(self.canvas.performance_metrics())
        metrics["lyrics_pending_count"] = self.lyrics_controller.pending_count
        return metrics

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._position_lyrics_panel()

    def _position_lyrics_panel(self) -> None:
        panel = getattr(self, "lyrics_panel", None)
        root = self.centralWidget()
        if panel is None or root is None:
            return
        if (
            self._help_visible
            or not bool(self._lyrics_settings["party_mode_lyrics_enabled"])
            or panel.presentation_mode == "hidden"
        ):
            panel.hide()
            self._update_firework_lyrics_protection()
            return
        width = max(320, min(1_040, root.width() - 72))
        mode = panel.presentation_mode
        compact = root.height() <= 800
        panel.set_compact(compact)
        preferred_height = {
            "synchronized": 154,
            "plain": 292,
            "state": 96,
        }.get(mode, 120)
        if compact:
            preferred_height = {
                "synchronized": 24,
                "state": 24,
                "plain": 140,
            }.get(mode, 24)
        x = max(0, (root.width() - width) // 2)
        controls_top = self.controls_panel.mapTo(
            root, self.controls_panel.rect().topLeft()
        ).y()
        protected_bottom = round(center_content_bottom(root.width(), root.height()))
        content_y = protected_bottom + 4
        available_height = max(0, controls_top - 8 - content_y)
        minimum_height = 120 if mode == "plain" else preferred_height
        if available_height >= minimum_height:
            height = min(preferred_height, available_height)
            y = controls_top - 8 - height
        else:
            artwork_top = center_artwork_rect(root.width(), root.height()).top()
            height = preferred_height
            y = max(76, round(artwork_top - height - 12))
        panel.setGeometry(x, y, width, height)
        panel.show()
        panel.raise_()
        self._update_firework_lyrics_protection()

    def _update_firework_lyrics_protection(self) -> None:
        """Keep firework burst centers outside the visible lyrics overlay."""

        panel = getattr(self, "lyrics_panel", None)
        canvas = getattr(self, "canvas", None)
        if panel is None or canvas is None:
            return
        if not panel.isVisible() or canvas.width() <= 0 or canvas.height() <= 0:
            canvas.set_firework_protected_rects(())
            return
        top_left = panel.mapTo(canvas, panel.rect().topLeft())
        padding = 12
        canvas_width = float(canvas.width())
        canvas_height = float(canvas.height())
        canvas.set_firework_protected_rects(
            (
                (
                    (top_left.x() - padding) / canvas_width,
                    (top_left.y() - padding) / canvas_height,
                    (top_left.x() + panel.width() + padding) / canvas_width,
                    (top_left.y() + panel.height() + padding) / canvas_height,
                ),
            )
        )

    def _host(self) -> Any | None:
        return self._host_ref()

    def _connect_player_signals(self) -> None:
        if self._player_signals_connected:
            return
        host = self._host()
        player = getattr(host, "player", None) if host is not None else None
        if player is None:
            return
        player.positionChanged.connect(self._set_position)
        player.durationChanged.connect(self._set_duration)
        player.playbackStateChanged.connect(self._set_playback_state)
        self._player_signals_connected = True

    def _disconnect_player_signals(self) -> None:
        if not self._player_signals_connected:
            return
        host = self._host()
        player = getattr(host, "player", None) if host is not None else None
        if player is not None:
            for signal, slot in (
                (player.positionChanged, self._set_position),
                (player.durationChanged, self._set_duration),
                (player.playbackStateChanged, self._set_playback_state),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._player_signals_connected = False

    def shutdown(self) -> None:
        """Close the reusable surface and release its one lyrics worker."""

        self.close()
        self.lyrics_controller.close()

    def closeEvent(self, event: object) -> None:
        self.lyrics_controller.suspend()
        self._disconnect_player_signals()
        self._overlay_hovered = False
        self.overlay_timer.stop()
        self.state_timer.stop()
        self.fallback_timer.stop()
        self.palette_timer.stop()
        self.canvas.stop_rendering()
        self.help_panel.hide()
        self._help_visible = False
        self._set_audio_reactivity_available(False)
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        host = self._host()
        if host is not None:
            QTimer.singleShot(0, host.activateWindow)
        self._notify_closed()
        super().closeEvent(event)

    def hideEvent(self, event: object) -> None:
        """Suspend all Party Mode work whenever the top-level surface is hidden."""

        self.lyrics_controller.suspend()
        self._disconnect_player_signals()
        self._overlay_hovered = False
        self.overlay_timer.stop()
        self.state_timer.stop()
        self.fallback_timer.stop()
        self.palette_timer.stop()
        self.canvas.stop_rendering()
        self.canvas.set_firework_protected_rects(())
        self._last_audio_feature_at = 0.0
        self._latest_audio_features = None
        self._set_audio_reactivity_available(False)
        self._notify_closed()
        super().hideEvent(event)

    def _notify_closed(self) -> None:
        if self._closed_notified:
            return
        self._closed_notified = True
        self.party_closed.emit()


__all__ = [
    "PARTY_FRAME_RATES",
    "PARTY_MODE_CONFIG_VERSION",
    "PARTY_MODE_DEFAULTS",
    "PARTY_PRESET_LABELS",
    "PARTY_PRESETS",
    "PARTY_QUALITIES",
    "PartyAudioAnalysisThread",
    "PartyModeWindow",
    "normalize_party_mode_settings",
    "party_preset_label",
    "party_preset_value",
]
