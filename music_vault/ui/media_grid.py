from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPoint,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from music_vault.ui.icons import render_icon_pixmap
from music_vault.ui.theme import COLORS, RADII, TYPOGRAPHY


class MediaKind(StrEnum):
    ALBUM = "album"
    ARTIST = "artist"


class MediaImageState(StrEnum):
    MISSING = "missing"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


class MediaGridState(StrEnum):
    CONTENT = "content"
    EMPTY = "empty"
    LOADING = "loading"
    ERROR = "error"


class MediaRole(IntEnum):
    KEY = int(Qt.ItemDataRole.UserRole) + 1
    KIND = KEY + 1
    TITLE = KEY + 2
    SUBTITLE = KEY + 3
    ARTWORK_PATH = KEY + 4
    THUMBNAIL = KEY + 5
    IMAGE_STATE = KEY + 6
    HAS_CACHED_IMAGE = KEY + 7
    SOURCE_URL = KEY + 8


@dataclass(frozen=True, slots=True)
class MediaItem:
    key: str
    kind: MediaKind | str
    title: str
    subtitle: str = ""
    artwork_path: str | Path | None = None
    image_state: MediaImageState | str = MediaImageState.MISSING
    has_cached_image: bool = False
    source_url: str | None = None

    def __post_init__(self) -> None:
        key = str(self.key).strip()
        if not key:
            raise ValueError("Media items require a stable non-empty key.")
        try:
            kind = MediaKind(self.kind)
        except ValueError as exc:
            raise ValueError(f"Unsupported media kind: {self.kind!r}") from exc
        try:
            image_state = MediaImageState(self.image_state)
        except ValueError as exc:
            raise ValueError(f"Unsupported media image state: {self.image_state!r}") from exc
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "title", str(self.title).strip() or "Unknown")
        object.__setattr__(self, "subtitle", str(self.subtitle).strip())
        object.__setattr__(self, "image_state", image_state)
        if self.artwork_path is not None:
            object.__setattr__(self, "artwork_path", str(self.artwork_path))


class MediaGridModel(QAbstractListModel):
    """Small immutable-row model; thumbnails remain owned by the bounded cache."""

    def __init__(self, items: Iterable[MediaItem] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[MediaItem, ...] = ()
        self._rows_by_key: dict[str, int] = {}
        self._thumbnail_cache = None
        self._thumbnail_keys: dict[str, object] = {}
        self._items_by_thumbnail: dict[object, set[str]] = {}
        self._thumbnail_generation = 0
        self.set_items(items)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == int(Qt.ItemDataRole.DisplayRole):
            return item.title
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return item.title if not item.subtitle else f"{item.title}\n{item.subtitle}"
        if role == int(Qt.ItemDataRole.AccessibleTextRole):
            label = "Album" if item.kind == MediaKind.ALBUM else "Artist"
            return f"{label}: {item.title}" + (f", {item.subtitle}" if item.subtitle else "")
        if role == int(MediaRole.KEY):
            return item.key
        if role == int(MediaRole.KIND):
            return item.kind.value
        if role == int(MediaRole.TITLE):
            return item.title
        if role == int(MediaRole.SUBTITLE):
            return item.subtitle
        if role == int(MediaRole.ARTWORK_PATH):
            return item.artwork_path
        if role == int(MediaRole.IMAGE_STATE):
            return item.image_state.value
        if role == int(MediaRole.HAS_CACHED_IMAGE):
            return item.has_cached_image
        if role == int(MediaRole.SOURCE_URL):
            return item.source_url
        if role == int(MediaRole.THUMBNAIL):
            key = self._thumbnail_keys.get(item.key)
            if key is not None and self._thumbnail_cache is not None:
                return self._thumbnail_cache.peek(key)
        return None

    def roleNames(self) -> dict[int, bytes]:  # noqa: N802
        names = super().roleNames()
        names.update(
            {
                int(MediaRole.KEY): b"key",
                int(MediaRole.KIND): b"kind",
                int(MediaRole.TITLE): b"title",
                int(MediaRole.SUBTITLE): b"subtitle",
                int(MediaRole.ARTWORK_PATH): b"artworkPath",
                int(MediaRole.THUMBNAIL): b"thumbnail",
                int(MediaRole.IMAGE_STATE): b"imageState",
                int(MediaRole.HAS_CACHED_IMAGE): b"hasCachedImage",
                int(MediaRole.SOURCE_URL): b"sourceUrl",
            }
        )
        return names

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_items(self, items: Iterable[MediaItem]) -> None:
        prepared = tuple(items)
        keys = [item.key for item in prepared]
        if len(set(keys)) != len(keys):
            raise ValueError("Media item keys must be unique.")
        self.beginResetModel()
        self._items = prepared
        self._rows_by_key = {item.key: row for row, item in enumerate(prepared)}
        self._thumbnail_keys.clear()
        self._items_by_thumbnail.clear()
        self.endResetModel()

    def items(self) -> tuple[MediaItem, ...]:
        return self._items

    def item(self, row: int) -> MediaItem | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def item_for_key(self, key: str) -> MediaItem | None:
        row = self._rows_by_key.get(str(key))
        return self._items[row] if row is not None else None

    def index_for_key(self, key: str) -> QModelIndex:
        row = self._rows_by_key.get(str(key))
        return self.index(row, 0) if row is not None else QModelIndex()

    def replace_item(self, key: str, **changes) -> bool:
        row = self._rows_by_key.get(str(key))
        if row is None:
            return False
        if "key" in changes and str(changes["key"]) != str(key):
            raise ValueError("A media item's stable key cannot be replaced in place.")
        previous_item = self._items[row]
        replacement = replace(previous_item, **changes)
        if replacement.artwork_path != previous_item.artwork_path:
            previous_thumbnail = self._thumbnail_keys.pop(str(key), None)
            if previous_thumbnail is not None:
                bound_items = self._items_by_thumbnail.get(previous_thumbnail)
                if bound_items is not None:
                    bound_items.discard(str(key))
                    if not bound_items:
                        self._items_by_thumbnail.pop(previous_thumbnail, None)
        mutable = list(self._items)
        mutable[row] = replacement
        self._items = tuple(mutable)
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)
        return True

    def bind_thumbnail_cache(self, cache) -> None:
        if self._thumbnail_cache is cache:
            return
        if self._thumbnail_cache is not None:
            try:
                self._thumbnail_cache.thumbnail_ready.disconnect(self._thumbnail_ready)
                self._thumbnail_cache.thumbnail_failed.disconnect(self._thumbnail_failed)
            except (RuntimeError, TypeError):
                pass
        self._thumbnail_cache = cache
        if cache is not None:
            cache.thumbnail_ready.connect(self._thumbnail_ready)
            cache.thumbnail_failed.connect(self._thumbnail_failed)

    def set_thumbnail_generation(self, generation: int) -> None:
        self._thumbnail_generation = int(generation)

    def bind_thumbnail(self, item_key: str, thumbnail_key: object) -> bool:
        row = self._rows_by_key.get(str(item_key))
        if row is None:
            return False
        previous = self._thumbnail_keys.get(str(item_key))
        if previous is not None:
            previous_items = self._items_by_thumbnail.get(previous)
            if previous_items is not None:
                previous_items.discard(str(item_key))
        self._thumbnail_keys[str(item_key)] = thumbnail_key
        self._items_by_thumbnail.setdefault(thumbnail_key, set()).add(str(item_key))
        if self._thumbnail_cache is not None and self._thumbnail_cache.peek(thumbnail_key) is not None:
            mutable = list(self._items)
            mutable[row] = replace(mutable[row], image_state=MediaImageState.READY)
            self._items = tuple(mutable)
        index = self.index(row, 0)
        self.dataChanged.emit(
            index,
            index,
            [int(MediaRole.THUMBNAIL), int(MediaRole.IMAGE_STATE)],
        )
        return True

    def _emit_thumbnail_change(self, thumbnail_key: object, generation: int, failed: bool) -> None:
        if int(generation) != self._thumbnail_generation:
            return
        for item_key in tuple(self._items_by_thumbnail.get(thumbnail_key, ())):
            row = self._rows_by_key.get(item_key)
            if row is None:
                continue
            mutable = list(self._items)
            mutable[row] = replace(
                mutable[row],
                image_state=(MediaImageState.FAILED if failed else MediaImageState.READY),
            )
            self._items = tuple(mutable)
            index = self.index(row, 0)
            self.dataChanged.emit(
                index,
                index,
                [int(MediaRole.THUMBNAIL), int(MediaRole.IMAGE_STATE)],
            )

    def _thumbnail_ready(self, thumbnail_key: object, _pixmap: QPixmap, generation: int) -> None:
        self._emit_thumbnail_change(thumbnail_key, generation, False)

    def _thumbnail_failed(self, thumbnail_key: object, _reason: str, generation: int) -> None:
        self._emit_thumbnail_change(thumbnail_key, generation, True)


class MediaFilterProxyModel(QSortFilterProxyModel):
    """Case-insensitive title/subtitle filtering without rebuilding the source model."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._needle = ""
        self.setDynamicSortFilter(True)

    def set_filter_text(self, text: str) -> None:
        needle = str(text).strip().casefold()
        if needle == self._needle:
            return
        if hasattr(self, "beginFilterChange") and hasattr(self, "endFilterChange"):
            self.beginFilterChange()
            self._needle = needle
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:  # PySide 6.7/6.8 compatibility.
            self._needle = needle
            self.invalidateFilter()

    def filter_text(self) -> str:
        return self._needle

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        if not self._needle:
            return True
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        title = str(model.data(index, int(MediaRole.TITLE)) or "")
        subtitle = str(model.data(index, int(MediaRole.SUBTITLE)) or "")
        return self._needle in f"{title}\n{subtitle}".casefold()


class MediaCardDelegate(QStyledItemDelegate):
    GRID_SIZE = QSize(200, 248)
    CARD_SIZE = QSize(184, 232)
    ARTWORK_SIZE = 156

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(self.GRID_SIZE)

    @staticmethod
    def _elided(painter: QPainter, text: str, width: int) -> str:
        return painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(0, width))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        cell = QRectF(option.rect)
        card = QRectF(
            cell.x() + (cell.width() - self.CARD_SIZE.width()) / 2,
            cell.y() + (cell.height() - self.CARD_SIZE.height()) / 2,
            self.CARD_SIZE.width(),
            self.CARD_SIZE.height(),
        )
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        background = COLORS["selection"] if selected else (
            COLORS["hover_surface"] if hovered else COLORS["card_surface"]
        )
        border = COLORS["focus_ring"] if focused else (
            COLORS["strong_border"] if selected or hovered else COLORS["border"]
        )
        painter.setPen(QPen(QColor(border), 2 if focused else 1))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(card, RADII["card"], RADII["card"])

        artwork = QRectF(card.x() + 14, card.y() + 14, self.ARTWORK_SIZE, self.ARTWORK_SIZE)
        kind = MediaKind(index.data(int(MediaRole.KIND)) or MediaKind.ALBUM.value)
        artwork_path = QPainterPath()
        if kind == MediaKind.ARTIST:
            artwork_path.addEllipse(artwork)
        else:
            artwork_path.addRoundedRect(artwork, RADII["artwork"], RADII["artwork"])
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.setBrush(QColor(COLORS["subtle_surface"]))
        painter.drawPath(artwork_path)

        thumbnail = index.data(int(MediaRole.THUMBNAIL))
        if isinstance(thumbnail, QPixmap) and not thumbnail.isNull():
            painter.save()
            painter.setClipPath(artwork_path)
            painter.drawPixmap(artwork, thumbnail, QRectF(thumbnail.rect()))
            painter.restore()
        else:
            dpr = option.widget.devicePixelRatioF() if option.widget is not None else 1.0
            icon_name = "artist-unknown" if kind == MediaKind.ARTIST else "albums"
            icon_size = 70 if kind == MediaKind.ARTIST else 44
            placeholder = render_icon_pixmap(icon_name, icon_size, COLORS["text_muted"], dpr=dpr)
            logical = placeholder.deviceIndependentSize()
            target = QRectF(
                artwork.center().x() - logical.width() / 2,
                artwork.center().y() - logical.height() / 2,
                logical.width(),
                logical.height(),
            )
            painter.drawPixmap(target, placeholder, QRectF(placeholder.rect()))

        if index.data(int(MediaRole.IMAGE_STATE)) == MediaImageState.LOADING.value:
            marker = QRectF(artwork.right() - 22, artwork.top() + 8, 12, 12)
            painter.setBrush(QColor(COLORS["accent"]))
            painter.setPen(QPen(QColor(COLORS["app_background"]), 2))
            painter.drawEllipse(marker)

        title = str(index.data(int(MediaRole.TITLE)) or "")
        subtitle = str(index.data(int(MediaRole.SUBTITLE)) or "")
        text_left = card.x() + 14
        text_width = int(card.width() - 28)
        title_rect = QRectF(text_left, artwork.bottom() + 9, text_width, 20)
        subtitle_rect = QRectF(text_left, title_rect.bottom() + 2, text_width, 18)
        font = painter.font()
        font.setPixelSize(int(TYPOGRAPHY["body_size"]))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text_primary"]))
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._elided(painter, title, text_width),
        )
        font.setPixelSize(int(TYPOGRAPHY["metadata_size"]))
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text_muted"]))
        painter.drawText(
            subtitle_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._elided(painter, subtitle, text_width),
        )
        painter.restore()


class MediaGridView(QListView):
    item_opened = Signal(str)
    item_context_requested = Signal(str, QPoint)
    visible_items_changed = Signal(tuple)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MediaGridView")
        self.setAccessibleName("Media browser")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(96)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setGridSize(MediaCardDelegate.GRID_SIZE)
        self.setItemDelegate(MediaCardDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSpacing(0)
        self.clicked.connect(self._open_index)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_index)
        self.verticalScrollBar().valueChanged.connect(self.schedule_visible_items)
        self._prefetch_rows = 1
        self._last_visible_signature: tuple[tuple[str, ...], float] | None = None
        self._state = MediaGridState.CONTENT
        self._state_title = ""
        self._state_description = ""
        self._state_icon = "albums"
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(0)
        self._visible_timer.timeout.connect(self._emit_visible_items)

    def setModel(self, model) -> None:  # noqa: N802
        previous = self.model()
        if previous is not None:
            for signal in (
                previous.modelReset,
                previous.rowsInserted,
                previous.rowsRemoved,
                previous.layoutChanged,
            ):
                try:
                    signal.disconnect(self._model_content_changed)
                except (RuntimeError, TypeError):
                    pass
        super().setModel(model)
        if model is not None:
            model.modelReset.connect(self._model_content_changed)
            model.rowsInserted.connect(self._model_content_changed)
            model.rowsRemoved.connect(self._model_content_changed)
            model.layoutChanged.connect(self._model_content_changed)
        self._last_visible_signature = None
        self.schedule_visible_items()

    def _model_content_changed(self, *_args) -> None:
        # A reset clears model-to-thumbnail bindings even when the stable keys
        # remain the same. Force visible work to be requested again.
        self._last_visible_signature = None
        self.schedule_visible_items()

    def set_prefetch_rows(self, rows: int) -> None:
        self._prefetch_rows = max(0, int(rows))
        self.schedule_visible_items()

    def set_view_state(
        self,
        state: MediaGridState | str,
        title: str = "",
        description: str = "",
        icon_name: str = "albums",
    ) -> None:
        self._state = MediaGridState(state)
        self._state_title = str(title)
        self._state_description = str(description)
        self._state_icon = str(icon_name)
        self.viewport().update()

    def view_state(self) -> MediaGridState:
        return self._state

    def visible_item_keys(self, near_rows: int | None = None) -> tuple[str, ...]:
        model = self.model()
        if model is None or model.rowCount() <= 0 or self._state != MediaGridState.CONTENT:
            return ()
        grid = self.gridSize()
        grid_width = max(1, grid.width())
        grid_height = max(1, grid.height())
        columns = max(1, self.viewport().width() // grid_width)
        prefetch = self._prefetch_rows if near_rows is None else max(0, int(near_rows))
        scroll = max(0, self.verticalScrollBar().value())
        first_grid_row = max(0, scroll // grid_height - prefetch)
        last_grid_row = (scroll + self.viewport().height() + grid_height - 1) // grid_height + prefetch
        first = first_grid_row * columns
        last = min(model.rowCount(), (last_grid_row + 1) * columns)
        keys: list[str] = []
        for row in range(first, last):
            key = model.index(row, 0).data(int(MediaRole.KEY))
            if key is not None:
                keys.append(str(key))
        return tuple(keys)

    def schedule_visible_items(self, *_args) -> None:
        if not self._visible_timer.isActive():
            self._visible_timer.start()

    def _emit_visible_items(self) -> None:
        keys = self.visible_item_keys()
        signature = (keys, round(float(self.devicePixelRatioF()), 2))
        if signature == self._last_visible_signature:
            return
        self._last_visible_signature = signature
        self.visible_items_changed.emit(keys)

    @staticmethod
    def _key_for_index(index: QModelIndex) -> str | None:
        if not index.isValid():
            return None
        key = index.data(int(MediaRole.KEY))
        return str(key) if key is not None else None

    def _open_index(self, index: QModelIndex) -> None:
        key = self._key_for_index(index)
        if key:
            self.item_opened.emit(key)

    def _context_index(self, position: QPoint) -> None:
        index = self.indexAt(position)
        key = self._key_for_index(index)
        if not key:
            return
        self.setCurrentIndex(index)
        self.item_context_requested.emit(key, self.viewport().mapToGlobal(position))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            key = self._key_for_index(self.currentIndex())
            if key:
                self.item_opened.emit(key)
                event.accept()
                return
        super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.schedule_visible_items()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.schedule_visible_items()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self.schedule_visible_items()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        super().mousePressEvent(event)
        self.schedule_visible_items()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._state == MediaGridState.CONTENT:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.viewport().rect(), QColor(COLORS["card_surface"]))
        center = QRectF(self.viewport().rect()).center()
        icon = render_icon_pixmap(self._state_icon, 42, COLORS["text_muted"], self.devicePixelRatioF())
        logical = icon.deviceIndependentSize()
        icon_rect = QRectF(center.x() - logical.width() / 2, center.y() - 64, logical.width(), logical.height())
        painter.drawPixmap(icon_rect, icon, QRectF(icon.rect()))
        title_font = painter.font()
        title_font.setPixelSize(int(TYPOGRAPHY["section_title_size"]))
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text_primary"]))
        painter.drawText(
            QRectF(24, center.y() - 10, self.viewport().width() - 48, 28),
            Qt.AlignmentFlag.AlignCenter,
            self._state_title,
        )
        body_font = painter.font()
        body_font.setPixelSize(int(TYPOGRAPHY["metadata_size"]))
        body_font.setWeight(QFont.Weight.Normal)
        painter.setFont(body_font)
        painter.setPen(QColor(COLORS["text_muted"]))
        painter.drawText(
            QRectF(40, center.y() + 22, self.viewport().width() - 80, 44),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            self._state_description,
        )
        painter.end()
