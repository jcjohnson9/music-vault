from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from music_vault.lyrics import (
    LRCLIBProvider,
    LyricLine,
    LyricsCache,
    LyricsCacheError,
    LyricsParseError,
    LyricsQuery,
    LyricsResult,
    LyricsService,
    LyricsSource,
    LyricsStatus,
    SafeLyricsTransport,
    TrackLyricsIdentity,
    extract_embedded_lyrics,
    parse_lrc,
    parse_plain_text,
)
from music_vault.lyrics.providers.base import LyricsContentError, UnsafeLyricsUrlError
from music_vault.lyrics.providers.lrclib import (
    MAX_JSON_BYTES,
    choose_lrclib_result,
    score_lrclib_candidate,
    validate_lrclib_url,
)
from music_vault.lyrics.service import EmbeddedLyrics


def track(tmp_path: Path, **changes) -> TrackLyricsIdentity:
    values = {
        "track_id": 17,
        "title": "Synthetic Signal",
        "artist": "Test Ensemble",
        "album": "Fixture Collection",
        "duration_ms": 180_000,
        "media_path": tmp_path / "fixture.wav",
    }
    values.update(changes)
    return TrackLyricsIdentity(**values)


def available(identity: TrackLyricsIdentity, *, synced: bool = True) -> LyricsResult:
    return LyricsResult(
        LyricsStatus.AVAILABLE,
        identity,
        LyricsSource.PROVIDER,
        (LyricLine(1000, "Synthetic line"),) if synced else (),
        None if synced else "Synthetic plain line",
        provider="LRCLIB",
        provider_result_id="42",
        provider_duration_ms=identity.duration_ms,
        attribution="Lyrics via LRCLIB",
        confidence=0.99,
    )


def candidate(**changes):
    payload = {
        "id": 42,
        "trackName": "Synthetic Signal",
        "artistName": "Test Ensemble",
        "albumName": "Fixture Collection",
        "duration": 180,
        "syncedLyrics": "[00:01.000]Synthetic line",
        "plainLyrics": "Synthetic line",
        "instrumental": False,
    }
    payload.update(changes)
    return payload


def test_lrc_parser_supports_timestamps_offsets_sorting_dedup_unicode_and_literal_html():
    parsed = parse_lrc(
        "[offset:-250]\n"
        "[00:03.125]Third\n"
        "[00:01.25][00:02]Héllo <b>literal</b>\n"
        "[00:01.25]Héllo <b>literal</b>\n"
        "[ar:Ignored metadata]\n"
        "[bad]ignored"
    )
    assert [(line.timestamp_ms, line.text) for line in parsed.lines] == [
        (1000, "Héllo <b>literal</b>"),
        (1750, "Héllo <b>literal</b>"),
        (2875, "Third"),
    ]
    assert parsed.offset_ms == -250
    assert "Héllo" not in repr(parsed)
    assert "literal" not in repr(parsed.lines[0])


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [("[01:02]", 62_000), ("[01:02.3]", 62_300), ("[01:02.34]", 62_340), ("[01:02.345]", 62_345)],
)
def test_lrc_timestamp_precision(timestamp, expected):
    parsed = parse_lrc(f"{timestamp}Synthetic")
    assert parsed.lines == (LyricLine(expected, "Synthetic"),)


def test_lrc_parser_bounds_and_empty_inputs_fail_safely():
    assert parse_lrc("").empty
    assert parse_plain_text(" \n").empty
    with pytest.raises(LyricsParseError, match="lyrics_too_large"):
        parse_lrc(b"x" * (1024 * 1024 + 1))
    with pytest.raises(LyricsParseError, match="too_many_lyric_lines"):
        parse_plain_text("\n" * 10_001)


def test_plain_parser_preserves_unicode_and_does_not_interpret_html():
    parsed = parse_plain_text("\n  Héllo <script>text</script>  \n\n")
    assert parsed.plain_text == "  Héllo <script>text</script>"
    assert not parsed.synchronized
    assert "script" not in repr(parsed)


def test_cache_is_hashed_content_addressed_atomic_and_verifies_content(tmp_path):
    identity = track(tmp_path)
    cache = LyricsCache(tmp_path / "lyrics")
    stored = cache.store(available(identity))
    index = json.loads(cache.index_path.read_text(encoding="utf-8"))
    entry_key = next(iter(index["entries"]))
    record = index["entries"][entry_key]["automatic"]
    content = cache.root / record["content_file"]
    assert len(entry_key) == 64
    assert content.stem == record["content_hash"]
    assert not list(cache.root.rglob("*.tmp"))
    assert cache.lookup_automatic(identity).from_cache
    content.write_bytes(b"corrupt")
    assert cache.lookup_automatic(identity) is None
    assert stored.synced_lines


def test_cache_negative_ttls_force_refresh_and_error_sanitization(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    identity = track(tmp_path)
    cache = LyricsCache(tmp_path / "lyrics", clock=lambda: now[0])
    no_match = cache.store_negative(identity, LyricsStatus.NO_MATCH)
    assert no_match.retry_after == "2026-01-31T00:00:00Z"
    assert cache.lookup_automatic(identity).status is LyricsStatus.NO_MATCH
    assert cache.lookup_automatic(identity, force=True) is None
    now[0] += timedelta(days=29)
    assert cache.lookup_automatic(identity) is not None
    now[0] += timedelta(days=2)
    assert cache.lookup_automatic(identity) is None

    temporary = cache.store_negative(
        identity,
        LyricsStatus.TEMPORARY_ERROR,
        error_code="C:/private/path?query=secret",
    )
    assert temporary.error_code == "lyrics_error"
    now[0] += timedelta(hours=5)
    assert cache.lookup_automatic(identity) is not None
    now[0] += timedelta(hours=2)
    assert cache.lookup_automatic(identity) is None


def test_manual_cache_survives_metadata_change_and_automatic_clear(tmp_path):
    identity = track(tmp_path)
    cache = LyricsCache(tmp_path / "lyrics")
    cache.store(available(identity))
    manual = LyricsResult(
        LyricsStatus.AVAILABLE,
        identity,
        LyricsSource.MANUAL,
        (),
        "Private synthetic manual text",
    )
    cache.store_manual(manual)
    changed = track(tmp_path, title="Corrected Synthetic Signal")
    assert cache.lookup_automatic(changed) is None
    assert cache.lookup_manual(changed).source is LyricsSource.MANUAL
    cache.clear_automatic(identity)
    assert cache.lookup_manual(identity) is not None
    assert cache.lookup_automatic(identity) is None


def test_cache_instances_observe_global_clear_without_resurrecting_entries(
    tmp_path,
):
    root = tmp_path / "lyrics"
    first_identity = track(tmp_path, track_id=17)
    second_identity = track(tmp_path, track_id=18, title="Second Synthetic Signal")
    stale_instance = LyricsCache(root)
    stale_instance.store(available(first_identity))
    stale_instance.store(available(second_identity))
    assert stale_instance.statistics()["automatic_count"] == 2

    clearing_instance = LyricsCache(root)
    clearing_instance.clear_automatic()
    assert clearing_instance.statistics()["automatic_count"] == 0
    assert stale_instance.lookup_automatic(first_identity) is None
    assert stale_instance.lookup_automatic(second_identity) is None

    manual = LyricsResult(
        LyricsStatus.AVAILABLE,
        first_identity,
        LyricsSource.MANUAL,
        (),
        "Private synthetic manual text",
    )
    stale_instance.store_manual(manual)
    final = LyricsCache(root)
    assert final.statistics()["automatic_count"] == 0
    assert final.statistics()["manual_count"] == 1


def test_manual_import_validates_then_copies_without_retaining_external_path(tmp_path):
    identity = track(tmp_path)
    external = tmp_path / "selected.lrc"
    external.write_text("[00:00.500]Imported synthetic line", encoding="utf-8")
    cache = LyricsCache(tmp_path / "private-cache")
    result = cache.import_manual(identity, external)
    external.unlink()
    cached = cache.lookup_manual(identity)
    assert result.source is LyricsSource.MANUAL
    assert cached.synced_lines[0].timestamp_ms == 500
    assert "selected.lrc" not in cache.index_path.read_text(encoding="utf-8")
    with pytest.raises(LyricsCacheError, match="unsupported"):
        cache.import_manual(identity, tmp_path / "selected.html")


def test_lrclib_strict_matching_duration_artist_version_ambiguity_and_preference(tmp_path):
    query = LyricsQuery(track(tmp_path))
    result = choose_lrclib_result(query, candidate(), exact=True)
    assert result.synchronized and result.provider == "LRCLIB"
    assert choose_lrclib_result(query, candidate(duration=240), exact=True) is None
    assert choose_lrclib_result(query, candidate(artistName="Different Artist"), exact=True) is None
    assert choose_lrclib_result(query, candidate(trackName="Synthetic Signal (Live)"), exact=True) is None
    assert choose_lrclib_result(query, [candidate(id=1), candidate(id=2)]) is None
    assert choose_lrclib_result(query, [candidate(id=1), candidate(id=1)]).synchronized
    preferred = choose_lrclib_result(
        query,
        [candidate(id=1, syncedLyrics=None), candidate(id=2)],
    )
    assert preferred.synchronized and preferred.provider_result_id == "2"


def test_lrclib_plain_and_instrumental_results_are_honest(tmp_path):
    query = LyricsQuery(track(tmp_path))
    plain = choose_lrclib_result(query, candidate(syncedLyrics=None), exact=True)
    assert plain.available and not plain.synchronized and plain.plain_text
    instrumental = choose_lrclib_result(
        query,
        candidate(syncedLyrics=None, plainLyrics=None, instrumental=True),
        exact=True,
    )
    assert instrumental.instrumental and not instrumental.synced_lines


def test_lrclib_url_validation_rejects_http_userinfo_hosts_and_private_dns():
    public = lambda *_args: [(2, 1, 6, "", ("8.8.8.8", 443))]
    assert validate_lrclib_url("https://lrclib.net/api/get", resolver=public).startswith("https://")
    for url in (
        "http://lrclib.net/api/get",
        "https://user@lrclib.net/api/get",
        "https://example.com/api/get",
        "https://127.0.0.1/api/get",
    ):
        with pytest.raises(UnsafeLyricsUrlError):
            validate_lrclib_url(url, resolver=public)
    private = lambda *_args: [(2, 1, 6, "", ("127.0.0.1", 443))]
    with pytest.raises(UnsafeLyricsUrlError, match="private_address_rejected"):
        validate_lrclib_url("https://lrclib.net/api/get", resolver=private)


class FakeResponse:
    def __init__(self, body: bytes, *, content_type="application/json", status=200):
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class FakeSession(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        return self.responses.pop(0)


def public_dns(*_args):
    return [(2, 1, 6, "", ("8.8.8.8", 443))]


def test_safe_transport_disables_environment_proxies_and_bounds_json():
    response = FakeResponse(json.dumps(candidate()).encode("utf-8"))
    session = FakeSession([response])
    transport = SafeLyricsTransport(session=session, resolver=public_dns)
    assert session.trust_env is False
    assert transport.get_json("https://lrclib.net/api/get")["id"] == 42
    request, kwargs = session.sent[0]
    assert "Music Vault" in request.headers["User-Agent"]
    assert "github.com/jcjohnson9/music-vault" in request.headers["User-Agent"]
    assert kwargs["timeout"] == (5.0, 15.0)
    assert kwargs["allow_redirects"] is False
    oversized = FakeResponse(b"{}")
    oversized.headers["Content-Length"] = str(MAX_JSON_BYTES + 1)
    with pytest.raises(LyricsContentError, match="response_too_large"):
        SafeLyricsTransport(session=FakeSession([oversized]), resolver=public_dns).get_json(
            "https://lrclib.net/api/get"
        )
    with pytest.raises(LyricsContentError, match="invalid_json"):
        SafeLyricsTransport(session=FakeSession([FakeResponse(b"")]), resolver=public_dns).get_json(
            "https://lrclib.net/api/get"
        )


class FakeTransport:
    def __init__(self, exact=None, search=None, error: Exception | None = None):
        self.exact = exact
        self.search = [] if search is None else search
        self.error = error
        self.calls = []

    def get_json(self, url, *, params=None, allow_not_found=False):
        self.calls.append((url, dict(params or {}), allow_not_found))
        if self.error:
            raise self.error
        return self.exact if url.endswith("/get") else self.search


def test_lrclib_provider_uses_exact_then_search_and_sanitizes_malformed_errors(tmp_path):
    identity = track(tmp_path)
    transport = FakeTransport(exact=None, search=[candidate()])
    result = LRCLIBProvider(transport).lookup(LyricsQuery(identity))
    assert result.synchronized
    assert [call[0].rsplit("/", 1)[-1] for call in transport.calls] == ["get", "search"]
    assert transport.calls[0][1]["duration"] == 180
    malformed = LRCLIBProvider(FakeTransport(exact="raw lyric content")).lookup(LyricsQuery(identity))
    assert malformed.status is LyricsStatus.TEMPORARY_ERROR
    assert "raw lyric" not in (malformed.error_code or "")
    secret_error = LRCLIBProvider(
        FakeTransport(error=RuntimeError("private/path Synthetic line"))
    ).lookup(LyricsQuery(identity))
    assert secret_error.error_code == "provider_error"


class CountingProvider:
    name = "Synthetic"

    def __init__(self, result_factory=available):
        self.calls = 0
        self.result_factory = result_factory

    def lookup(self, query, cancel_event=None):
        self.calls += 1
        return self.result_factory(query.identity)


def test_service_source_priority_and_online_consent(monkeypatch, tmp_path):
    media = tmp_path / "Fixture.MP3"
    media.write_bytes(b"synthetic")
    identity = track(tmp_path, media_path=media)
    cache = LyricsCache(tmp_path / "cache")
    provider = CountingProvider()
    service = LyricsService(provider, cache)
    try:
        disabled = service.resolve(identity, online_enabled=False)
        assert disabled.status is LyricsStatus.DISABLED and provider.calls == 0
        automatic = cache.store(available(identity))
        (tmp_path / "Fixture.lrc").write_text("[00:02]Sidecar synthetic", encoding="utf-8")
        sidecar = service.resolve(identity, online_enabled=True)
        assert sidecar.source is LyricsSource.SIDECAR_SYNCED
        manual = LyricsResult(LyricsStatus.AVAILABLE, identity, LyricsSource.MANUAL, (), "Manual synthetic")
        cache.store_manual(manual)
        winner = service.resolve(identity, online_enabled=True)
        assert winner.source is LyricsSource.MANUAL
        assert provider.calls == 0 and automatic.synced_lines
    finally:
        service.close()


def test_service_priority_places_embedded_sync_before_cache_and_plain_after_cache(monkeypatch, tmp_path):
    identity = track(tmp_path)
    cache = LyricsCache(tmp_path / "cache")
    cache.store(available(identity))
    embedded_sync = LyricsResult(
        LyricsStatus.AVAILABLE,
        identity,
        LyricsSource.EMBEDDED_SYNCED,
        (LyricLine(500, "Embedded synthetic"),),
    )
    monkeypatch.setattr(
        "music_vault.lyrics.service.extract_embedded_lyrics",
        lambda _identity: EmbeddedLyrics(embedded_sync, None),
    )
    service = LyricsService(CountingProvider(), cache)
    try:
        assert service.resolve(identity).source is LyricsSource.EMBEDDED_SYNCED
        monkeypatch.setattr(
            "music_vault.lyrics.service.extract_embedded_lyrics",
            lambda _identity: EmbeddedLyrics(
                None,
                LyricsResult(
                    LyricsStatus.AVAILABLE,
                    identity,
                    LyricsSource.EMBEDDED_PLAIN,
                    (),
                    "Embedded plain synthetic",
                ),
            ),
        )
        assert service.resolve(identity).source is LyricsSource.CACHE_SYNCED
    finally:
        service.close()


def test_service_cache_prevents_repeat_provider_request(tmp_path):
    identity = track(tmp_path)
    provider = CountingProvider()
    service = LyricsService(provider, LyricsCache(tmp_path / "cache"))
    try:
        first = service.resolve(identity, online_enabled=True)
        second = service.resolve(identity, online_enabled=True)
        assert first.available and second.from_cache and provider.calls == 1
    finally:
        service.close()


def test_mutagen_embedded_read_is_read_only_and_supports_sylt_uslt(monkeypatch, tmp_path):
    media_path = tmp_path / "fixture.mp3"
    original = b"not-real-audio-but-never-written"
    media_path.write_bytes(original)
    identity = track(tmp_path, media_path=media_path)

    class SYLT:
        text = [("Timed synthetic", 750)]

    class USLT:
        text = "Plain synthetic"

    class Tags(dict):
        def values(self):
            return [SYLT(), USLT()]

    import mutagen

    fake_media = SimpleNamespace(tags=Tags())
    monkeypatch.setattr(mutagen, "File", lambda path, easy=False: fake_media)
    embedded = extract_embedded_lyrics(identity)
    assert embedded.synchronized.synced_lines == (LyricLine(750, "Timed synthetic"),)
    assert embedded.plain.plain_text == "Plain synthetic"
    assert media_path.read_bytes() == original


def test_service_generation_suppresses_stale_result_and_keeps_one_logical_lookup(tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()

    class BlockingProvider:
        name = "Synthetic"

        def __init__(self):
            self.calls = 0

        def lookup(self, query, cancel_event=None):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                release_first.wait(2)
            return available(query.identity)

    provider = BlockingProvider()
    cache = LyricsCache(tmp_path / "cache")
    service = LyricsService(provider, cache)
    callbacks = []
    first = track(tmp_path, track_id=1, title="First Synthetic")
    second = track(tmp_path, track_id=2, title="Second Synthetic")
    third = track(tmp_path, track_id=3, title="Latest Synthetic")
    try:
        generation_one = service.request(
            first,
            lambda generation, result: callbacks.append((generation, result)),
            online_enabled=True,
        )
        assert first_started.wait(1)
        generation_two = service.request(
            second,
            lambda generation, result: callbacks.append((generation, result)),
            online_enabled=True,
        )
        generation_three = service.request(
            third,
            lambda generation, result: callbacks.append((generation, result)),
            online_enabled=True,
        )
        assert generation_three > generation_two > generation_one
        assert service.pending_count == 1
        release_first.set()
        deadline = time.monotonic() + 3
        while not callbacks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(callbacks) == 1
        assert callbacks[0][0] == generation_three
        assert callbacks[0][1].identity.track_id == 3
        assert provider.calls == 2
        assert cache.lookup_automatic(first) is None
        assert cache.lookup_automatic(second) is None
        assert service.pending_count == 0
    finally:
        release_first.set()
        service.close()
