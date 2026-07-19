"""Pure, bounded evidence assessment for ambiguous source-title orientation.

This module performs no provider, database, file, or network access.  It
compares already-normalized provider candidates with the two hypotheses from
``title_parser`` and returns a small decision record safe for persistence.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .ensemble import versions_compatible
from .matching import text_similarity
from .title_parser import TitleOrientationHypothesis


_ORIENTATION_ALIASES = {
    "artist_then_title": "left_is_artist",
    "title_then_artist": "right_is_artist",
}
_SAFE_PROVIDER_REASON_CODES = frozenset(
    {
        "exact_tracklist_title",
        "track_title_mismatch",
        "exact_artist_credit",
        "artist_mismatch",
        "duration_plausible",
        "duration_mismatch",
        "version_conflict",
        "year_hint_match",
        "year_hint_mismatch",
        "album_context_match",
        "album_context_mismatch",
        "release_ambiguous",
        "unofficial_release",
    }
)
_YEAR_RE = re.compile(r"(?:^|\D)((?:18|19|20)\d{2})(?:\D|$)")
_DISCOGS_SCORE_MARGIN = 5.0


def normalize_orientation(value: object) -> str:
    """Normalize current and Batch 10.5 orientation labels."""

    text = str(value or "").strip().casefold()
    return _ORIENTATION_ALIASES.get(text, text)


def _value(source: object, name: str, default: object = None) -> object:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _score(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return max(0.0, min(100.0, numeric))


def _provider_reason_codes(candidate: object) -> frozenset[str]:
    raw = _value(candidate, "reasons", ()) or ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, Sequence):
        return frozenset()
    return frozenset(
        code
        for item in raw
        if (code := str(item).strip().casefold()) in _SAFE_PROVIDER_REASON_CODES
    )


def _candidate_year(candidate: object) -> int | None:
    for name in ("original_release_date", "release_date", "year"):
        value = str(_value(candidate, name, "") or "").strip()
        match = _YEAR_RE.search(value)
        if match:
            return int(match.group(1))
    return None


def _candidate_sequence(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return tuple(value)
    return (value,)


@dataclass(frozen=True)
class OrientationAssessment:
    """One provider candidate assessed against one title orientation."""

    hypothesis: TitleOrientationHypothesis
    provider: str
    score: float
    provider_score: float
    title_similarity: float
    artist_similarity: float
    duration_delta_seconds: float | None
    year_match: bool | None
    version_match: bool | None
    tracklist_match: bool
    coherent: bool
    conclusive: bool
    reasons: tuple[str, ...]
    candidate: object = field(repr=False, compare=False)

    @property
    def orientation(self) -> str:
        return normalize_orientation(self.hypothesis.orientation)

    def to_dict(self) -> dict[str, object]:
        """Return decision facts only; candidate values and queries are omitted."""

        return {
            "orientation": self.orientation,
            "provider": self.provider,
            "score": self.score,
            "provider_score": self.provider_score,
            "title_similarity": self.title_similarity,
            "artist_similarity": self.artist_similarity,
            "duration_delta_seconds": self.duration_delta_seconds,
            "year_match": self.year_match,
            "version_match": self.version_match,
            "tracklist_match": self.tracklist_match,
            "coherent": self.coherent,
            "conclusive": self.conclusive,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class OrientationDecision:
    """A bounded orientation choice and persistence-safe audit evidence."""

    selected: TitleOrientationHypothesis | None
    evaluated_count: int
    confidence: float
    reasons: tuple[str, ...]
    provider_confirmed: bool
    requires_provider_adjudication: bool
    discogs_queries: int
    musicbrainz_queries: int
    selected_candidate: object | None = field(default=None, repr=False, compare=False)
    assessments: tuple[OrientationAssessment, ...] = field(
        default=(), repr=False, compare=False
    )

    @property
    def selected_orientation(self) -> str | None:
        if self.selected is None:
            return None
        return normalize_orientation(self.selected.orientation)

    @property
    def fallback_terminalizable(self) -> bool:
        return bool(
            self.selected is not None
            and (
                self.provider_confirmed
                or self.evaluated_count >= 2
                or "unique_local_artist_identity" in self.reasons
            )
        )

    def to_dict(self) -> dict[str, object]:
        """Return stable JSON evidence without personal/provider response values."""

        return {
            "schema_version": 1,
            "evaluated_count": self.evaluated_count,
            "selected": self.selected_orientation,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "provider_confirmed": self.provider_confirmed,
            "requires_provider_adjudication": self.requires_provider_adjudication,
            "discogs_queries": self.discogs_queries,
            "musicbrainz_queries": self.musicbrainz_queries,
            "evaluations": [item.to_dict() for item in self.assessments],
        }


def assess_orientation(
    hypothesis: TitleOrientationHypothesis,
    candidate: object,
    *,
    provider: str,
    local_duration: float | None = None,
) -> OrientationAssessment:
    """Assess one normalized candidate without trusting its score alone."""

    provider_name = str(provider or _value(candidate, "provider", "unknown"))
    provider_key = provider_name.strip().casefold() or "unknown"
    candidate_title = _value(candidate, "title", "")
    candidate_artist = _value(candidate, "artist", "")
    title_similarity = text_similarity(hypothesis.title, candidate_title, title=True)
    artist_similarity = text_similarity(hypothesis.artist, candidate_artist)
    provider_score = _score(
        _value(candidate, "provider_score", _value(candidate, "score", 0.0))
    )
    provider_reasons = _provider_reason_codes(candidate)

    duration_delta: float | None = None
    duration_conflict = "duration_mismatch" in provider_reasons
    candidate_duration = _value(candidate, "duration_seconds")
    try:
        if local_duration is not None and candidate_duration is not None:
            duration_delta = round(
                abs(float(local_duration) - float(candidate_duration)), 3
            )
            duration_conflict = duration_conflict or duration_delta > max(
                30.0, abs(float(local_duration)) * 0.2
            )
    except (TypeError, ValueError, OverflowError):
        duration_delta = None

    candidate_version = str(_value(candidate, "version_type", "unknown") or "unknown")
    source_version = str(hypothesis.version_type or "unknown")
    version_match: bool | None = None
    version_conflict = "version_conflict" in provider_reasons
    if source_version != "unknown" and candidate_version != "unknown":
        version_match = versions_compatible(source_version, candidate_version)
        version_conflict = version_conflict or not version_match
    elif source_version == "unknown" or candidate_version == source_version:
        version_match = True

    candidate_year = _candidate_year(candidate)
    year_match = (
        candidate_year == hypothesis.year_hint
        if candidate_year is not None and hypothesis.year_hint is not None
        else None
    )
    tracklist_match = "exact_tracklist_title" in provider_reasons
    exact_artist_credit = "exact_artist_credit" in provider_reasons
    title_mismatch = bool(
        title_similarity < 75.0 or "track_title_mismatch" in provider_reasons
    )
    artist_mismatch = bool(
        artist_similarity < 75.0 or "artist_mismatch" in provider_reasons
    )

    reasons: list[str] = []
    reasons.append(
        "exact_title_match"
        if title_similarity == 100.0
        else "strong_title_match"
        if title_similarity >= 85.0
        else "title_mismatch"
    )
    reasons.append(
        "exact_artist_match"
        if artist_similarity == 100.0
        else "strong_artist_match"
        if artist_similarity >= 85.0
        else "artist_mismatch"
    )
    if tracklist_match:
        reasons.append("exact_tracklist_title")
    if exact_artist_credit:
        reasons.append("exact_artist_credit")
    if duration_conflict:
        reasons.append("duration_mismatch")
    elif duration_delta is not None:
        reasons.append(
            "duration_match" if duration_delta <= 5.0 else "duration_plausible"
        )
    if version_conflict:
        reasons.append("version_conflict")
    elif version_match is True and source_version != "unknown":
        reasons.append("version_coherent")
    if year_match is True:
        reasons.append("year_hint_match")
    elif year_match is False:
        reasons.append("year_hint_mismatch")
    if _value(candidate, "release_id") or _value(candidate, "master_id"):
        reasons.append("release_identity_present")

    score = (
        provider_score * 0.30
        + title_similarity * 0.35
        + artist_similarity * 0.25
    )
    if tracklist_match:
        score += 5.0
    if exact_artist_credit:
        score += 3.0
    if year_match is True:
        score += 3.0
    elif year_match is False:
        score -= 4.0
    if duration_delta is not None and not duration_conflict:
        score += 3.0 if duration_delta <= 5.0 else 1.0
    if version_match is True and source_version != "unknown":
        score += 2.0
    if title_mismatch or artist_mismatch:
        score -= 25.0
    if duration_conflict or version_conflict:
        score -= 30.0
    score = round(max(0.0, min(100.0, score)), 3)

    coherent = bool(
        provider_score >= 55.0
        and not title_mismatch
        and not artist_mismatch
        and not duration_conflict
        and not version_conflict
    )
    conclusive = bool(
        coherent
        and provider_score >= (85.0 if provider_key == "discogs" else 90.0)
        and title_similarity >= 90.0
        and artist_similarity >= 90.0
        and (tracklist_match or (title_similarity == 100.0 and artist_similarity == 100.0))
    )
    if coherent:
        reasons.append("coherent_provider_identity")
    if conclusive:
        reasons.append("conclusive_provider_identity")
    return OrientationAssessment(
        hypothesis=hypothesis,
        provider=provider_key,
        score=score,
        provider_score=round(provider_score, 3),
        title_similarity=title_similarity,
        artist_similarity=artist_similarity,
        duration_delta_seconds=duration_delta,
        year_match=year_match,
        version_match=version_match,
        tracklist_match=tracklist_match,
        coherent=coherent,
        conclusive=conclusive,
        reasons=tuple(dict.fromkeys(reasons)),
        candidate=candidate,
    )


def _hypothesis_map(
    hypotheses: Sequence[TitleOrientationHypothesis],
) -> dict[str, TitleOrientationHypothesis]:
    result: dict[str, TitleOrientationHypothesis] = {}
    for hypothesis in hypotheses:
        result.setdefault(normalize_orientation(hypothesis.orientation), hypothesis)
    return result


def _best_assessment(
    hypothesis: TitleOrientationHypothesis,
    candidates: object,
    *,
    provider: str,
    local_duration: float | None,
) -> OrientationAssessment | None:
    assessments = tuple(
        assess_orientation(
            hypothesis,
            candidate,
            provider=provider,
            local_duration=local_duration,
        )
        for candidate in _candidate_sequence(candidates)
    )
    if not assessments:
        return None
    return max(
        assessments,
        key=lambda item: (
            item.coherent,
            item.conclusive,
            item.tracklist_match,
            item.score,
            item.provider_score,
        ),
    )


def _current_orientation(
    hypotheses: Mapping[str, TitleOrientationHypothesis],
    *,
    current_artist: object,
    current_title: object,
) -> str | None:
    if not str(current_artist or "").strip() or not str(current_title or "").strip():
        return None
    for orientation, hypothesis in hypotheses.items():
        if (
            text_similarity(current_artist, hypothesis.artist) == 100.0
            and text_similarity(current_title, hypothesis.title, title=True) == 100.0
        ):
            return orientation
    return None


def choose_orientation(
    hypotheses: Sequence[TitleOrientationHypothesis],
    discogs_results_by_orientation: Mapping[str, object],
    *,
    musicbrainz_candidate: object | None = None,
    musicbrainz_orientation: str | None = None,
    musicbrainz_query_attempted: bool = False,
    unique_local_orientation: str | None = None,
    current_artist: object = None,
    current_title: object = None,
    local_duration: float | None = None,
    local_evidence_evaluated: bool = False,
) -> OrientationDecision:
    """Select one orientation from at most two Discogs and one MB evaluation.

    ``discogs_results_by_orientation`` records attempted searches: an empty
    value therefore still counts as one evaluated orientation.  Values may be
    a candidate or a candidate sequence; only the best normalized candidate is
    retained.  Provider response bodies and query values never enter the
    serialized decision.
    """

    by_orientation = _hypothesis_map(tuple(hypotheses))
    if not by_orientation:
        return OrientationDecision(None, 0, 0.0, ("no_valid_orientation",), False, True, 0, 0)

    normalized_discogs = {
        normalize_orientation(orientation): candidates
        for orientation, candidates in discogs_results_by_orientation.items()
        if normalize_orientation(orientation) in by_orientation
    }
    if len(normalized_discogs) > 2:
        raise ValueError("At most two Discogs orientation searches are allowed.")

    assessments: list[OrientationAssessment] = []
    discogs_by_orientation: dict[str, OrientationAssessment] = {}
    for orientation, candidates in normalized_discogs.items():
        assessment = _best_assessment(
            by_orientation[orientation],
            candidates,
            provider="discogs",
            local_duration=local_duration,
        )
        if assessment is not None:
            discogs_by_orientation[orientation] = assessment
            assessments.append(assessment)

    evaluated_orientations = set(normalized_discogs)
    mb_assessment: OrientationAssessment | None = None
    mb_candidates = _candidate_sequence(musicbrainz_candidate)
    mb_query_count = int(
        musicbrainz_query_attempted or musicbrainz_candidate is not None
    )
    if mb_candidates:
        candidate_orientations: tuple[str, ...]
        normalized_mb_orientation = normalize_orientation(musicbrainz_orientation)
        if normalized_mb_orientation in by_orientation:
            candidate_orientations = (normalized_mb_orientation,)
        else:
            candidate_orientations = tuple(by_orientation)
        possible = tuple(
            assessment
            for orientation in candidate_orientations
            if (
                assessment := _best_assessment(
                    by_orientation[orientation],
                    mb_candidates,
                    provider="musicbrainz",
                    local_duration=local_duration,
                )
            )
            is not None
        )
        if possible:
            mb_assessment = max(
                possible,
                key=lambda item: (
                    item.coherent,
                    item.conclusive,
                    item.score,
                    item.provider_score,
                ),
            )
            assessments.append(mb_assessment)
            evaluated_orientations.add(mb_assessment.orientation)

    local_orientation = normalize_orientation(unique_local_orientation)
    if local_orientation not in by_orientation:
        local_orientation = ""
    if local_orientation and len(by_orientation) > 1:
        # A strict uniqueness result necessarily compared both candidate sides.
        evaluated_orientations.update(by_orientation)
    if local_evidence_evaluated:
        evaluated_orientations.update(by_orientation)

    reasons: list[str] = []
    selected_assessment: OrientationAssessment | None = None
    selected_from_local = False
    coherent_discogs = sorted(
        (item for item in discogs_by_orientation.values() if item.coherent),
        key=lambda item: (
            item.tracklist_match,
            item.year_match is True,
            item.duration_delta_seconds is not None
            and item.duration_delta_seconds <= 5.0,
            item.score,
            item.provider_score,
        ),
        reverse=True,
    )

    if len(coherent_discogs) == 1:
        only = coherent_discogs[0]
        all_orientations_evaluated = len(evaluated_orientations) >= len(by_orientation)
        if only.conclusive or len(by_orientation) == 1 or all_orientations_evaluated:
            selected_assessment = only
            reasons.append(
                "conclusive_first_discogs_orientation"
                if only.conclusive and len(normalized_discogs) == 1
                else "only_coherent_discogs_orientation"
            )
    elif len(coherent_discogs) >= 2:
        first, second = coherent_discogs[:2]
        decisive = False
        if first.tracklist_match != second.tracklist_match:
            decisive = first.tracklist_match
            if decisive:
                reasons.append("exact_tracklist_orientation")
        elif first.year_match is True and second.year_match is False:
            decisive = True
            reasons.append("year_coherent_orientation")
        elif (
            first.duration_delta_seconds is not None
            and first.duration_delta_seconds <= 5.0
            and (
                second.duration_delta_seconds is None
                or second.duration_delta_seconds > 5.0
            )
        ):
            decisive = True
            reasons.append("duration_coherent_orientation")
        elif (
            first.provider_score - second.provider_score >= _DISCOGS_SCORE_MARGIN
            or first.score - second.score >= _DISCOGS_SCORE_MARGIN
        ):
            decisive = True
            reasons.append("clear_discogs_score_margin")
        if decisive:
            selected_assessment = first

    if selected_assessment is None and mb_assessment is not None and mb_assessment.coherent:
        matching_discogs = discogs_by_orientation.get(mb_assessment.orientation)
        if matching_discogs is not None and matching_discogs.coherent:
            selected_assessment = matching_discogs
            reasons.append("musicbrainz_corroborated_discogs_orientation")
        elif not coherent_discogs and (
            mb_assessment.conclusive
            or len(evaluated_orientations) >= len(by_orientation)
        ):
            selected_assessment = mb_assessment
            reasons.append("musicbrainz_fallback_orientation")

    if selected_assessment is None and local_orientation:
        conflicting_discogs = [
            item
            for item in coherent_discogs
            if item.orientation != local_orientation and item.conclusive
        ]
        if not conflicting_discogs:
            selected_from_local = True
            reasons.append("unique_local_artist_identity")

    selected = (
        selected_assessment.hypothesis
        if selected_assessment is not None
        else by_orientation.get(local_orientation)
        if selected_from_local
        else None
    )
    provider_confirmed = selected_assessment is not None
    selected_candidate = (
        selected_assessment.candidate if selected_assessment is not None else None
    )
    confidence = selected_assessment.score if selected_assessment is not None else 75.0 if selected else 0.0

    if (
        selected is not None
        and mb_assessment is not None
        and mb_assessment.coherent
        and mb_assessment.orientation == normalize_orientation(selected.orientation)
        and selected_assessment is not mb_assessment
    ):
        confidence = min(100.0, confidence + 3.0)
        if "musicbrainz_corroborated_discogs_orientation" not in reasons:
            reasons.append("musicbrainz_corroborated_selected_orientation")
    elif (
        selected_assessment is not None
        and selected_assessment.provider == "discogs"
        and mb_assessment is not None
        and mb_assessment.coherent
        and mb_assessment.orientation != selected_assessment.orientation
    ):
        reasons.append("discogs_preferred_over_conflicting_musicbrainz")

    current_orientation = _current_orientation(
        by_orientation,
        current_artist=current_artist,
        current_title=current_title,
    )
    if current_orientation:
        reasons.append(f"current_matches_{current_orientation}")
    preserved_current = bool(
        selected is None and local_evidence_evaluated and current_orientation
    )
    if preserved_current:
        selected = by_orientation[current_orientation]
        confidence = 45.0
        reasons.append("preserved_current_orientation")
    provisional_conventional = bool(
        selected is None and local_evidence_evaluated and by_orientation
    )
    if provisional_conventional:
        selected = next(iter(by_orientation.values()))
        confidence = 35.0
        reasons.append("provisional_conventional_orientation")
    requires_provider_adjudication = bool(
        selected is None or preserved_current or provisional_conventional
    )
    if requires_provider_adjudication:
        reasons.append("provider_adjudication_required")

    return OrientationDecision(
        selected=selected,
        evaluated_count=min(len(by_orientation), len(evaluated_orientations)),
        confidence=round(confidence, 3),
        reasons=tuple(dict.fromkeys(reasons)),
        provider_confirmed=provider_confirmed,
        requires_provider_adjudication=requires_provider_adjudication,
        discogs_queries=len(normalized_discogs),
        musicbrainz_queries=mb_query_count,
        selected_candidate=selected_candidate,
        assessments=tuple(assessments),
    )


__all__ = [
    "OrientationAssessment",
    "OrientationDecision",
    "assess_orientation",
    "choose_orientation",
    "normalize_orientation",
]
