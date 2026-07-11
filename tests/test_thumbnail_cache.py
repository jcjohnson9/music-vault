from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest

from music_vault.ui import thumbnail_cache as thumbnail_module
from music_vault.ui.media_grid import MediaGridModel, MediaItem, MediaKind, MediaRole
from music_vault.ui.thumbnail_cache import ThumbnailCache, make_thumbnail_key


def _write_image(path: Path, color: str, width: int = 180, height: int = 120) -> Path:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "PNG")
    return path


def _wait(qapp, predicate, timeout_ms: int = 3000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(10)
    assert predicate()


def test_thumbnail_key_includes_size_dpr_and_crop(tmp_path):
    path = tmp_path / "image.png"
    first = make_thumbnail_key(path, 156, 1.25, "square")
    second = make_thumbnail_key(path, 156, 1.5, "square")
    third = make_thumbnail_key(path, 72, 1.25, "portrait")
    assert len({first, second, third}) == 3
    assert first.physical_size == 195
    assert second.physical_size == 234


def test_background_decode_returns_high_dpi_pixmap_on_gui_thread(tmp_path, qapp):
    path = _write_image(tmp_path / "wide.png", "#2E8B57", 240, 120)
    cache = ThumbnailCache(max_workers=1)
    generation = cache.advance_generation()
    delivered: list[tuple[object, QPixmap, int, object]] = []
    cache.thumbnail_ready.connect(
        lambda key, pixmap, token: delivered.append(
            (key, pixmap, token, QThread.currentThread())
        )
    )
    key = cache.request(path, 40, 1.5, generation=generation)
    _wait(qapp, lambda: bool(delivered))

    pixmap = delivered[0][1]
    assert delivered[0][0] == key
    assert delivered[0][2] == generation
    assert delivered[0][3] is qapp.thread()
    assert pixmap.width() == 60 and pixmap.height() == 60
    assert pixmap.devicePixelRatio() == 1.5
    assert pixmap.deviceIndependentSize().width() == 40
    assert cache.peek(key) is not None
    cache.close()


def test_duplicate_requests_coalesce(monkeypatch, tmp_path, qapp):
    path = _write_image(tmp_path / "coalesce.png", "#315E8A")
    original = thumbnail_module._decode_thumbnail

    def delayed(key):
        time.sleep(0.08)
        return original(key)

    monkeypatch.setattr(thumbnail_module, "_decode_thumbnail", delayed)
    cache = ThumbnailCache(max_workers=1)
    generation = cache.advance_generation()
    ready: list[object] = []
    cache.thumbnail_ready.connect(lambda key, _pixmap, _token: ready.append(key))
    first = cache.request(path, 48, generation=generation)
    second = cache.request(path, 48, generation=generation)
    assert first == second
    assert cache.pending_count == 1
    _wait(qapp, lambda: cache.pending_count == 0)
    assert cache.stats.misses == 1
    assert cache.stats.coalesced == 1
    assert cache.stats.decodes == 1
    assert ready == [first]
    cache.close()


def test_stale_generation_is_silently_ignored(monkeypatch, tmp_path, qapp):
    path = _write_image(tmp_path / "stale.png", "#995533")
    original = thumbnail_module._decode_thumbnail

    def delayed(key):
        time.sleep(0.08)
        return original(key)

    monkeypatch.setattr(thumbnail_module, "_decode_thumbnail", delayed)
    cache = ThumbnailCache(max_workers=1)
    stale_generation = cache.advance_generation()
    ready: list[int] = []
    cache.thumbnail_ready.connect(lambda _key, _pixmap, token: ready.append(token))
    cache.request(path, 48, generation=stale_generation)
    current_generation = cache.advance_generation()
    _wait(qapp, lambda: cache.pending_count == 0)
    assert current_generation != stale_generation
    assert ready == []
    cache.close()


def test_invalidated_pending_decode_cannot_replace_new_request(monkeypatch, tmp_path, qapp):
    path = _write_image(tmp_path / "refresh.png", "#225588")
    original = thumbnail_module._decode_thumbnail

    def delayed(key):
        time.sleep(0.06)
        return original(key)

    monkeypatch.setattr(thumbnail_module, "_decode_thumbnail", delayed)
    cache = ThumbnailCache(max_workers=1)
    generation = cache.advance_generation()
    ready: list[object] = []
    cache.thumbnail_ready.connect(lambda key, _pixmap, _token: ready.append(key))
    old_key = cache.request(path, 36, generation=generation)
    cache.invalidate_source(path)
    new_key = cache.request(path, 36, generation=generation)
    assert new_key == old_key
    _wait(qapp, lambda: cache.pending_count == 0)
    assert ready == [new_key]
    assert cache.peek(new_key) is not None
    cache.close()


def test_lru_obeys_byte_bound_and_visible_requests_are_explicit(tmp_path, qapp):
    paths = [
        _write_image(tmp_path / f"image-{index}.png", color)
        for index, color in enumerate(("#A33", "#3A3", "#33A", "#AA3"))
    ]
    one_image_bytes = 32 * 32 * 4
    cache = ThumbnailCache(max_bytes=one_image_bytes * 2, max_workers=1)
    generation = cache.advance_generation()
    keys = cache.request_visible(paths[:3], 32, generation=generation)
    _wait(qapp, lambda: cache.pending_count == 0)
    assert len(keys) == 3
    assert cache.stats.decodes == 3
    assert cache.cache_count <= 2
    assert cache.cache_bytes <= one_image_bytes * 2
    assert cache.stats.evictions >= 1
    assert cache.stats.requests == 3
    assert make_thumbnail_key(paths[3], 32, 1.0) not in keys
    cache.close()


def test_missing_and_corrupt_images_fail_safely(tmp_path, qapp):
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not an image")
    cache = ThumbnailCache(max_workers=1)
    generation = cache.advance_generation()
    failed: list[str] = []
    cache.thumbnail_failed.connect(lambda _key, reason, _token: failed.append(reason))
    cache.request(tmp_path / "missing.png", 32, generation=generation)
    cache.request(corrupt, 32, generation=generation)
    _wait(qapp, lambda: cache.pending_count == 0)
    assert sorted(failed) == ["decode_failed", "missing"]
    assert cache.stats.failures == 2
    cache.close()


def test_model_reads_pixmap_from_cache_without_retaining_a_copy(tmp_path, qapp):
    path = _write_image(tmp_path / "bound.png", "#187744")
    cache = ThumbnailCache(max_workers=1)
    generation = cache.advance_generation()
    model = MediaGridModel(
        [MediaItem("album:bound", MediaKind.ALBUM, "Bound", artwork_path=path)]
    )
    model.bind_thumbnail_cache(cache)
    model.set_thumbnail_generation(generation)
    key = cache.request(path, 40, generation=generation)
    assert model.bind_thumbnail("album:bound", key)
    index = model.index(0, 0)
    _wait(qapp, lambda: isinstance(index.data(int(MediaRole.THUMBNAIL)), QPixmap))
    assert index.data(int(MediaRole.IMAGE_STATE)) == "ready"
    cache.clear()
    assert index.data(int(MediaRole.THUMBNAIL)) is None
    model.bind_thumbnail_cache(None)
    cache.close()


def test_close_abandons_pending_results(monkeypatch, tmp_path, qapp):
    path = _write_image(tmp_path / "close.png", "#4444AA")
    original = thumbnail_module._decode_thumbnail

    def delayed(key):
        time.sleep(0.08)
        return original(key)

    monkeypatch.setattr(thumbnail_module, "_decode_thumbnail", delayed)
    cache = ThumbnailCache(max_workers=1)
    cache.advance_generation()
    delivered: list[object] = []
    cache.thumbnail_ready.connect(lambda key, _pixmap, _token: delivered.append(key))
    cache.request(path, 32)
    cache.close()
    QTest.qWait(150)
    qapp.processEvents()
    assert cache.is_closed
    assert cache.pending_count == 0
    assert cache.cache_count == 0
    assert delivered == []
