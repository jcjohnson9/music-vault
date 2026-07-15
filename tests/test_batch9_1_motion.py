from __future__ import annotations

from dataclasses import replace
import math

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from music_vault.core.musical_motion import (
    BeatClock,
    FALLBACK_TEMPO_BPM,
    MAX_INTERVAL_HISTORY,
)
from music_vault.ui.party_visuals import (
    MAX_FIREWORK_BURSTS,
    MAX_FIREWORK_PARTICLES,
    MAX_FIREWORK_PROTECTED_RECTS,
    MAX_ORBS,
    PRESETS,
    PRESET_LABELS,
    FireworksSimulation,
    OrbClusterSimulation,
    PartyCanvas,
    PartyVisualEngine,
    album_transform_for_preset,
    is_safe_firework_position,
)


def _established_clock(seed: int = 11) -> BeatClock:
    clock = BeatClock(seed=seed)
    for timestamp in (0.0, 0.5, 1.0, 1.5, 2.0):
        assert clock.observe_beat(timestamp)
    return clock


def test_six_preset_order_and_friendly_names_are_exact() -> None:
    assert PRESETS == (
        "static",
        "starfield",
        "aurora",
        "orb_cluster",
        "fireworks",
        "pulse",
    )
    assert tuple(PRESET_LABELS[preset] for preset in PRESETS) == (
        "Static",
        "Starfield",
        "Aurora",
        "Orb Cluster",
        "Fireworks",
        "Pulse",
    )


@pytest.mark.parametrize(
    "preset", ("static", "starfield", "aurora", "orb_cluster", "fireworks")
)
def test_album_transform_is_exact_identity_outside_pulse(preset: str) -> None:
    for phase in (0.0, 0.125, 0.5, 0.875, 1.0):
        transform = album_transform_for_preset(preset, phase)
        assert transform.scale == 1.0
        assert transform.translate_x == 0.0
        assert transform.translate_y == 0.0
        assert transform.rotation_degrees == 0.0


def test_pulse_is_smooth_four_beat_only_album_transform() -> None:
    phases = [index / 40 for index in range(41)]
    scales = [album_transform_for_preset("pulse", phase).scale for phase in phases]
    assert scales[0] == pytest.approx(1.0)
    assert scales[-1] == pytest.approx(1.0)
    assert max(scales) == pytest.approx(1.03)
    assert scales[:21] == sorted(scales[:21])
    assert scales[20:] == sorted(scales[20:], reverse=True)
    assert max(abs(right - left) for left, right in zip(scales, scales[1:])) < 0.003
    reduced = [
        album_transform_for_preset("pulse", phase, reduced_motion=True).scale
        for phase in phases
    ]
    assert max(reduced) <= 1.006


def test_entering_and_leaving_pulse_eases_from_the_rendered_album_scale() -> None:
    engine = PartyVisualEngine(
        seed=19,
        preset="static",
        transition_seconds=0.5,
    )
    before = None
    for _ in range(17):
        before = engine.update(0.1)
    assert before is not None
    assert before.album_transform.scale == 1.0

    engine.set_preset("pulse")
    entered = engine.update(0.0)
    assert entered.transition_progress == 0.0
    assert entered.album_transform.scale == before.album_transform.scale

    entering = [engine.update(0.1) for _ in range(5)]
    assert entering[0].album_transform.scale > 1.0
    assert entering[-1].previous_preset is None
    assert entering[-1].album_transform == album_transform_for_preset(
        "pulse",
        entering[-1].motion.bar_phase,
    )

    pulse_scale = entering[-1].album_transform.scale
    assert pulse_scale > 1.0
    engine.set_preset("static")
    left = engine.update(0.0)
    assert left.transition_progress == 0.0
    assert left.album_transform.scale == pulse_scale

    leaving = [engine.update(0.1) for _ in range(5)]
    scales = [left.album_transform.scale, *(frame.album_transform.scale for frame in leaving)]
    assert scales == sorted(scales, reverse=True)
    assert leaving[-1].album_transform.scale == 1.0
    assert leaving[-1].album_transform.translate_x == 0.0
    assert leaving[-1].album_transform.translate_y == 0.0
    assert leaving[-1].album_transform.rotation_degrees == 0.0


def test_transition_between_non_pulse_presets_keeps_exact_identity_transform() -> None:
    engine = PartyVisualEngine(seed=23, preset="starfield", transition_seconds=0.3)
    engine.update(0.1)
    engine.set_preset("aurora")
    frames = [engine.update(0.0), *(engine.update(0.1) for _ in range(3))]
    for frame in frames:
        assert frame.album_transform.scale == 1.0
        assert frame.album_transform.translate_x == 0.0
        assert frame.album_transform.translate_y == 0.0
        assert frame.album_transform.rotation_degrees == 0.0


def test_valid_beats_establish_tempo_and_outliers_are_rejected() -> None:
    clock = _established_clock()
    assert clock.tempo_bpm == pytest.approx(120.0)
    count = clock.interval_count
    assert clock.observe_beat(2.1) is False
    assert clock.interval_count == count
    assert clock.tempo_bpm == pytest.approx(120.0)
    assert clock.minimum_bpm <= clock.tempo_bpm <= clock.maximum_bpm


def test_beat_phase_continues_smoothly_through_missed_and_late_beats() -> None:
    clock = _established_clock()
    before = clock.last_state
    missed = clock.state_at(2.25)
    later = clock.state_at(2.45)
    assert missed.beat_position > before.beat_position
    assert later.beat_position > missed.beat_position
    assert 0.0 <= later.beat_phase < 1.0

    position_before_late = clock.last_state.beat_position
    assert clock.observe_beat(2.56)
    # Detection advances only by elapsed time, then queues correction; it does
    # not add an instantaneous phase jump.
    expected_at_detection = position_before_late + (0.11 / 0.5)
    assert clock.last_state.beat_position == pytest.approx(expected_at_detection, abs=0.02)
    position_at_detection = clock.last_state.beat_position
    corrected = clock.state_at(2.66)
    expected_without_correction = position_at_detection + (0.10 / 0.5)
    assert abs(corrected.beat_position - expected_without_correction) < 0.08
    assert corrected.beat_position > position_before_late


def test_beat_clock_reseeds_after_a_long_decoder_gap_without_phase_jump() -> None:
    clock = _established_clock()
    before = clock.state_at(5.0, audio_available=False)
    assert clock.observe_beat(5.0)
    assert clock.last_state.beat_position == pytest.approx(before.beat_position)
    assert clock.interval_count == 0
    assert clock.observe_beat(5.5)
    assert clock.observe_beat(6.0)
    assert clock.tempo_bpm == pytest.approx(120.0, rel=0.05)
    assert clock.interval_count == 2


def test_bar_phrase_phase_confidence_and_history_remain_bounded() -> None:
    clock = _established_clock(seed=37)
    for index in range(40):
        timestamp = 2.5 + (index * 0.5)
        clock.observe_beat(timestamp)
    state = clock.last_state
    assert 0.0 <= state.bar_phase < 1.0
    assert 0.0 <= state.phrase_phase < 1.0
    assert 0.0 <= state.tempo_confidence <= 1.0
    assert clock.interval_count <= MAX_INTERVAL_HISTORY
    confident = state.tempo_confidence
    decayed = clock.state_at(state.timestamp + 5.0, audio_available=False)
    assert 0.0 <= decayed.tempo_confidence < confident


def test_fallback_clock_is_calm_continuous_and_schedules_are_bounded() -> None:
    clock = BeatClock(seed=91)
    states = [clock.advance(0.1, audio_available=False) for _ in range(20)]
    assert all(state.tempo_bpm == pytest.approx(FALLBACK_TEMPO_BPM) for state in states)
    assert all(right.beat_position > left.beat_position for left, right in zip(states, states[1:]))
    assert all(0 <= state.beats_crossed <= 4 for state in states)
    assert all(0 <= state.next_accent_in_beats <= 8 for state in states)
    assert all(0 <= state.next_firework_in_beats <= 64 for state in states)


def test_beat_clock_emits_only_bounded_stable_schedule_events() -> None:
    clock = BeatClock(seed=117)
    accent_beats: list[int] = []
    firework_beats: list[int] = []
    for _ in range(1_200):
        state = clock.advance(0.1, audio_available=False)
        if state.accent_trigger:
            accent_beats.append(state.total_beat_count)
        if state.firework_trigger:
            firework_beats.append(state.total_beat_count)
    assert len(accent_beats) > 5
    assert all(4 <= right - left <= 8 for left, right in zip(accent_beats, accent_beats[1:]))
    assert len(firework_beats) > 1
    assert all(
        1 <= right - left <= 64
        for left, right in zip(firework_beats, firework_beats[1:])
    )


def test_static_engine_has_no_effect_data_and_canvas_timer_stays_stopped(qapp) -> None:
    engine = PartyVisualEngine(seed=4)
    frame = engine.update(0.1, {"energy": 1.0, "beat": True})
    assert frame.preset == "static"
    assert frame.particles == ()
    assert frame.aurora_offsets == ()
    assert frame.orbs == ()
    assert frame.firework_particles == ()
    assert frame.album_transform.scale == 1.0

    canvas = PartyCanvas(seed=4, preset="static")
    canvas.start_rendering()
    qapp.processEvents()
    assert canvas.rendering_active is False
    canvas.set_preset("starfield")
    assert canvas.rendering_active is True
    canvas.stop_rendering()
    canvas.deleteLater()


def test_starfield_drive_and_aurora_bands_glide_instead_of_snapping() -> None:
    starfield = PartyVisualEngine(seed=9, preset="starfield", quality="low")
    initial = starfield.update(0.05, {"energy": 0.1})
    before_y = initial.particles[0].y
    driven = starfield.update(0.05, {"energy": 1.0})
    assert 0.1 < driven.energy < 1.0
    assert ((driven.particles[0].y - before_y) % 1.0) < 0.02
    assert driven.album_transform.scale == 1.0

    aurora = PartyVisualEngine(seed=10, preset="aurora", quality="medium")
    high = aurora.update(
        0.05,
        {"energy": 0.9, "bass": 0.9, "low_mid": 0.8, "mid": 0.7, "high": 0.6},
    )
    released = aurora.update(
        0.05,
        {"energy": 0.0, "bass": 0.0, "low_mid": 0.0, "mid": 0.0, "high": 0.0},
    )
    assert 0.0 < released.bass < high.bass
    assert max(abs(a - b) for a, b in zip(high.aurora_offsets, released.aurora_offsets)) < 0.2
    assert released.album_transform.scale == 1.0


def test_orb_cluster_is_stable_depth_sorted_and_deterministic() -> None:
    state = _established_clock().state_at(2.2)
    first = OrbClusterSimulation(seed=12)
    second = OrbClusterSimulation(seed=12)
    a, radius_a, rotation_a = first.update(
        0.1, state, count=120, energy=0.5, reduced_motion=False
    )
    b, radius_b, rotation_b = second.update(
        0.1, state, count=120, energy=0.5, reduced_motion=False
    )
    assert a == b
    assert radius_a == radius_b
    assert rotation_a == rotation_b
    assert len(a) == 120 <= MAX_ORBS
    assert [orb.depth for orb in a] == sorted(orb.depth for orb in a)
    assert len({round(orb.depth, 3) for orb in a}) > 20
    assert all(0.0 <= orb.x <= 1.0 and 0.0 <= orb.y <= 1.0 for orb in a)
    far, near = a[0], a[-1]
    assert near.size > far.size * 0.75
    assert near.opacity > far.opacity * 0.75


def test_orb_breathing_expands_and_contracts_over_thirty_two_beats() -> None:
    assert OrbClusterSimulation.radius_scale(0.0, False) == pytest.approx(0.92)
    assert OrbClusterSimulation.radius_scale(0.25, False) == pytest.approx(1.0)
    assert OrbClusterSimulation.radius_scale(0.5, False) == pytest.approx(1.08)
    assert OrbClusterSimulation.radius_scale(0.75, False) == pytest.approx(1.0)
    assert OrbClusterSimulation.radius_scale(1.0, False) == pytest.approx(0.92)
    samples = [OrbClusterSimulation.radius_scale(index / 64, False) for index in range(65)]
    assert max(abs(right - left) for left, right in zip(samples, samples[1:])) < 0.01
    assert 0.98 <= OrbClusterSimulation.radius_scale(0.0, True) <= 1.02
    assert 0.98 <= OrbClusterSimulation.radius_scale(0.5, True) <= 1.02


def test_orb_accent_uses_small_subset_without_resetting_rotation() -> None:
    clock_state = _established_clock(seed=3).state_at(2.2)
    accented_state = replace(clock_state, accent_trigger=True)
    simulation = OrbClusterSimulation(seed=7)
    baseline, _, rotation_before = simulation.update(
        0.0, clock_state, count=100, energy=0.4, reduced_motion=False
    )
    assert 4 <= int(simulation.next_accent_in_beats(clock_state.total_beat_count) or 0) <= 8
    accented, _, rotation_after = simulation.update(
        0.0,
        accented_state,
        count=100,
        energy=0.4,
        reduced_motion=False,
        force_accent=True,
    )
    assert rotation_after == rotation_before
    assert 1 <= sum(orb.accent > 0.0 for orb in accented) <= 8
    assert sum(orb.accent > 0.0 for orb in accented) < len(accented)
    assert [(orb.x, orb.y) for orb in accented] == [(orb.x, orb.y) for orb in baseline]
    assert 4 <= int(simulation.next_accent_in_beats(clock_state.total_beat_count) or 0) <= 8


@pytest.mark.parametrize(
    ("position", "safe"),
    [
        ((0.18, 0.30), True),
        ((0.82, 0.30), True),
        ((0.50, 0.12), True),
        ((0.50, 0.50), False),
        ((0.90, 0.10), False),
        ((0.50, 0.90), False),
    ],
)
def test_firework_safe_position_contract(position: tuple[float, float], safe: bool) -> None:
    assert is_safe_firework_position(*position) is safe


def test_firework_safe_position_honors_bounded_dynamic_regions() -> None:
    lyrics_rect = ((0.10, 0.20, 0.26, 0.40),)
    assert is_safe_firework_position(0.18, 0.30) is True
    assert is_safe_firework_position(0.18, 0.30, lyrics_rect) is False
    assert is_safe_firework_position(0.82, 0.30, lyrics_rect) is True

    simulation = FireworksSimulation(seed=17)
    simulation.set_protected_rects(
        (
            (0.26, 0.40, 0.10, 0.20),
            (-5.0, -4.0, -3.0, -2.0),
            (float("nan"), 0.0, 1.0, 1.0),
        )
        + tuple((0.01, 0.01, 0.02, 0.02) for _ in range(20))
    )
    assert simulation.protected_rects[0] == pytest.approx((0.10, 0.20, 0.26, 0.40))
    assert len(simulation.protected_rects) <= MAX_FIREWORK_PROTECTED_RECTS
    assert simulation.spawn(
        particles_per_burst=24,
        maximum_bursts=2,
        maximum_particles=48,
        reduced_motion=False,
        center=(0.18, 0.30),
    ) is False
    assert simulation.spawn(
        particles_per_burst=24,
        maximum_bursts=2,
        maximum_particles=48,
        reduced_motion=False,
        center=(0.82, 0.30),
    ) is True


def test_firework_dynamic_regions_propagate_without_changing_seeded_behavior(qapp) -> None:
    protected = ((0.10, 0.20, 0.26, 0.40),)
    first = PartyVisualEngine(seed=71, preset="fireworks")
    second = PartyVisualEngine(seed=71, preset="fireworks")
    first.set_firework_protected_rects(protected)
    second.set_firework_protected_rects(protected)
    assert first.firework_protected_rects == protected
    assert first.update(0.1, {"energy": 0.4}) == second.update(
        0.1, {"energy": 0.4}
    )

    canvas = PartyCanvas(seed=71, preset="fireworks")
    canvas.set_firework_protected_rects(protected)
    assert canvas.firework_protected_rects == protected
    canvas.set_firework_protected_rects(())
    assert canvas.firework_protected_rects == ()
    canvas.deleteLater()
    qapp.processEvents()


def test_firework_particles_expand_drag_fall_fade_and_expire() -> None:
    simulation = FireworksSimulation(seed=17)
    assert simulation.spawn(
        particles_per_burst=24,
        maximum_bursts=2,
        maximum_particles=48,
        reduced_motion=False,
        center=(0.18, 0.30),
    )
    initial = simulation.update(
        0.05,
        trigger=False,
        particles_per_burst=24,
        maximum_bursts=2,
        maximum_particles=48,
        reduced_motion=False,
    )
    assert initial
    first = initial[0]
    radial_x, radial_y = first.x - 0.18, first.y - 0.30
    assert (radial_x * first.velocity_x) + (radial_y * first.velocity_y) > 0.0
    later = simulation.update(
        0.25,
        trigger=False,
        particles_per_burst=24,
        maximum_bursts=2,
        maximum_particles=48,
        reduced_motion=False,
    )
    same = next(p for p in later if p.burst_id == first.burst_id)
    assert abs(same.velocity_x) < abs(first.velocity_x)
    assert same.velocity_y > first.velocity_y * math.exp(-1.05 * 0.25)
    assert same.opacity < first.opacity
    for _ in range(30):
        simulation.update(
            0.1,
            trigger=False,
            particles_per_burst=24,
            maximum_bursts=2,
            maximum_particles=48,
            reduced_motion=False,
        )
    assert simulation.live_particle_count == 0
    assert simulation.active_burst_count == 0


def test_firework_countdown_is_one_to_sixty_four_beats_after_each_event() -> None:
    simulation = FireworksSimulation(seed=51)
    state = _established_clock(seed=51).state_at(2.2)
    simulation.update(
        0.0,
        trigger=False,
        particles_per_burst=24,
        maximum_bursts=2,
        maximum_particles=48,
        reduced_motion=False,
        total_beat_count=state.total_beat_count,
    )
    countdown = simulation.next_firework_in_beats(state.total_beat_count)
    assert countdown is not None and 1 <= countdown <= 64
    event_beat = state.total_beat_count + countdown
    particles = simulation.update(
        0.0,
        trigger=False,
        particles_per_burst=24,
        maximum_bursts=2,
        maximum_particles=48,
        reduced_motion=False,
        total_beat_count=event_beat,
    )
    assert particles
    next_countdown = simulation.next_firework_in_beats(event_beat)
    assert next_countdown is not None and 1 <= next_countdown <= 64


def test_firework_caps_and_reduced_motion_are_strict() -> None:
    normal = FireworksSimulation(seed=23)
    for center in ((0.18, 0.30), (0.82, 0.30), (0.50, 0.12)):
        assert normal.spawn(
            particles_per_burst=52,
            maximum_bursts=MAX_FIREWORK_BURSTS,
            maximum_particles=MAX_FIREWORK_PARTICLES,
            reduced_motion=False,
            center=center,
        )
    assert normal.spawn(
        particles_per_burst=52,
        maximum_bursts=MAX_FIREWORK_BURSTS,
        maximum_particles=MAX_FIREWORK_PARTICLES,
        reduced_motion=False,
        center=(0.18, 0.30),
    ) is False
    assert normal.active_burst_count == MAX_FIREWORK_BURSTS
    assert normal.live_particle_count <= MAX_FIREWORK_PARTICLES

    reduced = FireworksSimulation(seed=23)
    reduced.spawn(
        particles_per_burst=52,
        maximum_bursts=1,
        maximum_particles=52,
        reduced_motion=True,
        center=(0.18, 0.30),
    )
    assert reduced.live_particle_count < normal.live_particle_count / 2
    normal_state = normal.update(
        0.01,
        trigger=False,
        particles_per_burst=52,
        maximum_bursts=3,
        maximum_particles=156,
        reduced_motion=False,
    )[0]
    reduced_state = reduced.update(
        0.01,
        trigger=False,
        particles_per_burst=52,
        maximum_bursts=1,
        maximum_particles=52,
        reduced_motion=True,
    )[0]
    assert math.hypot(reduced_state.velocity_x, reduced_state.velocity_y) < math.hypot(
        normal_state.velocity_x, normal_state.velocity_y
    )


def test_engine_modes_keep_orbs_and_fireworks_separate_and_bounded() -> None:
    orb_engine = PartyVisualEngine(seed=29, preset="orb_cluster", quality="high")
    orb_frame = orb_engine.update(0.1, {"energy": 0.6})
    assert 0 < len(orb_frame.orbs) <= MAX_ORBS
    assert orb_frame.firework_particles == ()
    assert orb_frame.album_transform.scale == 1.0

    firework_engine = PartyVisualEngine(seed=29, preset="fireworks", quality="high")
    firework_frame = firework_engine.update(0.1, {"energy": 0.6})
    assert firework_frame.orbs == ()
    assert len(firework_frame.firework_particles) <= MAX_FIREWORK_PARTICLES
    assert firework_frame.active_firework_bursts <= MAX_FIREWORK_BURSTS
    assert firework_frame.album_transform.scale == 1.0


def test_orb_and_firework_render_paths_are_single_canvas_and_bounded(qapp) -> None:
    target = QImage(800, 450, QImage.Format.Format_ARGB32)
    target.fill(Qt.GlobalColor.transparent)
    canvas = PartyCanvas(seed=73, preset="orb_cluster", quality="low")
    canvas.resize(800, 450)
    canvas.set_track_text("Synthetic title", "Synthetic artist")
    canvas.set_playback_state(True, True)
    canvas.render(target)
    assert not target.isNull()
    assert len(canvas._orb_sprite_cache) <= 192
    assert not hasattr(canvas, "_player")

    canvas.set_preset("fireworks")
    assert canvas._engine._fireworks.spawn(
        particles_per_burst=24,
        maximum_bursts=1,
        maximum_particles=24,
        reduced_motion=False,
        center=(0.18, 0.30),
    )
    canvas._refresh_frame()
    canvas.render(target)
    assert len(canvas._frame.firework_particles) <= 24
    canvas.stop_rendering()
    canvas.deleteLater()


def test_raw_beats_do_not_restart_pulse_or_move_other_album_presets() -> None:
    engine = PartyVisualEngine(seed=31, preset="pulse", quality="low")
    positions: list[float] = []
    scales: list[float] = []
    for index in range(24):
        frame = engine.update(
            0.1,
            {
                "energy": 0.5,
                "beat": index % 2 == 0,
                "beat_strength": 0.8,
                "timestamp": index * 0.1,
            },
        )
        positions.append(frame.motion.beat_position)
        scales.append(frame.album_transform.scale)
    assert positions == sorted(positions)
    assert all(1.0 <= scale <= 1.03 for scale in scales)
    assert max(abs(right - left) for left, right in zip(scales, scales[1:])) < 0.01

    for preset in ("static", "starfield", "aurora", "orb_cluster", "fireworks"):
        engine.set_preset(preset)
        transition_steps = math.ceil(engine.transition_seconds / 0.1) + 1
        for step in range(transition_steps):
            frame = engine.update(
                0.1,
                {
                    "energy": 1.0,
                    "beat": True,
                    "timestamp": 99.0 + step,
                },
            )
        assert frame.previous_preset is None
        assert frame.album_transform.scale == 1.0
