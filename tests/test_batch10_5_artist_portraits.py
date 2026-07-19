from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from music_vault.metadata.artist_images import (
    ArtistIdentity,
    ArtistImageCache,
    ArtistImageContentError,
    ArtistImageResult,
    ArtistImageService,
    ArtistImageStatus,
    DiscogsArtistImageProvider,
    SyntheticArtistImageProvider,
    validate_image_payload,
)


MBID = "11111111-1111-4111-8111-111111111111"


def _png(width: int, height: int, color: str = "#257c58") -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


class _Catalogue:
    def __init__(self, images: list[dict]) -> None:
        self.images = images

    def get_artist(self, artist_id: str, **_kwargs):
        return {"id": artist_id, "name": "Portrait Unit", "images": self.images}


class _Transport:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.urls: list[str] = []

    def get_image(self, url: str):
        self.urls.append(url)
        return validate_image_payload(self.payloads[url], "image/png")


def _result(
    identity: ArtistIdentity,
    *,
    provider: str,
    kind: str,
    width: int,
    height: int,
    pinned: bool = False,
) -> ArtistImageResult:
    payload = _png(width, height)
    return ArtistImageResult(
        ArtistImageStatus.RESOLVED,
        identity,
        matched_artist_name=identity.display_name,
        musicbrainz_artist_id=identity.musicbrainz_artist_id,
        discogs_artist_id=identity.discogs_artist_id,
        image_provider=provider,
        content_type="image/png",
        width=width,
        height=height,
        portrait_kind=kind,
        pinned=pinned,
        image_bytes=payload,
    )


def test_discogs_full_size_uri_precedes_uri150_and_resource_url():
    full = "https://i.discogs.com/full.png"
    resource = "https://i.discogs.com/resource.png"
    thumbnail = "https://i.discogs.com/thumb.png"
    transport = _Transport({full: _png(640, 640)})
    provider = DiscogsArtistImageProvider(
        catalogue_provider=_Catalogue(
            [
                {
                    "type": "primary",
                    "uri": full,
                    "uri150": thumbnail,
                    "resource_url": resource,
                    "width": 640,
                    "height": 640,
                }
            ]
        ),
        transport=transport,
    )

    result = provider.resolve(
        ArtistIdentity.from_display_name("Portrait Unit", discogs_artist_id="71")
    )

    assert result.status is ArtistImageStatus.RESOLVED
    assert result.image_url == full
    assert transport.urls == [full]


def test_discogs_rejects_actual_thumbnail_dimensions_without_storing_it():
    misleading = "https://i.discogs.com/not-really-full.png"
    transport = _Transport({misleading: _png(150, 150)})
    provider = DiscogsArtistImageProvider(
        catalogue_provider=_Catalogue(
            [
                {
                    "type": "primary",
                    "uri": misleading,
                    "uri150": "https://i.discogs.com/thumb.png",
                    "width": 640,
                    "height": 640,
                }
            ]
        ),
        transport=transport,
    )

    result = provider.resolve(
        ArtistIdentity.from_display_name("Portrait Unit", discogs_artist_id="71")
    )

    assert result.status is ArtistImageStatus.NO_MATCH
    assert result.image_bytes is None
    assert result.error_code == "discogs_portrait_quality_rejected"


def test_new_cache_entries_reject_thumbnail_scale_portraits(tmp_path: Path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Small Portrait")
    with pytest.raises(ArtistImageContentError, match="portrait_dimensions_too_small"):
        cache.store(
            ArtistImageResult(
                ArtistImageStatus.RESOLVED,
                identity,
                content_type="image/png",
                image_bytes=_png(150, 150),
            )
        )
    assert not cache.index_path.exists()


def test_complementary_provider_id_finds_existing_musicbrainz_cache(tmp_path: Path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    musicbrainz_identity = ArtistIdentity.from_display_name(
        "Alias Portrait", musicbrainz_artist_id=MBID
    )
    stored = cache.store(
        _result(
            musicbrainz_identity,
            provider="Wikimedia Commons",
            kind="musicbrainz_wikimedia",
            width=512,
            height=512,
        )
    )
    combined = ArtistIdentity.from_display_name(
        "Alias Portrait",
        musicbrainz_artist_id=MBID,
        discogs_artist_id="71",
    )

    found = cache.lookup(combined)

    assert found is not None and found.resolved
    assert found.cache_file == stored.cache_file


def test_canonical_identity_can_find_name_only_legacy_entry(tmp_path: Path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    legacy = ArtistIdentity.from_display_name("Historical Portrait Name")
    stored = cache.store(SyntheticArtistImageProvider().resolve(legacy))
    canonical = ArtistIdentity.from_display_name(
        "Canonical Portrait Name",
        canonical_artist_id=42,
        historical_aliases=("Historical Portrait Name",),
    )

    found = cache.lookup(canonical)

    assert found is not None and found.cache_file == stored.cache_file


def test_offline_repair_selects_provider_priority_backs_up_and_deletes_nothing(
    tmp_path: Path,
):
    cache = ArtistImageCache(tmp_path / "artist_images")
    mb_identity = ArtistIdentity.from_display_name(
        "Priority Portrait", musicbrainz_artist_id=MBID
    )
    discogs_identity = ArtistIdentity.from_display_name(
        "Priority Portrait", discogs_artist_id="71"
    )
    mb = cache.store(
        _result(
            mb_identity,
            provider="Wikimedia Commons",
            kind="musicbrainz_wikimedia",
            width=400,
            height=400,
        )
    )
    discogs = cache.store(
        _result(
            discogs_identity,
            provider="Discogs",
            kind="discogs",
            width=800,
            height=800,
        )
    )
    original_index = cache.index_path.read_bytes()
    backup_path = tmp_path / "index.before-repair.json"
    canonical = ArtistIdentity.from_display_name(
        "Priority Portrait",
        canonical_artist_id=42,
        musicbrainz_artist_id=MBID,
        discogs_artist_id="71",
    )

    report = cache.repair_index(
        {canonical: (mb_identity, discogs_identity)},
        backup_path=backup_path,
    )

    assert report["changed_alias_count"] > 0
    assert backup_path.read_bytes() == original_index
    assert report["backup_sha256"] == hashlib.sha256(original_index).hexdigest()
    selected = cache.lookup(canonical)
    assert selected is not None and selected.cache_file == mb.cache_file
    assert mb.cache_file.is_file()
    assert discogs.cache_file.is_file()


@pytest.mark.parametrize(
    "rekey_order",
    (("discogs", "musicbrainz"), ("musicbrainz", "discogs")),
)
def test_rekey_preserves_all_resolved_entries_and_selects_best_in_any_order(
    rekey_order: tuple[str, str], tmp_path: Path
):
    cache = ArtistImageCache(tmp_path / "artist_images")
    mb_identity = ArtistIdentity.from_display_name(
        "Historical Portrait Name", musicbrainz_artist_id=MBID
    )
    discogs_identity = ArtistIdentity.from_display_name(
        "Historical Portrait Name", discogs_artist_id="71"
    )
    canonical = ArtistIdentity.from_display_name(
        "Canonical Portrait Name",
        canonical_artist_id=42,
        musicbrainz_artist_id=MBID,
        discogs_artist_id="71",
        historical_aliases=("Historical Portrait Name",),
    )
    mb = cache.store(
        _result(
            mb_identity,
            provider="Wikimedia Commons",
            kind="musicbrainz_wikimedia",
            width=400,
            height=400,
        )
    )
    discogs = cache.store(
        _result(
            discogs_identity,
            provider="Discogs",
            kind="discogs",
            width=800,
            height=800,
        )
    )
    identities = {"musicbrainz": mb_identity, "discogs": discogs_identity}

    for key in rekey_order:
        assert cache.rekey(identities[key], canonical)

    selected = cache.lookup(canonical)
    assert selected is not None and selected.cache_file == mb.cache_file
    manifest = json.loads(cache.index_path.read_text(encoding="utf-8"))
    assert len(manifest["entries"]) == 3
    assert {
        record["cache_file"] for record in manifest["entries"].values()
    } == {
        mb.cache_file.relative_to(cache.root).as_posix(),
        discogs.cache_file.relative_to(cache.root).as_posix(),
    }
    assert {path.name for path in cache.files_dir.iterdir()} == {
        mb.cache_file.name,
        discogs.cache_file.name,
    }

    report = cache.repair_index({canonical: (mb_identity, discogs_identity)})
    assert report["selected_group_count"] == 1
    assert cache.lookup(canonical).cache_file == mb.cache_file
    assert len(json.loads(cache.index_path.read_text(encoding="utf-8"))["entries"]) == 3


def test_cache_hit_prevents_lazy_provider_construction(tmp_path: Path, qapp):
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Already Cached")
    cache.store(SyntheticArtistImageProvider().resolve(identity))
    constructions: list[bool] = []

    def provider_factory():
        constructions.append(True)
        raise AssertionError("provider factory must not run for a valid cache hit")

    service = ArtistImageService(None, cache, provider_factory=provider_factory)
    result = service._resolve_job(
        identity,
        force=False,
        network_enabled=True,
        cancel_event=threading.Event(),
        generation=0,
    )
    service.shutdown()

    assert result.resolved and result.from_cache
    assert constructions == []


def test_ambiguous_historical_alias_requires_explicit_safe_cache_permission(
    tmp_path: Path,
):
    cache = ArtistImageCache(tmp_path / "artist_images")
    alias = ArtistIdentity.from_display_name("Shared Historical Alias")
    cached = cache.store(SyntheticArtistImageProvider().resolve(alias))
    first = ArtistIdentity.from_display_name(
        "First Real Artist",
        canonical_artist_id=1,
        discogs_artist_id="8201",
        historical_aliases=("Shared Historical Alias",),
    )
    second = ArtistIdentity.from_display_name(
        "Second Real Artist",
        canonical_artist_id=2,
        discogs_artist_id="8202",
        historical_aliases=("Shared Historical Alias",),
    )

    assert cache.lookup(first) is None
    assert cache.lookup(second) is None

    explicitly_safe = ArtistIdentity.from_display_name(
        "First Real Artist",
        canonical_artist_id=1,
        discogs_artist_id="8201",
        historical_aliases=("Shared Historical Alias",),
        allow_historical_alias_cache=True,
    )
    resolved = cache.lookup(explicitly_safe)
    assert resolved is not None and resolved.cache_file == cached.cache_file


def test_direct_wikimedia_with_musicbrainz_id_still_loses_to_discogs(
    tmp_path: Path,
):
    cache = ArtistImageCache(tmp_path / "artist_images")
    direct_identity = ArtistIdentity.from_display_name(
        "Priority Artist", musicbrainz_artist_id=MBID
    )
    discogs_identity = ArtistIdentity.from_display_name(
        "Priority Artist", discogs_artist_id="8301"
    )
    direct = cache.store(
        _result(
            direct_identity,
            provider="Wikimedia Commons",
            kind="direct_wikimedia",
            width=900,
            height=900,
        )
    )
    discogs = cache.store(
        _result(
            discogs_identity,
            provider="Discogs",
            kind="discogs",
            width=400,
            height=400,
        )
    )
    combined = ArtistIdentity.from_display_name(
        "Priority Artist",
        canonical_artist_id=42,
        musicbrainz_artist_id=MBID,
        discogs_artist_id="8301",
    )

    selected = cache.lookup(combined)

    assert selected is not None and selected.cache_file == discogs.cache_file
    assert selected.cache_file != direct.cache_file
