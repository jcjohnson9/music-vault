from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView

from music_vault.core.queue_editor import ManualQueueEditor, preview_continuation
from music_vault.ui.queue_panel import QueuePanel, QueueTrackLabel


def _populate(panel, editor, *, shuffle=False, repeat="off", autoplay=True, labels=None):
    panel.set_snapshot(
        editor.snapshot(),
        labels or {1: QueueTrackLabel("One", "Synthetic Artist"), 2: QueueTrackLabel("Two"), 3: QueueTrackLabel("Three")},
        "Current <b>track</b>",
        "Artist & guests",
        preview_continuation([1, 2, 3], 1, shuffle, autoplay, repeat, True),
        "Synthetic playlist",
        True,
    )


def test_panel_renders_literal_metadata_and_duplicate_occurrences(qapp):
    queue = [1, 1, 2]
    editor = ManualQueueEditor(lambda: queue)
    panel = QueuePanel()
    _populate(panel, editor, labels={1: QueueTrackLabel("<b>Song</b>", "A & B")})
    assert panel.queue_list.count() == 3
    assert "<b>Song</b>" in panel.queue_list.item(0).text()
    assert "&lt;b&gt;" in panel.queue_list.item(0).toolTip()
    assert panel.queue_list.item(0).data(Qt.ItemDataRole.UserRole) != panel.queue_list.item(1).data(Qt.ItemDataRole.UserRole)
    assert panel.now_title.text() == "Current <b>track</b>"
    assert panel.now_title.textFormat() == Qt.TextFormat.PlainText
    assert "Unavailable" in panel.queue_list.item(2).text()
    assert panel.queue_list.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop
    panel.close()


def test_remove_and_move_emit_occurrence_revision_without_optimistic_mutation(qapp):
    queue = [1, 1, 2]
    editor = ManualQueueEditor(lambda: queue)
    panel = QueuePanel()
    _populate(panel, editor)
    old = editor.snapshot()
    removed, moved = [], []
    panel.remove_requested.connect(lambda *args: removed.append(args))
    panel.move_requested.connect(lambda *args: moved.append(args))
    panel.queue_list.setCurrentRow(1)
    panel.remove_button.click()
    panel.up_button.click()
    panel.down_button.click()
    assert removed == [(old.entries[1].token, old.revision)]
    assert moved == [(old.entries[1].token, old.entries[0].token, old.revision), (old.entries[1].token, None, old.revision)]
    assert queue == [1, 1, 2]
    assert panel.queue_list.count() == 3
    assert panel.queue_list.currentRow() == 1
    panel.close()


def test_panel_reconciles_host_snapshot_and_preserves_occurrence_selection(qapp):
    queue = [1, 2, 3]
    editor = ManualQueueEditor(lambda: queue)
    panel = QueuePanel()
    _populate(panel, editor)
    panel.queue_list.setCurrentRow(1)
    selected_token = editor.snapshot().entries[1].token
    panel.move_requested.connect(lambda token, before, revision: (editor.move(token, before, revision), _populate(panel, editor)))
    panel.up_button.click()
    assert queue == [2, 1, 3]
    assert panel.queue_list.currentRow() == 0
    assert panel.queue_list.currentItem().data(Qt.ItemDataRole.UserRole) == selected_token
    assert not panel.up_button.isEnabled()
    assert panel.down_button.isEnabled()
    assert panel.undo_button.isEnabled()
    editor.pop_next()
    _populate(panel, editor)
    assert panel.queue_list.currentRow() == 0
    assert not panel.undo_button.isEnabled()
    panel.close()


def test_empty_queue_disables_edits_and_clear_but_allows_clear_undo(qapp):
    queue = [1]
    editor = ManualQueueEditor(lambda: queue)
    panel = QueuePanel()
    _populate(panel, editor)
    old = editor.snapshot()
    editor.clear(old.revision)
    _populate(panel, editor)
    assert not panel.clear_button.isEnabled()
    assert not panel.remove_button.isEnabled()
    assert not panel.up_button.isEnabled()
    assert not panel.down_button.isEnabled()
    assert panel.undo_button.isEnabled()
    assert not panel.empty_label.isHidden()
    requested = []
    panel.undo_requested.connect(requested.append)
    panel.undo_button.click()
    assert requested == [editor.snapshot().revision]
    assert queue == []
    panel.close()


def test_clear_return_and_close_are_signal_only(qapp):
    queue = [1, 2]
    editor = ManualQueueEditor(lambda: queue)
    panel = QueuePanel()
    _populate(panel, editor)
    events = []
    panel.clear_requested.connect(lambda revision: events.append(("clear", revision)))
    panel.return_context_requested.connect(lambda: events.append(("return",)))
    panel.close_requested.connect(lambda: events.append(("close",)))
    panel.clear_button.click()
    panel.return_button.click()
    panel.close_button.click()
    assert events == [("clear", editor.snapshot().revision), ("return",), ("close",)]
    assert queue == [1, 2]
    panel.close()


def test_shuffle_preview_is_unordered_and_repeat_one_is_visible(qapp):
    queue = [1]
    editor = ManualQueueEditor(lambda: queue)
    panel = QueuePanel()
    _populate(panel, editor, shuffle=True, autoplay=False, repeat="one")
    assert panel.following_list.item(0).text().startswith("• ")
    assert "not chosen yet" in panel.continuation_explanation.text()
    assert "Repeat One" in panel.queue_explanation.text()
    assert "Next" in panel.queue_explanation.text()
    panel.close()


def test_large_base_preview_is_bounded_without_fabricating_known_labels(qapp):
    editor = ManualQueueEditor(lambda: [])
    panel = QueuePanel()
    panel.set_snapshot(editor.snapshot(), {}, None, None, preview_continuation(list(range(5000)), 0, False, True, "off", True), "Library", False)
    assert panel.following_list.count() == panel.PREVIEW_LIMIT
    assert panel.more_label.text() == "4987 more in this context"
    assert panel.now_title.text() == "Nothing playing"
    assert not panel.return_button.isEnabled()
    assert "Unavailable" in panel.following_list.item(0).text()
    panel.close()


def test_keyboard_edits_have_scoped_shortcuts_and_accessible_buttons(qapp):
    queue = [1, 2, 3]
    editor = ManualQueueEditor(lambda: queue)
    panel = QueuePanel()
    _populate(panel, editor)
    shortcuts = {shortcut.key().toString(): shortcut for shortcut in panel._shortcuts}
    assert shortcuts[QKeySequence("Ctrl+Up").toString()].context() == Qt.ShortcutContext.WidgetShortcut
    assert shortcuts[QKeySequence("Delete").toString()].parent() is panel.queue_list
    assert shortcuts[QKeySequence("Ctrl+Z").toString()].context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    assert panel.up_button.accessibleName()
    assert panel.down_button.accessibleName()
    assert panel.remove_button.accessibleName()
    removed, moved = [], []
    panel.remove_requested.connect(lambda *args: removed.append(args))
    panel.move_requested.connect(lambda *args: moved.append(args))
    panel.resize(400, 850)
    panel.show()
    panel.activateWindow()
    panel.queue_list.setFocus()
    panel.queue_list.setCurrentRow(1)
    qapp.processEvents()
    QTest.keyClick(panel.queue_list, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(panel.queue_list, Qt.Key.Key_Delete)
    assert moved == [(editor.snapshot().entries[1].token, editor.snapshot().entries[0].token, editor.snapshot().revision)]
    assert removed == [(editor.snapshot().entries[1].token, editor.snapshot().revision)]
    assert queue == [1, 2, 3]
    panel.close()
