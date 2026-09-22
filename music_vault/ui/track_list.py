from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import PurePosixPath, PureWindowsPath
import unicodedata
from typing import Iterable, Mapping

from PySide6.QtCore import (
    QAbstractTableModel, QItemSelection, QItemSelectionModel, QModelIndex,
    QSize, QSortFilterProxyModel, QTimer, Qt, Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QTableView, QToolTip,
)

from music_vault.ui.icons import render_icon_pixmap
from music_vault.ui.theme import COLORS
from music_vault.ui.thumbnail_cache import ThumbnailCache


TRACK_ID_ROLE = int(Qt.ItemDataRole.UserRole)
NOW_PLAYING_ROLE = TRACK_ID_ROLE + 1


def normalize_track_text(value: str) -> str:
    """Literal, accent-insensitive local matching; never a regular expression."""
    return "".join(
        char for char in unicodedata.normalize("NFKD", str(value).casefold())
        if not unicodedata.combining(char)
    )


@dataclass(frozen=True, slots=True)
class TrackRow:
    id: int
    title: str
    artist: str = ""
    album: str = ""
    year: str = ""
    cover_path: str | None = None

    @classmethod
    def from_record(cls, record: TrackRow | Mapping) -> TrackRow:
        if isinstance(record, cls):
            return record
        # sqlite3.Row has keys/__getitem__, but deliberately has no .get().
        keys = record.keys()

        def value(key: str):
            return record[key] if key in keys else None

        title = value("title")
        if not title:
            path = str(value("path") or "")
            title = (PureWindowsPath(path) if "\\" in path else PurePosixPath(path)).stem
        cover = value("cover_path")
        return cls(
            int(record["id"]), str(title or "Untitled"),
            str(value("artist") or ""), str(value("album") or ""),
            str(value("year") or ""), str(cover) if cover else None,
        )


class TrackTableModel(QAbstractTableModel):
    """One immutable display row per track. Paths are not exposed as Qt roles."""

    HEADERS = ("Title", "Artist", "Album", "Year")

    def __init__(self, tracks: Iterable = (), parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[TrackRow] = []
        self._rows_by_id: dict[int, int] = {}
        self._search_text: list[str] = []
        self._sort_values: list[tuple[str, ...]] = []
        self._now_playing: int | None = None
        self.set_tracks(tracks)

    @staticmethod
    def _normalized_values(track: TrackRow) -> tuple[str, ...]:
        return tuple(normalize_track_text(value) for value in (
            track.title, track.artist, track.album, track.year,
        ))

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._tracks)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._tracks):
            return None
        track = self._tracks[index.row()]
        if role == TRACK_ID_ROLE:
            return track.id
        if role == NOW_PLAYING_ROLE:
            return track.id == self._now_playing
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return (track.title, track.artist, track.album, track.year)[index.column()]
        if role == Qt.ItemDataRole.AccessibleTextRole:
            label = ", ".join(value for value in (track.title, track.artist, track.album, track.year) if value)
            return f"Now playing: {label}" if track.id == self._now_playing else label
        if track.id == self._now_playing:
            if role == Qt.ItemDataRole.ForegroundRole:
                return QColor(COLORS["now_playing"])
            if role == Qt.ItemDataRole.FontRole:
                font = QFont()
                font.setWeight(QFont.Weight.DemiBold)
                return font
        return None

    def roleNames(self):  # noqa: N802
        return {**super().roleNames(), TRACK_ID_ROLE: b"trackId", NOW_PLAYING_ROLE: b"nowPlaying"}

    def headerData(self, section, orientation, role=int(Qt.ItemDataRole.DisplayRole)):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return super().headerData(section, orientation, role)

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_tracks(self, tracks: Iterable) -> None:
        prepared = [TrackRow.from_record(track) for track in tracks]
        rows = {track.id: row for row, track in enumerate(prepared)}
        if len(rows) != len(prepared):
            raise ValueError("Track IDs must be unique within a playback view.")
        sort_values = [self._normalized_values(track) for track in prepared]
        self.beginResetModel()
        self._tracks = prepared
        self._rows_by_id = rows
        self._sort_values = sort_values
        self._search_text = ["\n".join(values) for values in sort_values]
        self.endResetModel()

    def replace_track(self, record) -> bool:
        track = TrackRow.from_record(record)
        row = self.source_row_for_track_id(track.id)
        if row is None:
            return False
        self._tracks[row] = track
        self._sort_values[row] = self._normalized_values(track)
        self._search_text[row] = "\n".join(self._sort_values[row])
        self.dataChanged.emit(self.index(row, 0), self.index(row, 3))
        return True

    def source_row_for_track_id(self, track_id: int) -> int | None:
        return self._rows_by_id.get(track_id)

    def track_for_id(self, track_id: int) -> TrackRow | None:
        row = self.source_row_for_track_id(track_id)
        return self._tracks[row] if row is not None else None

    def set_now_playing(self, track_id: int | None) -> None:
        if track_id == self._now_playing:
            return
        previous, self._now_playing = self._now_playing, track_id
        for changed in (previous, track_id):
            row = self.source_row_for_track_id(changed)
            if row is not None:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 3), [
                    NOW_PLAYING_ROLE, int(Qt.ItemDataRole.ForegroundRole),
                    int(Qt.ItemDataRole.FontRole), int(Qt.ItemDataRole.AccessibleTextRole),
                ])


class TrackFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tokens: tuple[str, ...] = ()
        self._filter_text = ""
        self.setDynamicSortFilter(True)

    def set_filter(self, text: str) -> None:
        self._filter_text = str(text)
        tokens = tuple(normalize_track_text(text).split())
        if tokens == self._tokens:
            return
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            self._tokens = tokens
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self._tokens = tokens
            self.invalidateFilter()

    def filter_text(self) -> str:
        return self._filter_text

    def filterAcceptsRow(self, source_row, source_parent):  # noqa: N802
        if not self._tokens:
            return True
        text = self.sourceModel()._search_text[source_row]
        return all(token in text for token in self._tokens)

    def lessThan(self, left, right):  # noqa: N802
        values = self.sourceModel()._sort_values
        left_value, right_value = values[left.row()][left.column()], values[right.row()][right.column()]
        if left_value != right_value:
            return left_value < right_value
        # Qt reverses the comparator for descending order. Equal labels retain
        # original context order in either direction, not reverse playlist order.
        return left.row() < right.row() if self.sortOrder() == Qt.SortOrder.AscendingOrder else left.row() > right.row()


class TrackDelegate(QStyledItemDelegate):
    ART_SIZE = 42

    def __init__(self, view):
        super().__init__(view)
        self._view = view
        self._placeholder = QIcon(render_icon_pixmap("albums", self.ART_SIZE, COLORS["text_muted"]))

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        if index.column() == 0:
            thumbnail = self._view.thumbnail_for_track(index.data(TRACK_ID_ROLE))
            styled.icon = QIcon(thumbnail) if thumbnail is not None else self._placeholder
            styled.decorationSize = QSize(self.ART_SIZE, self.ART_SIZE)
            styled.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration
        style = styled.widget.style() if styled.widget is not None else self._view.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)

    def helpEvent(self, event, view, option, index):  # noqa: N802
        text = index.data(Qt.ItemDataRole.ToolTipRole)
        if text:
            # Qt auto-detects rich text; escape imported metadata explicitly.
            QToolTip.showText(event.globalPos(), f"<qt>{escape(str(text))}</qt>", view)
            return True
        return super().helpEvent(event, view, option, index)


class TrackTableView(QTableView):
    """Stable-ID table API with bounded, visible-only thumbnail work."""

    selected_tracks_changed = Signal()
    ROW_HEIGHT = 54

    def __init__(self, parent=None, thumbnail_cache=None):
        super().__init__(parent)
        self.setObjectName("LibraryTable")
        self.track_model = TrackTableModel(parent=self)
        self.proxy_model = TrackFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.track_model)
        self.setModel(self.proxy_model)
        self._cache = thumbnail_cache if thumbnail_cache is not None else ThumbnailCache(parent=self)
        self._owns_cache = thumbnail_cache is None
        self._thumbnail_bindings: dict[int, tuple[str, float, object]] = {}
        self._thumbnail_failures: set[object] = set()
        self._thumbnail_generation = self._cache.generation
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.timeout.connect(self._request_visible_artwork)
        self._cache.thumbnail_ready.connect(self._thumbnail_ready)
        self._cache.thumbnail_failed.connect(self._thumbnail_failed)
        self.setItemDelegate(TrackDelegate(self))
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Music library tracks")
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalHeader().hide()
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.verticalHeader().setDefaultSectionSize(self.ROW_HEIGHT)
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(3, 68)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.setSortIndicatorShown(False)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._cycle_sort)
        self.selectionModel().selectionChanged.connect(lambda *_: self.selected_tracks_changed.emit())
        self.verticalScrollBar().valueChanged.connect(self._schedule_artwork)
        for signal in (self.proxy_model.modelReset, self.proxy_model.layoutChanged,
                       self.proxy_model.rowsInserted, self.proxy_model.rowsRemoved):
            signal.connect(self._schedule_artwork)

    def set_tracks(self, tracks: Iterable) -> None:
        selected, current = self.selected_track_ids(), self.current_track_id()
        self.track_model.set_tracks(tracks)
        self._thumbnail_bindings.clear()
        self._thumbnail_failures.clear()
        self.restore_selection(selected, current)
        self._schedule_artwork()

    def replace_track(self, track) -> bool:
        prepared = TrackRow.from_record(track)
        previous = self._thumbnail_bindings.pop(prepared.id, None)
        if previous is not None:
            self._thumbnail_failures.discard(previous[2])
        changed = self.track_model.replace_track(prepared)
        self._schedule_artwork()
        return changed

    def set_filter(self, text: str) -> None:
        selected, current = self.selected_track_ids(), self.current_track_id()
        self.proxy_model.set_filter(text)
        self.restore_selection(selected, current)
        self._schedule_artwork()

    def set_now_playing(self, track_id: int | None) -> None:
        self.track_model.set_now_playing(track_id)

    def track_id_at(self, proxy_row: int) -> int | None:
        return self.proxy_model.index(proxy_row, 0).data(TRACK_ID_ROLE)

    def row_for_track_id(self, track_id: int) -> int | None:
        row = self.track_model.source_row_for_track_id(track_id)
        index = self.proxy_model.mapFromSource(self.track_model.index(row, 0)) if row is not None else QModelIndex()
        return index.row() if index.isValid() else None

    def visible_track_ids(self) -> list[int]:
        model = self.track_model
        return [model._tracks[self.proxy_model.mapToSource(self.proxy_model.index(row, 0)).row()].id
                for row in range(self.visible_track_count())]

    def source_track_ids(self) -> list[int]:
        return [track.id for track in self.track_model._tracks]

    def total_track_count(self) -> int:
        return self.track_model.rowCount()

    def visible_track_count(self) -> int:
        return self.proxy_model.rowCount()

    def selected_track_ids(self) -> list[int]:
        return [index.data(TRACK_ID_ROLE) for index in sorted(self.selectionModel().selectedRows(), key=lambda item: item.row())]

    def current_track_id(self) -> int | None:
        return self.currentIndex().data(TRACK_ID_ROLE)

    def select_track(self, track_id: int, *, clear: bool = True) -> bool:
        row = self.row_for_track_id(track_id)
        if row is None:
            return False
        selection = self.selectionModel()
        flags = QItemSelectionModel.SelectionFlag.Rows | QItemSelectionModel.SelectionFlag.Select
        if clear:
            flags |= QItemSelectionModel.SelectionFlag.Clear
        selection.setCurrentIndex(self.proxy_model.index(row, 0), flags)
        return True

    def restore_selection(self, track_ids: Iterable[int], current_track_id: int | None = None) -> None:
        selected = QItemSelection()
        for track_id in track_ids:
            row = self.row_for_track_id(track_id)
            if row is not None:
                selected.select(self.proxy_model.index(row, 0), self.proxy_model.index(row, 3))
        selection = self.selectionModel()
        selection.select(selected, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        row = self.row_for_track_id(current_track_id)
        selection.setCurrentIndex(self.proxy_model.index(row, 0) if row is not None else QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate)

    def scroll_to_track(self, track_id: int) -> bool:
        row = self.row_for_track_id(track_id)
        if row is None:
            return False
        self.scrollTo(self.proxy_model.index(row, 0), QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def set_sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        if not 0 <= column < 4:
            raise ValueError("Track sort column must be between 0 and 3.")
        self.proxy_model.sort(column, order)
        self.horizontalHeader().setSortIndicator(column, order)
        self.horizontalHeader().setSortIndicatorShown(True)

    def clear_sort(self) -> None:
        self.proxy_model.sort(-1)
        self.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.horizontalHeader().setSortIndicatorShown(False)

    def _cycle_sort(self, column: int) -> None:
        # Sorting remains opt-in: a third click restores the supplied playlist
        # order. Qt's built-in sorting would sort on view construction too.
        if self.proxy_model.sortColumn() != column:
            self.set_sort(column)
        elif self.proxy_model.sortOrder() == Qt.SortOrder.AscendingOrder:
            self.set_sort(column, Qt.SortOrder.DescendingOrder)
        else:
            self.clear_sort()

    def _schedule_artwork(self, *_):
        if not self._visible_timer.isActive():
            self._visible_timer.start(0)

    def _near_visible_ids(self) -> list[int]:
        count = self.visible_track_count()
        if not self.isVisible() or not count:
            return []
        first = max(0, self.rowAt(0))
        last = self.rowAt(max(0, self.viewport().height() - 1))
        if last < 0:
            last = min(count - 1, first + self.viewport().height() // self.ROW_HEIGHT + 1)
        return [self.track_id_at(row) for row in range(max(0, first - 2), min(count, last + 3))]

    def _request_visible_artwork(self):
        track_ids = self._near_visible_ids()
        visible = set(track_ids)
        self._thumbnail_bindings = {key: value for key, value in self._thumbnail_bindings.items() if key in visible}
        generation = self._cache.generation
        if generation != self._thumbnail_generation:
            self._thumbnail_bindings.clear()
            self._thumbnail_failures.clear()
            self._thumbnail_generation = generation
        self._thumbnail_failures.intersection_update(
            binding[2] for binding in self._thumbnail_bindings.values()
        )
        dpr = round(float(self.devicePixelRatioF()), 2)
        for track_id in track_ids:
            track = self.track_model.track_for_id(track_id)
            if track is None or not track.cover_path:
                continue
            previous = self._thumbnail_bindings.get(track_id)
            if previous is not None and previous[:2] == (track.cover_path, dpr):
                # A binding is not ownership: another browser can evict or
                # invalidate this shared-cache entry while the table is hidden.
                if self._cache.peek(previous[2]) is not None or previous[2] in self._thumbnail_failures:
                    continue
            # The cache coalesces in-flight requests, including an invalidated
            # same-path decode. Only visibility/layout events request work;
            # painting and decode callbacks never schedule retry loops.
            key = self._cache.request(track.cover_path, TrackDelegate.ART_SIZE, dpr, generation=generation)
            self._thumbnail_bindings[track_id] = (track.cover_path, dpr, key)
        self.viewport().update()

    def thumbnail_for_track(self, track_id):
        binding = self._thumbnail_bindings.get(track_id)
        return self._cache.peek(binding[2]) if binding is not None else None

    def _thumbnail_ready(self, key, _pixmap, generation):
        if generation != self._thumbnail_generation:
            return
        self._thumbnail_failures.discard(key)
        self._update_thumbnail_rows(key)

    def _update_thumbnail_rows(self, key):
        for track_id, binding in self._thumbnail_bindings.items():
            if binding[2] == key:
                row = self.row_for_track_id(track_id)
                if row is not None:
                    self.viewport().update(self.visualRect(self.proxy_model.index(row, 0)))

    def _thumbnail_failed(self, key, _reason, generation):
        if generation != self._thumbnail_generation:
            return
        if any(binding[2] == key for binding in self._thumbnail_bindings.values()):
            self._thumbnail_failures.add(key)
            self._update_thumbnail_rows(key)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        # One new attempt after returning to this page allows repaired artwork
        # to recover without continuously decoding a missing/corrupt file.
        self._thumbnail_failures.clear()
        self._schedule_artwork()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._schedule_artwork()

    def scrollContentsBy(self, dx, dy):  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self._schedule_artwork()

    def closeEvent(self, event):  # noqa: N802
        if self._owns_cache:
            self._cache.close()
        super().closeEvent(event)
