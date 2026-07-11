from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QObject, QRunnable, QSize, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QImage, QImageReader, QPixmap


@dataclass(frozen=True, slots=True)
class ThumbnailKey:
    source: str
    logical_size: int
    dpr: float
    crop: str = "square"

    @property
    def physical_size(self) -> int:
        return max(1, int(math.ceil(self.logical_size * self.dpr)))


@dataclass(frozen=True, slots=True)
class ThumbnailCacheStats:
    requests: int
    hits: int
    misses: int
    coalesced: int
    decodes: int
    failures: int
    evictions: int
    entries: int
    bytes_used: int
    pending: int


def _normalized_dpr(value: float) -> float:
    try:
        dpr = float(value)
    except (TypeError, ValueError, OverflowError):
        dpr = 1.0
    if not math.isfinite(dpr) or dpr <= 0:
        dpr = 1.0
    return round(min(dpr, 4.0), 2)


def make_thumbnail_key(
    source: str | Path,
    logical_size: int,
    dpr: float,
    crop: str = "square",
) -> ThumbnailKey:
    size = int(logical_size)
    if size <= 0:
        raise ValueError("Thumbnail size must be positive.")
    crop_mode = str(crop).strip().lower()
    if crop_mode not in {"square", "portrait"}:
        raise ValueError("Thumbnail crop must be 'square' or 'portrait'.")
    path = Path(source).expanduser().resolve(strict=False)
    return ThumbnailKey(str(path), size, _normalized_dpr(dpr), crop_mode)


def _decode_thumbnail(key: ThumbnailKey) -> tuple[QImage, str | None]:
    path = Path(key.source)
    if not path.is_file():
        return QImage(), "missing"

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    source_size = reader.size()
    target = key.physical_size
    if source_size.isValid() and source_size.width() > 0 and source_size.height() > 0:
        factor = target / min(source_size.width(), source_size.height())
        scaled_size = QSize(
            max(target, int(math.ceil(source_size.width() * factor))),
            max(target, int(math.ceil(source_size.height() * factor))),
        )
        reader.setScaledSize(scaled_size)

    image = reader.read()
    if image.isNull():
        return QImage(), "decode_failed"

    scaled = image.scaled(
        QSize(target, target),
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    left = max(0, (scaled.width() - target) // 2)
    top = max(0, (scaled.height() - target) // 2)
    cropped = scaled.copy(left, top, target, target)
    if cropped.isNull():
        return QImage(), "decode_failed"
    return cropped.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied), None


class _DecodeSignals(QObject):
    finished = Signal(object, int, object, object)


class _DecodeTask(QRunnable):
    def __init__(self, key: ThumbnailKey, request_id: int, signals: _DecodeSignals) -> None:
        super().__init__()
        self.key = key
        self.request_id = int(request_id)
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            image, error = _decode_thumbnail(self.key)
        except Exception:
            image, error = QImage(), "decode_failed"
        try:
            self.signals.finished.emit(self.key, self.request_id, image, error)
        except RuntimeError:
            # The owning window may have been destroyed while this bounded
            # local-file decode was completing. The result is safe to abandon.
            pass


class ThumbnailCache(QObject):
    """Bounded GUI-owned pixmap LRU with coalesced worker-thread QImage decoding."""

    thumbnail_ready = Signal(object, object, int)
    thumbnail_failed = Signal(object, str, int)

    def __init__(
        self,
        max_bytes: int = 64 * 1024 * 1024,
        max_workers: int = 3,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if int(max_bytes) <= 0:
            raise ValueError("Thumbnail cache memory bound must be positive.")
        self.max_bytes = int(max_bytes)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_workers)))
        self._signals = _DecodeSignals(self)
        self._signals.finished.connect(
            self._on_decoded,
            Qt.ConnectionType.QueuedConnection,
        )
        self._cache: OrderedDict[ThumbnailKey, tuple[QPixmap, int]] = OrderedDict()
        self._bytes_used = 0
        self._pending: dict[ThumbnailKey, tuple[int, set[int]]] = {}
        self._next_request_id = 0
        self._generation = 0
        self._closed = False
        self._requests = 0
        self._hits = 0
        self._misses = 0
        self._coalesced = 0
        self._decodes = 0
        self._failures = 0
        self._evictions = 0

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def cache_bytes(self) -> int:
        return self._bytes_used

    @property
    def cache_count(self) -> int:
        return len(self._cache)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> ThumbnailCacheStats:
        return ThumbnailCacheStats(
            requests=self._requests,
            hits=self._hits,
            misses=self._misses,
            coalesced=self._coalesced,
            decodes=self._decodes,
            failures=self._failures,
            evictions=self._evictions,
            entries=len(self._cache),
            bytes_used=self._bytes_used,
            pending=len(self._pending),
        )

    def advance_generation(self) -> int:
        self._generation += 1
        return self._generation

    def peek(self, key: ThumbnailKey) -> QPixmap | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        self._cache.move_to_end(key)
        return entry[0]

    def request(
        self,
        source: str | Path,
        logical_size: int,
        dpr: float = 1.0,
        *,
        crop: str = "square",
        generation: int | None = None,
    ) -> ThumbnailKey:
        key = make_thumbnail_key(source, logical_size, dpr, crop)
        if self._closed:
            return key
        token = self._generation if generation is None else int(generation)
        self._requests += 1
        cached = self.peek(key)
        if cached is not None:
            self._hits += 1
            if token == self._generation:
                self.thumbnail_ready.emit(key, cached, token)
            return key
        pending = self._pending.get(key)
        if pending is not None:
            self._coalesced += 1
            pending[1].add(token)
            return key
        self._misses += 1
        self._next_request_id += 1
        request_id = self._next_request_id
        self._pending[key] = (request_id, {token})
        self._pool.start(_DecodeTask(key, request_id, self._signals))
        return key

    def request_visible(
        self,
        sources: Iterable[str | Path],
        logical_size: int,
        dpr: float = 1.0,
        *,
        crop: str = "square",
        generation: int | None = None,
    ) -> tuple[ThumbnailKey, ...]:
        return tuple(
            self.request(
                source,
                logical_size,
                dpr,
                crop=crop,
                generation=generation,
            )
            for source in sources
        )

    @Slot(object, int, object, object)
    def _on_decoded(
        self,
        key: ThumbnailKey,
        request_id: int,
        image: QImage,
        error: str | None,
    ) -> None:
        pending = self._pending.get(key)
        if pending is None or pending[0] != int(request_id):
            return
        _active_id, observers = self._pending.pop(key)
        if self._closed:
            return
        self._decodes += 1
        if error or image.isNull():
            self._failures += 1
            for token in observers:
                if token == self._generation:
                    self.thumbnail_failed.emit(key, error or "decode_failed", token)
            return

        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(key.dpr)
        byte_count = max(1, pixmap.width() * pixmap.height() * 4)
        if byte_count <= self.max_bytes:
            self._insert(key, pixmap, byte_count)
        for token in observers:
            if token == self._generation:
                self.thumbnail_ready.emit(key, pixmap, token)

    def _insert(self, key: ThumbnailKey, pixmap: QPixmap, byte_count: int) -> None:
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._bytes_used -= previous[1]
        self._cache[key] = (pixmap, byte_count)
        self._bytes_used += byte_count
        while self._bytes_used > self.max_bytes and self._cache:
            _evicted_key, (_pixmap, evicted_bytes) = self._cache.popitem(last=False)
            self._bytes_used -= evicted_bytes
            self._evictions += 1

    def invalidate_source(self, source: str | Path) -> int:
        normalized = str(Path(source).expanduser().resolve(strict=False))
        removed = 0
        for key in tuple(self._cache):
            if key.source != normalized:
                continue
            _pixmap, byte_count = self._cache.pop(key)
            self._bytes_used -= byte_count
            removed += 1
        for key in tuple(self._pending):
            if key.source == normalized:
                self._pending.pop(key, None)
        return removed

    def clear(self) -> None:
        self._cache.clear()
        self._bytes_used = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._pool.clear()
        self._pending.clear()
        self.clear()
