from __future__ import annotations

import math
import struct
from dataclasses import FrozenInstanceError

import pytest

import music_vault.core.audio_analysis as analysis
from music_vault.core.audio_analysis import (
    AdaptiveBeatDetector,
    AttackReleaseSmoother,
    AudioAnalyzer,
    AudioBuffer,
    AudioFeatures,
    LatestAudioBufferSlot,
    MAX_FFT_SIZE,
    MAX_PCM_FRAMES,
    analyze_audio_frame,
    normalize_pcm_to_mono,
)


def _sine(frequency: float, sample_rate: int, count: int, amplitude: float = 0.8):
    return tuple(
        amplitude * math.sin(2.0 * math.pi * frequency * index / sample_rate)
        for index in range(count)
    )


def test_audio_features_snapshot_is_immutable():
    features = AudioFeatures(rms=0.5, peak=0.8, is_silent=False)
    with pytest.raises(FrozenInstanceError):
        features.rms = 0.9  # type: ignore[misc]
    assert features.silent is False


@pytest.mark.parametrize(
    ("sample_format", "payload", "expected"),
    [
        ("u8", bytes((0, 128, 255)), (-1.0, 0.0, 127.0 / 128.0)),
        (
            "s16",
            struct.pack("<hhh", -32_768, 0, 32_767),
            (-1.0, 0.0, 32_767.0 / 32_768.0),
        ),
        (
            "s32",
            struct.pack("<iii", -2_147_483_648, 0, 2_147_483_647),
            (-1.0, 0.0, 2_147_483_647.0 / 2_147_483_648.0),
        ),
        ("f32", struct.pack("<fff", -1.0, 0.25, 1.0), (-1.0, 0.25, 1.0)),
    ],
)
def test_pcm_formats_normalize_to_unit_mono(sample_format, payload, expected):
    assert normalize_pcm_to_mono(
        payload,
        sample_format=sample_format,
        channel_count=1,
    ) == pytest.approx(expected)


def test_pcm_interleaved_channels_are_averaged_by_frame():
    payload = struct.pack(
        "<hhhhhh",
        32_767,
        -32_768,
        16_384,
        16_384,
        -16_384,
        -16_384,
    )
    mono = normalize_pcm_to_mono(
        payload,
        sample_format="QAudioFormat.SampleFormat.Int16",
        channel_count=2,
    )
    assert mono == pytest.approx((-1.0 / 65_536.0, 0.5, -0.5))


def test_pcm_malformed_input_and_incomplete_frames_fail_safely():
    one_stereo_frame_plus_trailer = struct.pack("<hh", 16_384, -16_384) + b"\xff"
    assert normalize_pcm_to_mono(
        one_stereo_frame_plus_trailer,
        sample_format="s16",
        channel_count=2,
    ) == pytest.approx((0.0,))
    assert normalize_pcm_to_mono(b"\x00", sample_format="s16", channel_count=1) == ()
    assert normalize_pcm_to_mono(b"abc", sample_format="unsupported", channel_count=1) == ()
    assert normalize_pcm_to_mono(object(), sample_format="u8", channel_count=1) == ()
    assert normalize_pcm_to_mono(b"abc", sample_format="u8", channel_count=0) == ()


def test_float_pcm_clamps_nonfinite_and_out_of_range_values():
    payload = struct.pack("<fffff", float("nan"), float("inf"), -2.0, 0.5, 2.0)
    assert normalize_pcm_to_mono(
        payload,
        sample_format="Float32",
        channel_count=1,
    ) == pytest.approx((0.0, 0.0, -1.0, 0.5, 1.0))


def test_qt_numeric_sample_format_ids_are_supported_without_importing_qt():
    assert normalize_pcm_to_mono(
        struct.pack("<h", 16_384),
        sample_format=2,
        channel_count=1,
    ) == pytest.approx((0.5,))
    assert normalize_pcm_to_mono(b"\x80", sample_format=0, channel_count=1) == ()


def test_pcm_normalization_keeps_only_bounded_latest_frames():
    payload = bytes(index % 256 for index in range(MAX_PCM_FRAMES + 7))
    mono = normalize_pcm_to_mono(
        payload,
        sample_format="u8",
        channel_count=1,
    )
    assert len(mono) == MAX_PCM_FRAMES
    assert mono[0] == pytest.approx((payload[7] - 128) / 128.0)


@pytest.mark.parametrize(
    ("frequency", "dominant_name"),
    [
        (128.0, "bass"),
        (384.0, "low_mid"),
        (1_024.0, "mid"),
        (3_072.0, "high"),
    ],
)
def test_hann_fft_reports_the_dominant_frequency_band(frequency, dominant_name):
    features = analyze_audio_frame(
        _sine(frequency, 8_192, 4_096),
        sample_rate=8_192,
        timestamp=1.0,
    )
    levels = {
        "bass": features.bass,
        "low_mid": features.low_mid,
        "mid": features.mid,
        "high": features.high,
    }
    assert features.rms == pytest.approx(0.8 / math.sqrt(2.0), rel=0.02)
    assert features.peak == pytest.approx(0.8, rel=0.01)
    assert features.is_silent is False
    assert levels[dominant_name] > 0.5
    assert levels[dominant_name] > max(
        value for name, value in levels.items() if name != dominant_name
    ) * 10.0
    assert all(0.0 <= value <= 1.0 for value in levels.values())


def test_analysis_uses_a_bounded_latest_window_before_fft(monkeypatch):
    observed_sizes = []
    original_fft = analysis._radix2_fft

    def recording_fft(values):
        observed_sizes.append(len(values))
        return original_fft(values)

    monkeypatch.setattr(analysis, "_radix2_fft", recording_fft)
    samples = _sine(440.0, 48_000, MAX_PCM_FRAMES * 3)
    features = analyze_audio_frame(samples, sample_rate=48_000)
    assert features.sample_count == MAX_PCM_FRAMES
    assert observed_sizes
    assert observed_sizes[0] <= MAX_FFT_SIZE
    assert observed_sizes[0] & (observed_sizes[0] - 1) == 0


def test_long_high_frequency_buffer_does_not_alias_into_lower_bands():
    features = analyze_audio_frame(
        _sine(8_000.0, 48_000, MAX_PCM_FRAMES),
        sample_rate=48_000,
    )

    assert features.high > 0.5
    assert features.high > max(features.bass, features.low_mid, features.mid) * 10.0


@pytest.mark.parametrize("sample_rate", (None, 0, -1, float("nan"), 1_000_000))
def test_invalid_sample_rates_keep_levels_safe_without_spectral_bins(sample_rate):
    features = analyze_audio_frame((0.5, -0.5) * 64, sample_rate=sample_rate)
    assert features.sample_rate == 0.0
    assert features.rms == pytest.approx(0.5)
    assert features.peak == pytest.approx(0.5)
    assert (features.bass, features.low_mid, features.mid, features.high) == (
        0.0,
        0.0,
        0.0,
        0.0,
    )


def test_low_sample_rate_respects_nyquist_and_never_invents_high_bands():
    features = analyze_audio_frame(_sine(5.0, 20, 128), sample_rate=20)
    assert features.sample_rate == 20.0
    assert features.bass == 0.0
    assert features.low_mid == 0.0
    assert features.mid == 0.0
    assert features.high == 0.0


def test_silence_produces_a_finite_zero_snapshot():
    features = analyze_audio_frame((0.0,) * 4_096, sample_rate=48_000, timestamp=4.0)
    assert features == AudioFeatures(
        sample_rate=48_000.0,
        sample_count=4_096,
        timestamp=4.0,
    )


def test_attack_is_faster_than_release_and_values_remain_normalized():
    smoother = AttackReleaseSmoother(
        attack_seconds=0.01,
        release_seconds=0.5,
    )
    attacked = smoother.update(1.0, 0.05)
    released = smoother.update(0.0, 0.05)
    assert attacked > 0.99
    assert released > 0.85
    assert attacked - 0.0 > attacked - released
    assert 0.0 <= smoother.update(float("inf"), -1.0) <= 1.0


def test_adaptive_beat_detection_warms_up_and_rate_limits():
    detector = AdaptiveBeatDetector(minimum_interval_seconds=0.25)
    for index in range(6):
        assert detector.update(0.1, timestamp=index * 0.1) == (False, 0.0)

    beat, strength = detector.update(0.7, timestamp=0.7)
    assert beat is True
    assert 0.0 < strength <= 1.0
    assert detector.update(0.9, timestamp=0.8)[0] is False
    assert detector.update(0.1, timestamp=0.9)[0] is False
    assert detector.update(0.8, timestamp=1.0)[0] is True
    assert detector.update(1.0, timestamp=0.5)[0] is False
    assert detector.update(1.0, timestamp=1.5, is_silent=True) == (False, 0.0)


def test_latest_slot_replaces_pending_work_and_rejects_older_buffers():
    slot = LatestAudioBufferSlot()
    first = AudioBuffer.from_pcm(
        b"first",
        sample_format="u8",
        channel_count=1,
        sample_rate=48_000,
        timestamp=1.0,
    )
    latest = AudioBuffer.from_pcm(
        b"latest",
        sample_format="u8",
        channel_count=1,
        sample_rate=48_000,
        timestamp=2.0,
    )
    stale = AudioBuffer.from_pcm(
        b"stale",
        sample_format="u8",
        channel_count=1,
        sample_rate=48_000,
        timestamp=1.5,
    )
    assert slot.submit(first) is True
    assert slot.submit(latest) is True
    assert slot.submit(stale) is False
    assert slot.submit(latest) is False
    assert slot.submitted_count == 4
    assert slot.dropped_count == 3
    assert slot.take_latest() is latest
    assert slot.take_latest() is None


def test_stateful_analyzer_processes_only_the_latest_pcm_buffer():
    analyzer = AudioAnalyzer(attack_seconds=0.0, release_seconds=0.0)
    quiet = struct.pack("<" + "f" * 128, *((0.01,) * 128))
    loud = struct.pack("<" + "f" * 128, *((0.75,) * 128))
    assert analyzer.submit_pcm(
        quiet,
        sample_format="f32",
        channel_count=1,
        sample_rate=48_000,
        timestamp=1.0,
    )
    assert analyzer.submit_pcm(
        loud,
        sample_format="f32",
        channel_count=1,
        sample_rate=48_000,
        timestamp=2.0,
    )
    features = analyzer.process_latest()
    assert features is not None
    assert features.rms == pytest.approx(0.75)
    assert features.timestamp == 2.0
    assert analyzer.dropped_buffers == 1
    assert analyzer.process_latest() is None
    assert analyzer.latest_features is features


def test_process_pcm_uses_millisecond_timestamps_and_public_aliases():
    analyzer = AudioAnalyzer(attack_seconds=0.0, release_seconds=0.0)
    payload = struct.pack("<hhhh", 16_384, 16_384, 16_384, 16_384)
    features = analyzer.process_pcm(payload, "s16", 1, 48_000, timestamp_ms=2_500)
    assert features.timestamp == 2.5
    assert features.rms == pytest.approx(0.5)
    assert analyzer.latest is features
    assert analyzer.dropped_buffer_count == 0

    assert analyzer.submit_latest(payload, "s16", 1, 48_000, timestamp_ms=3_000)
    assert analyzer.process_latest().timestamp == 3.0


def test_stateful_analyzer_turns_malformed_pcm_into_silence_and_resets():
    analyzer = AudioAnalyzer()
    analyzer.submit_pcm(
        b"\x00",
        sample_format="s32",
        channel_count=2,
        sample_rate=48_000,
        timestamp=3.0,
    )
    features = analyzer.process_latest()
    assert features is not None
    assert features.is_silent is True
    assert features.sample_count == 0
    assert analyzer.reset().is_silent is True
    assert analyzer.slot.has_pending is False
    assert analyzer.dropped_buffer_count == 0
