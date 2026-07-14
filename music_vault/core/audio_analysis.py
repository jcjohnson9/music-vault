from __future__ import annotations

import math
import struct
import threading
import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import islice
from statistics import median


MAX_PCM_FRAMES = 32_768
MAX_FFT_SIZE = 4_096
MIN_FFT_SIZE = 32
MAX_SAMPLE_RATE = 768_000.0
DEFAULT_SILENCE_THRESHOLD = 0.004

_BAND_RANGES = {
    "bass": (20.0, 250.0),
    "low_mid": (250.0, 500.0),
    "mid": (500.0, 2_000.0),
    "high": (2_000.0, 20_000.0),
}

_PCM_FORMATS = {
    "u8": (1, "u8"),
    "uint8": (1, "u8"),
    "unsigned8": (1, "u8"),
    "s16": (2, "s16"),
    "int16": (2, "s16"),
    "signed16": (2, "s16"),
    "s32": (4, "s32"),
    "int32": (4, "s32"),
    "signed32": (4, "s32"),
    "f32": (4, "f32"),
    "float": (4, "f32"),
    "float32": (4, "f32"),
}


def _unit_value(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _audio_sample(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(-1.0, min(1.0, number))


def _sample_rate(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        rate = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(rate) or rate <= 0.0 or rate > MAX_SAMPLE_RATE:
        return 0.0
    return rate


def _timestamp(value: object | None) -> float:
    if value is None:
        return time.monotonic()
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return time.monotonic()
    if not math.isfinite(result):
        return time.monotonic()
    return result


def _format_key(sample_format: object) -> str:
    if isinstance(sample_format, int) and not isinstance(sample_format, bool):
        return {1: "u8", 2: "s16", 3: "s32", 4: "f32"}.get(
            sample_format,
            "",
        )
    name = getattr(sample_format, "name", sample_format)
    key = str(name).strip().lower().rsplit(".", 1)[-1]
    return "".join(character for character in key if character.isalnum())


@dataclass(frozen=True, slots=True)
class AudioFeatures:
    """Immutable, normalized audio-analysis snapshot.

    Level fields and ``beat_strength`` are always finite values from 0 through
    1. ``sample_rate`` is 0 when an input rate is invalid or unavailable.
    """

    rms: float = 0.0
    peak: float = 0.0
    bass: float = 0.0
    low_mid: float = 0.0
    mid: float = 0.0
    high: float = 0.0
    beat: bool = False
    beat_strength: float = 0.0
    is_silent: bool = True
    sample_rate: float = 0.0
    sample_count: int = 0
    timestamp: float = 0.0

    @classmethod
    def silence(
        cls,
        *,
        sample_rate: object = 0.0,
        sample_count: int = 0,
        timestamp: object = 0.0,
    ) -> AudioFeatures:
        return cls(
            sample_rate=_sample_rate(sample_rate),
            sample_count=max(0, int(sample_count)),
            timestamp=_timestamp(timestamp),
        )

    @property
    def silent(self) -> bool:
        return self.is_silent


@dataclass(frozen=True, slots=True)
class AudioBuffer:
    """A copied PCM buffer safe to hand from a producer to an analyzer."""

    data: bytes
    sample_format: object
    channel_count: int
    sample_rate: float
    timestamp: float

    @classmethod
    def from_pcm(
        cls,
        data: object,
        *,
        sample_format: object,
        channel_count: object,
        sample_rate: object,
        timestamp: object | None = None,
    ) -> AudioBuffer:
        try:
            channels = int(channel_count)
        except (TypeError, ValueError, OverflowError):
            channels = 0
        format_info = _PCM_FORMATS.get(_format_key(sample_format))
        try:
            raw = memoryview(data)  # type: ignore[arg-type]
            if raw.ndim != 1 or raw.format != "B" or not raw.c_contiguous:
                raw = memoryview(raw.tobytes())
            if format_info is None or not 1 <= channels <= 64:
                copied = b""
            else:
                frame_width = format_info[0] * channels
                complete_bytes = len(raw) // frame_width * frame_width
                retained_bytes = min(
                    complete_bytes,
                    MAX_PCM_FRAMES * frame_width,
                )
                copied = raw[
                    complete_bytes - retained_bytes : complete_bytes
                ].tobytes()
        except (TypeError, ValueError, BufferError):
            copied = b""
        return cls(
            data=copied,
            sample_format=sample_format,
            channel_count=channels,
            sample_rate=_sample_rate(sample_rate),
            timestamp=_timestamp(timestamp),
        )


def normalize_pcm_to_mono(
    data: object,
    *,
    sample_format: object,
    channel_count: object,
    max_frames: int = MAX_PCM_FRAMES,
) -> tuple[float, ...]:
    """Decode little-endian interleaved PCM to bounded normalized mono.

    Supported formats are unsigned 8-bit, signed 16/32-bit, and 32-bit float.
    Incomplete trailing frames are ignored. Unsupported or malformed input
    returns an empty tuple instead of raising.
    """

    format_info = _PCM_FORMATS.get(_format_key(sample_format))
    if format_info is None:
        return ()
    try:
        channels = int(channel_count)
        frame_limit = int(max_frames)
    except (TypeError, ValueError, OverflowError):
        return ()
    if channels < 1 or channels > 64 or frame_limit < 1:
        return ()

    try:
        raw = memoryview(data)  # type: ignore[arg-type]
        if raw.ndim != 1 or raw.format != "B" or not raw.c_contiguous:
            raw = memoryview(raw.tobytes())
    except (TypeError, ValueError, BufferError):
        return ()

    sample_width, canonical_format = format_info
    frame_width = sample_width * channels
    complete_frames = len(raw) // frame_width
    if complete_frames < 1:
        return ()
    frame_count = min(complete_frames, frame_limit)
    start = (complete_frames - frame_count) * frame_width

    def decode(offset: int) -> float:
        if canonical_format == "u8":
            return (int(raw[offset]) - 128) / 128.0
        if canonical_format == "s16":
            return struct.unpack_from("<h", raw, offset)[0] / 32_768.0
        if canonical_format == "s32":
            return struct.unpack_from("<i", raw, offset)[0] / 2_147_483_648.0
        return _audio_sample(struct.unpack_from("<f", raw, offset)[0])

    mono: list[float] = []
    try:
        for frame_index in range(frame_count):
            frame_offset = start + frame_index * frame_width
            total = 0.0
            for channel in range(channels):
                total += decode(frame_offset + channel * sample_width)
            mono.append(_audio_sample(total / channels))
    except (IndexError, struct.error, ValueError, BufferError):
        return ()
    return tuple(mono)


def _bounded_samples(
    samples: Iterable[object],
    limit: int = MAX_PCM_FRAMES,
) -> tuple[float, ...]:
    if isinstance(samples, Sequence):
        try:
            start = max(0, len(samples) - limit)
            values = (samples[index] for index in range(start, len(samples)))
        except (TypeError, ValueError, OverflowError):
            return ()
    else:
        try:
            values = islice(iter(samples), limit)
        except TypeError:
            return ()
    return tuple(_audio_sample(value) for value in values)


def _downsample_for_fft(
    samples: Sequence[float],
    sample_rate: float,
    max_fft_size: int,
) -> tuple[tuple[float, ...], float]:
    if len(samples) <= max_fft_size:
        return tuple(samples), sample_rate
    # Use the newest bounded window at the original rate.  Naive block
    # averaging followed by a reduced effective rate aliases ordinary treble
    # into lower bands; a fixed recent window preserves the frequencies the
    # visualizer is meant to distinguish without increasing FFT work.
    return tuple(samples[-max_fft_size:]), sample_rate


def _power_of_two_floor(value: int) -> int:
    if value < 1:
        return 0
    return 1 << (value.bit_length() - 1)


def _radix2_fft(values: Sequence[complex]) -> list[complex]:
    """Iterative radix-2 FFT used to avoid a heavyweight numeric dependency."""

    size = len(values)
    if size < 1 or size & (size - 1):
        raise ValueError("FFT input length must be a non-zero power of two")
    output = [complex(value) for value in values]

    destination = 0
    for source in range(1, size):
        bit = size >> 1
        while destination & bit:
            destination ^= bit
            bit >>= 1
        destination ^= bit
        if source < destination:
            output[source], output[destination] = output[destination], output[source]

    span = 2
    while span <= size:
        angle = -2.0 * math.pi / span
        step = complex(math.cos(angle), math.sin(angle))
        half = span // 2
        for start in range(0, size, span):
            rotation = 1.0 + 0.0j
            for offset in range(half):
                even = output[start + offset]
                odd = output[start + offset + half] * rotation
                output[start + offset] = even + odd
                output[start + offset + half] = even - odd
                rotation *= step
        span *= 2
    return output


def _spectral_bands(
    samples: Sequence[float],
    sample_rate: float,
    rms: float,
    max_fft_size: int,
) -> dict[str, float]:
    empty = {name: 0.0 for name in _BAND_RANGES}
    if sample_rate <= 0.0 or len(samples) < MIN_FFT_SIZE or rms <= 0.0:
        return empty

    reduced, effective_rate = _downsample_for_fft(
        samples,
        sample_rate,
        max(MIN_FFT_SIZE, max_fft_size),
    )
    fft_size = _power_of_two_floor(min(len(reduced), max_fft_size))
    if fft_size < MIN_FFT_SIZE or effective_rate <= 0.0:
        return empty
    segment = reduced[-fft_size:]
    mean = sum(segment) / fft_size
    windowed = [
        (sample - mean) * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / (fft_size - 1)))
        for index, sample in enumerate(segment)
    ]
    spectrum = _radix2_fft(windowed)
    energies = [abs(spectrum[index]) ** 2 for index in range(1, fft_size // 2 + 1)]
    total_energy = sum(energies)
    if not math.isfinite(total_energy) or total_energy <= 1e-20:
        return empty

    nyquist = effective_rate / 2.0
    band_energy = {name: 0.0 for name in _BAND_RANGES}
    for bin_index, energy in enumerate(energies, start=1):
        frequency = bin_index * effective_rate / fft_size
        for name, (lower, configured_upper) in _BAND_RANGES.items():
            upper = min(configured_upper, nyquist)
            if lower <= frequency < upper or (
                name == "high"
                and lower <= frequency
                and frequency == upper
                and upper == nyquist
            ):
                band_energy[name] += energy
                break

    return {
        name: _unit_value(rms * math.sqrt(energy / total_energy))
        for name, energy in band_energy.items()
    }


def analyze_audio_frame(
    samples: Iterable[object],
    *,
    sample_rate: object,
    timestamp: object | None = None,
    silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
    max_fft_size: int = MAX_FFT_SIZE,
) -> AudioFeatures:
    """Analyze normalized mono samples without retaining mutable state."""

    bounded = _bounded_samples(samples)
    rate = _sample_rate(sample_rate)
    observed_at = _timestamp(timestamp)
    if not bounded:
        return AudioFeatures.silence(
            sample_rate=rate,
            sample_count=0,
            timestamp=observed_at,
        )
    try:
        fft_limit = max(MIN_FFT_SIZE, min(MAX_FFT_SIZE, int(max_fft_size)))
        threshold = max(0.0, min(1.0, float(silence_threshold)))
    except (TypeError, ValueError, OverflowError):
        fft_limit = MAX_FFT_SIZE
        threshold = DEFAULT_SILENCE_THRESHOLD

    mean_square = sum(sample * sample for sample in bounded) / len(bounded)
    rms = _unit_value(math.sqrt(max(0.0, mean_square)))
    peak = _unit_value(max(abs(sample) for sample in bounded))
    bands = _spectral_bands(bounded, rate, rms, fft_limit)
    return AudioFeatures(
        rms=rms,
        peak=peak,
        bass=bands["bass"],
        low_mid=bands["low_mid"],
        mid=bands["mid"],
        high=bands["high"],
        is_silent=rms <= threshold,
        sample_rate=rate,
        sample_count=len(bounded),
        timestamp=observed_at,
    )


def analyze_pcm(
    data: object,
    *,
    sample_format: object,
    channel_count: object,
    sample_rate: object,
    timestamp: object | None = None,
) -> AudioFeatures:
    samples = normalize_pcm_to_mono(
        data,
        sample_format=sample_format,
        channel_count=channel_count,
    )
    return analyze_audio_frame(
        samples,
        sample_rate=sample_rate,
        timestamp=timestamp,
    )


class AttackReleaseSmoother:
    """Exponential attack/release smoothing for a normalized scalar."""

    __slots__ = ("attack_seconds", "release_seconds", "_value")

    def __init__(
        self,
        *,
        attack_seconds: float = 0.035,
        release_seconds: float = 0.28,
        initial: float = 0.0,
    ) -> None:
        self.attack_seconds = max(0.0, float(attack_seconds))
        self.release_seconds = max(0.0, float(release_seconds))
        self._value = _unit_value(initial)

    @property
    def value(self) -> float:
        return self._value

    def reset(self, value: object = 0.0) -> float:
        self._value = _unit_value(value)
        return self._value

    def update(self, target: object, elapsed_seconds: object) -> float:
        normalized = _unit_value(target)
        try:
            elapsed = float(elapsed_seconds)
        except (TypeError, ValueError, OverflowError):
            elapsed = 1.0 / 60.0
        if not math.isfinite(elapsed) or elapsed <= 0.0:
            elapsed = 1.0 / 60.0
        time_constant = (
            self.attack_seconds if normalized > self._value else self.release_seconds
        )
        alpha = 1.0 if time_constant <= 0.0 else 1.0 - math.exp(-elapsed / time_constant)
        self._value = _unit_value(self._value + alpha * (normalized - self._value))
        return self._value


class AdaptiveBeatDetector:
    """Adaptive low-frequency onset detector with a refractory interval."""

    def __init__(
        self,
        *,
        history_size: int = 43,
        warmup_samples: int = 4,
        threshold_ratio: float = 1.45,
        onset_ratio: float = 1.12,
        minimum_energy: float = 0.025,
        minimum_interval_seconds: float = 0.22,
    ) -> None:
        self.history_size = max(4, int(history_size))
        self.warmup_samples = max(1, min(self.history_size, int(warmup_samples)))
        self.threshold_ratio = max(1.0, float(threshold_ratio))
        self.onset_ratio = max(1.0, float(onset_ratio))
        self.minimum_energy = _unit_value(minimum_energy)
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self._history: deque[float] = deque(maxlen=self.history_size)
        self._previous_energy = 0.0
        self._last_beat_at: float | None = None
        self._last_timestamp: float | None = None

    def reset(self) -> None:
        self._history.clear()
        self._previous_energy = 0.0
        self._last_beat_at = None
        self._last_timestamp = None

    def update(
        self,
        energy: object,
        *,
        timestamp: object | None = None,
        is_silent: bool = False,
    ) -> tuple[bool, float]:
        level = _unit_value(energy)
        observed_at = _timestamp(timestamp)
        if self._last_timestamp is not None and observed_at < self._last_timestamp:
            observed_at = self._last_timestamp
        self._last_timestamp = observed_at

        baseline = median(self._history) if self._history else 0.0
        threshold = max(self.minimum_energy, baseline * self.threshold_ratio)
        warmed_up = len(self._history) >= self.warmup_samples
        onset = level >= max(
            threshold,
            self._previous_energy * self.onset_ratio,
            self._previous_energy + 0.005,
        )
        interval_ready = (
            self._last_beat_at is None
            or observed_at - self._last_beat_at >= self.minimum_interval_seconds
        )
        beat = bool(not is_silent and warmed_up and onset and interval_ready)
        strength = 0.0
        if beat:
            self._last_beat_at = observed_at
            strength = _unit_value(
                (level - threshold) / max(1e-9, 1.0 - threshold)
            )

        self._history.append(0.0 if is_silent else level)
        self._previous_energy = 0.0 if is_silent else level
        return beat, strength


class LatestAudioBufferSlot:
    """Thread-safe one-item slot where a newer buffer replaces stale work."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: AudioBuffer | None = None
        self._submitted_count = 0
        self._dropped_count = 0

    def submit(self, buffer: AudioBuffer) -> bool:
        if not isinstance(buffer, AudioBuffer):
            raise TypeError("buffer must be an AudioBuffer")
        with self._lock:
            self._submitted_count += 1
            if self._pending is not None:
                if buffer.timestamp <= self._pending.timestamp:
                    self._dropped_count += 1
                    return False
                self._dropped_count += 1
            self._pending = buffer
            return True

    def take_latest(self) -> AudioBuffer | None:
        with self._lock:
            pending = self._pending
            self._pending = None
            return pending

    def clear(self) -> None:
        with self._lock:
            self._pending = None

    def reset(self) -> None:
        with self._lock:
            self._pending = None
            self._submitted_count = 0
            self._dropped_count = 0

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    @property
    def submitted_count(self) -> int:
        with self._lock:
            return self._submitted_count

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count


class AudioAnalyzer:
    """Stateful smoothing/beat layer over a drop-stale latest-buffer slot."""

    _LEVEL_NAMES = ("rms", "peak", "bass", "low_mid", "mid", "high")

    def __init__(
        self,
        *,
        attack_seconds: float = 0.035,
        release_seconds: float = 0.28,
        silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
    ) -> None:
        self.slot = LatestAudioBufferSlot()
        self.silence_threshold = _unit_value(silence_threshold)
        self._smoothers = {
            name: AttackReleaseSmoother(
                attack_seconds=attack_seconds,
                release_seconds=release_seconds,
            )
            for name in self._LEVEL_NAMES
        }
        self._beat_detector = AdaptiveBeatDetector()
        self._last_timestamp: float | None = None
        self._latest = AudioFeatures.silence(timestamp=0.0)

    @property
    def latest_features(self) -> AudioFeatures:
        return self._latest

    @property
    def latest(self) -> AudioFeatures:
        return self._latest

    @property
    def dropped_buffers(self) -> int:
        return self.slot.dropped_count

    @property
    def dropped_buffer_count(self) -> int:
        return self.slot.dropped_count

    def submit_pcm(
        self,
        data: object,
        *,
        sample_format: object,
        channel_count: object,
        sample_rate: object,
        timestamp: object | None = None,
    ) -> bool:
        return self.slot.submit(
            AudioBuffer.from_pcm(
                data,
                sample_format=sample_format,
                channel_count=channel_count,
                sample_rate=sample_rate,
                timestamp=timestamp,
            )
        )

    def process_latest(self) -> AudioFeatures | None:
        buffer = self.slot.take_latest()
        if buffer is None:
            return None
        return self._process_buffer(buffer)

    def process_pcm(
        self,
        data: object,
        sample_format: object,
        channels: object,
        sample_rate: object,
        timestamp_ms: object | None = None,
    ) -> AudioFeatures:
        """Synchronously analyze one PCM buffer using millisecond timestamps."""

        observed_at = None
        if timestamp_ms is not None:
            try:
                observed_at = float(timestamp_ms) / 1_000.0
            except (TypeError, ValueError, OverflowError):
                observed_at = None
        return self._process_buffer(
            AudioBuffer.from_pcm(
                data,
                sample_format=sample_format,
                channel_count=channels,
                sample_rate=sample_rate,
                timestamp=observed_at,
            )
        )

    def submit_latest(
        self,
        data: object,
        sample_format: object,
        channels: object,
        sample_rate: object,
        timestamp_ms: object | None = None,
    ) -> bool:
        """Submit to the single pending slot, replacing older unprocessed work."""

        observed_at = None
        if timestamp_ms is not None:
            try:
                observed_at = float(timestamp_ms) / 1_000.0
            except (TypeError, ValueError, OverflowError):
                observed_at = None
        return self.submit_pcm(
            data,
            sample_format=sample_format,
            channel_count=channels,
            sample_rate=sample_rate,
            timestamp=observed_at,
        )

    def _process_buffer(self, buffer: AudioBuffer) -> AudioFeatures:
        samples = normalize_pcm_to_mono(
            buffer.data,
            sample_format=buffer.sample_format,
            channel_count=buffer.channel_count,
        )
        raw = analyze_audio_frame(
            samples,
            sample_rate=buffer.sample_rate,
            timestamp=buffer.timestamp,
            silence_threshold=self.silence_threshold,
        )
        if self._last_timestamp is None or raw.timestamp <= self._last_timestamp:
            elapsed = 1.0 / 60.0
        else:
            elapsed = min(0.5, raw.timestamp - self._last_timestamp)
        self._last_timestamp = raw.timestamp

        levels = {
            name: self._smoothers[name].update(getattr(raw, name), elapsed)
            for name in self._LEVEL_NAMES
        }
        beat_energy = max(raw.bass, raw.low_mid * 0.8, raw.rms * 0.5)
        beat, beat_strength = self._beat_detector.update(
            beat_energy,
            timestamp=raw.timestamp,
            is_silent=raw.is_silent,
        )
        self._latest = AudioFeatures(
            **levels,
            beat=beat,
            beat_strength=beat_strength,
            is_silent=raw.is_silent,
            sample_rate=raw.sample_rate,
            sample_count=raw.sample_count,
            timestamp=raw.timestamp,
        )
        return self._latest

    def reset(self) -> AudioFeatures:
        self.slot.reset()
        for smoother in self._smoothers.values():
            smoother.reset()
        self._beat_detector.reset()
        self._last_timestamp = None
        self._latest = AudioFeatures.silence(timestamp=0.0)
        return self._latest


__all__ = [
    "AdaptiveBeatDetector",
    "AttackReleaseSmoother",
    "AudioAnalyzer",
    "AudioBuffer",
    "AudioFeatures",
    "DEFAULT_SILENCE_THRESHOLD",
    "LatestAudioBufferSlot",
    "MAX_FFT_SIZE",
    "MAX_PCM_FRAMES",
    "analyze_audio_frame",
    "analyze_pcm",
    "normalize_pcm_to_mono",
]
