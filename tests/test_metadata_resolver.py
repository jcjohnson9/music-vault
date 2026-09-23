from dataclasses import FrozenInstanceError, replace

import pytest

from music_vault.metadata.ensemble import ConfidenceLevel, FieldAction, FieldResolution, MetadataEnsemble
from music_vault.metadata.evidence import CreditEvidence, MetadataEvidence, ProviderIdentity
from music_vault.metadata.providers import ProviderArtistCredit
from music_vault.metadata.resolver import resolve_metadata
from music_vault.metadata.uploader_classifier import classify_uploader


def field(name="title", value="Synthetic title", source="discogs", **changes):
    return replace(FieldResolution(name, None, value, source, ConfidenceLevel.HIGH, 95, FieldAction.APPLY), **changes)


def ensemble(*fields, reasons=()):
    return MetadataEnsemble(tuple(fields), None, None, None, classify_uploader(None), (), (), reasons, None)


def evidence(provider="discogs", *, kind="edition", identity="edition-1", primary=True):
    return MetadataEvidence(
        provider, "synthetic-v1", **{kind: ProviderIdentity(provider, kind, identity)},
        recording_credits=(CreditEvidence("Synthetic Artist", "Synthetic Artist", role="primary" if primary else "featured"),),
    )


def resolve(**changes):
    args = dict(track_id=7, expected_fingerprint="baseline-1", current_values={}, locked_fields=frozenset(), protected_credits=False,
                ensemble=ensemble(field()), evidence=(evidence(),))
    args.update(changes)
    return resolve_metadata(**args)


def test_proposal_is_frozen_stable_and_tracks_full_decision_fingerprint():
    proposal = resolve()
    assert proposal.field_names == ("title",)
    assert proposal.allows_provider("discogs")
    assert not proposal.allows_provider("musicbrainz")
    assert proposal == resolve()
    assert proposal.proposal_key != resolve(expected_fingerprint="baseline-2").proposal_key
    assert proposal.proposal_key != resolve(current_values={"album": "Synthetic album"}).proposal_key
    assert proposal.proposal_key != resolve(locked_fields=frozenset({"title"})).proposal_key
    with pytest.raises(FrozenInstanceError):
        proposal.track_id = 9


@pytest.mark.parametrize("changes", [
    {"score": 59.99}, {"action": FieldAction.REVIEW}, {"conflict": True},
    {"value": ""}, {"value": "  "}, {"value": None},
    {"field_name": "source_upload_date"}, {"field_name": "musicbrainz_recording_id"}, {"field_name": "artwork"},
])
def test_existing_field_gates_are_not_upgraded(changes):
    assert resolve(ensemble=ensemble(field(**changes))).field_names == ()


def test_medium_threshold_and_keep_action_preserve_existing_service_policy():
    assert resolve(ensemble=ensemble(field(score=60, action=FieldAction.KEEP, confidence=ConfidenceLevel.MEDIUM))).field_names == ("title",)


@pytest.mark.parametrize("provider,kind,column", [
    ("discogs", "edition", "discogs_release_id"),
    ("discogs", "release_family", "discogs_master_id"),
    ("musicbrainz", "recording", "musicbrainz_recording_id"),
    ("musicbrainz", "edition", "musicbrainz_release_id"),
    ("musicbrainz", "release_family", "musicbrainz_release_group_id"),
])
def test_identity_conflict_blocks_only_that_provider(provider, kind, column):
    other = "musicbrainz" if provider == "discogs" else "discogs"
    proposal = resolve(
        current_values={column: "known-1", "title": "Synthetic title"},
        evidence=(evidence(provider, kind=kind), evidence(other)),
        ensemble=ensemble(field(source=provider), field("album", "Other synthetic album", other), field("artist_credits", ("credit",), provider)),
    )
    assert proposal.blocked_providers == (provider,)
    assert proposal.field_names == ("album",)
    assert proposal.credits_provider is None
    assert not proposal.allows_provider(provider)
    assert proposal.allows_provider(other)
    assert "known-1" not in str(proposal.blocked_reasons)
    assert "Synthetic" not in str(proposal.blocked_reasons)


def test_equal_numeric_discogs_id_is_not_a_conflict_and_blank_is_unknown():
    assert resolve(current_values={"discogs_release_id": 12}, evidence=(evidence(identity="12"),)).blocked_providers == ()
    assert resolve(current_values={"discogs_release_id": ""}).blocked_providers == ()
    assert resolve(current_values={"musicbrainz_release_id": "different"}).blocked_providers == ()


def test_disagreeing_same_provider_evidence_is_blocked_even_without_current_id():
    proposal = resolve(evidence=(evidence(identity="edition-1"), evidence(identity="edition-2")))
    assert proposal.blocked_providers == ("discogs",)
    assert proposal.field_names == ()


def test_missing_evidence_cannot_authorize_catalogue_fields_or_credits():
    proposal = resolve(evidence=(), ensemble=ensemble(field(), field("artist_credits", ("credit",))))
    assert proposal.field_names == ()
    assert proposal.credits_provider is None
    assert proposal.blocked_reasons == ("discogs_evidence_missing",)


def test_rejected_duration_provider_cannot_write_and_other_provider_survives():
    proposal = resolve(rejected_providers=frozenset({"discogs"}), evidence=(evidence(), evidence("musicbrainz")),
                       ensemble=ensemble(field(), field("album", "Synthetic album", "musicbrainz")))
    assert proposal.field_names == ("album",)
    assert not proposal.allows_provider("discogs")


def test_version_conflict_blocks_catalogues_but_does_not_promote_source_fallback():
    proposal = resolve(evidence=(evidence(), evidence("musicbrainz")), ensemble=ensemble(
        field(), field("album", "Synthetic album", "musicbrainz"),
        field("artist", "Source performer", "youtube_title_parsed", score=59), reasons=("version_identity_conflict",)))
    assert proposal.blocked_providers == ("discogs", "musicbrainz")
    assert proposal.field_names == ()


@pytest.mark.parametrize("locked,protected", [(frozenset({"artist"}), False), (frozenset(), True)])
def test_blank_artist_lock_and_protected_credits_are_authoritative(locked, protected):
    proposal = resolve(current_values={"artist": ""}, locked_fields=locked, protected_credits=protected,
                       ensemble=ensemble(field("artist", "New performer"), field("artist_credits", ("credit",))))
    assert proposal.field_names == ()
    assert proposal.credits_provider is None
    assert not proposal.allow_source_credits


@pytest.mark.parametrize("provider", ["discogs", "musicbrainz"])
def test_credits_use_only_qualified_ensemble_selected_provider_with_primary_evidence(provider):
    proposal = resolve(evidence=(evidence(provider),), ensemble=ensemble(field("artist_credits", ("credit",), provider)))
    assert proposal.credits_provider == provider
    assert not proposal.allow_source_credits
    assert resolve(evidence=(evidence(provider, primary=False),), ensemble=ensemble(field("artist_credits", ("credit",), provider))).credits_provider is None


@pytest.mark.parametrize("changes", [{"score": 59}, {"action": FieldAction.REVIEW}, {"conflict": True}])
def test_credit_confidence_review_and_conflict_are_preserved(changes):
    proposal = resolve(ensemble=ensemble(field("artist_credits", ("credit",), **changes)))
    assert proposal.credits_provider is None


def test_parsed_source_credit_fallback_requires_selected_qualified_sequence():
    credits = (ProviderArtistCredit("Synthetic Primary"), ProviderArtistCredit("Synthetic Guest", role="featured"))
    selected = ensemble(field("artist_credits", credits, "youtube_title_parsed", score=72))
    proposal = resolve(ensemble=selected)
    assert proposal.allow_source_credits
    assert proposal.credits_provider is None
    assert not resolve(ensemble=selected, protected_credits=True).allow_source_credits
    assert not resolve(ensemble=selected, locked_fields=frozenset({"artist_credits"})).allow_source_credits
    assert not resolve(ensemble=ensemble(field("artist_credits", "not a sequence", "youtube_title_parsed"))).allow_source_credits


def test_hash_canonicalizes_mapping_order_and_lock_set_order():
    assert resolve(current_values={"title": "A", "album": "B"}, locked_fields=frozenset({"artist", "album"})).proposal_key == resolve(
        current_values={"album": "B", "title": "A"}, locked_fields=frozenset({"album", "artist"})).proposal_key


def test_reasons_and_evidence_change_proposal_key_without_mutating_inputs():
    values = {"title": "Synthetic current"}
    original = dict(values)
    first = resolve(current_values=values)
    assert values == original
    assert first.proposal_key != resolve(current_values=values, ensemble=ensemble(field(), reasons=("new_reason",))).proposal_key
    assert first.proposal_key != resolve(current_values=values, evidence=(evidence(identity="edition-2"),)).proposal_key


@pytest.mark.parametrize("changes", [{"track_id": 0}, {"track_id": True}, {"expected_fingerprint": ""}, {"evidence": []}, {"rejected_providers": frozenset({"arbitrary"})}])
def test_invalid_proposal_inputs_fail_closed(changes):
    with pytest.raises(ValueError):
        resolve(**changes)
