"""Bounded, deterministic Party Mode visuals.

``PartyVisualEngine`` contains no Qt state and can be exercised as a pure
seeded simulation.  ``PartyCanvas`` is the deliberately thin QWidget adapter;
it owns a render timer but never owns or controls media playback.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
import math
import random
from typing import Any, Final

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from music_vault.ui.party_palette import ArtworkPalette, DEFAULT_PARTY_PALETTE, RGB


PRESETS: Final[tuple[str, str, str]] = ("pulse", "starfield", "aurora")
QUALITY_LEVELS: Final[tuple[str, str, str]] = ("low", "medium", "high")
MAX_DELTA_SECONDS: Final[float] = 0.1
MAX_PARTICLES: Final[int] = 420
MAX_BEAT: Final[float] = 0.88
MAX_BRIGHTNESS: Final[float] = 0.84


@dataclass(frozen=True, slots=True)
class QualityBudget:
    pulse_particles: int
    starfield_particles: int
    aurora_particles: int
    aurora_bands: int
    target_fps: int

    def particle_count(self, preset: str) -> int:
        if preset == "pulse":
            return self.pulse_particles
        if preset == "starfield":
            return self.starfield_particles
        if preset == "aurora":
            return self.aurora_particles
        raise ValueError(f"Unknown Party Mode preset: {preset!r}")


QUALITY_BUDGETS: Final[dict[str, QualityBudget]] = {
    "low": QualityBudget(88, 110, 80, 3, 30),
    "medium": QualityBudget(190, 240, 180, 5, 60),
    "high": QualityBudget(360, MAX_PARTICLES, 340, 7, 60),
}


@dataclass(frozen=True, slots=True)
class ParticleState:
    """Normalized, renderer-independent particle state."""

    x: float
    y: float
    size: float
    opacity: float
    depth: float
    color_index: int


@dataclass(frozen=True, slots=True)
class VisualFrame:
    """An immutable output snapshot from ``PartyVisualEngine``."""

    preset: str
    previous_preset: str | None
    transition_progress: float
    delta_seconds: float
    elapsed_seconds: float
    energy: float
    bass: float
    low_mid: float
    mid: float
    high: float
    beat: float
    brightness: float
    pulse: float
    particles: tuple[ParticleState, ...]
    aurora_offsets: tuple[float, ...]
    quality: str
    reduced_motion: bool


@dataclass(slots=True)
class _Particle:
    x: float
    y: float
    size: float
    opacity: float
    depth: float
    color_index: int
    speed: float
    drift: float
    phase: float


def _finite_unit(value: object, default: float = 0.0, cap: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return max(0.0, min(cap, default))
    if not math.isfinite(number):
        return max(0.0, min(cap, default))
    return max(0.0, min(cap, number))


def _feature_value(snapshot: object | None, names: tuple[str, ...], default: float) -> float:
    if snapshot is None:
        return default
    for name in names:
        try:
            if isinstance(snapshot, Mapping) and name in snapshot:
                return _finite_unit(snapshot[name], default)
            if hasattr(snapshot, name):
                return _finite_unit(getattr(snapshot, name), default)
        except Exception:
            continue
    return default


class PartyVisualEngine:
    """Pure seeded visual simulation with strictly bounded work."""

    def __init__(
        self,
        *,
        seed: int = 0,
        preset: str = "pulse",
        quality: str = "high",
        reduced_motion: bool = False,
        transition_seconds: float = 0.55,
    ) -> None:
        self._validate_preset(preset)
        self._validate_quality(quality)
        self.seed = int(seed)
        self.preset = preset
        self.quality = quality
        self.reduced_motion = bool(reduced_motion)
        self.transition_seconds = max(0.05, min(2.0, float(transition_seconds)))
        self._features: object | None = None
        self._elapsed = 0.0
        self._previous_preset: str | None = None
        self._transition_elapsed = self.transition_seconds
        self._particles = self._make_particles(self.seed)
        self._last_frame: VisualFrame | None = None

    @staticmethod
    def _validate_preset(preset: str) -> None:
        if preset not in PRESETS:
            raise ValueError(f"Unknown Party Mode preset: {preset!r}")

    @staticmethod
    def _validate_quality(quality: str) -> None:
        if quality not in QUALITY_LEVELS:
            raise ValueError(f"Unknown Party Mode quality: {quality!r}")

    @staticmethod
    def _make_particles(seed: int) -> list[_Particle]:
        generator = random.Random(seed)
        return [
            _Particle(
                x=generator.random(),
                y=generator.random(),
                size=0.55 + (generator.random() * 1.75),
                opacity=0.32 + (generator.random() * 0.64),
                depth=0.18 + (generator.random() * 0.82),
                color_index=generator.randrange(3),
                speed=0.018 + (generator.random() * 0.085),
                drift=(generator.random() - 0.5) * 0.024,
                phase=generator.random() * math.tau,
            )
            for _ in range(MAX_PARTICLES)
        ]

    @property
    def last_frame(self) -> VisualFrame | None:
        return self._last_frame

    @property
    def target_fps(self) -> int:
        fps = QUALITY_BUDGETS[self.quality].target_fps
        return min(fps, 30) if self.reduced_motion else fps

    def particle_budget(self, preset: str | None = None) -> int:
        selected = preset or self.preset
        self._validate_preset(selected)
        count = QUALITY_BUDGETS[self.quality].particle_count(selected)
        if self.reduced_motion:
            count = max(3, math.ceil(count * 0.45))
        return min(MAX_PARTICLES, count)

    def set_features(self, snapshot: object | None) -> None:
        self._features = snapshot

    def set_preset(self, preset: str) -> None:
        self._validate_preset(preset)
        if preset == self.preset:
            return
        if self.reduced_motion:
            self._previous_preset = None
            self._transition_elapsed = self.transition_seconds
        else:
            self._previous_preset = self.preset
            self._transition_elapsed = 0.0
        self.preset = preset

    def set_quality(self, quality: str) -> None:
        self._validate_quality(quality)
        self.quality = quality

    def set_reduced_motion(self, reduced: bool) -> None:
        self.reduced_motion = bool(reduced)
        if self.reduced_motion:
            self._previous_preset = None
            self._transition_elapsed = self.transition_seconds

    def reset(self) -> None:
        """Return this engine to its deterministic initial simulation state."""

        self._elapsed = 0.0
        self._previous_preset = None
        self._transition_elapsed = self.transition_seconds
        self._particles = self._make_particles(self.seed)
        self._last_frame = None

    def update(
        self, delta_seconds: float, features: object | None = None
    ) -> VisualFrame:
        if features is not None:
            self._features = features
        delta = _finite_unit(delta_seconds, 0.0, MAX_DELTA_SECONDS)
        self._elapsed += delta

        energy = _feature_value(
            self._features, ("energy", "rms", "level", "amplitude", "peak"), 0.18
        )
        bass = _feature_value(self._features, ("bass", "low", "low_energy"), energy * 0.8)
        low_mid = _feature_value(
            self._features, ("low_mid", "lowmid", "low_mid_energy"), energy * 0.72
        )
        mid = _feature_value(self._features, ("mid", "mid_energy"), energy * 0.64)
        high = _feature_value(
            self._features, ("high", "treble", "high_energy"), energy * 0.52
        )
        beat = min(
            MAX_BEAT,
            max(
                _feature_value(self._features, ("beat_strength",), 0.0),
                _feature_value(self._features, ("beat", "on_beat"), 0.0),
            ),
        )
        brightness = min(
            MAX_BRIGHTNESS,
            _feature_value(
                self._features,
                ("brightness", "high", "treble", "high_energy"),
                0.24 + (energy * 0.32),
            ),
        )

        motion_scale = 0.14 if self.reduced_motion else 1.0
        starfield_drive = 1.0
        if self.preset == "starfield":
            starfield_drive += (energy * 1.45) + (beat * 0.72)
        for particle in self._particles:
            particle.y = (
                particle.y
                + (delta * particle.speed * motion_scale * starfield_drive)
            ) % 1.0
            particle.x = (
                particle.x
                + (delta * particle.drift * motion_scale)
                + (math.sin(self._elapsed + particle.phase) * delta * 0.002 * motion_scale)
            ) % 1.0

        if self._previous_preset is not None:
            self._transition_elapsed += delta
            transition = min(1.0, self._transition_elapsed / self.transition_seconds)
            if transition >= 1.0:
                self._previous_preset = None
        else:
            transition = 1.0

        tempo_wave = 0.5 + (0.5 * math.sin((self._elapsed * 1.35) + (bass * 1.7)))
        if self.reduced_motion:
            pulse = min(0.58, 0.16 + (energy * 0.24) + (beat * 0.12))
        else:
            pulse = min(1.0, 0.18 + (tempo_wave * 0.28) + (energy * 0.30) + (beat * 0.34))

        count = self.particle_budget()
        particles = tuple(
            ParticleState(
                x=particle.x,
                y=particle.y,
                size=particle.size,
                opacity=min(1.0, particle.opacity * (0.58 + (brightness * 0.5))),
                depth=particle.depth,
                color_index=particle.color_index,
            )
            for particle in self._particles[:count]
        )
        band_count = QUALITY_BUDGETS[self.quality].aurora_bands
        if self.reduced_motion:
            band_count = max(2, math.ceil(band_count * 0.5))
        aurora_levels = (bass, low_mid, mid, high)
        aurora_offsets = tuple(
            math.sin(
                (
                    self._elapsed
                    * (0.18 + (index * 0.025) + (aurora_levels[index % 4] * 0.12))
                    * motion_scale
                )
                + index
                + (aurora_levels[index % 4] * 0.82)
            )
            for index in range(band_count)
        )

        frame = VisualFrame(
            preset=self.preset,
            previous_preset=self._previous_preset,
            transition_progress=transition,
            delta_seconds=delta,
            elapsed_seconds=self._elapsed,
            energy=energy,
            bass=bass,
            low_mid=low_mid,
            mid=mid,
            high=high,
            beat=beat,
            brightness=brightness,
            pulse=pulse,
            particles=particles,
            aurora_offsets=aurora_offsets,
            quality=self.quality,
            reduced_motion=self.reduced_motion,
        )
        self._last_frame = frame
        return frame


class AdaptiveQualityController:
    """Small deterministic frame-time controller with hysteresis."""

    def __init__(
        self,
        quality: str = "high",
        *,
        evaluation_window: int = 45,
        metrics_window: int = 180,
        downgrade_frame_ms: float = 23.0,
        upgrade_frame_ms: float = 14.5,
        bad_windows_required: int = 2,
        good_windows_required: int = 4,
    ) -> None:
        PartyVisualEngine._validate_quality(quality)
        self.quality = quality
        self.evaluation_window = max(3, min(240, int(evaluation_window)))
        self.downgrade_frame_ms = max(8.0, float(downgrade_frame_ms))
        self.upgrade_frame_ms = max(4.0, min(self.downgrade_frame_ms, float(upgrade_frame_ms)))
        self.bad_windows_required = max(1, int(bad_windows_required))
        self.good_windows_required = max(1, int(good_windows_required))
        self._samples: deque[float] = deque(maxlen=max(self.evaluation_window, metrics_window))
        self._evaluation: list[float] = []
        self._bad_windows = 0
        self._good_windows = 0
        self._changes = 0

    def set_quality(self, quality: str) -> None:
        PartyVisualEngine._validate_quality(quality)
        self.quality = quality
        self._bad_windows = 0
        self._good_windows = 0

    def _effective_thresholds(self) -> tuple[float, float]:
        """Account for the intentionally lower cadence of reduced budgets."""

        target_frame_ms = 1000.0 / QUALITY_BUDGETS[self.quality].target_fps
        downgrade = max(self.downgrade_frame_ms, target_frame_ms * 1.30)
        # Promotion always requires genuine 60 FPS headroom; being comfortable
        # at the low tier's intentional 30 FPS cadence is not enough by itself.
        return downgrade, min(downgrade, self.upgrade_frame_ms)

    def record_frame(self, frame_seconds: float) -> str | None:
        """Record a duration and return a new quality only when it changes."""

        try:
            frame_ms = float(frame_seconds) * 1000.0
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(frame_ms) or frame_ms <= 0.0:
            return None
        frame_ms = min(1000.0, frame_ms)
        self._samples.append(frame_ms)
        self._evaluation.append(frame_ms)
        if len(self._evaluation) < self.evaluation_window:
            return None

        window = self._evaluation
        self._evaluation = []
        ordered = sorted(window)
        average = sum(window) / len(window)
        p95 = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]
        downgrade_threshold, upgrade_threshold = self._effective_thresholds()
        overloaded = average > downgrade_threshold or p95 > (downgrade_threshold * 1.35)
        comfortable = average < upgrade_threshold and p95 < (upgrade_threshold * 1.2)

        if overloaded:
            self._bad_windows += 1
            self._good_windows = 0
        elif comfortable:
            self._good_windows += 1
            self._bad_windows = 0
        else:
            self._bad_windows = 0
            self._good_windows = 0

        index = QUALITY_LEVELS.index(self.quality)
        if self._bad_windows >= self.bad_windows_required:
            self._bad_windows = 0
            if index > 0:
                self.quality = QUALITY_LEVELS[index - 1]
                self._changes += 1
                return self.quality
        if self._good_windows >= self.good_windows_required:
            self._good_windows = 0
            if index < len(QUALITY_LEVELS) - 1:
                self.quality = QUALITY_LEVELS[index + 1]
                self._changes += 1
                return self.quality
        return None

    def performance_metrics(self) -> dict[str, float | int | str]:
        downgrade_threshold, upgrade_threshold = self._effective_thresholds()
        if not self._samples:
            return {
                "quality": self.quality,
                "samples": 0,
                "average_frame_ms": 0.0,
                "p95_frame_ms": 0.0,
                "estimated_fps": 0.0,
                "slow_frame_ratio": 0.0,
                "quality_changes": self._changes,
                "downgrade_threshold_ms": round(downgrade_threshold, 3),
                "upgrade_threshold_ms": round(upgrade_threshold, 3),
            }
        samples = list(self._samples)
        ordered = sorted(samples)
        average = sum(samples) / len(samples)
        p95 = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]
        slow_ratio = sum(value > downgrade_threshold for value in samples) / len(samples)
        return {
            "quality": self.quality,
            "samples": len(samples),
            "average_frame_ms": round(average, 3),
            "p95_frame_ms": round(p95, 3),
            "estimated_fps": round(1000.0 / average, 2),
            "slow_frame_ratio": round(slow_ratio, 4),
            "quality_changes": self._changes,
            "downgrade_threshold_ms": round(downgrade_threshold, 3),
            "upgrade_threshold_ms": round(upgrade_threshold, 3),
        }


def _qcolor(color: RGB, alpha: int = 255) -> QColor:
    return QColor(color[0], color[1], color[2], max(0, min(255, alpha)))


class PartyCanvas(QWidget):
    """Qt renderer for ``PartyVisualEngine`` with no playback ownership."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        seed: int = 0,
        preset: str = "pulse",
        quality: str = "high",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PartyCanvas")
        self.setAccessibleName("Party Mode visualizer")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        if quality not in ("auto",) + QUALITY_LEVELS:
            raise ValueError(f"Unknown Party Mode quality: {quality!r}")
        effective_quality = "medium" if quality == "auto" else quality
        self._engine = PartyVisualEngine(
            seed=seed, preset=preset, quality=effective_quality
        )
        self._adaptive = AdaptiveQualityController(effective_quality)
        self._quality_mode = quality
        self._frame_rate_mode = "auto"
        self._show_artwork = True
        self._audio_reactivity_available = False
        self._palette = DEFAULT_PARTY_PALETTE
        self._features: object | None = None
        self._frame: VisualFrame | None = self._engine.update(0.0)
        self._artwork: QPixmap | None = None
        self._artwork_cache: QPixmap | None = None
        self._title = ""
        self._artist = ""
        self._album = ""
        self._is_playing = False
        self._has_track = False
        self._clock = QElapsedTimer()
        self._last_simulation_seconds = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._render_tick)
        self._apply_timer_interval()

    @property
    def rendering_active(self) -> bool:
        return self._timer.isActive()

    @property
    def preset(self) -> str:
        return self._engine.preset

    @property
    def quality(self) -> str:
        return self._quality_mode

    def set_features(self, snapshot: object | None) -> None:
        self._features = snapshot
        self._engine.set_features(snapshot)
        self._refresh_frame()

    def set_preset(self, name: str) -> None:
        self._engine.set_preset(name)
        self._refresh_frame()

    def set_palette(self, palette: ArtworkPalette) -> None:
        if not isinstance(palette, ArtworkPalette):
            raise TypeError("palette must be an ArtworkPalette")
        self._palette = palette
        self.update()

    def set_reduced_motion(self, reduced: bool) -> None:
        self._engine.set_reduced_motion(reduced)
        self._apply_timer_interval()
        self._refresh_frame()

    def set_quality(self, quality: str) -> None:
        if quality not in ("auto",) + QUALITY_LEVELS:
            raise ValueError(f"Unknown Party Mode quality: {quality!r}")
        self._quality_mode = quality
        effective = "medium" if quality == "auto" else quality
        self._engine.set_quality(effective)
        self._adaptive.set_quality(effective)
        self._apply_timer_interval()
        self._refresh_frame()

    def set_frame_rate(self, frame_rate: str | int) -> None:
        normalized = str(frame_rate).strip().lower()
        if normalized not in {"auto", "30", "60"}:
            normalized = "auto"
        self._frame_rate_mode = normalized
        self._apply_timer_interval()

    def set_show_artwork(self, show: bool) -> None:
        self._show_artwork = bool(show)
        self.update()

    def set_audio_reactivity_available(self, available: bool) -> None:
        self._audio_reactivity_available = bool(available)
        if not self._audio_reactivity_available:
            self._features = None
            self._engine.set_features(None)
            self._refresh_frame()

    def set_artwork(self, artwork: QPixmap | None) -> None:
        if artwork is not None and not isinstance(artwork, QPixmap):
            raise TypeError("artwork must be a QPixmap or None")
        self._artwork = None
        if artwork is not None and not artwork.isNull():
            bounded = QPixmap(artwork)
            if max(bounded.width(), bounded.height()) > 1_024:
                bounded = bounded.scaled(
                    1_024,
                    1_024,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._artwork = bounded
        self._rebuild_artwork_cache()
        self.update()

    def _rebuild_artwork_cache(self) -> None:
        self._artwork_cache = None
        if self._artwork is None or self._artwork.isNull():
            return
        size = max(96, min(1_024, math.ceil(min(self.width(), self.height()) * 0.34)))
        scaled = self._artwork.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - size) // 2)
        y = max(0, (scaled.height() - size) // 2)
        self._artwork_cache = scaled.copy(x, y, size, size)

    def set_track_text(self, title: str, artist: str, album: str = "") -> None:
        self._title = str(title or "")[:240]
        self._artist = str(artist or "")[:240]
        self._album = str(album or "")[:240]
        self.update()

    def set_playback_state(self, is_playing: bool, has_track: bool) -> None:
        self._is_playing = bool(is_playing)
        self._has_track = bool(has_track)
        self._refresh_frame()

    def _effective_features(self) -> object:
        """Return decoded features or a bounded playback-state ambient profile."""

        if self._audio_reactivity_available and self._features is not None:
            return self._features
        if not self._has_track:
            return {"energy": 0.025, "bass": 0.018, "mid": 0.02, "high": 0.015}
        if self._is_playing:
            return {"energy": 0.16, "bass": 0.12, "mid": 0.10, "high": 0.065}
        return {"energy": 0.055, "bass": 0.04, "mid": 0.035, "high": 0.02}

    def start_rendering(self) -> None:
        if self._timer.isActive():
            return
        self._clock.start()
        self._timer.start()

    def stop_rendering(self) -> None:
        self._timer.stop()
        self._clock.invalidate()

    def performance_metrics(self) -> dict[str, float | int | str | bool]:
        metrics: dict[str, float | int | str | bool] = self._adaptive.performance_metrics()
        metrics.update(
            {
                "rendering_active": self.rendering_active,
                "preset": self._engine.preset,
                "quality": self._engine.quality,
                "quality_mode": self._quality_mode,
                "particle_budget": self._engine.particle_budget(),
                "target_fps": self._target_fps(),
                "reduced_motion": self._engine.reduced_motion,
                "audio_reactivity_available": self._audio_reactivity_available,
            }
        )
        return metrics

    def _target_fps(self) -> int:
        if self._engine.reduced_motion:
            return 30
        if self._frame_rate_mode in {"30", "60"}:
            return int(self._frame_rate_mode)
        return self._engine.target_fps

    def _apply_timer_interval(self) -> None:
        self._timer.setInterval(max(1, round(1000 / self._target_fps())))

    def _refresh_frame(self) -> None:
        self._frame = self._engine.update(0.0, self._effective_features())
        self.update()

    def _render_tick(self) -> None:
        work_clock = QElapsedTimer()
        work_clock.start()
        if self._clock.isValid():
            elapsed = self._clock.nsecsElapsed() / 1_000_000_000.0
            self._clock.restart()
        else:
            elapsed = 1.0 / self._engine.target_fps
            self._clock.start()
        self._frame = self._engine.update(elapsed, self._effective_features())
        self._last_simulation_seconds = work_clock.nsecsElapsed() / 1_000_000_000.0
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        del event
        paint_clock = QElapsedTimer()
        paint_clock.start()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._paint_background(painter)
        frame = self._frame or self._engine.update(0.0, self._features)
        if frame.previous_preset and frame.transition_progress < 1.0:
            self._paint_preset(
                painter, frame.previous_preset, frame, 1.0 - frame.transition_progress
            )
            self._paint_preset(painter, frame.preset, frame, frame.transition_progress)
        else:
            self._paint_preset(painter, frame.preset, frame, 1.0)
        self._paint_center(painter, frame)
        painter.end()
        if (
            self._quality_mode == "auto"
            and not self._engine.reduced_motion
        ):
            paint_seconds = paint_clock.nsecsElapsed() / 1_000_000_000.0
            changed_quality = self._adaptive.record_frame(
                self._last_simulation_seconds + paint_seconds
            )
            if changed_quality is not None:
                self._engine.set_quality(changed_quality)
                self._apply_timer_interval()

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        self._rebuild_artwork_cache()
        super().resizeEvent(event)

    def _paint_background(self, painter: QPainter) -> None:
        gradient = QLinearGradient(0.0, 0.0, float(self.width()), float(self.height()))
        gradient.setColorAt(0.0, _qcolor(self._palette.background))
        gradient.setColorAt(0.55, _qcolor(self._palette.surface))
        gradient.setColorAt(1.0, _qcolor(self._palette.background))
        painter.fillRect(self.rect(), gradient)

    def _paint_preset(
        self, painter: QPainter, preset: str, frame: VisualFrame, opacity: float
    ) -> None:
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        if preset == "pulse":
            self._paint_pulse(painter, frame)
        elif preset == "starfield":
            self._paint_starfield(painter, frame)
        else:
            self._paint_aurora(painter, frame)
        painter.restore()

    def _paint_pulse(self, painter: QPainter, frame: VisualFrame) -> None:
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        span = max(1.0, min(self.width(), self.height()))
        radius = span * (0.19 + (frame.pulse * 0.18))
        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0.0, _qcolor(self._palette.primary, 92))
        gradient.setColorAt(0.62, _qcolor(self._palette.accent, 46))
        gradient.setColorAt(1.0, _qcolor(self._palette.background, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, radius, radius)

        for index in range(3):
            ring_radius = radius * (0.54 + (index * 0.22))
            pen = QPen(
                _qcolor(
                    (
                        self._palette.primary,
                        self._palette.secondary,
                        self._palette.accent,
                    )[index],
                    72 - (index * 14),
                )
            )
            pen.setWidthF(max(1.0, span * 0.0022))
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, ring_radius, ring_radius)

        colors = (self._palette.primary, self._palette.secondary, self._palette.accent)
        painter.setPen(Qt.PenStyle.NoPen)
        for particle in frame.particles:
            angle = (particle.x * math.tau) + (
                frame.elapsed_seconds * (0.10 + (frame.mid * 0.22))
            )
            orbit = radius * (0.92 + (particle.depth * 0.78))
            point = QPointF(
                center.x() + (math.cos(angle) * orbit),
                center.y() + (math.sin(angle) * orbit * 0.72),
            )
            painter.setBrush(
                _qcolor(
                    colors[particle.color_index % len(colors)],
                    round(min(0.7, particle.opacity) * 150),
                )
            )
            particle_radius = max(0.65, particle.size * 0.72)
            painter.drawEllipse(point, particle_radius, particle_radius)

    def _paint_starfield(self, painter: QPainter, frame: VisualFrame) -> None:
        colors = (self._palette.primary, self._palette.secondary, self._palette.accent)
        painter.setPen(Qt.PenStyle.NoPen)
        width = max(1, self.width())
        height = max(1, self.height())
        for particle in frame.particles:
            radius = max(
                0.7,
                particle.size
                * (0.8 + particle.depth)
                * (1.0 + (frame.beat * particle.depth * 0.16)),
            )
            color = colors[particle.color_index % len(colors)]
            painter.setBrush(_qcolor(color, round(particle.opacity * 190)))
            painter.drawEllipse(QPointF(particle.x * width, particle.y * height), radius, radius)

    def _paint_aurora(self, painter: QPainter, frame: VisualFrame) -> None:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        colors = (self._palette.primary, self._palette.secondary, self._palette.accent)
        painter.setPen(Qt.PenStyle.NoPen)
        levels = (frame.bass, frame.low_mid, frame.mid, frame.high)
        for index, offset in enumerate(frame.aurora_offsets):
            baseline = height * (0.17 + (index * 0.085))
            band_level = levels[index % len(levels)]
            amplitude = height * (
                0.045 + (frame.energy * 0.025) + (band_level * 0.045)
            )
            path = QPainterPath(QPointF(0.0, baseline + (offset * amplitude)))
            segments = 8
            for segment in range(1, segments + 1):
                x = width * (segment / segments)
                wave = math.sin((segment * 0.82) + offset + (index * 0.74))
                path.lineTo(x, baseline + (wave * amplitude))
            path.lineTo(width, height)
            path.lineTo(0.0, height)
            path.closeSubpath()
            gradient = QLinearGradient(0.0, baseline, width, height)
            color = colors[index % len(colors)]
            gradient.setColorAt(0.0, _qcolor(color, 20))
            gradient.setColorAt(0.5, _qcolor(color, 62))
            gradient.setColorAt(1.0, _qcolor(self._palette.background, 0))
            painter.setBrush(gradient)
            painter.drawPath(path)

        colors = (self._palette.primary, self._palette.secondary, self._palette.accent)
        for particle in frame.particles:
            painter.setBrush(
                _qcolor(
                    colors[particle.color_index % len(colors)],
                    round(min(0.45, particle.opacity) * 90),
                )
            )
            radius = max(0.55, particle.size * 0.5)
            painter.drawEllipse(
                QPointF(particle.x * width, particle.y * height), radius, radius
            )

    def _paint_center(self, painter: QPainter, frame: VisualFrame) -> None:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        if not self._has_track:
            painter.setPen(_qcolor(self._palette.foreground, 220))
            title_font = QFont(self.font())
            title_font.setPointSize(max(20, round(min(width, height) / 24)))
            title_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(title_font)
            center_rect = QRectF(40.0, (height / 2.0) - 58.0, width - 80.0, 54.0)
            painter.drawText(center_rect, Qt.AlignmentFlag.AlignCenter, "Music Vault")
            painter.setPen(_qcolor(self._palette.foreground, 145))
            body_font = QFont(self.font())
            body_font.setPointSize(max(10, round(min(width, height) / 58)))
            painter.setFont(body_font)
            painter.drawText(
                QRectF(40.0, (height / 2.0) + 4.0, width - 80.0, 40.0),
                Qt.AlignmentFlag.AlignCenter,
                "Choose a song to begin",
            )
            return

        base_artwork_scale = {
            "pulse": 0.30,
            "starfield": 0.235,
            "aurora": 0.27,
        }.get(frame.preset, 0.28)
        pulse_scale = frame.pulse * (0.025 if frame.preset == "pulse" else 0.01)
        artwork_size = max(96.0, min(width, height) * (base_artwork_scale + pulse_scale))
        artwork_rect = QRectF(
            (width - artwork_size) / 2.0,
            (height - artwork_size) / 2.0 - 24.0,
            artwork_size,
            artwork_size,
        )
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(artwork_rect, artwork_size * 0.07, artwork_size * 0.07)
        painter.setClipPath(clip)
        if self._show_artwork and self._artwork_cache is not None:
            painter.drawPixmap(artwork_rect, self._artwork_cache, self._artwork_cache.rect())
        else:
            placeholder = QLinearGradient(artwork_rect.topLeft(), artwork_rect.bottomRight())
            placeholder.setColorAt(0.0, _qcolor(self._palette.primary))
            placeholder.setColorAt(1.0, _qcolor(self._palette.secondary))
            painter.fillRect(artwork_rect, placeholder)
        painter.restore()

        if self._title:
            painter.setPen(_qcolor(self._palette.foreground, 230))
            font = QFont(self.font())
            font.setPointSize(max(10, round(min(width, height) / 55)))
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            title = painter.fontMetrics().elidedText(
                self._title,
                Qt.TextElideMode.ElideRight,
                max(1, round(width - 80.0)),
            )
            painter.drawText(
                QRectF(40.0, artwork_rect.bottom() + 14.0, width - 80.0, 32.0),
                Qt.AlignmentFlag.AlignCenter,
                title,
            )
        detail = self._artist or self._album
        if detail:
            painter.setPen(_qcolor(self._palette.foreground, 150))
            font = QFont(self.font())
            font.setPointSize(max(9, round(min(width, height) / 68)))
            painter.setFont(font)
            detail = painter.fontMetrics().elidedText(
                detail,
                Qt.TextElideMode.ElideRight,
                max(1, round(width - 80.0)),
            )
            painter.drawText(
                QRectF(40.0, artwork_rect.bottom() + 46.0, width - 80.0, 28.0),
                Qt.AlignmentFlag.AlignCenter,
                detail,
            )


__all__ = [
    "AdaptiveQualityController",
    "MAX_BEAT",
    "MAX_BRIGHTNESS",
    "MAX_DELTA_SECONDS",
    "MAX_PARTICLES",
    "PRESETS",
    "ParticleState",
    "PartyCanvas",
    "PartyVisualEngine",
    "QUALITY_BUDGETS",
    "QUALITY_LEVELS",
    "QualityBudget",
    "VisualFrame",
]
