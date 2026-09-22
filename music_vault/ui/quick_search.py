"""Signal-only global search: the window owns navigation and playback actions."""

from __future__ import annotations

import html
from collections.abc import Iterable

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from music_vault.core.quick_search import (
    LocalSearchIndex, SearchEntity, group_results, track_result_ids,
)
from music_vault.ui.theme import COLORS


_GROUP_LABELS = {
    "track": "Tracks", "album": "Albums", "artist": "Artists",
    "playlist": "Playlists", "action": "Actions",
}


class QuickSearchDialog(QDialog):
    """Bounded results with plain text and no database/provider/player access.

    ``action_requested`` emits ``(action_id, SearchEntity, ordered_track_ids)``.
    IDs are play_track, queue_track, open_entity, invoke_action, go_artist,
    go_album and add_to_playlist. The host resolves and authorizes every action;
    even an Actions result only emits a request. Queueing keeps the dialog open
    so several tracks can be appended without replacing the playback context.
    """

    action_requested = Signal(str, object, object)
    DEBOUNCE_MS = 100
    RESULT_LIMIT = 40

    def __init__(
        self,
        index: LocalSearchIndex,
        parent: QWidget | None = None,
        *,
        context_keys: Iterable[str] = (),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("QuickSearchDialog")
        self.setWindowTitle("Search Music Vault")
        self.setAccessibleName("Search Music Vault")
        # Existing global Space handling checks activeModalWidget. This surface
        # must not turn a space typed into a query into a playback command.
        self.setModal(True)
        self.resize(660, 560)
        self.setMinimumSize(440, 360)
        self._index = index
        self._context_keys = tuple(context_keys)
        self._results: tuple[SearchEntity, ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(self._label("Find your next listen", "SectionHeaderTitle"), 1)
        close_button = QPushButton("Close", self)
        close_button.setAccessibleName("Close search (Escape)")
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.reject)
        heading.addWidget(close_button)
        layout.addLayout(heading)

        self.query_edit = QLineEdit(self)
        self.query_edit.setAccessibleName("Search tracks, albums, artists, playlists and actions")
        self.query_edit.setPlaceholderText("Tracks, albums, artists, playlists, actions…")
        self.query_edit.setClearButtonEnabled(True)
        self.query_edit.setMaxLength(256)
        self.query_edit.installEventFilter(self)
        layout.addWidget(self.query_edit)

        self.results_list = QListWidget(self)
        self.results_list.setAccessibleName("Grouped search results")
        self.results_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results_list.setUniformItemSizes(False)
        self.results_list.installEventFilter(self)
        self.results_list.currentItemChanged.connect(self._update_controls)
        self.results_list.itemActivated.connect(lambda _item: self._activate_primary())
        layout.addWidget(self.results_list, 1)
        self.empty_label = self._label("Start typing to search your local library.", "MutedLabel")
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

        buttons = QHBoxLayout()
        self.primary_button = QPushButton("Open", self)
        self.primary_button.setObjectName("PrimaryButton")
        self.primary_button.setAutoDefault(False)
        self.primary_button.clicked.connect(self._activate_primary)
        buttons.addWidget(self.primary_button)
        self.queue_button = QPushButton("Add to queue", self)
        self.queue_button.setAutoDefault(False)
        self.queue_button.setToolTip("Append this track to the manual queue (Ctrl+Enter).")
        self.queue_button.clicked.connect(lambda: self._dispatch("queue_track"))
        buttons.addWidget(self.queue_button)
        self.more_button = QToolButton(self)
        self.more_button.setText("More")
        self.more_button.setAccessibleName("More actions for selected track")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_button)
        for label, action_id in (
            ("Go to artist", "go_artist"),
            ("Go to album", "go_album"),
            ("Add to playlist…", "add_to_playlist"),
        ):
            action = menu.addAction(label)
            action.triggered.connect(lambda _checked=False, action_id=action_id: self._dispatch(action_id))
        self.more_button.setMenu(menu)
        buttons.addWidget(self.more_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.hint_label = self._label("↑ ↓ Select  ·  Enter Open  ·  Esc Close", "MutedLabel")
        layout.addWidget(self.hint_label)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self.refresh_results)
        self.query_edit.textChanged.connect(self._schedule_search)
        self._queue_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._queue_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._queue_shortcut.activated.connect(lambda: self._dispatch("queue_track"))
        self._queue_enter_shortcut = QShortcut(QKeySequence("Ctrl+Enter"), self)
        self._queue_enter_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._queue_enter_shortcut.activated.connect(lambda: self._dispatch("queue_track"))
        self.setStyleSheet(f"""
            QDialog#QuickSearchDialog {{ background: {COLORS['elevated_surface']}; }}
            QListWidget {{ background: {COLORS['subtle_surface']};
                border: 1px solid {COLORS['border']}; border-radius: 10px; }}
            QListWidget::item {{ padding: 8px 10px; border-radius: 5px; }}
            QListWidget::item:selected {{ background: {COLORS['selection']}; }}
            QLineEdit {{ padding: 12px; }}
        """)
        self.refresh_results()

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    @property
    def results(self) -> tuple[SearchEntity, ...]:
        return self._results

    def set_index(self, index: LocalSearchIndex, *, context_keys: Iterable[str] = ()) -> None:
        """Replace an upstream-built snapshot; never load or rebuild it here."""

        self._index = index
        self._context_keys = tuple(context_keys)
        self.refresh_results()

    def _schedule_search(self, _text: str) -> None:
        self._debounce.start()
        # Stale visible rows must not act on the previous query during debounce.
        self.results_list.setEnabled(False)
        self.primary_button.setEnabled(False)
        self.queue_button.setEnabled(False)
        self.more_button.setEnabled(False)

    def refresh_results(self) -> None:
        self._debounce.stop()
        selected = self.selected_entity()
        identity = selected.identity if selected else None
        self._results = self._index.search(
            self.query_edit.text(), limit=self.RESULT_LIMIT, context_keys=self._context_keys,
        )
        first_row = -1
        selected_row = -1
        with QSignalBlocker(self.results_list):
            self.results_list.clear()
            for group in group_results(self._results):
                header = QListWidgetItem(_GROUP_LABELS[group.kind])
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setForeground(QBrush(QColor(COLORS["text_muted"])))
                font = header.font()
                font.setBold(True)
                header.setFont(font)
                self.results_list.addItem(header)
                for entity in group.items:
                    text = entity.label + ("\n" + entity.detail if entity.detail else "")
                    item = QListWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, entity)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
                    item.setToolTip("<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>")
                    self.results_list.addItem(item)
                    row = self.results_list.count() - 1
                    if first_row < 0:
                        first_row = row
                    if entity.identity == identity:
                        selected_row = row
            self.results_list.setCurrentRow(selected_row if selected_row >= 0 else first_row)
        self.results_list.setEnabled(True)
        self.empty_label.setVisible(not self._results)
        self.empty_label.setText(
            "No local matches. Try a title, artist, album or playlist."
            if self.query_edit.text().strip() else "Start typing to search your local library."
        )
        self._update_controls()

    def selected_entity(self) -> SearchEntity | None:
        item = self.results_list.currentItem()
        entity = item.data(Qt.ItemDataRole.UserRole) if item else None
        return entity if isinstance(entity, SearchEntity) else None

    def _update_controls(self, *_args) -> None:
        entity = self.selected_entity()
        is_track = entity is not None and entity.kind == "track"
        available_track = is_track and entity.track_id is not None
        self.primary_button.setText("Play" if is_track else "Open")
        self.primary_button.setEnabled(entity is not None and (not is_track or available_track))
        self.primary_button.setToolTip(
            "This track is no longer available." if is_track and not available_track
            else "Play from this ordered Search result context." if is_track
            else "Open this local destination."
        )
        self.queue_button.setEnabled(available_track)
        self.more_button.setEnabled(available_track)
        self.hint_label.setText(
            "↑ ↓ Select  ·  Enter Play  ·  Ctrl+Enter Queue  ·  Esc Close"
            if available_track else "↑ ↓ Select  ·  Enter Open  ·  Esc Close"
        )

    def _activate_primary(self) -> None:
        if self._debounce.isActive():
            self.refresh_results()
        entity = self.selected_entity()
        if entity is None:
            return
        action = "play_track" if entity.kind == "track" else "invoke_action" if entity.kind == "action" else "open_entity"
        self._dispatch(action)

    def _dispatch(self, action: str) -> None:
        if self._debounce.isActive():
            self.refresh_results()
        entity = self.selected_entity()
        if entity is None:
            return
        track_actions = {"play_track", "queue_track", "go_artist", "go_album", "add_to_playlist"}
        if action in track_actions and (entity.kind != "track" or entity.track_id is None):
            return
        ordered_ids = track_result_ids(self._results)
        # Queue keeps search open; the host confirms success and tracks recency.
        # Navigation/play/playlist selection returns control to the host first.
        if action != "queue_track":
            self.accept()
        self.action_requested.emit(action, entity, ordered_ids)

    def _move_selection(self, direction: int) -> None:
        if self._debounce.isActive():
            self.refresh_results()
        row = self.results_list.currentRow()
        target = row + direction
        while 0 <= target < self.results_list.count():
            item = self.results_list.item(target)
            if isinstance(item.data(Qt.ItemDataRole.UserRole), SearchEntity):
                self.results_list.setCurrentRow(target)
                self.results_list.scrollToItem(item)
                return
            target += direction

    def eventFilter(self, watched, event) -> bool:
        if watched in (self.query_edit, self.results_list) and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not event.modifiers():
                self._move_selection(-1 if event.key() == Qt.Key.Key_Up else 1)
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    self._dispatch("queue_track")
                elif not event.modifiers():
                    self._activate_primary()
                else:
                    return super().eventFilter(watched, event)
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.query_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.query_edit.selectAll()

    def done(self, result: int) -> None:
        self._debounce.stop()
        super().done(result)
