from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from music_vault.app import MusicVaultWindow
from music_vault.core.db import MusicVaultDB
from music_vault.metadata.artist_images import (
    ArtistIdentity,
    ArtistImageCache,
    ArtistImageContentError,
    ArtistImageResult,
    ArtistImageStatus,
    ChainedArtistImageProvider,
    DiscogsArtistImageProvider,
    SyntheticArtistImageProvider,
    validate_image_payload,
)


class _Catalogue:
    def __init__(self, payload: dict, *, results: tuple[dict, ...] = ()) -> None:
        self.payload = payload
        self.results = results
        self.search_calls = 0
        self.artist_calls: list[str] = []

    def search_artists(self, *_args, **_kwargs):
        self.search_calls += 1
        return self.results

    def get_artist(self, artist_id: str, **_kwargs):
        self.artist_calls.append(str(artist_id))
        return self.payload


class _ImageTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.urls: list[str] = []
        payload = SyntheticArtistImageProvider._portrait(
            ArtistIdentity.from_display_name("Synthetic Portrait")
        )
        self.image = validate_image_payload(payload, "image/png")

    def get_image(self, url: str):
        self.urls.append(url)
        if self.fail:
            raise ArtistImageContentError("image_decode_failed")
        return self.image


class _Fallback:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, identity, _cancel_event=None):
        self.calls += 1
        payload = SyntheticArtistImageProvider._portrait(identity)
        return ArtistImageResult(
            ArtistImageStatus.RESOLVED,
            identity,
            matched_artist_name=identity.display_name,
            image_provider="Synthetic Wikimedia fallback",
            source_page_url="https://en.wikipedia.org/wiki/Synthetic_Unit",
            image_url="https://upload.wikimedia.org/synthetic.png",
            content_type="image/png",
            image_bytes=payload,
        )


class _StatusProvider:
    def __init__(self, status: ArtistImageStatus) -> None:
        self.status = status

    def resolve(self, identity, _cancel_event=None):
        return ArtistImageResult(self.status, identity)


def _artist_payload(name: str = "Synthetic Unit") -> dict:
    return {
        "id": 7001,
        "name": name,
        "images": [
            {
                "type": "primary",
                "uri": "https://i.discogs.com/synthetic-artist.png",
                "width": 512,
                "height": 512,
            },
            {
                "type": "secondary",
                "uri": "https://i.discogs.com/secondary.png",
                "width": 512,
                "height": 512,
            },
        ],
    }


def test_discogs_canonical_artist_id_uses_artist_image_not_release_artwork():
    catalogue = _Catalogue(_artist_payload())
    transport = _ImageTransport()
    provider = DiscogsArtistImageProvider(
        catalogue_provider=catalogue,
        transport=transport,
    )
    identity = ArtistIdentity.from_display_name(
        "Synthetic Unit", discogs_artist_id="7001"
    )
    result = provider.resolve(identity)

    assert result.status is ArtistImageStatus.RESOLVED
    assert result.discogs_artist_id == "7001"
    assert result.image_provider == "Discogs"
    assert result.attribution_text == "Data provided by Discogs"
    assert result.source_page_url == "https://www.discogs.com/artist/7001"
    assert transport.urls == ["https://i.discogs.com/synthetic-artist.png"]
    assert catalogue.search_calls == 0
    assert catalogue.artist_calls == ["7001"]


def test_discogs_name_lookup_requires_one_exact_canonical_match():
    catalogue = _Catalogue(
        _artist_payload(),
        results=(
            {"id": 7001, "title": "Synthetic Unit (3)"},
            {"id": 8002, "title": "Different Unit"},
        ),
    )
    result = DiscogsArtistImageProvider(
        catalogue_provider=catalogue,
        transport=_ImageTransport(),
    ).resolve(ArtistIdentity.from_display_name("Synthetic Unit"))
    assert result.status is ArtistImageStatus.RESOLVED
    assert result.discogs_artist_id == "7001"
    assert catalogue.search_calls == 1

    ambiguous = _Catalogue(
        _artist_payload(),
        results=(
            {"id": 7001, "title": "Synthetic Unit"},
            {"id": 7002, "title": "Synthetic Unit (2)"},
        ),
    )
    unresolved = DiscogsArtistImageProvider(
        catalogue_provider=ambiguous,
        transport=_ImageTransport(),
    ).resolve(ArtistIdentity.from_display_name("Synthetic Unit"))
    assert unresolved.status is ArtistImageStatus.NO_MATCH
    assert ambiguous.artist_calls == []


def test_invalid_discogs_portrait_falls_through_to_public_chain():
    discogs = DiscogsArtistImageProvider(
        catalogue_provider=_Catalogue(_artist_payload()),
        transport=_ImageTransport(fail=True),
    )
    fallback = _Fallback()
    result = ChainedArtistImageProvider((discogs, fallback)).resolve(
        ArtistIdentity.from_display_name("Synthetic Unit", discogs_artist_id="7001")
    )
    assert result.status is ArtistImageStatus.RESOLVED
    assert result.image_provider == "Synthetic Wikimedia fallback"
    assert fallback.calls == 1


def test_temporary_provider_failure_is_not_downgraded_by_later_no_match():
    identity = ArtistIdentity.from_display_name("Synthetic Retry Unit")
    result = ChainedArtistImageProvider(
        (
            _StatusProvider(ArtistImageStatus.TEMPORARY_ERROR),
            _StatusProvider(ArtistImageStatus.NO_MATCH),
        )
    ).resolve(identity)
    assert result.status is ArtistImageStatus.TEMPORARY_ERROR


def test_discogs_portraits_are_private_content_addressed_and_provider_aware(
    tmp_path: Path,
):
    cache = ArtistImageCache(tmp_path / "artist_images")
    provider = DiscogsArtistImageProvider(
        catalogue_provider=_Catalogue(_artist_payload()),
        transport=_ImageTransport(),
    )
    first_identity = ArtistIdentity.from_display_name(
        "Shared Public Name", discogs_artist_id="7001"
    )
    second_identity = ArtistIdentity.from_display_name(
        "Shared Public Name", discogs_artist_id="7002"
    )
    first_result = provider.resolve(
        ArtistIdentity.from_display_name("Synthetic Unit", discogs_artist_id="7001")
    )
    # Preserve exact identity keys while reusing a deterministic synthetic
    # payload; same-name provider entities must not share manifest entries.
    first = cache.store(
        ArtistImageResult(
            **{
                **first_result.__dict__,
                "identity": first_identity,
                "discogs_artist_id": "7001",
            }
        )
    )
    second = cache.store(
        ArtistImageResult(
            **{
                **first_result.__dict__,
                "identity": second_identity,
                "discogs_artist_id": "7002",
            }
        )
    )
    assert first.cache_file == second.cache_file
    assert cache.statistics() == {
        "entry_count": 2,
        "file_count": 1,
        "total_bytes": first.cache_file.stat().st_size,
    }
    assert cache.lookup(first_identity).discogs_artist_id == "7001"
    assert cache.lookup(second_identity).discogs_artist_id == "7002"
    assert cache.lookup(first_identity).attribution_text == "Data provided by Discogs"


def test_provider_backed_same_name_identities_never_inherit_name_only_portrait(
    tmp_path: Path,
):
    cache = ArtistImageCache(tmp_path / "artist_images")
    legacy = ArtistIdentity.from_display_name("Shared Synthetic Name")
    cached = SyntheticArtistImageProvider().resolve(legacy)
    cache.store(cached)

    first = ArtistIdentity.from_display_name(
        "Shared Synthetic Name", discogs_artist_id="7001"
    )
    second = ArtistIdentity.from_display_name(
        "Shared Synthetic Name", discogs_artist_id="7002"
    )
    assert cache.lookup(first) is None
    assert cache.lookup(second) is None
    assert cache.lookup(legacy).status is ArtistImageStatus.RESOLVED


def test_startup_rekeys_unambiguous_consolidation_alias_portrait(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "portrait-rekey.sqlite3")
    artist_id = int(
        db.conn.execute(
            """
            INSERT INTO artists (
                display_name,normalized_name,sort_name,entity_type,
                discogs_artist_id,musicbrainz_artist_id,created_at,updated_at
            ) VALUES ('Canonical Unit','canonical unit','canonical unit','person',
                      '7001','11111111-1111-4111-8111-111111111111',
                      '2026-07-17T00:00:00Z','2026-07-17T00:00:00Z')
            """
        ).lastrowid
    )
    db.conn.execute(
        """
        INSERT INTO artist_aliases (
            artist_id,alias_name,normalized_alias,alias_kind,provenance,
            confidence,created_at
        ) VALUES (?, 'Canonical Unit Live at Synthetic Hall',
                  'canonical unit live at synthetic hall',
                  'corrected_version_suffix','synthetic',100,
                  '2026-07-17T00:00:00Z')
        """,
        (artist_id,),
    )
    db.conn.commit()
    cache = ArtistImageCache(tmp_path / "artist_images")
    old_identity = ArtistIdentity.from_display_name(
        "Canonical Unit Live at Synthetic Hall",
        musicbrainz_artist_id="11111111-1111-4111-8111-111111111111",
    )
    cache.store(SyntheticArtistImageProvider().resolve(old_identity))
    window = SimpleNamespace(db=db, artist_image_cache=cache)

    assert MusicVaultWindow._rekey_consolidated_artist_portraits(window) == 1
    new_identity = ArtistIdentity.from_display_name(
        "Canonical Unit",
        discogs_artist_id="7001",
        musicbrainz_artist_id="11111111-1111-4111-8111-111111111111",
    )
    assert cache.lookup(new_identity).status is ArtistImageStatus.RESOLVED
    assert cache.lookup(
        ArtistIdentity.from_display_name("Canonical Unit Live at Synthetic Hall")
    ).status is ArtistImageStatus.RESOLVED
    db.close()


def test_provider_rekey_uses_exact_entry_and_leaves_same_name_legacy_cache(
    tmp_path: Path,
):
    cache = ArtistImageCache(tmp_path / "artist_images")
    legacy_identity = ArtistIdentity.from_display_name("Shared Rekey Name")
    provider_identity = ArtistIdentity.from_display_name(
        "Shared Rekey Name", discogs_artist_id="8001"
    )
    canonical_identity = ArtistIdentity.from_display_name(
        "Canonical Rekey Name", discogs_artist_id="8001"
    )
    cache.store(ArtistImageResult(ArtistImageStatus.NO_MATCH, legacy_identity))
    provider = cache.store(SyntheticArtistImageProvider().resolve(provider_identity))

    assert cache.rekey(provider_identity, canonical_identity)
    assert cache.lookup(canonical_identity).status is ArtistImageStatus.RESOLVED
    assert cache.lookup(legacy_identity).status is ArtistImageStatus.NO_MATCH
    manifest = json.loads(cache.index_path.read_text(encoding="utf-8"))
    assert {
        record["cache_file"]
        for record in manifest["entries"].values()
        if record["status"] == ArtistImageStatus.RESOLVED.value
    } == {
        provider.cache_file.relative_to(cache.root).as_posix()
    }
