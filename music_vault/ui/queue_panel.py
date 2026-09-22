"""A signal-only listening queue surface; the host remains playback authority."""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from music_vault.core.queue_editor import ContinuationPreview, QueueSnapshot
from music_vault.ui.theme import COLORS


@dataclass(frozen=True)
class QueueTrackLabel:
    title: str
    artist: str = ""
    available: bool = True


class QueuePanel(QWidget):
    remove_requested = Signal(int, int)
    move_requested = Signal(int, object, int)
    clear_requested = Signal(int)
    undo_requested = Signal(int)
    return_context_requested = Signal()
    close_requested = Signal()

    PREVIEW_LIMIT = 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ListeningQueuePanel")
        self.setAccessibleName("Playback queue")
        self.setMinimumWidth(270)
        self._snapshot = QueueSnapshot(0, (), False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        heading.addWidget(self._label("Queue", "SectionHeaderTitle"), 1)
        self.close_button = QPushButton("Close", self)
        self.close_button.setAccessibleName("Close queue")
        self.close_button.clicked.connect(self.close_requested.emit)
        heading.addWidget(self.close_button)
        layout.addLayout(heading)

        layout.addWidget(self._label("Now playing", "CardTitle"))
        self.now_title = self._label("Nothing playing")
        self.now_artist = self._label("", "MutedLabel")
        layout.addWidget(self.now_title)
        layout.addWidget(self.now_artist)

        added_heading = QHBoxLayout()
        self.added_heading = self._label("Added by you · 0", "CardTitle")
        added_heading.addWidget(self.added_heading, 1)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.setAccessibleName("Clear queued tracks")
        self.clear_button.clicked.connect(lambda: self.clear_requested.emit(self._snapshot.revision))
        added_heading.addWidget(self.clear_button)
        self.undo_button = QPushButton("Undo", self)
        self.undo_button.setAccessibleName("Undo last queue edit")
        self.undo_button.setToolTip("Undo last edit (Ctrl+Z). Expires when the queue advances or grows.")
        self.undo_button.clicked.connect(self._undo)
        added_heading.addWidget(self.undo_button)
        layout.addLayout(added_heading)
        self.queue_explanation = self._label("", "MutedLabel")
        layout.addWidget(self.queue_explanation)

        self.queue_list = QListWidget(self)
        self.queue_list.setAccessibleName("Tracks added by you")
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.queue_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.queue_list.setMinimumHeight(110)
        self.queue_list.currentRowChanged.connect(self._update_controls)
        layout.addWidget(self.queue_list, 3)
        self.empty_label = self._label("No tracks added. Use Add to queue from any track.", "MutedLabel")
        layout.addWidget(self.empty_label)

        edits = QHBoxLayout()
        self.up_button = QPushButton("Move up", self)
        self.down_button = QPushButton("Move down", self)
        self.remove_button = QPushButton("Remove", self)
        self.up_button.setAccessibleName("Move selected queued occurrence up")
        self.down_button.setAccessibleName("Move selected queued occurrence down")
        self.remove_button.setAccessibleName("Remove selected queued occurrence")
        self.up_button.setToolTip("Move up (Ctrl+Up)")
        self.down_button.setToolTip("Move down (Ctrl+Down)")
        self.remove_button.setToolTip("Remove from queue (Delete); the current song is unaffected.")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.remove_button.clicked.connect(self._remove)
        for button in (self.up_button, self.down_button, self.remove_button):
            edits.addWidget(button)
        layout.addLayout(edits)

        layout.addWidget(self._label("Following", "CardTitle"))
        self.context_label = self._label("No playback context", "MutedLabel")
        layout.addWidget(self.context_label)
        self.continuation_explanation = self._label("", "MutedLabel")
        layout.addWidget(self.continuation_explanation)
        self.following_list = QListWidget(self)
        self.following_list.setAccessibleName("Following from the captured playback context")
        self.following_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.following_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.following_list.setMinimumHeight(80)
        layout.addWidget(self.following_list, 2)
        self.more_label = self._label("", "MutedLabel")
        layout.addWidget(self.more_label)
        self.return_button = QPushButton("Return to context", self)
        self.return_button.setAccessibleName("Return to original playback context without changing playback")
        self.return_button.clicked.connect(self.return_context_requested.emit)
        layout.addWidget(self.return_button)

        self._shortcuts: list[QShortcut] = []
        for sequence, callback in (("Ctrl+Up", lambda: self._move(-1)), ("Ctrl+Down", lambda: self._move(1)), ("Delete", self._remove)):
            shortcut = QShortcut(QKeySequence(sequence), self.queue_list)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        for sequence, callback in (("Ctrl+Z", self._undo), ("Escape", self.close_requested.emit)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

        self.setStyleSheet(f"""
            QWidget#ListeningQueuePanel {{ background: {COLORS['card_surface']};
                border-left: 1px solid {COLORS['border']}; }}
            QListWidget {{ background: {COLORS['subtle_surface']};
                border: 1px solid {COLORS['border']}; border-radius: 8px; }}
            QListWidget::item {{ padding: 8px; border-radius: 4px; }}
            QListWidget::item:selected {{ background: {COLORS['selection']}; }}
        """)
        self._update_controls()

    @staticmethod
    def _label(text: str, object_name: str = "") -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        if object_name:
            label.setObjectName(object_name)
        return label

    @staticmethod
    def _track_item(label: QueueTrackLabel, prefix: str = "") -> QListWidgetItem:
        title = label.title or "Untitled track"
        detail = label.artist
        if not label.available:
            detail = "Unavailable · " + detail if detail else "Unavailable"
        text = prefix + title + ("\n" + detail if detail else "")
        item = QListWidgetItem(text)
        item.setToolTip("<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>")
        item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
        return item

    def set_snapshot(
        self,
        snapshot: QueueSnapshot,
        track_labels: Mapping[int, QueueTrackLabel],
        now_title: str | None,
        now_artist: str | None,
        continuation: ContinuationPreview,
        context_label: str,
        can_return_to_context: bool,
    ) -> None:
        previous = self.queue_list.currentItem()
        previous_token = previous.data(Qt.ItemDataRole.UserRole) if previous else None
        previous_row = self.queue_list.currentRow()
        self._snapshot = snapshot
        self.now_title.setText(now_title or "Nothing playing")
        self.now_artist.setText(now_artist or "")
        self.added_heading.setText(f"Added by you · {len(snapshot.entries)}")
        self.queue_explanation.setText(continuation.queue_explanation)
        with QSignalBlocker(self.queue_list):
            self.queue_list.clear()
            selected_row = -1
            for index, entry in enumerate(snapshot.entries):
                label = track_labels.get(entry.track_id, QueueTrackLabel("Unavailable track", available=False))
                item = self._track_item(label, f"{index + 1}. ")
                item.setData(Qt.ItemDataRole.UserRole, entry.token)
                self.queue_list.addItem(item)
                if entry.token == previous_token:
                    selected_row = index
            if selected_row < 0 and snapshot.entries:
                selected_row = min(max(0, previous_row), len(snapshot.entries) - 1)
            self.queue_list.setCurrentRow(selected_row)
        self.empty_label.setVisible(not snapshot.entries)
        self.context_label.setText(context_label or "No playback context")
        self.continuation_explanation.setText(continuation.explanation)
        self.following_list.clear()
        for index, track_id in enumerate(continuation.track_ids[: self.PREVIEW_LIMIT]):
            label = track_labels.get(track_id, QueueTrackLabel("Unavailable track", available=False))
            prefix = "• " if continuation.mode == "shuffle" else f"{index + 1}. "
            self.following_list.addItem(self._track_item(label, prefix))
        remaining = len(continuation.track_ids) - self.PREVIEW_LIMIT
        self.more_label.setText(f"{remaining} more in this context" if remaining > 0 else "")
        self.more_label.setVisible(remaining > 0)
        self.return_button.setEnabled(can_return_to_context)
        self._update_controls()

    def _update_controls(self, _row: int = -1) -> None:
        row = self.queue_list.currentRow()
        count = len(self._snapshot.entries)
        selected = 0 <= row < count
        self.up_button.setEnabled(selected and row > 0)
        self.down_button.setEnabled(selected and row + 1 < count)
        self.remove_button.setEnabled(selected)
        self.clear_button.setEnabled(bool(count))
        self.undo_button.setEnabled(self._snapshot.can_undo)

    def _remove(self) -> None:
        row = self.queue_list.currentRow()
        if 0 <= row < len(self._snapshot.entries):
            self.remove_requested.emit(self._snapshot.entries[row].token, self._snapshot.revision)

    def _move(self, direction: int) -> None:
        row = self.queue_list.currentRow()
        entries = self._snapshot.entries
        if not 0 <= row < len(entries) or not 0 <= row + direction < len(entries):
            return
        destination = row - 1 if direction < 0 else row + 2
        before = entries[destination].token if destination < len(entries) else None
        self.move_requested.emit(entries[row].token, before, self._snapshot.revision)

    def _undo(self) -> None:
        if self._snapshot.can_undo:
            self.undo_requested.emit(self._snapshot.revision)
