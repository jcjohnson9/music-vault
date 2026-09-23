"""Deterministic synthetic colour checks; no artwork or media from the library."""
import math

import pytest
from PySide6.QtGui import QColor, QImage

from music_vault.ui.party_palette import (
    DEFAULT_PARTY_PALETTE, PaletteExtractor, contrast_ratio, interpolate_color,
    interpolate_palette, interpolate_perceptual_color, oklch_to_rgb,
    palette_for_preset, relative_luminance, rgb_to_oklch,
)


def solid(color):
    image = QImage(80, 120, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    return image


def hue_distance(a, b):
    return abs((a - b + 180) % 360 - 180)


@pytest.mark.parametrize("color", [(0, 0, 0), (255, 255, 255), (128, 128, 128),
                                  (255, 0, 0), (0, 255, 0), (0, 0, 255), (170, 83, 29)])
def test_perceptual_roundtrip_is_finite_and_channel_accurate(color):
    lch = rgb_to_oklch(color)
    assert all(math.isfinite(value) for value in lch)
    assert oklch_to_rgb(*lch) == color


def test_known_red_conversion_and_neutral_hue():
    lightness, chroma, hue = rgb_to_oklch((255, 0, 0))
    assert lightness == pytest.approx(0.62796, abs=0.0001)
    assert chroma == pytest.approx(0.25768, abs=0.0001)
    assert hue == pytest.approx(29.23, abs=0.01)
    assert rgb_to_oklch((120, 120, 120))[2] == 0


@pytest.mark.parametrize("lightness,chroma,hue", [
    (-10, -1, -1000), (10, 100, 1000), (float("nan"), float("inf"), float("-inf")),
    (0.78, 0.4, 0), (0.78, 0.4, 120), (0.78, 0.4, 260),
])
def test_gamut_mapping_is_bounded_deterministic(lightness, chroma, hue):
    color = oklch_to_rgb(lightness, chroma, hue)
    assert color == oklch_to_rgb(lightness, chroma, hue)
    assert all(type(value) is int and 0 <= value <= 255 for value in color)
    if lightness == 0.78:
        mapped_l, mapped_c, mapped_h = rgb_to_oklch(color)
        assert mapped_l == pytest.approx(lightness, abs=0.004)
        assert 0.03 < mapped_c < chroma
        assert hue_distance(mapped_h, hue) < 2


def test_short_hue_arc_and_achromatic_endpoint_do_not_fade_through_gray():
    first, second = oklch_to_rgb(0.75, 0.12, 350), oklch_to_rgb(0.75, 0.12, 10)
    midpoint = interpolate_perceptual_color(first, second, 0.5)
    assert hue_distance(rgb_to_oklch(midpoint)[2], 0) < 2
    blue = oklch_to_rgb(0.75, 0.12, 260)
    neutral_midpoint = interpolate_perceptual_color((180, 180, 180), blue, 0.5)
    assert hue_distance(rgb_to_oklch(neutral_midpoint)[2], rgb_to_oklch(blue)[2]) < 2
    opposite = oklch_to_rgb(0.75, 0.12, 170)
    assert rgb_to_oklch(interpolate_perceptual_color(first, opposite, 0.5))[1] > 0.075


def test_legacy_rgb_api_and_perceptual_endpoints_remain_exact():
    start, end = (0, 10, 20), (100, 110, 120)
    assert interpolate_color(start, end, 0.5) == (50, 60, 70)
    for amount in (-10, 0, float("nan"), float("inf"), "invalid"):
        assert interpolate_perceptual_color(start, end, amount) == start
    for amount in (1, 5):
        assert interpolate_perceptual_color(start, end, amount) == end


@pytest.mark.parametrize("color", ["#080706", "#000000", "#777777", "#ffffff", "#6b4325", "#c49a7c", "#db315f", "#2a63c7"])
def test_gray_brown_dark_and_chromatic_art_have_useful_lights_and_ink_stage(color):
    extractor = PaletteExtractor(max_cache_entries=2)
    image = solid(color)
    palette = extractor.extract(image)
    assert extractor.extract(image) is palette
    assert PaletteExtractor().extract(image) == palette
    stage_l, stage_c, _ = rgb_to_oklch(palette.background)
    assert 0.10 < stage_l < 0.17 and stage_c < 0.018
    assert relative_luminance(palette.background) < 0.01
    assert contrast_ratio(palette.foreground, palette.background) >= 7
    for light in (palette.primary, palette.secondary, palette.accent):
        l, c, _ = rgb_to_oklch(light)
        assert 0.70 < l < 0.88 and c > 0.055
        assert contrast_ratio(light, palette.background) >= 3
    for preset in ("orb_cluster", "fireworks"):
        directed = palette_for_preset(palette, preset)
        assert palette_for_preset(palette, preset) is directed
        assert directed.background == palette.background
        assert directed.foreground == palette.foreground
        assert all(contrast_ratio(light, directed.background) >= 3
                   for light in (directed.primary, directed.secondary, directed.accent))


def test_missing_art_and_palette_transition_preserve_contract_and_cache_bounds(tmp_path):
    extractor = PaletteExtractor(max_cache_entries=2)
    assert extractor.extract(None) == DEFAULT_PARTY_PALETTE
    assert extractor.extract(tmp_path / "missing.png") == DEFAULT_PARTY_PALETTE
    assert extractor.extract(b"invalid") == DEFAULT_PARTY_PALETTE
    assert extractor.cache_size == 2
    start, end = extractor.extract(solid("#ff0055")), extractor.extract(solid("#0088ee"))
    assert interpolate_palette(start, end, 0) == start
    assert interpolate_palette(start, end, 1) == end
    for step in range(21):
        palette = interpolate_palette(start, end, step / 20)
        assert contrast_ratio(palette.foreground, palette.background) >= 7
        assert all(contrast_ratio(light, palette.background) >= 3
                   for light in (palette.primary, palette.secondary, palette.accent))
    assert palette_for_preset(start, "static") is start
    assert palette_for_preset.cache_info().maxsize == 128
