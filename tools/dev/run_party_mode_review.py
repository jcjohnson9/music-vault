from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, is_dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
import wave


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RATE = 44_100
PRESETS = ("pulse", "starfield", "aurora")
ALLOWED_SCALES = (1.0, 1.25, 1.5)


@dataclass(frozen=True, slots=True)
class ReviewScene:
    name: str
    preset: str
    audio: str
    width: int
    height: int
    artwork: str = "primary"
    title: str = "Synthetic Signal"
    artist: str = "Music Vault Review"
    album: str = "Synthetic Party Mode"
    quality: str = "auto"
    reduced_motion: bool = False
    playing: bool = True


@dataclass(frozen=True, slots=True)
class WindowReviewScene:
    name: str
    preset: str
    audio: str
    width: int
    height: int
    overlay_visible: bool = True
    help_visible: bool = False
    queue_count: int = 0


CANVAS_SCENES = (
    ReviewScene(
        "idle_no_track",
        "pulse",
        "silence",
        1280,
        720,
        artwork="missing",
        title="Choose a song to begin",
        artist="",
        album="",
        playing=False,
    ),
    ReviewScene("pulse_quiet", "pulse", "quiet", 1920, 1080),
    ReviewScene("pulse_simulated_beat", "pulse", "beat", 1920, 1080),
    ReviewScene("starfield_mid", "starfield", "mid", 2560, 1440),
    ReviewScene("aurora_high", "aurora", "high", 2560, 1440),
    ReviewScene(
        "missing_artwork_fallback",
        "pulse",
        "bass",
        1280,
        720,
        artwork="missing",
    ),
    ReviewScene(
        "paused_state",
        "pulse",
        "silence",
        1920,
        1080,
        playing=False,
    ),
    ReviewScene(
        "reduced_motion",
        "aurora",
        "mid",
        1920,
        1080,
        reduced_motion=True,
    ),
    ReviewScene(
        "long_metadata",
        "pulse",
        "bass",
        2560,
        1440,
        title="A Deliberately Long Synthetic Track Title for Elision and Contrast Review",
        artist="The Entirely Fictional Music Vault Review Ensemble with Additional Guests",
        album="Synthetic Long-Value Collection",
    ),
    ReviewScene(
        "track_change",
        "starfield",
        "high",
        1920,
        1080,
        artwork="secondary",
        title="Synthetic Track Two",
        artist="A Different Fictional Artist",
    ),
)

WINDOW_SCENES = (
    WindowReviewScene("pulse_bass_overlay", "pulse", "bass", 1920, 1080),
    WindowReviewScene(
        "pulse_overlay_hidden",
        "pulse",
        "bass",
        1280,
        720,
        overlay_visible=False,
    ),
    WindowReviewScene(
        "queue_count_visible",
        "starfield",
        "beat",
        1920,
        1080,
        queue_count=3,
    ),
    WindowReviewScene(
        "shortcut_help_overlay",
        "aurora",
        "quiet",
        3840,
        2160,
        help_visible=True,
    ),
)

SCALE_SMOKE_SCENES = (
    WindowReviewScene("pulse_bass_overlay", "pulse", "bass", 1280, 720),
    WindowReviewScene(
        "shortcut_help_overlay",
        "aurora",
        "quiet",
        1920,
        1080,
        help_visible=True,
    ),
)


class NetworkAccessBlocked(RuntimeError):
    pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Music Vault Party Mode against temporary synthetic audio, artwork, "
            "and metadata using Qt's offscreen platform."
        )
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=60,
        help="Paint iterations per benchmark (12-600; default: 60).",
    )
    parser.add_argument(
        "--scale",
        type=float,
        choices=ALLOWED_SCALES,
        default=1.0,
        help="Qt scale factor for this review process.",
    )
    parser.add_argument(
        "--capture-profile",
        choices=("full", "scale-smoke"),
        default="full",
        help=(
            "Capture the complete 14-state matrix, or exactly two representative "
            "PartyModeWindow states for non-default scale smoke checks."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional empty directory for retained captures and sanitized summary. "
            "Inside the repository, only .ui-review is accepted."
        ),
    )
    return parser.parse_args(argv)


def _install_network_guard() -> None:
    """Fail closed if reviewed Python code attempts a network operation."""

    guarded_events = {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.sendto",
    }

    def audit(event: str, _arguments: tuple[object, ...]) -> None:
        if event in guarded_events:
            raise NetworkAccessBlocked(
                f"Party Mode review blocked network audit event: {event}"
            )

    sys.addaudithook(audit)


def _prepare_output(path: Path | None) -> Path | None:
    if path is None:
        return None
    destination = path.expanduser().resolve()
    permitted = (PROJECT_ROOT / ".ui-review").resolve()
    if destination.is_relative_to(PROJECT_ROOT) and not destination.is_relative_to(
        permitted
    ):
        raise ValueError(
            "Repository-contained Party Mode output is allowed only under .ui-review/."
        )
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Party Mode review output directory must be empty.")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _tone_pcm(
    frequency: float,
    amplitude: float,
    *,
    duration: float = 0.32,
    beat: bool = False,
) -> bytes:
    samples = bytearray()
    frame_count = max(1, int(SAMPLE_RATE * duration))
    for index in range(frame_count):
        moment = index / SAMPLE_RATE
        envelope = 1.0
        if beat:
            phase = moment % 0.16
            envelope = math.exp(-phase * 28.0)
        sample = math.sin(math.tau * frequency * moment) * amplitude * envelope
        samples.extend(struct.pack("<h", round(max(-1.0, min(1.0, sample)) * 32767)))
    return bytes(samples)


def _synthetic_pcm() -> dict[str, bytes]:
    return {
        "silence": bytes(int(SAMPLE_RATE * 0.32) * 2),
        "quiet": _tone_pcm(440.0, 0.018),
        "bass": _tone_pcm(90.0, 0.78),
        "mid": _tone_pcm(1_000.0, 0.62),
        "high": _tone_pcm(5_200.0, 0.48),
        "beat": _tone_pcm(90.0, 0.9, beat=True),
    }


def _write_synthetic_wav(path: Path, pcm: Mapping[str, bytes]) -> None:
    gap = bytes(int(SAMPLE_RATE * 0.04) * 2)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        for name in ("quiet", "bass", "mid", "high", "beat"):
            target.writeframes(pcm[name])
            target.writeframes(gap)
    with wave.open(str(path), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != SAMPLE_RATE
            or source.getnframes() <= 0
        ):
            raise RuntimeError("Synthetic review WAV failed its structural check.")


def _load_party_api():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from PySide6.QtCore import (
            QCoreApplication,
            QEventLoop,
            QRectF,
            QTimer,
            Qt,
            QUrl,
        )
        from PySide6.QtGui import (
            QColor,
            QConicalGradient,
            QImage,
            QLinearGradient,
            QPainter,
            QPixmap,
        )
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QSlider
        from music_vault.core.audio_analysis import AudioAnalyzer, AudioFeatures
        from music_vault.ui.party_palette import DEFAULT_PARTY_PALETTE, PaletteExtractor
        from music_vault.ui.party_mode import PartyModeWindow
        from music_vault.ui.party_visuals import (
            AdaptiveQualityController,
            PartyCanvas,
            PartyVisualEngine,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Party Mode review dependencies are unavailable. Finish the PartyCanvas "
            "implementation and install the project development requirements."
        ) from exc
    return {
        "QApplication": QApplication,
        "QCoreApplication": QCoreApplication,
        "QEventLoop": QEventLoop,
        "QRectF": QRectF,
        "QTimer": QTimer,
        "Qt": Qt,
        "QUrl": QUrl,
        "QColor": QColor,
        "QConicalGradient": QConicalGradient,
        "QImage": QImage,
        "QLinearGradient": QLinearGradient,
        "QPainter": QPainter,
        "QPixmap": QPixmap,
        "QAudioOutput": QAudioOutput,
        "QMediaPlayer": QMediaPlayer,
        "QLabel": QLabel,
        "QMainWindow": QMainWindow,
        "QSlider": QSlider,
        "AudioAnalyzer": AudioAnalyzer,
        "AudioFeatures": AudioFeatures,
        "DEFAULT_PARTY_PALETTE": DEFAULT_PARTY_PALETTE,
        "PaletteExtractor": PaletteExtractor,
        "PartyModeWindow": PartyModeWindow,
        "PartyCanvas": PartyCanvas,
        "PartyVisualEngine": PartyVisualEngine,
        "AdaptiveQualityController": AdaptiveQualityController,
    }


def _generate_artwork(api: Mapping[str, Any], path: Path, variant: int) -> Any:
    QImage = api["QImage"]
    QColor = api["QColor"]
    QLinearGradient = api["QLinearGradient"]
    QConicalGradient = api["QConicalGradient"]
    QPainter = api["QPainter"]
    QRectF = api["QRectF"]

    colors = (
        ("#0b1728", "#19b66a", "#7657d5"),
        ("#10112c", "#3a8cf0", "#df6e54"),
    )[variant % 2]
    image = QImage(512, 512, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(colors[0]))
    painter = QPainter(image)
    background = QLinearGradient(0, 0, 512, 512)
    background.setColorAt(0.0, QColor(colors[1]))
    background.setColorAt(1.0, QColor(colors[2]))
    painter.fillRect(QRectF(0, 0, 512, 512), background)
    painter.setPen(QColor(255, 255, 255, 105))
    painter.setBrush(QColor(5, 10, 20, 110))
    painter.drawEllipse(QRectF(76, 76, 360, 360))
    ring = QConicalGradient(256, 256, 30 + variant * 70)
    ring.setColorAt(0.0, QColor(255, 255, 255, 220))
    ring.setColorAt(0.5, QColor(colors[1]))
    ring.setColorAt(1.0, QColor(255, 255, 255, 220))
    painter.setPen(QColor(255, 255, 255, 120))
    painter.setBrush(ring)
    painter.drawEllipse(QRectF(166, 166, 180, 180))
    painter.end()
    if not image.save(str(path), "PNG"):
        raise RuntimeError("Could not create synthetic Party Mode artwork.")
    return api["QPixmap"].fromImage(image)


def _analyze_scenes(api: Mapping[str, Any], pcm: Mapping[str, bytes]) -> dict[str, Any]:
    analyzer = api["AudioAnalyzer"]()
    features: dict[str, Any] = {}
    for index, name in enumerate(("silence", "quiet", "bass", "mid", "high", "beat")):
        value = analyzer.process_pcm(
            pcm[name],
            "s16",
            1,
            SAMPLE_RATE,
            timestamp_ms=1_000 + index * 50,
        )
        if name == "beat":
            value = replace(value, beat=True, beat_strength=max(0.85, value.beat_strength))
        features[name] = value
    return features


def _call_optional(target: object, name: str, *args: object) -> bool:
    method = getattr(target, name, None)
    if not callable(method):
        return False
    method(*args)
    return True


def _set_artwork(canvas: object, image: object | None, path: Path | None) -> None:
    method = getattr(canvas, "set_artwork", None)
    if not callable(method):
        raise RuntimeError("PartyCanvas is missing set_artwork().")
    try:
        method(image)
    except TypeError:
        method(str(path) if path is not None else None)


def _set_track_text(canvas: object, scene: ReviewScene) -> None:
    method = getattr(canvas, "set_track_text", None)
    if not callable(method):
        raise RuntimeError("PartyCanvas is missing set_track_text().")
    try:
        method(scene.title, scene.artist, scene.album)
    except TypeError:
        method(scene.title, scene.artist)


def _configure_canvas(
    canvas: object,
    scene: ReviewScene,
    features: Mapping[str, object],
    artwork: Mapping[str, tuple[object | None, Path | None, object]],
) -> dict[str, bool]:
    required = (
        "set_preset",
        "set_quality",
        "set_reduced_motion",
        "set_features",
        "start_rendering",
        "stop_rendering",
        "performance_metrics",
    )
    missing = [name for name in required if not callable(getattr(canvas, name, None))]
    if missing:
        raise RuntimeError(f"PartyCanvas review API is incomplete: {', '.join(missing)}")

    canvas.set_preset(scene.preset)
    canvas.set_quality(scene.quality)
    canvas.set_reduced_motion(scene.reduced_motion)
    canvas.set_features(features[scene.audio])
    selected = artwork.get(scene.artwork)
    _set_artwork(canvas, selected[0] if selected else None, selected[1] if selected else None)
    fallback = artwork["missing"][2]
    canvas.set_palette(selected[2] if selected else fallback)
    _set_track_text(canvas, scene)
    return {
        "playing": _call_optional(
            canvas,
            "set_playback_state",
            scene.playing,
            bool(scene.title and scene.title != "Choose a song to begin"),
        ),
        "reactivity": _call_optional(
            canvas,
            "set_audio_reactivity_available",
            scene.audio not in {"silence"},
        ),
        "frame_rate": _call_optional(canvas, "set_frame_rate", "auto"),
    }


def _settle(app: object, milliseconds: int = 70) -> None:
    deadline = time.monotonic() + milliseconds / 1_000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()


def _image_metrics(image: object) -> dict[str, object]:
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        raise RuntimeError("Party Mode capture is empty.")
    columns = min(64, width)
    rows = min(36, height)
    luminance: list[float] = []
    colors: set[int] = set()
    for row in range(rows):
        y = min(height - 1, round((row + 0.5) * height / rows))
        for column in range(columns):
            x = min(width - 1, round((column + 0.5) * width / columns))
            color = image.pixelColor(x, y)
            colors.add(int(color.rgba()))
            luminance.append(
                (0.2126 * color.red() + 0.7152 * color.green() + 0.0722 * color.blue())
                / 255.0
            )
    bright = sum(value >= 0.92 for value in luminance) / len(luminance)
    if len(colors) < 4:
        raise RuntimeError("Party Mode capture appears blank or monochrome.")
    if bright > 0.65:
        raise RuntimeError("Party Mode capture exceeds the full-screen brightness cap.")
    return {
        "width": width,
        "height": height,
        "sampled_color_count": len(colors),
        "mean_luminance": round(sum(luminance) / len(luminance), 4),
        "bright_fraction": round(bright, 4),
    }


def _capture_canvas_matrix(
    api: Mapping[str, Any],
    app: object,
    canvas: object,
    captures: Path,
    features: Mapping[str, object],
    artwork: Mapping[str, tuple[object | None, Path | None, object]],
    scenes: Sequence[ReviewScene],
) -> tuple[list[dict[str, object]], dict[str, bool]]:
    results: list[dict[str, object]] = []
    optional_coverage = {
        "playing": False,
        "reactivity": False,
        "frame_rate": False,
    }
    canvas.start_rendering()
    for scene in scenes:
        coverage = _configure_canvas(canvas, scene, features, artwork)
        optional_coverage = {
            name: optional_coverage[name] or coverage[name] for name in optional_coverage
        }
        canvas.resize(scene.width, scene.height)
        canvas.show()
        canvas.update()
        _settle(app)
        pixmap = canvas.grab()
        if pixmap.isNull():
            raise RuntimeError(f"Party Mode capture failed for {scene.name}.")
        path = captures / f"{scene.name}.png"
        if not pixmap.save(str(path), "PNG"):
            raise RuntimeError(f"Party Mode capture could not be saved for {scene.name}.")
        image = pixmap.toImage()
        metrics = _image_metrics(image)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        results.append(
            {
                "scene": scene.name,
                "preset": scene.preset,
                "audio": scene.audio,
                "quality": scene.quality,
                "reduced_motion": scene.reduced_motion,
                "surface": "PartyCanvas",
                "capture_sha256": digest,
                **metrics,
            }
        )
    return results, optional_coverage


def _make_synthetic_host(
    api: Mapping[str, Any], wav_path: Path, artwork_path: Path
) -> object:
    """Build the minimum real host contract PartyModeWindow consumes."""

    QMainWindow = api["QMainWindow"]
    QMediaPlayer = api["QMediaPlayer"]
    QAudioOutput = api["QAudioOutput"]
    QSlider = api["QSlider"]
    Qt = api["Qt"]
    QUrl = api["QUrl"]

    class SyntheticDB:
        def __init__(self) -> None:
            self._track = {
                "path": str(wav_path),
                "title": "Synthetic Signal",
                "artist": "Music Vault Review",
                "album": "Synthetic Party Mode",
                "cover_path": str(artwork_path),
            }

        def get_track(self, track_id: object) -> dict[str, str] | None:
            return self._track if track_id == 1 else None

    class SyntheticHost(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.config = {
                "party_mode_preset": "pulse",
                "party_mode_quality": "auto",
                "party_mode_frame_rate": "auto",
                "party_mode_reduced_motion": False,
                "party_mode_show_artwork": True,
                "party_mode_auto_hide_overlay": True,
                "party_mode_overlay_timeout_seconds": 10,
            }
            self.db = SyntheticDB()
            self.current_track_id: int | None = 1
            self.autoplay_enabled = True
            self.shuffle_enabled = False
            self.repeat_mode = "off"
            self.manual_queue: list[int] = []
            self.volume_percent = 68
            self.save_count = 0
            self.previous_count = 0
            self.next_count = 0

            self.player = QMediaPlayer(self)
            self.audio_output = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_output)
            self.player.setSource(QUrl.fromLocalFile(str(wav_path)))
            self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
            self.volume_slider.setRange(0, 100)
            self.volume_slider.setValue(self.volume_percent)
            self.volume_slider.valueChanged.connect(self._set_volume)

        def _set_volume(self, value: int) -> None:
            self.volume_percent = max(0, min(100, int(value)))

        def save_config(self) -> None:
            self.save_count += 1

        def toggle_play(self) -> None:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()

        def play_previous(self) -> None:
            self.previous_count += 1

        def play_next(self) -> None:
            self.next_count += 1

        def toggle_autoplay(self) -> None:
            self.autoplay_enabled = not self.autoplay_enabled
            if self.autoplay_enabled:
                self.shuffle_enabled = False

        def toggle_shuffle(self) -> None:
            self.shuffle_enabled = not self.shuffle_enabled
            if self.shuffle_enabled:
                self.autoplay_enabled = False

        def cycle_repeat(self) -> None:
            modes = ("off", "all", "one")
            self.repeat_mode = modes[(modes.index(self.repeat_mode) + 1) % len(modes)]

    return SyntheticHost()


def _widget_is_topmost(window: object, widget: object) -> bool:
    root = window.centralWidget()
    point = widget.mapTo(root, widget.rect().center())
    hit = root.childAt(point)
    return hit is not None and (hit is widget or widget.isAncestorOf(hit))


def _exercise_party_window_matrix(
    api: Mapping[str, Any],
    app: object,
    host: object,
    window: object,
    captures: Path,
    features: Mapping[str, object],
    wav_path: Path,
    artwork_path: Path,
    scenes: Sequence[WindowReviewScene],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Capture real PartyModeWindow states and assert its host contract."""

    QMediaPlayer = api["QMediaPlayer"]
    host_players = host.findChildren(QMediaPlayer)
    if len(host_players) != 1 or host_players[0] is not host.player:
        raise RuntimeError("Synthetic host did not begin with exactly one media player.")
    source_before = host.player.source().toLocalFile()
    if Path(source_before).resolve() != wav_path.resolve():
        raise RuntimeError("Synthetic host player is not using the generated review WAV.")

    if window.findChildren(QMediaPlayer):
        raise RuntimeError("PartyModeWindow created a second media player.")
    if host.player is not host_players[0] or host.player.source().toLocalFile() != source_before:
        raise RuntimeError("PartyModeWindow replaced or changed the host media player.")

    results: list[dict[str, object]] = []
    assertions = {
        "single_host_media_player": True,
        "generated_wav_is_host_source": True,
        "overlay_content_verified": True,
        "overlay_z_order_verified": True,
        "hidden_overlay_verified": True,
        "queue_content_and_z_order_verified": True,
        "help_content_and_z_order_verified": True,
        "manual_queue_unchanged": True,
    }
    window.resize(scenes[0].width, scenes[0].height)
    window.show()
    window._connect_player_signals()
    window.canvas.start_rendering()
    window.state_timer.start()
    window.fallback_timer.start()
    _settle(app, 80)

    for scene in scenes:
        if window._help_visible:
            window.toggle_help()
        host.manual_queue = list(range(101, 101 + scene.queue_count))
        queue_before = tuple(host.manual_queue)
        host.config["party_mode_preset"] = scene.preset
        settings = dict(host.config)
        settings["party_mode_preset"] = scene.preset
        window.apply_settings(settings)
        window.resize(scene.width, scene.height)
        window.refresh_from_host(force=True)
        window.on_audio_features(features[scene.audio])
        window.show_overlay()
        _settle(app, 210)

        if scene.help_visible:
            window.toggle_help()
            _settle(app, 210)
        elif not scene.overlay_visible:
            window.hide_overlay()
            _settle(app, 230)

        expected_queue = f"Q: {scene.queue_count}"
        if window.title_label.text() != "Synthetic Signal":
            raise RuntimeError(f"Party overlay title mismatch in {scene.name}.")
        if window.artist_label.text() != "Music Vault Review":
            raise RuntimeError(f"Party overlay artist mismatch in {scene.name}.")
        if window.album_label.text() != "Synthetic Party Mode":
            raise RuntimeError(f"Party overlay album mismatch in {scene.name}.")
        if window.queue_label.text() != expected_queue:
            raise RuntimeError(f"Party queue count mismatch in {scene.name}.")
        if window.preset_button.text() != scene.preset.title():
            raise RuntimeError(f"Party preset label mismatch in {scene.name}.")
        if window.current_preset != scene.preset:
            raise RuntimeError(f"Party preset state mismatch in {scene.name}.")
        if tuple(host.manual_queue) != queue_before:
            raise RuntimeError(f"Party review mutated the host queue in {scene.name}.")

        if scene.help_visible:
            if not window._help_visible or not window.help_panel.isVisible():
                raise RuntimeError(f"Shortcut help is not visible in {scene.name}.")
            if window.root_stack.currentWidget() is not window.help_panel:
                raise RuntimeError(f"Shortcut help is not topmost in {scene.name}.")
            help_text = "\n".join(
                label.text()
                for label in window.help_panel.findChildren(api["QLabel"])
            )
            if "Party Mode shortcuts" not in help_text or "F11" not in help_text:
                raise RuntimeError(f"Shortcut help content is incomplete in {scene.name}.")
            if not _widget_is_topmost(window, window.help_panel):
                raise RuntimeError(f"Shortcut help failed its z-order check in {scene.name}.")
        elif scene.overlay_visible:
            if not window.overlay_visible or window.overlay_effect.opacity() < 0.95:
                raise RuntimeError(f"Party overlay is not visible in {scene.name}.")
            if window.root_stack.currentWidget() is not window.overlay:
                raise RuntimeError(f"Party overlay is not topmost in {scene.name}.")
            if not _widget_is_topmost(window, window.exit_button):
                raise RuntimeError(f"Party overlay failed its z-order check in {scene.name}.")
            if scene.queue_count and not _widget_is_topmost(window, window.queue_label):
                raise RuntimeError(f"Party queue failed its z-order check in {scene.name}.")
        else:
            if window.overlay_visible or window.overlay_effect.opacity() > 0.05:
                raise RuntimeError(f"Party overlay did not hide in {scene.name}.")

        pixmap = window.grab()
        if pixmap.isNull():
            raise RuntimeError(f"PartyModeWindow capture failed for {scene.name}.")
        path = captures / f"{scene.name}.png"
        if not pixmap.save(str(path), "PNG"):
            raise RuntimeError(f"PartyModeWindow capture could not be saved for {scene.name}.")
        metrics = _image_metrics(pixmap.toImage())
        results.append(
            {
                "scene": scene.name,
                "preset": scene.preset,
                "audio": scene.audio,
                "overlay_visible": scene.overlay_visible,
                "help_visible": scene.help_visible,
                "queue_count": scene.queue_count,
                "surface": "PartyModeWindow",
                "capture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                **metrics,
            }
        )

    if len(host.findChildren(QMediaPlayer)) != 1 or window.findChildren(QMediaPlayer):
        raise RuntimeError("PartyModeWindow media-player ownership changed during review.")
    if host.player is not host_players[0] or host.player.source().toLocalFile() != source_before:
        raise RuntimeError("PartyModeWindow replaced or changed the host player during review.")

    return results, assertions


def _release_party_window(
    api: Mapping[str, Any], app: object, host: object, window: object
) -> int:
    """Release the synthetic media source even when a capture assertion fails."""

    window.close()
    _settle(app, 40)
    active = [timer for timer in window.findChildren(api["QTimer"]) if timer.isActive()]
    host.player.stop()
    host.player.setSource(api["QUrl"]())
    window.deleteLater()
    host.close()
    host.deleteLater()
    _settle(app, 80)
    app.processEvents(api["QEventLoop"].ProcessEventsFlag.AllEvents, 100)
    return len(active)


def _capture_party_window_matrix(
    api: Mapping[str, Any],
    app: object,
    captures: Path,
    features: Mapping[str, object],
    wav_path: Path,
    artwork_path: Path,
    scenes: Sequence[WindowReviewScene],
) -> tuple[list[dict[str, object]], dict[str, object], int]:
    host = _make_synthetic_host(api, wav_path, artwork_path)
    window = api["PartyModeWindow"](host)
    try:
        results, assertions = _exercise_party_window_matrix(
            api,
            app,
            host,
            window,
            captures,
            features,
            wav_path,
            artwork_path,
            scenes,
        )
    finally:
        active_timers = _release_party_window(api, app, host, window)
    if active_timers:
        raise RuntimeError("PartyModeWindow timer remained active after close.")
    return results, assertions, active_timers


def _json_safe(value: object) -> object:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _percentile_95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _benchmark(
    api: Mapping[str, Any],
    app: object,
    features: Mapping[str, object],
    artwork: Mapping[str, tuple[object | None, Path | None, object]],
    frames: int,
) -> tuple[list[dict[str, object]], int]:
    results: list[dict[str, object]] = []
    active_timers_after_stop = 0
    for name, width, height, quality in (
        ("1080p_high", 1920, 1080, "high"),
        ("4k_auto", 3840, 2160, "auto"),
    ):
        scene = ReviewScene(
            name,
            "starfield" if name.startswith("4k") else "pulse",
            "beat",
            width,
            height,
            quality=quality,
        )
        canvas = api["PartyCanvas"](seed=9001, quality="medium")
        _configure_canvas(canvas, scene, features, artwork)
        canvas.resize(width, height)
        canvas.show()
        canvas.start_rendering()
        _settle(app)
        effective_quality = "medium" if quality == "auto" else quality
        engine = api["PartyVisualEngine"](
            seed=9001,
            preset=scene.preset,
            quality=effective_quality,
        )
        engine.set_features(features[scene.audio])
        simulation_times: list[float] = []
        simulated_frame = None
        for _index in range(frames):
            started = time.perf_counter_ns()
            simulated_frame = engine.update(1.0 / 60.0, features[scene.audio])
            simulation_times.append((time.perf_counter_ns() - started) / 1_000_000)
        frame_times: list[float] = []
        for _index in range(frames):
            started = time.perf_counter_ns()
            canvas.repaint()
            app.processEvents()
            frame_times.append((time.perf_counter_ns() - started) / 1_000_000)
        reported = _json_safe(canvas.performance_metrics())
        if isinstance(reported, Mapping):
            particle_count = reported.get("particle_count", reported.get("particle_budget"))
            if isinstance(particle_count, (int, float)) and particle_count > 1_000:
                raise RuntimeError("Party Mode particle count exceeded its bounded budget.")
        if simulated_frame is None or len(simulated_frame.particles) > 1_000:
            raise RuntimeError("Party Mode simulation did not produce a bounded frame.")
        stopped = _stop_and_check(api, app, canvas)
        active_timers_after_stop += stopped
        results.append(
            {
                "name": name,
                "width": width,
                "height": height,
                "requested_quality": quality,
                "frames": frames,
                "mean_simulation_ms": round(
                    sum(simulation_times) / len(simulation_times), 3
                ),
                "p95_simulation_ms": round(_percentile_95(simulation_times), 3),
                "mean_synchronous_paint_ms": round(sum(frame_times) / len(frame_times), 3),
                "p95_synchronous_paint_ms": round(_percentile_95(frame_times), 3),
                "simulated_particle_count": len(simulated_frame.particles),
                "renderer": reported,
                "active_timers_after_stop": stopped,
            }
        )
    return results, active_timers_after_stop


def _stop_and_check(api: Mapping[str, Any], app: object, canvas: object) -> int:
    canvas.stop_rendering()
    canvas.hide()
    _settle(app, 30)
    active = [timer for timer in canvas.findChildren(api["QTimer"]) if timer.isActive()]
    if active:
        raise RuntimeError("Party Mode timer remained active after stop/hide.")
    canvas.close()
    canvas.deleteLater()
    app.processEvents(api["QEventLoop"].ProcessEventsFlag.AllEvents, 50)
    return len(active)


def _adaptive_quality_probe(api: Mapping[str, Any]) -> dict[str, object]:
    controller = api["AdaptiveQualityController"](
        "high",
        evaluation_window=3,
        bad_windows_required=2,
        good_windows_required=2,
    )
    transitions: list[str] = []
    for _index in range(6):
        changed = controller.record_frame(0.060)
        if changed is not None:
            transitions.append(changed)
    downgraded = controller.quality
    for _index in range(6):
        changed = controller.record_frame(0.005)
        if changed is not None:
            transitions.append(changed)
    restored = controller.quality
    if downgraded != "medium" or restored != "high":
        raise RuntimeError("Party Mode adaptive-quality hysteresis probe failed.")
    return {
        "initial_quality": "high",
        "overload_quality": downgraded,
        "headroom_quality": restored,
        "transitions": transitions,
        "hysteresis_verified": True,
    }


def _print_summary(payload: Mapping[str, object]) -> None:
    print("Music Vault Party Mode synthetic review")
    print("Synthetic temporary data only; Python networking blocked; Qt offscreen.")
    print(
        f"Capture profile: {payload['capture_profile']} | states: "
        f"{payload['capture_count']} "
        f"({payload['canvas_capture_count']} canvas, "
        f"{payload['party_window_capture_count']} PartyModeWindow)"
    )
    for result in payload["benchmarks"]:  # type: ignore[index]
        print(
            f"  {result['name']}: simulation mean/p95 "
            f"{result['mean_simulation_ms']:.3f}/{result['p95_simulation_ms']:.3f} ms | "
            f"paint mean/p95 {result['mean_synchronous_paint_ms']:.3f}/"
            f"{result['p95_synchronous_paint_ms']:.3f} ms | "
            f"{result['frames']} frames"
        )
    probe = payload["adaptive_quality_probe"]  # type: ignore[index]
    print(
        "Adaptive quality: "
        f"{probe['initial_quality']} -> {probe['overload_quality']} -> "
        f"{probe['headroom_quality']} (hysteresis verified)"
    )
    print("Temporary synthetic runtime removed: yes")


def run_review(args: argparse.Namespace) -> dict[str, object]:
    frames = int(args.frames)
    if not 12 <= frames <= 600:
        raise ValueError("--frames must be from 12 through 600.")
    output = _prepare_output(args.output)

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if os.name == "nt" and not os.environ.get("QT_QPA_FONTDIR"):
        windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if windows_fonts.is_dir():
            os.environ["QT_QPA_FONTDIR"] = str(windows_fonts)
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    os.environ["MUSIC_VAULT_DISABLE_NETWORK"] = "1"
    os.environ["MUSIC_VAULT_PARTY_REVIEW_SEED"] = "9001"
    _install_network_guard()

    payload: dict[str, object]
    with tempfile.TemporaryDirectory(prefix="MusicVault_Party_Review_") as temporary:
        runtime = Path(temporary)
        os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(runtime)
        (runtime / "data").mkdir()
        (runtime / "music-vault.portable.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "product": "Music Vault",
                    "portable": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        captures = runtime / "captures"
        captures.mkdir()
        shutil.copytree(
            PROJECT_ROOT / "assets" / "icons" / "ui",
            runtime / "assets" / "icons" / "ui",
        )
        pcm = _synthetic_pcm()
        wav_path = runtime / "synthetic-party-mode.wav"
        _write_synthetic_wav(wav_path, pcm)

        api = _load_party_api()
        QApplication = api["QApplication"]
        app = QApplication.instance() or QApplication([])
        app.setApplicationName("Music Vault Party Mode Synthetic Review")
        primary_path = runtime / "synthetic-artwork-primary.png"
        secondary_path = runtime / "synthetic-artwork-secondary.png"
        primary = _generate_artwork(api, primary_path, 0)
        secondary = _generate_artwork(api, secondary_path, 1)
        extractor = api["PaletteExtractor"]()
        artwork = {
            "primary": (primary, primary_path, extractor.extract(primary_path)),
            "secondary": (secondary, secondary_path, extractor.extract(secondary_path)),
            "missing": (None, None, api["DEFAULT_PARTY_PALETTE"]),
        }
        (runtime / "synthetic-track-metadata.json").write_text(
            json.dumps(
                {
                    "synthetic_only": True,
                    "title": "Synthetic Signal",
                    "artist": "Music Vault Review",
                    "album": "Synthetic Party Mode",
                    "queue_count": 3,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        features = _analyze_scenes(api, pcm)
        if args.capture_profile == "full":
            canvas = api["PartyCanvas"](seed=9001, quality="medium")
            canvas_captures, optional_coverage = _capture_canvas_matrix(
                api,
                app,
                canvas,
                captures,
                features,
                artwork,
                CANVAS_SCENES,
            )
            capture_timers_after_stop = _stop_and_check(api, app, canvas)
            window_scenes = WINDOW_SCENES
        else:
            canvas_captures = []
            optional_coverage = {
                "playing": False,
                "reactivity": False,
                "frame_rate": False,
            }
            capture_timers_after_stop = 0
            window_scenes = SCALE_SMOKE_SCENES

        window_captures, window_assertions, window_timers_after_stop = (
            _capture_party_window_matrix(
                api,
                app,
                captures,
                features,
                wav_path,
                primary_path,
                window_scenes,
            )
        )
        captures_result = [*canvas_captures, *window_captures]
        expected_captures = 14 if args.capture_profile == "full" else 2
        if len(captures_result) != expected_captures:
            raise RuntimeError(
                f"{args.capture_profile} capture profile produced "
                f"{len(captures_result)} states instead of {expected_captures}."
            )
        benchmarks, benchmark_timers_after_stop = _benchmark(
            api, app, features, artwork, frames
        )
        adaptive_probe = _adaptive_quality_probe(api)
        active_timers_after_stop = (
            capture_timers_after_stop
            + window_timers_after_stop
            + benchmark_timers_after_stop
        )

        if output is not None:
            for capture in captures.glob("*.png"):
                shutil.copy2(capture, output / capture.name)

        payload = {
            "schema_version": 1,
            "review": "Music Vault Party Mode",
            "capture_profile": args.capture_profile,
            "synthetic_only": True,
            "network_used": False,
            "personal_data_used": False,
            "qt_platform": os.environ["QT_QPA_PLATFORM"],
            "qt_scale_factor": float(args.scale),
            "capture_count": len(captures_result),
            "canvas_capture_count": len(canvas_captures),
            "party_window_capture_count": len(window_captures),
            "presets": list(PRESETS),
            "audio_scenarios": sorted(features),
            "optional_canvas_controls_exercised": optional_coverage,
            "party_window_assertions": window_assertions,
            "captures": captures_result,
            "benchmarks": benchmarks,
            "adaptive_quality_probe": adaptive_probe,
            "active_timers_after_stop": active_timers_after_stop,
            "captures_retained": output is not None,
            "runtime_removed": True,
        }
        if output is not None:
            (output / "party-mode-review.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    # Validate that the complete sanitized result remains portable JSON even
    # when the caller does not retain an output directory.
    json.dumps(payload, sort_keys=True)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run_review(args)
        _print_summary(payload)
        if args.output is not None:
            print("Sanitized captures and metrics written to the requested review directory.")
        return 0
    except Exception as exc:
        print(
            f"Party Mode synthetic review failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
