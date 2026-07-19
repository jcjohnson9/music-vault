"""Field-level metadata review outcomes and offline evidence classification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from .ensemble import FieldAction, MetadataEnsemble
from .matching import normalize_for_comparison
from .soundtrack import SoundtrackClassification, classify_soundtrack
from .title_parser import STRONG_TITLE_PATTERNS


class ReviewOutcome(str, Enum):
    APPLIED = "applied"
    APPLIED_WITH_GAPS = "applied_with_gaps"
    SOURCE_FALLBACK = "source_fallback"
    ACCEPTED_SOURCE_FALLBACK = "source_fallback"
    NEEDS_REVIEW = "review"
    FAILED = "failed"
    NO_MATCH = "no_match"
    SKIPPED = "skipped"


CRITICAL_IDENTITY_FIELDS = frozenset(
    {"title", "artist", "artist_credits", "version_type", "duration"}
)
SECONDARY_METADATA_FIELDS = frozenset(
    {
        "album",
        "album_artist",
        "release_date",
        "original_release_date",
        "artwork",
        "label",
        "catalog_number",
        "country",
        "edition",
        "discogs_release_id",
        "discogs_master_id",
        "musicbrainz_recording_id",
        "musicbrainz_release_id",
    }
)

_CRITICAL_REVIEW_REASONS = frozenset(
    {
        "artist_ambiguity",
        "critical_provider_conflict",
        "duration_conflict",
        "file_write_failed",
        "identity_conflict",
        "incompatible_duration",
        "possible_wrong_song",
        "primary_artist_ambiguity",
        "provider_or_apply_failure",
        "title_ambiguity",
        "unsafe_structured_credit_split",
        "version_conflict",
        "wrong_song",
    }
)
_SECONDARY_REASON_FIELDS = {
    "album_ambiguity": "exact_edition",
    "date_ambiguity": "release_date",
    "artwork_missing": "artwork",
    "label_missing": "label",
    "catalog_number_missing": "catalog_number",
    "soundtrack_edition_ambiguity": "exact_edition",
}
_RECOGNIZED_NONCRITICAL_REASONS = frozenset(
    {
        "",
        "manual_or_confirmed_complete",
        "no_provider_match",
        "provider_disagreement",
        "secondary_metadata_gaps",
        "strong_source_fallback",
        "youtube_exclusive",
        *_SECONDARY_REASON_FIELDS,
    }
)
_NESTED_EVIDENCE_MAPPINGS = frozenset(
    {
        "_current",
        "_reasons",
        "_discogs",
        "_musicbrainz",
        "_sources",
        "_artwork",
        "_orientation",
    }
)


@dataclass(frozen=True)
class ReviewDecision:
    outcome: ReviewOutcome
    reason: str | None
    critical_conflicts: tuple[str, ...] = ()
    secondary_gaps: tuple[str, ...] = ()
    safe_critical_fields: tuple[str, ...] = ()
    soundtrack: SoundtrackClassification | None = None

    @property
    def needs_review(self) -> bool:
        return self.outcome is ReviewOutcome.NEEDS_REVIEW

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "critical_conflicts": list(self.critical_conflicts),
            "secondary_gaps": list(self.secondary_gaps),
            "safe_critical_fields": list(self.safe_critical_fields),
            "soundtrack_kind": self.soundtrack.kind.value if self.soundtrack else "none",
        }


def _mapping(value: object) -> Mapping[str, Any]:
    return _mapping_status(value)[0]


def _mapping_status(value: object) -> tuple[Mapping[str, Any], bool]:
    """Return a mapping plus whether nonempty stored evidence was malformed."""

    if isinstance(value, Mapping):
        return value, False
    if value in (None, ""):
        return {}, False
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, True
    return (decoded, False) if isinstance(decoded, Mapping) else ({}, True)


def _confidence(value: object) -> float:
    if isinstance(value, Mapping):
        value = value.get("score", value.get("confidence", 0))
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _reason_fields(proposal: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    raw = proposal.get("_reasons", {})
    if not isinstance(raw, Mapping):
        return result
    for name, reasons in raw.items():
        if isinstance(reasons, str):
            values = (reasons,)
        elif isinstance(reasons, Sequence):
            values = tuple(str(item) for item in reasons)
        else:
            values = ()
        result[str(name)] = values
    return result


def _strong_source_fallback(
    *,
    hints: Mapping[str, Any],
    agreement: str,
    review_reason: str,
    proposal: Mapping[str, Any],
) -> bool:
    title = str(hints.get("title") or "").strip()
    artist = str(hints.get("artist") or "").strip()
    pattern = str(hints.get("pattern") or "").strip()
    has_provider = bool(
        proposal.get("_discogs") or proposal.get("_musicbrainz")
    )
    return bool(
        title
        and artist
        and pattern in STRONG_TITLE_PATTERNS
        and _orientation_fallback_ready(hints, proposal)
        and agreement in {"none", "unknown"}
        and not has_provider
        and review_reason
        in {"youtube_exclusive", "strong_source_fallback", "no_provider_match", ""}
    )


def _orientation_fallback_ready(
    hints: Mapping[str, Any], proposal: Mapping[str, Any]
) -> bool:
    """Require an auditable decision before closing an ambiguous dash item."""

    if str(hints.get("pattern") or "").strip() != "artist_dash_title":
        return True
    evidence = _mapping(proposal.get("_orientation")) or _mapping(
        hints.get("orientation")
    )
    if not evidence:
        return False
    try:
        evaluated = int(evidence.get("evaluated_count", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        evaluated = 0
    reasons = evidence.get("reasons", ())
    if isinstance(reasons, str):
        reasons = (reasons,)
    reason_set = {
        str(value).strip().casefold()
        for value in reasons
        if str(value).strip()
    }
    selected = str(evidence.get("selected") or "").strip().casefold()
    if selected not in {"left_is_artist", "right_is_artist"}:
        return False
    provider_confirmed = evidence.get("provider_confirmed") is True
    requires_provider = evidence.get("requires_provider_adjudication") is True
    if provider_confirmed and requires_provider:
        return False
    return bool(
        provider_confirmed
        or evaluated >= 2
        or "unique_local_artist_identity" in reason_set
    )


def _soundtrack_from_evidence(
    hints: Mapping[str, Any], proposal: Mapping[str, Any]
) -> SoundtrackClassification:
    current = _mapping(proposal.get("_current"))
    discogs = _mapping(proposal.get("_discogs"))
    return classify_soundtrack(
        title=proposal.get("title") or current.get("title") or hints.get("title"),
        album=proposal.get("album") or current.get("album") or discogs.get("album"),
        version_type=proposal.get("version_type") or hints.get("version_type"),
        source_title=hints.get("raw_title"),
        release_type=discogs.get("release_type"),
        release_format=discogs.get("release_format"),
        album_artist=proposal.get("album_artist") or current.get("album_artist"),
        provider_credits=discogs.get("artist_credits", ()) or (),
    )


def classify_stored_review_evidence(
    *,
    parsed_hints: object,
    field_proposal: object,
    field_confidence: object,
    provider_agreement: object,
    review_reason: object,
) -> ReviewDecision:
    """Classify a persisted review item without network or raw provider data."""

    hints, malformed_hints = _mapping_status(parsed_hints)
    proposal, malformed_proposal = _mapping_status(field_proposal)
    confidences, malformed_confidences = _mapping_status(field_confidence)
    if malformed_hints or malformed_proposal or malformed_confidences:
        return ReviewDecision(
            ReviewOutcome.NEEDS_REVIEW,
            "malformed_stored_review_evidence",
            ("malformed_stored_evidence",),
        )
    if any(
        key in proposal and not isinstance(proposal[key], Mapping)
        for key in _NESTED_EVIDENCE_MAPPINGS
    ):
        return ReviewDecision(
            ReviewOutcome.NEEDS_REVIEW,
            "malformed_stored_review_evidence",
            ("malformed_stored_evidence",),
        )
    stored_pattern = str(hints.get("pattern") or "").strip()
    if stored_pattern and stored_pattern not in STRONG_TITLE_PATTERNS:
        return ReviewDecision(
            ReviewOutcome.NEEDS_REVIEW,
            "unrecognized_source_title_pattern",
            ("unrecognized_source_title_pattern",),
        )
    current = _mapping(proposal.get("_current"))
    agreement = str(provider_agreement or "unknown").strip().casefold()
    reason = str(review_reason or "").strip().casefold()
    reason_fields = _reason_fields(proposal)
    soundtrack = _soundtrack_from_evidence(hints, proposal)

    critical_conflicts: set[str] = set()
    conflict_is_explicitly_secondary = bool(reason_fields) and all(
        field_name in SECONDARY_METADATA_FIELDS for field_name in reason_fields
    )
    for field_name, reasons in reason_fields.items():
        if field_name in CRITICAL_IDENTITY_FIELDS and any(
            marker in {
                "provider_value_conflict",
                "version_identity_conflict",
                "duration_mismatch",
                "identity_conflict",
            }
            for marker in reasons
        ):
            critical_conflicts.add(field_name)
    if reason in _CRITICAL_REVIEW_REASONS:
        critical_conflicts.add(reason)
    elif reason == "provider_disagreement" and not reason_fields:
        # Old items did not always persist field-level conflict names.  Treat
        # unknown-scope disagreement conservatively instead of auto-approving.
        critical_conflicts.add("provider_disagreement")
    elif reason not in _RECOGNIZED_NONCRITICAL_REASONS:
        # Old versions could persist free-form reasons without a field map.
        # Unknown reasons are evidence of unresolved uncertainty, not proof
        # that the identity is safe to terminalize offline.
        critical_conflicts.add("unclassified_legacy_reason")
    if agreement == "conflict" and not conflict_is_explicitly_secondary:
        # Persisted provider disagreement is safe to reclassify offline only
        # when the saved field map proves its scope is entirely secondary.
        # Missing or malformed scope may hide a title/artist/version conflict.
        critical_conflicts.add("provider_disagreement")
    effective_title = proposal.get("title") or current.get("title") or hints.get("title")
    effective_artist = proposal.get("artist") or current.get("artist") or hints.get("artist")
    if not str(effective_title or "").strip():
        critical_conflicts.add("missing_title_identity")
    if not str(effective_artist or "").strip():
        critical_conflicts.add("missing_primary_artist_identity")

    safe_critical: list[str] = []
    for field_name in ("title", "artist", "version_type"):
        proposed = proposal.get(field_name)
        if proposed in (None, "") or _confidence(confidences.get(field_name)) < 85.0:
            continue
        field_reasons = reason_fields.get(field_name, ())
        if any("conflict" in marker or "ambiguous" in marker for marker in field_reasons):
            continue
        current_value = current.get(field_name)
        if not current_value or normalize_for_comparison(current_value) == normalize_for_comparison(proposed):
            safe_critical.append(field_name)

    secondary_gaps: set[str] = set()
    if reason in _SECONDARY_REASON_FIELDS:
        secondary_gaps.add(_SECONDARY_REASON_FIELDS[reason])
    for field_name in ("album", "album_artist", "release_date", "original_release_date"):
        if current.get(field_name) in (None, "") and proposal.get(field_name) in (None, ""):
            secondary_gaps.add(field_name)
        if field_name in reason_fields and any(
            marker in {"provider_value_conflict", "release_context_ambiguous"}
            for marker in reason_fields[field_name]
        ):
            secondary_gaps.add("exact_edition" if field_name == "album" else field_name)
    artwork = _mapping(proposal.get("_artwork"))
    if not current.get("artwork") and not artwork.get("candidate_available"):
        secondary_gaps.add("artwork")
    if soundtrack.is_soundtrack and reason in {
        "album_ambiguity",
        "date_ambiguity",
        "soundtrack_edition_ambiguity",
    }:
        secondary_gaps.add("exact_edition")

    if critical_conflicts:
        return ReviewDecision(
            ReviewOutcome.NEEDS_REVIEW,
            reason or "critical_identity_conflict",
            tuple(sorted(critical_conflicts)),
            tuple(sorted(secondary_gaps)),
            tuple(safe_critical),
            soundtrack,
        )
    if _strong_source_fallback(
        hints=hints,
        agreement=agreement,
        review_reason=reason,
        proposal=proposal,
    ):
        return ReviewDecision(
            ReviewOutcome.SOURCE_FALLBACK,
            "strong_source_fallback",
            secondary_gaps=tuple(sorted(secondary_gaps)),
            safe_critical_fields=tuple(safe_critical),
            soundtrack=soundtrack,
        )
    if secondary_gaps or reason in _SECONDARY_REASON_FIELDS:
        return ReviewDecision(
            ReviewOutcome.APPLIED_WITH_GAPS,
            "secondary_metadata_gaps",
            secondary_gaps=tuple(sorted(secondary_gaps)),
            safe_critical_fields=tuple(safe_critical),
            soundtrack=soundtrack,
        )
    return ReviewDecision(
        ReviewOutcome.APPLIED,
        None,
        safe_critical_fields=tuple(safe_critical),
        soundtrack=soundtrack,
    )


def terminalize_stored_review_evidence(
    *,
    parsed_hints: object,
    field_proposal: object,
    field_confidence: object,
    provider_agreement: object,
    review_reason: object,
) -> ReviewDecision:
    """Resolve a legacy Review row to a terminal best-available outcome.

    The older classifier remains available for audit compatibility, but the
    product pipeline no longer leaves ordinary uncertainty waiting for manual
    approval.  Corrupt saved evidence is an operational failure; otherwise we
    keep the uncertainty diagnostics and accept provider or source evidence.
    No network access is performed here.
    """

    decision = classify_stored_review_evidence(
        parsed_hints=parsed_hints,
        field_proposal=field_proposal,
        field_confidence=field_confidence,
        provider_agreement=provider_agreement,
        review_reason=review_reason,
    )
    if decision.outcome is not ReviewOutcome.NEEDS_REVIEW:
        return decision
    if "malformed_stored_evidence" in decision.critical_conflicts:
        return ReviewDecision(
            ReviewOutcome.FAILED,
            "corrupt_stored_evidence",
            decision.critical_conflicts,
            decision.secondary_gaps,
            decision.safe_critical_fields,
            decision.soundtrack,
        )

    hints = _mapping(parsed_hints)
    proposal = _mapping(field_proposal)
    has_provider = bool(
        _mapping(proposal.get("_discogs"))
        or _mapping(proposal.get("_musicbrainz"))
    )
    stored_reason = str(review_reason or "").strip().casefold()
    if stored_reason in {
        "file_write_failed",
        "file_write_rollback_failed",
        "database_transaction_failed",
        "database_apply_failed",
    } or (stored_reason == "provider_or_apply_failure" and not has_provider):
        return ReviewDecision(
            ReviewOutcome.FAILED,
            "stored_operational_failure",
            decision.critical_conflicts,
            decision.secondary_gaps,
            decision.safe_critical_fields,
            decision.soundtrack,
        )
    stored_pattern = str(hints.get("pattern") or "").strip()
    has_source_identity = bool(
        (hints.get("title") or proposal.get("title"))
        and (hints.get("artist") or proposal.get("artist"))
        and stored_pattern in STRONG_TITLE_PATTERNS
        and _orientation_fallback_ready(hints, proposal)
    )
    if has_provider:
        outcome = ReviewOutcome.APPLIED_WITH_GAPS
        reason = "best_available_provider_evidence"
    elif has_source_identity:
        outcome = ReviewOutcome.SOURCE_FALLBACK
        reason = "accepted_source_fallback"
    else:
        # Preserve the current effective values and close the obsolete Review
        # row with gaps.  Absence of an exact album/year is not a failure.
        outcome = ReviewOutcome.APPLIED_WITH_GAPS
        reason = "preserved_existing_with_gaps"
    return ReviewDecision(
        outcome,
        reason,
        decision.critical_conflicts,
        decision.secondary_gaps,
        decision.safe_critical_fields,
        decision.soundtrack,
    )


def classify_ensemble_outcome(
    ensemble: MetadataEnsemble,
    *,
    current: Mapping[str, object],
    parsed_hints: Mapping[str, object],
    changed: bool,
    youtube_exclusive: bool,
    provider_failures: Sequence[str] = (),
    local_duration: float | None = None,
) -> ReviewDecision:
    """Classify a newly analyzed item with critical conflicts kept fail-closed."""

    critical_conflicts: set[str] = set()
    secondary_gaps: set[str] = set()
    for field in ensemble.fields:
        if field.field_name in CRITICAL_IDENTITY_FIELDS and (
            field.action is FieldAction.REVIEW or field.conflict
        ):
            critical_conflicts.add(field.field_name)
        elif field.field_name in SECONDARY_METADATA_FIELDS:
            if field.action is FieldAction.REVIEW or (
                field.current_value in (None, "") and field.value in (None, "")
            ):
                secondary_gaps.add(
                    "exact_edition" if field.field_name == "album" and field.conflict else field.field_name
                )
    if "version_identity_conflict" in ensemble.reasons:
        critical_conflicts.add("version_type")
    resolved_title = next(
        (
            field.value
            for field in ensemble.fields
            if field.field_name == "title" and field.value not in (None, "")
        ),
        current.get("title"),
    )
    resolved_artist = next(
        (
            field.value
            for field in ensemble.fields
            if field.field_name == "artist" and field.value not in (None, "")
        ),
        current.get("artist"),
    )
    if not str(resolved_title or "").strip():
        critical_conflicts.add("missing_title_identity")
    if not str(resolved_artist or "").strip():
        critical_conflicts.add("missing_primary_artist_identity")

    candidates = tuple(
        candidate
        for candidate in (ensemble.discogs_candidate, ensemble.musicbrainz_candidate)
        if candidate is not None
    )
    if local_duration is not None and candidates:
        # Discogs is authoritative when present. A mismatching secondary
        # MusicBrainz candidate must not invalidate a coherent Discogs match.
        candidate = ensemble.discogs_candidate or ensemble.musicbrainz_candidate
        candidate_duration = getattr(candidate, "duration_seconds", None)
        if candidate_duration is not None:
            delta = abs(float(local_duration) - float(candidate_duration))
            if delta > max(30.0, float(local_duration) * 0.2):
                critical_conflicts.add("duration")

    soundtrack = classify_soundtrack(
        title=current.get("title"),
        album=current.get("album"),
        version_type=parsed_hints.get("version_type"),
        source_title=parsed_hints.get("raw_title"),
        release_type=getattr(ensemble.discogs_candidate, "release_type", None),
        release_format=getattr(ensemble.discogs_candidate, "release_format", None),
        album_artist=current.get("album_artist"),
        provider_credits=getattr(ensemble.discogs_candidate, "artist_credits", ()) or (),
    )
    if current.get("artwork") in (None, ""):
        candidate_artwork = any(getattr(candidate, "artwork", None) for candidate in candidates)
        if not candidate_artwork:
            secondary_gaps.add("artwork")

    if critical_conflicts:
        reason = (
            "version_conflict"
            if "version_type" in critical_conflicts
            else "incompatible_duration"
            if "duration" in critical_conflicts
            else "title_ambiguity"
            if "missing_title_identity" in critical_conflicts
            else "primary_artist_ambiguity"
            if "missing_primary_artist_identity" in critical_conflicts
            else "critical_provider_conflict"
        )
        if candidates:
            return ReviewDecision(
                ReviewOutcome.APPLIED_WITH_GAPS,
                "best_available_identity_with_conflicts",
                tuple(sorted(critical_conflicts)),
                tuple(sorted(secondary_gaps)),
                soundtrack=soundtrack,
            )
        if (
            parsed_hints.get("title")
            and parsed_hints.get("artist")
            and _orientation_fallback_ready(parsed_hints, {})
        ):
            return ReviewDecision(
                ReviewOutcome.SOURCE_FALLBACK,
                "accepted_source_fallback",
                tuple(sorted(critical_conflicts)),
                tuple(sorted(secondary_gaps)),
                soundtrack=soundtrack,
            )
        return ReviewDecision(
            ReviewOutcome.SKIPPED,
            reason,
            tuple(sorted(critical_conflicts)),
            tuple(sorted(secondary_gaps)),
            soundtrack=soundtrack,
        )
    if provider_failures and not candidates:
        # A strong source-title pattern is a valid fallback only after real
        # provider no-match results. A provider outage remains retryable and
        # must not be converted into a terminal source-fallback success.
        return ReviewDecision(
            ReviewOutcome.FAILED, "provider_unavailable", soundtrack=soundtrack
        )
    if youtube_exclusive:
        return ReviewDecision(
            ReviewOutcome.SOURCE_FALLBACK,
            "strong_source_fallback",
            secondary_gaps=tuple(sorted(secondary_gaps)),
            soundtrack=soundtrack,
        )
    if changed or any(field.safe_to_apply for field in ensemble.fields):
        if secondary_gaps:
            return ReviewDecision(
                ReviewOutcome.APPLIED_WITH_GAPS,
                "secondary_metadata_gaps",
                secondary_gaps=tuple(sorted(secondary_gaps)),
                soundtrack=soundtrack,
            )
        return ReviewDecision(ReviewOutcome.APPLIED, None, soundtrack=soundtrack)
    if secondary_gaps and candidates:
        return ReviewDecision(
            ReviewOutcome.APPLIED_WITH_GAPS,
            "secondary_metadata_gaps",
            secondary_gaps=tuple(sorted(secondary_gaps)),
            soundtrack=soundtrack,
        )
    return ReviewDecision(ReviewOutcome.SKIPPED, "no_credible_match", soundtrack=soundtrack)


__all__ = [
    "CRITICAL_IDENTITY_FIELDS",
    "SECONDARY_METADATA_FIELDS",
    "ReviewDecision",
    "ReviewOutcome",
    "classify_ensemble_outcome",
    "classify_stored_review_evidence",
    "terminalize_stored_review_evidence",
]
