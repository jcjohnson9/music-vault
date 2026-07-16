from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QModelIndex, QObject, QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from music_vault.ui.components import ElidedLabel, EmptyState
from music_vault.ui.icons import ui_icon
from music_vault.ui.theme import COLORS, repolish
from music_vault.core.safety import sanitize_error_text


SOURCE_ID_ROLE = int(Qt.ItemDataRole.UserRole) + 301
SOURCE_VIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 302

SUMMARY_FIELDS = (
    ("enabled_sources", "Enabled Sources"),
    ("completed_sources", "Completed"),
    ("issue_sources", "Source Issues"),
    ("failed_sources", "Failed Sources"),
    ("downloaded", "Downloaded"),
    ("existing", "Existing"),
    ("failed_items", "Failed Items"),
)


def _value(source: object, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        try:
            return source[name]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            pass
        value = getattr(source, name, None)
        if value is not None:
            return value
    return default


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "Never synchronized"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%b %d, %Y %I:%M %p")
    except (ValueError, OSError):
        return text[:32]


def _friendly_source_kind(value: object) -> str:
    normalized = str(value or "youtube_playlist").replace("_", " ").strip()
    return normalized.title() or "YouTube Playlist"


def _friendly_status(value: object) -> str:
    normalized = str(value or "idle").strip().casefold()
    return {
        "idle": "Not synced",
        "complete": "Complete",
        "complete_with_issues": "Complete with issues",
        "failed": "Failed",
        "syncing": "Synchronizing",
        "stopped": "Stopped after current",
        "cancelled": "Stopped after current",
    }.get(normalized, normalized.replace("_", " ").title() or "Not synced")


@dataclass(frozen=True)
class SyncSourceView:
    id: int
    external_id: str
    source_kind: str
    source_url: str
    label: str | None
    remote_title: str | None
    enabled: bool
    sort_order: int
    destination_kind: str
    destination_playlist_id: int | None
    destination_playlist_name: str | None
    storage_key: str
    last_sync_at: str | None
    last_sync_status: str | None
    downloaded_count: int
    imported_count: int
    existing_count: int
    failed_count: int
    unresolved_failure_count: int
    last_error: str | None

    @property
    def display_label(self) -> str:
        return (
            (self.label or "").strip()
            or (self.remote_title or "").strip()
            or "Saved YouTube Source"
        )

    @property
    def destination_label(self) -> str:
        if self.destination_kind == "playlist":
            return self.destination_playlist_name or "Managed Local Playlist"
        return "Library Only"

    @property
    def status_label(self) -> str:
        return _friendly_status(self.last_sync_status)

    @property
    def shortened_external_id(self) -> str:
        value = self.external_id.strip()
        if len(value) <= 18:
            return value
        return f"{value[:10]}…{value[-6:]}"

    @classmethod
    def from_source(cls, source: object) -> "SyncSourceView":
        destination_id = _value(source, "destination_playlist_id")
        return cls(
            id=_integer(_value(source, "id")),
            external_id=str(_value(source, "external_id", default="") or ""),
            source_kind=str(
                _value(source, "source_kind", default="youtube_playlist")
                or "youtube_playlist"
            ),
            source_url=str(_value(source, "source_url", default="") or ""),
            label=_value(source, "label"),
            remote_title=_value(source, "remote_title"),
            enabled=bool(_value(source, "enabled", default=True)),
            sort_order=_integer(_value(source, "sort_order")),
            destination_kind=str(
                _value(source, "destination_kind", default="library") or "library"
            ),
            destination_playlist_id=(
                _integer(destination_id) if destination_id is not None else None
            ),
            destination_playlist_name=_value(
                source,
                "destination_playlist_name",
                "playlist_name",
            ),
            storage_key=str(_value(source, "storage_key", default="") or ""),
            last_sync_at=_value(source, "last_sync_at"),
            last_sync_status=_value(source, "last_sync_status"),
            downloaded_count=_integer(
                _value(source, "last_downloaded_count", "downloaded_count")
            ),
            imported_count=_integer(
                _value(source, "last_imported_count", "imported_count")
            ),
            existing_count=_integer(
                _value(source, "last_existing_count", "existing_count")
            ),
            failed_count=_integer(
                _value(source, "last_failed_count", "failed_count")
            ),
            unresolved_failure_count=_integer(
                _value(source, "unresolved_failure_count", "failure_count")
            ),
            last_error=(
                str(_value(source, "last_error"))
                if _value(source, "last_error")
                else None
            ),
        )


@dataclass(frozen=True)
class SourceEditorValues:
    source_value: str
    external_id: str
    source_url: str
    label: str | None
    enabled: bool
    sort_order: int
    destination_kind: str
    destination_playlist_id: int | None
    new_playlist_name: str | None


class SyncSourceItemDelegate(QStyledItemDelegate):
    """Paint source rows without allocating a widget hierarchy per source."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(max(320, option.rect.width()), 88)

    @staticmethod
    def _view(index: QModelIndex) -> SyncSourceView | None:
        value = index.data(SOURCE_VIEW_ROLE)
        return value if isinstance(value, SyncSourceView) else None

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        view = self._view(index)
        if view is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect.adjusted(2, 3, -2, -3)
        selected = bool(option.state & option.state.State_Selected)
        background = COLORS["selection"] if selected else COLORS["card_surface"]
        border = COLORS["accent_pressed"] if selected else COLORS["border"]
        painter.setPen(QPen(QColor(border), 1))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(rect, 13, 13)

        box = QRect(rect.left() + 14, rect.center().y() - 9, 18, 18)
        painter.setPen(QPen(QColor(COLORS["strong_border"]), 1))
        painter.setBrush(
            QColor(COLORS["accent"] if view.enabled else COLORS["subtle_surface"])
        )
        painter.drawRoundedRect(box, 5, 5)
        if view.enabled:
            painter.setPen(QPen(QColor(COLORS["accent_ink"]), 2))
            painter.drawLine(box.left() + 4, box.center().y(), box.left() + 8, box.bottom() - 4)
            painter.drawLine(box.left() + 8, box.bottom() - 4, box.right() - 3, box.top() + 4)

        left = box.right() + 13
        right = rect.right() - 14
        painter.setFont(QFont(option.font.family(), option.font.pointSize(), QFont.Weight.DemiBold))
        painter.setPen(QColor(COLORS["text_primary"] if view.enabled else COLORS["disabled_text"]))
        title = QFontMetrics(painter.font()).elidedText(
            view.display_label,
            Qt.TextElideMode.ElideRight,
            max(80, right - left - 122),
        )
        painter.drawText(QRect(left, rect.top() + 10, right - left, 21), Qt.AlignmentFlag.AlignVCenter, title)

        status_text = view.status_label
        status_width = min(128, max(62, QFontMetrics(option.font).horizontalAdvance(status_text) + 20))
        status_rect = QRect(right - status_width, rect.top() + 9, status_width, 22)
        status_color = {
            "complete": COLORS["accent_hover"],
            "complete_with_issues": COLORS["warning"],
            "failed": COLORS["danger"],
            "syncing": COLORS["accent_hover"],
        }.get(str(view.last_sync_status or "").casefold(), COLORS["text_muted"])
        painter.setPen(QPen(QColor(status_color), 1))
        status_background = QColor(status_color)
        status_background.setAlpha(36)
        painter.setBrush(status_background)
        painter.drawRoundedRect(status_rect, 10, 10)
        painter.setFont(option.font)
        painter.setPen(QColor(status_color))
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, status_text)

        destination_prefix = "Managed • " if view.destination_kind == "playlist" else ""
        destination = destination_prefix + view.destination_label
        painter.setPen(QColor(COLORS["text_secondary"] if view.enabled else COLORS["disabled_text"]))
        destination = QFontMetrics(option.font).elidedText(
            f"{_friendly_source_kind(view.source_kind)} • {destination}",
            Qt.TextElideMode.ElideRight,
            right - left,
        )
        painter.drawText(QRect(left, rect.top() + 35, right - left, 19), Qt.AlignmentFlag.AlignVCenter, destination)

        metrics = (
            f"{view.downloaded_count} downloaded  •  {view.imported_count} imported  •  "
            f"{view.unresolved_failure_count or view.failed_count} unresolved"
        )
        painter.setPen(QColor(COLORS["text_muted"] if view.enabled else COLORS["disabled_text"]))
        metrics = QFontMetrics(option.font).elidedText(
            metrics,
            Qt.TextElideMode.ElideRight,
            right - left,
        )
        painter.drawText(QRect(left, rect.top() + 58, right - left, 18), Qt.AlignmentFlag.AlignVCenter, metrics)
        painter.restore()

    def editorEvent(self, event, model, option, index):  # noqa: N802
        if event.type() == QEvent.Type.MouseButtonRelease:
            rect = option.rect.adjusted(2, 3, -2, -3)
            checkbox = QRect(rect.left() + 8, rect.center().y() - 16, 38, 32)
            if checkbox.contains(event.position().toPoint()):
                state = index.data(Qt.ItemDataRole.CheckStateRole)
                next_state = (
                    Qt.CheckState.Unchecked
                    if state == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                return model.setData(index, next_state, Qt.ItemDataRole.CheckStateRole)
        return super().editorEvent(event, model, option, index)


class SyncMetricCard(QFrame):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SyncMetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.value_label = QLabel("0")
        self.value_label.setObjectName("SyncMetricValue")
        self.caption_label = QLabel(label)
        self.caption_label.setObjectName("TinyLabel")
        layout.addWidget(self.value_label)
        layout.addWidget(self.caption_label)

    def set_value(self, value: object) -> None:
        self.value_label.setText(str(value if value is not None else 0))


class SyncCenterWidget(QWidget):
    add_source_requested = Signal()
    edit_source_requested = Signal(int)
    remove_source_requested = Signal(int)
    move_source_requested = Signal(int, int)
    source_enabled_changed = Signal(int, bool)
    source_selection_changed = Signal(object)
    sync_selected_requested = Signal(object)
    sync_all_requested = Signal()
    stop_after_current_requested = Signal()
    clear_activity_requested = Signal()
    clear_source_failures_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SyncCenter")
        self._sources: dict[int, SyncSourceView] = {}
        self._changing_items = False
        self._batch_active = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        header = QFrame()
        header.setObjectName("TopHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 18, 22, 18)
        header_layout.setSpacing(10)
        heading = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title = QLabel("Sync Center")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Manage authorized public or unlisted YouTube playlist sources. "
            "Synchronization runs only when you start it."
        )
        subtitle.setObjectName("MutedLabel")
        subtitle.setWordWrap(True)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        heading.addLayout(title_col, 1)

        self.add_button = self._button("Add Source", "add", primary=True)
        self.edit_button = self._button("Edit Source", "metadata")
        self.remove_button = self._button("Remove Source", "remove", danger=True)
        self.add_button.clicked.connect(self.add_source_requested)
        self.edit_button.clicked.connect(self._emit_edit)
        self.remove_button.clicked.connect(self._emit_remove)
        heading.addWidget(self.add_button)
        heading.addWidget(self.edit_button)
        heading.addWidget(self.remove_button)
        header_layout.addLayout(heading)

        root.addWidget(header)

        summary_grid = QGridLayout()
        summary_grid.setContentsMargins(0, 0, 0, 0)
        summary_grid.setHorizontalSpacing(10)
        summary_grid.setVerticalSpacing(10)
        self.summary_cards: dict[str, SyncMetricCard] = {}
        for index, (name, label) in enumerate(SUMMARY_FIELDS):
            card = SyncMetricCard(label)
            self.summary_cards[name] = card
            summary_grid.addWidget(card, index // 4, index % 4)
        root.addLayout(summary_grid)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("SyncSourceSplitter")
        splitter.setChildrenCollapsible(False)

        source_card = QFrame()
        source_card.setObjectName("Card")
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(14, 14, 14, 14)
        source_layout.setSpacing(10)
        source_header = QHBoxLayout()
        source_title = QLabel("Saved Sources")
        source_title.setObjectName("CardTitle")
        self.source_count_label = QLabel("0 sources")
        self.source_count_label.setObjectName("TinyLabel")
        source_header.addWidget(source_title)
        source_header.addStretch(1)
        source_header.addWidget(self.source_count_label)
        source_layout.addLayout(source_header)

        self.source_list = QListWidget()
        self.source_list.setObjectName("SyncSourceList")
        self.source_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.source_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.source_list.setItemDelegate(SyncSourceItemDelegate(self.source_list))
        self.source_list.setAccessibleName("Saved synchronization sources")
        self.source_list.itemChanged.connect(self._item_changed)
        self.source_list.itemSelectionChanged.connect(self._selection_changed)
        self.source_list.currentItemChanged.connect(
            lambda _current, _previous: self._selection_changed()
        )
        source_layout.addWidget(self.source_list, 1)

        ordering = QHBoxLayout()
        self.move_up_button = self._button("Move Up", "chevron-down")
        self.move_down_button = self._button("Move Down", "chevron-down")
        self.move_up_button.setProperty("direction", "up")
        self.move_down_button.setProperty("direction", "down")
        self.move_up_button.clicked.connect(lambda: self._emit_move(-1))
        self.move_down_button.clicked.connect(lambda: self._emit_move(1))
        ordering.addWidget(self.move_up_button)
        ordering.addWidget(self.move_down_button)
        ordering.addStretch(1)
        source_layout.addLayout(ordering)

        self.detail_stack = QStackedWidget()
        self.detail_stack.setObjectName("SyncSourceDetailStack")
        self.empty_state = EmptyState(
            "sync",
            "No saved sources yet",
            "Add an authorized YouTube playlist source. Saving a source does not start synchronization.",
            "Add Source",
        )
        if self.empty_state.action_button is not None:
            self.empty_state.action_button.clicked.connect(self.add_source_requested)
        self.detail_stack.addWidget(self.empty_state)
        self.detail_panel = self._build_detail_panel()
        self.detail_stack.addWidget(self.detail_panel)

        splitter.addWidget(source_card)
        splitter.addWidget(self.detail_stack)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([460, 560])
        root.addWidget(splitter, 1)

        operation_card = QFrame()
        operation_card.setObjectName("Card")
        operation_layout = QVBoxLayout(operation_card)
        operation_layout.setContentsMargins(16, 14, 16, 14)
        operation_layout.setSpacing(10)
        action_row = QHBoxLayout()
        self.sync_selected_button = self._button("Sync Selected", "sync", primary=True)
        self.sync_all_button = self._button("Sync All Enabled", "sync")
        self.stop_button = self._button("Stop After Current", "warning", danger=True)
        self.clear_activity_button = self._button("Clear Activity", "remove")
        self.sync_selected_button.clicked.connect(self._emit_sync_selected)
        self.sync_all_button.clicked.connect(self.sync_all_requested)
        self.stop_button.clicked.connect(self.stop_after_current_requested)
        self.clear_activity_button.clicked.connect(self.clear_activity_requested)
        action_row.addWidget(self.sync_selected_button)
        action_row.addWidget(self.sync_all_button)
        action_row.addWidget(self.stop_button)
        action_row.addWidget(self.clear_activity_button)
        action_row.addStretch(1)
        operation_layout.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setObjectName("SyncProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        operation_layout.addWidget(self.progress)

        self.activity_log = QTextEdit()
        self.activity_log.setObjectName("SyncLog")
        self.activity_log.setReadOnly(True)
        self.activity_log.setAccessibleName("Synchronization activity")
        self.activity_log.document().setMaximumBlockCount(500)
        self.activity_log.setPlaceholderText(
            "No source activity yet. Source progress will appear here."
        )
        self.activity_log.setMaximumHeight(116)
        operation_layout.addWidget(self.activity_log)
        root.addWidget(operation_card)

        self.clear_activity_requested.connect(self.activity_log.clear)
        self.set_batch_state("idle")
        self._update_actions()

    @staticmethod
    def _button(
        text: str,
        icon_name: str,
        *,
        primary: bool = False,
        danger: bool = False,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(
            "PrimaryButton" if primary else "DangerButton" if danger else "SoftButton"
        )
        button.setIcon(ui_icon(icon_name, 18))
        button.setAccessibleName(text)
        button.setToolTip(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _build_detail_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("Card")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        self.detail_name = ElidedLabel("Saved Source")
        self.detail_name.setObjectName("CardTitle")
        self.detail_status = QLabel("Not synced")
        self.detail_status.setObjectName("SyncStatusBadge")
        heading.addWidget(self.detail_name, 1)
        heading.addWidget(self.detail_status)
        layout.addLayout(heading)

        self.detail_remote_title = ElidedLabel("")
        self.detail_remote_title.setObjectName("MutedLabel")
        self.detail_identity = QLabel()
        self.detail_identity.setObjectName("TinyLabel")
        self.detail_destination = QLabel()
        self.detail_destination.setObjectName("StatusLine")
        self.detail_destination.setWordWrap(True)
        self.detail_folder = QLabel()
        self.detail_folder.setObjectName("StatusLine")
        self.detail_folder.setWordWrap(True)
        layout.addWidget(self.detail_remote_title)
        layout.addWidget(self.detail_identity)
        layout.addWidget(self.detail_destination)
        layout.addWidget(self.detail_folder)

        metrics = QGridLayout()
        self.detail_metric_labels: dict[str, QLabel] = {}
        for index, (name, title) in enumerate(
            (
                ("downloaded", "Downloaded"),
                ("imported", "Imported"),
                ("existing", "Existing"),
                ("failed", "Unresolved"),
            )
        ):
            card = SyncMetricCard(title)
            self.detail_metric_labels[name] = card.value_label
            metrics.addWidget(card, 0, index)
        layout.addLayout(metrics)

        self.detail_error = QLabel("No current source error.")
        self.detail_error.setObjectName("SyncSourceError")
        self.detail_error.setWordWrap(True)
        layout.addWidget(self.detail_error)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("SyncDetailTabs")
        self.run_history = QListWidget()
        self.run_history.setObjectName("SyncHistoryList")
        self.run_history.setAccessibleName("Recent source synchronization runs")
        self.failure_history = QListWidget()
        self.failure_history.setObjectName("SyncFailureList")
        self.failure_history.setAccessibleName("Source-specific unresolved failures")
        self.source_activity = QTextEdit()
        self.source_activity.setObjectName("SyncLog")
        self.source_activity.setReadOnly(True)
        self.source_activity.setAccessibleName("Source-specific activity")
        self.source_activity.document().setMaximumBlockCount(100)
        self.detail_tabs.addTab(self.run_history, "Recent Runs")
        self.detail_tabs.addTab(self.failure_history, "Failures")
        self.detail_tabs.addTab(self.source_activity, "Activity")
        layout.addWidget(self.detail_tabs, 1)
        detail_actions = QHBoxLayout()
        self.clear_source_failures_button = self._button(
            "Clear Source Failure History",
            "remove",
            danger=True,
        )
        self.clear_source_failures_button.clicked.connect(
            self._emit_clear_source_failures
        )
        detail_actions.addWidget(self.clear_source_failures_button)
        detail_actions.addStretch(1)
        layout.addLayout(detail_actions)
        return panel

    def set_sources(self, sources: Iterable[object]) -> None:
        views = sorted(
            (SyncSourceView.from_source(source) for source in sources),
            key=lambda source: (source.sort_order, source.id),
        )
        previous = set(self.selected_source_ids())
        current = self.current_source_id()
        self._sources = {view.id: view for view in views if view.id > 0}
        self._changing_items = True
        try:
            self.source_list.clear()
            for view in views:
                if view.id <= 0:
                    continue
                item = QListWidgetItem()
                item.setData(SOURCE_ID_ROLE, view.id)
                item.setData(SOURCE_VIEW_ROLE, view)
                item.setData(
                    Qt.ItemDataRole.CheckStateRole,
                    Qt.CheckState.Checked if view.enabled else Qt.CheckState.Unchecked,
                )
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setData(
                    Qt.ItemDataRole.AccessibleDescriptionRole,
                    f"{view.display_label}; {view.destination_label}; {view.status_label}; "
                    f"{'enabled' if view.enabled else 'disabled'}",
                )
                item.setToolTip(
                    f"{view.display_label}\n{view.destination_label}\n{view.status_label}"
                )
                self.source_list.addItem(item)
                if view.id in previous:
                    item.setSelected(True)
                if current == view.id:
                    self.source_list.setCurrentItem(item)
        finally:
            self._changing_items = False

        count = len(self._sources)
        self.source_count_label.setText(f"{count} source{'s' if count != 1 else ''}")
        if count == 0:
            self.detail_stack.setCurrentWidget(self.empty_state)
        elif self.source_list.currentItem() is None:
            self.source_list.setCurrentRow(0)
        self._update_actions()

    def set_summary(self, summary: Mapping[str, object] | object | None) -> None:
        summary = summary or {}
        aliases = {
            "enabled_sources": ("enabled_sources", "enabled_source_count"),
            "completed_sources": ("completed_sources", "completed_source_count"),
            "issue_sources": ("issue_sources", "issue_source_count"),
            "failed_sources": ("failed_sources", "failed_source_count"),
            "downloaded": ("downloaded", "downloaded_count", "total_downloaded"),
            "existing": ("existing", "existing_count", "total_existing"),
            "failed_items": ("failed_items", "failed_count", "total_failed_items"),
        }
        for name, card in self.summary_cards.items():
            card.set_value(_value(summary, *aliases[name], default=0))

    def set_source_detail(
        self,
        source: object | None,
        *,
        runs: Sequence[object] = (),
        failures: Sequence[object] = (),
        activity: Sequence[str] | str = (),
    ) -> None:
        if source is None:
            self.detail_stack.setCurrentWidget(self.empty_state)
            return
        view = source if isinstance(source, SyncSourceView) else SyncSourceView.from_source(source)
        self.detail_stack.setCurrentWidget(self.detail_panel)
        self.detail_name.setText(view.display_label)
        self.detail_remote_title.setText(
            view.remote_title or "Remote title will appear after a successful synchronization."
        )
        self.detail_identity.setText(
            f"{_friendly_source_kind(view.source_kind)} • ID {view.shortened_external_id}"
        )
        self.detail_destination.setText(f"Destination: {view.destination_label}")
        relative_folder = (
            str(Path("sources") / view.storage_key)
            if view.storage_key
            else "Assigned after the source is saved"
        )
        self.detail_folder.setText(f"Stable Download Folder: {relative_folder}")
        self.detail_status.setText(view.status_label)
        self.detail_status.setProperty(
            "syncState", str(view.last_sync_status or "idle").casefold()
        )
        repolish(self.detail_status)
        values = {
            "downloaded": view.downloaded_count,
            "imported": view.imported_count,
            "existing": view.existing_count,
            "failed": view.unresolved_failure_count or view.failed_count,
        }
        for name, value in values.items():
            self.detail_metric_labels[name].setText(str(value))
        self.detail_error.setText(
            f"Latest issue: {view.last_error}" if view.last_error else "No current source error."
        )
        self.detail_error.setProperty("hasError", bool(view.last_error))
        repolish(self.detail_error)

        self.run_history.clear()
        for run in runs[:25]:
            status = _friendly_status(_value(run, "status"))
            timestamp = _format_timestamp(_value(run, "finished_at", "started_at"))
            counts = (
                f"{_integer(_value(run, 'downloaded_count'))} downloaded • "
                f"{_integer(_value(run, 'existing_count'))} existing • "
                f"{_integer(_value(run, 'failed_count'))} failed"
            )
            self.run_history.addItem(f"{status} — {timestamp}\n{counts}")
        if self.run_history.count() == 0:
            self.run_history.addItem("No synchronization runs recorded for this source.")

        self.failure_history.clear()
        for failure in failures[:50]:
            title = str(_value(failure, "title", default="Unavailable source item") or "Unavailable source item")
            reason = str(_value(failure, "reason", "last_error", default="Needs attention") or "Needs attention")
            self.failure_history.addItem(f"{title}\n{reason}")
        if self.failure_history.count() == 0:
            self.failure_history.addItem("No unresolved failures for this source.")

        if isinstance(activity, str):
            activity_text = activity
        else:
            activity_text = "\n".join(str(line) for line in activity[-100:])
        self.source_activity.setPlainText(activity_text)

    def selected_source_ids(self) -> tuple[int, ...]:
        return tuple(
            int(item.data(SOURCE_ID_ROLE))
            for item in self.source_list.selectedItems()
            if item.data(SOURCE_ID_ROLE) is not None
        )

    def current_source_id(self) -> int | None:
        item = self.source_list.currentItem()
        return int(item.data(SOURCE_ID_ROLE)) if item is not None else None

    def set_batch_state(
        self,
        state: str,
        *,
        source_index: int | None = None,
        source_count: int | None = None,
        message: str | None = None,
        progress: int | None = None,
    ) -> None:
        normalized = state if state in {
            "idle", "syncing", "complete", "complete_with_issues", "failed", "stopped"
        } else "idle"
        self._batch_active = normalized == "syncing"
        self.progress.setProperty("syncState", normalized)
        self.activity_log.setProperty("syncState", normalized)
        repolish(self.progress)
        repolish(self.activity_log)
        if self._batch_active and progress is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, _integer(progress, 0))))
        if message:
            label = message
        elif self._batch_active and source_index is not None and source_count:
            label = f"Synchronizing source {source_index} of {source_count}"
        else:
            label = _friendly_status(normalized)
        self.progress.setFormat(label)
        self.stop_button.setEnabled(self._batch_active)
        self._update_actions()

    def append_activity(self, message: object) -> None:
        text = str(message or "").strip()
        if text:
            self.activity_log.append(text)

    def apply_review_state(
        self,
        state: str,
        *,
        sources: Iterable[object] = (),
        summary: Mapping[str, object] | object | None = None,
        runs: Sequence[object] = (),
        failures: Sequence[object] = (),
        activity: Sequence[str] | str = (),
    ) -> None:
        """Apply a display-only synthetic state for the isolated review harness.

        The method performs no persistence, provider lookup, or synchronization.
        The review harness remains responsible for validating its isolated root.
        """

        if state not in {
            "empty",
            "sources",
            "syncing",
            "complete_with_issues",
            "source_failures",
        }:
            raise ValueError("Unsupported synthetic Sync Center review state.")
        self.set_sources(sources)
        self.set_summary(summary)
        if state == "syncing":
            self.set_batch_state(
                "syncing",
                source_index=2,
                source_count=max(3, len(self._sources)),
                message="Synchronizing source 2 of 3",
            )
        elif state == "complete_with_issues":
            self.set_batch_state(
                "complete_with_issues",
                progress=100,
                message="Complete with issues",
            )
        else:
            self.set_batch_state("idle")
        current = self.current_source_id()
        if current is not None:
            self.set_source_detail(
                self._sources.get(current),
                runs=runs,
                failures=failures,
                activity=activity,
            )

    def _item_changed(self, item: QListWidgetItem) -> None:
        if self._changing_items:
            return
        source_id = item.data(SOURCE_ID_ROLE)
        if source_id is None:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        previous = self._sources.get(int(source_id))
        if self._batch_active:
            self._changing_items = True
            try:
                item.setCheckState(
                    Qt.CheckState.Checked
                    if previous is not None and previous.enabled
                    else Qt.CheckState.Unchecked
                )
            finally:
                self._changing_items = False
            return
        if previous is not None and previous.enabled == enabled:
            return
        self.source_enabled_changed.emit(int(source_id), enabled)

    def _selection_changed(self) -> None:
        if self._changing_items:
            return
        current_id = self.current_source_id()
        if current_id is not None:
            source = self._sources.get(current_id)
            if source is not None:
                self.set_source_detail(source)
        elif self._sources:
            self.detail_stack.setCurrentWidget(self.empty_state)
        self._update_actions()
        self.source_selection_changed.emit(self.selected_source_ids())

    def _emit_edit(self) -> None:
        source_id = self.current_source_id()
        if source_id is not None:
            self.edit_source_requested.emit(source_id)

    def _emit_remove(self) -> None:
        source_id = self.current_source_id()
        if source_id is not None:
            self.remove_source_requested.emit(source_id)

    def _emit_move(self, direction: int) -> None:
        source_id = self.current_source_id()
        if source_id is not None:
            self.move_source_requested.emit(source_id, direction)

    def _emit_sync_selected(self) -> None:
        selected = tuple(
            source_id
            for source_id in self.selected_source_ids()
            if self._sources.get(source_id) is not None
            and self._sources[source_id].enabled
        )
        if selected:
            self.sync_selected_requested.emit(selected)

    def _emit_clear_source_failures(self) -> None:
        source_id = self.current_source_id()
        if source_id is not None:
            self.clear_source_failures_requested.emit(source_id)

    def _update_actions(self) -> None:
        selected = self.selected_source_ids()
        current = self.current_source_id()
        current_view = self._sources.get(current or -1)
        has_current = current_view is not None
        ordered_ids = [
            source.id
            for source in sorted(
                self._sources.values(),
                key=lambda source: (source.sort_order, source.id),
            )
        ]
        current_index = ordered_ids.index(current) if current in ordered_ids else -1
        self.edit_button.setEnabled(has_current and not self._batch_active)
        self.remove_button.setEnabled(has_current and not self._batch_active)
        self.move_up_button.setEnabled(
            has_current and not self._batch_active and current_index > 0
        )
        self.move_down_button.setEnabled(
            has_current
            and not self._batch_active
            and 0 <= current_index < len(ordered_ids) - 1
        )
        self.sync_selected_button.setEnabled(
            bool(selected)
            and not self._batch_active
            and any(self._sources.get(source_id) and self._sources[source_id].enabled for source_id in selected)
        )
        self.sync_all_button.setEnabled(
            not self._batch_active and any(source.enabled for source in self._sources.values())
        )
        self.add_button.setEnabled(not self._batch_active)
        self.stop_button.setEnabled(self._batch_active)
        self.clear_source_failures_button.setEnabled(
            has_current
            and not self._batch_active
            and current_view.unresolved_failure_count > 0
        )


class SourceEditorDialog(QDialog):
    def __init__(
        self,
        *,
        source: object | None = None,
        playlists: Iterable[object] = (),
        normalize_source: Callable[[str], object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._source = SyncSourceView.from_source(source) if source is not None else None
        self._normalizer = normalize_source
        self._normalized_external_id = self._source.external_id if self._source else ""
        self._normalized_url = self._source.source_url if self._source else ""
        self.setWindowTitle("Edit Source" if self._source else "Add Source")
        self.setObjectName("SyncSourceDialog")
        self.setModal(True)
        self.setMinimumWidth(570)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 22)
        root.setSpacing(14)
        title = QLabel("Edit Saved Source" if self._source else "Add Saved Source")
        title.setObjectName("PageTitle")
        description = QLabel(
            "Saving updates the source definition only. Synchronization starts only when you request it."
        )
        description.setObjectName("MutedLabel")
        description.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(description)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(11)
        self.source_value = QLineEdit()
        self.source_value.setObjectName("SearchBox")
        self.source_value.setPlaceholderText("YouTube playlist URL or playlist ID")
        self.source_value.setAccessibleName("YouTube playlist URL or ID")
        if self._source:
            self.source_value.setText(self._source.external_id)
            self.source_value.setReadOnly(True)
        self.source_value.textChanged.connect(self._validate_identity)
        form.addRow("Playlist URL or ID", self.source_value)

        self.normalized_id = QLabel("Normalized ID: —")
        self.normalized_id.setObjectName("StatusLine")
        self.normalized_id.setWordWrap(True)
        form.addRow("", self.normalized_id)

        self.label = QLineEdit()
        self.label.setObjectName("SearchBox")
        self.label.setPlaceholderText("Optional personal label")
        self.label.setMaxLength(160)
        if self._source and self._source.label:
            self.label.setText(self._source.label)
        form.addRow("Label", self.label)

        self.enabled = QCheckBox("Include this source in Sync All Enabled")
        self.enabled.setChecked(self._source.enabled if self._source else True)
        form.addRow("Enabled", self.enabled)

        self.sort_order = QSpinBox()
        self.sort_order.setObjectName("QualityCombo")
        self.sort_order.setRange(0, 9999)
        self.sort_order.setValue(self._source.sort_order if self._source else 0)
        self.sort_order.setAccessibleName("Source execution order")
        form.addRow("Execution Order", self.sort_order)

        self.destination = QComboBox()
        self.destination.setObjectName("QualityCombo")
        self.destination.addItem("Library Only", "library")
        self.destination.addItem("Managed Local Playlist", "playlist")
        if self._source and self._source.destination_kind == "playlist":
            self.destination.setCurrentIndex(1)
        self.destination.currentIndexChanged.connect(self._update_destination_fields)
        form.addRow("Destination", self.destination)

        self.playlist_mode = QComboBox()
        self.playlist_mode.setObjectName("QualityCombo")
        self.playlist_mode.addItem("Create New Playlist", "new")
        self.playlist_mode.addItem("Select Existing Playlist", "existing")
        self.playlist_mode.currentIndexChanged.connect(self._update_destination_fields)
        self.playlist_mode_label = QLabel("Playlist Choice")
        form.addRow(self.playlist_mode_label, self.playlist_mode)

        self.existing_playlist = QComboBox()
        self.existing_playlist.setObjectName("QualityCombo")
        current_destination = self._source.destination_playlist_id if self._source else None
        for playlist in playlists:
            playlist_id = _integer(_value(playlist, "id"))
            manager_id = _value(playlist, "managing_source_id", "sync_source_id")
            if manager_id is not None and _integer(manager_id) != (self._source.id if self._source else -1):
                continue
            name = str(_value(playlist, "name", default="Local Playlist") or "Local Playlist")
            self.existing_playlist.addItem(name, playlist_id)
            if current_destination == playlist_id:
                self.existing_playlist.setCurrentIndex(self.existing_playlist.count() - 1)
                self.playlist_mode.setCurrentIndex(1)
        self.existing_playlist_label = QLabel("Existing Playlist")
        form.addRow(self.existing_playlist_label, self.existing_playlist)

        self.new_playlist_name = QLineEdit()
        self.new_playlist_name.setObjectName("SearchBox")
        self.new_playlist_name.setPlaceholderText("New local playlist name")
        self.new_playlist_name.setMaxLength(160)
        self._new_playlist_name_customized = False
        if self._source is None:
            self.new_playlist_name.setText("YouTube Playlist")
            self.label.textChanged.connect(self._update_new_playlist_name_suggestion)
            self.new_playlist_name.textEdited.connect(
                self._mark_new_playlist_name_customized
            )
        self.new_playlist_name_label = QLabel("New Playlist Name")
        form.addRow(self.new_playlist_name_label, self.new_playlist_name)
        root.addLayout(form)

        self.error_label = QLabel()
        self.error_label.setObjectName("DialogError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Save Source", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.save_button.setObjectName("PrimaryButton")
        self.cancel_button.setObjectName("SoftButton")
        self.save_button.clicked.connect(self._accept_if_valid)
        self.cancel_button.clicked.connect(self.reject)
        root.addWidget(buttons)

        self._validate_identity()
        self._update_destination_fields()

    def _update_new_playlist_name_suggestion(self, label: str) -> None:
        if self._source is not None or self._new_playlist_name_customized:
            return
        self.new_playlist_name.setText(label.strip() or "YouTube Playlist")

    def _mark_new_playlist_name_customized(self, _text: str) -> None:
        self._new_playlist_name_customized = True

    def _validate_identity(self) -> bool:
        if self._source:
            self.normalized_id.setText(f"Normalized ID: {self._source.external_id}")
            self.save_button.setEnabled(True)
            return True
        value = self.source_value.text().strip()
        if not value:
            self._normalized_external_id = ""
            self._normalized_url = ""
            self.normalized_id.setText("Normalized ID: —")
            if hasattr(self, "save_button"):
                self.save_button.setEnabled(False)
            return False
        try:
            normalized = self._normalizer(value) if self._normalizer else value
            if isinstance(normalized, tuple):
                external_id = str(normalized[0])
                source_url = str(normalized[1] if len(normalized) > 1 else value)
            else:
                external_id = str(
                    _value(normalized, "external_id", "playlist_id", default=normalized)
                )
                source_url = str(
                    _value(normalized, "source_url", "canonical_url", default=value)
                )
            if not external_id.strip():
                raise ValueError("Enter a valid public or unlisted YouTube playlist URL or ID.")
            self._normalized_external_id = external_id.strip()
            self._normalized_url = source_url.strip()
            self.normalized_id.setText(f"Normalized ID: {self._normalized_external_id}")
            if hasattr(self, "save_button"):
                self.save_button.setEnabled(True)
            self.set_error(None)
            return True
        except Exception as exc:
            self._normalized_external_id = ""
            self._normalized_url = ""
            self.normalized_id.setText("Normalized ID: invalid")
            if hasattr(self, "save_button"):
                self.save_button.setEnabled(False)
            self.set_error(str(exc) or "Enter a valid YouTube playlist URL or ID.")
            return False

    def _update_destination_fields(self) -> None:
        managed = self.destination.currentData() == "playlist"
        existing = self.playlist_mode.currentData() == "existing"
        self.playlist_mode_label.setVisible(managed)
        self.playlist_mode.setVisible(managed)
        self.existing_playlist_label.setVisible(managed and existing)
        self.existing_playlist.setVisible(managed and existing)
        self.new_playlist_name_label.setVisible(managed and not existing)
        self.new_playlist_name.setVisible(managed and not existing)

    def set_error(self, message: str | None) -> None:
        self.error_label.setText(message or "")
        self.error_label.setVisible(bool(message))

    def values(self) -> SourceEditorValues:
        destination_kind = str(self.destination.currentData())
        existing = destination_kind == "playlist" and self.playlist_mode.currentData() == "existing"
        playlist_id = (
            _integer(self.existing_playlist.currentData())
            if existing and self.existing_playlist.currentData() is not None
            else None
        )
        new_name = (
            self.new_playlist_name.text().strip()
            if destination_kind == "playlist" and not existing
            else None
        )
        return SourceEditorValues(
            source_value=(
                self._source.external_id
                if self._source is not None
                else self.source_value.text().strip()
            ),
            external_id=self._normalized_external_id,
            source_url=self._normalized_url,
            label=self.label.text().strip() or None,
            enabled=self.enabled.isChecked(),
            sort_order=self.sort_order.value(),
            destination_kind=destination_kind,
            destination_playlist_id=playlist_id,
            new_playlist_name=new_name or None,
        )

    def _accept_if_valid(self) -> None:
        if not self._validate_identity():
            return
        values = self.values()
        if values.destination_kind == "playlist":
            if values.destination_playlist_id is None and not values.new_playlist_name:
                self.set_error("Choose an eligible playlist or enter a new playlist name.")
                return
        self.accept()


class RemoveSourceDialog(QDialog):
    def __init__(self, source: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        view = source if isinstance(source, SyncSourceView) else SyncSourceView.from_source(source)
        self.setWindowTitle("Remove Saved Source")
        self.setObjectName("RemoveSyncSourceDialog")
        self.setModal(True)
        self.setMinimumWidth(530)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(13)
        title = QLabel(f"Remove {view.display_label}?")
        title.setObjectName("PageTitle")
        body = QLabel(
            "Synchronization for this source will stop. The source history is archived, "
            "and all library tracks, media, metadata, artwork, lyrics, failure history, "
            "and the linked local playlist remain. Current managed playlist contents "
            "become ordinary manual entries."
        )
        body.setObjectName("StatusLine")
        body.setWordWrap(True)
        assurance = QLabel("Music files are never deleted by this action.")
        assurance.setObjectName("PreservationNotice")
        assurance.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(assurance)
        buttons = QDialogButtonBox()
        remove = buttons.addButton("Remove Source", QDialogButtonBox.ButtonRole.DestructiveRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        remove.setObjectName("DangerButton")
        cancel.setObjectName("SoftButton")
        remove.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)


_BATCH_STATUS_FIELDS = frozenset(
    {
        "active_sync_batch",
        "active_sync_source_index",
        "last_sync_batch_status",
        "last_sync_batch_source_count",
        "last_sync_batch_complete_count",
        "last_sync_batch_issue_count",
        "last_sync_batch_failed_count",
        "last_sync_batch_downloaded_count",
        "last_sync_batch_imported_count",
        "last_sync_batch_item_failure_count",
    }
)


def aggregate_status_transition(values: Mapping[str, object]) -> dict[str, object]:
    """Whitelist privacy-safe batch aggregates for App Status."""

    return {key: values[key] for key in _BATCH_STATUS_FIELDS if key in values}


def multi_source_status_payload(
    result: object,
    *,
    sync_source_count: int,
    enabled_sync_source_count: int,
) -> dict[str, object]:
    """Return an identity-free App Status sync section for a completed batch."""

    return {
        # Keep the stable legacy summary keys but explicitly clear identity and
        # item-detail values for the multi-source workflow.
        "last_sync_at": _value(result, "finished_at"),
        "last_sync_status": _value(result, "status"),
        "last_sync_playlist_title": None,
        "last_sync_new_items": _integer(_value(result, "total_new")),
        "last_sync_imported_count": _integer(_value(result, "total_imported")),
        "last_sync_error": None,
        "last_sync_playlist_id": None,
        "last_sync_visible_item_count": _integer(_value(result, "total_visible")),
        "last_sync_downloaded_count": _integer(_value(result, "total_downloaded")),
        "last_sync_existing_count": _integer(_value(result, "total_existing")),
        "last_sync_failed_count": _integer(_value(result, "total_failed_items")),
        "last_sync_failures": [],
        "sync_source_count": int(sync_source_count),
        "enabled_sync_source_count": int(enabled_sync_source_count),
        "active_sync_batch": False,
        "active_sync_source_index": None,
        "last_sync_batch_status": _value(result, "status"),
        "last_sync_batch_source_count": _integer(
            _value(result, "selected_source_count")
        ),
        "last_sync_batch_complete_count": _integer(
            _value(result, "completed_source_count")
        ),
        "last_sync_batch_issue_count": _integer(
            _value(result, "issue_source_count")
        ),
        "last_sync_batch_failed_count": _integer(
            _value(result, "failed_source_count")
        ),
        "last_sync_batch_downloaded_count": _integer(
            _value(result, "total_downloaded")
        ),
        "last_sync_batch_imported_count": _integer(
            _value(result, "total_imported")
        ),
        "last_sync_batch_item_failure_count": _integer(
            _value(result, "total_failed_items")
        ),
    }


class MultiSourceSyncWorker(QThread):
    progress = Signal(object)
    transition = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        orchestrator_factory: Callable[[Callable, Callable], object],
        source_ids: tuple[int, ...] | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.orchestrator_factory = orchestrator_factory
        self.source_ids = source_ids
        self.orchestrator: object | None = None
        self._stop_requested = False

    def run(self) -> None:
        try:
            def forward_progress(event: object) -> None:
                # A stop request can arrive after the QThread starts but before the
                # orchestrator marks its batch active. Re-apply it at the first
                # active-batch event so the request cannot be lost when _run()
                # clears its per-batch event.
                if (
                    self._stop_requested
                    and str(_value(event, "phase", default="")) == "batch_started"
                ):
                    request = getattr(
                        self.orchestrator,
                        "request_stop_after_current",
                        None,
                    )
                    if callable(request):
                        request()
                self.progress.emit(event)

            self.orchestrator = self.orchestrator_factory(
                forward_progress,
                self.transition.emit,
            )
            if self._stop_requested:
                request = getattr(self.orchestrator, "request_stop_after_current", None)
                if callable(request):
                    request()
            if self.source_ids is None:
                result = self.orchestrator.sync_all_enabled()
            else:
                result = self.orchestrator.sync_selected(self.source_ids)
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(sanitize_error_text(exc))
        finally:
            worker_db = getattr(self.orchestrator, "_music_vault_worker_db", None)
            close = getattr(worker_db, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def request_stop_after_current(self) -> None:
        self._stop_requested = True
        request = getattr(self.orchestrator, "request_stop_after_current", None)
        if callable(request):
            request()


class SyncCenterController(QObject):
    """Connect the persistent source service to the reusable Sync Center UI."""

    sources_changed = Signal()
    sync_started = Signal()
    sync_finished = Signal(object)
    status_transition = Signal(object)

    def __init__(
        self,
        widget: SyncCenterWidget,
        source_service: object,
        *,
        normalize_source: Callable[[str], object],
        orchestrator_factory: Callable[[Callable, Callable], object],
        playlist_provider: Callable[[], Iterable[object]],
        playlist_creator: Callable[[str], int],
        dialog_parent: QWidget,
    ) -> None:
        super().__init__(dialog_parent)
        self.widget = widget
        self.source_service = source_service
        self.normalize_source = normalize_source
        self.orchestrator_factory = orchestrator_factory
        self.playlist_provider = playlist_provider
        self.playlist_creator = playlist_creator
        self.dialog_parent = dialog_parent
        self.worker: MultiSourceSyncWorker | None = None
        self._activity_by_source: dict[int, list[str]] = {}

        widget.add_source_requested.connect(self.open_add_source)
        widget.edit_source_requested.connect(self.open_edit_source)
        widget.remove_source_requested.connect(self.remove_source)
        widget.move_source_requested.connect(self.move_source)
        widget.source_enabled_changed.connect(self.set_source_enabled)
        widget.source_selection_changed.connect(self._selection_changed)
        widget.sync_selected_requested.connect(self.sync_selected)
        widget.sync_all_requested.connect(self.sync_all_enabled)
        widget.stop_after_current_requested.connect(self.stop_after_current)
        widget.clear_source_failures_requested.connect(
            self.clear_source_failure_history
        )

    def _playlist_names(self) -> dict[int, str]:
        return {
            _integer(_value(row, "id")): str(_value(row, "name", default="Local Playlist"))
            for row in self.playlist_provider()
        }

    def _views(self) -> list[SyncSourceView]:
        names = self._playlist_names()
        views: list[SyncSourceView] = []
        for source in self.source_service.list_active():
            view = SyncSourceView.from_source(source)
            view = replace(
                view,
                destination_playlist_name=names.get(view.destination_playlist_id or -1),
                unresolved_failure_count=_integer(
                    self.source_service.unresolved_failure_count(view.id)
                ),
            )
            views.append(view)
        return views

    def refresh(self, *, preserve_detail: bool = True) -> None:
        current = self.widget.current_source_id() if preserve_detail else None
        views = self._views()
        self.widget.set_sources(views)
        enabled = [source for source in views if source.enabled]
        self.widget.set_summary(
            {
                "enabled_sources": len(enabled),
                "completed_sources": sum(
                    source.last_sync_status == "complete" for source in views
                ),
                "issue_sources": sum(
                    source.last_sync_status == "complete_with_issues" for source in views
                ),
                "failed_sources": sum(
                    source.last_sync_status == "failed" for source in views
                ),
                "downloaded": sum(source.downloaded_count for source in views),
                "existing": sum(source.existing_count for source in views),
                "failed_items": sum(source.unresolved_failure_count for source in views),
            }
        )
        if current is not None:
            for row in range(self.widget.source_list.count()):
                item = self.widget.source_list.item(row)
                if _integer(item.data(SOURCE_ID_ROLE)) == current:
                    self.widget.source_list.setCurrentItem(item)
                    break
        self._refresh_current_detail()

    def _refresh_current_detail(self) -> None:
        source_id = self.widget.current_source_id()
        if source_id is None:
            self.widget.set_source_detail(None)
            return
        try:
            source = self.source_service.get(source_id)
            view = self.widget._sources.get(source_id)
            if view is None:
                view = SyncSourceView.from_source(source)
                view = replace(
                    view,
                    destination_playlist_name=self._playlist_names().get(
                        view.destination_playlist_id or -1
                    ),
                    unresolved_failure_count=_integer(
                        self.source_service.unresolved_failure_count(source_id)
                    ),
                )
            runs = self.source_service.recent_runs(source_id)
            failure_reader = getattr(
                self.source_service, "list_unresolved_failures", None
            )
            if callable(failure_reader):
                failures = failure_reader(source_id)
            else:
                db = getattr(self.source_service, "db", None)
                legacy_failure_reader = getattr(db, "list_sync_failures", None)
                failures = (
                    legacy_failure_reader("unresolved", sync_source_id=source_id)
                    if callable(legacy_failure_reader)
                    else ()
                )
            self.widget.set_source_detail(
                view,
                runs=runs,
                failures=failures,
                activity=self._activity_by_source.get(source_id, ()),
            )
        except Exception:
            self.widget.set_source_detail(None)

    def _selection_changed(self, _source_ids: object) -> None:
        self._refresh_current_detail()

    def _place_source(self, source_id: int, requested_order: int) -> None:
        ordered = [source.id for source in self.source_service.list_active()]
        if source_id not in ordered:
            return
        ordered.remove(source_id)
        target = max(0, min(int(requested_order), len(ordered)))
        ordered.insert(target, source_id)
        self.source_service.reorder(ordered)

    def _batch_running(self) -> bool:
        if self.worker is not None and self.worker.isRunning():
            return True
        legacy_worker = getattr(self.dialog_parent, "sync_worker", None)
        is_running = getattr(legacy_worker, "isRunning", None)
        return bool(callable(is_running) and is_running())

    def open_add_source(self) -> None:
        if self._batch_running():
            return
        dialog = SourceEditorDialog(
            playlists=self.playlist_provider(),
            normalize_source=self.normalize_source,
            parent=self.dialog_parent,
        )
        dialog.sort_order.setValue(len(self.source_service.list_active()))
        while dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                values = dialog.values()
                existing = {
                    source.external_id: source for source in self.source_service.list_active()
                }
                if values.external_id in existing:
                    raise ValueError(
                        "That playlist is already saved. Select its existing source card instead."
                    )
                playlist_id = values.destination_playlist_id
                if values.destination_kind == "playlist" and playlist_id is None:
                    playlist_id = self.playlist_creator(values.new_playlist_name or "")
                source = self.source_service.create_source(
                    values.source_value,
                    label=values.label,
                    enabled=values.enabled,
                    destination_kind=values.destination_kind,
                    destination_playlist_id=playlist_id,
                )
                self._place_source(source.id, values.sort_order)
                self.refresh(preserve_detail=False)
                self._select_source(source.id)
                self.sources_changed.emit()
                return
            except Exception as exc:
                dialog.set_error(sanitize_error_text(exc))

    def open_edit_source(self, source_id: int) -> None:
        if self._batch_running():
            return
        try:
            source = self.source_service.get(source_id)
        except Exception as exc:
            QMessageBox.warning(self.dialog_parent, "Source unavailable", sanitize_error_text(exc))
            return
        dialog = SourceEditorDialog(
            source=source,
            playlists=self.playlist_provider(),
            normalize_source=self.normalize_source,
            parent=self.dialog_parent,
        )
        while dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                values = dialog.values()
                playlist_id = values.destination_playlist_id
                if values.destination_kind == "playlist" and playlist_id is None:
                    playlist_id = self.playlist_creator(values.new_playlist_name or "")
                self.source_service.update_source(
                    source_id,
                    label=values.label,
                    enabled=values.enabled,
                    destination_kind=values.destination_kind,
                    destination_playlist_id=playlist_id,
                )
                self._place_source(source_id, values.sort_order)
                self.refresh()
                self.sources_changed.emit()
                return
            except Exception as exc:
                dialog.set_error(sanitize_error_text(exc))

    def remove_source(self, source_id: int) -> None:
        if self._batch_running():
            return
        try:
            source = self.source_service.get(source_id)
        except Exception as exc:
            QMessageBox.warning(self.dialog_parent, "Source unavailable", sanitize_error_text(exc))
            return
        dialog = RemoveSourceDialog(source, self.dialog_parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.source_service.archive(source_id)
            self.refresh(preserve_detail=False)
            self.sources_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self.dialog_parent, "Source removal failed", sanitize_error_text(exc))

    def set_source_enabled(self, source_id: int, enabled: bool) -> None:
        if self._batch_running():
            self.refresh()
            return
        try:
            self.source_service.set_enabled(source_id, enabled)
            self.refresh()
            self.sources_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self.dialog_parent, "Source update failed", sanitize_error_text(exc))
            self.refresh()

    def clear_source_failure_history(self, source_id: int) -> None:
        if self._batch_running():
            return
        count = _integer(self.source_service.unresolved_failure_count(source_id))
        if count <= 0:
            QMessageBox.information(
                self.dialog_parent,
                "Source failure history",
                "This source has no unresolved failure history to clear.",
            )
            return
        answer = QMessageBox.question(
            self.dialog_parent,
            "Clear source failure history?",
            "Clear unresolved failure history for this saved source only? "
            "This does not delete music or affect another source.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.source_service.clear_failure_history(source_id)
        self.refresh()
        self.sources_changed.emit()

    def move_source(self, source_id: int, direction: int) -> None:
        if self._batch_running():
            return
        try:
            self.source_service.move(source_id, direction)
            self.refresh()
            self._select_source(source_id)
            self.sources_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self.dialog_parent, "Source order failed", sanitize_error_text(exc))

    def _select_source(self, source_id: int) -> None:
        for row in range(self.widget.source_list.count()):
            item = self.widget.source_list.item(row)
            if _integer(item.data(SOURCE_ID_ROLE)) == int(source_id):
                self.widget.source_list.setCurrentItem(item)
                item.setSelected(True)
                return

    def sync_selected(self, source_ids: object) -> None:
        try:
            selected = tuple(int(value) for value in source_ids)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            selected = ()
        enabled_ids = {
            source.id
            for source in self.source_service.list_active(enabled_only=True)
        }
        selected = tuple(
            source_id for source_id in selected if source_id in enabled_ids
        )
        if not selected:
            QMessageBox.information(self.dialog_parent, "Select a source", "Select at least one enabled source first.")
            return
        self._start_batch(selected)

    def sync_all_enabled(self) -> None:
        self._start_batch(None)

    def _start_batch(self, source_ids: tuple[int, ...] | None) -> None:
        if self._batch_running():
            QMessageBox.information(self.dialog_parent, "Synchronization active", "A synchronization batch is already running.")
            return
        self.widget.set_batch_state("syncing", message="Preparing sequential synchronization")
        self.widget.append_activity("Starting saved-source synchronization.")
        self.status_transition.emit(
            {
                "active_sync_batch": True,
                "active_sync_source_index": 0,
                "last_sync_batch_source_count": (
                    len(source_ids)
                    if source_ids is not None
                    else len(self.source_service.list_active(enabled_only=True))
                ),
            }
        )
        worker = MultiSourceSyncWorker(self.orchestrator_factory, source_ids, self)
        worker.progress.connect(self._progress)
        worker.transition.connect(self._transition)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.finished.connect(lambda: setattr(self, "worker", None))
        self.worker = worker
        self.sync_started.emit()
        worker.start()

    def stop_after_current(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        self.worker.request_stop_after_current()
        self.widget.append_activity(
            "Stop requested. The active source will finish safely; no later source will start."
        )
        self.widget.set_batch_state("syncing", message="Stopping after the current source")

    def _progress(self, event: object) -> None:
        source_index = _integer(_value(event, "source_index"))
        source_count = _integer(_value(event, "source_count"))
        message = str(_value(event, "message", default="") or "").strip()
        phase = str(_value(event, "phase", default="") or "")
        source_id = _value(event, "source_id")
        source_label = str(_value(event, "source_label", default="") or "").strip()
        if not message:
            message = {
                "batch_started": "Sequential synchronization started.",
                "source_started": f"Starting {source_label or 'saved source'}.",
                "source_finished": f"Finished {source_label or 'saved source'}.",
                "stopped_after_current": "Stopped safely after the current source.",
                "batch_finished": "Synchronization batch finished.",
            }.get(phase, "Synchronization is progressing.")
        message = sanitize_error_text(message)
        self.widget.append_activity(message)
        if source_id is not None:
            activity = self._activity_by_source.setdefault(_integer(source_id), [])
            activity.append(message)
            del activity[:-100]
        self.widget.set_batch_state(
            "syncing",
            source_index=source_index,
            source_count=source_count,
            message=(
                f"Source {source_index} of {source_count}: {source_label}"
                if source_index and source_count and source_label
                else message
            ),
        )
        if phase == "source_finished":
            self.refresh()

    def _transition(self, values: object) -> None:
        if isinstance(values, Mapping):
            safe = aggregate_status_transition(values)
            if safe:
                self.status_transition.emit(safe)

    def _completed(self, result: object) -> None:
        status = str(_value(result, "status", default="failed"))
        self.widget.set_batch_state(status, progress=100, message=_friendly_status(status))
        self.refresh()
        self.sync_finished.emit(result)

    def _failed(self, message: str) -> None:
        self.widget.set_batch_state("failed", progress=0, message="Synchronization failed")
        self.widget.append_activity(message)
        self.status_transition.emit(
            {
                "active_sync_batch": False,
                "active_sync_source_index": None,
                "last_sync_batch_status": "failed",
            }
        )
        QMessageBox.warning(
            self.dialog_parent,
            "Synchronization failed",
            "The saved-source batch could not complete. Prior source state and media were preserved.",
        )


def explain_source_managed_removal(parent: QWidget, *, manual_origin_removed: bool) -> None:
    if manual_origin_removed:
        message = (
            "The manual playlist pin was removed, but this track remains visible because "
            "it is managed by the linked saved source."
        )
    else:
        message = (
            "This track is managed by the linked saved source and cannot be removed from "
            "the playlist by deleting its managed origin. Remove or change the saved source "
            "relationship in Sync Center instead."
        )
    QMessageBox.information(parent, "Source-managed track", message)
