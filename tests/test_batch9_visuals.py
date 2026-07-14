from __future__ import annotations

import math
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest

from music_vault.ui.party_palette import (
    ArtworkPalette,
    DEFAULT_PARTY_PALETTE,
    PaletteExtractor,
    contrast_ratio,
    ensure_contrast,
    interpolate_color,
    interpolate_palette,
)
from music_vault.ui.party_visuals import (
    MAX_BEAT,
    MAX_BRIGHTNESS,
    MAX_DELTA_SECONDS,
    MAX_PARTICLES,
    PRESETS,
    AdaptiveQualityController,
    PartyCanvas,
    PartyVisualEngine,
    QUALITY_BUDGETS,
    QUALITY_LEVELS,
)


def _artwork(path: Path) -> QImage:
    image = QImage(48, 32, QImage.Format.Format_ARGB32)
    image.fill(QColor("#db315f"))
    for y in range(32):
        for x in range(24, 48):
            image.setPixelColor(x, y, QColor("#25b7cf"))
    assert image.save(str(path), "PNG")
    return image


def test_fallback_palette_is_stable_bounded_and_high_contrast() -> None:
    assert ArtworkPalette.fallback() == DEFAULT_PARTY_PALETTE
    assert contrast_ratio(
        DEFAULT_PARTY_PALETTE.foreground, DEFAULT_PARTY_PALETTE.background
    ) >= 7.0
    assert set(DEFAULT_PARTY_PALETTE.as_hex()) == {
        "background",
        "surface",
        "primary",
        "secondary",
        "accent",
        "foreground",
    }
    for color in DEFAULT_PARTY_PALETTE.as_hex().values():
        assert color.startswith("#") and len(color) == 7


def test_color_interpolation_clamps_and_contrast_repair_is_safe() -> None:
    assert interpolate_color((0, 10, 20), (100, 110, 120), -5) == (0, 10, 20)
    assert interpolate_color((0, 10, 20), (100, 110, 120), 5) == (100, 110, 120)
    assert interpolate_color((0, 10, 20), (100, 110, 120), 0.5) == (50, 60, 70)
    repaired = ensure_contrast((25, 25, 25), (20, 20, 20), minimum=4.5)
    assert contrast_ratio(repaired, (20, 20, 20)) >= 4.5

    other = ArtworkPalette(
        background=(20, 30, 40),
        surface=(30, 40, 50),
        primary=(40, 50, 60),
        secondary=(50, 60, 70),
        accent=(60, 70, 80),
        foreground=(240, 245, 250),
    )
    assert interpolate_palette(DEFAULT_PARTY_PALETTE, other, 0) == DEFAULT_PARTY_PALETTE
    assert interpolate_palette(DEFAULT_PARTY_PALETTE, other, 1) == other


def test_palette_extraction_is_deterministic_cached_and_contrast_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "synthetic-art.png"
    image = _artwork(path)
    extractor = PaletteExtractor(max_cache_entries=2)
    decode_count = 0
    original_reader = extractor._read_path_sample

    def recording_reader(resolved: Path):
        nonlocal decode_count
        decode_count += 1
        return original_reader(resolved)

    monkeypatch.setattr(extractor, "_read_path_sample", recording_reader)

    first = extractor.extract(path)
    second = extractor.extract(path)
    from_image = extractor.extract(image)

    assert first is second
    assert decode_count == 1
    assert first == from_image
    assert first != DEFAULT_PARTY_PALETTE
    assert extractor.cache_size == 2
    assert contrast_ratio(first.foreground, first.background) >= 7.0
    for role in (first.primary, first.secondary, first.accent):
        assert contrast_ratio(role, first.background) >= 3.0

    changed = QImage(52, 36, QImage.Format.Format_ARGB32)
    changed.fill(QColor("#2a63c7"))
    assert changed.save(str(path), "PNG")
    assert extractor.extract(path) != first
    assert decode_count == 2


def test_palette_extractor_fails_closed_and_bounds_cache(tmp_path: Path) -> None:
    extractor = PaletteExtractor(max_cache_entries=2)
    assert extractor.extract(None) == DEFAULT_PARTY_PALETTE
    assert extractor.extract(tmp_path / "missing.png") == DEFAULT_PARTY_PALETTE
    assert extractor.extract(b"not an image") == DEFAULT_PARTY_PALETTE
    assert extractor.cache_size == 2
    extractor.clear()
    assert extractor.cache_size == 0


def test_palette_transparent_image_uses_fallback() -> None:
    image = QImage(10, 10, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    assert PaletteExtractor().extract(image) == DEFAULT_PARTY_PALETTE


def test_presets_and_quality_budgets_are_explicit_and_bounded() -> None:
    assert PRESETS == ("pulse", "starfield", "aurora")
    assert QUALITY_LEVELS == ("low", "medium", "high")
    assert set(QUALITY_BUDGETS) == set(QUALITY_LEVELS)
    for quality in QUALITY_LEVELS:
        engine = PartyVisualEngine(seed=7, quality=quality)
        for preset in PRESETS:
            engine.set_preset(preset)
            frame = engine.update(0.0)
            assert len(frame.particles) == engine.particle_budget(preset)
            assert 0 < len(frame.particles) <= MAX_PARTICLES
            assert 2 <= len(frame.aurora_offsets) <= 7
        assert 1 <= engine.target_fps <= 60


def test_seeded_engine_is_repeatable_and_resettable() -> None:
    features = {"energy": 0.61, "bass": 0.72, "beat": 0.44, "brightness": 0.58}
    first = PartyVisualEngine(seed=912, preset="starfield", quality="medium")
    second = PartyVisualEngine(seed=912, preset="starfield", quality="medium")

    first_frames = [first.update(step, features) for step in (0.016, 0.021, 0.033)]
    second_frames = [second.update(step, features) for step in (0.016, 0.021, 0.033)]
    assert first_frames == second_frames

    different = PartyVisualEngine(seed=913, preset="starfield", quality="medium")
    assert different.update(0.016, features).particles != first_frames[0].particles

    first.reset()
    assert first.update(0.016, features) == first_frames[0]


def test_engine_clamps_delta_brightness_beat_and_bad_feature_values() -> None:
    engine = PartyVisualEngine(seed=3)
    frame = engine.update(
        50.0,
        {
            "energy": float("inf"),
            "bass": -4,
            "beat": 8,
            "brightness": 5,
        },
    )
    assert frame.delta_seconds == MAX_DELTA_SECONDS
    assert frame.elapsed_seconds == MAX_DELTA_SECONDS
    assert frame.energy == 0.18
    assert frame.bass == 0.0
    assert frame.beat == MAX_BEAT
    assert frame.brightness == MAX_BRIGHTNESS
    assert 0.0 <= frame.pulse <= 1.0
    assert all(0.0 <= particle.x <= 1.0 for particle in frame.particles)
    assert all(0.0 <= particle.y <= 1.0 for particle in frame.particles)


class _DuckFeatures:
    rms = 0.42
    bass = 0.64
    beat_strength = 0.31
    high = 0.53


def test_engine_accepts_duck_typed_audio_features() -> None:
    frame = PartyVisualEngine(seed=10).update(0.02, _DuckFeatures())
    assert frame.energy == 0.42
    assert frame.bass == 0.64
    assert frame.beat == 0.31
    assert frame.brightness == 0.53


def test_starfield_motion_responds_to_energy_and_beat_with_bounded_drive() -> None:
    baseline = PartyVisualEngine(seed=27, preset="starfield", quality="low")
    energetic = PartyVisualEngine(seed=27, preset="starfield", quality="low")
    initial_y = baseline.update(0.0).particles[0].y

    calm = baseline.update(0.05, {"energy": 0.0, "beat": 0.0})
    driven = energetic.update(0.05, {"energy": 1.0, "beat": 1.0})
    calm_travel = (calm.particles[0].y - initial_y) % 1.0
    driven_travel = (driven.particles[0].y - initial_y) % 1.0

    assert driven_travel > calm_travel > 0.0
    assert driven.beat == MAX_BEAT
    assert all(0.0 <= particle.y <= 1.0 for particle in driven.particles)


def test_aurora_offsets_and_frame_contract_follow_frequency_bands() -> None:
    bass_engine = PartyVisualEngine(seed=9, preset="aurora", quality="medium")
    high_engine = PartyVisualEngine(seed=9, preset="aurora", quality="medium")
    bass_frame = bass_engine.update(
        0.05,
        {"energy": 0.4, "bass": 0.9, "low_mid": 0.1, "mid": 0.1, "high": 0.1},
    )
    high_frame = high_engine.update(
        0.05,
        {"energy": 0.4, "bass": 0.1, "low_mid": 0.1, "mid": 0.1, "high": 0.9},
    )

    assert bass_frame.bass == 0.9
    assert high_frame.high == 0.9
    assert bass_frame.aurora_offsets != high_frame.aurora_offsets
    assert all(-1.0 <= offset <= 1.0 for offset in high_frame.aurora_offsets)


def test_preset_transition_and_reduced_motion_are_deterministic() -> None:
    engine = PartyVisualEngine(seed=5, preset="pulse", transition_seconds=0.2)
    engine.set_preset("starfield")
    halfway = engine.update(0.1)
    assert halfway.previous_preset == "pulse"
    assert halfway.transition_progress == pytest.approx(0.5)
    complete = engine.update(0.1)
    assert complete.previous_preset is None
    assert complete.transition_progress == 1.0

    normal_budget = engine.particle_budget()
    engine.set_reduced_motion(True)
    reduced_budget = engine.particle_budget()
    assert reduced_budget < normal_budget
    assert engine.target_fps <= 30
    engine.set_preset("aurora")
    reduced = engine.update(0.1, {"energy": 1, "beat": 1})
    assert reduced.previous_preset is None
    assert reduced.pulse <= 0.58
    assert reduced.reduced_motion is True


@pytest.mark.parametrize("bad_value", ["unknown", "", "Pulse", None])
def test_invalid_preset_and_quality_fail_fast(bad_value: object) -> None:
    engine = PartyVisualEngine()
    with pytest.raises(ValueError):
        engine.set_preset(bad_value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        engine.set_quality(bad_value)  # type: ignore[arg-type]


def test_adaptive_quality_uses_hysteresis_and_reports_metrics() -> None:
    controller = AdaptiveQualityController(
        "high",
        evaluation_window=3,
        metrics_window=12,
        downgrade_frame_ms=20,
        upgrade_frame_ms=12,
        bad_windows_required=1,
        good_windows_required=1,
    )
    assert [controller.record_frame(0.040) for _ in range(3)][-1] == "medium"
    assert [controller.record_frame(0.040) for _ in range(3)][-1] == "low"
    assert [controller.record_frame(0.008) for _ in range(3)][-1] == "medium"
    assert [controller.record_frame(0.008) for _ in range(3)][-1] == "high"
    assert controller.record_frame(float("nan")) is None

    metrics = controller.performance_metrics()
    assert metrics["quality"] == "high"
    assert metrics["samples"] == 12
    assert metrics["quality_changes"] == 4
    assert math.isclose(float(metrics["slow_frame_ratio"]), 0.5)
    assert float(metrics["estimated_fps"]) > 0


def test_low_quality_requires_real_sixty_fps_headroom_before_promotion() -> None:
    controller = AdaptiveQualityController(
        "low",
        evaluation_window=3,
        upgrade_frame_ms=14.5,
        good_windows_required=1,
    )
    assert [controller.record_frame(0.020) for _ in range(3)][-1] is None
    assert controller.quality == "low"
    assert [controller.record_frame(0.008) for _ in range(3)][-1] == "medium"


@pytest.mark.parametrize("fixed_fps", ("30", "60"))
def test_auto_quality_adapts_particles_while_fixed_fps_remains_honored(
    fixed_fps: str,
    qapp,
) -> None:
    class AdaptiveSpy:
        def __init__(self) -> None:
            self.samples: list[float] = []

        def record_frame(self, seconds: float) -> str:
            self.samples.append(seconds)
            return "low"

    canvas = PartyCanvas(seed=19, quality="auto")
    canvas.resize(640, 360)
    canvas.set_frame_rate(fixed_fps)
    adaptive = AdaptiveSpy()
    canvas._adaptive = adaptive

    target = QImage(640, 360, QImage.Format.Format_ARGB32)
    target.fill(Qt.GlobalColor.transparent)
    canvas.render(target)
    qapp.processEvents()

    assert adaptive.samples and adaptive.samples[-1] >= 0.0
    assert canvas._engine.quality == "low"
    assert canvas._target_fps() == int(fixed_fps)
    assert canvas._timer.interval() == round(1_000 / int(fixed_fps))
    canvas.deleteLater()


def test_party_canvas_contract_and_bounded_rendering(qapp) -> None:
    canvas = PartyCanvas(seed=41, preset="pulse", quality="medium")
    canvas.resize(640, 360)
    assert canvas.rendering_active is False
    assert not hasattr(canvas, "_player")

    canvas.set_features({"energy": 0.7, "beat": 0.6})
    canvas.set_preset("aurora")
    canvas.set_palette(DEFAULT_PARTY_PALETTE)
    canvas.set_reduced_motion(True)
    canvas.set_quality("low")
    canvas.set_track_text("Synthetic title", "Synthetic artist", "Synthetic album")
    canvas.set_playback_state(True, True)
    artwork = QPixmap(80, 80)
    artwork.fill(QColor("#8c55ff"))
    canvas.set_artwork(artwork)

    target = QImage(640, 360, QImage.Format.Format_ARGB32)
    target.fill(Qt.GlobalColor.transparent)
    canvas.render(target)
    assert not target.isNull()

    canvas.start_rendering()
    assert canvas.rendering_active is True
    QTest.qWait(45)
    qapp.processEvents()
    canvas.stop_rendering()
    assert canvas.rendering_active is False

    metrics = canvas.performance_metrics()
    assert metrics["preset"] == "aurora"
    assert metrics["quality"] == "low"
    assert metrics["reduced_motion"] is True
    assert 0 < int(metrics["particle_budget"]) <= MAX_PARTICLES
    assert int(metrics["target_fps"]) <= 30
    assert int(metrics["samples"]) == 0

    canvas.set_artwork(None)
    canvas.set_playback_state(False, False)
    canvas.render(target)
    canvas.deleteLater()


def test_ambient_fallback_tracks_playback_state_without_claiming_reactivity(qapp) -> None:
    canvas = PartyCanvas(seed=7, preset="pulse", quality="low")
    canvas.set_audio_reactivity_available(False)

    canvas.set_playback_state(True, True)
    playing_energy = canvas._frame.energy
    canvas.set_playback_state(False, True)
    paused_energy = canvas._frame.energy
    canvas.set_playback_state(False, False)
    idle_energy = canvas._frame.energy

    assert playing_energy > paused_energy > idle_energy >= 0.0
    assert canvas.performance_metrics()["audio_reactivity_available"] is False
    canvas.deleteLater()
