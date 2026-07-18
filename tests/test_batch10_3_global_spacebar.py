from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QWidget,
)

from music_vault.app import (
    MusicVaultWindow,
    _GlobalPlayPauseEventFilter,
    should_handle_global_play_pause,
)


class _Source:
    def __init__(self, empty: bool) -> None:
        self._empty = empty

    def isEmpty(self) -> bool:
        return self._empty


class _Player:
    def __init__(self, *, empty: bool) -> None:
        self._source = _Source(empty)

    def source(self) -> _Source:
        return self._source


@pytest.mark.parametrize(
    "widget_factory",
    [
        QLineEdit,
        QTextEdit,
        QPlainTextEdit,
        QSpinBox,
        QCheckBox,
        QPushButton,
        QSlider,
    ],
)
def test_global_spacebar_preserves_focused_editors_and_controls(
    qapp, widget_factory
):
    widget = widget_factory()
    try:
        assert should_handle_global_play_pause(widget) is False
    finally:
        widget.deleteLater()


def test_global_spacebar_preserves_combo_but_handles_page(qapp):
    combo = QComboBox()
    page = QWidget()
    try:
        assert should_handle_global_play_pause(combo) is False
        combo.setEditable(True)
        assert should_handle_global_play_pause(combo.lineEdit()) is False
        assert should_handle_global_play_pause(page) is True
        assert should_handle_global_play_pause(None) is True
    finally:
        combo.deleteLater()
        page.deleteLater()


def test_global_spacebar_is_suppressed_by_modal_and_party_mode(qapp):
    page = QWidget()
    modal = QDialog()
    try:
        assert (
            should_handle_global_play_pause(page, active_modal_widget=modal) is False
        )
        assert should_handle_global_play_pause(page, party_mode_active=True) is False
    finally:
        page.deleteLater()
        modal.deleteLater()


def test_global_spacebar_protects_active_item_editor(qapp):
    table = QTableWidget()
    try:
        assert should_handle_global_play_pause(table) is True
        table.setState(QAbstractItemView.EditingState)
        assert should_handle_global_play_pause(table) is False
    finally:
        table.deleteLater()


def test_global_spacebar_toggles_only_an_already_loaded_source():
    calls: list[str] = []
    loaded = SimpleNamespace(
        player=_Player(empty=False),
        toggle_play=lambda: calls.append("toggle"),
    )
    empty = SimpleNamespace(
        player=_Player(empty=True),
        toggle_play=lambda: calls.append("unexpected"),
    )

    assert MusicVaultWindow.toggle_loaded_playback_from_global_shortcut(loaded)
    assert calls == ["toggle"]
    assert not MusicVaultWindow.toggle_loaded_playback_from_global_shortcut(empty)
    assert calls == ["toggle"]


def test_global_spacebar_event_filter_preserves_native_control_space(qapp):
    calls: list[str] = []
    window = QWidget()
    checkbox = QCheckBox("Synthetic control", window)
    page = QWidget(window)
    page.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    page.setGeometry(0, 40, 100, 50)
    event_filter = _GlobalPlayPauseEventFilter(
        lambda: calls.append("toggle"), lambda: False, window
    )
    qapp.installEventFilter(event_filter)
    try:
        window.show()
        checkbox.setFocus()
        QTest.qWait(10)
        QTest.keyClick(checkbox, Qt.Key.Key_Space)
        assert checkbox.isChecked()
        assert calls == []

        page.setFocus()
        QTest.keyClick(page, Qt.Key.Key_Space)
        assert calls == ["toggle"]
    finally:
        qapp.removeEventFilter(event_filter)
        window.close()
        window.deleteLater()


def test_global_spacebar_event_filter_and_accessibility_are_wired_once():
    source = __import__("inspect").getsource(MusicVaultWindow.__init__)
    player_bar = __import__("inspect").getsource(MusicVaultWindow.build_player_bar)

    assert "self.global_play_pause_event_filter = _GlobalPlayPauseEventFilter" in source
    assert "installEventFilter" in source
    assert "self.on_global_play_pause_shortcut" in source
    assert "Play or pause (Space)" in player_bar
    assert 'setProperty("accessibleShortcut", "Space")' in player_bar
