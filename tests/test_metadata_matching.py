from __future__ import annotations

import json

import pytest

from music_vault.metadata.matching import (
    FieldConfidence,
    MatchCandidate,
    MatchClassification,
    TrackQuery,
    classify_candidates,
    clean_presentation_suffixes,
    extract_risk_qualifiers,
    normalize_for_comparison,
    normalize_query,
)


def _candidate(**changes):
    values = {
        "title": "Signal Bloom",
        "artist": "The North Lights",
        "album": "Synthetic Album",
        "album_artist": "The North Lights",
        "release_date": "2001-02-03",
        "duration_seconds": 201,
        "recording_id": "recording-a",
        "release_id": "release-a",
        "provider_score": 99,
        "provider": "MusicBrainz",
    }
    values.update(changes)
    return MatchCandidate(**values)


def _query(**changes):
    values = {
        "title": "Signal Bloom (Official Video)",
        "artist": "The North Lights",
        "duration_seconds": 200,
    }
    values.update(changes)
    return TrackQuery(**values)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Signal Bloom (Official Video)", "Signal Bloom"),
        ("Signal Bloom [Official Audio]", "Signal Bloom"),
        ("Signal Bloom | Lyrics", "Signal Bloom"),
        ("Signal Bloom (Official Video) [4K]", "Signal Bloom"),
        ("Signal Bloom (Live)", "Signal Bloom (Live)"),
        ("Signal Bloom (2011 Remaster)", "Signal Bloom (2011 Remaster)"),
        ("Signal Bloom - Acoustic Version", "Signal Bloom - Acoustic Version"),
    ],
)
def test_presentation_cleanup_is_conservative(source, expected):
    assert clean_presentation_suffixes(source) == expected


def test_comparison_normalization_handles_unicode_case_punctuation_and_spacing():
    assert normalize_for_comparison("  BEYONCÉ—Knowles  ") == "beyonce knowles"
    assert normalize_for_comparison("Beyonce Knowles") == "beyonce knowles"
    assert normalize_for_comparison("Cedar & Signal") == "cedar and signal"
    assert normalize_for_comparison("Cedar and Signal") == "cedar and signal"
    assert normalize_for_comparison("Don't Stop") == "dont stop"


def test_normalized_provider_query_removes_only_presentation_noise():
    query = normalize_query("Signal Bloom (Live) (Official Video)", "  Artist   Name ")
    assert query.title == "Signal Bloom (Live)"
    assert query.artist == "Artist Name"
    assert query.normalized_title == "signal bloom live"
    assert query.risk_flags == ("live",)
    assert extract_risk_qualifiers("Signal Bloom (Official Video)") == ()


def test_strict_high_confidence_requires_every_gate_and_is_serializable():
    result = classify_candidates(_query(), [_candidate()])
    assert result.classification is MatchClassification.HIGH_CONFIDENCE
    assert result.is_high_confidence
    assert result.selected is not None
    assert result.selected.title_exact and result.selected.artist_exact
    assert result.selected.duration_delta_seconds == 1
    assert result.reasons == ("strict_high_confidence_match",)

    sanitized = result.to_dict()
    assert sanitized["classification"] == "high_confidence"
    assert "title" not in sanitized["assessments"][0]["candidate"]
    assert sanitized["assessments"][0]["candidate"]["has_title"] is True
    json.dumps(sanitized)
    assert result.to_dict(include_values=True)["assessments"][0]["candidate"]["title"] == "Signal Bloom"


@pytest.mark.parametrize(
    ("candidate_changes", "reason"),
    [
        ({"provider_score": 94}, "provider_score_below_high_confidence"),
        ({"duration_seconds": None}, "duration_unavailable"),
        ({"duration_seconds": 206.1}, "duration_conflict"),
        ({"title": "Different Song"}, "title_not_exact"),
        ({"artist": "Different Artist"}, "artist_not_exact"),
    ],
)
def test_failed_strict_gates_route_to_review(candidate_changes, reason):
    result = classify_candidates(_query(), [_candidate(**candidate_changes)])
    expected = (
        MatchClassification.NO_MATCH
        if reason in {"title_not_exact", "artist_not_exact"}
        else MatchClassification.REVIEW
    )
    assert result.classification is expected
    assert reason in result.selected.reasons
    if expected is MatchClassification.NEEDS_REVIEW:
        assert result.to_dict()["classification"] == "needs_review"


def test_close_distinct_candidates_are_ambiguous_but_a_clear_margin_is_unique():
    close = classify_candidates(
        _query(),
        [
            _candidate(provider_score=99, recording_id="recording-a"),
            _candidate(provider_score=96, recording_id="recording-b", release_id="release-b"),
        ],
    )
    assert close.classification is MatchClassification.AMBIGUOUS
    assert close.unique_margin == 3
    assert close.reasons == ("candidate_not_unique",)

    clear = classify_candidates(
        _query(),
        [
            _candidate(provider_score=99, recording_id="recording-a"),
            _candidate(provider_score=90, recording_id="recording-b", release_id="release-b"),
        ],
    )
    assert clear.classification is MatchClassification.HIGH_CONFIDENCE
    assert clear.unique_margin == 9


def test_duplicate_unidentified_rows_are_not_treated_as_proven_unique():
    first = _candidate(recording_id=None, release_id=None, provider_score=99)
    second = _candidate(recording_id=None, release_id=None, provider_score=98)
    result = classify_candidates(_query(), [first, second])
    assert result.classification is MatchClassification.AMBIGUOUS


def test_multiple_releases_for_one_recording_do_not_make_identity_ambiguous():
    result = classify_candidates(
        _query(),
        [
            _candidate(release_id="release-a", album="Release A", provider_score=99),
            _candidate(release_id="release-b", album="Release B", provider_score=99),
        ],
    )
    assert result.classification is MatchClassification.HIGH_CONFIDENCE
    assert result.unique_margin is None
    assert result.selected.field_decision("album").confidence is FieldConfidence.REVIEW
    assert result.selected.field_decision("release_date").confidence is FieldConfidence.REVIEW
    assert result.selected.field_decision("album").reason == "release_identity_ambiguous"


def test_clear_release_preference_keeps_selected_release_field_confidence():
    result = classify_candidates(
        _query(),
        [
            _candidate(release_id="release-a", provider_score=99),
            _candidate(release_id="release-b", provider_score=90),
        ],
    )
    assert result.classification is MatchClassification.HIGH_CONFIDENCE
    assert result.selected.field_decision("album").confidence is FieldConfidence.HIGH

    existing = classify_candidates(
        _query(release_id="release-b"),
        [
            _candidate(release_id="release-a", provider_score=99),
            _candidate(release_id="release-b", provider_score=99),
        ],
    )
    assert existing.selected.candidate.release_id == "release-b"
    assert existing.selected.field_decision("release_date").confidence is not FieldConfidence.REVIEW


def test_risky_version_and_existing_identity_conflict_block_automatic_match():
    risky = classify_candidates(
        _query(title="Signal Bloom (Live)"),
        [_candidate(title="Signal Bloom (Live)")],
    )
    assert risky.classification is MatchClassification.REVIEW
    assert risky.selected.risk_flags == ("live",)
    assert "version_risk_present" in risky.reasons

    conflicting = classify_candidates(
        _query(recording_id="known-recording"),
        [_candidate(recording_id="other-recording")],
    )
    assert conflicting.classification is MatchClassification.REVIEW
    assert "identity_conflict" in conflicting.reasons


@pytest.mark.parametrize(
    "qualifier",
    [
        "Mashup",
        "Game Edit",
        "Theme",
        "Unreleased",
        "Fan Upload",
        "Parody",
        "Re-recorded",
        "Radio Version",
        "Edit",
        "Mix",
        "Extended",
        "Fan-Made",
        "Bootleg",
        "Unofficial",
        "2024 Version",
    ],
)
def test_extended_version_risk_vocabulary_never_auto_approves(qualifier):
    title = f"Signal Bloom ({qualifier})"
    result = classify_candidates(_query(title=title), [_candidate(title=title)])
    assert result.classification is MatchClassification.NEEDS_REVIEW
    assert "version_risk_present" in result.reasons


def test_version_risk_anywhere_in_title_never_auto_approves():
    title = "Live at Wembley Signal Bloom"
    result = classify_candidates(_query(title=title), [_candidate(title=title)])
    assert result.classification is MatchClassification.NEEDS_REVIEW
    assert result.selected.risk_flags == ("live",)


def test_missing_stable_recording_identity_never_auto_approves():
    result = classify_candidates(_query(), [_candidate(recording_id=None)])
    assert result.classification is MatchClassification.NEEDS_REVIEW


def test_release_field_confidence_handles_precision_missing_conflict_and_invalid():
    precise = classify_candidates(
        _query(release_date="2001"),
        [_candidate(release_date="2001-02-03")],
    ).selected.field_decision("release_date")
    assert precise.confidence is FieldConfidence.COMPATIBLE
    assert precise.safe_to_apply

    missing = classify_candidates(
        _query(release_date=None), [_candidate(release_date="2001")]
    ).selected.field_decision("release_date")
    assert missing.confidence is FieldConfidence.HIGH
    assert missing.safe_to_apply

    conflict = classify_candidates(
        _query(release_date="2002"), [_candidate(release_date="2001")]
    )
    assert conflict.classification is MatchClassification.HIGH_CONFIDENCE
    assert conflict.selected.field_decision("release_date").confidence is FieldConfidence.CONFLICT

    invalid = classify_candidates(
        _query(release_date=None), [_candidate(release_date="2001-99")]
    )
    assert invalid.selected.field_decision("release_date").reason == "candidate_date_invalid"


def test_locked_or_incomplete_queries_are_skipped_and_no_candidates_are_no_match():
    locked = classify_candidates(_query(), [_candidate()], locked_fields=frozenset({"title"}))
    assert locked.classification is MatchClassification.SKIPPED
    assert locked.reasons == ("authoritative_lock_present",)
    assert locked.selected.field_decision("title").confidence is FieldConfidence.LOCKED
    assert classify_candidates(
        _query(), [], locked_fields=frozenset({"title"})
    ).classification is MatchClassification.SKIPPED

    incomplete = classify_candidates(TrackQuery(title="Signal", artist=None), [_candidate()])
    assert incomplete.classification is MatchClassification.SKIPPED
    assert incomplete.reasons == ("missing_required_query_metadata",)

    absent = classify_candidates(_query(), [])
    assert absent.classification is MatchClassification.NO_MATCH
    assert absent.reasons == ("no_candidates",)


def test_unrelated_candidates_are_no_match_and_reasons_never_embed_values():
    result = classify_candidates(
        _query(),
        [_candidate(title="Completely Elsewhere", artist="Unrelated Person")],
    )
    assert result.classification is MatchClassification.NO_MATCH
    assert result.reasons == ("no_viable_candidates",)
    encoded = json.dumps(result.to_dict())
    assert "Signal Bloom" not in encoded
    assert "Unrelated Person" not in encoded
    assert all(" " not in reason for reason in result.selected.reasons)
