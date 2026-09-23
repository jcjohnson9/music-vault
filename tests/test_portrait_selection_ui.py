"""Local portrait selection uses durable identity and revision, not a name."""
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QMessageBox

from music_vault.metadata.artist_images import (
    ArtistImageResult, ArtistImageStatus, SyntheticArtistImageProvider,
)
from test_ui_system import isolated_ui_window, _wait_for_browser_rows  # noqa: F401


def portrait_fixture(fixture, qapp):
    window = fixture.window
    window.show_artist_browser()
    _wait_for_browser_rows(qapp, window.artist_browser_model)
    summary = next(s for s in window._browser_summary_maps["artists"].values() if s.key.normalized_name)
    identity = window.artist_image_identity(summary)
    result = window.artist_image_cache.store(SyntheticArtistImageProvider().resolve(identity))
    window._artist_image_result(summary.browser_key, result)
    return window, summary, identity, result


@pytest.mark.parametrize("field,value", [
    ("canonical_artist_id", 999999), ("discogs_artist_id", "98765"),
    ("musicbrainz_artist_id", "12345678-1234-4123-8123-123456789abc"),
])
def test_same_name_wrong_identity_cannot_replace_or_clear_pending(isolated_ui_window, qapp, field, value):
    window, summary, identity, result = portrait_fixture(isolated_ui_window, qapp)
    key = summary.browser_key
    before = window.artist_browser_model.item_for_key(key)
    window._pending_artist_image_keys.add(key)
    wrong = replace(identity, **{field: value})
    window._artist_image_result(key, ArtistImageResult(ArtistImageStatus.NO_MATCH, wrong))
    assert window.artist_browser_model.item_for_key(key) == before
    assert key in window._pending_artist_image_keys


def test_negative_callback_retains_valid_local_selection(isolated_ui_window, qapp):
    window, summary, identity, result = portrait_fixture(isolated_ui_window, qapp)
    window._artist_image_result(summary.browser_key, ArtistImageResult(ArtistImageStatus.TEMPORARY_ERROR, identity))
    item = window.artist_browser_model.item_for_key(summary.browser_key)
    assert item.has_cached_image and item.artwork_path == str(result.cache_file)


def test_summary_identity_refresh_retires_pending_generation(isolated_ui_window, qapp, monkeypatch):
    window, summary, identity, result = portrait_fixture(isolated_ui_window, qapp)
    key = summary.browser_key
    cancelled = []
    monkeypatch.setattr(window.artist_image_service, "cancel_all", lambda: cancelled.append(True))
    window._pending_artist_image_keys.add(key)
    revised = replace(summary, discogs_artist_id="98765")
    summaries = tuple(revised if value.browser_key == key else value
                      for value in window._browser_summary_maps["artists"].values())
    window._apply_browser_summaries("artists", summaries, object())
    assert cancelled == [True]
    assert key not in window._pending_artist_image_keys
    # A newly submitted request is not consumed by the older identity's result.
    window._pending_artist_image_keys.add(key)
    window._artist_image_result(key, result)
    assert key in window._pending_artist_image_keys
    window._artist_image_result(key, ArtistImageResult(
        ArtistImageStatus.NO_MATCH, window.artist_image_identity(revised),
    ))
    assert key not in window._pending_artist_image_keys


def test_pin_is_local_revision_bound_and_clear_preserves_it(isolated_ui_window, qapp, monkeypatch):
    window, summary, identity, result = portrait_fixture(isolated_ui_window, qapp)
    revision = window.artist_image_cache.selection(identity).revision
    monkeypatch.setattr(window.artist_image_service, "request", lambda *_a, **_k: pytest.fail("provider request"))
    window.change_artist_photo_selection(summary.browser_key, "pin", identity, revision)
    assert window.artist_image_cache.selection(identity).pinned
    window.change_artist_photo_selection(summary.browser_key, "unpin", identity, revision)
    assert window.artist_image_cache.selection(identity).pinned
    assert "Reopen the menu" in window.statusBar().currentMessage()
    window.clear_cached_artist_photo(summary.browser_key)
    assert window.artist_browser_model.item_for_key(summary.browser_key).artwork_path == str(result.cache_file)
    assert window.artist_image_cache.selection(identity).pinned
    revision = window.artist_image_cache.selection(identity).revision
    window.change_artist_photo_selection(summary.browser_key, "unpin", identity, revision)
    assert not window.artist_image_cache.selection(identity).pinned


def test_restore_updates_browser_without_provider_or_database_edit(isolated_ui_window, qapp, monkeypatch):
    window, summary, identity, first = portrait_fixture(isolated_ui_window, qapp)
    generated = SyntheticArtistImageProvider().resolve(replace(identity, display_name="Different synthetic portrait", normalized_key="different synthetic portrait"))
    second = window.artist_image_cache.store(replace(generated, identity=identity), replace_existing=True)
    assert second.cache_file != first.cache_file
    window._artist_image_result(summary.browser_key, second)
    before = list(window.db.conn.iterdump())
    monkeypatch.setattr(window.artist_image_service, "request", lambda *_a, **_k: pytest.fail("provider request"))
    selection = window.artist_image_cache.selection(identity)
    assert selection.can_restore
    window.change_artist_photo_selection(summary.browser_key, "restore", identity, selection.revision)
    assert window.artist_browser_model.item_for_key(summary.browser_key).artwork_path == str(first.cache_file)
    assert first.cache_file.exists() and second.cache_file.exists()
    assert list(window.db.conn.iterdump()) == before


def test_global_clear_retains_visible_pinned_selection(isolated_ui_window, qapp, monkeypatch):
    window, summary, identity, result = portrait_fixture(isolated_ui_window, qapp)
    window.artist_image_cache.set_pinned(identity, True, expected_revision=window.artist_image_cache.selection(identity).revision)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *_a, **_k: QMessageBox.Ok)
    window.clear_artist_image_cache()
    assert result.cache_file.exists()
    assert window.artist_browser_model.item_for_key(summary.browser_key).artwork_path == str(result.cache_file)
