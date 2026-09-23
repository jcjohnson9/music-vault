"""Party visual exclusions follow visible controls without owning playback."""
from PySide6.QtTest import QTest

from test_batch9_party_mode import party_surface  # noqa: F401


def covered(window, widget):
    center = widget.mapTo(window.canvas, widget.rect().center())
    x, y = center.x() / window.canvas.width(), center.y() / window.canvas.height()
    return any(left <= x <= right and top <= y <= bottom
               for left, top, right, bottom in window.canvas.firework_protected_rects)


def test_visible_controls_and_help_are_protected_without_lyrics(party_surface, qapp):
    host, window = party_surface
    window.resize(1280, 720)
    window.show()
    qapp.processEvents()
    window.show_overlay()
    assert covered(window, window.controls_panel)
    assert covered(window, window.exit_button)
    assert covered(window, window.preset_button)
    assert not host.calls
    window.toggle_help()
    assert covered(window, window.help_panel)
    assert len(window.canvas.firework_protected_rects) <= 4
    window.close()
    qapp.processEvents()
    assert not window.canvas.firework_protected_rects
    assert not window.canvas.rendering_active


def test_control_protection_survives_fade_then_releases(party_surface, qapp):
    _host, window = party_surface
    window.resize(1280, 720)
    window.show()
    qapp.processEvents()
    window.show_overlay()
    window._settings["party_mode_reduced_motion"] = False
    window.hide_overlay()
    assert covered(window, window.controls_panel)
    QTest.qWait(250)
    assert window.overlay_effect.opacity() == 0.0
    assert not window.canvas.firework_protected_rects
    window.show_overlay()
    assert covered(window, window.controls_panel)


def test_protected_controls_follow_resize(party_surface, qapp):
    _host, window = party_surface
    window.show()
    for width, height in ((1280, 720), (1920, 1080), (2560, 1080)):
        window.resize(width, height)
        qapp.processEvents()
        window._update_firework_lyrics_protection()
        assert covered(window, window.controls_panel)
        assert covered(window, window.exit_button)
        assert all(0 <= a <= b <= 1 and 0 <= c <= d <= 1
                   for a, c, b, d in window.canvas.firework_protected_rects)
