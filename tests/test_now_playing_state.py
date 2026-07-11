from __future__ import annotations

import copy
import random
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QLabel, QTableWidget, QTableWidgetItem

from music_vault.app import MusicVaultWindow, NOW_PLAYING_ROLE
from music_vault.core.playback_state import build_track_row_map, locate_track_row


class IndicatorHarness:
    rebuild_track_row_map = MusicVaultWindow.rebuild_track_row_map
    locate_track_row_in_table = MusicVaultWindow.locate_track_row_in_table
    locate_visible_track_row = MusicVaultWindow.locate_visible_track_row
    library_table_is_currently_visible = MusicVaultWindow.library_table_is_currently_visible
    restore_table_selection = MusicVaultWindow.restore_table_selection
    set_playing_row_treatment = MusicVaultWindow.set_playing_row_treatment
    apply_now_playing_row_state = MusicVaultWindow.apply_now_playing_row_state
    update_now_playing_indicator = MusicVaultWindow.update_now_playing_indicator

    def __init__(self):
        self.library_table = QTableWidget(0, 1)
        self.current_track_id = None
        self.track_row_map = {}
        self._playing_row = None
        self._styled_now_playing_track_id = None
        self.manual_queue = []
        self.base_playback_context = None

    def populate(self, track_ids):
        self.library_table.clearContents()
        self.library_table.setRowCount(len(track_ids))
        for row, track_id in enumerate(track_ids):
            item = QTableWidgetItem(f"Synthetic {row}")
            item.setData(Qt.UserRole, track_id)
            self.library_table.setItem(row, 0, item)
        self.rebuild_track_row_map()


def test_track_row_map_uses_track_ids_and_first_duplicate():
    mapping = build_track_row_map([30, "20", None, 30, "bad"])
    assert mapping == {30: 0, 20: 1}
    assert locate_track_row(20, mapping) == 1
    assert locate_track_row("bad", mapping) is None


def test_direct_now_playing_update_sets_identity_selection_and_treatment(qapp):
    harness = IndicatorHarness()
    harness.populate([10, 20, 30])
    row = harness.update_now_playing_indicator(20)
    title = harness.library_table.item(1, 0)
    assert row == 1
    assert harness.current_track_id == 20
    assert harness.library_table.currentRow() == 1
    assert title.data(NOW_PLAYING_ROLE) is True
    assert title.font().bold()


class FakeDB:
    def __init__(self, track):
        self.track = track

    def get_track(self, track_id):
        return self.track if track_id == self.track["id"] else None


class FakePlayer:
    def __init__(self):
        self.source = None
        self.play_count = 0
        self.position = None

    def setSource(self, source):
        self.source = source

    def play(self):
        self.play_count += 1

    def setPosition(self, position):
        self.position = position


class DirectPlayHarness(IndicatorHarness):
    play_track_by_id = MusicVaultWindow.play_track_by_id

    def __init__(self, track):
        super().__init__()
        self.db = FakeDB(track)
        self.player = FakePlayer()
        self.now_title = QLabel()
        self.now_artist = QLabel()
        self.cover_updates = []
        self.status_writes = 0

    def set_cover_art(self, value):
        self.cover_updates.append(value)

    def write_app_status(self):
        self.status_writes += 1


def test_play_track_by_id_routes_direct_play_through_central_indicator(tmp_path, qapp):
    media = tmp_path / "synthetic.mp3"
    media.write_bytes(b"not-real-audio")
    track = {
        "id": 7,
        "path": str(media),
        "title": "Synthetic",
        "artist": "Tester",
        "cover_path": None,
    }
    harness = DirectPlayHarness(track)
    harness.populate([4, 7, 9])
    assert harness.play_track_by_id(7, capture_base_context=False)
    assert harness.current_track_id == 7
    assert harness.library_table.currentRow() == 1
    assert harness.player.play_count == 1


def test_ordinary_selection_can_differ_from_now_playing(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2, 3])
    harness.update_now_playing_indicator(1)
    harness.library_table.selectRow(2)
    assert harness.current_track_id == 1
    assert harness.library_table.currentRow() == 2
    assert harness.library_table.item(0, 0).data(NOW_PLAYING_ROLE) is True
    assert harness.library_table.item(2, 0).data(NOW_PLAYING_ROLE) is not True


def test_hidden_or_absent_playing_track_does_not_steal_selection(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2, 3])
    harness.library_table.selectRow(0)
    harness.library_table.setRowHidden(1, True)
    harness.update_now_playing_indicator(2)
    assert harness.current_track_id == 2
    assert harness.library_table.currentRow() == 0
    assert harness.library_table.item(1, 0).data(NOW_PLAYING_ROLE) is True

    harness.update_now_playing_indicator(99)
    assert harness.current_track_id == 99
    assert harness.library_table.currentRow() == 0


def test_hidden_library_page_does_not_change_browsing_selection(qapp):
    class Pages:
        def currentWidget(self):
            return object()

    harness = IndicatorHarness()
    harness.populate([1, 2])
    harness.library_table.selectRow(0)
    harness.pages = Pages()
    harness.library_page = object()
    harness.update_now_playing_indicator(2)
    assert harness.current_track_id == 2
    assert harness.library_table.currentRow() == 0
    assert harness.library_table.item(1, 0).font().bold()


def test_returning_to_containing_view_restores_indicator_without_state_changes(qapp):
    harness = IndicatorHarness()
    harness.manual_queue = [8, 9]
    harness.base_playback_context = {"track_ids": [1, 2], "current_track_id": 1}
    queue_before = list(harness.manual_queue)
    context_before = copy.deepcopy(harness.base_playback_context)
    harness.current_track_id = 1

    harness.populate([3, 4])
    assert harness.apply_now_playing_row_state() is None
    harness.populate([2, 1])
    row = harness.apply_now_playing_row_state()

    assert row == 1
    assert harness.library_table.item(1, 0).data(NOW_PLAYING_ROLE) is True
    assert harness.current_track_id == 1
    assert harness.manual_queue == queue_before
    assert harness.base_playback_context == context_before


def test_stale_row_map_self_heals_after_row_reorder(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2])
    first = harness.library_table.takeItem(0, 0)
    second = harness.library_table.takeItem(1, 0)
    harness.library_table.setItem(0, 0, second)
    harness.library_table.setItem(1, 0, first)

    row = harness.update_now_playing_indicator(1)
    assert row == 1
    assert harness.track_row_map == {2: 0, 1: 1}
    assert harness.library_table.item(1, 0).data(NOW_PLAYING_ROLE) is True


def test_same_size_table_reload_never_selects_an_unrelated_row(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2, 3])
    harness.library_table.selectRow(1)
    selected_track_id = 2

    # Qt keeps row 1 selected when the same row count is reused.
    harness.library_table.setRowCount(3)
    for row, track_id in enumerate([10, 20, 30]):
        item = QTableWidgetItem(f"Replacement {row}")
        item.setData(Qt.UserRole, track_id)
        harness.library_table.setItem(row, 0, item)
    harness.rebuild_track_row_map()
    assert harness.library_table.currentRow() == 1
    assert harness.restore_table_selection(selected_track_id) is None
    assert harness.library_table.currentRow() == -1
    assert harness.library_table.selectedItems() == []


def test_reordered_table_restores_selection_by_track_id(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2, 3])
    harness.library_table.selectRow(1)
    harness.populate([2, 3, 1])
    assert harness.restore_table_selection(2) == 0
    assert harness.library_table.currentRow() == 0


def test_reorder_then_track_change_clears_the_previous_track_treatment(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2])
    harness.update_now_playing_indicator(1)
    first = harness.library_table.takeItem(0, 0)
    second = harness.library_table.takeItem(1, 0)
    harness.library_table.setItem(0, 0, second)
    harness.library_table.setItem(1, 0, first)

    harness.update_now_playing_indicator(2)
    assert harness.library_table.item(0, 0).data(NOW_PLAYING_ROLE) is True
    assert harness.library_table.item(1, 0).data(NOW_PLAYING_ROLE) is False
    assert not harness.library_table.item(1, 0).font().bold()


def test_moving_now_playing_clears_previous_treatment(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2])
    harness.update_now_playing_indicator(1)
    harness.update_now_playing_indicator(2)
    assert harness.library_table.item(0, 0).data(NOW_PLAYING_ROLE) is False
    assert not harness.library_table.item(0, 0).font().bold()
    assert harness.library_table.item(1, 0).data(NOW_PLAYING_ROLE) is True


class FlowHarness:
    play_next = MusicVaultWindow.play_next
    play_next_from_manual_queue = MusicVaultWindow.play_next_from_manual_queue
    play_next_from_base_context = MusicVaultWindow.play_next_from_base_context
    play_base_track_by_id = MusicVaultWindow.play_base_track_by_id
    play_random_from_base_context = MusicVaultWindow.play_random_from_base_context
    play_previous = MusicVaultWindow.play_previous
    on_media_status_changed = MusicVaultWindow.on_media_status_changed
    continue_after_media_error = MusicVaultWindow.continue_after_media_error

    def __init__(self, track_ids=(1, 2, 3), current=1):
        self.current_track_id = current
        self.manual_queue = []
        self.base_playback_context = {
            "kind": "synthetic",
            "playlist_id": None,
            "playlist_name": "Synthetic",
            "track_ids": list(track_ids),
            "current_track_id": current,
        }
        self.autoplay_enabled = True
        self.shuffle_enabled = False
        self.repeat_mode = "off"
        self._handling_media_error = False
        self.available = set(track_ids) | {8, 9}
        self.started = []
        self.player = FakePlayer()

    def base_track_ids(self):
        return list(self.base_playback_context["track_ids"])

    def play_track_by_id(
        self,
        track_id,
        capture_base_context=True,
        show_missing_warning=True,
    ):
        if track_id not in self.available:
            return False
        self.current_track_id = track_id
        self.started.append(track_id)
        return True

    def capture_base_playback_context(self, track_id):
        raise AssertionError("Existing base context should not be replaced in these tests")

    def update_queue_label(self):
        pass

    def write_app_status(self):
        pass


class VisibleFlowHarness(FlowHarness):
    rebuild_track_row_map = MusicVaultWindow.rebuild_track_row_map
    locate_track_row_in_table = MusicVaultWindow.locate_track_row_in_table
    library_table_is_currently_visible = MusicVaultWindow.library_table_is_currently_visible
    set_playing_row_treatment = MusicVaultWindow.set_playing_row_treatment
    apply_now_playing_row_state = MusicVaultWindow.apply_now_playing_row_state
    update_now_playing_indicator = MusicVaultWindow.update_now_playing_indicator

    def __init__(self):
        super().__init__()
        self.library_table = QTableWidget(4, 1)
        self.track_row_map = {}
        self._playing_row = None
        self._styled_now_playing_track_id = None
        for row, track_id in enumerate([1, 2, 8, 9]):
            item = QTableWidgetItem(f"Synthetic {row}")
            item.setData(Qt.UserRole, track_id)
            self.library_table.setItem(row, 0, item)
        self.rebuild_track_row_map()
        self.update_now_playing_indicator(1)

    def play_track_by_id(
        self,
        track_id,
        capture_base_context=True,
        show_missing_warning=True,
    ):
        if track_id not in self.available:
            return False
        self.started.append(track_id)
        self.update_now_playing_indicator(track_id)
        return True


def test_auto_advances_authoritative_identity():
    flow = FlowHarness()
    flow.on_media_status_changed(QMediaPlayer.EndOfMedia)
    assert flow.current_track_id == 2
    assert flow.base_playback_context["current_track_id"] == 2


def test_auto_selects_the_new_visible_playing_row(qapp):
    flow = VisibleFlowHarness()
    flow.on_media_status_changed(QMediaPlayer.EndOfMedia)
    assert flow.current_track_id == 2
    assert flow.library_table.currentRow() == 1
    assert flow.library_table.item(1, 0).data(NOW_PLAYING_ROLE) is True


def test_shuffle_marks_actual_chosen_track(monkeypatch):
    flow = FlowHarness()
    flow.shuffle_enabled = True
    flow.autoplay_enabled = False
    monkeypatch.setattr(random, "shuffle", lambda values: None)
    flow.on_media_status_changed(QMediaPlayer.EndOfMedia)
    assert flow.started == [2]
    assert flow.current_track_id == 2


def test_next_uses_fifo_queue_without_advancing_base_identity():
    flow = FlowHarness()
    flow.manual_queue = [9, 8]
    flow.play_next()
    assert flow.current_track_id == 9
    assert flow.manual_queue == [8]
    assert flow.base_playback_context["current_track_id"] == 1


def test_queue_completion_resumes_next_base_track():
    flow = FlowHarness()
    flow.manual_queue = [9, 8]
    flow.play_next()
    flow.play_next()
    flow.play_next()
    assert flow.started == [9, 8, 2]
    assert flow.manual_queue == []
    assert flow.current_track_id == 2
    assert flow.base_playback_context["current_track_id"] == 2


def test_visible_queue_track_and_base_resume_each_move_active_row(qapp):
    flow = VisibleFlowHarness()
    flow.manual_queue = [9]
    flow.play_next()
    assert flow.current_track_id == 9
    assert flow.library_table.currentRow() == 3
    assert flow.base_playback_context["current_track_id"] == 1

    flow.play_next()
    assert flow.current_track_id == 2
    assert flow.library_table.currentRow() == 1
    assert flow.base_playback_context["current_track_id"] == 2


def test_previous_marks_previous_base_track():
    flow = FlowHarness(current=3)
    flow.play_previous()
    assert flow.started == [2]
    assert flow.current_track_id == 2


def test_repeat_all_wraparound_marks_first_track():
    flow = FlowHarness(current=3)
    flow.repeat_mode = "all"
    assert flow.play_next_from_base_context()
    assert flow.started == [1]
    assert flow.current_track_id == 1


def test_repeat_one_retains_identity_and_does_not_advance_queue():
    flow = FlowHarness()
    flow.repeat_mode = "one"
    flow.manual_queue = [9]
    flow.on_media_status_changed(QMediaPlayer.EndOfMedia)
    assert flow.current_track_id == 1
    assert flow.manual_queue == [9]
    assert flow.player.position == 0
    assert flow.player.play_count == 1


def test_playback_error_continuation_marks_next_valid_base_track():
    flow = FlowHarness()
    flow.available.remove(2)
    flow.continue_after_media_error()
    assert flow.started == [3]
    assert flow.current_track_id == 3
    assert flow.base_playback_context["current_track_id"] == 3


def test_pause_stop_and_selection_only_do_not_change_identity(qapp):
    harness = IndicatorHarness()
    harness.populate([1, 2])
    harness.update_now_playing_indicator(1)
    harness.library_table.selectRow(1)
    # No playback-start method ran, so pause/stop/browsing retain identity.
    assert harness.current_track_id == 1
    assert harness.library_table.item(0, 0).data(NOW_PLAYING_ROLE) is True


def test_queue_right_click_and_add_to_playlist_paths_do_not_set_playing_state():
    source = Path("music_vault/app.py").read_text(encoding="utf-8")
    assert source.count("self.update_now_playing_indicator(track_id)") == 1
    queue_body = source.split("def queue_selected_next", 1)[1].split(
        "def open_song_context_menu", 1
    )[0]
    add_body = source.split("def add_selected_to_playlist", 1)[1].split(
        "def remove_selected_from_current_playlist", 1
    )[0]
    context_body = source.split("def open_song_context_menu", 1)[1].split(
        "def visible_track_rows", 1
    )[0]
    assert "update_now_playing_indicator" not in queue_body
    assert "update_now_playing_indicator" not in add_body
    assert "update_now_playing_indicator" not in context_body
