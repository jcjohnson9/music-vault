"""Local favorites, bounded history pages and explainable rediscovery.

This surface has no provider or transport authority. Explicit play requests go
through the same window action as a library or search result.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from music_vault.core.navigation import Route
from music_vault.ui.icons import ui_icon
from music_vault.ui.theme import COLORS


LISTENING_ROUTES = {"liked", "recently_played", "rediscover"}


def _style_dialog(dialog):
    # QListWidget otherwise keeps the Windows light palette while inheriting
    # the application's light text, making history unreadable in the EXE.
    dialog.setStyleSheet(f"""
        QDialog {{ background: {COLORS['elevated_surface']}; color: {COLORS['text_primary']}; }}
        QListWidget {{ background: {COLORS['subtle_surface']}; color: {COLORS['text_primary']};
            border: 1px solid {COLORS['border']}; border-radius: 8px; padding: 4px; }}
        QListWidget::item {{ padding: 10px; border-radius: 5px; }}
        QListWidget::item:selected {{ background: {COLORS['selection']}; color: {COLORS['text_primary']}; }}
        QListWidget::item:hover {{ background: {COLORS['hover_surface']}; }}
    """)


def _label(text, parent=None):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _local_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d, %Y · %H:%M")
    except (TypeError, ValueError, AttributeError):
        return "Time unavailable"


class ListeningHistoryDialog(QDialog):
    """One indexed page at a time, never an eager full-history widget."""

    play_requested = Signal(int, object)
    PAGE_SIZE = 100

    def __init__(self, store, run_id, parent=None):
        super().__init__(parent)
        _style_dialog(self)
        self.store, self.run_id = store, run_id
        self._cursors = [None]
        self._page = 0
        self._rows = []
        self.setWindowTitle("Listening history")
        self.setAccessibleName("Local listening history")
        self.resize(720, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addWidget(_label("Your listening history"))
        layout.addWidget(_label(
            "Recorded only after playback progresses. A meaningful listen is 30 seconds "
            "or half the track, whichever is shorter—not a guarantee that audio was heard. "
            "History stays on this device."
        ))
        self.rows = QListWidget(self)
        self.rows.setAccessibleName("Listening occurrences, newest first")
        self.rows.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.rows.currentRowChanged.connect(self._selection_changed)
        self.rows.itemActivated.connect(lambda _item: self._play())
        layout.addWidget(self.rows, 1)
        self.status = _label("")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.play_button = QPushButton("Play selected", self)
        self.play_button.clicked.connect(self._play)
        buttons.addWidget(self.play_button)
        buttons.addStretch(1)
        self.newer_button = QPushButton("Newer", self)
        self.newer_button.clicked.connect(self._newer)
        buttons.addWidget(self.newer_button)
        self.older_button = QPushButton("Older", self)
        self.older_button.clicked.connect(self._older)
        buttons.addWidget(self.older_button)
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self._refresh)
        buttons.addWidget(refresh)
        close = QPushButton("Close", self)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._load()

    def _load(self):
        self.rows.clear()
        self._rows = []
        try:
            fetched = self.store.history_page(
                limit=self.PAGE_SIZE + 1, before=self._cursors[self._page], run_id=self.run_id,
            )
        except Exception:
            self.status.setText("History is temporarily unavailable. Playback is unaffected.")
            self.newer_button.setEnabled(self._page > 0)
            self.older_button.setEnabled(False)
            self._selection_changed()
            return
        self._rows = fetched[:self.PAGE_SIZE]
        for row in self._rows:
            status = "Meaningful listen" if row.get("qualified_at") else "Started playback"
            reason = "Interrupted" if row.get("interrupted") else {
                "ended": "Reached end", "next": "Skipped forward", "previous": "Previous",
                "replaced": "Changed track", "error": "Playback error", "stopped": "Stopped",
                "app_closed": "App closed", None: "Current session",
            }.get(row.get("end_reason"), "Recorded")
            available = bool(row.get("available"))
            title = row.get("title") or "Untitled track"
            artist = row.get("artist") or "Unknown artist"
            text = f"{title} — {artist}\n{_local_time(row.get('started_at'))} · {status} · {reason}"
            if not available:
                text += " · No longer in library"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
            self.rows.addItem(item)
        self.newer_button.setEnabled(self._page > 0)
        self.older_button.setEnabled(len(fetched) > self.PAGE_SIZE)
        self.status.setText(
            f"Page {self._page + 1} · {len(self._rows)} occurrences. Unavailable tracks remain in history."
            if self._rows else "Nothing played yet. Choose a track to begin your local history."
        )
        if self.rows.count():
            self.rows.setCurrentRow(0)
        self._selection_changed()

    def _selection_changed(self, *_args):
        item = self.rows.currentItem()
        row = item.data(Qt.ItemDataRole.UserRole) if item else {}
        self.play_button.setEnabled(bool(row and row.get("available") and row.get("track_id")))

    def _play(self):
        item = self.rows.currentItem()
        row = item.data(Qt.ItemDataRole.UserRole) if item else {}
        if not row or not row.get("available") or not row.get("track_id"):
            return
        ids = tuple(dict.fromkeys(item["track_id"] for item in self._rows if item.get("available") and item.get("track_id")))
        self.play_requested.emit(int(row["track_id"]), ids)

    def _older(self):
        if not self._rows or not self.older_button.isEnabled():
            return
        last = self._rows[-1]
        self._cursors[self._page + 1:] = [(last["started_at"], last["event_id"])]
        self._page += 1
        self._load()

    def _newer(self):
        if self._page:
            self._page -= 1
            self._load()

    def _refresh(self):
        self._page, self._cursors = 0, [None]
        self._load()


class ListeningLibraryController(QObject):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.store = host.db.listening
        self._refresh_pending = False
        self._dialog = None

    def collection_controls(self, layout):
        self.history_button = QPushButton("History", self.host)
        self.history_button.setIcon(ui_icon("history", 18))
        self.history_button.setAccessibleName("Open listening history")
        self.history_button.clicked.connect(self.open_history)
        layout.addWidget(self.history_button)
        self.rediscovery_selector = QComboBox(self.host)
        self.rediscovery_selector.setAccessibleName("Rediscovery collection")
        self.rediscovery_selector.addItem("Favorites to revisit", "favorites_to_revisit")
        self.rediscovery_selector.addItem("No recorded plays", "no_recorded_plays")
        self.rediscovery_selector.currentIndexChanged.connect(self._rediscovery_changed)
        layout.addWidget(self.rediscovery_selector)
        self.history_button.hide()
        self.rediscovery_selector.hide()

    def like_button(self, parent):
        self.like = QPushButton(parent)
        self.like.setObjectName("CircleButton")
        self.like.setFixedSize(36, 36)
        self.like.setCheckable(True)
        self.like.setAccessibleName("Like current track")
        self.like.clicked.connect(lambda _checked: self.toggle_favorite(self.host.current_track_id))
        self.refresh_like()
        return self.like

    def refresh_like(self):
        if not hasattr(self, "like"):
            return
        track_id = self.host.current_track_id
        try:
            liked = bool(track_id and self.store.is_favorite(track_id))
        except Exception:
            self.like.setEnabled(False)
            self.like.setToolTip("Favorites are temporarily unavailable")
            return
        self.like.setEnabled(track_id is not None)
        self.like.setChecked(liked)
        label = "Unlike current track" if liked else "Like current track"
        self.like.setAccessibleName(label)
        self.like.setToolTip(label)
        self.like.setIcon(ui_icon("heart", 19, color=COLORS["accent"] if liked else COLORS["text_secondary"]))

    def toggle_favorite(self, track_id):
        if track_id is None:
            return
        try:
            desired = not self.store.is_favorite(track_id)
            changed = self.store.set_favorite(track_id, desired)
        except Exception:
            self.host.statusBar().showMessage("Could not save favorite. Playback is unaffected.", 6000)
            self.refresh_like()
            return
        if changed:
            self.host.statusBar().showMessage("Added to Liked Tracks" if desired else "Removed from Liked Tracks", 2500)
            self.changed()
            self.favorites_changed()
        self.refresh_like()

    def changed(self):
        # Never reorder the actively browsed table on every progress checkpoint.
        # These routes are refreshed when entered; favorite edits explicitly
        # refresh their collection, preserving selected canonical IDs.
        self.refresh_like()

    def favorites_changed(self):
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self._refresh_collection)

    def _refresh_collection(self):
        self._refresh_pending = False
        if self.host.current_view_kind in {"liked", "rediscover"}:
            table = self.host.library_table
            ids, current = table.selected_track_ids(), table.current_track_id()
            scroll = table.verticalScrollBar().value()
            self.show_route(self.host.current_view_kind)
            table.restore_selection(ids, current)
            table.verticalScrollBar().setValue(scroll)

    def _rediscovery_changed(self):
        if self.host.current_view_kind == "rediscover":
            route = Route("rediscover", section=str(self.rediscovery_selector.currentData()), label="Rediscover")
            self.host.listening.navigate(route)

    def update_controls(self, kind):
        self.history_button.setVisible(kind in {"recently_played", "liked", "rediscover"})
        self.rediscovery_selector.setVisible(kind == "rediscover")

    def show_route(self, kind):
        self.update_controls(kind)
        if kind not in LISTENING_ROUTES:
            return False
        host = self.host
        try:
            if kind == "liked":
                rows = self.store.liked_tracks(limit=None)
                title, subtitle = "Liked Tracks", "Your favorites, kept separate from playlists."
                empty = ("Keep the tracks you love close", "Use the heart in Now Playing or Like in a track's menu.")
            elif kind == "recently_played":
                rows = self.store.recently_played_tracks(limit=200)
                title, subtitle = "Recently Played", "Your 200 most recently played tracks. Open History for individual listens."
                empty = ("Your listening starts here", "Tracks appear after playback actually progresses. Recently Added is still separate.")
            else:
                section = host.listening.content_route.section
                index = self.rediscovery_selector.findData(section)
                self.rediscovery_selector.blockSignals(True)
                self.rediscovery_selector.setCurrentIndex(max(0, index))
                self.rediscovery_selector.blockSignals(False)
                mode = self.rediscovery_selector.currentData()
                rows = self.store.rediscover_tracks(kind=mode, limit=50)
                title = "Rediscover"
                subtitle = ("50 favorites ordered by least recent recorded playback. Entirely local."
                            if mode == "favorites_to_revisit" else "50 tracks with no playback recorded by this installation—not a claim you have never heard them.")
                empty = ("A fresh way back into your library", "Like some tracks or choose No recorded plays. No online recommendations are used.")
        except Exception:
            host.load_library([], "Listening collection", "This collection is temporarily unavailable. Your library and playback are unaffected.")
            host.library_empty_state.title_label.setText("Collection unavailable")
            host.library_empty_state.description_label.setText("Try opening the collection again. No history or library data has been deleted.")
            return True
        host.load_library(rows, title, subtitle)
        if not rows:
            host.library_empty_state.title_label.setText(empty[0])
            host.library_empty_state.description_label.setText(empty[1])
        return True

    def open_history(self):
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        bridge = getattr(self.host, "listening_history", None)
        dialog = ListeningHistoryDialog(self.store, getattr(bridge, "run_id", None), self.host)
        self._dialog = dialog
        dialog.play_requested.connect(lambda track_id, ids: self.host.listening.play_explicit_context(track_id, ids, "Listening history"))
        try:
            dialog.exec()
        finally:
            self._dialog = None
            dialog.deleteLater()

    def open_unavailable_favorites(self):
        dialog = UnavailableFavoritesDialog(self.store, self.host)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()


class UnavailableFavoritesDialog(QDialog):
    """Explicitly unlike a removed-library tombstone, never touch its media."""

    PAGE_SIZE = 100

    def __init__(self, store, parent=None):
        super().__init__(parent)
        _style_dialog(self)
        self.store, self.offset = store, 0
        self.setWindowTitle("Unavailable favorites")
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(_label("These favorites no longer have a track in the library. "
                                "They are not automatically matched to reimports. Removing a favorite does not delete music or history."))
        self.rows = QListWidget(self)
        self.rows.setAccessibleName("Unavailable favorites")
        self.rows.currentRowChanged.connect(self._selection)
        layout.addWidget(self.rows, 1)
        self.status = _label("")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.remove_button = QPushButton("Remove favorite", self)
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        self.previous_button = QPushButton("Previous", self)
        self.previous_button.clicked.connect(lambda: self._move(-1))
        buttons.addWidget(self.previous_button)
        self.next_button = QPushButton("Next", self)
        self.next_button.clicked.connect(lambda: self._move(1))
        buttons.addWidget(self.next_button)
        close = QPushButton("Close", self)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._load()

    def _load(self):
        self.rows.clear()
        try:
            rows = self.store.unavailable_favorites(limit=self.PAGE_SIZE + 1, offset=self.offset)
        except Exception:
            self.status.setText("Favorites are temporarily unavailable.")
            self.next_button.setEnabled(False)
            self.previous_button.setEnabled(False)
            self._selection()
            return
        for row in rows[:self.PAGE_SIZE]:
            text = f"{row.get('title') or 'Untitled track'} — {row.get('artist') or 'Unknown artist'}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row["favorite_id"])
            self.rows.addItem(item)
        self.status.setText("No unavailable favorites." if not rows else f"Page {self.offset // self.PAGE_SIZE + 1} · Saved display names only")
        self.previous_button.setEnabled(self.offset > 0)
        self.next_button.setEnabled(len(rows) > self.PAGE_SIZE)
        self._selection()

    def _selection(self, *_args):
        self.remove_button.setEnabled(self.rows.currentItem() is not None)

    def _move(self, direction):
        self.offset = max(0, self.offset + direction * self.PAGE_SIZE)
        self._load()

    def _remove(self):
        item = self.rows.currentItem()
        if item is None:
            return
        try:
            self.store.remove_favorite(item.data(Qt.ItemDataRole.UserRole))
        except Exception:
            self.status.setText("Could not remove the favorite. Nothing else was changed.")
            return
        self._load()
