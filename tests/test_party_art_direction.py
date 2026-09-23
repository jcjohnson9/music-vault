"""Approved glass constellation / comet-crown direction, synthetic inputs only."""

from dataclasses import replace
import math

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QImage, QPainter

from music_vault.core.musical_motion import BeatClock
from music_vault.ui.party_visuals import (
    AlbumTransform,
    FireworkParticleState,
    FireworksSimulation,
    MAX_FIREWORK_PARTICLES,
    MAX_FIREWORK_TRAIL_POINTS,
    MAX_ORB_SPRITE_CACHE,
    OrbClusterSimulation,
    OrbState,
    PartyCanvas,
    PartyVisualEngine,
    center_artwork_rect,
)


def step(simulation, delta=1 / 60, *, beat=None, energy=0.65, reduced=False, low=False):
    return simulation.update(
        delta,
        trigger=False,
        total_beat_count=beat,
        energy=energy,
        reduced_motion=reduced,
        particles_per_burst=24 if low else 52,
        maximum_bursts=1 if low else 3,
        maximum_particles=24 if low else MAX_FIREWORK_PARTICLES,
    )


def test_comet_is_prompt_then_crown_and_embers_are_deterministic_and_expire():
    first, second = FireworksSimulation(seed=98), FireworksSimulation(seed=98)
    assert step(first, 0, beat=0) == step(second, 0, beat=0) == ()
    assert first.next_firework_in_beats(0) == 1
    comet = step(first, 0, beat=1)
    assert comet == step(second, 0, beat=1)
    assert len(comet) == 1 and comet[0].phase == "comet"
    assert first.active_burst_count == 1
    phases = set()
    longest = 0
    colors_by_burst = {}
    for _ in range(200):
        a, b = step(first), step(second)
        assert a == b
        for particle in a:
            phases.add(particle.phase)
            longest = max(longest, len(particle.trail))
            colors_by_burst.setdefault(particle.burst_id, set()).add(particle.color_index)
            assert len(particle.trail) <= MAX_FIREWORK_TRAIL_POINTS
            assert all(math.isfinite(value) for point in particle.trail for value in point)
    assert phases == {"comet", "crown", "ember"}
    assert longest == MAX_FIREWORK_TRAIL_POINTS
    assert all(len(colors) == 1 for colors in colors_by_burst.values())
    assert first.live_particle_count == first.active_burst_count == 0


@pytest.mark.parametrize(("energy", "interval"), [(0.65, 4), (0.16, 8)])
def test_choreography_follows_existing_beats_and_has_no_long_random_wait(energy, interval):
    simulation = FireworksSimulation(seed=7)
    births = []
    seen = set()
    for tick in range(60 * 14):
        beat = tick // 30
        states = step(simulation, beat=beat, energy=energy)
        for particle in states:
            if particle.burst_id not in seen:
                births.append(beat)
                seen.add(particle.burst_id)
    assert births[0] == 1
    assert len(births) >= 4
    assert all(beat % interval == 0 for beat in births[1:])
    assert all(right - left <= interval for left, right in zip(births, births[1:]))


def test_quiet_frames_do_not_launch_and_existing_burst_dies_without_retrigger():
    simulation = FireworksSimulation(seed=32)
    for beat in range(90):
        assert step(simulation, 0.1, beat=beat, energy=0.055) == ()
    step(simulation, 0, beat=90)
    assert step(simulation, 0, beat=91)
    for tick in range(250):
        states = step(simulation, beat=92 + tick // 30, energy=0.0)
    assert states == ()
    assert simulation.live_particle_count == 0


def test_budget_downshift_counts_launches_and_trails_and_reset_clears_all():
    simulation = FireworksSimulation(seed=36)
    for center in ((0.18, 0.30), (0.82, 0.30), (0.5, 0.12)):
        assert simulation.spawn(
            particles_per_burst=52, maximum_bursts=3, maximum_particles=156,
            reduced_motion=False, center=center,
        )
    assert simulation.live_particle_count == 156
    states = step(simulation, low=True)
    assert len(states) <= 24
    assert simulation.active_burst_count <= 1
    simulation.reset()
    assert simulation.live_particle_count == 0
    assert simulation.next_firework_in_beats(0) is None
    assert step(simulation, 0, beat=0) == ()
    assert step(simulation, 0, beat=1)[0].phase == "comet"
    simulation.reset()
    assert step(simulation) == ()


def test_reduced_motion_retires_inflight_comet_and_limits_new_bloom():
    simulation = FireworksSimulation(seed=41)
    step(simulation, 0, beat=0)
    assert step(simulation, 0, beat=1)[0].phase == "comet"
    assert step(simulation, reduced=True) == ()
    states = step(simulation, beat=8, reduced=True)
    assert 0 < len(states) <= 22
    assert all(particle.phase == "crown" for particle in states)
    for _ in range(35):
        states = step(simulation, reduced=True)
        assert len(states) <= 22
        assert all(len(particle.trail) <= 4 for particle in states)
        assert not any(particle.phase == "comet" for particle in states)


def test_no_safe_center_does_not_force_burst_under_protected_content():
    simulation = FireworksSimulation(seed=5)
    simulation.set_protected_rects(((0, 0, 1, 1),))
    for beat in range(24):
        assert step(simulation, 0.1, beat=beat) == ()
    assert simulation.live_particle_count == 0


def test_crown_retains_its_origin_for_readable_arc_then_expires_without_unbounded_trail():
    simulation = FireworksSimulation(seed=45)
    simulation.spawn(
        particles_per_burst=24, maximum_bursts=1, maximum_particles=24,
        reduced_motion=False, center=(0.18, 0.30),
    )
    for _ in range(48):
        states = step(simulation)
    assert states
    assert all(p.trail[0] == (0.18, 0.30) for p in states)
    assert all(8 <= len(p.trail) <= 12 for p in states)
    assert all(p.opacity > 0.30 for p in states)
    for _ in range(100):
        states = step(simulation)
        assert all(len(p.trail) <= MAX_FIREWORK_TRAIL_POINTS for p in states)
    assert states == ()


def test_all_quality_orb_prefixes_form_a_depth_rich_constellation():
    clock = BeatClock(seed=13).advance(0.1, audio_available=False)
    for count in (64, 120, 200):
        states, _, _ = OrbClusterSimulation(seed=74).update(
            0.1, clock, count=count, energy=0.4, reduced_motion=False,
        )
        assert len(states) == count
        assert min(o.y for o in states) < 0.25
        assert max(o.y for o in states) > 0.65
        assert min(o.x for o in states) < 0.25
        assert max(o.x for o in states) > 0.75
        assert all(0 <= o.x <= 1 and 0 <= o.y <= 1 for o in states)
        sizes = [(0.009 + 0.033 * o.depth**2) * o.size for o in states]
        assert max(sizes) / min(sizes) > 3
        assert sum(o.color_index == 2 for o in states) < count / 4


def _paint(canvas, frame, *, fireworks=True, opacity=1):
    image = QImage(canvas.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setOpacity(opacity)
    if fireworks:
        canvas._paint_fireworks(painter, frame)
    else:
        canvas._paint_orb_cluster(painter, frame)
    assert painter.opacity() == pytest.approx(opacity)
    painter.end()
    return image


def _assert_clear(image, rect):
    left, top, right, bottom = rect
    assert all(
        image.pixelColor(x, y).alpha() == 0
        for x in range(left, right)
        for y in range(top, bottom)
    )


def test_entire_firework_trails_heads_and_glow_are_clipped_out_of_content(qapp):
    canvas = PartyCanvas(seed=2, preset="fireworks")
    canvas.resize(800, 600)
    canvas.set_firework_protected_rects(((0.10, 0.33, 0.25, 0.47),))
    crossing = FireworkParticleState(
        1, 0.82, 0.40, 0, 0, 4, 0.78, 0.82, 1,
        ((0.04, 0.40), (0.2, 0.40), (0.5, 0.40), (0.82, 0.40)), "crown",
    )
    protected_head = replace(crossing, x=0.12, y=0.10, trail=((0.22, 0.2), (0.12, 0.1)))
    frame = replace(canvas._frame, firework_particles=(crossing, protected_head))
    image = _paint(canvas, frame)
    _assert_clear(image, (80, 198, 200, 282))  # supplied overlay, including crossing tail
    art = center_artwork_rect(800, 600)
    _assert_clear(image, (int(art.left()), int(art.top()), int(art.right()), int(art.bottom())))
    _assert_clear(image, (0, 0, 160, 108))  # upper-left controls, including halo
    assert image.pixelColor(656, 240).alpha() > 0
    # A new overlay takes effect immediately, even for already in-flight trails.
    canvas.set_firework_protected_rects(((0.75, 0.30, 0.9, 0.5),))
    newer = _paint(canvas, frame)
    _assert_clear(newer, (600, 180, 720, 300))
    canvas.deleteLater()


def test_orb_material_is_cached_without_depth_opacity_and_crossfade_is_preserved(qapp):
    canvas = PartyCanvas(seed=2, preset="orb_cluster")
    canvas.resize(800, 600)
    orb = OrbState(0.20, 0.42, 0.9, 1.2, 0.8, 0, 0.0, 0.0)
    sprite = canvas._orb_sprite(orb, 28)
    assert sprite.cacheKey() == canvas._orb_sprite(replace(orb, opacity=0.2), 28).cacheKey()
    assert sprite.cacheKey() == canvas._orb_sprite(replace(orb, color_mix=0.8), 28).cacheKey()
    frame = replace(canvas._frame, orbs=(orb,))
    full = _paint(canvas, frame, fireworks=False)
    half = _paint(canvas, frame, fireworks=False, opacity=0.5)
    total = lambda image: sum(image.pixelColor(x, y).alpha() for x in range(140, 180) for y in range(232, 272))
    assert 0.47 < total(half) / total(full) < 0.52
    canvas.set_firework_protected_rects(((0.16, 0.35, 0.24, 0.50),))
    protected = _paint(canvas, frame, fireworks=False)
    _assert_clear(protected, (128, 210, 192, 300))
    for diameter in range(2, 150):
        canvas._orb_sprite(replace(orb, color_index=diameter % 3), diameter)
    assert len(canvas._orb_sprite_cache) <= MAX_ORB_SPRITE_CACHE
    canvas.deleteLater()


def test_title_protection_leaves_unused_side_lanes_open(qapp):
    canvas = PartyCanvas(seed=9, preset="orb_cluster")
    canvas.resize(800, 600)
    canvas.set_track_text("Synthetic title", "Synthetic artist")
    clip = canvas._effect_clip_path()
    bottom = center_artwork_rect(800, 600).bottom()
    assert not clip.contains(QPointF(400, bottom + 25))
    assert not clip.contains(QPointF(400, bottom + 56))
    assert clip.contains(QPointF(80, bottom + 25))
    assert clip.contains(QPointF(720, bottom + 56))
    assert not clip.contains(center_artwork_rect(800, 600).center())
    original_transform = canvas._frame.album_transform
    # Both long strings use the same width cap as the center's elided text.
    canvas.set_track_text("W" * 240, "W" * 240)
    clip = canvas._effect_clip_path()
    assert not clip.contains(QPointF(42, bottom + 25))
    assert not clip.contains(QPointF(758, bottom + 56))
    assert clip.contains(QPointF(20, bottom + 25))
    assert clip.contains(QPointF(780, bottom + 56))
    assert not clip.contains(center_artwork_rect(800, 600).center())
    assert canvas._frame.album_transform == original_transform == AlbumTransform()
    canvas.deleteLater()


def test_engine_switch_clears_comets_and_preserves_album_geometry():
    engine = PartyVisualEngine(seed=3, preset="fireworks")
    seen = False
    for _ in range(90):
        frame = engine.update(1 / 60, {"energy": 0.65})
        seen |= bool(frame.firework_particles)
        assert frame.album_transform == AlbumTransform()
    assert seen
    engine.set_preset("orb_cluster")
    for _ in range(60):
        frame = engine.update(1 / 60, {"energy": 0.65})
    assert frame.firework_particles == ()
    assert engine._fireworks.live_particle_count == 0
    assert frame.album_transform == AlbumTransform()


@pytest.mark.parametrize(("width", "height"), [(3440, 1440), (1920, 1080)])
def test_center_typography_fits_fixed_rows_and_protection_uses_same_font(qapp, width, height):
    class TextRecordingPainter(QPainter):
        def __init__(self, image):
            super().__init__(image)
            self.text_rows = []

        def drawText(self, *args):
            self.text_rows.append((args[0], QFont(self.font())))
            return super().drawText(*args)

    canvas = PartyCanvas(seed=4, preset="orb_cluster")
    canvas.resize(width, height)
    image = QImage(canvas.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDotsPerMeterX(round(canvas.logicalDpiX() / 0.0254))
    image.setDotsPerMeterY(round(canvas.logicalDpiY() / 0.0254))
    image.fill(Qt.GlobalColor.transparent)
    painter = TextRecordingPainter(image)
    canvas._paint_center(painter, canvas._frame)
    assert [rect.height() for rect, _ in painter.text_rows] == [54.0, 40.0]
    for rect, font in painter.text_rows:
        assert QFontMetricsF(font, canvas).height() <= rect.height() - 4.0
    assert painter.text_rows[0][1] == canvas._center_text_font(54, 24, 20, QFont.Weight.DemiBold)
    painter.text_rows.clear()
    canvas.set_track_text("Synthetic ultrawide title", "Synthetic artist")
    canvas.set_playback_state(False, True)
    canvas._paint_center(painter, canvas._frame)
    assert [rect.height() for rect, _ in painter.text_rows] == [32.0, 28.0]
    art = center_artwork_rect(width, height)
    assert painter.text_rows[0][0].top() == art.bottom() + 14.0
    assert painter.text_rows[1][0].top() == art.bottom() + 46.0
    clip = canvas._effect_clip_path()
    for (rect, font), text in zip(painter.text_rows, (canvas._title, canvas._artist)):
        metrics = QFontMetricsF(font, canvas)
        assert metrics.height() <= rect.height() - 4.0
        text_half_width = metrics.horizontalAdvance(text) / 2.0
        assert not clip.contains(QPointF(width / 2 + text_half_width, rect.center().y()))
        assert clip.contains(QPointF(width / 2 + text_half_width + 12.0, rect.center().y()))
    assert canvas._frame.album_transform == AlbumTransform()
    painter.end()
    canvas.deleteLater()
