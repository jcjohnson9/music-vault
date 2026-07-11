from __future__ import annotations

import json
import threading
import time
from concurrent.futures import CancelledError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from music_vault.core import paths
from music_vault.metadata.artist_images import (
    MAX_IMAGE_BYTES,
    ArtistIdentity,
    ArtistImageCache,
    ArtistImageContentError,
    ArtistImageResult,
    ArtistImageService,
    ArtistImageStatus,
    ArtistImageTemporaryError,
    MusicBrainzWikimediaProvider,
    SafeArtistImageTransport,
    SyntheticArtistImageProvider,
    UnsafeArtistImageUrlError,
    choose_musicbrainz_artist,
    create_artist_image_provider,
    is_safe_artist_source_url,
    normalize_artist_identity,
    validate_image_payload,
    validate_public_url,
)


ARTIST_ID_ONE = "11111111-1111-4111-8111-111111111111"
ARTIST_ID_TWO = "22222222-2222-4222-8222-222222222222"


def public_dns(host: str, port: int, *_args):
    del host, port
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


def private_dns(host: str, port: int, *_args):
    del host, port
    return [(2, 1, 6, "", ("127.0.0.1", 443))]


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = dict(headers or {})
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def prepare_request(self, request):
        return request.prepare()

    def send(self, prepared, **kwargs):
        self.calls.append((prepared, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def synthetic_png(name="Synthetic Portrait") -> bytes:
    result = SyntheticArtistImageProvider().resolve(ArtistIdentity.from_display_name(name))
    assert result.image_bytes
    return result.image_bytes


def wait_until(qapp, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    qapp.processEvents()
    assert predicate()


def test_artist_image_paths_follow_isolated_project_root(tmp_path, monkeypatch):
    (tmp_path / "run.py").write_text("", encoding="utf-8")
    (tmp_path / "music_vault").mkdir()
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(tmp_path))
    paths._resolved_project_root.cache_clear()
    try:
        assert paths.artist_images_dir() == tmp_path / "data" / "artist_images"
        assert paths.artist_image_files_dir() == (
            tmp_path / "data" / "artist_images" / "files"
        )
        assert paths.artist_image_index_path() == (
            tmp_path / "data" / "artist_images" / "index.json"
        )
    finally:
        paths._resolved_project_root.cache_clear()


def test_artist_identity_normalization_is_conservative():
    assert normalize_artist_identity("  The\tArtist  ") == "the artist"
    assert normalize_artist_identity("Beyonc\N{LATIN SMALL LETTER E WITH ACUTE}") == "beyonc\N{LATIN SMALL LETTER E WITH ACUTE}"
    assert normalize_artist_identity("Artist A & Artist B feat. Guest") == (
        "artist a & artist b feat. guest"
    )
    identity = ArtistIdentity.from_display_name("  Ensemble   &  Friends ")
    assert identity.display_name == "Ensemble & Friends"
    assert identity.normalized_key == "ensemble & friends"


def test_musicbrainz_match_requires_unique_exact_high_score():
    identity = ArtistIdentity.from_display_name("Exact Artist")
    status, match = choose_musicbrainz_artist(
        [{"id": ARTIST_ID_ONE, "name": " exact artist ", "score": 99}],
        identity,
    )
    assert status is ArtistImageStatus.RESOLVED
    assert match and match.artist_id == ARTIST_ID_ONE and match.score == 99

    low_status, _ = choose_musicbrainz_artist(
        [{"id": ARTIST_ID_ONE, "name": "Exact Artist", "score": 94}], identity
    )
    partial_status, _ = choose_musicbrainz_artist(
        [{"id": ARTIST_ID_ONE, "name": "Exact Artist Band", "score": 100}], identity
    )
    ambiguous_status, ambiguous = choose_musicbrainz_artist(
        [
            {"id": ARTIST_ID_ONE, "name": "Exact Artist", "score": 100},
            {"id": ARTIST_ID_TWO, "name": "EXACT ARTIST", "score": 99},
        ],
        identity,
    )
    assert low_status is ArtistImageStatus.NO_MATCH
    assert partial_status is ArtistImageStatus.NO_MATCH
    assert ambiguous_status is ArtistImageStatus.AMBIGUOUS
    assert ambiguous is None


@pytest.mark.parametrize(
    "url",
    [
        "http://musicbrainz.org/ws/2/artist",
        "https://musicbrainz.org.evil.example/ws/2/artist",
        "https://user@musicbrainz.org/ws/2/artist",
        "https://musicbrainz.org:444/ws/2/artist",
        "file:///etc/passwd",
        "https://127.0.0.1/ws/2/artist",
    ],
)
def test_public_url_rejects_unsafe_syntax(url):
    with pytest.raises(UnsafeArtistImageUrlError):
        validate_public_url(url, resolver=public_dns)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "fc00::1", "fe80::1"],
)
def test_public_url_rejects_every_non_public_dns_answer(address):
    def resolver(*_args):
        return [(2, 1, 6, "", (address, 443))]

    with pytest.raises(UnsafeArtistImageUrlError, match="private_address_rejected"):
        validate_public_url("https://musicbrainz.org/ws/2/artist", resolver=resolver)


def test_public_url_accepts_whitelisted_https_global_destination():
    assert validate_public_url(
        "https://musicbrainz.org/ws/2/artist/?fmt=json#ignored",
        resolver=public_dns,
    ) == "https://musicbrainz.org/ws/2/artist/?fmt=json"


def test_public_url_dns_failure_is_temporary():
    def failed_dns(*_args):
        raise socket_error("synthetic dns failure")

    class socket_error(OSError):
        pass

    with pytest.raises(ArtistImageTemporaryError, match="dns_unavailable"):
        validate_public_url("https://musicbrainz.org/ws/2/artist", resolver=failed_dns)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://musicbrainz.org/artist/{ARTIST_ID_ONE}", True),
        ("https://www.wikidata.org/wiki/Q42", True),
        ("https://en.wikipedia.org/wiki/Synthetic_Artist", True),
        ("https://commons.wikimedia.org/wiki/File:Synthetic.jpg", True),
        ("https://upload.wikimedia.org/image.jpg", False),
        ("http://en.wikipedia.org/wiki/Synthetic", False),
        ("https://en.wikipedia.org.evil.example/wiki/Synthetic", False),
    ],
)
def test_source_page_validation_is_strict_and_non_networked(url, expected):
    assert is_safe_artist_source_url(url) is expected


def test_transport_disables_environment_proxies_and_sets_safety_options():
    png = synthetic_png()
    session = FakeSession(
        [FakeResponse(headers={"Content-Type": "image/png"}, body=png)]
    )
    transport = SafeArtistImageTransport(session=session, resolver=public_dns)
    image = transport.get_image("https://upload.wikimedia.org/synthetic.png")
    prepared, options = session.calls[0]
    assert session.trust_env is False
    assert prepared.headers["User-Agent"].startswith("MusicVault/")
    assert options["allow_redirects"] is False
    assert options["stream"] is True
    assert options["timeout"][0] > 0 and options["timeout"][1] > 0
    assert image.width == 512 and image.height == 512


def test_transport_validates_every_redirect_before_following():
    first = FakeResponse(
        302,
        {"Location": "https://upload.wikimedia.org/synthetic.png"},
    )
    session = FakeSession([first])

    def resolver(host, *_args):
        address = "127.0.0.1" if host == "upload.wikimedia.org" else "93.184.216.34"
        return [(2, 1, 6, "", (address, 443))]

    transport = SafeArtistImageTransport(session=session, resolver=resolver)
    with pytest.raises(UnsafeArtistImageUrlError, match="private_address_rejected"):
        transport.get_image("https://commons.wikimedia.org/start")
    assert first.closed
    assert len(session.calls) == 1


def test_transport_rejects_oversize_non_image_and_corrupt_payloads():
    oversized = FakeResponse(
        headers={"Content-Type": "image/png", "Content-Length": str(MAX_IMAGE_BYTES + 1)}
    )
    wrong_mime = FakeResponse(headers={"Content-Type": "text/html"}, body=b"<html>")
    corrupt = FakeResponse(headers={"Content-Type": "image/png"}, body=b"not-png")
    transport = SafeArtistImageTransport(
        session=FakeSession([oversized, wrong_mime, corrupt]),
        resolver=public_dns,
    )
    with pytest.raises(ArtistImageContentError, match="response_too_large"):
        transport.get_image("https://upload.wikimedia.org/large.png")
    with pytest.raises(ArtistImageContentError, match="unsupported_image_type"):
        transport.get_image("https://upload.wikimedia.org/not-image")
    with pytest.raises(ArtistImageContentError, match="image_dimensions_invalid"):
        transport.get_image("https://upload.wikimedia.org/corrupt.png")


def test_validate_image_rejects_mime_mismatch_and_pixel_limit():
    png = synthetic_png()
    with pytest.raises(ArtistImageContentError, match="image_type_mismatch"):
        validate_image_payload(png, "image/jpeg")
    with pytest.raises(ArtistImageContentError, match="image_dimensions_rejected"):
        validate_image_payload(png, "image/png", max_pixels=100)


class FakeProviderTransport:
    def __init__(self, mode="resolved"):
        self.mode = mode
        self.calls = []

    def get_json(self, url, *, params=None):
        self.calls.append(("json", url, dict(params or {})))
        if self.mode == "temporary":
            raise ArtistImageTemporaryError("https://private.invalid/query?token=secret")
        if "ws/2/artist/" in url and url.rstrip("/").endswith(ARTIST_ID_ONE):
            return {
                "relations": [
                    {
                        "type": "wikidata",
                        "url": {"resource": "https://www.wikidata.org/wiki/Q42"},
                    }
                ]
            }
        if url == "https://musicbrainz.org/ws/2/artist/":
            candidates = [{"id": ARTIST_ID_ONE, "name": "Exact Artist", "score": 100}]
            if self.mode == "ambiguous":
                candidates.append(
                    {"id": ARTIST_ID_TWO, "name": "Exact Artist", "score": 99}
                )
            return {"artists": candidates}
        if "wikidata.org" in url:
            return {
                "entities": {
                    "Q42": {
                        "claims": {
                            "P18": [
                                {
                                    "mainsnak": {
                                        "datavalue": {"value": "Synthetic Portrait.png"}
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        if "commons.wikimedia.org" in url:
            return {
                "query": {
                    "pages": [
                        {
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.wikimedia.org/synthetic.png",
                                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Synthetic_Portrait.png",
                                }
                            ]
                        }
                    ]
                }
            }
        raise AssertionError(url)

    def get_image(self, url):
        self.calls.append(("image", url, {}))
        return validate_image_payload(synthetic_png(), "image/png")


def test_production_provider_resolves_only_high_confidence_identity():
    transport = FakeProviderTransport()
    provider = MusicBrainzWikimediaProvider(transport)  # type: ignore[arg-type]
    provider._musicbrainz_rate.minimum_interval_seconds = 0
    result = provider.resolve(ArtistIdentity.from_display_name("Exact Artist"))
    assert result.status is ArtistImageStatus.RESOLVED
    assert result.musicbrainz_artist_id == ARTIST_ID_ONE
    assert result.match_score == 100
    assert result.image_provider == "Wikimedia Commons"
    assert result.image_bytes
    assert is_safe_artist_source_url(result.source_page_url)


def test_production_provider_rejects_ambiguous_without_image_requests():
    transport = FakeProviderTransport("ambiguous")
    provider = MusicBrainzWikimediaProvider(transport)  # type: ignore[arg-type]
    provider._musicbrainz_rate.minimum_interval_seconds = 0
    result = provider.resolve(ArtistIdentity.from_display_name("Exact Artist"))
    assert result.status is ArtistImageStatus.AMBIGUOUS
    assert [call[0] for call in transport.calls] == ["json"]


def test_production_provider_sanitizes_temporary_errors():
    transport = FakeProviderTransport("temporary")
    provider = MusicBrainzWikimediaProvider(transport)  # type: ignore[arg-type]
    provider._musicbrainz_rate.minimum_interval_seconds = 0
    result = provider.resolve(ArtistIdentity.from_display_name("Exact Artist"))
    assert result.status is ArtistImageStatus.TEMPORARY_ERROR
    assert result.error_code == "provider_error"
    assert result.image_url is None


def test_cache_writes_atomic_hashed_runtime_provenance(tmp_path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Synthetic Artist")
    result = SyntheticArtistImageProvider().resolve(identity)
    stored = cache.store(result)
    assert stored.cache_file and stored.cache_file.is_file()
    assert stored.cache_file.parent.name == "files"
    assert identity.display_name not in stored.cache_file.name
    assert len(stored.cache_file.stem) == 64
    manifest = json.loads((cache.root / "index.json").read_text(encoding="utf-8"))
    record = next(iter(manifest["entries"].values()))
    assert manifest["schema_version"] == 1
    assert record["requested_display_name"] == "Synthetic Artist"
    assert record["normalized_key"] == "synthetic artist"
    assert record["cache_file"].startswith("files/")
    assert not list(cache.root.glob("*.tmp"))
    assert cache.lookup(identity).from_cache


def test_cache_drops_untrusted_provenance_urls_and_invalid_mbid(tmp_path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Synthetic Artist")
    stored = cache.store(
        ArtistImageResult(
            ArtistImageStatus.RESOLVED,
            identity,
            musicbrainz_artist_id="not-an-mbid",
            source_page_url="https://evil.example/source",
            image_url="https://evil.example/image.png",
            content_type="image/png",
            image_bytes=synthetic_png(),
        )
    )
    assert stored.musicbrainz_artist_id is None
    assert stored.source_page_url is None
    assert stored.image_url is None
    cached = cache.lookup(identity)
    assert cached.source_page_url is None and cached.image_url is None


def test_cache_deduplicates_identical_image_content(tmp_path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    first_identity = ArtistIdentity.from_display_name("First")
    second_identity = ArtistIdentity.from_display_name("Second")
    payload = synthetic_png("Shared")
    for identity in (first_identity, second_identity):
        cache.store(
            ArtistImageResult(
                ArtistImageStatus.RESOLVED,
                identity,
                content_type="image/png",
                image_bytes=payload,
            )
        )
    assert cache.statistics() == {
        "entry_count": 2,
        "file_count": 1,
        "total_bytes": len(payload),
    }


def test_cache_negative_and_temporary_ttls(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    cache = ArtistImageCache(tmp_path / "artist_images", clock=lambda: now[0])
    no_match = ArtistIdentity.from_display_name("No Match")
    temporary = ArtistIdentity.from_display_name("Temporary")
    cached_no_match = cache.store(ArtistImageResult(ArtistImageStatus.NO_MATCH, no_match))
    cached_temporary = cache.store(
        ArtistImageResult(ArtistImageStatus.TEMPORARY_ERROR, temporary)
    )
    assert cached_no_match.retry_after != cached_temporary.retry_after
    assert cache.lookup(no_match).status is ArtistImageStatus.NO_MATCH
    assert cache.lookup(temporary).status is ArtistImageStatus.TEMPORARY_ERROR
    now[0] += timedelta(hours=7)
    assert cache.lookup(temporary) is None
    assert cache.lookup(no_match) is not None
    now[0] += timedelta(days=31)
    assert cache.lookup(no_match) is None


def test_cache_missing_or_corrupt_file_self_heals(tmp_path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Synthetic")
    stored = cache.store(SyntheticArtistImageProvider().resolve(identity))
    stored.cache_file.unlink()
    assert cache.lookup(identity) is None
    assert json.loads(cache.index_path.read_text(encoding="utf-8"))["entries"] == {}

    stored = cache.store(SyntheticArtistImageProvider().resolve(identity))
    stored.cache_file.write_bytes(b"corrupt")
    assert cache.lookup(identity) is None
    assert not stored.cache_file.exists()


def test_cache_rejects_manifest_path_escape_and_clear_preserves_unrelated_data(tmp_path):
    root = tmp_path / "artist_images"
    root.mkdir()
    outside = tmp_path / "must-remain.txt"
    outside.write_text("safe", encoding="utf-8")
    identity = ArtistIdentity.from_display_name("Escape")
    entry_key = ArtistImageCache._entry_key(identity)
    (root / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": {
                    entry_key: {
                        "status": "resolved",
                        "normalized_key": identity.normalized_key,
                        "cache_file": "files/../../must-remain.txt",
                        "content_type": "image/png",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cache = ArtistImageCache(root)
    assert cache.lookup(identity) is None
    cache.clear()
    assert outside.read_text(encoding="utf-8") == "safe"


def test_cache_clear_one_preserves_shared_file_until_last_reference(tmp_path):
    cache = ArtistImageCache(tmp_path / "artist_images")
    payload = synthetic_png("Shared")
    identities = [ArtistIdentity.from_display_name(name) for name in ("One", "Two")]
    stored = []
    for identity in identities:
        stored.append(
            cache.store(
                ArtistImageResult(
                    ArtistImageStatus.RESOLVED,
                    identity,
                    content_type="image/png",
                    image_bytes=payload,
                )
            )
        )
    cache.clear(identities[0])
    assert stored[0].cache_file.exists()
    assert cache.lookup(identities[1]).resolved
    cache.clear(identities[1])
    assert not stored[0].cache_file.exists()


def test_synthetic_provider_factory_requires_explicit_isolated_review(monkeypatch):
    monkeypatch.setenv("MUSIC_VAULT_ARTIST_IMAGE_PROVIDER", "synthetic")
    monkeypatch.delenv("MUSIC_VAULT_UI_REVIEW", raising=False)
    monkeypatch.delenv("MUSIC_VAULT_PROJECT_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="isolated UI review"):
        create_artist_image_provider()

    monkeypatch.setenv("MUSIC_VAULT_UI_REVIEW", "synthetic-plan.json")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", "synthetic-root")
    assert isinstance(create_artist_image_provider(), SyntheticArtistImageProvider)


class CountingProvider:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = 0
        self.started = threading.Event()

    def resolve(self, identity, cancel_event=None):
        self.calls += 1
        self.started.set()
        if self.delay and cancel_event is not None and cancel_event.wait(self.delay):
            raise CancelledError()
        return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)


def test_service_disabled_mode_uses_cache_but_never_provider(tmp_path, qapp):
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Cached Artist")
    cache.store(SyntheticArtistImageProvider().resolve(identity))
    provider = CountingProvider()
    service = ArtistImageService(provider, cache)
    results = []
    service.request("Cached Artist", results.append, network_enabled=False)
    wait_until(qapp, lambda: len(results) == 1)
    assert results[0].resolved and results[0].from_cache
    assert provider.calls == 0

    service.request("Uncached Artist", results.append, network_enabled=False)
    wait_until(qapp, lambda: len(results) == 2)
    assert results[1].status is ArtistImageStatus.DISABLED
    assert provider.calls == 0
    service.shutdown()


def test_service_coalesces_duplicate_requests_and_delivers_on_owner_thread(
    tmp_path, qapp
):
    provider = CountingProvider(delay=0.08)
    service = ArtistImageService(provider, ArtistImageCache(tmp_path / "cache"))
    results = []
    callback_threads = []
    owner_thread = threading.get_ident()

    def callback(result):
        results.append(result)
        callback_threads.append(threading.get_ident())

    assert service.request("Same Artist", callback)
    assert not service.request(" same   artist ", callback)
    wait_until(qapp, lambda: len(results) == 2)
    assert provider.calls == 1
    assert callback_threads == [owner_thread, owner_thread]
    assert service.pending_count == 0
    service.shutdown()


def test_service_force_bypasses_negative_cache(tmp_path, qapp):
    provider = CountingProvider()
    cache = ArtistImageCache(tmp_path / "cache")
    identity = ArtistIdentity.from_display_name("Negative")
    cache.store(ArtistImageResult(ArtistImageStatus.NO_MATCH, identity))
    service = ArtistImageService(provider, cache)
    normal = []
    forced = []
    service.request("Negative", normal.append)
    wait_until(qapp, lambda: normal)
    assert normal[0].from_cache and provider.calls == 0
    service.request("Negative", forced.append, force=True)
    wait_until(qapp, lambda: forced)
    assert provider.calls == 1
    service.shutdown()


def test_service_cancel_and_clear_abandon_pending_results(tmp_path, qapp):
    root = tmp_path / "cache"
    cache = ArtistImageCache(root)
    provider = CountingProvider(delay=1.0)
    service = ArtistImageService(provider, cache)
    delivered = []
    service.request("Delayed", delivered.append)
    assert provider.started.wait(1)
    service.clear_cache()
    wait_until(qapp, lambda: service.pending_count == 0)
    assert delivered == []
    assert not cache.index_path.exists()
    service.shutdown()


def test_service_generation_prevents_cancel_ignoring_job_from_repopulating_cache(
    tmp_path, qapp
):
    class GateProvider:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def resolve(self, identity, cancel_event=None):
            del cancel_event
            self.started.set()
            assert self.release.wait(2)
            return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)

    provider = GateProvider()
    cache = ArtistImageCache(tmp_path / "cache")
    service = ArtistImageService(provider, cache)
    delivered = []
    service.request("Stale Artist", delivered.append)
    assert provider.started.wait(1)
    service.clear_cache()
    provider.release.set()
    wait_until(qapp, lambda: service.pending_count == 0)
    time.sleep(0.05)
    qapp.processEvents()
    assert delivered == []
    assert cache.lookup(ArtistIdentity.from_display_name("Stale Artist")) is None
    service.shutdown()


def test_service_converts_corrupt_provider_image_to_cached_unavailable(tmp_path, qapp):
    provider = SyntheticArtistImageProvider()
    cache = ArtistImageCache(tmp_path / "cache")
    service = ArtistImageService(provider, cache)
    results = []
    service.request("Corrupt Artist", results.append)
    wait_until(qapp, lambda: results)
    assert results[0].status is ArtistImageStatus.UNAVAILABLE
    cached = cache.lookup(ArtistIdentity.from_display_name("Corrupt Artist"))
    assert cached and cached.status is ArtistImageStatus.UNAVAILABLE
    service.shutdown()
