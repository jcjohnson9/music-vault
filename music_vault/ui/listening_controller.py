"""Listening surfaces delegating to the window's existing playback authority.

Navigation never captures playback, search never starts provider work, and queue
editing uses the same list consumed by the existing transport methods.
"""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QInputDialog

from music_vault.core.library_browser import (
    query_album_summaries, query_artist_summaries,
    query_album_tracks, query_artist_track_sections, normalize_identity,
)
from music_vault.core.navigation import NavigationHistory, Route, ViewState
from music_vault.core.queue_editor import preview_continuation
from music_vault.core.quick_search import LocalSearchIndex, SearchEntity
from music_vault.ui.queue_panel import QueuePanel, QueueTrackLabel
from music_vault.ui.quick_search import QuickSearchDialog
from music_vault.ui.media_grid import MediaRole


DESTINATIONS = {
    "library": "Library", "albums": "Albums", "artists": "Artists",
    "recent": "Recently Added", "downloaded": "Downloaded",
    "liked": "Liked Tracks", "recently_played": "Recently Played", "rediscover": "Rediscover",
    "sync": "Sync Center", "settings": "Settings",
}


class ListeningController(QObject):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.history = NavigationHistory()
        self.restoring = False
        self.panel = None
        self._index = None
        self._index_stamp = None
        self._albums = ()
        self._artists = ()
        self._shortcuts = []
        self.content_route = Route()
        self._browser_restore = None

    def navigation_bar(self):
        bar = QFrame(self.host)
        bar.setObjectName("ListeningNavigation")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 2, 0)
        self.back_button = QPushButton("‹", bar)
        self.forward_button = QPushButton("›", bar)
        for button, label, callback in (
            (self.back_button, "Back (Alt+Left)", self.back),
            (self.forward_button, "Forward (Alt+Right)", self.forward),
        ):
            button.setFixedWidth(36)
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.search_button = QPushButton("Search your music   Ctrl+K", bar)
        self.search_button.setAccessibleName("Search Music Vault")
        self.search_button.clicked.connect(self.open_search)
        layout.addWidget(self.search_button, 1)
        self.queue_button = QPushButton("Queue", bar)
        self.queue_button.setAccessibleName("Show playback queue")
        self.queue_button.setCheckable(True)
        self.queue_button.setToolTip("Show or hide queue (Ctrl+Shift+Q)")
        self.queue_button.clicked.connect(self.toggle_queue)
        layout.addWidget(self.queue_button)
        for sequence, callback in (
            ("Ctrl+K", self.open_search), ("Alt+Left", self.back),
            ("Alt+Right", self.forward), ("Ctrl+Shift+Q", self.toggle_queue),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self.host)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self._update_navigation()
        return bar

    def queue_panel(self):
        self.panel = QueuePanel(self.host)
        self.panel.setMaximumWidth(430)
        self.panel.remove_requested.connect(lambda token, rev: self._edit_queue("remove", token, rev))
        self.panel.move_requested.connect(lambda token, before, rev: self._edit_queue("move", token, before, rev))
        self.panel.clear_requested.connect(lambda rev: self._edit_queue("clear", rev))
        self.panel.undo_requested.connect(lambda rev: self._edit_queue("undo", rev))
        self.panel.return_context_requested.connect(self.return_to_context)
        self.panel.close_requested.connect(self.hide_queue)
        self.panel.hide()
        return self.panel

    def toggle_queue(self):
        if self.panel is None:
            return
        self.panel.setVisible(not self.panel.isVisible())
        self.queue_button.setChecked(self.panel.isVisible())
        self.refresh_queue()

    def hide_queue(self):
        self.panel.hide()
        self.queue_button.setChecked(False)
        self.queue_button.setFocus()

    def open_queue(self):
        if self.panel is not None:
            self.panel.show()
            self.queue_button.setChecked(True)
            self.refresh_queue()

    def _edit_queue(self, method, *args):
        editor = self.host._manual_queue_editor()
        changed = getattr(editor, method)(*args)
        if changed:
            self.host.update_queue_label()
            self.host.write_app_status()
        else:
            self.refresh_queue()

    def refresh_queue(self):
        if self.panel is None or not self.panel.isVisible():
            return
        host = self.host
        context = host.base_playback_context or {}
        snapshot = host._manual_queue_editor().snapshot()
        continuation = preview_continuation(
            context.get("track_ids", ()), context.get("current_track_id"),
            host.shuffle_enabled, host.autoplay_enabled, host.repeat_mode,
            host.current_track_id is not None,
        )
        labels = {}
        ids = {entry.track_id for entry in snapshot.entries}
        ids.update(continuation.track_ids[:self.panel.PREVIEW_LIMIT])
        if host.current_track_id is not None:
            ids.add(host.current_track_id)
        for track_id in ids:
            row = host.db.get_track(track_id)
            if row:
                labels[track_id] = QueueTrackLabel(
                    row["title"] or Path(row["path"]).stem,
                    row["artist"] or "", Path(row["path"]).is_file(),
                )
        current = labels.get(host.current_track_id)
        self.panel.set_snapshot(
            snapshot, labels, current.title if current else None,
            current.artist if current else None, continuation,
            str(context.get("playlist_name") or "No playback context"),
            isinstance(context.get("route"), Route),
        )

    def _capture_view(self):
        host = self.host
        pending = self._browser_restore
        if pending is not None and pending.route == self.history.current.route:
            # Leaving a loading browser must not replace its saved canonical
            # selection/scroll with the transient empty model.
            return replace(pending, query=host.search_box.text())
        table = host.library_table
        browser = self.history.current.route.kind in {"albums", "artists"}
        scroll = host.browser_view.verticalScrollBar() if browser else table.verticalScrollBar()
        return ViewState(
            self.history.current.route, host.search_box.text(),
            tuple(table.selected_track_ids()), table.current_track_id(),
            scroll.value(), table.proxy_model.sortColumn(),
            table.proxy_model.sortOrder() == Qt.SortOrder.DescendingOrder,
            host.browser_view.currentIndex().data(MediaRole.KEY) if browser else None,
        )

    def _remember(self):
        if not self.restoring:
            self.history.save(self._capture_view())

    def navigate(self, route: Route):
        self._remember()
        self._show(self.history.visit(route))

    def back(self):
        self._remember()
        state = self.history.back()
        if state is not None:
            self._show(state)

    def forward(self):
        self._remember()
        state = self.history.forward()
        if state is not None:
            self._show(state)

    def _show(self, state: ViewState):
        host, route = self.host, state.route
        self._browser_restore = state if route.kind in {"albums", "artists"} else None
        self.restoring = True
        try:
            collections = getattr(host, "listening_library", None)
            if collections is not None:
                collections.update_controls(route.kind)
            if route.kind not in {"sync", "settings"}:
                self.content_route = route
                host.current_view_kind = route.kind
                host.current_playlist_id = route.playlist_id
                host.current_playlist_name = route.label
                host._detail_browser_context = None
            host.search_box.blockSignals(True)
            host.search_box.setText(state.query)
            host.search_box.blockSignals(False)
            table = host.library_table
            if state.sort_column < 0:
                table.clear_sort()
            else:
                table.set_sort(state.sort_column, Qt.SortOrder.DescendingOrder if state.descending else Qt.SortOrder.AscendingOrder)
            if route.kind in {"sync", "settings"}:
                host.pages.setCurrentIndex(1 if route.kind == "sync" else 2)
            else:
                host.pages.setCurrentIndex(0)
                if route.kind == "album_tracks":
                    host._detail_browser_context = (route.kind, route.entity_key, route.label)
                    host.load_library(query_album_tracks(host.db.conn, route.entity_key), route.label, "Album view")
                elif route.kind == "artist_tracks":
                    self._show_artist(route)
                else:
                    host.refresh_current_view()
                host.filter_library(state.query)
                if route.kind not in {"albums", "artists"}:
                    table.restore_selection(state.selected_ids, state.current_id)
                    table.verticalScrollBar().setValue(state.scroll)
            host.update_sidebar_navigation_state()
        finally:
            self.restoring = False
        self._update_navigation()

    def restore_browser_view(self, kind):
        """Restore canonical selection only after an accepted summary is applied."""
        state = self._browser_restore
        if state is None or state.route.kind != kind:
            return

        def restore():
            if self._browser_restore is not state or self.history.current.route != state.route:
                return
            host = self.host
            proxy = host._browser_proxy(kind)
            if host.browser_view.model() is not proxy:
                return
            # Keys, not row numbers, survive asynchronous refresh and resorting.
            if state.browser_key is not None:
                source = host._browser_model(kind).index_for_key(state.browser_key)
                index = proxy.mapFromSource(source)
                if index.isValid():
                    host.browser_view.setCurrentIndex(index)
            host._browser_scroll_positions[kind] = state.scroll
            host.browser_view.verticalScrollBar().setValue(state.scroll)
            self._browser_restore = None

        QTimer.singleShot(0, restore)

    def _show_artist(self, route):
        host = self.host
        host._detail_browser_context = (route.kind, route.entity_key, route.label)
        sections = query_artist_track_sections(host.db.conn, route.entity_key)
        selector = host.artist_section_selector
        selector.blockSignals(True)
        try:
            selector.clear()
            for name, label in (("tracks", "Tracks"), ("featured_on", "Featured On"), ("collaborations", "Collaborations"), ("group_appearances", "Group Appearances")):
                if name == "tracks" or getattr(sections, name):
                    selector.addItem(label, name)
            index = selector.findData(route.section)
            if route.section == "tracks" and not sections.tracks and selector.count() > 1:
                index = 1
            selector.setCurrentIndex(max(0, index))
        finally:
            selector.blockSignals(False)
        selector.setVisible(selector.count() > 1)
        rows = getattr(sections, selector.currentData() or "tracks")
        host.load_library(rows, route.label, f"Artist view • {selector.currentText()}")

    def _update_navigation(self):
        if hasattr(self, "back_button"):
            self.back_button.setEnabled(self.history.can_back)
            self.forward_button.setEnabled(self.history.can_forward)

    def return_to_context(self):
        route = (self.host.base_playback_context or {}).get("route")
        if isinstance(route, Route):
            self.navigate(route)

    def search_index(self):
        conn = self.host.db.conn
        # Listening checkpoints are not library/identity changes. Retain the
        # conservative invalidation for other writes and external connections.
        store = getattr(self.host.db, "listening", None)
        stamp = (conn.total_changes - getattr(store, "write_count", 0), conn.execute("PRAGMA data_version").fetchone()[0])
        if self._index is not None and stamp == self._index_stamp:
            return self._index
        entities = [SearchEntity(
            "track", str(row["id"]), row["title"] or Path(row["path"]).stem,
            " · ".join(filter(None, (row["artist"], row["album"]))),
            track_id=int(row["id"]),
        ) for row in self.host.db.list_tracks()]
        self._albums = query_album_summaries(conn)
        self._artists = query_artist_summaries(conn)
        entities.extend(SearchEntity("album", item.browser_key, item.album_title, item.album_artist, payload=Route("album_tracks", entity_key=item.key, label=item.album_title)) for item in self._albums)
        entities.extend(SearchEntity("artist", item.browser_key, item.display_name, f"{item.track_count} tracks", payload=Route("artist_tracks", entity_key=item.key, label=item.display_name)) for item in self._artists)
        entities.extend(SearchEntity("playlist", str(row["id"]), row["name"], "Playlist", payload=Route("custom", int(row["id"]), label=row["name"])) for row in self.host.db.list_playlists())
        entities.extend(SearchEntity("action", kind, label, "Open view", payload=Route(kind, label=label)) for kind, label in DESTINATIONS.items())
        entities.extend(SearchEntity("action", key, label, "Playback action") for key, label in (("play_pause", "Play / pause"), ("next", "Next track"), ("previous", "Previous track"), ("queue", "Open queue"), ("party", "Toggle Party Mode"), ("history", "Listening history")))
        self._index = LocalSearchIndex(entities)
        self._index_stamp = stamp
        return self._index

    def open_search(self, _checked=False, *, query=""):
        dialog = QuickSearchDialog(self.search_index(), self.host)
        dialog.action_requested.connect(self.search_action)
        collections = getattr(self.host, "listening_library", None)
        if collections is not None:
            dialog.favorite_lookup = collections.store.is_favorite
        if query:
            dialog.query_edit.setText(query)
        dialog.exec()
        dialog.deleteLater()

    def dispatch(self, action: str):
        host = self.host
        callbacks = {
            "play_pause": host.toggle_play, "next": host.play_next,
            "previous": host.play_previous, "party": host.toggle_party_mode,
            "queue": self.open_queue, "search": self.open_search,
            "back": self.back, "forward": self.forward,
        }
        collections = getattr(host, "listening_library", None)
        if collections is not None:
            callbacks["history"] = collections.open_history
        callback = callbacks.get(action)
        if callback is not None:
            callback()

    def search_action(self, action, entity, ordered_ids):
        if not isinstance(entity, SearchEntity):
            return
        host = self.host
        if self._index is not None:
            self._index.mark_used(entity.kind, entity.key)
        if action == "play_track" and entity.track_id is not None:
            self.play_explicit_context(entity.track_id, ordered_ids, "Search results")
        elif action == "queue_track" and entity.track_id is not None:
            host.queue_track_by_id(entity.track_id)
        elif action in {"open_entity", "invoke_action"}:
            if isinstance(entity.payload, Route):
                self.navigate(entity.payload)
            elif entity.kind == "action":
                self.dispatch(entity.key)
        elif action == "add_to_playlist" and entity.track_id is not None:
            host.add_track_to_playlist_by_id(entity.track_id)
        elif action == "toggle_favorite" and entity.track_id is not None:
            host.listening_library.toggle_favorite(entity.track_id)
        elif action in {"go_artist", "go_album"} and entity.track_id is not None:
            self._go_related(entity.track_id, action == "go_artist")

    def play_explicit_context(self, track_id, ordered_ids, label):
        """Capture context before synchronous Qt source/position notifications."""
        host = self.host
        previous = host.base_playback_context
        ids = tuple(dict.fromkeys(ordered_ids))
        host.base_playback_context = {
            "kind": "search", "playlist_id": None,
            "playlist_name": label, "track_ids": list(ids),
            "current_track_id": track_id,
            "route": Route("search", entity_key=ids, label=label),
        }
        try:
            played = host.play_track_by_id(track_id, capture_base_context=False)
        except Exception:
            host.base_playback_context = previous
            raise
        if not played:
            host.base_playback_context = previous
        self.refresh_queue()
        return played

    def _go_related(self, track_id, artist):
        row = self.host.db.get_track(track_id)
        if row is None:
            return
        self.search_index()
        conn = self.host.db.conn
        if artist:
            ids = {int(item[0]) for item in conn.execute("SELECT artist_id FROM track_artist_credits WHERE track_id=? AND role='primary'", (track_id,))}
            choices = [item for item in self._artists if ids.intersection(item.key.cluster_artist_ids or (item.key.artist_id,))]
            if not ids:
                choices = [item for item in self._artists if item.key.normalized_name == normalize_identity(row["artist"])]
        else:
            ids = {int(item[0]) for item in conn.execute("SELECT canonical_album_id FROM track_album_memberships WHERE track_id=?", (track_id,))}
            choices = [item for item in self._albums if item.key.canonical_album_id in ids]
            if not ids:
                choices = [item for item in self._albums if item.key.title_key == normalize_identity(row["album"]) and item.key.artist_key == normalize_identity(row["album_artist"] or row["artist"])]
        if not choices:
            self.navigate(Route("artists" if artist else "albums", label="Artists" if artist else "Albums"))
            return
        selected = choices[0]
        if len(choices) > 1:
            labels = [f"{index + 1}. {item.display_name if artist else item.album_title}" for index, item in enumerate(choices)]
            choice, accepted = QInputDialog.getItem(self.host, "Choose context", "Related artist" if artist else "Related album", labels, 0, False)
            if not accepted:
                return
            selected = choices[labels.index(choice)]
        self.navigate(Route("artist_tracks" if artist else "album_tracks", entity_key=selected.key, label=selected.display_name if artist else selected.album_title))
