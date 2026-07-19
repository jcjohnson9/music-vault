from __future__ import annotations

from types import SimpleNamespace

import pytest

from music_vault.metadata.title_orientation import (
    assess_orientation,
    choose_orientation,
    normalize_orientation,
)
from music_vault.metadata.title_parser import (
    parse_youtube_title,
    title_orientation_hypotheses,
)


def _candidate(**changes):
    values = {
        "provider": "discogs",
        "title": "Synthetic Anthem",
        "artist": "The Cosmic Assembly",
        "provider_score": 94.0,
        "duration_seconds": 240.0,
        "version_type": "studio",
        "original_release_date": "1978",
        "release_id": "synthetic-release",
        "reasons": ("exact_tracklist_title", "exact_artist_credit"),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _dual(raw: str = "The Cosmic Assembly - Synthetic Anthem (1978)"):
    return title_orientation_hypotheses(parse_youtube_title(raw))


def test_dash_parser_returns_two_typed_bounded_orientations():
    raw = "The Cosmic Assembly - Synthetic Anthem (1978)"
    parsed = parse_youtube_title(raw)
    hypotheses = title_orientation_hypotheses(parsed)

    assert parsed.raw_title == raw
    assert [item.orientation for item in hypotheses] == [
        "left_is_artist",
        "right_is_artist",
    ]
    assert [(item.artist, item.title) for item in hypotheses] == [
        ("The Cosmic Assembly", "Synthetic Anthem"),
        ("Synthetic Anthem", "The Cosmic Assembly"),
    ]
    assert all(item.year_hint == 1978 for item in hypotheses)
    assert all(item.source_pattern == "artist_dash_title" for item in hypotheses)
    assert all(item.confidence_reasons for item in hypotheses)


@pytest.mark.parametrize("separator", (" – ", " — "))
def test_unicode_dash_parser_returns_two_orientations(separator: str):
    hypotheses = _dual(f"The Cosmic Assembly{separator}Synthetic Anthem")

    assert len(hypotheses) == 2
    assert hypotheses[1].orientation == "right_is_artist"


def test_title_by_artist_stays_unambiguous_and_typed():
    parsed = parse_youtube_title("Synthetic Anthem by The Cosmic Assembly (1978)")
    hypotheses = title_orientation_hypotheses(parsed)

    assert len(hypotheses) == 1
    assert hypotheses[0].orientation == "title_by_artist"
    assert hypotheses[0].year_hint == 1978
    assert hypotheses[0].source_pattern == "title_by_artist"


def test_version_and_featured_clues_are_preserved_on_both_orientations():
    parsed = parse_youtube_title(
        "The Cosmic Assembly feat. Guest Unit - Synthetic Anthem - Live"
    )
    hypotheses = title_orientation_hypotheses(parsed)

    assert len(hypotheses) == 2
    assert all(item.version_type == "live" for item in hypotheses)
    assert all(item.version_label == "Live" for item in hypotheses)
    assert all(item.featured_artist == "Guest Unit" for item in hypotheses)
    assert (hypotheses[1].artist, hypotheses[1].title) == (
        "Synthetic Anthem",
        "The Cosmic Assembly",
    )


@pytest.mark.parametrize(
    "raw",
    (
        "Twenty-One Pilots",
        "-12",
        "1978 - 1980",
        "The Cosmic Assembly - Synthetic Anthem - Alternate Name",
    ),
)
def test_unsafe_or_ambiguous_dashes_do_not_create_orientation_searches(raw: str):
    assert title_orientation_hypotheses(parse_youtube_title(raw)) == ()


def test_recognized_trailing_version_is_the_only_allowed_second_dash():
    parsed = parse_youtube_title("Synthetic Anthem - The Cosmic Assembly - Live")
    hypotheses = title_orientation_hypotheses(parsed)

    assert len(hypotheses) == 2
    assert parsed.version_type == "live"
    assert [(item.artist, item.title) for item in hypotheses] == [
        ("Synthetic Anthem", "The Cosmic Assembly"),
        ("The Cosmic Assembly", "Synthetic Anthem"),
    ]


def test_for_orientation_changes_hints_without_losing_source_provenance():
    raw = "Synthetic Anthem - The Cosmic Assembly (1978) [Official Audio]"
    parsed = parse_youtube_title(raw)
    selected = parsed.for_orientation(title_orientation_hypotheses(parsed)[1])

    assert (selected.artist_hint, selected.title_hint) == (
        "The Cosmic Assembly",
        "Synthetic Anthem",
    )
    assert selected.raw_title == raw
    assert selected.year_hint == 1978
    assert selected.presentation_suffixes == ("Official Audio",)
    assert len(selected.orientation_hypotheses) == 2


def test_old_orientation_names_normalize_for_saved_evidence_compatibility():
    assert normalize_orientation("artist_then_title") == "left_is_artist"
    assert normalize_orientation("title_then_artist") == "right_is_artist"


def test_exact_discogs_candidate_is_coherent_conclusive_and_safe_to_serialize():
    hypothesis = _dual()[0]
    candidate = _candidate()

    assessment = assess_orientation(
        hypothesis, candidate, provider="discogs", local_duration=241.0
    )
    payload = assessment.to_dict()

    assert assessment.coherent
    assert assessment.conclusive
    assert assessment.orientation == "left_is_artist"
    assert assessment.tracklist_match
    assert "duration_match" in assessment.reasons
    assert "year_hint_match" in assessment.reasons
    assert candidate.title not in repr(payload)
    assert candidate.artist not in repr(payload)


def test_duration_mismatch_rejects_an_orientation():
    assessment = assess_orientation(
        _dual()[0],
        _candidate(duration_seconds=400.0),
        provider="discogs",
        local_duration=240.0,
    )

    assert not assessment.coherent
    assert "duration_mismatch" in assessment.reasons


def test_explicit_version_conflict_rejects_an_orientation():
    hypothesis = _dual("The Cosmic Assembly - Synthetic Anthem - Live")[0]
    assessment = assess_orientation(
        hypothesis,
        _candidate(version_type="studio"),
        provider="discogs",
        local_duration=240.0,
    )

    assert not assessment.coherent
    assert "version_conflict" in assessment.reasons


def test_matching_year_improves_orientation_assessment():
    hypothesis = _dual()[0]
    matching = assess_orientation(
        hypothesis, _candidate(original_release_date="1978"), provider="discogs"
    )
    conflicting = assess_orientation(
        hypothesis, _candidate(original_release_date="1989"), provider="discogs"
    )

    assert matching.year_match is True
    assert conflicting.year_match is False
    assert matching.score > conflicting.score


def test_conclusive_first_discogs_orientation_stops_with_one_evaluation():
    left, right = _dual()
    decision = choose_orientation(
        (left, right), {left.orientation: _candidate()}, local_duration=240.0
    )

    assert decision.selected_orientation == "left_is_artist"
    assert decision.evaluated_count == 1
    assert decision.discogs_queries == 1
    assert decision.provider_confirmed
    assert not decision.requires_provider_adjudication


def test_weak_first_discogs_orientation_requires_reverse_adjudication():
    left, right = _dual()
    weak = _candidate(provider_score=70.0, reasons=())
    decision = choose_orientation((left, right), {left.orientation: weak})

    assert decision.selected is None
    assert decision.requires_provider_adjudication
    assert decision.discogs_queries == 1


def test_reverse_exact_discogs_orientation_wins_after_bounded_second_search():
    left, right = _dual("Synthetic Anthem - The Cosmic Assembly (1978)")
    provider_match = _candidate()
    decision = choose_orientation(
        (left, right),
        {
            left.orientation: provider_match,
            right.orientation: provider_match,
        },
        local_duration=240.0,
    )

    assert decision.selected_orientation == "right_is_artist"
    assert decision.discogs_queries == 2
    assert decision.evaluated_count == 2
    assert decision.selected_candidate is provider_match


def test_discogs_remains_primary_over_conflicting_musicbrainz():
    left, right = _dual()
    discogs = _candidate()
    conflicting_mb = _candidate(
        provider="MusicBrainz",
        title=right.title,
        artist=right.artist,
        provider_score=99.0,
        reasons=(),
    )
    decision = choose_orientation(
        (left, right),
        {left.orientation: discogs},
        musicbrainz_candidate=conflicting_mb,
        musicbrainz_orientation=right.orientation,
    )

    assert decision.selected_orientation == "left_is_artist"
    assert decision.selected_candidate is discogs
    assert "discogs_preferred_over_conflicting_musicbrainz" in decision.reasons
    assert decision.musicbrainz_queries == 1


def test_musicbrainz_corroboration_increases_discogs_confidence():
    left, right = _dual()
    discogs = _candidate()
    without_mb = choose_orientation((left, right), {left.orientation: discogs})
    with_mb = choose_orientation(
        (left, right),
        {left.orientation: discogs},
        musicbrainz_candidate=_candidate(provider="MusicBrainz", provider_score=96.0),
        musicbrainz_orientation=left.orientation,
    )

    assert with_mb.selected_orientation == "left_is_artist"
    assert with_mb.confidence > without_mb.confidence
    assert "musicbrainz_corroborated_selected_orientation" in with_mb.reasons


def test_unique_local_artist_selects_strict_offline_orientation():
    left, right = _dual("Synthetic Anthem - The Cosmic Assembly (1978)")
    decision = choose_orientation(
        (left, right), {}, unique_local_orientation=right.orientation
    )

    assert decision.selected_orientation == "right_is_artist"
    assert decision.evaluated_count == 2
    assert not decision.provider_confirmed
    assert decision.fallback_terminalizable
    assert not decision.requires_provider_adjudication


def test_unresolved_dual_orientation_is_safe_and_remains_provider_eligible():
    left, right = _dual()
    decision = choose_orientation(
        (left, right),
        {left.orientation: (), right.orientation: ()},
        current_artist=left.artist,
        current_title=left.title,
    )
    payload = decision.to_dict()

    assert decision.selected is None
    assert decision.evaluated_count == 2
    assert decision.requires_provider_adjudication
    assert "current_matches_left_is_artist" in decision.reasons
    assert payload["selected"] is None
    assert left.artist not in repr(payload)
    assert left.title not in repr(payload)


def test_evaluated_local_fallback_preserves_current_orientation_but_stays_eligible():
    left, right = _dual()
    decision = choose_orientation(
        (left, right),
        {},
        current_artist=left.artist,
        current_title=left.title,
        local_evidence_evaluated=True,
    )

    assert decision.selected_orientation == "left_is_artist"
    assert decision.evaluated_count == 2
    assert decision.confidence == 45.0
    assert not decision.provider_confirmed
    assert decision.requires_provider_adjudication
    assert decision.fallback_terminalizable
    assert "preserved_current_orientation" in decision.reasons
