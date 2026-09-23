"""Synthetic portrait selection authority, preservation and reversible refresh."""
from dataclasses import replace
from concurrent.futures import Future
import json
import threading

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from music_vault.metadata.artist_images import (
    ArtistIdentity, ArtistImageCache, ArtistImageContentError, ArtistImageResult,
    ArtistImageService, ArtistImageStatus, StalePortraitSelection,
)


MBID = "11111111-1111-4111-8111-111111111111"
OTHER_MBID = "22222222-2222-4222-8222-222222222222"


def payload(color, size=360):
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly) and image.save(buffer, "PNG")
    return bytes(data)


def portrait(identity, color="red", **kwargs):
    return ArtistImageResult(ArtistImageStatus.RESOLVED, identity, content_type="image/png",
                             image_bytes=payload(color), attribution_text="Synthetic attribution", **kwargs)


@pytest.fixture
def cached(tmp_path):
    cache = ArtistImageCache(tmp_path / "portraits")
    identity = ArtistIdentity.from_display_name("Synthetic Artist", canonical_artist_id=1, musicbrainz_artist_id=MBID)
    first = cache.store(portrait(identity))
    return cache, identity, first


def resolve(cache, identity, provider):
    service = ArtistImageService(provider, cache)
    try:
        return service._resolve_job(identity, force=True, network_enabled=True, cancel_event=threading.Event(), generation=0)
    finally:
        service.shutdown()


@pytest.mark.parametrize("status", [ArtistImageStatus.NO_MATCH, ArtistImageStatus.AMBIGUOUS,
                                    ArtistImageStatus.UNAVAILABLE, ArtistImageStatus.TEMPORARY_ERROR])
def test_negative_force_refresh_preserves_last_good_and_manifest(cached, status):
    cache, identity, first = cached
    before = cache.index_path.read_bytes()
    provider = type("Provider", (), {"resolve": lambda *_: ArtistImageResult(status, identity)})()
    result = resolve(cache, identity, provider)
    assert result.resolved and result.cache_file == first.cache_file and result.refresh_error
    assert result.attribution_text == first.attribution_text
    assert cache.index_path.read_bytes() == before


@pytest.mark.parametrize("failure", ["exception", "invalid", "small", "identity", "claimed_id"])
def test_refresh_failure_returns_last_good(cached, failure):
    cache, identity, first = cached
    before = cache.index_path.read_bytes()
    def fail(*_args):
        if failure == "exception":
            raise RuntimeError("synthetic failure")
        if failure == "invalid":
            return replace(portrait(identity), image_bytes=b"not an image")
        if failure == "small":
            return replace(portrait(identity), image_bytes=payload("blue", 100))
        if failure == "claimed_id":
            return portrait(identity, "blue", musicbrainz_artist_id=OTHER_MBID)
        other = ArtistIdentity.from_display_name(identity.display_name, canonical_artist_id=2, musicbrainz_artist_id=OTHER_MBID)
        return portrait(other, "blue")
    result = resolve(cache, identity, type("Provider", (), {"resolve": fail})())
    assert result.resolved and result.cache_file == first.cache_file
    assert cache.index_path.read_bytes() == before


@pytest.mark.parametrize("missing", [False, True])
def test_pin_survives_force_refresh_repair_and_both_clear_modes_without_provider(cached, missing):
    cache, identity, first = cached
    cache.set_pinned(identity, True, expected_revision=cache.selection(identity).revision)
    if missing:
        first.cache_file.unlink()
    before = cache.index_path.read_bytes()
    assert cache.lookup(identity, repair=True).pinned
    factory_calls = []
    def factory():
        factory_calls.append(True)
        raise AssertionError("provider must not be constructed")
    service = ArtistImageService(None, cache, provider_factory=factory)
    try:
        result = service._resolve_job(identity, force=True, network_enabled=True, cancel_event=threading.Event(), generation=0)
    finally:
        service.shutdown()
    assert result.pinned and not factory_calls
    assert cache.index_path.read_bytes() == before
    cache.clear(identity)
    cache.clear()
    assert cache.selection(identity).pinned
    assert cache.lookup(identity, repair=False).pinned
    if not missing:
        assert first.cache_file.is_file()


def test_missing_pin_can_be_explicitly_unpinned_without_committed_failure(cached):
    cache, identity, first = cached
    cache.set_pinned(identity, True, expected_revision=cache.selection(identity).revision)
    first.cache_file.unlink()
    result = cache.set_pinned(identity, False, expected_revision=cache.selection(identity).revision)
    assert result.status is ArtistImageStatus.UNAVAILABLE and not result.pinned
    assert not cache.selection(identity).pinned
    refreshed = cache.store(portrait(identity, "blue"), replace_existing=True)
    assert refreshed.resolved


def test_one_previous_selection_is_reversible_without_recursive_history(cached):
    cache, identity, first = cached
    second = cache.store(portrait(identity, "blue"), replace_existing=True, expected_revision=cache.selection(identity).revision)
    assert second.cache_file != first.cache_file and first.cache_file.is_file()
    for expected in (first.cache_file, second.cache_file, first.cache_file):
        selection = cache.selection(identity)
        assert selection.can_restore
        assert cache.restore_previous(identity, expected_revision=selection.revision).cache_file == expected
        record = cache._load()["entries"][cache._entry_key(identity)]
        assert "previous_selection" not in record["previous_selection"]


def test_stale_pin_restore_and_store_cannot_overwrite_newer_selection(cached):
    cache, identity, first = cached
    cache.store(portrait(identity, "blue"), replace_existing=True)
    stale = cache.selection(identity).revision
    cache.set_pinned(identity, True, expected_revision=stale)
    before = cache.index_path.read_bytes()
    with pytest.raises(StalePortraitSelection):
        cache.set_pinned(identity, False, expected_revision=stale)
    with pytest.raises(StalePortraitSelection):
        cache.restore_previous(identity, expected_revision=stale)
    with pytest.raises(StalePortraitSelection):
        cache.store(portrait(identity, "green"), replace_existing=True, expected_revision=stale)
    assert cache.index_path.read_bytes() == before


def test_pin_during_provider_work_defeats_late_refresh(cached):
    cache, identity, first = cached
    def resolve_and_pin(*_args):
        cache.set_pinned(identity, True, expected_revision=cache.selection(identity).revision)
        return portrait(identity, "blue")
    result = resolve(cache, identity, type("Provider", (), {"resolve": resolve_and_pin})())
    assert result.pinned and result.cache_file == first.cache_file


@pytest.mark.parametrize("action", ["store", "pin", "restore"])
def test_manifest_failure_preserves_selected_record_and_bytes(cached, monkeypatch, action):
    cache, identity, first = cached
    if action == "restore":
        cache.store(portrait(identity, "blue"), replace_existing=True)
    before = cache.index_path.read_bytes()
    selected = cache.selection(identity)
    def fail():
        raise OSError("injected manifest failure")
    monkeypatch.setattr(cache, "_write_manifest", fail)
    with pytest.raises(OSError, match="injected"):
        if action == "store":
            cache.store(portrait(identity, "green"), replace_existing=True)
        elif action == "pin":
            cache.set_pinned(identity, True, expected_revision=selected.revision)
        else:
            cache.restore_previous(identity, expected_revision=selected.revision)
    assert cache.index_path.read_bytes() == before
    assert cache.selection(identity).revision == selected.revision
    assert cache.selection(identity).result.cache_file == selected.result.cache_file


def test_clear_unpinned_invalidates_inflight_empty_revision(cached):
    cache, identity, first = cached
    cache.clear(identity)
    revision = cache.selection(identity).revision
    cache.clear(identity)
    with pytest.raises(StalePortraitSelection):
        cache.store(portrait(identity, "blue"), expected_revision=revision)


def test_new_known_provider_identity_cannot_claim_same_name_old_portrait(cached):
    cache, identity, first = cached
    other = replace(identity, musicbrainz_artist_id=OTHER_MBID)
    assert cache.lookup(other) is None
    assert first.cache_file.is_file()


def test_corrupt_previous_restore_fails_without_changing_selection(cached):
    cache, identity, first = cached
    cache.store(portrait(identity, "blue"), replace_existing=True)
    first.cache_file.write_bytes(b"synthetic corrupt fixture")
    before = cache.index_path.read_bytes()
    with pytest.raises(ArtistImageContentError):
        cache.restore_previous(identity, expected_revision=cache.selection(identity).revision)
    assert cache.index_path.read_bytes() == before


@pytest.mark.parametrize("all_cache", [False, True])
def test_unpin_rekeyed_legacy_selection_allows_clear_without_resurrection(tmp_path, all_cache):
    cache = ArtistImageCache(tmp_path / "portraits")
    legacy = ArtistIdentity.from_display_name("Legacy Artist")
    identity = ArtistIdentity.from_display_name("Legacy Artist", canonical_artist_id=1, musicbrainz_artist_id=MBID)
    first = cache.store(portrait(legacy, pinned=True))
    assert cache.rekey(legacy, identity)
    cache.set_pinned(identity, False, expected_revision=cache.selection(identity).revision)
    assert not cache.lookup(identity).pinned
    cache.clear(None if all_cache else identity)
    assert cache.lookup(identity, repair=False) is None
    assert not first.cache_file.exists()


def test_unpin_and_global_clear_preserve_distinct_same_name_pin_and_shared_bytes(tmp_path):
    cache = ArtistImageCache(tmp_path / "portraits")
    legacy = ArtistIdentity.from_display_name("Shared Name")
    identity = ArtistIdentity.from_display_name("Shared Name", canonical_artist_id=1, musicbrainz_artist_id=MBID)
    other = ArtistIdentity.from_display_name("Shared Name", canonical_artist_id=2, musicbrainz_artist_id=OTHER_MBID)
    cache.store(portrait(legacy, pinned=True))
    cache.rekey(legacy, identity)
    other_result = cache.store(portrait(other, pinned=True))
    cache.set_pinned(identity, False, expected_revision=cache.selection(identity).revision)
    cache.clear()
    assert cache.lookup(identity, repair=False) is None
    assert cache.lookup(other, repair=False).pinned and other_result.cache_file.is_file()


@pytest.mark.parametrize("kind", ["manual", "MANUAL", "Pinned", "MANUAL_PINNED", "  manual  ", "\tPinned\n"])
def test_provider_response_cannot_create_a_user_pin(cached, kind):
    cache, identity, first = cached
    provider = type("Provider", (), {"resolve": lambda *_: portrait(identity, "blue", pinned=True, portrait_kind=kind)})()
    result = resolve(cache, identity, provider)
    assert result.resolved and not result.pinned
    assert not cache.selection(identity).pinned


@pytest.mark.parametrize("targeted_first", [False, True])
def test_clear_unpinned_alias_preserves_other_owner_and_does_not_resurrect(tmp_path, targeted_first):
    cache = ArtistImageCache(tmp_path / "portraits")
    legacy = ArtistIdentity.from_display_name("Shared Legacy")
    identity = ArtistIdentity.from_display_name("Shared Legacy", canonical_artist_id=1)
    other = ArtistIdentity.from_display_name("Shared Legacy", canonical_artist_id=2)
    first = cache.store(portrait(legacy, pinned=True))
    legacy_key = cache._entry_key(legacy)
    # Historical alias-only manifest: two canonical subjects reference one pin.
    for subject in (identity, other):
        cache._load()["aliases"][cache._entry_key(subject)] = [legacy_key]
    cache._write_manifest()
    cache.set_pinned(identity, False, expected_revision=cache.selection(identity).revision)
    cache.clear(identity if targeted_first else None)
    assert cache.lookup(identity, repair=False) is None
    assert cache.lookup(other, repair=False).pinned
    assert first.cache_file.is_file()
    cache.clear()
    assert cache.lookup(identity, repair=False) is None
    assert cache.lookup(other, repair=False).pinned
    assert first.cache_file.is_file()
    reopened = ArtistImageCache(cache.root)
    assert reopened.lookup(identity, repair=False) is None
    assert reopened.lookup(other, repair=False).pinned


def test_old_completion_cannot_remove_replacement_pending_request(cached):
    cache, identity, first = cached
    service = ArtistImageService(object(), cache)
    service._executor.shutdown(wait=False, cancel_futures=True)
    class PendingExecutor:
        def submit(self, *args, **kwargs):
            return Future()
        def shutdown(self, **kwargs):
            pass
    service._executor = PendingExecutor()
    old_deliveries, new_deliveries = [], []
    try:
        assert service.request(identity, old_deliveries.append, network_enabled=False)
        key = next(iter(service._pending))
        old_generation = service._generation
        service.cancel_all()
        assert service.request(identity, new_deliveries.append, network_enabled=False)
        replacement = service._pending[key]
        service._deliver(key, first, old_generation)
        assert service._pending[key] is replacement
        assert old_deliveries == new_deliveries == []
        service._deliver(key, first, service._generation)
        assert service.pending_count == 0
        assert len(new_deliveries) == 1 and new_deliveries[0].resolved
        assert old_deliveries == []
    finally:
        service.shutdown()
