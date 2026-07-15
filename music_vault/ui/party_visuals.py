"""Bounded, deterministic Party Mode visuals.

``PartyVisualEngine`` contains no Qt state and can be exercised as a pure
seeded simulation.  ``PartyCanvas`` is the deliberately thin QWidget adapter;
it owns a render timer but never owns or controls media playback.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
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

from music_vault.core.musical_motion import BeatClock, MusicalMotionState
from music_vault.ui.party_palette import (
    ArtworkPalette,
    DEFAULT_PARTY_PALETTE,
    RGB,
    interpolate_color,
)


PRESETS: Final[tuple[str, str, str, str, str, str]] = (
    "static",
    "starfield",
    "aurora",
    "orb_cluster",
    "fireworks",
    "pulse",
)
PRESET_LABELS: Final[dict[str, str]] = {
    "static": "Static",
    "starfield": "Starfield",
    "aurora": "Aurora",
    "orb_cluster": "Orb Cluster",
    "fireworks": "Fireworks",
    "pulse": "Pulse",
}
QUALITY_LEVELS: Final[tuple[str, str, str]] = ("low", "medium", "high")
MAX_DELTA_SECONDS: Final[float] = 0.1
MAX_PARTICLES: Final[int] = 420
MAX_ORBS: Final[int] = 200
MAX_FIREWORK_PARTICLES: Final[int] = 156
MAX_FIREWORK_BURSTS: Final[int] = 3
MAX_BEAT: Final[float] = 0.88
MAX_BRIGHTNESS: Final[float] = 0.84
BASE_ARTWORK_SCALE: Final[float] = 0.30
MAX_ORB_SPRITE_CACHE: Final[int] = 192
MAX_FIREWORK_PROTECTED_RECTS: Final[int] = 8

NormalizedRect = tuple[float, float, float, float]


def center_artwork_rect(width: object, height: object) -> QRectF:
    """Return the locked Batch 9 artwork geometry for layout protection."""

    bounded_width = max(1.0, float(width))
    bounded_height = max(1.0, float(height))
    size = max(96.0, min(bounded_width, bounded_height) * BASE_ARTWORK_SCALE)
    return QRectF(
        (bounded_width - size) / 2.0,
        (bounded_height - size) / 2.0 - 24.0,
        size,
        size,
    )


def center_content_bottom(width: object, height: object) -> float:
    """Bottom edge of the approved artwork/title/artist presentation."""

    return center_artwork_rect(width, height).bottom() + 74.0


@dataclass(frozen=True, slots=True)
class QualityBudget:
    pulse_particles: int
    starfield_particles: int
    aurora_particles: int
    orb_particles: int
    firework_particles_per_burst: int
    firework_bursts: int
    aurora_bands: int
    target_fps: int

    def particle_count(self, preset: str) -> int:
        if preset == "static":
            return 0
        if preset == "pulse":
            return self.pulse_particles
        if preset == "starfield":
            return self.starfield_particles
        if preset == "aurora":
            return self.aurora_particles
        if preset == "orb_cluster":
            return self.orb_particles
        if preset == "fireworks":
            return self.firework_particles_per_burst * self.firework_bursts
        raise ValueError(f"Unknown Party Mode preset: {preset!r}")


QUALITY_BUDGETS: Final[dict[str, QualityBudget]] = {
    "low": QualityBudget(88, 110, 80, 64, 24, 1, 3, 30),
    "medium": QualityBudget(190, 240, 180, 120, 36, 2, 5, 60),
    "high": QualityBudget(360, MAX_PARTICLES, 340, MAX_ORBS, 52, 3, 7, 60),
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
class AlbumTransform:
    """Central album geometry contract shared by every renderer."""

    scale: float = 1.0
    translate_x: float = 0.0
    translate_y: float = 0.0
    rotation_degrees: float = 0.0


@dataclass(frozen=True, slots=True)
class OrbState:
    """Projected, depth-sorted state for one stable spherical orb."""

    x: float
    y: float
    depth: float
    size: float
    opacity: float
    color_index: int
    color_mix: float
    accent: float


@dataclass(frozen=True, slots=True)
class FireworkParticleState:
    """Normalized firework particle state with observable bounded physics."""

    burst_id: int
    x: float
    y: float
    velocity_x: float
    velocity_y: float
    size: float
    opacity: float
    brightness: float
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
    motion: MusicalMotionState
    album_transform: AlbumTransform
    orbs: tuple[OrbState, ...]
    cluster_radius_scale: float
    orb_rotation: tuple[float, float]
    firework_particles: tuple[FireworkParticleState, ...]
    active_firework_bursts: int
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


@dataclass(slots=True)
class _Orb:
    x: float
    y: float
    z: float
    radius: float
    size: float
    opacity: float
    color_index: int
    color_phase: float
    accent: float = 0.0


@dataclass(slots=True)
class _FireworkParticle:
    burst_id: int
    x: float
    y: float
    velocity_x: float
    velocity_y: float
    size: float
    age: float
    lifetime: float
    color_index: int


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


def _feature_bool(snapshot: object | None, names: tuple[str, ...], default: bool) -> bool:
    if snapshot is None:
        return default
    for name in names:
        try:
            if isinstance(snapshot, Mapping) and name in snapshot:
                return bool(snapshot[name])
            if hasattr(snapshot, name):
                return bool(getattr(snapshot, name))
        except Exception:
            continue
    return default


def _feature_timestamp(snapshot: object | None) -> float | None:
    if snapshot is None:
        return None
    try:
        value = snapshot.get("timestamp") if isinstance(snapshot, Mapping) else getattr(snapshot, "timestamp")
        number = float(value)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _smooth_value(
    current: float | None,
    target: float,
    delta: float,
    *,
    attack_seconds: float,
    release_seconds: float,
) -> float:
    """Asymmetric exponential smoothing with exact first-sample behavior."""

    if current is None:
        return target
    time_constant = attack_seconds if target > current else release_seconds
    if delta <= 0.0:
        return current
    factor = 1.0 - math.exp(-delta / max(0.001, time_constant))
    return current + ((target - current) * factor)


def album_transform_for_preset(
    preset: str,
    bar_phase: object = 0.0,
    *,
    reduced_motion: bool = False,
) -> AlbumTransform:
    """Return the single authoritative album transform for a visual preset.

    Five presets are exact identity transforms.  Pulse alone follows a smooth
    four-beat grow-and-return curve and never translates or rotates artwork.
    """

    if preset not in PRESETS:
        raise ValueError(f"Unknown Party Mode preset: {preset!r}")
    if preset != "pulse":
        return AlbumTransform()
    phase = _finite_unit(bar_phase, 0.0) % 1.0
    pulse_curve = math.sin(math.pi * phase) ** 2
    amplitude = 0.006 if reduced_motion else 0.030
    return AlbumTransform(scale=1.0 + (amplitude * pulse_curve))


def _interpolate_album_transform(
    start: AlbumTransform,
    target: AlbumTransform,
    progress: float,
) -> AlbumTransform:
    """Ease album geometry without allowing a preset-switch discontinuity."""

    bounded = _finite_unit(progress)
    eased = bounded * bounded * (3.0 - (2.0 * bounded))

    def blend(left: float, right: float) -> float:
        return left + ((right - left) * eased)

    return AlbumTransform(
        scale=blend(start.scale, target.scale),
        translate_x=blend(start.translate_x, target.translate_x),
        translate_y=blend(start.translate_y, target.translate_y),
        rotation_degrees=blend(start.rotation_degrees, target.rotation_degrees),
    )


def _normalize_protected_rects(
    protected_rects: Iterable[tuple[object, object, object, object]] | None,
) -> tuple[NormalizedRect, ...]:
    """Return a small, finite set of ordered normalized rectangles."""

    if protected_rects is None:
        return ()
    normalized: list[NormalizedRect] = []
    try:
        iterator = iter(protected_rects)
    except TypeError:
        return ()
    for index, candidate in enumerate(iterator):
        if index >= MAX_FIREWORK_PROTECTED_RECTS:
            break
        try:
            left, top, right, bottom = candidate
            values = tuple(float(value) for value in (left, top, right, bottom))
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in values):
            continue
        x1, x2 = sorted((max(0.0, min(1.0, values[0])), max(0.0, min(1.0, values[2]))))
        y1, y2 = sorted((max(0.0, min(1.0, values[1])), max(0.0, min(1.0, values[3]))))
        if x2 <= x1 or y2 <= y1:
            continue
        normalized.append((x1, y1, x2, y2))
    return tuple(normalized)


def is_safe_firework_position(
    x: object,
    y: object,
    protected_rects: Iterable[tuple[object, object, object, object]] | None = None,
) -> bool:
    """Return whether a normalized burst center avoids all protected content."""

    px = _finite_unit(x, -1.0)
    py = _finite_unit(y, -1.0)
    if not 0.06 <= px <= 0.94 or not 0.08 <= py <= 0.68:
        return False
    # Central artwork and title/artist stack.
    if 0.30 <= px <= 0.70 and 0.24 <= py <= 0.70:
        return False
    # Full-screen exit control in the upper-right and top controls on the left.
    if py <= 0.18 and (px <= 0.20 or px >= 0.80):
        return False
    if any(
        left <= px <= right and top <= py <= bottom
        for left, top, right, bottom in _normalize_protected_rects(protected_rects)
    ):
        return False
    return True


class OrbClusterSimulation:
    """Stable spherical coordinates projected into a bounded 3D cluster."""

    def __init__(self, *, seed: int = 0) -> None:
        self.seed = int(seed)
        self._random = random.Random(self.seed)
        self._schedule_random = random.Random(self.seed + 1)
        self._accent_random = random.Random(self.seed + 2)
        self._orbs = self._make_orbs()
        self.rotation_x = 0.0
        self.rotation_y = 0.0
        self._next_accent_beat: int | None = None

    def _make_orbs(self) -> list[_Orb]:
        orbs: list[_Orb] = []
        # Fibonacci distribution avoids a flat ring while seeded jitter keeps
        # the cluster organic and deterministic.
        golden_angle = math.pi * (3.0 - math.sqrt(5.0))
        for index in range(MAX_ORBS):
            normalized = (index + 0.5) / MAX_ORBS
            y = 1.0 - (2.0 * normalized)
            radial = math.sqrt(max(0.0, 1.0 - (y * y)))
            angle = (index * golden_angle) + self._random.uniform(-0.08, 0.08)
            orbs.append(
                _Orb(
                    x=math.cos(angle) * radial,
                    y=y,
                    z=math.sin(angle) * radial,
                    radius=self._random.uniform(0.91, 1.08),
                    size=self._random.uniform(0.72, 1.30),
                    opacity=self._random.uniform(0.56, 0.92),
                    color_index=self._random.randrange(3),
                    color_phase=self._random.random(),
                )
            )
        return orbs

    def reset(self) -> None:
        self._random.seed(self.seed)
        self._schedule_random.seed(self.seed + 1)
        self._accent_random.seed(self.seed + 2)
        self._orbs = self._make_orbs()
        self.rotation_x = 0.0
        self.rotation_y = 0.0
        self._next_accent_beat = None

    def next_accent_in_beats(self, total_beat_count: int) -> int | None:
        if self._next_accent_beat is None:
            return None
        return max(0, self._next_accent_beat - max(0, int(total_beat_count)))

    @staticmethod
    def radius_scale(phrase_phase: float, reduced_motion: bool) -> float:
        phase = _finite_unit(phrase_phase) % 1.0
        eased = 0.5 - (0.5 * math.cos(math.tau * phase))
        minimum, maximum = ((0.98, 1.02) if reduced_motion else (0.92, 1.08))
        return minimum + ((maximum - minimum) * eased)

    def update(
        self,
        delta: float,
        motion: MusicalMotionState,
        *,
        count: int,
        energy: float,
        reduced_motion: bool,
        force_accent: bool = False,
    ) -> tuple[tuple[OrbState, ...], float, tuple[float, float]]:
        bounded_count = max(0, min(MAX_ORBS, int(count)))
        motion_scale = 0.32 if reduced_motion else 1.0
        speed_drive = min(1.15, 0.92 + (_finite_unit(energy) * 0.18))
        self.rotation_y = (self.rotation_y + (delta * 0.20 * speed_drive * motion_scale)) % math.tau
        self.rotation_x = (self.rotation_x + (delta * 0.071 * speed_drive * motion_scale)) % math.tau

        if self._next_accent_beat is None:
            self._next_accent_beat = (
                motion.total_beat_count + self._schedule_random.randint(4, 8)
            )
        scheduled_accent = motion.total_beat_count >= self._next_accent_beat
        if (force_accent or scheduled_accent) and bounded_count:
            subset_size = max(1, min(8, math.ceil(bounded_count * 0.035)))
            for index in self._accent_random.sample(range(bounded_count), subset_size):
                self._orbs[index].accent = 1.0
            self._next_accent_beat = (
                motion.total_beat_count + self._schedule_random.randint(4, 8)
            )
        accent_release = math.exp(-delta / (0.75 if not reduced_motion else 0.42))
        radius_scale = self.radius_scale(motion.phrase_phase, reduced_motion)

        sin_y, cos_y = math.sin(self.rotation_y), math.cos(self.rotation_y)
        sin_x, cos_x = math.sin(self.rotation_x), math.cos(self.rotation_x)
        projected: list[OrbState] = []
        for orb in self._orbs[:bounded_count]:
            orb.accent *= accent_release
            source_x = orb.x * orb.radius
            source_y = orb.y * orb.radius
            source_z = orb.z * orb.radius
            rotated_x = (source_x * cos_y) + (source_z * sin_y)
            rotated_z = (-source_x * sin_y) + (source_z * cos_y)
            rotated_y = (source_y * cos_x) - (rotated_z * sin_x)
            depth = (source_y * sin_x) + (rotated_z * cos_x)
            normalized_depth = (depth + 1.1) / 2.2
            perspective = 0.82 + (normalized_depth * 0.30)
            size = orb.size * perspective * (1.0 + (orb.accent * 0.045))
            opacity = min(
                0.86,
                orb.opacity * (0.40 + (normalized_depth * 0.48)) + (orb.accent * 0.06),
            )
            projected.append(
                OrbState(
                    x=0.5 + (rotated_x * radius_scale * perspective * 0.255),
                    y=0.47 + (rotated_y * radius_scale * perspective * 0.255),
                    depth=max(0.0, min(1.0, normalized_depth)),
                    size=size,
                    opacity=max(0.0, opacity),
                    color_index=orb.color_index,
                    color_mix=(orb.color_phase + (motion.beat_position / 48.0)) % 1.0,
                    accent=orb.accent,
                )
            )
        projected.sort(key=lambda orb: orb.depth)
        return tuple(projected), radius_scale, (self.rotation_x, self.rotation_y)


class FireworksSimulation:
    """Bounded intermittent bursts with drag, gravity, fade, and cleanup."""

    def __init__(self, *, seed: int = 0) -> None:
        self.seed = int(seed)
        self._random = random.Random(self.seed)
        self._schedule_random = random.Random(self.seed + 1)
        self._particles: list[_FireworkParticle] = []
        self._next_burst_id = 1
        self._next_firework_beat: int | None = None
        self._protected_rects: tuple[NormalizedRect, ...] = ()

    @property
    def live_particle_count(self) -> int:
        return len(self._particles)

    @property
    def active_burst_count(self) -> int:
        return len({particle.burst_id for particle in self._particles})

    @property
    def protected_rects(self) -> tuple[NormalizedRect, ...]:
        return self._protected_rects

    def set_protected_rects(
        self,
        protected_rects: Iterable[tuple[object, object, object, object]] | None,
    ) -> None:
        self._protected_rects = _normalize_protected_rects(protected_rects)

    def reset(self) -> None:
        self._random.seed(self.seed)
        self._schedule_random.seed(self.seed + 1)
        self._particles.clear()
        self._next_burst_id = 1
        self._next_firework_beat = None

    def next_firework_in_beats(self, total_beat_count: int) -> int | None:
        if self._next_firework_beat is None:
            return None
        return max(0, self._next_firework_beat - max(0, int(total_beat_count)))

    def _safe_center(self) -> tuple[float, float]:
        for _ in range(24):
            x = self._random.uniform(0.08, 0.92)
            y = self._random.uniform(0.10, 0.64)
            if is_safe_firework_position(x, y, self._protected_rects):
                return x, y
        return (0.18, 0.30)

    def spawn(
        self,
        *,
        particles_per_burst: int,
        maximum_bursts: int,
        maximum_particles: int,
        reduced_motion: bool,
        center: tuple[float, float] | None = None,
    ) -> bool:
        max_bursts = max(1, min(MAX_FIREWORK_BURSTS, int(maximum_bursts)))
        max_particles = max(1, min(MAX_FIREWORK_PARTICLES, int(maximum_particles)))
        if self.active_burst_count >= max_bursts or len(self._particles) >= max_particles:
            return False
        x, y = center if center is not None else self._safe_center()
        if not is_safe_firework_position(x, y, self._protected_rects):
            return False
        requested = max(4, min(64, int(particles_per_burst)))
        if reduced_motion:
            requested = max(4, math.ceil(requested * 0.42))
        count = min(requested, max_particles - len(self._particles))
        burst_id = self._next_burst_id
        self._next_burst_id += 1
        speed_scale = 0.56 if reduced_motion else 1.0
        phase_offset = self._random.random() * math.tau
        for index in range(count):
            angle = phase_offset + (math.tau * index / count) + self._random.uniform(-0.10, 0.10)
            speed = self._random.uniform(0.13, 0.25) * speed_scale
            self._particles.append(
                _FireworkParticle(
                    burst_id=burst_id,
                    x=x,
                    y=y,
                    velocity_x=math.cos(angle) * speed,
                    velocity_y=math.sin(angle) * speed,
                    size=self._random.uniform(0.65, 1.25),
                    age=0.0,
                    lifetime=self._random.uniform(1.35, 2.15) * (0.72 if reduced_motion else 1.0),
                    color_index=self._random.randrange(3),
                )
            )
        return True

    def update(
        self,
        delta: float,
        *,
        trigger: bool,
        particles_per_burst: int,
        maximum_bursts: int,
        maximum_particles: int,
        reduced_motion: bool,
        total_beat_count: int | None = None,
    ) -> tuple[FireworkParticleState, ...]:
        scheduled = False
        beat_count = None if total_beat_count is None else max(0, int(total_beat_count))
        if beat_count is not None:
            if self._next_firework_beat is None:
                self._next_firework_beat = (
                    beat_count + self._schedule_random.randint(1, 64)
                )
            scheduled = beat_count >= self._next_firework_beat
        if trigger or scheduled:
            spawned = self.spawn(
                particles_per_burst=particles_per_burst,
                maximum_bursts=maximum_bursts,
                maximum_particles=maximum_particles,
                reduced_motion=reduced_motion,
            )
            if scheduled and beat_count is not None:
                interval = self._schedule_random.randint(1, 64) if spawned else 1
                self._next_firework_beat = beat_count + interval
        drag = math.exp(-1.05 * delta)
        gravity = 0.105 if reduced_motion else 0.145
        live: list[_FireworkParticle] = []
        states: list[FireworkParticleState] = []
        for particle in self._particles[:MAX_FIREWORK_PARTICLES]:
            particle.age += delta
            particle.velocity_x *= drag
            particle.velocity_y = (particle.velocity_y * drag) + (gravity * delta)
            particle.x += particle.velocity_x * delta
            particle.y += particle.velocity_y * delta
            life = 1.0 - (particle.age / particle.lifetime)
            if life <= 0.0 or particle.y > 1.08 or particle.x < -0.08 or particle.x > 1.08:
                continue
            live.append(particle)
            opacity = max(0.0, min(0.78, (life**1.45) * 0.78))
            states.append(
                FireworkParticleState(
                    burst_id=particle.burst_id,
                    x=particle.x,
                    y=particle.y,
                    velocity_x=particle.velocity_x,
                    velocity_y=particle.velocity_y,
                    size=particle.size,
                    opacity=opacity,
                    brightness=min(0.82, 0.36 + (life * 0.46)),
                    color_index=particle.color_index,
                )
            )
        self._particles = live
        return tuple(states)


class PartyVisualEngine:
    """Pure seeded visual simulation with strictly bounded work."""

    def __init__(
        self,
        *,
        seed: int = 0,
        preset: str = "static",
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
        self.transition_seconds = max(0.30, min(0.70, float(transition_seconds)))
        self._features: object | None = None
        self._elapsed = 0.0
        self._previous_preset: str | None = None
        self._transition_elapsed = self.transition_seconds
        self._album_transition_start = AlbumTransform()
        self._particles = self._make_particles(self.seed)
        self._beat_clock = BeatClock(seed=self.seed + 101)
        self._orb_simulation = OrbClusterSimulation(seed=self.seed + 211)
        self._fireworks = FireworksSimulation(seed=self.seed + 307)
        self._smoothed: dict[str, float | None] = {
            "energy": None,
            "bass": None,
            "low_mid": None,
            "mid": None,
            "high": None,
            "starfield_drive": None,
        }
        self._beat_accent = 0.0
        self._last_beat_flag = False
        self._last_beat_timestamp: float | None = None
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

    @property
    def requires_animation(self) -> bool:
        return self.preset != "static" or self._previous_preset is not None

    @property
    def firework_protected_rects(self) -> tuple[NormalizedRect, ...]:
        return self._fireworks.protected_rects

    def particle_budget(self, preset: str | None = None) -> int:
        selected = preset or self.preset
        self._validate_preset(selected)
        count = QUALITY_BUDGETS[self.quality].particle_count(selected)
        if self.reduced_motion and count:
            count = max(3, math.ceil(count * 0.45))
        return min(MAX_PARTICLES, count)

    def set_features(self, snapshot: object | None) -> None:
        self._features = snapshot

    def set_firework_protected_rects(
        self,
        protected_rects: Iterable[tuple[object, object, object, object]] | None,
    ) -> None:
        self._fireworks.set_protected_rects(protected_rects)

    def set_preset(self, preset: str) -> None:
        self._validate_preset(preset)
        if preset == self.preset:
            return
        current_transform = (
            self._last_frame.album_transform
            if self._last_frame is not None
            else album_transform_for_preset(
                self.preset,
                0.0,
                reduced_motion=self.reduced_motion,
            )
        )
        if self.reduced_motion:
            self._previous_preset = None
            self._transition_elapsed = self.transition_seconds
        else:
            self._previous_preset = self.preset
            self._transition_elapsed = 0.0
            self._album_transition_start = current_transform
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
        self._album_transition_start = AlbumTransform()
        self._particles = self._make_particles(self.seed)
        self._beat_clock = BeatClock(seed=self.seed + 101)
        self._orb_simulation.reset()
        self._fireworks.reset()
        self._smoothed = {name: None for name in self._smoothed}
        self._beat_accent = 0.0
        self._last_beat_flag = False
        self._last_beat_timestamp = None
        self._last_frame = None

    def _motion_state(self, delta: float, beat: float) -> MusicalMotionState:
        raw_flag = _feature_bool(self._features, ("beat", "on_beat"), False)
        timestamp = _feature_timestamp(self._features)
        if timestamp is not None:
            is_new = raw_flag and timestamp != self._last_beat_timestamp
        else:
            is_new = raw_flag and not self._last_beat_flag
        if is_new:
            self._last_beat_timestamp = timestamp
        self._last_beat_flag = raw_flag
        audio_available = _feature_bool(
            self._features,
            ("audio_available", "audio_reactivity_available"),
            self._features is not None,
        )
        return self._beat_clock.advance(
            delta,
            detected_beat=is_new,
            beat_strength=beat,
            audio_available=audio_available,
        )

    def update(
        self, delta_seconds: float, features: object | None = None
    ) -> VisualFrame:
        if features is not None:
            self._features = features
        delta = _finite_unit(delta_seconds, 0.0, MAX_DELTA_SECONDS)
        self._elapsed += delta

        raw_energy = _feature_value(
            self._features, ("energy", "rms", "level", "amplitude", "peak"), 0.18
        )
        raw_levels = {
            "energy": raw_energy,
            "bass": _feature_value(
                self._features, ("bass", "low", "low_energy"), raw_energy * 0.8
            ),
            "low_mid": _feature_value(
                self._features,
                ("low_mid", "lowmid", "low_mid_energy"),
                raw_energy * 0.72,
            ),
            "mid": _feature_value(
                self._features, ("mid", "mid_energy"), raw_energy * 0.64
            ),
            "high": _feature_value(
                self._features,
                ("high", "treble", "high_energy"),
                raw_energy * 0.52,
            ),
        }
        beat = min(
            MAX_BEAT,
            max(
                _feature_value(self._features, ("beat_strength",), 0.0),
                _feature_value(self._features, ("beat", "on_beat"), 0.0),
            ),
        )
        motion = self._motion_state(delta, beat)

        smoothing_delta = delta if delta > 0.0 else (1.0 / 60.0)
        for name, target in raw_levels.items():
            self._smoothed[name] = _smooth_value(
                self._smoothed[name],
                target,
                smoothing_delta,
                attack_seconds=0.10 if name != "high" else 0.14,
                release_seconds=1.35 if name != "energy" else 0.90,
            )
        energy = float(self._smoothed["energy"] or 0.0)
        bass = float(self._smoothed["bass"] or 0.0)
        low_mid = float(self._smoothed["low_mid"] or 0.0)
        mid = float(self._smoothed["mid"] or 0.0)
        high = float(self._smoothed["high"] or 0.0)

        if _feature_bool(self._features, ("beat", "on_beat"), False):
            self._beat_accent = max(self._beat_accent, beat * 0.08)
        self._beat_accent *= math.exp(-delta / 0.18) if delta else 1.0
        brightness = min(
            MAX_BRIGHTNESS,
            _feature_value(
                self._features,
                ("brightness", "high", "treble", "high_energy"),
                0.24 + (energy * 0.32),
            )
            + self._beat_accent,
        )

        active_presets = {self.preset}
        if self._previous_preset is not None:
            active_presets.add(self._previous_preset)
        motion_scale = 0.14 if self.reduced_motion else 1.0
        starfield_target = 1.0 + (energy * 1.10)
        self._smoothed["starfield_drive"] = _smooth_value(
            self._smoothed["starfield_drive"],
            starfield_target,
            smoothing_delta,
            attack_seconds=0.38,
            release_seconds=1.20,
        )
        starfield_drive = float(self._smoothed["starfield_drive"] or 1.0)
        needs_standard_particles = bool(
            active_presets.intersection({"starfield", "aurora", "pulse"})
        )
        if needs_standard_particles:
            for particle in self._particles:
                drive = starfield_drive if "starfield" in active_presets else 1.0
                particle.y = (
                    particle.y + (delta * particle.speed * motion_scale * drive)
                ) % 1.0
                particle.x = (
                    particle.x
                    + (delta * particle.drift * motion_scale)
                    + (
                        math.sin(self._elapsed + particle.phase)
                        * delta
                        * 0.002
                        * motion_scale
                    )
                ) % 1.0

        if self._previous_preset is not None:
            self._transition_elapsed += delta
            transition = min(1.0, self._transition_elapsed / self.transition_seconds)
            if transition >= 1.0:
                self._previous_preset = None
        else:
            transition = 1.0

        pulse_curve = math.sin(math.pi * motion.bar_phase) ** 2
        pulse = min(0.30 if self.reduced_motion else 1.0, pulse_curve)
        standard_count = 0
        for visual in ("pulse", "starfield", "aurora"):
            if visual in active_presets:
                standard_count = max(standard_count, self.particle_budget(visual))
        particles = tuple(
            ParticleState(
                x=particle.x,
                y=particle.y,
                size=particle.size,
                opacity=min(1.0, particle.opacity * (0.58 + (brightness * 0.5))),
                depth=particle.depth,
                color_index=particle.color_index,
            )
            for particle in self._particles[:standard_count]
        )

        aurora_offsets: tuple[float, ...] = ()
        if "aurora" in active_presets:
            band_count = QUALITY_BUDGETS[self.quality].aurora_bands
            if self.reduced_motion:
                band_count = max(2, math.ceil(band_count * 0.5))
            aurora_levels = (bass, low_mid, mid, high)
            wave_scale = 0.42 if self.reduced_motion else 1.0
            aurora_offsets = tuple(
                math.sin(
                    (self._elapsed * (0.34 + (index * 0.035)) * wave_scale)
                    + index
                    + (aurora_levels[index % 4] * 0.32)
                )
                for index in range(band_count)
            )

        orbs: tuple[OrbState, ...] = ()
        cluster_radius = OrbClusterSimulation.radius_scale(
            motion.phrase_phase, self.reduced_motion
        )
        orb_rotation = (
            self._orb_simulation.rotation_x,
            self._orb_simulation.rotation_y,
        )
        if "orb_cluster" in active_presets:
            orbs, cluster_radius, orb_rotation = self._orb_simulation.update(
                delta,
                motion,
                count=self.particle_budget("orb_cluster"),
                energy=energy,
                reduced_motion=self.reduced_motion,
            )

        budget = QUALITY_BUDGETS[self.quality]
        maximum_bursts = 1 if self.reduced_motion else budget.firework_bursts
        maximum_firework_particles = min(
            MAX_FIREWORK_PARTICLES,
            budget.firework_particles_per_burst * maximum_bursts,
        )
        firework_particles: tuple[FireworkParticleState, ...] = ()
        if "fireworks" in active_presets:
            firework_particles = self._fireworks.update(
                delta,
                trigger=False,
                particles_per_burst=budget.firework_particles_per_burst,
                maximum_bursts=maximum_bursts,
                maximum_particles=maximum_firework_particles,
                reduced_motion=self.reduced_motion,
                total_beat_count=(
                    motion.total_beat_count if self.preset == "fireworks" else None
                ),
            )
        elif self._fireworks.live_particle_count:
            # Leaving Fireworks clears private simulation state rather than
            # carrying an invisible live-particle workload indefinitely.
            self._fireworks.reset()

        target_album_transform = album_transform_for_preset(
            self.preset,
            motion.bar_phase,
            reduced_motion=self.reduced_motion,
        )
        album_transform = (
            _interpolate_album_transform(
                self._album_transition_start,
                target_album_transform,
                transition,
            )
            if transition < 1.0
            else target_album_transform
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
            motion=motion,
            album_transform=album_transform,
            orbs=orbs,
            cluster_radius_scale=cluster_radius,
            orb_rotation=orb_rotation,
            firework_particles=firework_particles,
            active_firework_bursts=self._fireworks.active_burst_count,
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
        preset: str = "static",
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
        self._orb_sprite_cache: dict[tuple[int, int, int, int], QPixmap] = {}
        self._title = ""
        self._artist = ""
        self._album = ""
        self._is_playing = False
        self._has_track = False
        self._clock = QElapsedTimer()
        self._last_simulation_seconds = 0.0
        self._render_requested = False
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

    @property
    def firework_protected_rects(self) -> tuple[NormalizedRect, ...]:
        return self._engine.firework_protected_rects

    def set_features(self, snapshot: object | None) -> None:
        self._features = snapshot
        self._engine.set_features(snapshot)
        self._refresh_frame()

    def set_firework_protected_rects(
        self,
        protected_rects: Iterable[tuple[object, object, object, object]] | None,
    ) -> None:
        self._engine.set_firework_protected_rects(protected_rects)

    def set_preset(self, name: str) -> None:
        self._engine.set_preset(name)
        self._refresh_frame()
        self._sync_render_timer()

    def set_palette(self, palette: ArtworkPalette) -> None:
        if not isinstance(palette, ArtworkPalette):
            raise TypeError("palette must be an ArtworkPalette")
        self._palette = palette
        self._orb_sprite_cache.clear()
        self.update()

    def set_reduced_motion(self, reduced: bool) -> None:
        self._engine.set_reduced_motion(reduced)
        self._apply_timer_interval()
        self._refresh_frame()
        self._sync_render_timer()

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
            return {
                "energy": 0.025,
                "bass": 0.018,
                "mid": 0.02,
                "high": 0.015,
                "audio_available": False,
            }
        if self._is_playing:
            return {
                "energy": 0.16,
                "bass": 0.12,
                "mid": 0.10,
                "high": 0.065,
                "audio_available": False,
            }
        return {
            "energy": 0.055,
            "bass": 0.04,
            "mid": 0.035,
            "high": 0.02,
            "audio_available": False,
        }

    def start_rendering(self) -> None:
        self._render_requested = True
        self._sync_render_timer()

    def stop_rendering(self) -> None:
        self._render_requested = False
        self._timer.stop()
        self._clock.invalidate()

    def _sync_render_timer(self) -> None:
        should_run = self._render_requested and self._engine.requires_animation
        if should_run and not self._timer.isActive():
            self._clock.start()
            self._timer.start()
        elif not should_run and self._timer.isActive():
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
                "orb_count": len(self._frame.orbs) if self._frame is not None else 0,
                "firework_particle_count": (
                    len(self._frame.firework_particles) if self._frame is not None else 0
                ),
                "active_firework_bursts": (
                    self._frame.active_firework_bursts if self._frame is not None else 0
                ),
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
        self._sync_render_timer()

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
        if preset == "static":
            pass
        elif preset == "pulse":
            self._paint_pulse(painter, frame)
        elif preset == "starfield":
            self._paint_starfield(painter, frame)
        elif preset == "aurora":
            self._paint_aurora(painter, frame)
        elif preset == "orb_cluster":
            self._paint_orb_cluster(painter, frame)
        elif preset == "fireworks":
            self._paint_fireworks(painter, frame)
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
                * (1.0 + (frame.energy * particle.depth * 0.08)),
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
        bar_accent = (
            (math.sin(math.pi * frame.motion.bar_phase) ** 2)
            * frame.motion.tempo_confidence
            * (0.015 if frame.reduced_motion else 0.04)
        )
        for index, offset in enumerate(frame.aurora_offsets):
            baseline = height * (0.17 + (index * 0.085))
            band_level = levels[index % len(levels)]
            amplitude = height * (
                0.045
                + (frame.energy * 0.025)
                + (band_level * 0.045)
                + bar_accent
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

    def _orb_sprite(self, orb: OrbState, diameter: int) -> QPixmap:
        diameter_bucket = max(4, min(64, round(diameter / 4) * 4))
        mix_bucket = max(0, min(7, round(orb.color_mix * 7)))
        opacity_bucket = max(1, min(7, round(orb.opacity * 7)))
        key = (diameter_bucket, orb.color_index % 3, mix_bucket, opacity_bucket)
        cached = self._orb_sprite_cache.get(key)
        if cached is not None:
            return cached
        if len(self._orb_sprite_cache) >= MAX_ORB_SPRITE_CACHE:
            self._orb_sprite_cache.pop(next(iter(self._orb_sprite_cache)))
        colors = (self._palette.primary, self._palette.secondary, self._palette.accent)
        start = colors[orb.color_index % len(colors)]
        end = colors[(orb.color_index + 1) % len(colors)]
        color = interpolate_color(start, end, mix_bucket / 7.0)
        pixmap = QPixmap(diameter_bucket, diameter_bucket)
        pixmap.fill(Qt.GlobalColor.transparent)
        sprite_painter = QPainter(pixmap)
        sprite_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = diameter_bucket / 2.0
        gradient = QRadialGradient(
            QPointF(radius * 0.68, radius * 0.64),
            radius,
            QPointF(radius * 0.58, radius * 0.52),
        )
        alpha = round((opacity_bucket / 7.0) * 205)
        highlight = tuple(min(255, channel + 58) for channel in color)
        rim = tuple(max(0, round(channel * 0.38)) for channel in color)
        gradient.setColorAt(0.0, _qcolor(highlight, min(220, alpha + 34)))
        gradient.setColorAt(0.23, _qcolor(color, alpha))
        gradient.setColorAt(0.72, _qcolor(color, round(alpha * 0.68)))
        gradient.setColorAt(0.93, _qcolor(rim, round(alpha * 0.52)))
        gradient.setColorAt(1.0, _qcolor(rim, 0))
        sprite_painter.setPen(Qt.PenStyle.NoPen)
        sprite_painter.setBrush(gradient)
        sprite_painter.drawEllipse(QRectF(0.0, 0.0, diameter_bucket, diameter_bucket))
        sprite_painter.end()
        self._orb_sprite_cache[key] = pixmap
        return pixmap

    def _paint_orb_cluster(self, painter: QPainter, frame: VisualFrame) -> None:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        span = min(width, height)
        for orb in frame.orbs:
            diameter = max(4, round(span * 0.0105 * orb.size))
            sprite = self._orb_sprite(orb, diameter)
            rendered = diameter * (1.0 + (orb.accent * 0.04))
            rect = QRectF(
                (orb.x * width) - (rendered / 2.0),
                (orb.y * height) - (rendered / 2.0),
                rendered,
                rendered,
            )
            painter.setOpacity(max(0.0, min(0.88, orb.opacity)))
            painter.drawPixmap(rect, sprite, sprite.rect())
        painter.setOpacity(1.0)

    def _paint_fireworks(self, painter: QPainter, frame: VisualFrame) -> None:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        span = min(width, height)
        colors = (self._palette.primary, self._palette.secondary, self._palette.accent)
        painter.setPen(Qt.PenStyle.NoPen)
        for particle in frame.firework_particles:
            radius = max(1.0, span * 0.0022 * particle.size)
            center = QPointF(particle.x * width, particle.y * height)
            color = colors[particle.color_index % len(colors)]
            glow = QRadialGradient(center, radius * 2.8)
            alpha = round(min(0.72, particle.opacity) * 220)
            glow.setColorAt(0.0, _qcolor(color, alpha))
            glow.setColorAt(0.46, _qcolor(color, round(alpha * 0.68)))
            glow.setColorAt(1.0, _qcolor(color, 0))
            painter.setBrush(glow)
            painter.drawEllipse(center, radius * 2.8, radius * 2.8)

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

        base_artwork_rect = center_artwork_rect(width, height)
        base_artwork_size = base_artwork_rect.width()
        transform = frame.album_transform
        artwork_size = base_artwork_size * transform.scale
        center_x = (width / 2.0) + (transform.translate_x * width)
        center_y = (height / 2.0) - 24.0 + (transform.translate_y * height)
        artwork_rect = QRectF(
            center_x - (artwork_size / 2.0),
            center_y - (artwork_size / 2.0),
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
                QRectF(
                    40.0,
                    base_artwork_rect.bottom() + 14.0,
                    width - 80.0,
                    32.0,
                ),
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
                QRectF(
                    40.0,
                    base_artwork_rect.bottom() + 46.0,
                    width - 80.0,
                    28.0,
                ),
                Qt.AlignmentFlag.AlignCenter,
                detail,
            )


__all__ = [
    "AdaptiveQualityController",
    "AlbumTransform",
    "BASE_ARTWORK_SCALE",
    "center_artwork_rect",
    "center_content_bottom",
    "FireworkParticleState",
    "FireworksSimulation",
    "MAX_BEAT",
    "MAX_BRIGHTNESS",
    "MAX_DELTA_SECONDS",
    "MAX_FIREWORK_BURSTS",
    "MAX_FIREWORK_PARTICLES",
    "MAX_FIREWORK_PROTECTED_RECTS",
    "MAX_ORBS",
    "MAX_PARTICLES",
    "OrbClusterSimulation",
    "OrbState",
    "PRESETS",
    "PRESET_LABELS",
    "ParticleState",
    "PartyCanvas",
    "PartyVisualEngine",
    "QUALITY_BUDGETS",
    "QUALITY_LEVELS",
    "QualityBudget",
    "VisualFrame",
    "album_transform_for_preset",
    "is_safe_firework_position",
]
