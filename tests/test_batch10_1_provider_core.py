from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from music_vault.metadata.discogs_artwork import (
    ATTRIBUTION_STATE,
    DISCOGS_IMAGE_HOSTS,
    DiscogsArtworkCache,
    DiscogsArtworkError,
    is_true_artwork_gap,
    validate_discogs_image_url,
)
from music_vault.metadata.ensemble import (
    ConfidenceLevel,
    FieldAction,
    build_metadata_ensemble,
    recording_group_key,
)
from music_vault.metadata.providers import (
    ProviderArtistCredit,
    ProviderArtworkCandidate,
    ProviderQuery,
    ProviderReleaseCandidate,
)
from music_vault.metadata.providers.discogs import (
    DISCOGS_ATTRIBUTION_TEXT,
    DiscogsProvider,
    DiscogsProviderError,
    DiscogsRateLimiter,
    _MemoryResponseCache,
    format_artist_credits,
    parse_discogs_artist_credits,
    parse_discogs_release,
    rank_discogs_candidates,
)
from music_vault.metadata.title_parser import parse_youtube_title
from music_vault.metadata.uploader_classifier import (
    UploaderClass,
    choose_artist_fallback,
    classify_uploader,
)


def public_dns(host: str, port: int, *_args):
    assert host in {"api.discogs.com", *DISCOGS_IMAGE_HOSTS}
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def image_bytes(fmt: str = "PNG", width: int = 24, height: int = 24) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#725cff"))
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt)
    buffer.close()
    return bytes(encoded)


class FakeResponse:
    def __init__(self, status=200, *, headers=None, body=b""):
        self.status_code = status
        self.headers = dict(headers or {})
        self.body = bytes(body)
        self.closed = False

    def iter_content(self, chunk_size=64 * 1024):
        for offset in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected synthetic network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload, status=200, headers=None):
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    body = json.dumps(payload).encode("utf-8")
    merged.setdefault("Content-Length", str(len(body)))
    return FakeResponse(status, headers=merged, body=body)


def release_payload(
    *,
    release_id=101,
    release_title="Synthetic Sunrise",
    track_title="Signal Fire",
    artist="Example Duo",
    duration="3:20",
    released="1984-02-03",
    master_id=201,
    formats=None,
    images=None,
):
    return {
        "id": release_id,
        "title": release_title,
        "artists": [{"id": 301, "name": artist, "join": ""}],
        "tracklist": [
            {
                "position": "A1",
                "title": track_title,
                "duration": duration,
                "artists": [{"id": 301, "name": artist, "join": ""}],
            }
        ],
        "released": released,
        "master_id": master_id,
        "formats": formats or [{"name": "Vinyl", "descriptions": ["Album"]}],
        "labels": [{"name": "Synthetic Records", "catno": "S-1"}],
        "country": "US",
        "images": images or [],
    }


def provider_candidate(**changes):
    values = {
        "provider": "Discogs",
        "title": "Signal Fire",
        "artist": "Example Duo",
        "artist_credits": (ProviderArtistCredit("Example Duo"),),
        "album": "Synthetic Sunrise",
        "album_artist": "Example Duo",
        "release_date": "1984-02-03",
        "original_release_date": "1984",
        "version_type": "studio",
        "provider_score": 96.0,
        "release_id": "101",
        "master_id": "201",
        "track_position": "A1",
        "provider_reference": "https://www.discogs.com/release/101",
        "field_scores": {},
    }
    values.update(changes)
    return ProviderReleaseCandidate(**values)


def artwork_candidate(**changes):
    values = {
        "source_url": "https://i.discogs.com/synthetic-front.png",
        "provider_page_url": "https://www.discogs.com/release/101",
        "release_id": "101",
        "image_type": "front",
        "width": 24,
        "height": 24,
        "catalogue_image": True,
    }
    values.update(changes)
    return ProviderArtworkCandidate(**values)


@pytest.mark.parametrize(
    ("raw", "artist", "title", "pattern"),
    [
        ("Alpha - Bright Sky", "Alpha", "Bright Sky", "artist_dash_title"),
        ("Alpha – Bright Sky", "Alpha", "Bright Sky", "artist_dash_title"),
        ("Alpha — Bright Sky", "Alpha", "Bright Sky", "artist_dash_title"),
        ("Bright Sky by Alpha", "Alpha", "Bright Sky", "title_by_artist"),
        ("Alpha: Bright Sky", "Alpha", "Bright Sky", "artist_colon_title"),
    ],
)
def test_title_parser_supported_identity_patterns(raw, artist, title, pattern):
    parsed = parse_youtube_title(raw)
    assert (parsed.artist_hint, parsed.title_hint, parsed.pattern) == (artist, title, pattern)
    assert parsed.raw_title == raw


def test_title_parser_year_feature_and_presentation_are_hints_only():
    raw = "Alpha - Bright Sky feat. Beta (1998) [Official Audio]"
    parsed = parse_youtube_title(raw)
    assert parsed.raw_title == raw
    assert parsed.artist_hint == "Alpha"
    assert parsed.title_hint == "Bright Sky"
    assert parsed.featured_artist_hint == "Beta"
    assert parsed.year_hint == 1998
    assert parsed.presentation_suffixes == ("Official Audio",)


@pytest.mark.parametrize(
    ("suffix", "version_type"),
    [("Live", "live"), ("Remix", "remix"), ("Slowed & Reverb", "slowed")],
)
def test_title_parser_preserves_meaningful_version_qualifier(suffix, version_type):
    parsed = parse_youtube_title(f"Alpha - Bright Sky [{suffix}]")
    assert parsed.version_type == version_type
    assert parsed.version_label == suffix
    assert parsed.search_title.endswith(f"[{suffix}]")


def test_title_parser_does_not_split_group_ampersand():
    parsed = parse_youtube_title("Alpha & Beta - Bright Sky")
    assert parsed.artist_hint == "Alpha & Beta"
    assert parsed.featured_artist_hint is None


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        ("Synthetic Records", UploaderClass.LIKELY_LABEL),
        ("Synthetic Fan Archive", UploaderClass.LIKELY_FAN),
        ("Synthetic Distribution", UploaderClass.LIKELY_DISTRIBUTOR),
        ("Synthetic Artist - Topic", UploaderClass.LIKELY_TOPIC),
    ],
)
def test_uploader_company_or_fan_classes_cannot_be_artist(channel, expected):
    result = classify_uploader(channel)
    assert result.classification is expected
    assert result.prevents_artist_use
    fallback = choose_artist_fallback(uploader=channel)
    assert fallback.artist is None


def test_uploader_provider_match_is_only_weak_artist_evidence():
    result = classify_uploader("Example Duo Official", provider_artists=["Example Duo"])
    assert result.classification is UploaderClass.LIKELY_OFFICIAL_ARTIST
    assert result.confidence < 0.8
    fallback = choose_artist_fallback(uploader="Example Duo Official")
    assert fallback.provenance == "youtube_uploader_fallback"
    assert fallback.confidence == 0.25


@pytest.mark.parametrize(
    ("kwargs", "artist", "provenance"),
    [
        ({"discogs_artist": "Catalogue Artist"}, "Catalogue Artist", "discogs"),
        ({"musicbrainz_artist": "Secondary Artist"}, "Secondary Artist", "musicbrainz"),
        ({"embedded_artist": "Tagged Artist"}, "Tagged Artist", "embedded"),
        ({"parsed_artist": "Parsed Artist"}, "Parsed Artist", "youtube_title_parsed"),
    ],
)
def test_better_artist_evidence_always_beats_uploader(kwargs, artist, provenance):
    chosen = choose_artist_fallback(uploader="Random Upload Channel", **kwargs)
    assert (chosen.artist, chosen.provenance) == (artist, provenance)


def test_unknown_uploader_is_final_fallback_and_remains_provenance():
    chosen = choose_artist_fallback(uploader="Unclassified Channel")
    assert chosen.artist == "Unclassified Channel"
    assert chosen.provenance == "youtube_uploader_fallback"
    assert chosen.confidence < 0.5


def test_discogs_structured_featured_credit_targets_following_artist():
    credits = parse_discogs_artist_credits(
        [
            {"id": 1, "name": "Primary", "join": " feat. "},
            {"id": 2, "name": "Guest", "join": ""},
        ]
    )
    assert [credit.role for credit in credits] == ["primary", "featured"]
    assert [credit.join_phrase for credit in credits] == [" feat. ", ""]
    assert format_artist_credits(credits) == "Primary feat. Guest"


def test_discogs_group_credit_remains_one_entity_and_label_is_release_only():
    query = ProviderQuery("Signal Fire", artist="Example Duo")
    candidate = parse_discogs_release(release_payload(), query)
    assert candidate is not None
    assert len(candidate.artist_credits) == 1
    assert candidate.artist == "Example Duo"
    assert candidate.label == "Synthetic Records"
    assert all(credit.name != candidate.label for credit in candidate.artist_credits)


def test_discogs_exact_tracklist_artist_duration_scores_highly():
    candidate = parse_discogs_release(
        release_payload(), ProviderQuery("Signal Fire", artist="Example Duo", duration_seconds=200)
    )
    assert candidate is not None
    assert candidate.provider_score >= 95
    assert "exact_tracklist_title" in candidate.reasons
    assert "exact_artist_credit" in candidate.reasons


def test_discogs_artist_duration_and_version_conflicts_lower_or_cap_score():
    artist_mismatch = parse_discogs_release(
        release_payload(), ProviderQuery("Signal Fire", artist="Different Artist")
    )
    duration_mismatch = parse_discogs_release(
        release_payload(), ProviderQuery("Signal Fire", artist="Example Duo", duration_seconds=400)
    )
    version_mismatch = parse_discogs_release(
        release_payload(), ProviderQuery("Signal Fire", artist="Example Duo", version_type="live")
    )
    assert artist_mismatch and artist_mismatch.provider_score <= 59
    assert duration_mismatch and duration_mismatch.provider_score < 90
    assert version_mismatch and version_mismatch.provider_score <= 59
    assert "version_conflict" in version_mismatch.reasons


def test_provider_query_album_hint_affects_search_and_scoring():
    matching = parse_discogs_release(
        release_payload(),
        ProviderQuery("Signal Fire", artist="Example Duo", album="Synthetic Sunrise"),
    )
    mismatching = parse_discogs_release(
        release_payload(),
        ProviderQuery("Signal Fire", artist="Example Duo", album="Unrelated Collection"),
    )
    assert matching and mismatching
    assert "album_context_match" in matching.reasons
    assert "album_context_mismatch" in mismatching.reasons
    assert matching.provider_score > mismatching.provider_score

    session = FakeSession(
        [json_response({"results": [], "pagination": {"pages": 1}})]
    )
    provider = DiscogsProvider(
        "x",
        session,
        resolver=public_dns,
        rate_limiter=DiscogsRateLimiter(0),
    )
    provider.search_catalogue(
        ProviderQuery("Signal Fire", artist="Example Duo", album="Synthetic Sunrise")
    )
    assert session.calls[0][1]["params"]["release_title"] == "Synthetic Sunrise"


def test_discogs_master_original_year_prevents_late_reissue_year():
    candidate = parse_discogs_release(
        release_payload(released="2022-04-01"),
        ProviderQuery("Signal Fire", artist="Example Duo"),
        master_payload={"year": 1984},
    )
    assert candidate is not None
    assert candidate.release_date == "1984"
    assert candidate.original_release_date == "1984"


def test_compilation_not_blindly_preferred_and_ambiguous_album_is_withheld():
    normal = parse_discogs_release(
        release_payload(release_id=101, release_title="Original Context"),
        ProviderQuery("Signal Fire", artist="Example Duo"),
    )
    compilation = parse_discogs_release(
        release_payload(
            release_id=102,
            release_title="Synthetic Collection",
            formats=[{"name": "CD", "descriptions": ["Compilation"]}],
        ),
        ProviderQuery("Signal Fire", artist="Example Duo"),
    )
    assert normal and compilation
    assert rank_discogs_candidates([compilation, normal])[0].release_id == "101"
    assert compilation.album is None

    other = replace(normal, release_id="103", album="Other Context", provider_order=2)
    ambiguous = rank_discogs_candidates([normal, other])[0]
    assert ambiguous.album is None
    assert "release_ambiguous" in ambiguous.reasons


def test_normalized_candidate_persists_ids_not_raw_response():
    candidate = parse_discogs_release(
        release_payload(), ProviderQuery("Signal Fire", artist="Example Duo")
    )
    assert candidate is not None
    accepted = candidate.accepted_metadata()
    assert accepted["release_id"] == "101"
    assert accepted["master_id"] == "201"
    assert accepted["track_position"] == "A1"
    assert "tracklist" not in accepted
    assert "images" not in accepted


def test_discogs_provider_requires_token_and_never_uses_environment_proxies():
    with pytest.raises(DiscogsProviderError, match="discogs_token_required"):
        DiscogsProvider("")
    session = FakeSession([json_response({"username": "synthetic"})])
    provider = DiscogsProvider(
        "x", session, resolver=public_dns, rate_limiter=DiscogsRateLimiter(0)
    )
    assert session.trust_env is False
    assert provider.test_connection()
    url, kwargs = session.calls[0]
    assert url == "https://api.discogs.com/oauth/identity"
    assert "token" not in (kwargs.get("params") or {})
    assert kwargs["headers"]["Authorization"] == "Discogs token=x"


def test_discogs_errors_are_sanitized_and_do_not_echo_credential():
    session = FakeSession([json_response({}, status=401)])
    provider = DiscogsProvider(
        "x", session, resolver=public_dns, rate_limiter=DiscogsRateLimiter(0)
    )
    with pytest.raises(DiscogsProviderError) as raised:
        provider.test_connection()
    assert str(raised.value) == "discogs_auth_rejected"
    assert "Discogs token" not in str(raised.value)


def test_discogs_rate_limit_and_temporary_failure_are_bounded_and_retryable():
    limiter = DiscogsRateLimiter(0, sleeper=lambda _seconds: None)
    session = FakeSession(
        [
            json_response({}, status=429, headers={"Retry-After": "0"}),
            json_response({}, status=503),
            json_response({"username": "synthetic"}),
        ]
    )
    provider = DiscogsProvider("x", session, resolver=public_dns, rate_limiter=limiter)
    assert provider.test_connection()
    assert len(session.calls) == 3


def test_discogs_rejects_redirect_invalid_mime_and_oversized_response():
    responses = [
        FakeResponse(302, headers={"Location": "https://elsewhere.invalid"}),
        FakeResponse(200, headers={"Content-Type": "text/html"}, body=b"bad"),
        FakeResponse(
            200,
            headers={"Content-Type": "application/json", "Content-Length": str(3_000_000)},
        ),
    ]
    expected = [
        "discogs_redirect_rejected",
        "discogs_response_rejected",
        "discogs_response_too_large",
    ]
    for response, error in zip(responses, expected):
        provider = DiscogsProvider(
            "x",
            FakeSession([response]),
            resolver=public_dns,
            rate_limiter=DiscogsRateLimiter(0),
        )
        with pytest.raises(DiscogsProviderError, match=error):
            provider.test_connection()


def test_discogs_short_lived_memory_cache_suppresses_duplicate_request_and_expires():
    now = [0.0]
    cache = _MemoryResponseCache(max_age_seconds=5, clock=lambda: now[0])
    session = FakeSession([json_response({"id": 101}), json_response({"id": 101})])
    provider = DiscogsProvider(
        "x",
        session,
        resolver=public_dns,
        rate_limiter=DiscogsRateLimiter(0),
        cache=cache,
    )
    assert provider.get_release(101)["id"] == 101
    assert provider.get_release(101)["id"] == 101
    assert len(session.calls) == 1
    now[0] = 6
    assert provider.get_release(101)["id"] == 101
    assert len(session.calls) == 2


def test_discogs_cancellation_and_stale_result_stop_before_network():
    provider = DiscogsProvider(
        "x",
        FakeSession([]),
        resolver=public_dns,
        rate_limiter=DiscogsRateLimiter(0),
    )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(DiscogsProviderError, match="cancelled"):
        provider.get_release(101, cancel_event=cancelled)
    with pytest.raises(DiscogsProviderError, match="stale"):
        provider.get_release(101, stale_check=lambda: True)


def test_discogs_search_pagination_is_bounded():
    pages = [
        json_response({"results": [{"id": page}], "pagination": {"pages": 99}})
        for page in (1, 2, 3)
    ]
    session = FakeSession(pages)
    provider = DiscogsProvider(
        "x", session, resolver=public_dns, rate_limiter=DiscogsRateLimiter(0)
    )
    results = provider.search_catalogue(
        ProviderQuery("Signal Fire"), max_pages=99, max_results=50
    )
    assert [item["id"] for item in results] == [1, 2, 3]
    assert len(session.calls) == 3


def test_discogs_artist_search_uses_artist_term_not_track_term():
    session = FakeSession(
        [json_response({"results": [], "pagination": {"pages": 1}})]
    )
    provider = DiscogsProvider(
        "x", session, resolver=public_dns, rate_limiter=DiscogsRateLimiter(0)
    )
    provider.search_artists(ProviderQuery("Example Duo"))
    params = session.calls[0][1]["params"]
    assert params["artist"] == "Example Duo"
    assert "track" not in params


def test_ensemble_manual_and_confirmed_locks_beat_every_provider():
    for kwargs in ({"locked_fields": ["artist"]}, {"confirmed_locked_fields": ["artist"]}):
        result = build_metadata_ensemble(
            current={"title": "Signal Fire", "artist": "Manual Artist"},
            discogs_candidates=[provider_candidate(artist="Catalogue Artist")],
            **kwargs,
        )
        artist = result.field("artist")
        assert artist and artist.value == "Manual Artist"
        assert artist.confidence is ConfidenceLevel.LOCKED
        assert artist.action is FieldAction.KEEP


def test_ensemble_discogs_authority_agreement_disagreement_and_mb_fallback():
    mb_same = SimpleNamespace(
        title="Signal Fire", artist="Example Duo", provider_score=92, recording_id="mb-1"
    )
    agreement = build_metadata_ensemble(
        current={}, discogs_candidates=[provider_candidate()], musicbrainz_candidates=[mb_same]
    )
    assert "artist" in agreement.provider_agreement
    assert agreement.field("artist").source == "discogs"
    assert agreement.field("artist").score >= 95

    mb_conflict = SimpleNamespace(title="Signal Fire", artist="Other Artist", provider_score=95)
    conflict = build_metadata_ensemble(
        current={},
        discogs_candidates=[provider_candidate(provider_score=94)],
        musicbrainz_candidates=[mb_conflict],
    )
    assert conflict.field("artist").action is FieldAction.REVIEW
    assert conflict.field("artist").conflict

    mb_strong = SimpleNamespace(title="Signal Fire", artist="Secondary Artist", provider_score=96)
    weak_discogs = provider_candidate(artist="Weak Artist", provider_score=75)
    fallback = build_metadata_ensemble(
        current={}, discogs_candidates=[weak_discogs], musicbrainz_candidates=[mb_strong]
    )
    assert fallback.field("artist").source == "musicbrainz"

    mb_only = build_metadata_ensemble(current={}, musicbrainz_candidates=[mb_strong])
    assert mb_only.field("artist").source == "musicbrainz"


def test_ensemble_embedded_parsed_exclusive_and_uploader_are_ordered_fallbacks():
    embedded = build_metadata_ensemble(
        current={}, embedded={"artist": "Tagged Artist"}, uploader="Random Channel"
    )
    assert embedded.field("artist").source == "embedded"

    parsed = parse_youtube_title("Parsed Artist - Signal Fire")
    exclusive = build_metadata_ensemble(
        current={}, parsed_title=parsed, youtube_exclusive=True, uploader="Random Channel"
    )
    assert exclusive.field("artist").source == "youtube_title_parsed"
    assert exclusive.field("artist").safe_to_apply
    assert "youtube_exclusive_fallback" in exclusive.reasons

    uploader = build_metadata_ensemble(current={}, uploader="Random Channel")
    assert uploader.field("artist").source == "youtube_uploader_fallback"
    assert uploader.field("artist").action is FieldAction.REVIEW


def test_ensemble_version_conflict_and_unofficial_live_withhold_release_context():
    parsed = parse_youtube_title("Example Duo - Signal Fire [Live]")
    result = build_metadata_ensemble(
        current={},
        discogs_candidates=[provider_candidate(version_type="studio")],
        parsed_title=parsed,
        unofficial_live=True,
    )
    assert result.field("release_date").value is None
    assert result.field("album").value is None
    assert result.field("original_release_date").value == "1984"
    assert result.field("title").action is FieldAction.REVIEW
    assert "version_identity_conflict" in result.reasons


def test_recording_group_key_is_stable_informational_not_a_track_identity():
    first = recording_group_key("Signal Fire", "Example Duo", master_id="201")
    second = recording_group_key(" signal fire ", "EXAMPLE DUO", master_id="201")
    assert first == second
    assert first and first.startswith("rg1_")
    assert recording_group_key("", "Example Duo") is None


def test_artwork_gap_detection_is_exact_and_preserves_manual_locked_valid(tmp_path):
    valid = tmp_path / "valid.png"
    valid.write_bytes(image_bytes())
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not an image")
    missing = tmp_path / "missing.png"

    assert is_true_artwork_gap(None)
    assert is_true_artwork_gap(missing)
    assert is_true_artwork_gap(corrupt)
    assert is_true_artwork_gap(valid, placeholder=True)
    assert not is_true_artwork_gap(valid)
    assert not is_true_artwork_gap(None, manual=True)
    assert not is_true_artwork_gap(None, locked=True)


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://i.discogs.com/a.png", "https_required"),
        ("https://example.invalid/a.png", "host_rejected"),
        ("https://user@i.discogs.com/a.png", "userinfo_rejected"),
        ("https://i.discogs.com:444/a.png", "port_rejected"),
    ],
)
def test_discogs_artwork_url_boundary(url, error):
    with pytest.raises(DiscogsArtworkError, match=error):
        validate_discogs_image_url(url, resolve_dns=False)


def test_discogs_artwork_rejects_private_dns():
    def private_dns(host, port, *_args):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    with pytest.raises(DiscogsArtworkError, match="private_address"):
        validate_discogs_image_url(
            "https://i.discogs.com/a.png", resolver=private_dns
        )


def test_discogs_artwork_fetch_is_gap_only_hashed_private_and_attributed(tmp_path):
    png = image_bytes()
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "image/png", "Content-Length": str(len(png))},
                body=png,
            )
        ]
    )
    cache = DiscogsArtworkCache(
        tmp_path / "discogs", session=session, resolver=public_dns
    )
    record = cache.fetch_for_gap(
        artwork_candidate(),
        accepted_release_id="101",
        provider_score=96,
        current_cover_path=None,
    )
    assert record is not None
    assert record.path.parent == (tmp_path / "discogs").resolve()
    assert record.path.name == f"{record.sha256}.png"
    assert record.sha256 == __import__("hashlib").sha256(png).hexdigest()
    assert record.attribution_text == DISCOGS_ATTRIBUTION_TEXT
    assert record.attribution_state == ATTRIBUTION_STATE
    assert record.attribution_url == "https://www.discogs.com/release/101"
    assert (tmp_path / "discogs" / "index.json").is_file()
    headers = session.calls[0][1]["headers"]
    assert "Authorization" not in headers
    assert cache.attribution_for_release("101") == (
        DISCOGS_ATTRIBUTION_TEXT,
        "https://www.discogs.com/release/101",
    )


def test_discogs_artwork_cache_reuses_fresh_validated_entry(tmp_path):
    png = image_bytes()
    session = FakeSession([FakeResponse(headers={"Content-Type": "image/png"}, body=png)])
    cache = DiscogsArtworkCache(tmp_path, session=session, resolver=public_dns)
    first = cache.fetch_for_gap(
        artwork_candidate(), accepted_release_id="101", provider_score=96, current_cover_path=None
    )
    second = cache.fetch_for_gap(
        artwork_candidate(), accepted_release_id="101", provider_score=96, current_cover_path=None
    )
    assert first and second and second.from_cache
    assert first.path == second.path
    assert len(session.calls) == 1


def test_valid_existing_cover_stops_before_discogs_network(tmp_path):
    valid = tmp_path / "valid.png"
    valid.write_bytes(image_bytes())
    session = FakeSession([])
    cache = DiscogsArtworkCache(tmp_path / "cache", session=session, resolver=public_dns)
    assert (
        cache.fetch_for_gap(
            artwork_candidate(),
            accepted_release_id="101",
            provider_score=96,
            current_cover_path=valid,
        )
        is None
    )
    assert not session.calls


@pytest.mark.parametrize(
    ("candidate", "release_id", "score", "error"),
    [
        (artwork_candidate(image_type="secondary"), "101", 96, "front_required"),
        (artwork_candidate(catalogue_image=False), "101", 96, "catalogue_image_required"),
        (artwork_candidate(), "102", 96, "release_mismatch"),
        (artwork_candidate(), "101", 70, "match_not_confident"),
    ],
)
def test_discogs_artwork_requires_accepted_high_confidence_release_front(
    tmp_path, candidate, release_id, score, error
):
    cache = DiscogsArtworkCache(tmp_path, session=FakeSession([]), resolver=public_dns)
    with pytest.raises(DiscogsArtworkError, match=error):
        cache.fetch_for_gap(
            candidate,
            accepted_release_id=release_id,
            provider_score=score,
            current_cover_path=None,
        )


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (FakeResponse(headers={"Content-Type": "text/html"}, body=b"<html>"), "type_rejected"),
        (FakeResponse(headers={"Content-Type": "image/png"}, body=b"not-png"), "type_rejected"),
        (
            FakeResponse(
                headers={"Content-Type": "image/png", "Content-Length": str(9 * 1024 * 1024)}
            ),
            "response_too_large",
        ),
    ],
)
def test_discogs_artwork_rejects_invalid_or_oversized_image(tmp_path, response, error):
    cache = DiscogsArtworkCache(
        tmp_path, session=FakeSession([response]), resolver=public_dns
    )
    with pytest.raises(DiscogsArtworkError, match=error):
        cache.fetch_for_gap(
            artwork_candidate(),
            accepted_release_id="101",
            provider_score=96,
            current_cover_path=None,
        )


def test_discogs_artwork_redirect_is_revalidated_and_off_host_is_rejected(tmp_path):
    session = FakeSession(
        [FakeResponse(302, headers={"Location": "https://market.invalid/listing.png"})]
    )
    cache = DiscogsArtworkCache(tmp_path, session=session, resolver=public_dns)
    with pytest.raises(DiscogsArtworkError, match="host_rejected"):
        cache.fetch_for_gap(
            artwork_candidate(),
            accepted_release_id="101",
            provider_score=96,
            current_cover_path=None,
        )


def test_discogs_artwork_approved_redirect_retains_candidate_and_delivery_urls(tmp_path):
    png = image_bytes()
    source = "https://i.discogs.com/synthetic-front.png"
    delivery = "https://api-img.discogs.com/final-front.png"
    session = FakeSession(
        [
            FakeResponse(302, headers={"Location": delivery}),
            FakeResponse(headers={"Content-Type": "image/png"}, body=png),
        ]
    )
    cache = DiscogsArtworkCache(tmp_path, session=session, resolver=public_dns)
    first = cache.fetch_for_gap(
        artwork_candidate(source_url=source),
        accepted_release_id="101",
        provider_score=96,
        current_cover_path=None,
    )
    second = cache.fetch_for_gap(
        artwork_candidate(source_url=source),
        accepted_release_id="101",
        provider_score=96,
        current_cover_path=None,
    )
    assert first and second
    assert first.source_url == source
    assert first.delivery_url == delivery
    assert second.from_cache
    assert len(session.calls) == 2


def test_discogs_artwork_stale_entry_is_revalidated_with_new_fetch(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    png = image_bytes()
    session = FakeSession(
        [
            FakeResponse(headers={"Content-Type": "image/png"}, body=png),
            FakeResponse(headers={"Content-Type": "image/png"}, body=png),
        ]
    )
    cache = DiscogsArtworkCache(
        tmp_path, session=session, resolver=public_dns, clock=lambda: now[0]
    )
    first = cache.fetch_for_gap(
        artwork_candidate(), accepted_release_id="101", provider_score=96, current_cover_path=None
    )
    now[0] += timedelta(hours=7)
    second = cache.fetch_for_gap(
        artwork_candidate(), accepted_release_id="101", provider_score=96, current_cover_path=None
    )
    assert first and second
    assert first.fetched_at != second.fetched_at
    assert len(session.calls) == 2


def test_discogs_artwork_does_not_offer_any_media_embedding_operation():
    assert not hasattr(DiscogsArtworkCache, "embed")
    assert not hasattr(DiscogsArtworkCache, "write_tags")
