from copy import deepcopy
import pytest

from PySide6.QtCore import Qt

from music_vault.core.navigation import Route
from music_vault.core.quick_search import SearchEntity
from music_vault.ui.media_grid import MediaRole
from test_ui_system import isolated_ui_window


def test_navigation_restores_filter_selection_and_sort_without_changing_playback(isolated_ui_window):
    window = isolated_ui_window.window
    table = window.library_table
    selected = table.track_id_at(1)
    table.select_track(selected)
    table.set_sort(0, Qt.DescendingOrder)
    window.search_box.setText("Synthetic")
    before_ids = table.visible_track_ids()
    window.manual_queue[:] = [selected]
    window.capture_base_playback_context(selected)
    before_context = deepcopy(window.base_playback_context)
    window.listening.navigate(Route("downloaded", label="Downloaded"))
    assert window.search_box.text() == ""
    window.listening.back()
    assert window.search_box.text() == "Synthetic"
    assert table.visible_track_ids() == before_ids
    assert table.current_track_id() == selected
    assert table.proxy_model.sortOrder() == Qt.DescendingOrder
    assert window.manual_queue == [selected]
    assert window.base_playback_context == before_context
    window.listening.forward()
    assert window.current_view_kind == "downloaded"


def test_play_from_search_has_explicit_context_and_preserves_manual_queue(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    ids = isolated_ui_window.track_ids[:4]
    calls = []
    def play(track_id, **kwargs):
        calls.append((track_id, kwargs))
        window.current_track_id = track_id
        return True
    monkeypatch.setattr(window, "play_track_by_id", play)
    window.manual_queue[:] = [ids[3]]
    entity = SearchEntity("track", str(ids[1]), "Synthetic result", track_id=ids[1])
    window.listening.search_action("play_track", entity, (ids[2], ids[1], ids[0]))
    assert calls == [(ids[1], {"capture_base_context": False})]
    assert window.base_playback_context["track_ids"] == [ids[2], ids[1], ids[0]]
    assert window.base_playback_context["current_track_id"] == ids[1]
    assert window.manual_queue == [ids[3]]
    window.listening.return_to_context()
    assert window.current_view_kind == "search"
    assert window.visible_track_ids() == [ids[2], ids[1], ids[0]]


def test_queue_surface_edits_exact_occurrence_and_never_changes_memberships(isolated_ui_window):
    window = isolated_ui_window.window
    ids = isolated_ui_window.track_ids[:2]
    before = tuple(tuple(row) for row in window.db.conn.execute("SELECT * FROM playlist_tracks"))
    window.capture_base_playback_context(ids[0])
    base = deepcopy(window.base_playback_context)
    window.queue_track_by_id(ids[1])
    window.queue_track_by_id(ids[1])
    editor = window._manual_queue_editor()
    snapshot = editor.snapshot()
    window.listening.toggle_queue()
    panel = window.listening.panel
    assert panel.queue_list.count() == 2
    panel.remove_requested.emit(snapshot.entries[1].token, snapshot.revision)
    assert window.manual_queue == [ids[1]]
    panel.undo_requested.emit(editor.snapshot().revision)
    assert window.manual_queue == [ids[1], ids[1]]
    panel.clear_requested.emit(editor.snapshot().revision)
    assert window.manual_queue == []
    assert window.base_playback_context == base
    assert tuple(tuple(row) for row in window.db.conn.execute("SELECT * FROM playlist_tracks")) == before


def test_search_index_is_local_cached_and_invalidated_by_database_changes(isolated_ui_window):
    window = isolated_ui_window.window
    first = window.listening.search_index()
    assert first is window.listening.search_index()
    results = first.search("Synthetic", limit=40)
    assert any(item.kind == "track" for item in results)
    assert all(str(isolated_ui_window.root) not in str(item) for item in results)
    playlist_id = window.db.create_playlist("New synthetic discovery")
    second = window.listening.search_index()
    assert second is not first
    assert any(item.kind == "playlist" and item.key == str(playlist_id) for item in second.search("discovery"))
    sync = next(item for item in second.search("Sync Center") if item.kind == "action")
    window.listening.search_action("invoke_action", sync, ())
    assert window.pages.currentIndex() == 1
    assert window.sync_worker is None


def test_admin_navigation_does_not_destroy_library_context(isolated_ui_window):
    window = isolated_ui_window.window
    playlist = window.db.list_playlists()[0]
    route = Route("custom", playlist["id"], label=playlist["name"])
    window.listening.navigate(route)
    window.listening.navigate(Route("settings", label="Settings"))
    assert window.current_view_kind == "custom"
    assert window.current_playlist_id == playlist["id"]
    assert window.listening.content_route == route
    window.listening.back()
    assert window.pages.currentIndex() == 0
    assert window.current_playlist_id == playlist["id"]


def test_admin_refresh_uses_captured_search_ids(isolated_ui_window):
    window = isolated_ui_window.window
    ids = tuple(reversed(isolated_ui_window.track_ids[:3]))
    window.listening.navigate(Route("search", entity_key=ids, label="Search results"))
    window.listening.navigate(Route("settings", label="Settings"))
    window.refresh_current_view()
    assert window.pages.currentIndex() == 2
    assert window.library_table.source_track_ids() == list(ids)
    window.listening.back()
    assert window.visible_track_ids() == list(ids)


@pytest.mark.parametrize("leave_while_loading", [False, True])
def test_browser_back_restores_canonical_selection_after_async_refresh(isolated_ui_window, monkeypatch, qapp, leave_while_loading):
    window = isolated_ui_window.window
    requests = []
    monkeypatch.setattr(window.browser_summary_loader, "request", lambda *args: requests.append(args))

    def finish_request():
        kind, token, query = requests.pop(0)
        window._browser_summaries_loaded(kind, 1, token, query())
        qapp.processEvents()

    window.listening.navigate(Route("albums", label="Albums"))
    finish_request()
    window.browser_view.setCurrentIndex(window.album_browser_proxy.index(2, 0))
    expected_key = window.browser_view.currentIndex().data(MediaRole.KEY)
    assert expected_key is not None
    window.listening.navigate(Route("artists", label="Artists"))
    finish_request()
    # Invalidate summaries to force an asynchronous browser reload on Back.
    window.browser_summary_cache.clear()
    window.listening.back()
    assert requests
    if leave_while_loading:
        window.listening.navigate(Route("settings", label="Settings"))
        window.listening.back()
        # Complete the most recent request, not the now obsolete first one.
        requests.pop(0)
    finish_request()
    assert window.browser_view.currentIndex().data(MediaRole.KEY) == expected_key
    assert window.browser_view.selectionModel().selectedIndexes()[0].data(MediaRole.KEY) == expected_key


def test_open_queue_action_is_idempotent(isolated_ui_window):
    listening = isolated_ui_window.window.listening
    listening.dispatch("queue")
    assert listening.panel.isVisible()
    listening.dispatch("queue")
    assert listening.panel.isVisible()
    assert listening.queue_button.isChecked()
