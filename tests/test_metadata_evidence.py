from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from music_vault.metadata.evidence import (
    CreditEvidence, DateAssertion, FieldAssertion, MetadataEvidence,
    ProviderIdentity, normalize_candidate,
)
from music_vault.metadata.musicbrainz_enricher import MetadataCandidate, MusicBrainzProvider
from music_vault.metadata.providers import ProviderArtistCredit, ProviderReleaseCandidate
from music_vault.metadata.providers.discogs import parse_discogs_artist_credits


def _musicbrainz_candidate():
    return MetadataCandidate(
        "Synthetic Song", "Stage Name & Guest", "Synthetic Album", "2020-02",
        "recording-1", "release-1", 96,
        release_group_id="family-1", original_release_date="1994",
        release_group_first_release_date="1995-03-02",
        artist_credits=(
            ProviderArtistCredit("Stage Name", artist_id="artist-1", join_phrase=" & ",
                                 provider="musicbrainz", canonical_name="Canonical Artist", credited_as="Stage Name"),
            ProviderArtistCredit("Guest", artist_id="artist-2", provider="musicbrainz", canonical_name="Guest"),
        ),
        album_artist_credits=(ProviderArtistCredit("Album Ensemble", artist_id="ensemble-1", provider="musicbrainz", canonical_name="Album Ensemble"),),
    )


def test_legacy_candidate_positional_contracts_remain_compatible():
    candidate = MetadataCandidate("Title", "Artist", None, None, "recording-1", None, 91)
    assert candidate.artist_credits == ()
    assert normalize_candidate(candidate).recording == ProviderIdentity("musicbrainz", "recording", "recording-1")
    credit = ProviderArtistCredit("Name", "primary", "123", " & ", "unknown", None)
    assert credit.provider is None


def test_normalized_identity_dates_and_credit_names_are_distinct():
    evidence = normalize_candidate(_musicbrainz_candidate())
    assert evidence.recording.entity_id == "recording-1"
    assert evidence.edition.entity_id == "release-1"
    assert evidence.release_family.entity_id == "family-1"
    assert [(item.value, item.precision, item.meaning, item.subject.entity_kind) for item in evidence.dates] == [
        ("2020-02", "month", "edition_release", "edition"),
        ("1994", "year", "recording_first_release", "recording"),
        ("1995-03-02", "day", "family_first_release", "release_family"),
    ]
    first, guest = evidence.recording_credits
    assert (first.canonical_name, first.credited_as) == ("Canonical Artist", "Stage Name")
    assert first.identities == (ProviderIdentity("musicbrainz", "artist", "artist-1"),)
    assert (first.prefix_join, guest.prefix_join) == ("", " & ")
    assert evidence.release_credits[0].credited_as == "Album Ensemble"


def test_round_trip_stable_hash_and_immutable_collections():
    evidence = normalize_candidate(_musicbrainz_candidate())
    encoded = json.loads(json.dumps(evidence.to_dict()))
    restored = MetadataEvidence.from_dict(encoded)
    assert restored == evidence
    assert restored.evidence_key == normalize_candidate(_musicbrainz_candidate()).evidence_key
    assert isinstance(restored.recording_credits, tuple)
    with pytest.raises(FrozenInstanceError):
        restored.provider = "discogs"
    encoded["fields"][0]["value"] = "Tampered"
    with pytest.raises(ValueError, match="identity mismatch"):
        MetadataEvidence.from_dict(encoded)


def test_discogs_anv_preserves_entity_name_and_master_year_scope():
    credits = parse_discogs_artist_credits([
        {"id": 15, "name": "Canonical Artist (2)", "anv": "Stage Name", "join": " feat. "},
        {"id": 16, "name": "Second Artist"},
    ])
    assert (credits[0].name, credits[0].canonical_name, credits[0].credited_as) == ("Stage Name", "Canonical Artist", "Stage Name")
    candidate = ProviderReleaseCandidate("Discogs", "Song", "Stage Name feat. Second Artist", artist_credits=credits,
                                         release_id="20", master_id="10", release_date="2020", original_release_date="1990")
    evidence = normalize_candidate(candidate)
    assert evidence.recording is None
    assert evidence.release_family == ProviderIdentity("discogs", "release_family", "10")
    assert evidence.dates[1].meaning == "family_first_release"
    assert evidence.recording_credits[1].role_basis == "provider_join"
    assert evidence.recording_credits[1].prefix_join == " feat. "
    accepted = candidate.accepted_metadata()
    assert accepted["artist_credits"][0]["canonical_name"] == "Canonical Artist"
    assert accepted["artist_credits"][0]["provider"] == "discogs"


def test_parser_keeps_musicbrainz_structured_credits_without_network(monkeypatch):
    provider = MusicBrainzProvider()
    monkeypatch.setattr(provider, "_payload", lambda *_args: {"recordings": [{
        "id": "recording-1", "title": "Song", "score": 95, "first-release-date": "1982",
        "artist-credit": [{"name": "Alias", "artist": {"id": "artist-1", "name": "Canonical", "type": "Person"}, "joinphrase": " & "},
                          {"artist": {"id": "group-1", "name": "A & B", "type": "Group"}}],
        "releases": [{"id": "release-1", "title": "Album", "date": "2000",
                      "release-group": {"id": "family-1", "first-release-date": "1983"},
                      "artist-credit": [{"artist": {"id": "album-artist-1", "name": "Album Artist"}}]}],
    }]})
    candidate = provider.search("Song")[0]
    assert candidate.artist == "Alias & A & B"
    evidence = normalize_candidate(candidate)
    assert len(evidence.recording_credits) == 2  # Group name is never split.
    assert evidence.recording_credits[0].canonical_name == "Canonical"
    assert evidence.recording_credits[1].role == "primary"  # No inferred featured/collaborator role.
    assert evidence.recording_credits[1].entity_type == "group"
    assert evidence.release_credits[0].identities[0].entity_id == "album-artist-1"


def test_unknown_provider_does_not_fabricate_identity_or_original_date():
    evidence = normalize_candidate({"title": "Song", "artist": "Name", "recording_id": "12", "release_id": "13", "original_release_date": "1980"})
    assert evidence.provider == "unknown"
    assert evidence.recording is evidence.edition is evidence.release_family is None
    assert evidence.dates == ()


def test_missing_canonical_name_stays_unknown_and_same_names_keep_distinct_ids():
    candidate = _musicbrainz_candidate()
    candidate = replace(candidate, artist_credits=(ProviderArtistCredit("Same", artist_id="one"), ProviderArtistCredit("Same", artist_id="two")))
    evidence = normalize_candidate(candidate)
    assert evidence.recording_credits[0].canonical_name is None
    assert evidence.recording_credits[0].identities != evidence.recording_credits[1].identities


def test_slash_join_is_valid_and_converted_once():
    candidate = replace(_musicbrainz_candidate(), artist_credits=(ProviderArtistCredit("One", join_phrase="/"), ProviderArtistCredit("Two")))
    evidence = normalize_candidate(candidate)
    assert evidence.recording_credits[1].prefix_join == "/"
    assert MetadataEvidence.from_dict(evidence.to_dict()).recording_credits[1].prefix_join == "/"


@pytest.mark.parametrize("invalid", ["../secret", "https://example.test/?token=private", "C:\\private\\data", "x" * 129, True])
def test_identity_rejects_unbounded_or_path_values(invalid):
    with pytest.raises(ValueError):
        ProviderIdentity("musicbrainz", "recording", invalid)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, 86401])
def test_duration_must_be_finite_bounded_number(value):
    with pytest.raises(ValueError):
        FieldAssertion("duration_seconds", value)


@pytest.mark.parametrize("invalid", [
    {"raw_payload": {"token": "not-stored"}},
    {"schema_version": 2},
    {"reference": "https://musicbrainz.org/recording/id?token=not-stored"},
    {"fields": [{"field_name": "api_key", "value": "not-stored"}]},
    {"fields": [{"field_name": "title", "value": "C:\\private\\track.mp3"}]},
    {"fields": [{"field_name": "title", "value": "x" * 513}]},
    {"recording_credits": [{"credited_as": "Name", "canonical_name": None, "order": 3}]},
])
def test_deserialization_rejects_unknown_or_unsafe_payload(invalid):
    payload = normalize_candidate(_musicbrainz_candidate()).to_dict()
    payload.pop("evidence_key")
    payload.update(invalid)
    with pytest.raises(ValueError):
        MetadataEvidence.from_dict(payload)


def test_date_and_credit_namespace_validation():
    with pytest.raises(ValueError):
        DateAssertion("1990", "year", "recording_first_release", ProviderIdentity("discogs", "edition", "1"))
    with pytest.raises(ValueError):
        normalize_candidate(replace(_musicbrainz_candidate(), artist_credits=(ProviderArtistCredit("Name", artist_id="123", provider="discogs"),)))
    with pytest.raises(ValueError):
        CreditEvidence(None, "Name", identities=(ProviderIdentity("discogs", "artist", "1"), ProviderIdentity("discogs", "artist", "2")))


def test_raw_candidate_extras_are_not_serialized():
    candidate = {"provider": "Discogs", "title": "Song", "artist": "Artist", "release_id": "123",
                 "token": "not-stored", "raw_payload": {"authorization": "not-stored"},
                 "provider_reference": "https://evil.invalid/private?token=not-stored"}
    encoded = json.dumps(normalize_candidate(candidate).to_dict())
    assert "not-stored" not in encoded and "evil.invalid" not in encoded
    assert "https://www.discogs.com/release/123" in encoded
