from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from music_vault.core.library_browser import load_album_summaries, load_artist_summaries
from music_vault.ui.media_grid import (
    MediaFilterProxyModel,
    MediaGridModel,
    MediaGridState,
    MediaGridView,
    MediaImageState,
    MediaItem,
    MediaKind,
)
from music_vault.ui.review import (
    DEFAULT_REVIEW_SCENES,
    SCENE_LABELS,
    browser_review_metrics,
    finalize_review_scene,
    prepare_review_scene,
    review_scene_ready,
    sanitize_review_paths,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _capture_module():
    path = PROJECT_ROOT / "tools" / "dev" / "capture_ui_review.py"
    spec = importlib.util.spec_from_file_location("music_vault_capture_ui_review", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch5_review_scenes_are_explicit_and_labeled():
    assert "albums" in DEFAULT_REVIEW_SCENES
    assert "artists_fetch_disabled" in DEFAULT_REVIEW_SCENES
    assert "artists_fetch_enabled" in DEFAULT_REVIEW_SCENES
    assert SCENE_LABELS["artists_fetch_disabled"] != SCENE_LABELS["artists_fetch_enabled"]


def test_synthetic_seed_has_large_safe_browser_dataset(tmp_path):
    tool = _capture_module()
    runtime = tmp_path / "synthetic_runtime"
    (runtime / "data").mkdir(parents=True)
    (runtime / "profile").mkdir()
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# marker\n", encoding="utf-8")

    result = tool.seed_synthetic_runtime(PROJECT_ROOT, runtime)
    database = runtime / "data" / "music_vault.sqlite3"
    albums = load_album_summaries(database)
    artists = load_artist_summaries(database)
    config = json.loads(
        (runtime / "data" / "music_vault_config.json").read_text(encoding="utf-8")
    )

    assert result["track_count"] == 300
    assert result["synthetic_mp3_count"] == 1
    assert len(albums) == 100
    assert len(artists) == 200
    assert config["artist_image_fetch_enabled"] is False
    assert not (runtime / "data" / "youtube_api_key.txt").exists()
    assert sum(summary.album_title == "A Shared Synthetic Album Title" for summary in albums) == 2
    assert any(summary.representative_cover_path is None for summary in albums)
    assert any(len(summary.album_title) > 60 for summary in albums)

    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    finally:
        connection.close()


def _browser_window(qapp):
    items = [
        MediaItem(
            f"artist:{index:03d}",
            MediaKind.ARTIST,
            "Distant Loading Artist" if index == 2 else f"Synthetic Artist {index:03d}",
            f"{index + 1} tracks",
            image_state=MediaImageState.MISSING,
        )
        for index in range(60)
    ]
    model = MediaGridModel(items)
    proxy = MediaFilterProxyModel()
    proxy.setSourceModel(model)
    view = MediaGridView()
    view.resize(820, 500)
    view.setModel(proxy)
    view.set_view_state(MediaGridState.CONTENT)
    view.show()
    qapp.processEvents()
    return SimpleNamespace(
        artist_browser_model=model,
        artist_browser_proxy=proxy,
        browser_view=view,
        config={"artist_image_fetch_enabled": False},
        thumbnail_cache=SimpleNamespace(
            pending_count=0,
            stats=SimpleNamespace(
                requests=8,
                hits=2,
                misses=6,
                coalesced=1,
                decodes=6,
                failures=0,
                evictions=0,
                entries=6,
                bytes_used=1024,
                pending=0,
            ),
        ),
        artist_image_service=SimpleNamespace(
            pending_count=0,
            provider=SimpleNamespace(calls=[]),
        ),
    )


def test_async_readiness_and_metrics_are_path_and_name_free(monkeypatch, qapp):
    monkeypatch.setenv("MUSIC_VAULT_ARTIST_IMAGE_PROVIDER", "synthetic")
    window = _browser_window(qapp)
    assert review_scene_ready(window, "artists_fetch_disabled")
    metrics = browser_review_metrics(window, "artists_fetch_disabled")
    assert metrics is not None
    assert metrics["model_rows"] == 60
    assert metrics["per_item_widget_count"] == 0
    assert 0 < metrics["visible_key_count"] < 60
    assert len(metrics["visible_key_sha256"]) == 64
    assert "artist:000" not in json.dumps(metrics)
    assert metrics["public_provider_call_count"] == 0
    assert metrics["synthetic_provider_call_count"] == 0
    window.browser_view.close()


def test_enabled_review_finalizer_preserves_one_loading_artist(monkeypatch, qapp):
    monkeypatch.setenv("MUSIC_VAULT_ARTIST_IMAGE_PROVIDER", "synthetic")
    window = _browser_window(qapp)
    window.config["artist_image_fetch_enabled"] = True
    finalize_review_scene(window, "artists_fetch_enabled")
    loading = window.artist_browser_model.item_for_key("artist:002")
    assert loading is not None
    assert loading.image_state is MediaImageState.LOADING
    metrics = browser_review_metrics(window, "artists_fetch_enabled")
    assert metrics["artist_fetch_enabled"] is True
    assert metrics["image_states"]["loading"] == 1
    window.browser_view.close()


def test_prepare_artist_scenes_changes_only_in_memory_consent():
    class Pages:
        def setCurrentWidget(self, page):
            self.current = page

    class Window:
        def __init__(self):
            self.pages = Pages()
            self.library_page = object()
            self.config = {"artist_image_fetch_enabled": False}
            self.current_view_kind = "library"
            self.current_playlist_id = None
            self.current_playlist_name = "Library"
            self.calls = 0

        def show_artist_browser(self):
            self.calls += 1

    window = Window()
    prepare_review_scene(window, "artists_fetch_enabled")
    assert window.config["artist_image_fetch_enabled"] is True
    prepare_review_scene(window, "artists_fetch_disabled")
    assert window.config["artist_image_fetch_enabled"] is False
    assert window.calls == 2


def test_review_sanitizes_artist_cache_status_path():
    class Label:
        def __init__(self):
            self.value = "Cache Folder: C:\\Users\\" + "private\\artist_images"

        def setText(self, value):
            self.value = value

    label = Label()
    sanitize_review_paths(SimpleNamespace(artist_images_status=label))
    assert "<synthetic-runtime>" in label.value
    assert "C:\\Users" not in label.value
