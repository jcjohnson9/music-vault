from __future__ import annotations

import json

import pytest
import requests

from music_vault.metadata.musicbrainz_enricher import (
    MUSICBRAINZ_USER_AGENT,
    MetadataProviderError,
    MusicBrainzProvider,
    _RateLimiter,
)


def _resolver(*_args):
    return [(None, None, None, None, ("93.184.216.34", 443))]


class _Response:
    def __init__(self, payload=None, *, status=200, content_type="application/json"):
        self.status_code = status
        self.payload = payload
        self.body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(self.body)),
        }
        self.closed = False

    def iter_content(self, chunk_size=0):
        yield self.body

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _provider(payload):
    session = _Session(_Response(payload))
    return (
        MusicBrainzProvider(
            session,
            resolver=_resolver,
            rate_limiter=_RateLimiter(0),
        ),
        session,
    )


def test_candidate_parser_keeps_all_release_identity_and_stable_score_order():
    provider, session = _provider(
        {
            "recordings": [
                {
                    "id": "recording-a",
                    "score": 91,
                    "title": "Synthetic Song",
                    "length": "245678",
                    "artist-credit": [
                        {"name": "Artist One", "joinphrase": " & "},
                        {"artist": {"name": "Artist Two"}},
                    ],
                    "releases": [
                        {
                            "id": "release-a",
                            "title": "Release A",
                            "date": "2001-02-03",
                            "country": "US",
                            "status": "Official",
                            "cover-art-archive": {"front": True},
                            "artist-credit": [
                                {"name": "Release Artist", "joinphrase": " feat. "},
                                {"artist": {"name": "Guest Artist"}},
                            ],
                        },
                        {"id": "release-b", "title": "Release B", "date": "2002"},
                    ],
                },
                {
                    "id": "recording-b",
                    "score": 99,
                    "title": "Higher Score",
                    "artist-credit": [{"name": "Solo"}],
                    "releases": [],
                },
            ]
        }
    )
    candidates = provider.search("Synthetic Song", "Artist One")
    assert [candidate.score for candidate in candidates] == [99, 91, 91]
    release = candidates[1]
    assert release.artist == "Artist One & Artist Two"
    assert release.duration_seconds == pytest.approx(245.678)
    assert release.album_artist == "Release Artist feat. Guest Artist"
    assert release.release_id == "release-a"
    assert release.release_date == "2001-02-03" and release.year == "2001"
    assert release.country == "US" and release.release_status == "Official"
    assert release.artwork_available is True
    assert session.trust_env is False
    assert session.calls[0][1]["headers"]["User-Agent"] == MUSICBRAINZ_USER_AGENT
    assert session.calls[0][1]["allow_redirects"] is False


def test_low_confidence_and_empty_candidate_release_are_explicit():
    provider, _session = _provider(
        {"recordings": [{"id": "id", "score": 40, "title": "Maybe", "releases": []}]}
    )
    candidate = provider.search("Maybe")[0]
    assert candidate.low_confidence
    assert candidate.album is None
    assert candidate.release_id is None
    assert candidate.artwork_available is None


def test_search_requires_explicit_nonempty_title():
    provider, session = _provider({"recordings": []})
    with pytest.raises(ValueError):
        provider.search("   ")
    assert session.calls == []


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_Response({}, status=302), "redirect_rejected"),
        (_Response({}, status=503), "unavailable"),
        (_Response({}, status=403), "request_rejected"),
        (_Response({}, content_type="text/html"), "response_rejected"),
    ],
)
def test_provider_failures_are_sanitized(response, error):
    provider = MusicBrainzProvider(
        _Session(response),
        resolver=_resolver,
        rate_limiter=_RateLimiter(0),
    )
    with pytest.raises(MetadataProviderError, match=error):
        provider.search("Synthetic")


def test_invalid_provider_json_is_sanitized():
    response = _Response(None)
    response.body = b"not-json"
    response.headers["Content-Length"] = str(len(response.body))
    provider = MusicBrainzProvider(
        _Session(response),
        resolver=_resolver,
        rate_limiter=_RateLimiter(0),
    )
    with pytest.raises(MetadataProviderError, match="response_invalid"):
        provider.search("Synthetic")


def test_malformed_provider_score_is_sanitized():
    provider, _session = _provider(
        {"recordings": [{"id": "id", "score": "private malformed value"}]}
    )
    with pytest.raises(MetadataProviderError, match="^musicbrainz_response_invalid$"):
        provider.search("Synthetic")


def test_provider_sanitizes_stream_failures():
    response = _Response({"recordings": []})

    def broken_stream(*_args, **_kwargs):
        raise requests.exceptions.ChunkedEncodingError("C:/private/path query=secret")

    response.iter_content = broken_stream
    provider = MusicBrainzProvider(
        _Session(response),
        resolver=_resolver,
        rate_limiter=_RateLimiter(0),
    )

    with pytest.raises(MetadataProviderError, match="^musicbrainz_request_failed$"):
        provider.search("Synthetic")
