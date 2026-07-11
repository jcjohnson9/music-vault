from __future__ import annotations

import inspect
from types import SimpleNamespace

from music_vault.app import MusicVaultWindow
from music_vault.core.library_browser import BrowserInvalidationPlan


def test_real_library_mutations_are_wired_to_browser_invalidation():
    expected = {
        "import_music_folder": "BrowserInvalidationReason.IMPORT_FOLDER",
        "youtube_sync_finished": "BrowserInvalidationReason.YOUTUBE_IMPORT",
        "enrich_selected": "BrowserInvalidationReason.METADATA_ENRICHMENT",
        "remove_missing_tracks": "BrowserInvalidationReason.REMOVE_MISSING",
        "refresh_artwork": "BrowserInvalidationReason.ARTWORK_REFRESH",
    }
    for method_name, reason in expected.items():
        source = inspect.getsource(getattr(MusicVaultWindow, method_name))
        assert "invalidate_browser_data" in source, method_name
        assert reason in source, method_name


def test_playback_queue_volume_and_status_do_not_invalidate_browser_summaries():
    for method_name in (
        "on_volume_changed",
        "play_track_by_id",
        "play_next",
        "queue_selected_next",
        "on_playback_state_changed",
        "write_app_status",
    ):
        source = inspect.getsource(getattr(MusicVaultWindow, method_name))
        assert "invalidate_browser_data" not in source, method_name


def test_album_artwork_invalidation_does_not_advance_global_artist_generation():
    invalidated_sources: list[str] = []

    class SummaryCache:
        @staticmethod
        def invalidate(_reason):
            return BrowserInvalidationPlan(album_thumbnails=True)

    class ThumbnailCache:
        @staticmethod
        def invalidate_source(source):
            invalidated_sources.append(source)

        @staticmethod
        def advance_generation():
            raise AssertionError("album-only invalidation must not suppress artist decodes")

    owner = SimpleNamespace(
        browser_summary_cache=SummaryCache(),
        browser_summary_loader=SimpleNamespace(invalidate=lambda _kind: None),
        _browser_model_revisions={"albums": object(), "artists": object()},
        album_browser_model=SimpleNamespace(
            items=lambda: (
                SimpleNamespace(artwork_path="first-cover.png"),
                SimpleNamespace(artwork_path=None),
            )
        ),
        artist_browser_model=SimpleNamespace(items=lambda: ()),
        thumbnail_cache=ThumbnailCache(),
    )

    MusicVaultWindow.invalidate_browser_data(owner, "artwork_refresh")

    assert invalidated_sources == ["first-cover.png"]
