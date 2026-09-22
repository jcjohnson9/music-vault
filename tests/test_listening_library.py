from copy import deepcopy
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtMultimedia import QMediaPlayer

from music_vault import app
from music_vault.core.navigation import Route
from music_vault.core.quick_search import SearchEntity
from music_vault.ui.listening_library import ListeningHistoryDialog, UnavailableFavoritesDialog
from music_vault.ui.theme import COLORS
from test_ui_system import isolated_ui_window


def occurrence(track_id, index=0, **overrides):
    return {
        "event_id": f"event-{index:05d}", "run_id": "fixture-run",
        "track_id": track_id, "recorded_track_id": track_id,
        "title_at_start": "Snapshot title", "artist_at_start": "Snapshot artist",
        "album_at_start": "Snapshot album", "playback_origin": "manual",
        "started_at": "2026-09-22T00:00:00Z", "last_observed_at": "2026-09-22T00:00:01Z",
        "listened_ms": 1000, "duration_ms": 180000, "update_sequence": 1,
        **overrides,
    }


def test_likes_are_canonical_and_do_not_mutate_membership_queue_or_metadata(isolated_ui_window, qapp):
    window = isolated_ui_window.window
    first, second = isolated_ui_window.track_ids[:2]
    tables = ("tracks", "playlist_tracks", "playlist_track_origins", "track_metadata_history")
    before = {name: list(map(tuple, window.db.conn.execute(f"SELECT * FROM {name}"))) for name in tables}
    window.current_track_id = first
    window.manual_queue[:] = [second, second]
    window.capture_base_playback_context(first)
    context = deepcopy(window.base_playback_context)
    window.listening_library.toggle_favorite(first)
    window.listening.navigate(Route("liked", label="Liked Tracks"))
    assert window.visible_track_ids() == [first]
    assert window.listening_library.like.isChecked()
    window.listening_library.toggle_favorite(first)
    qapp.processEvents()
    assert window.visible_track_ids() == []
    assert not window.listening_library.like.isChecked()
    assert window.manual_queue == [second, second]
    assert window.base_playback_context == context
    assert {name: list(map(tuple, window.db.conn.execute(f"SELECT * FROM {name}"))) for name in tables} == before


def test_recently_played_is_not_import_chronology_and_history_writes_keep_search_cache(isolated_ui_window):
    window = isolated_ui_window.window
    track_id = isolated_ui_window.track_ids[2]
    index = window.listening.search_index()
    window.listening.navigate(Route("recently_played", label="Recently Played"))
    assert window.visible_track_ids() == []
    window.db.listening.save_event(occurrence(track_id))
    assert window.listening.search_index() is index
    window.refresh_current_view()
    assert window.visible_track_ids() == [track_id]
    window.db.listening.set_favorite(track_id, True)
    assert window.listening.search_index() is index
    window.db.create_playlist("Synthetic new playlist")
    assert window.listening.search_index() is not index


def test_rediscovery_routes_are_explicit_local_and_preserve_navigation(isolated_ui_window, qapp):
    window = isolated_ui_window.window
    first, second = isolated_ui_window.track_ids[:2]
    window.db.listening.set_favorite(first, True)
    window.db.listening.set_favorite(second, True)
    window.db.listening.save_event(occurrence(first))
    window.listening.navigate(Route("rediscover", label="Rediscover"))
    assert window.visible_track_ids() == [second, first]
    window.listening_library.rediscovery_selector.setCurrentIndex(1)
    assert first not in window.visible_track_ids()
    assert second in window.visible_track_ids()
    assert "not a claim" in window.page_subtitle.text()
    window.search_box.setText("Synthetic")
    window.listening.navigate(Route("recent", label="Recently Added"))
    window.listening.back()
    assert window.current_view_kind == "rediscover"
    assert window.listening_library.rediscovery_selector.currentData() == "no_recorded_plays"
    assert window.search_box.text() == "Synthetic"


def test_history_is_keyset_paged_and_preserves_unavailable_occurrences(isolated_ui_window):
    window = isolated_ui_window.window
    track_id = isolated_ui_window.track_ids[0]
    for index in range(105):
        window.db.listening.save_event(occurrence(track_id, index))
    # No track deletion needed: an already unavailable historical ID is valid
    # evidence but must not turn into a play action or guessed identity match.
    window.db.listening.save_event(occurrence(999999, 106))
    dialog = ListeningHistoryDialog(window.db.listening, "other-run", window)
    try:
        assert dialog.rows.count() == 100
        assert dialog.older_button.isEnabled()
        assert not dialog.play_button.isEnabled()
        assert "No longer in library" in dialog.rows.item(0).text()
        assert "Interrupted" in dialog.rows.item(0).text()
        first_page = {row["event_id"] for row in dialog._rows}
        dialog._older()
        assert dialog.rows.count() == 6
        assert first_page.isdisjoint({row["event_id"] for row in dialog._rows})
        assert not dialog.older_button.isEnabled()
        dialog._newer()
        assert {row["event_id"] for row in dialog._rows} == first_page
    finally:
        dialog.close()


def test_history_play_uses_current_ids_and_empty_like_state_is_not_selection(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    first, second = isolated_ui_window.track_ids[:2]
    window.library_table.select_track(first)
    window.listening_library.refresh_like()
    assert not window.listening_library.like.isEnabled()
    captured = []
    monkeypatch.setattr(window, "play_track_by_id", lambda track_id, **kwargs: captured.append((track_id, deepcopy(window.base_playback_context))) or True)
    window.manual_queue[:] = [first]
    window.listening.play_explicit_context(second, [first, second, first], "Listening history")
    assert captured[0][0] == second
    assert captured[0][1]["track_ids"] == [first, second]
    assert captured[0][1]["playlist_name"] == "Listening history"
    assert window.manual_queue == [first]


def test_failed_explicit_play_restores_base_context(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    first, second = isolated_ui_window.track_ids[:2]
    window.capture_base_playback_context(first)
    previous = deepcopy(window.base_playback_context)
    monkeypatch.setattr(window, "play_track_by_id", lambda *_args, **_kwargs: False)
    assert not window.listening.play_explicit_context(second, [second], "History")
    assert window.base_playback_context == previous


def test_quick_search_favorite_uses_explicit_result_not_visible_selection(isolated_ui_window):
    window = isolated_ui_window.window
    first, second = isolated_ui_window.track_ids[:2]
    window.library_table.select_track(first)
    entity = SearchEntity("track", str(second), "Synthetic favorite", track_id=second)
    window.listening.search_action("toggle_favorite", entity, (second,))
    assert window.db.listening.favorite_ids() == {second}
    assert window.current_track_id is None


def test_favorite_storage_failure_is_nonfatal_and_private(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    def fail(*_args):
        raise RuntimeError("Must not echo private exception detail")
    monkeypatch.setattr(window.db.listening, "set_favorite", fail)
    window.listening_library.toggle_favorite(isolated_ui_window.track_ids[0])
    assert window.statusBar().currentMessage() == "Could not save favorite. Playback is unaffected."
    assert window.db.listening.favorite_ids() == set()


def test_host_repeat_end_error_seek_and_close_hooks_preserve_existing_actions(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(window, "listening_history", SimpleNamespace(
            repeat_current=lambda **kwargs: calls.append("repeat"),
            finish=lambda reason: calls.append(reason),
            before_seek=lambda: calls.append("seek"),
            accepted_close=lambda: calls.append("close"),
        ))
        patch.setattr(window.player, "setPosition", lambda value: calls.append(("position", value)))
        patch.setattr(window.player, "play", lambda: calls.append("play"))
        window.repeat_mode = "one"
        window.on_media_status_changed(QMediaPlayer.EndOfMedia)
        assert calls == ["repeat", ("position", 0), "play"]
        calls.clear()
        window.repeat_mode = "off"
        patch.setattr(window, "play_next_from_base_context", lambda: calls.append("base"))
        window.on_media_status_changed(QMediaPlayer.EndOfMedia)
        assert calls == ["ended", "base"]
        calls.clear()
        window.on_slider_released()
        assert calls[0] == "seek"
        assert calls[1][0] == "position"
        calls.clear()
        patch.setattr(window.windows_transport, "close", lambda: False)
        patch.setattr(app.QTimer, "singleShot", lambda *_args: None)
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert "close" not in calls


def test_host_occurrence_intents_preserve_fifo_and_base_resume(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    ids = isolated_ui_window.track_ids
    window.listening.navigate(Route("search", entity_key=tuple(ids[:4]), label="Fixture base"))
    calls = []
    bridge = window.listening_history
    def prepare(row, **kwargs):
        calls.append((row["id"], bridge._intent_value(1, "manual"), bridge._intent_value(0, "replaced"), window.current_track_id))
    monkeypatch.setattr(bridge, "prepare_track", prepare)
    monkeypatch.setattr(window.player, "setSource", lambda *_args: None)
    monkeypatch.setattr(window.player, "play", lambda: None)
    assert window.play_track_by_id(ids[0])
    window.manual_queue[:] = [ids[5], ids[5], 999999]
    window.listening.navigate(Route())
    window.play_next()
    window.play_next()
    window.play_next()
    assert calls == [
        (ids[0], "manual", "replaced", None),
        (ids[5], "manual_queue", "next", ids[0]),
        (ids[5], "manual_queue", "next", ids[5]),
        (ids[1], "base", "next", ids[5]),
    ]
    assert window.base_playback_context["track_ids"] == ids[:4]
    assert window.base_playback_context["current_track_id"] == ids[1]
    assert bridge._intents == []
    window.play_previous()
    assert calls[-1][:3] == (ids[0], "base", "previous")


def test_unavailable_favorite_removal_is_explicit_and_history_survives(isolated_ui_window):
    window = isolated_ui_window.window
    # Synthetic tombstone, exactly the shape produced by ON DELETE SET NULL.
    window.db.conn.execute("INSERT INTO track_favorites(track_id,recorded_track_id,title_at_favorite,artist_at_favorite,favorited_at) VALUES(NULL,999999,'Unavailable fixture','Fixture','2026-09-22T00:00:00Z')")
    window.db.conn.commit()
    window.db.listening.save_event(occurrence(999999))
    before = tuple(map(tuple, window.db.conn.execute("SELECT * FROM listening_events")))
    dialog = UnavailableFavoritesDialog(window.db.listening, window)
    try:
        assert dialog.rows.count() == 1
        assert not dialog.remove_button.isEnabled()
        dialog.rows.setCurrentRow(0)
        dialog.remove_button.click()
        assert dialog.rows.count() == 0
        assert tuple(map(tuple, window.db.conn.execute("SELECT * FROM listening_events"))) == before
    finally:
        dialog.close()


def test_listening_dialogs_render_dark_list_surface_not_windows_white(isolated_ui_window, qapp):
    window = isolated_ui_window.window
    for dialog in (ListeningHistoryDialog(window.db.listening, "fixture", window), UnavailableFavoritesDialog(window.db.listening, window)):
        try:
            dialog.show()
            qapp.processEvents()
            viewport = dialog.rows.viewport()
            image = viewport.grab().toImage()
            assert image.pixelColor(image.width() - 10, image.height() - 10).name().lower() == COLORS["subtle_surface"].lower()
            assert COLORS["text_primary"] in dialog.styleSheet()
        finally:
            dialog.close()
