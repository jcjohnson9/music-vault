from dataclasses import FrozenInstanceError
import sqlite3
import threading
import time

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyleOptionViewItem, QWidget

from music_vault.ui.track_list import (
    NOW_PLAYING_ROLE, TRACK_ID_ROLE, TrackFilterProxyModel, TrackRow,
    TrackTableModel, TrackTableView,
)
from music_vault.ui.thumbnail_cache import ThumbnailCache


def _tracks(count=20):
    return [TrackRow(index + 1, f"Synthetic song {index:05}", f"Artist {index % 5}",
                     f"Collection {index % 3}", "2001", f"art-{index}.png")
            for index in range(count)]


class FakeCache(QObject):
    thumbnail_ready = Signal(object, object, int)
    thumbnail_failed = Signal(object, str, int)

    def __init__(self):
        super().__init__()
        self.generation = 0
        self.requests = []
        self.pixmaps = {}

    def request(self, source, size, dpr=1.0, *, generation=0):
        key = (source, size, dpr)
        self.requests.append((key, generation))
        return key

    def peek(self, key):
        return self.pixmaps.get(key)


def test_track_record_sqlite_mapping_and_path_stem_are_display_only():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    record = connection.execute(
        "SELECT 9 AS id, NULL AS title, ? AS path, 'Band' AS artist, NULL AS year",
        (r"C:\not-public\A file.mp3",),
    ).fetchone()
    row = TrackRow.from_record(record)
    connection.close()
    assert row == TrackRow(9, "A file", "Band")
    with pytest.raises(FrozenInstanceError):
        row.title = "modified"
    assert TrackRow.from_record({"id": 3, "path": "/private/Second.mp3"}).title == "Second"
    assert TrackRow.from_record({"id": 4}).title == "Untitled"
    assert TrackRow.from_record(row) is row


def test_model_literal_unicode_roles_no_private_path_or_extra_columns():
    title = "<b>Café & 夜</b> [Live]"
    model = TrackTableModel([{
        "id": 17, "title": title, "artist": "Björk", "album": "ＡＬＢＵＭ",
        "year": 2024, "path": r"C:\private-folder\secret-title.mp3",
        "cover_path": r"C:\private-art\cover.jpg",
    }])
    assert model.columnCount() == 4
    index = model.index(0, 0)
    assert index.data() == title
    assert index.data(Qt.ItemDataRole.ToolTipRole) == title
    assert index.data(TRACK_ID_ROLE) == 17
    assert index.data(NOW_PLAYING_ROLE) is False
    assert index.data(TRACK_ID_ROLE + 2) is None
    assert "private" not in index.data(Qt.ItemDataRole.AccessibleTextRole)
    assert model.index(0, 3).data() == "2024"
    assert model.track_for_id(17).cover_path.endswith("cover.jpg")
    assert model.source_row_for_track_id(17) == 0
    assert model.track_for_id(99) is None
    assert model.source_row_for_track_id(99) is None
    assert not (model.flags(index) & Qt.ItemFlag.ItemIsEditable)


def test_filter_normalizes_accents_case_width_and_literal_tokens_without_paths():
    model = TrackTableModel([
        {"id": 1, "title": "Café [Live]", "artist": "Björk", "album": "ＡＬＢＵＭ",
         "path": "/private-path-marker/hidden.mp3", "cover_path": "/cover-secret/art.jpg"},
        TrackRow(2, "Other", "Straße"),
    ])
    proxy = TrackFilterProxyModel()
    proxy.setSourceModel(model)
    for text, expected in [("cafe bjork", 1), ("ALBUM [live]", 1), ("STRASSE", 2)]:
        proxy.set_filter(text)
        assert proxy.rowCount() == 1
        assert proxy.index(0, 0).data(TRACK_ID_ROLE) == expected
    for text in ("private-path-marker", "cover-secret", "hidden", "Caf.*"):
        proxy.set_filter(text)
        assert proxy.rowCount() == 0
    proxy.set_filter("  ")
    assert proxy.rowCount() == 2


def test_model_reset_duplicate_rejection_and_replace_are_atomic():
    model = TrackTableModel(_tracks(2))
    with pytest.raises(ValueError, match="unique"):
        model.set_tracks([TrackRow(8, "One"), TrackRow(8, "Two")])
    assert model.rowCount() == 2
    assert model.track_for_id(1) is not None
    assert not model.replace_track(TrackRow(8, "Absent"))
    assert model.replace_track(TrackRow(2, "Changed", "New artist"))
    assert model.track_for_id(2).title == "Changed"
    assert model.source_row_for_track_id(2) == 1
    model.set_tracks([TrackRow(2, "Moved"), TrackRow(1, "Old")])
    assert model.source_row_for_track_id(2) == 0


def test_sort_filter_and_selection_are_stable_track_ids(qapp):
    view = TrackTableView(thumbnail_cache=FakeCache())
    view.set_tracks([TrackRow(30, "Same", "Artist"), TrackRow(10, "Zed", "Artist"),
                     TrackRow(40, "Same", "Artist"), TrackRow(20, "Alpha", "Else")])
    assert view.source_track_ids() == [30, 10, 40, 20]
    assert view.visible_track_ids() == [30, 10, 40, 20]
    view.select_track(40)
    view.select_track(30, clear=False)
    assert view.selected_track_ids() == [30, 40]
    assert view.current_track_id() == 30
    view.set_sort(0)
    assert view.visible_track_ids() == [20, 30, 40, 10]
    assert view.selected_track_ids() == [30, 40]
    assert view.current_track_id() == 30
    view.set_sort(0, Qt.SortOrder.DescendingOrder)
    assert view.visible_track_ids() == [10, 30, 40, 20]
    assert view.current_track_id() == 30
    view.set_filter("Artist")
    assert view.visible_track_ids() == [10, 30, 40]
    assert view.row_for_track_id(20) is None
    assert view.visible_track_count() == 3
    assert view.total_track_count() == 4
    assert view.track_id_at(0) == 10
    assert view.track_id_at(-1) is None
    assert not view.select_track(20)
    assert not view.scroll_to_track(20)
    view.clear_sort()
    assert view.visible_track_ids() == [30, 10, 40]
    assert view.source_track_ids() == [30, 10, 40, 20]
    view.close()


def test_header_click_cycles_sort_then_original_playlist_order_with_stable_ids(qapp):
    view = TrackTableView(thumbnail_cache=FakeCache())
    view.resize(800, 300)
    source_ids = [30, 10, 40, 20]
    view.set_tracks([TrackRow(30, "Same", "Z"), TrackRow(10, "Zed", "A"),
                     TrackRow(40, "Same", "Z"), TrackRow(20, "Alpha", "B")])
    view.show()
    qapp.processEvents()
    view.restore_selection([30, 40], 40)
    header = view.horizontalHeader()

    def click(column):
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(header.sectionViewportPosition(column) + header.sectionSize(column) // 2,
                                    header.height() // 2))
        qapp.processEvents()

    assert view.visible_track_ids() == source_ids
    assert not header.isSortIndicatorShown()
    for expected in ([20, 30, 40, 10], [10, 30, 40, 20], source_ids):
        click(0)
        assert view.visible_track_ids() == expected
        assert view.selected_track_ids() == [30, 40]
        assert view.current_track_id() == 40
        assert view.source_track_ids() == source_ids
    assert not header.isSortIndicatorShown()
    click(0)
    click(1)  # A different column starts ascending, regardless of prior sort.
    assert view.visible_track_ids() == [10, 20, 30, 40]
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    view.close()


def test_reset_restores_visible_selection_and_clears_missing_current(qapp):
    view = TrackTableView(thumbnail_cache=FakeCache())
    view.set_tracks(_tracks(4))
    emitted = []
    view.selected_tracks_changed.connect(lambda: emitted.append(True))
    view.restore_selection([1, 3], 3)
    view.set_tracks(list(reversed(_tracks(4))))
    assert view.selected_track_ids() == [3, 1]
    assert view.current_track_id() == 3
    view.set_filter("00000")
    assert view.selected_track_ids() == [1]
    assert view.current_track_id() is None
    assert emitted
    view.restore_selection([], None)
    assert view.selected_track_ids() == []
    assert view.current_track_id() is None
    view.close()


def test_now_playing_survives_filter_reorder_and_is_not_selection(qapp):
    view = TrackTableView(thumbnail_cache=FakeCache())
    view.set_tracks(_tracks(4))
    view.select_track(2)
    view.set_now_playing(3)
    source = view.track_model
    assert source.index(2, 0).data(NOW_PLAYING_ROLE) is True
    assert view.current_track_id() == 2
    view.set_filter("00000")
    assert view.row_for_track_id(3) is None
    assert source.index(2, 0).data(NOW_PLAYING_ROLE) is True
    view.set_tracks(list(reversed(_tracks(4))))
    assert source.index(1, 0).data(NOW_PLAYING_ROLE) is True
    view.set_filter("")
    assert view.row_for_track_id(3) == 1
    view.set_now_playing(None)
    assert not any(source.index(row, 0).data(NOW_PLAYING_ROLE) for row in range(4))
    view.set_now_playing(999)
    assert not any(source.index(row, 0).data(NOW_PLAYING_ROLE) for row in range(4))
    view.close()


def test_replace_rechecks_filter_and_does_not_rebuild_all_rows(qapp):
    view = TrackTableView(thumbnail_cache=FakeCache())
    view.set_tracks(_tracks(4))
    resets = []
    view.track_model.modelReset.connect(lambda: resets.append(True))
    view.set_filter("changed")
    assert view.visible_track_count() == 0
    assert view.replace_track(TrackRow(3, "Changed"))
    assert view.visible_track_ids() == [3]
    assert resets == []
    view.close()


def test_artwork_requests_only_visible_nearby_and_bounds_bindings(qapp):
    cache = FakeCache()
    view = TrackTableView(thumbnail_cache=cache)
    view.resize(800, 420)
    view.set_tracks(_tracks(20_000))
    view.set_filter("Synthetic")
    qapp.processEvents()
    assert cache.requests == []  # Hidden views do no image work.
    view.show()
    qapp.processEvents()
    qapp.processEvents()
    initial = len(cache.requests)
    assert 1 <= initial <= 14
    assert len(view._thumbnail_bindings) <= 14
    assert len(view.findChildren(QWidget)) < 25
    assert view.indexWidget(view.proxy_model.index(0, 0)) is None
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    qapp.processEvents()
    qapp.processEvents()
    assert initial < len(cache.requests) <= initial + 14
    assert len(view._thumbnail_bindings) <= 14
    assert all(track_id > 19_970 for track_id in view._thumbnail_bindings)
    view.set_filter("00000")
    qapp.processEvents()
    assert view.visible_track_ids() == [1]
    assert list(view._thumbnail_bindings) == [1]
    view.close()


def test_stale_artwork_results_never_attach_to_replaced_or_reordered_track(qapp):
    cache = FakeCache()
    view = TrackTableView(thumbnail_cache=cache)
    view.resize(800, 240)
    view.set_tracks(_tracks(3))
    view.show()
    qapp.processEvents()
    qapp.processEvents()
    old_key = view._thumbnail_bindings[1][2]
    pixmap = QPixmap(42, 42)
    pixmap.fill(QColor("red"))
    cache.pixmaps[old_key] = pixmap
    assert view.thumbnail_for_track(1) is pixmap
    view.replace_track(TrackRow(1, "Changed", cover_path="new.png"))
    assert view.thumbnail_for_track(1) is None
    cache.thumbnail_ready.emit(old_key, pixmap, 0)
    qapp.processEvents()
    assert view.thumbnail_for_track(1) is None
    assert view._thumbnail_bindings[1][0] == "new.png"
    view.set_tracks([TrackRow(9, "Replacement", cover_path="other.png")])
    cache.thumbnail_ready.emit(old_key, pixmap, 0)
    qapp.processEvents()
    assert view.thumbnail_for_track(9) is None
    assert 1 not in view._thumbnail_bindings
    cache.generation += 1
    view._request_visible_artwork()
    assert cache.requests[-1][1] == 1
    view.close()


def _wait_for(qapp, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(10)
    assert predicate()


def _write_artwork(path, color):
    image = QImage(180, 180, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "PNG")
    return path


@pytest.mark.parametrize("change", ["evict", "invalidate"])
def test_returning_to_tracks_recovers_shared_cache_artwork(qapp, tmp_path, change):
    path = _write_artwork(tmp_path / "track.png", "red")
    cache = ThumbnailCache(max_bytes=168 * 168 * 4, max_workers=1)
    view = TrackTableView(thumbnail_cache=cache)
    view.set_tracks([TrackRow(7, "Synthetic", cover_path=str(path))])
    view.show()
    try:
        _wait_for(qapp, lambda: view.thumbnail_for_track(7) is not None)
        key = view._thumbnail_bindings[7][2]
        generation = cache.generation
        view.hide()
        if change == "evict":
            # Simulate an album/artist browser using the same bounded cache.
            other = _write_artwork(tmp_path / "browser.png", "blue")
            cache.request(other, 168, 1.0)
            _wait_for(qapp, lambda: cache.pending_count == 0)
            assert cache.stats.evictions > 0
        else:
            _write_artwork(path, "green")
            assert cache.invalidate_source(path) == 1
        assert cache.peek(key) is None
        assert cache.generation == generation  # No generation change rescues it.
        view.show()
        _wait_for(qapp, lambda: view.thumbnail_for_track(7) is not None)
        pixmap = view.thumbnail_for_track(7)
        assert pixmap.toImage().pixelColor(0, 0) == QColor("green" if change == "invalidate" else "red")
        assert view._thumbnail_bindings[7][2] == key
    finally:
        view.close()
        cache.close()


def test_visible_artwork_requests_coalesce_and_recover_invalidated_pending_decode(qapp, tmp_path, monkeypatch):
    from music_vault.ui import thumbnail_cache as module

    path = _write_artwork(tmp_path / "pending.png", "green")
    decode = module._decode_thumbnail
    release = threading.Event()

    def delayed(key):
        release.wait(3)
        return decode(key)

    monkeypatch.setattr(module, "_decode_thumbnail", delayed)
    cache = ThumbnailCache(max_workers=1)
    view = TrackTableView(thumbnail_cache=cache)
    view.set_tracks([TrackRow(7, "Synthetic", cover_path=str(path))])
    view.show()
    try:
        qapp.processEvents()
        for _ in range(5):
            view._request_visible_artwork()
        assert cache.pending_count == 1
        assert cache.stats.misses == 1
        assert cache.stats.coalesced >= 4
        view.hide()
        cache.invalidate_source(path)
        assert cache.pending_count == 0
        view.show()
        qapp.processEvents()
        assert cache.pending_count == 1
        assert cache.stats.misses == 2
        release.set()
        _wait_for(qapp, lambda: view.thumbnail_for_track(7) is not None)
        assert cache.stats.decodes == 1  # Invalidated old completion is ignored.
    finally:
        release.set()
        view.close()
        cache.close()


def test_failed_artwork_does_not_retry_on_resize_or_scroll_but_recovers_on_return(qapp):
    cache = FakeCache()
    view = TrackTableView(thumbnail_cache=cache)
    view.set_tracks([TrackRow(7, "Synthetic", cover_path="missing.png")])
    view.show()
    qapp.processEvents()
    key = view._thumbnail_bindings[7][2]
    cache.thumbnail_failed.emit(key, "missing", cache.generation)
    before = len(cache.requests)
    for _ in range(5):
        view._request_visible_artwork()
        qapp.processEvents()
    assert len(cache.requests) == before
    assert view._thumbnail_failures == {key}
    view.hide()
    view.show()
    qapp.processEvents()
    assert len(cache.requests) > before
    assert not view._thumbnail_failures
    pixmap = QPixmap(42, 42)
    pixmap.fill(QColor("blue"))
    cache.pixmaps[key] = pixmap
    cache.thumbnail_ready.emit(key, pixmap, cache.generation)
    assert view.thumbnail_for_track(7) is pixmap
    view.close()


def test_delegate_paints_literal_metadata_and_now_playing_without_loading(qapp):
    cache = FakeCache()
    view = TrackTableView(thumbnail_cache=cache)
    view.set_tracks([TrackRow(1, "<b>Literal & 夜</b>", "Artist")])
    view.set_now_playing(1)
    image = QImage(600, 54, QImage.Format.Format_ARGB32)
    image.fill(QColor("black"))
    painter = QPainter(image)
    option = QStyleOptionViewItem()
    option.rect = image.rect()
    option.widget = view
    view.itemDelegate().paint(painter, option, view.proxy_model.index(0, 0))
    painter.end()
    assert cache.requests == []
    assert not image.isNull()
    view.close()
