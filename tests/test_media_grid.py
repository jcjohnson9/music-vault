from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QWidget

from music_vault.ui.media_grid import (
    MediaCardDelegate,
    MediaFilterProxyModel,
    MediaGridModel,
    MediaGridState,
    MediaGridView,
    MediaImageState,
    MediaItem,
    MediaKind,
    MediaRole,
)


def _items(count: int = 12) -> list[MediaItem]:
    return [
        MediaItem(
            key=f"album:{index}",
            kind=MediaKind.ALBUM if index % 2 == 0 else MediaKind.ARTIST,
            title=f"Synthetic Collection {index:03d}",
            subtitle=("Northbound Ensemble" if index % 3 else "Quiet Current"),
            image_state=MediaImageState.LOADING if index == 1 else MediaImageState.MISSING,
        )
        for index in range(count)
    ]


def test_media_model_roles_updates_and_path_free_tooltip():
    path = r"C:\private\cover.png"
    model = MediaGridModel(
        [
            MediaItem(
                "album:stable",
                MediaKind.ALBUM,
                "A Very Long Album",
                "Synthetic Artist • 9 tracks",
                artwork_path=path,
            )
        ]
    )
    index = model.index(0, 0)
    assert model.rowCount() == 1
    assert index.data(int(MediaRole.KEY)) == "album:stable"
    assert index.data(int(MediaRole.KIND)) == "album"
    assert index.data(int(MediaRole.ARTWORK_PATH)) == path
    assert path not in index.data(Qt.ItemDataRole.ToolTipRole)
    assert index.data(Qt.ItemDataRole.AccessibleTextRole).startswith("Album:")
    assert model.item_for_key("album:stable").title == "A Very Long Album"
    assert model.replace_item("album:stable", image_state=MediaImageState.READY)
    assert index.data(int(MediaRole.IMAGE_STATE)) == "ready"


def test_media_model_rejects_duplicate_keys():
    item = MediaItem("artist:same", MediaKind.ARTIST, "Same")
    model = MediaGridModel()
    try:
        model.set_items([item, item])
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate stable keys were accepted")


def test_artwork_replacement_drops_stale_thumbnail_binding(qapp):
    class FakeCache:
        @staticmethod
        def peek(key):
            if key != "old-thumbnail":
                return None
            from PySide6.QtGui import QPixmap

            pixmap = QPixmap(10, 10)
            pixmap.fill(QColor("#1DB954"))
            return pixmap

    model = MediaGridModel(
        [MediaItem("artist:stable", MediaKind.ARTIST, "Artist", artwork_path="old.png")]
    )
    model._thumbnail_cache = FakeCache()
    model.bind_thumbnail("artist:stable", "old-thumbnail")
    index = model.index(0, 0)
    assert index.data(int(MediaRole.THUMBNAIL)) is not None

    assert model.replace_item("artist:stable", artwork_path="new.png")
    assert index.data(int(MediaRole.THUMBNAIL)) is None


def test_proxy_filters_title_and_subtitle_without_resetting_source():
    source = MediaGridModel(_items(20))
    resets: list[bool] = []
    source.modelReset.connect(lambda: resets.append(True))
    proxy = MediaFilterProxyModel()
    proxy.setSourceModel(source)

    proxy.set_filter_text("quiet current")
    assert 0 < proxy.rowCount() < source.rowCount()
    assert all(
        "quiet current" in str(proxy.index(row, 0).data(int(MediaRole.SUBTITLE))).casefold()
        for row in range(proxy.rowCount())
    )
    assert resets == []
    proxy.set_filter_text("")
    assert proxy.rowCount() == source.rowCount()


def test_grid_has_no_per_item_widgets_and_reports_near_visible_keys(qapp):
    source = MediaGridModel(_items(500))
    proxy = MediaFilterProxyModel()
    proxy.setSourceModel(source)
    view = MediaGridView()
    view.resize(820, 520)
    view.setModel(proxy)
    view.show()
    qapp.processEvents()

    keys = view.visible_item_keys()
    assert 0 < len(keys) < source.rowCount() // 4
    assert keys[0] == "album:0"
    assert all(view.indexWidget(proxy.index(row, 0)) is None for row in range(proxy.rowCount()))
    assert len(view.findChildren(QWidget)) < 20

    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    qapp.processEvents()
    later = view.visible_item_keys()
    assert later
    assert later != keys
    view.close()


def test_same_key_model_reset_reissues_visible_requests(qapp):
    source = MediaGridModel(_items(40))
    view = MediaGridView()
    view.resize(820, 520)
    view.setModel(source)
    emitted: list[tuple[str, ...]] = []
    view.visible_items_changed.connect(emitted.append)
    view.show()
    QTest.qWait(30)
    qapp.processEvents()
    assert emitted
    first = emitted[-1]

    source.set_items(_items(40))
    QTest.qWait(30)
    qapp.processEvents()
    assert len(emitted) >= 2
    assert emitted[-1] == first
    view.close()


def test_grid_keyboard_activation_and_context_signal(qapp):
    source = MediaGridModel(_items(4))
    view = MediaGridView()
    view.resize(440, 300)
    view.setModel(source)
    view.show()
    qapp.processEvents()
    view.setCurrentIndex(source.index(1, 0))
    opened: list[str] = []
    context: list[tuple[str, object]] = []
    view.item_opened.connect(opened.append)
    view.item_context_requested.connect(lambda key, point: context.append((key, point)))

    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert opened == ["album:1"]

    rect = view.visualRect(source.index(0, 0))
    assert rect.isValid()
    view._context_index(rect.center())
    assert context[0][0] == "album:0"
    view.close()


def _paint_item(model: MediaGridModel, row: int, qapp) -> QImage:
    image = QImage(200, 248, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#06080C"))
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 200, 248)
    option.state = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Active
    option.widget = None
    painter = QPainter(image)
    MediaCardDelegate().paint(painter, option, model.index(row, 0))
    painter.end()
    return image


def test_delegate_distinguishes_album_square_and_artist_portrait(qapp):
    model = MediaGridModel(
        [
            MediaItem("album:1", MediaKind.ALBUM, "Album", "4 tracks"),
            MediaItem("artist:1", MediaKind.ARTIST, "Artist", "8 tracks"),
        ]
    )
    album = _paint_item(model, 0, qapp)
    artist = _paint_item(model, 1, qapp)
    assert not album.isNull() and not artist.isNull()
    assert album != artist
    # Near the artwork's upper-left, rounded-square fill remains while the
    # circular artist crop is outside its portrait mask.
    assert album.pixelColor(32, 32) != artist.pixelColor(32, 32)


def test_grid_empty_loading_and_focus_states_render(qapp):
    view = MediaGridView()
    view.resize(600, 400)
    view.setModel(MediaGridModel())
    view.show()
    for state, title in (
        (MediaGridState.EMPTY, "No albums yet"),
        (MediaGridState.LOADING, "Loading artists"),
        (MediaGridState.ERROR, "Browser unavailable"),
    ):
        view.set_view_state(state, title, "Synthetic state", "artist-unknown")
        qapp.processEvents()
        shot = view.viewport().grab()
        assert not shot.isNull()
        assert view.view_state() == state
        assert view.visible_item_keys() == ()
    view.close()
