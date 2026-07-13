from __future__ import annotations

import ctypes
import json
import math
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QItemSelectionModel, QObject, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtTest import QTest

from music_vault.core import paths
from music_vault.core.db import MusicVaultDB
from music_vault.ui import icons
from music_vault.ui.components import (
    ElidedLabel,
    EmptyState,
    IconButton,
    OverflowActionButton,
    SearchField,
)
from music_vault.ui.icons import REQUIRED_ICONS
from music_vault.ui import theme


PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_REQUIRED_ICONS = {
    "play",
    "pause",
    "previous",
    "next",
    "shuffle",
    "repeat",
    "repeat-one",
    "volume",
    "volume-low",
    "volume-muted",
    "search",
    "library",
    "recently-added",
    "downloaded",
    "albums",
    "artists",
    "artist-unknown",
    "playlists",
    "sync",
    "settings",
    "import",
    "add",
    "queue-next",
    "more",
    "refresh",
    "remove",
    "metadata",
    "enrich",
    "folder",
    "warning",
    "error",
}

EXPECTED_OVERFLOW_ACTIONS = [
    "Remove From Playlist",
    "Edit Metadata",
    "Review Library Metadata",
    "Remove Missing",
    "Refresh Art",
]

LONG_TITLE = (
    "A Deliberately Long Synthetic Track Title Used to Verify Responsive "
    "Elision Without Reading Any Personal Library Data"
)
LONG_ARTIST = (
    "The Exceptionally Long Synthetic Artist Collective for Layout Review"
)
LONG_ALBUM = (
    "An Intentionally Oversized Synthetic Album Name for Responsive Cards"
)
LONG_PLAYLIST = (
    "An Exceptionally Long Synthetic Playlist Name Intended to Verify Sidebar Elision"
)


@pytest.fixture
def source_icon_assets(monkeypatch):
    """Resolve UI assets from source without consulting any runtime root."""

    monkeypatch.setattr(icons, "assets_dir", lambda: PROJECT_ROOT / "assets")
    icons.clear_icon_cache()
    yield
    icons.clear_icon_cache()


def test_theme_exposes_required_design_tokens_and_nonempty_qss():
    required_colors = {
        "app_background",
        "sidebar_background",
        "elevated_surface",
        "card_surface",
        "subtle_surface",
        "hover_surface",
        "pressed_surface",
        "border",
        "strong_border",
        "text_primary",
        "text_secondary",
        "text_muted",
        "accent",
        "accent_hover",
        "accent_pressed",
        "danger",
        "warning",
        "selection",
        "now_playing",
        "focus_ring",
    }
    assert required_colors <= theme.COLORS.keys()
    assert {4, 8, 12, 16, 20, 24, 32} <= set(theme.SPACING.values())
    assert {"control", "card", "panel", "circular", "artwork"} <= theme.RADII.keys()
    assert {
        "family",
        "page_title_size",
        "section_title_size",
        "card_title_size",
        "body_size",
        "metadata_size",
        "caption_size",
        "button_size",
    } <= theme.TYPOGRAPHY.keys()

    stylesheet = theme.application_stylesheet()
    assert stylesheet.strip()
    assert len(stylesheet) > 1_000


def test_theme_qss_has_transparent_labels_scrollbars_and_control_states():
    stylesheet = theme.application_stylesheet()

    label_rule = re.search(r"QLabel\s*\{(?P<body>[^}]*)\}", stylesheet, re.DOTALL)
    assert label_rule is not None
    assert re.search(r"background\s*:\s*transparent", label_rule.group("body"))

    for selector, zero_extent in (
        ("QScrollBar::add-line:vertical", "height: 0px"),
        ("QScrollBar::sub-line:vertical", "height: 0px"),
        ("QScrollBar::add-line:horizontal", "width: 0px"),
        ("QScrollBar::sub-line:horizontal", "width: 0px"),
    ):
        assert selector in stylesheet
        assert zero_extent in stylesheet

    for state in ("QPushButton:hover", "QPushButton:pressed", "QPushButton:focus", "QPushButton:disabled"):
        assert state in stylesheet
    for variant in ('variant="primary"', 'variant="secondary"', 'variant="danger"'):
        assert variant in stylesheet
    assert theme.COLORS["focus_ring"] in stylesheet
    assert "QCheckBox::indicator:checked" in stylesheet
    assert "QComboBox QAbstractItemView" in stylesheet
    assert "QToolTip" in stylesheet
    assert "QMessageBox" in stylesheet
    assert f"background: {theme.COLORS['card_surface']}" in stylesheet
    assert "QSlider::groove:horizontal:focus" in stylesheet
    assert "QSlider::handle:horizontal:focus" in stylesheet
    assert 'QProgressBar#SyncProgress[syncState="complete"]' in stylesheet
    assert 'QProgressBar#SyncProgress[syncState="complete_with_issues"]' in stylesheet
    assert f"color: {theme.COLORS['accent_ink']}" in stylesheet


def test_required_svg_assets_exist_are_valid_and_contain_no_text(source_icon_assets):
    assert EXPECTED_REQUIRED_ICONS <= set(REQUIRED_ICONS)

    for name in REQUIRED_ICONS:
        path = icons.icon_path(name)
        assert path.is_file(), name
        assert path.suffix.casefold() == ".svg"
        assert QSvgRenderer(str(path)).isValid(), name
        source = path.read_text(encoding="utf-8").casefold()
        assert "<text" not in source, name
        assert "font-family" not in source, name


@pytest.mark.parametrize("size", [16, 20, 24, 32])
@pytest.mark.parametrize("dpr", [1.0, 2.0])
def test_every_required_icon_renders_at_multiple_sizes_and_dpr(
    source_icon_assets,
    qapp,
    size,
    dpr,
):
    for name in REQUIRED_ICONS:
        pixmap = icons.render_icon_pixmap(name, size, "#D7DEE8", dpr=dpr)
        assert not pixmap.isNull(), name
        assert pixmap.width() == math.ceil(size * dpr), name
        assert pixmap.height() == math.ceil(size * dpr), name
        assert pixmap.devicePixelRatio() == pytest.approx(dpr), name
        assert pixmap.deviceIndependentSize().width() == pytest.approx(size), name
        assert pixmap.deviceIndependentSize().height() == pytest.approx(size), name


def test_icon_pixmap_cache_and_qicon_variants(source_icon_assets, qapp):
    icons.clear_icon_cache()
    first = icons.render_icon_pixmap("play", 24, "#F4F7FB", dpr=2.0)
    before = icons.icon_cache_info()
    second = icons.render_icon_pixmap("play", 24, "#F4F7FB", dpr=2.0)
    after = icons.icon_cache_info()

    assert first.cacheKey() == second.cacheKey()
    assert after.hits == before.hits + 1

    icon = icons.ui_icon(
        "play",
        24,
        color="#C4CDD8",
        disabled_color="#596474",
        active_color="#25D366",
    )
    variants = [
        icon.pixmap(QSize(24, 24), QIcon.Mode.Normal, QIcon.State.Off),
        icon.pixmap(QSize(24, 24), QIcon.Mode.Active, QIcon.State.Off),
        icon.pixmap(QSize(24, 24), QIcon.Mode.Disabled, QIcon.State.Off),
        icon.pixmap(QSize(24, 24), QIcon.Mode.Selected, QIcon.State.On),
    ]
    assert all(not pixmap.isNull() for pixmap in variants)
    assert len({pixmap.cacheKey() for pixmap in variants}) >= 3


@pytest.mark.parametrize("dpr", [1.25, 1.5])
def test_direct_icon_pixmap_uses_application_dpr_by_default(
    source_icon_assets,
    qapp,
    monkeypatch,
    dpr,
):
    icons.clear_icon_cache()
    monkeypatch.setattr(icons, "_application_dpr", lambda: dpr)
    pixmap = icons.render_icon_pixmap("music-note", 24, "#F4F7FB")

    assert pixmap.width() == math.ceil(24 * dpr)
    assert pixmap.height() == math.ceil(24 * dpr)
    assert pixmap.devicePixelRatio() == pytest.approx(dpr)
    assert pixmap.deviceIndependentSize() == QSize(24, 24)


def test_icon_button_is_accessible_and_uses_an_icon(source_icon_assets, qapp):
    button = IconButton(
        "queue-next",
        "Queue selected track next",
        accessible_name="Queue next",
        variant="primary",
    )
    assert button.toolTip() == "Queue selected track next"
    assert button.accessibleName() == "Queue next"
    assert button.property("variant") == "primary"
    assert button.icon_name == "queue-next"
    assert not button.icon().isNull()


def test_search_field_has_leading_icon_and_escape_clears(
    source_icon_assets,
    qapp,
):
    search = SearchField("Search synthetic tracks")
    search.show()
    search.setFocus()
    search.setText("synthetic query")
    qapp.processEvents()

    assert search.search_action in search.actions()
    assert not search.search_action.icon().isNull()
    assert search.search_action.toolTip() == "Search"
    assert search.accessibleName() == "Search synthetic tracks"

    QTest.keyClick(search, Qt.Key.Key_Escape)
    assert search.text() == ""
    search.close()


def test_elided_label_preserves_full_text_accessibility_and_tooltip(qapp):
    label = ElidedLabel(LONG_TITLE)
    label.setFixedWidth(90)
    label.show()
    qapp.processEvents()

    assert label.fullText() == LONG_TITLE
    assert label.accessibleName() == LONG_TITLE
    assert label.text() != LONG_TITLE
    assert label.toolTip() == f"<qt>{LONG_TITLE}</qt>"
    label.close()


def test_elided_label_treats_metadata_markup_as_literal_text(qapp):
    label = ElidedLabel('<img src="file:///private/path"> Synthetic')
    label.setFixedWidth(20)
    label.show()
    qapp.processEvents()

    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.fullText().startswith("<img")
    assert "<img" not in label.toolTip()
    assert "&lt;img" in label.toolTip()


def test_overflow_button_keeps_exact_actions_and_callbacks(
    source_icon_assets,
    qapp,
):
    triggered: list[str] = []
    overflow = OverflowActionButton()
    for text, icon_name, destructive in (
        ("Remove From Playlist", "remove", True),
        ("Edit Metadata", "metadata", False),
        ("Review Library Metadata", "metadata", False),
        ("Remove Missing", "warning", True),
        ("Refresh Art", "refresh", False),
    ):
        overflow.add_action(
            text,
            icon_name,
            lambda value=text: triggered.append(value),
            destructive=destructive,
        )

    assert overflow.action_texts() == EXPECTED_OVERFLOW_ACTIONS
    assert overflow.accessibleName()
    assert overflow.toolTip()
    for text in EXPECTED_OVERFLOW_ACTIONS:
        action = overflow.action(text)
        assert action is not None
        assert not action.icon().isNull()
        assert action.toolTip() == text
        action.trigger()
    assert triggered == EXPECTED_OVERFLOW_ACTIONS
    assert overflow.action("Remove From Playlist").property("destructive") is True
    assert overflow.action("Remove Missing").property("destructive") is True


def test_empty_state_renders_accessibly_without_exception(source_icon_assets, qapp):
    state = EmptyState(
        "library",
        "No synthetic tracks",
        "Import a safe synthetic fixture to continue.",
        action_text="Import Folder",
    )
    state.show()
    qapp.processEvents()

    assert state.accessibleName() == "No synthetic tracks"
    assert state.title_label.text() == "No synthetic tracks"
    assert state.description_label.wordWrap()
    assert state.action_button is not None
    assert state.action_button.accessibleName() == "Import Folder"
    assert state.icon_label.pixmap() is not None
    assert not state.icon_label.pixmap().isNull()
    state.close()


def test_dark_title_bar_helper_noops_off_windows(monkeypatch):
    class MustNotUseHandle:
        def winId(self):
            raise AssertionError("winId must not be requested off Windows")

    monkeypatch.setattr(theme.sys, "platform", "linux")
    assert theme.apply_dark_title_bar(MustNotUseHandle()) is False


def test_dark_title_bar_helper_survives_unavailable_dwm(monkeypatch):
    class MissingDwm:
        def DwmSetWindowAttribute(self, *_args):
            raise OSError("synthetic DWM failure")

    monkeypatch.setattr(theme.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(dwmapi=MissingDwm()),
        raising=False,
    )
    assert theme.apply_dark_title_bar(SimpleNamespace(winId=lambda: 1)) is False


def _seed_synthetic_database(root: Path) -> list[int]:
    data_dir = root / "data"
    downloads = data_dir / "youtube_downloads"
    media_dir = data_dir / "synthetic_media"
    downloads.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True)

    db = MusicVaultDB(
        data_dir / "music_vault.sqlite3",
        backup_dir=data_dir / "backups",
        youtube_download_root=downloads,
    )
    track_ids: list[int] = []
    artists = [
        LONG_ARTIST,
        "Neon Harbor",
        "Lunar Assembly",
        "Glass Meridian",
        "Quiet Current",
        "Paper Satellites",
    ]
    albums = [
        LONG_ALBUM,
        "Signal Bloom",
        "Night Geometry",
        "Soft Machines",
        "Afterglow Index",
        "Parallel Rooms",
    ]

    for index in range(18):
        path = media_dir / f"synthetic-track-{index + 1:02d}.synthetic"
        path.write_bytes(b"synthetic UI fixture; not audio")
        db.upsert_track(
            path,
            title=LONG_TITLE if index == 0 else f"Synthetic Signal {index + 1:02d}",
            artist=None if index == 1 else artists[index % len(artists)],
            album=None if index == 2 else albums[index % len(albums)],
            duration_seconds=150 + index,
            source_kind="youtube" if index in (3, 8, 13) else "local",
        )
        row = db.conn.execute("SELECT id FROM tracks WHERE path=?", (str(path.resolve()),)).fetchone()
        track_id = int(row["id"])
        db.update_track_metadata(track_id, year=str(2001 + (index % 20)))
        track_ids.append(track_id)

    review_playlist = db.create_playlist("Synthetic UI Review Mix")
    for track_id in track_ids[:10]:
        db.add_track_to_playlist(review_playlist, track_id)

    long_playlist = db.create_playlist(LONG_PLAYLIST)
    for track_id in track_ids[10:15]:
        db.add_track_to_playlist(long_playlist, track_id)

    db.create_playlist("Empty Synthetic Playlist")
    db.close()
    return track_ids


@pytest.fixture
def isolated_ui_window(tmp_path: Path, monkeypatch, qapp):
    root = tmp_path / "synthetic_runtime"
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    shutil.copytree(PROJECT_ROOT / "assets" / "icons", root / "assets" / "icons")

    downloads = root / "data" / "youtube_downloads"
    downloads.mkdir(parents=True)
    config_path = root / "data" / "music_vault_config.json"
    config_path.write_text(
        json.dumps(
            {
                "download_folder": str(downloads),
                "audio_quality": "320",
                "volume_percent": 23,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    track_ids = _seed_synthetic_database(root)

    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(root))
    monkeypatch.setenv("HOME", str(root / "profile"))
    monkeypatch.setenv("USERPROFILE", str(root / "profile"))
    monkeypatch.delenv("MUSIC_VAULT_UI_REVIEW", raising=False)
    monkeypatch.delenv("MUSIC_VAULT_UI_REVIEW_PLAN", raising=False)
    paths._resolved_project_root.cache_clear()

    from music_vault import app as app_module

    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "use_system_default_audio_output",
        lambda self: None,
    )
    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "read_saved_api_key",
        lambda self: "",
    )
    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "find_ffmpeg_bin",
        lambda self: None,
    )
    monkeypatch.setattr(app_module, "apply_dark_title_bar", lambda _window: False)
    monkeypatch.setattr(
        app_module,
        "export_app_status",
        lambda *_args, **_kwargs: root / "data" / "music_vault_status.json",
    )

    window = app_module.MusicVaultWindow()
    window.show()
    qapp.processEvents()

    yield SimpleNamespace(
        window=window,
        root=root,
        track_ids=track_ids,
        app_module=app_module,
    )

    for timer_name in ("audio_device_timer", "volume_save_timer", "_browser_reflow_timer"):
        timer = getattr(window, timer_name, None)
        if timer is not None:
            timer.stop()
    try:
        window.player.stop()
        window.close()
        window.db.close()
        window.deleteLater()
    finally:
        qapp.processEvents()
        paths._resolved_project_root.cache_clear()


def _widget_rect_in_window(widget, window) -> QRect:
    origin = widget.mapTo(window, QPoint(0, 0))
    return QRect(origin, widget.size())


def _pixmap_contains_color(pixmap: QPixmap, color_value: str) -> bool:
    image = pixmap.toImage()
    target = QColor(color_value)
    for x in range(image.width()):
        for y in range(image.height()):
            color = image.pixelColor(x, y)
            if color.alpha() < 128:
                continue
            if all(
                abs(actual - expected) <= 2
                for actual, expected in (
                    (color.red(), target.red()),
                    (color.green(), target.green()),
                    (color.blue(), target.blue()),
                )
            ):
                return True
    return False


def _pixmap_color_count(pixmap: QPixmap, color_value: str) -> int:
    image = pixmap.toImage()
    target = QColor(color_value)
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).rgba() == target.rgba()
    )


def test_edit_metadata_action_requires_exactly_one_selected_track(isolated_ui_window, qapp):
    window = isolated_ui_window.window
    window.library_table.clearSelection()
    window.update_metadata_action_state()
    assert not window.edit_metadata_action.isEnabled()

    window.library_table.selectRow(0)
    qapp.processEvents()
    assert window.edit_metadata_action.isEnabled()

    selection = window.library_table.selectionModel()
    selection.select(
        window.library_table.model().index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    qapp.processEvents()
    assert len(window.selected_track_ids()) == 2
    assert not window.edit_metadata_action.isEnabled()


def test_metadata_refresh_preserves_player_queue_context_and_playlists(isolated_ui_window):
    window = isolated_ui_window.window
    track_id = isolated_ui_window.track_ids[0]
    window.current_track_id = track_id
    window.manual_queue = [isolated_ui_window.track_ids[1]]
    window.base_playback_context = {
        "kind": "library",
        "playlist_id": None,
        "playlist_name": "Library",
        "track_ids": list(isolated_ui_window.track_ids[:3]),
        "current_track_id": track_id,
    }
    queue_before = list(window.manual_queue)
    context_before = dict(window.base_playback_context)
    membership_before = window.db.conn.execute(
        "SELECT COUNT(*) FROM playlist_tracks"
    ).fetchone()[0]
    source_before = window.player.source()
    position_before = window.player.position()
    state_before = window.player.playbackState()

    result = window.metadata_service.apply_manual_patch(
        track_id,
        {"title": "Trusted Synthetic Title", "artist": "Trusted Synthetic Artist"},
    )
    window.metadata_change_applied(result)

    assert window.now_title.fullText() == "Trusted Synthetic Title"
    assert window.now_artist.fullText() == "Trusted Synthetic Artist"
    assert window.player.source() == source_before
    assert window.player.position() == position_before
    assert window.player.playbackState() == state_before
    assert window.manual_queue == queue_before
    assert window.base_playback_context == context_before
    assert window.db.conn.execute(
        "SELECT COUNT(*) FROM playlist_tracks"
    ).fetchone()[0] == membership_before


@pytest.mark.parametrize("width,height", [(1100, 720), (1440, 900), (1920, 1080)])
def test_window_responsive_critical_controls_and_centered_player(
    isolated_ui_window,
    qapp,
    width,
    height,
):
    window = isolated_ui_window.window
    window.pages.setCurrentWidget(window.library_page)
    window.resize(width, height)
    qapp.processEvents()
    QTest.qWait(30)

    assert window.size() == QSize(width, height)
    for widget in (
        window.sidebar,
        window.import_btn,
        window.create_playlist_btn,
        window.add_playlist_btn,
        window.queue_next_btn,
        window.library_overflow,
        window.player_center,
        window.play_btn,
        window.prev_btn,
        window.next_btn,
        window.volume_icon,
        window.volume_slider,
    ):
        assert widget.isVisible(), widget.objectName()
        assert window.rect().intersects(_widget_rect_in_window(widget, window))

    player_bar_rect = _widget_rect_in_window(window.player_bar, window)
    player_center_rect = _widget_rect_in_window(window.player_center, window)
    assert abs(player_center_rect.center().x() - player_bar_rect.center().x()) <= 12

    assert window.library_overflow.action_texts() == EXPECTED_OVERFLOW_ACTIONS
    assert window.volume_slider.value() == 23
    assert window.volume_slider.width() >= 96
    assert not window.play_btn.icon().isNull()
    assert not window.prev_btn.icon().isNull()
    assert not window.next_btn.icon().isNull()
    assert window.library_table.isColumnHidden(4)


def test_window_interaction_polish_and_dpr_assets(isolated_ui_window, qapp):
    fixture = isolated_ui_window
    window = fixture.window
    window.pages.setCurrentWidget(window.library_page)
    window.update_sidebar_navigation_state()

    assert window.library_btn.isChecked()
    window.library_btn.click()
    window.library_btn.click()
    assert window.library_btn.isChecked()
    assert window.pages.currentWidget() is window.library_page

    window.sync_btn_nav.click()
    window.sync_btn_nav.click()
    assert window.sync_btn_nav.isChecked()
    assert not window.library_btn.isChecked()
    assert window.pages.currentWidget() is window.sync_page
    window.library_btn.click()

    primary_active = window.import_btn.icon().pixmap(
        QSize(18, 18), QIcon.Mode.Active, QIcon.State.Off
    )
    play_active = window.play_btn.icon().pixmap(
        QSize(24, 24), QIcon.Mode.Active, QIcon.State.Off
    )
    danger_button = window.make_action_button(
        "Synthetic Danger",
        "warning",
        lambda: None,
        object_name="DangerButton",
    )
    danger_active = danger_button.icon().pixmap(
        QSize(18, 18), QIcon.Mode.Active, QIcon.State.Off
    )
    assert _pixmap_contains_color(primary_active, theme.COLORS["accent_ink"])
    assert _pixmap_contains_color(play_active, theme.COLORS["app_background"])
    assert _pixmap_contains_color(danger_active, theme.COLORS["danger_hover"])
    danger_button.deleteLater()

    first_title = window.library_table.item(0, 0)
    assert first_title.toolTip() == first_title.text()
    assert str(fixture.root) not in first_title.toolTip()

    source = QPixmap(400, 300)
    source.fill(QColor("#315E8A"))
    for dpr in (1.25, 1.5):
        artwork = window.rounded_cover_pixmap(source, 156, dpr=dpr)
        assert artwork.width() == math.ceil(156 * dpr)
        assert artwork.height() == math.ceil(156 * dpr)
        assert artwork.devicePixelRatio() == pytest.approx(dpr)
        assert artwork.deviceIndependentSize().width() == pytest.approx(156)
        assert artwork.deviceIndependentSize().height() == pytest.approx(156)

    assert window.autoplay_btn.objectName() == "ModeButtonActive"
    assert window.autoplay_btn.toolTip() == "Autoplay is on"
    assert window.shuffle_btn.toolTip() == "Shuffle is off"
    assert window.repeat_btn.toolTip() == "Repeat is off"

    window.import_btn.setFocus()
    qapp.processEvents()
    unfocused_slider = window.volume_slider.grab()
    window.volume_slider.setFocus(Qt.FocusReason.TabFocusReason)
    qapp.processEvents()
    focused_slider = window.volume_slider.grab()
    assert _pixmap_color_count(
        focused_slider, theme.COLORS["focus_ring"]
    ) > _pixmap_color_count(unfocused_slider, theme.COLORS["focus_ring"])


def test_window_pages_empty_states_and_long_synthetic_names(
    isolated_ui_window,
    qapp,
):
    window = isolated_ui_window.window

    assert window.pages.count() == 3
    assert window.library_table.rowCount() == 18
    assert any(
        window.library_table.item(row, 0).text() == LONG_TITLE
        for row in range(window.library_table.rowCount())
    )
    assert any(
        window.playlists.item(index).text() == LONG_PLAYLIST
        for index in range(window.playlists.count())
    )
    assert window.playlists.textElideMode() == Qt.TextElideMode.ElideRight

    window.pages.setCurrentWidget(window.sync_page)
    qapp.processEvents()
    assert window.sync_page.isVisible()
    assert window.youtube_sync_btn.isVisible()

    window.pages.setCurrentWidget(window.settings_page)
    qapp.processEvents()
    assert window.settings_page.isVisible()
    assert window.settings_scroll.isVisible()

    window.pages.setCurrentWidget(window.library_page)
    window.current_view_kind = "albums"
    window.show_album_browser()
    for _ in range(100):
        qapp.processEvents()
        if window.album_browser_model.rowCount():
            break
        QTest.qWait(10)
    assert window.browser_title.text() == "Albums"
    assert any(
        item.title == LONG_ALBUM for item in window.album_browser_model.items()
    )
    assert not window.findChildren(QObject, "BrowserCard")

    window.current_view_kind = "artists"
    window.show_artist_browser()
    for _ in range(100):
        qapp.processEvents()
        if window.artist_browser_model.rowCount():
            break
        QTest.qWait(10)
    assert window.browser_title.text() == "Artists"
    assert any(
        item.title == LONG_ARTIST for item in window.artist_browser_model.items()
    )
    assert all(
        item.artwork_path is None for item in window.artist_browser_model.items()
    )

    window.load_library([], "Empty Synthetic Playlist", "Synthetic empty state")
    assert window.library_body_stack.currentWidget() is window.library_empty_state

    window.load_library(window.db.list_tracks(), "Library", "Synthetic collection")
    window.search_box.setText("definitely-no-synthetic-match")
    assert window.library_body_stack.currentWidget() is window.search_empty_state
    QTest.keyClick(window.search_box, Qt.Key.Key_Escape)
    assert window.search_box.text() == ""
    assert window.library_body_stack.currentWidget() is window.library_table


def _wait_for_browser_rows(qapp, model, minimum: int = 1) -> None:
    for _ in range(150):
        qapp.processEvents()
        if model.rowCount() >= minimum:
            return
        QTest.qWait(10)
    assert model.rowCount() >= minimum


def test_artist_browser_never_queries_blank_unknown_identity(
    isolated_ui_window,
    qapp,
):
    from music_vault.metadata.artist_images import ArtistImageResult, ArtistImageStatus

    window = isolated_ui_window.window
    calls: list[str] = []

    class OfflineProvider:
        def resolve(self, identity, _cancel_event=None):
            calls.append(identity.display_name)
            return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)

    window.artist_image_service.provider = OfflineProvider()
    window.config["artist_image_fetch_enabled"] = True
    window.pages.setCurrentWidget(window.library_page)
    window.show_artist_browser()
    _wait_for_browser_rows(qapp, window.artist_browser_model)
    blank = next(
        summary
        for summary in window._browser_summary_maps["artists"].values()
        if not summary.key.normalized_name
    )
    window.load_visible_browser_images((blank.browser_key,))
    QTest.qWait(80)
    qapp.processEvents()

    assert "Unknown Artist" not in calls
    assert blank.browser_key not in window._pending_artist_image_keys
    window.config["artist_image_fetch_enabled"] = False
    window.artist_image_service.cancel_all()


def test_artist_result_maps_to_dedicated_photo_not_album_cover(
    isolated_ui_window,
    qapp,
):
    from music_vault.metadata.artist_images import (
        ArtistIdentity,
        ArtistImageResult,
        ArtistImageStatus,
    )

    fixture = isolated_ui_window
    window = fixture.window
    window.pages.setCurrentWidget(window.library_page)
    window.show_artist_browser()
    _wait_for_browser_rows(qapp, window.artist_browser_model)
    summary = next(
        value
        for value in window._browser_summary_maps["artists"].values()
        if value.key.normalized_name
    )
    portrait = fixture.root / "data" / "artist_images" / "files" / "portrait.png"
    portrait.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(64, 64, QImage.Format.Format_ARGB32)
    image.fill(QColor("#1DB954"))
    assert image.save(str(portrait), "PNG")

    window._artist_image_result(
        summary.browser_key,
        ArtistImageResult(
            ArtistImageStatus.RESOLVED,
            ArtistIdentity.from_display_name(f"  {summary.display_name}  "),
            cache_file=portrait,
        ),
    )
    item = window.artist_browser_model.item_for_key(summary.browser_key)
    assert item.artwork_path == str(portrait)
    assert item.has_cached_image is True
    assert item.artwork_path not in {
        album.representative_cover_path
        for album in window._browser_summary_maps["albums"].values()
    }


def test_artist_request_cancellation_resets_all_loading_cards(
    isolated_ui_window,
    qapp,
    monkeypatch,
):
    window = isolated_ui_window.window
    window.pages.setCurrentWidget(window.library_page)
    window.show_artist_browser()
    _wait_for_browser_rows(qapp, window.artist_browser_model, 2)
    keys = [
        summary.browser_key
        for summary in window._browser_summary_maps["artists"].values()
        if summary.key.normalized_name
    ][:2]
    assert len(keys) == 2
    for key in keys:
        window.artist_browser_model.replace_item(
            key,
            artwork_path=None,
            image_state="loading",
        )
    window._pending_artist_image_keys.update(keys)
    monkeypatch.setattr(window.artist_image_service, "clear_cache", lambda _identity: None)
    window.current_view_kind = "library"
    window._active_browser_kind = None

    window.clear_cached_artist_photo(keys[0])

    assert window._pending_artist_image_keys == set()
    assert all(
        window.artist_browser_model.item_for_key(key).image_state.value == "missing"
        for key in keys
    )


def test_disabling_during_artist_refresh_restores_ready_state_and_status(
    isolated_ui_window,
    qapp,
):
    window = isolated_ui_window.window
    window.show_artist_browser()
    _wait_for_browser_rows(qapp, window.artist_browser_model)
    key = next(
        summary.browser_key
        for summary in window._browser_summary_maps["artists"].values()
        if summary.key.normalized_name
    )
    window.artist_browser_model.replace_item(
        key,
        artwork_path="synthetic-cached-artist-photo.png",
        image_state="loading",
        has_cached_image=True,
    )
    window.config["artist_image_fetch_enabled"] = True

    window.on_artist_image_setting_clicked(False)

    assert window.config["artist_image_fetch_enabled"] is False
    assert window.artist_browser_model.item_for_key(key).image_state.value == "ready"
    assert "Disabled" in window.artist_images_status.text()


def test_browser_activation_uses_exact_stable_album_and_artist_keys(
    isolated_ui_window,
    qapp,
):
    from music_vault.core.library_browser import query_album_tracks, query_artist_tracks

    window = isolated_ui_window.window
    window.pages.setCurrentWidget(window.library_page)
    window.show_album_browser()
    _wait_for_browser_rows(qapp, window.album_browser_model)
    album = next(iter(window._browser_summary_maps["albums"].values()))
    expected_album_ids = {row["id"] for row in query_album_tracks(window.db.conn, album.key)}

    window.open_album(album.browser_key)

    actual_album_ids = {
        window.library_table.item(row, 0).data(Qt.UserRole)
        for row in range(window.library_table.rowCount())
    }
    assert actual_album_ids == expected_album_ids
    assert window.current_view_kind == "album_tracks"

    window.show_artist_browser()
    _wait_for_browser_rows(qapp, window.artist_browser_model)
    artist = next(
        value
        for value in window._browser_summary_maps["artists"].values()
        if value.key.normalized_name
    )
    expected_artist_ids = {row["id"] for row in query_artist_tracks(window.db.conn, artist.key)}

    window.open_artist(artist.browser_key)

    actual_artist_ids = {
        window.library_table.item(row, 0).data(Qt.UserRole)
        for row in range(window.library_table.rowCount())
    }
    assert actual_artist_ids == expected_artist_ids
    assert window.current_view_kind == "artist_tracks"


def test_artist_context_actions_are_consent_cache_and_source_gated(
    isolated_ui_window,
    qapp,
    monkeypatch,
):
    window = isolated_ui_window.window
    window.show_artist_browser()
    _wait_for_browser_rows(qapp, window.artist_browser_model)
    artist = next(
        value
        for value in window._browser_summary_maps["artists"].values()
        if value.key.normalized_name
    )
    menus: list[list[str]] = []

    class FakeAction:
        def __init__(self, text):
            self.text = text
            self.triggered = SimpleNamespace(connect=lambda _callback: None)

    class FakeMenu:
        def __init__(self, _parent=None):
            self.texts: list[str] = []

        def addAction(self, _icon, text):
            self.texts.append(text)
            return FakeAction(text)

        def addSeparator(self):
            return None

        def exec(self, _position):
            menus.append(list(self.texts))

    monkeypatch.setattr(isolated_ui_window.app_module, "QMenu", FakeMenu)
    before_changes = window.db.conn.total_changes

    window.config["artist_image_fetch_enabled"] = False
    window.show_browser_context_menu(artist.browser_key, QPoint())
    assert menus[-1] == ["Open Artist"]

    window.config["artist_image_fetch_enabled"] = True
    window.show_browser_context_menu(artist.browser_key, QPoint())
    assert menus[-1] == ["Open Artist", "Refresh Artist Photo"]

    window.artist_browser_model.replace_item(
        artist.browser_key,
        has_cached_image=True,
        source_url="https://en.wikipedia.org/wiki/Synthetic_artist",
    )
    window.show_browser_context_menu(artist.browser_key, QPoint())
    assert menus[-1] == [
        "Open Artist",
        "Refresh Artist Photo",
        "Clear Cached Artist Photo",
        "View Image Source",
    ]
    assert window.db.conn.total_changes == before_changes


def test_accepted_artist_photo_consent_persists_without_credentials(
    isolated_ui_window,
    monkeypatch,
):
    fixture = isolated_ui_window
    window = fixture.window
    monkeypatch.setattr(
        fixture.app_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: fixture.app_module.QMessageBox.Yes,
    )
    monkeypatch.setattr(window, "load_visible_browser_images", lambda _keys: None)
    window.config["artist_image_fetch_enabled"] = False

    window.confirm_enable_artist_photos()

    saved = json.loads(
        (fixture.root / "data" / "music_vault_config.json").read_text(encoding="utf-8")
    )
    assert saved["artist_image_fetch_enabled"] is True
    assert window.settings_artist_images_enabled.isChecked()
    assert not any("api_key" in key.casefold() for key in saved)
    assert not (fixture.root / "data" / "youtube_api_key.txt").exists()


def test_window_preserves_now_playing_selection_modes_queue_and_volume(
    isolated_ui_window,
    qapp,
):
    fixture = isolated_ui_window
    window = fixture.window
    window.pages.setCurrentWidget(window.library_page)
    window.load_library()

    playing_id, browsing_id = fixture.track_ids[:2]
    playing_row = window.update_now_playing_indicator(
        playing_id,
        select_if_visible=False,
        scroll_if_visible=False,
    )
    browsing_row = window.locate_track_row_in_table(browsing_id)
    window.library_table.selectRow(browsing_row)

    assert playing_row != browsing_row
    assert window.library_table.item(playing_row, 0).data(fixture.app_module.NOW_PLAYING_ROLE) is True
    assert window.library_table.currentRow() == browsing_row
    assert window.current_track_id == playing_id

    window.manual_queue[:] = fixture.track_ids[2:4]
    window.update_queue_label()
    assert window.queue_label.text() == "Q: 2"

    assert window.autoplay_enabled is True
    assert window.shuffle_enabled is False
    window.toggle_shuffle()
    assert window.shuffle_enabled is True
    assert window.autoplay_enabled is False
    assert window.shuffle_btn.objectName() == "ModeButtonActive"
    window.toggle_autoplay()
    assert window.autoplay_enabled is True
    assert window.shuffle_enabled is False
    assert window.autoplay_btn.objectName() == "ModeButtonActive"

    window.repeat_mode = "one"
    window.update_playback_mode_buttons()
    assert window.repeat_btn.objectName() == "ModeButtonActive"
    assert "one" in window.repeat_btn.toolTip().casefold()

    window.on_playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
    assert window.play_btn.accessibleName() == "Pause"
    assert not window.play_btn.icon().isNull()
    window.on_playback_state_changed(QMediaPlayer.PlaybackState.StoppedState)
    assert window.play_btn.accessibleName() == "Play"
    assert not window.play_btn.icon().isNull()

    assert window.volume_slider.value() == 23
    assert window.config["volume_percent"] == 23
    assert not (fixture.root / "data" / "youtube_api_key.txt").exists()
    assert paths.database_path().resolve().is_relative_to(fixture.root.resolve())


def test_batch8_release_settings_are_visible_without_exposing_a_key(
    isolated_ui_window,
):
    fixture = isolated_ui_window
    window = fixture.window
    window.pages.setCurrentWidget(window.settings_page)
    window.refresh_settings_status()

    assert window.windowTitle() == "Music Vault v1.0.0"
    assert "Music Vault v1.0.0" in window.release_status.text()
    assert "Release Channel: stable" in window.release_status.text()
    assert "Runtime Data:" in window.runtime_data_status.text()
    assert str(fixture.root.resolve()) in window.runtime_data_status.text()
    assert window.change_ffmpeg_btn.accessibleName() == "Change FFmpeg Location"
    assert window.shortcut_btn.accessibleName() == "Create or Update Shortcut"
    assert window.reopen_guide_btn.accessibleName() == "Reopen First-Run Guide"
    assert window.settings_api_key.echoMode() == fixture.app_module.QLineEdit.Password
    assert window.settings_api_key.text() == ""


def test_ui_review_hook_is_inert_without_explicit_environment(monkeypatch):
    monkeypatch.delenv("MUSIC_VAULT_UI_REVIEW", raising=False)
    monkeypatch.delenv("MUSIC_VAULT_UI_REVIEW_PLAN", raising=False)

    from music_vault.ui.review import schedule_ui_review

    class MustRemainUntouched:
        def __getattr__(self, name):
            raise AssertionError(f"inactive review hook accessed {name}")

    result = schedule_ui_review(MustRemainUntouched(), MustRemainUntouched())
    assert result in (None, False)


def test_ui_review_plan_validation_accepts_only_safe_explicit_matrix(tmp_path: Path):
    from music_vault.ui.review import (
        DEFAULT_REVIEW_SCENES,
        ReviewPlanError,
        load_review_plan,
    )

    runtime = tmp_path / "synthetic_runtime"
    output = tmp_path / "review_output"
    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    plan_path = runtime / "review-plan.json"
    payload = {
        "schema_version": 1,
        "runtime_root": str(runtime.resolve()),
        "output_dir": str(output.resolve()),
        "sizes": [
            {"width": 1100, "height": 720},
            {"width": 1440, "height": 900},
            {"width": 1920, "height": 1080},
        ],
        "scenes": list(DEFAULT_REVIEW_SCENES),
        "settle_ms": 100,
        "expected_capture_count": 3 * len(DEFAULT_REVIEW_SCENES),
    }
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    plan = load_review_plan(plan_path)
    assert plan.runtime_root == runtime.resolve()
    assert plan.output_dir == output.resolve()
    assert plan.capture_count == 3 * len(DEFAULT_REVIEW_SCENES)
    assert [(size.width, size.height) for size in plan.sizes] == [
        (1100, 720),
        (1440, 900),
        (1920, 1080),
    ]

    with pytest.raises(ReviewPlanError):
        load_review_plan("relative-review-plan.json")

    unsafe_output = dict(payload, output_dir=str((runtime / "screenshots").resolve()))
    plan_path.write_text(json.dumps(unsafe_output), encoding="utf-8")
    with pytest.raises(ReviewPlanError, match="outside the disposable runtime"):
        load_review_plan(plan_path)

    unsupported_scene = dict(payload, scenes=["library", "network_sync"])
    plan_path.write_text(json.dumps(unsupported_scene), encoding="utf-8")
    with pytest.raises(ReviewPlanError, match="unsupported scene"):
        load_review_plan(plan_path)

    malformed_size = dict(payload, sizes=[{"width": True, "height": 720}])
    malformed_size.pop("expected_capture_count")
    plan_path.write_text(json.dumps(malformed_size), encoding="utf-8")
    with pytest.raises(ReviewPlanError, match="dimensions must be integers"):
        load_review_plan(plan_path)
