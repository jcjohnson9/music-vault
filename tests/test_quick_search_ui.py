"""Synthetic Qt interactions for the local, signal-only search surface."""

import html

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from music_vault.core.quick_search import LocalSearchIndex, SearchEntity
from music_vault.ui.quick_search import QuickSearchDialog


def track(track_id, label, detail=""):
    return SearchEntity("track", str(track_id), label, detail, track_id=track_id)


@pytest.fixture
def dialogs(qapp):
    created = []

    def create(entities=(), **kwargs):
        dialog = QuickSearchDialog(LocalSearchIndex(entities), **kwargs)
        created.append(dialog)
        return dialog

    yield create
    for dialog in created:
        dialog.reject()
        dialog.deleteLater()
    qapp.processEvents()


def search(dialog, query):
    dialog.query_edit.setText(query)
    dialog.refresh_results()


def show(dialog, qapp):
    dialog.show()
    dialog.activateWindow()
    qapp.processEvents()


def capture(dialog):
    requests = []
    dialog.action_requested.connect(lambda *args: requests.append(args))
    return requests


def test_debounce_coalesces_changes_and_disables_stale_controls(dialogs, monkeypatch):
    dialog = dialogs([track(1, "Morning"), track(2, "Moonlight")])
    search(dialog, "morning")
    calls = []
    original = dialog._index.search

    def counted(query, **kwargs):
        calls.append(query)
        return original(query, **kwargs)

    monkeypatch.setattr(dialog._index, "search", counted)
    dialog.query_edit.setText("m")
    dialog.query_edit.setText("moon")
    assert calls == []
    assert dialog._debounce.isActive()
    assert not dialog.results_list.isEnabled()
    assert not dialog.primary_button.isEnabled()
    assert not dialog.queue_button.isEnabled()
    assert not dialog.more_button.isEnabled()
    QTest.qWait(dialog.DEBOUNCE_MS + 100)
    assert calls == ["moon"]
    assert dialog.selected_entity().track_id == 2
    assert dialog.results_list.isEnabled()
    assert dialog.primary_button.isEnabled()


def test_results_are_bounded_and_group_headers_are_not_selectable(dialogs):
    entities = [track(i, f"Match {i:03d}") for i in range(1, 81)]
    entities.extend(SearchEntity(kind, kind, "Match") for kind in ("album", "artist", "playlist", "action"))
    dialog = dialogs(entities)
    search(dialog, "match")
    assert len(dialog.results) == dialog.RESULT_LIMIT
    headers = []
    for row in range(dialog.results_list.count()):
        item = dialog.results_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole) is None:
            headers.append(item.text())
            assert item.flags() == Qt.ItemFlag.NoItemFlags
    assert headers == ["Tracks", "Albums", "Artists", "Playlists", "Actions"]
    assert dialog.results_list.count() == dialog.RESULT_LIMIT + 5


def test_keyboard_moves_over_headers_without_moving_query_focus(dialogs, qapp):
    first, second = track(1, "Match A"), track(2, "Match B")
    album = SearchEntity("album", "album", "Match C")
    dialog = dialogs([first, second, album])
    show(dialog, qapp)
    search(dialog, "match")
    assert dialog.query_edit.hasFocus()
    assert dialog.selected_entity() == first
    for expected in (second, album, album):
        QTest.keyClick(dialog.query_edit, Qt.Key.Key_Down)
        assert dialog.selected_entity() == expected
    QTest.keyClick(dialog.query_edit, Qt.Key.Key_Up)
    assert dialog.selected_entity() == second
    assert dialog.query_edit.hasFocus()


@pytest.mark.parametrize("focus_name", ["query_edit", "results_list"])
def test_enter_emits_explicit_ranked_track_context_and_closes(dialogs, qapp, focus_name):
    dialog = dialogs([track(8, "Match Beta"), track(3, "Match Alpha")])
    requests = capture(dialog)
    show(dialog, qapp)
    search(dialog, "match")
    QTest.keyClick(getattr(dialog, focus_name), Qt.Key.Key_Down)
    QTest.keyClick(getattr(dialog, focus_name), Qt.Key.Key_Return)
    assert requests == [("play_track", track(8, "Match Beta"), (3, 8))]
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.isVisible()


def test_ctrl_enter_and_button_only_request_fifo_append_and_keep_search_open(dialogs, qapp):
    dialog = dialogs([track(1, "Match A"), track(2, "Match B")])
    requests = capture(dialog)
    show(dialog, qapp)
    search(dialog, "match")
    QTest.keyClick(dialog.query_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(dialog.query_edit, Qt.Key.Key_Down)
    dialog.queue_button.click()
    assert [request[0] for request in requests] == ["queue_track", "queue_track"]
    assert [request[1].track_id for request in requests] == [1, 2]
    assert all(request[2] == (1, 2) for request in requests)
    assert dialog.isVisible()
    # No player or database is supplied: the surface emits only host requests.
    assert dialog._index.recent_count == 0


@pytest.mark.parametrize("action", ["play_track", "queue_track"])
def test_pending_query_is_resolved_before_dispatch_not_previous_selection(dialogs, action):
    dialog = dialogs([track(1, "Morning"), track(2, "Midnight")])
    requests = capture(dialog)
    search(dialog, "morning")
    dialog.query_edit.setText("midnight")
    dialog._dispatch(action)
    assert requests == [(action, track(2, "Midnight"), (2,))]
    assert not dialog._debounce.isActive()


@pytest.mark.parametrize("kind,expected", [("album", "open_entity"), ("artist", "open_entity"), ("playlist", "open_entity"), ("action", "invoke_action")])
def test_non_track_primary_dispatches_host_action_without_playback_context(dialogs, kind, expected):
    entity = SearchEntity(kind, "key", "Destination")
    dialog = dialogs([entity])
    requests = capture(dialog)
    search(dialog, "destination")
    assert not dialog.queue_button.isEnabled()
    assert not dialog.more_button.isEnabled()
    dialog._dispatch("queue_track")
    assert requests == []
    dialog.primary_button.click()
    assert requests == [(expected, entity, ())]


def test_metadata_remains_literal_in_rows_accessibility_and_escaped_tooltips(dialogs):
    title = '<b>Match & "More"</b>'
    detail = '<img src="https://example.invalid/tracker">\nLiteral artist'
    entity = track(1, title, detail)
    dialog = dialogs([entity])
    search(dialog, "match")
    item = dialog.results_list.currentItem()
    literal = title + "\n" + detail
    assert item.text() == literal
    assert item.data(Qt.ItemDataRole.AccessibleTextRole) == literal
    assert item.toolTip() == "<qt>" + html.escape(literal).replace("\n", "<br>") + "</qt>"
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in dialog.findChildren(QLabel))


def test_modal_query_accepts_spaces_and_escape_closes_without_action(dialogs, qapp):
    dialog = dialogs([track(1, "Match Moon")])
    requests = capture(dialog)
    show(dialog, qapp)
    assert QApplication.activeModalWidget() is dialog
    assert dialog.query_edit.hasFocus()
    QTest.keyClicks(dialog.query_edit, "Match Moon")
    assert dialog.query_edit.text() == "Match Moon"
    assert requests == []
    QTest.keyClick(dialog.query_edit, Qt.Key.Key_Escape)
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog._debounce.isActive()
    assert requests == []


def test_empty_or_missing_track_id_never_emits_track_action(dialogs):
    dialog = dialogs([SearchEntity("track", "missing", "Missing")])
    requests = capture(dialog)
    search(dialog, "missing")
    assert not dialog.primary_button.isEnabled()
    assert not dialog.queue_button.isEnabled()
    dialog._activate_primary()
    dialog._dispatch("queue_track")
    search(dialog, "no matches at all")
    dialog._activate_primary()
    assert dialog.selected_entity() is None
    assert requests == []


def test_index_replacement_preserves_identity_not_stale_payload_or_row(dialogs):
    first = track(1, "Match A")
    second = track(2, "Match B")
    dialog = dialogs([first, second])
    search(dialog, "match")
    dialog._move_selection(1)
    replacement = track(2, "Match Updated")
    dialog.set_index(LocalSearchIndex([track(3, "Match C"), replacement]))
    assert dialog.selected_entity() == replacement
    assert dialog.results == (track(3, "Match C"), replacement)


@pytest.mark.parametrize("action", ["go_artist", "go_album", "add_to_playlist"])
def test_more_actions_keep_entity_and_ordered_ids_for_host_resolution(dialogs, action):
    entity = track(7, "Match")
    dialog = dialogs([entity])
    requests = capture(dialog)
    search(dialog, "match")
    menu_actions = dict(zip(("go_artist", "go_album", "add_to_playlist"), dialog.more_button.menu().actions()))
    menu_actions[action].trigger()
    assert requests == [(action, entity, (7,))]
