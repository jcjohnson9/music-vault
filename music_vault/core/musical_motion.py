"""Bounded musical timing primitives for Party Mode.

The audio analyzer deliberately reports local, low-level observations.  This
module turns those observations into a continuous musical clock so renderers
do not jump in response to individual ``beat`` flags.  It owns no Qt objects,
threads, media objects, or unbounded event queues.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import random
from statistics import median
from typing import Final


MIN_TEMPO_BPM: Final[float] = 55.0
MAX_TEMPO_BPM: Final[float] = 200.0
FALLBACK_TEMPO_BPM: Final[float] = 72.0
MAX_INTERVAL_HISTORY: Final[int] = 12
MAX_BEATS_PER_UPDATE: Final[int] = 4


def _finite(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _unit(value: object) -> float:
    return max(0.0, min(1.0, _finite(value)))


@dataclass(frozen=True, slots=True)
class MusicalMotionState:
    """One immutable snapshot of the continuous musical clock."""

    timestamp: float
    tempo_bpm: float
    tempo_confidence: float
    beat_position: float
    beat_phase: float
    total_beat_count: int
    bar_phase: float
    phrase_phase: float
    beats_crossed: int
    accent_trigger: bool
    firework_trigger: bool
    next_accent_in_beats: int
    next_firework_in_beats: int


class BeatClock:
    """Continuous, outlier-resistant musical phase with bounded schedules.

    ``advance`` is the render-loop API.  ``observe_beat`` and ``state_at`` are
    provided for deterministic tests and callers that already own an absolute
    monotonic timestamp.  A late observation accumulates a small phase error
    which is paid down over time; it never teleports or reverses the clock.
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        minimum_bpm: float = MIN_TEMPO_BPM,
        maximum_bpm: float = MAX_TEMPO_BPM,
        fallback_bpm: float = FALLBACK_TEMPO_BPM,
        history_size: int = MAX_INTERVAL_HISTORY,
        phase_correction_seconds: float = 0.8,
    ) -> None:
        minimum = max(30.0, min(240.0, _finite(minimum_bpm, MIN_TEMPO_BPM)))
        maximum = max(minimum, min(300.0, _finite(maximum_bpm, MAX_TEMPO_BPM)))
        fallback = max(minimum, min(maximum, _finite(fallback_bpm, FALLBACK_TEMPO_BPM)))
        self.minimum_bpm = minimum
        self.maximum_bpm = maximum
        self.fallback_bpm = fallback
        self.phase_correction_seconds = max(
            0.2, min(3.0, _finite(phase_correction_seconds, 0.8))
        )
        self._intervals: deque[float] = deque(
            maxlen=max(3, min(MAX_INTERVAL_HISTORY, int(history_size)))
        )
        self._seed = int(seed)
        self._random = random.Random(self._seed)
        self._time = 0.0
        self._beat_position = 0.0
        self._period: float | None = None
        self._last_observation: float | None = None
        self._phase_error = 0.0
        self._confidence = 0.0
        self._last_crossed = 0
        self._next_accent_beat = self._random.randint(4, 8)
        self._next_firework_beat = self._random.randint(1, 64)
        self._last_state = self._snapshot(0, False, False)

    @property
    def interval_count(self) -> int:
        return len(self._intervals)

    @property
    def tempo_bpm(self) -> float:
        period = self._period or (60.0 / self.fallback_bpm)
        return 60.0 / period

    @property
    def tempo_confidence(self) -> float:
        return self._confidence

    @property
    def last_state(self) -> MusicalMotionState:
        return self._last_state

    @property
    def next_accent_interval(self) -> int:
        return max(0, self._next_accent_beat - math.floor(self._beat_position))

    @property
    def next_firework_interval(self) -> int:
        return max(0, self._next_firework_beat - math.floor(self._beat_position))

    def reset(self) -> MusicalMotionState:
        """Reset timing while retaining deterministic schedule identity."""

        self._intervals.clear()
        self._random.seed(self._seed)
        self._time = 0.0
        self._beat_position = 0.0
        self._period = None
        self._last_observation = None
        self._phase_error = 0.0
        self._confidence = 0.0
        self._last_crossed = 0
        self._next_accent_beat = self._random.randint(4, 8)
        self._next_firework_beat = self._random.randint(1, 64)
        self._last_state = self._snapshot(0, False, False)
        return self._last_state

    def _candidate_interval(self, elapsed: float) -> float | None:
        minimum_interval = 60.0 / self.maximum_bpm
        maximum_interval = 60.0 / self.minimum_bpm
        if elapsed <= 0.0:
            return None

        candidate = elapsed
        if self._period is not None:
            # A missing detector event commonly produces an exact multiple of
            # the recent period.  Normalize at most four missed beats without
            # recording or replaying each event.
            multiple = max(1, min(4, round(elapsed / self._period)))
            normalized = elapsed / multiple
            if abs(normalized - self._period) / self._period <= 0.24:
                candidate = normalized

        if not minimum_interval <= candidate <= maximum_interval:
            return None
        if self._intervals:
            center = median(self._intervals)
            if abs(candidate - center) / center > 0.30:
                return None
        return candidate

    def observe_beat(self, timestamp: object, strength: object = 1.0) -> bool:
        """Accept one detected beat, returning whether it was tempo-valid."""

        observed_at = _finite(timestamp, self._time)
        if observed_at < self._time:
            return False
        state = self._advance_to(observed_at, audio_available=True)
        accepted = self._record_observation(observed_at, strength)
        self._last_state = self._snapshot(
            state.beats_crossed, state.accent_trigger, state.firework_trigger
        )
        return accepted

    def _record_observation(self, observed_at: float, strength: object) -> bool:
        accepted = self._last_observation is None
        reseeded = False
        if self._last_observation is not None:
            elapsed = observed_at - self._last_observation
            candidate = self._candidate_interval(elapsed)
            if candidate is None:
                recent_period = self._period or (60.0 / self.fallback_bpm)
                if elapsed < recent_period * 4.5:
                    return False
                # After a bounded long gap, re-anchor observation timing while
                # retaining continuous rendered phase. Clearing interval
                # evidence lets the next two detections establish a changed
                # tempo instead of rejecting forever against stale history.
                self._intervals.clear()
                self._confidence *= 0.55
                reseeded = True
            else:
                self._intervals.append(candidate)
                estimate = median(self._intervals)
                if self._period is None:
                    self._period = estimate
                else:
                    self._period += (estimate - self._period) * 0.24
            accepted = True

        self._last_observation = observed_at
        if accepted:
            # The first event may establish an origin; later events only queue
            # a bounded correction which is applied smoothly by _advance_to.
            nearest = round(self._beat_position)
            if self._period is None and not self._intervals and not reseeded:
                self._beat_position = float(nearest)
            else:
                error = max(-0.45, min(0.45, nearest - self._beat_position))
                self._phase_error = max(
                    -0.5, min(0.5, self._phase_error + (error * 0.34))
                )
            evidence = 0.10 + (0.08 * min(1.0, _unit(strength)))
            self._confidence = min(1.0, self._confidence + evidence)
        return accepted

    def state_at(
        self,
        timestamp: object,
        *,
        audio_available: bool = True,
    ) -> MusicalMotionState:
        """Advance to an absolute monotonic timestamp and return a snapshot."""

        target = max(self._time, _finite(timestamp, self._time))
        return self._advance_to(target, audio_available=bool(audio_available))

    def advance(
        self,
        delta_seconds: object,
        *,
        detected_beat: bool = False,
        beat_strength: object = 1.0,
        audio_available: bool = True,
    ) -> MusicalMotionState:
        """Advance by a bounded delta and optionally observe one raw beat."""

        delta = max(0.0, min(0.25, _finite(delta_seconds)))
        target = self._time + delta
        state = self._advance_to(target, audio_available=bool(audio_available))
        if detected_beat:
            self._record_observation(target, beat_strength)
        self._last_state = self._snapshot(
            state.beats_crossed, state.accent_trigger, state.firework_trigger
        )
        return self._last_state

    def _advance_to(
        self,
        target: float,
        *,
        audio_available: bool,
    ) -> MusicalMotionState:
        delta = max(0.0, target - self._time)
        period = self._period or (60.0 / self.fallback_bpm)
        raw_advance = delta / period
        correction = 0.0
        if delta > 0.0 and abs(self._phase_error) > 1e-9:
            correction_fraction = 1.0 - math.exp(-delta / self.phase_correction_seconds)
            correction = self._phase_error * correction_fraction
            # Phase correction may slow or accelerate, never reverse or jump.
            correction = max(-raw_advance * 0.45, min(raw_advance * 0.45, correction))
            self._phase_error -= correction
        self._beat_position += raw_advance + correction
        self._time = target

        if delta > 0.0:
            if not audio_available:
                self._confidence *= math.exp(-delta / 6.0)
            elif self._last_observation is not None:
                age = self._time - self._last_observation
                hold = period * 6.0
                if age > hold:
                    self._confidence *= math.exp(-delta / 12.0)

        total = math.floor(self._beat_position)
        crossed = max(0, min(MAX_BEATS_PER_UPDATE, total - self._last_crossed))
        self._last_crossed = max(self._last_crossed, total)
        accent = False
        firework = False
        if crossed:
            if total >= self._next_accent_beat:
                accent = True
                self._next_accent_beat = total + self._random.randint(4, 8)
            if total >= self._next_firework_beat:
                firework = True
                self._next_firework_beat = total + self._random.randint(1, 64)
        self._last_state = self._snapshot(crossed, accent, firework)
        return self._last_state

    def _snapshot(
        self,
        beats_crossed: int,
        accent: bool,
        firework: bool,
    ) -> MusicalMotionState:
        total = max(0, math.floor(self._beat_position))
        phase = self._beat_position - math.floor(self._beat_position)
        return MusicalMotionState(
            timestamp=self._time,
            tempo_bpm=self.tempo_bpm,
            tempo_confidence=max(0.0, min(1.0, self._confidence)),
            beat_position=self._beat_position,
            beat_phase=phase,
            total_beat_count=total,
            bar_phase=(self._beat_position % 4.0) / 4.0,
            phrase_phase=(self._beat_position % 32.0) / 32.0,
            beats_crossed=beats_crossed,
            accent_trigger=accent,
            firework_trigger=firework,
            next_accent_in_beats=max(0, self._next_accent_beat - total),
            next_firework_in_beats=max(0, self._next_firework_beat - total),
        )


__all__ = [
    "BeatClock",
    "FALLBACK_TEMPO_BPM",
    "MAX_BEATS_PER_UPDATE",
    "MAX_INTERVAL_HISTORY",
    "MAX_TEMPO_BPM",
    "MIN_TEMPO_BPM",
    "MusicalMotionState",
]
