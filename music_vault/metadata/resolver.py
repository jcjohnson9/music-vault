"""Pure, conservative permission proposal over existing ensemble decisions.

This layer can withhold an ensemble decision, never upgrade its confidence or
infer catalogue identity from display text. A proposal is not write authority:
the materializer must recheck its expected fingerprint inside its transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Mapping

from .ensemble import FieldAction, FieldResolution, MetadataEnsemble
from .evidence import MetadataEvidence
from .schema import EDITABLE_METADATA_FIELDS


_CATALOGUES = frozenset({"discogs", "musicbrainz"})
_IDENTITY_COLUMNS = {
    "discogs": {"release_family": "discogs_master_id", "edition": "discogs_release_id"},
    "musicbrainz": {
        "recording": "musicbrainz_recording_id",
        "release_family": "musicbrainz_release_group_id",
        "edition": "musicbrainz_release_id",
    },
}
_SCALAR_FIELDS = frozenset(EDITABLE_METADATA_FIELDS) - {"artwork"}


@dataclass(frozen=True)
class MetadataProposal:
    track_id: int
    expected_fingerprint: str
    evidence: tuple[MetadataEvidence, ...]
    blocked_providers: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    field_names: tuple[str, ...]
    credits_provider: str | None
    allow_source_credits: bool
    proposal_key: str
    policy_version: int = 1

    def allows_provider(self, name: str) -> bool:
        return (
            name in _CATALOGUES
            and name not in self.blocked_providers
            and any(item.provider == name for item in self.evidence)
        )


def _qualified(field: FieldResolution | None) -> bool:
    return bool(
        field is not None
        and math.isfinite(field.score)
        and field.score >= 60.0
        and not field.conflict
        and field.action != FieldAction.REVIEW
    )


def _scalar(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return type(value) in (int, float) and math.isfinite(value)


def _canonical(value: object) -> object:
    """Serialize decision facts, never repr an opaque provider object."""
    if isinstance(value, Enum):
        return _canonical(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Proposal decision keys must be strings.")
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Unsupported proposal decision value.")


def resolve_metadata(
    *,
    track_id: int,
    expected_fingerprint: str,
    current_values: Mapping[str, object],
    locked_fields: frozenset[str],
    protected_credits: bool,
    ensemble: MetadataEnsemble,
    evidence: tuple[MetadataEvidence, ...],
    rejected_providers: frozenset[str] = frozenset(),
) -> MetadataProposal:
    """Resolve scalar/credit permissions without SQL, I/O, or provider calls.

Known catalogue IDs are compared only inside their provider and entity-kind
namespace. A mismatch blocks that provider's fields, credits and identities.
Absent identity is unknown, not a match or mismatch. Artwork is deliberately
outside this scalar proposal; its gap/lock/asset validation remains separate.
"""
    if type(track_id) is not int or track_id <= 0:
        raise ValueError("Invalid proposal track identity.")
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise ValueError("Expected fingerprint is required.")
    if not isinstance(evidence, tuple) or any(not isinstance(item, MetadataEvidence) for item in evidence):
        raise ValueError("Normalized evidence tuple is required.")
    if not rejected_providers <= _CATALOGUES:
        raise ValueError("Unknown rejected catalogue provider.")

    blocked = set(rejected_providers)
    reasons = {f"{provider}_rejected" for provider in rejected_providers}
    providers = {item.provider for item in evidence} & _CATALOGUES
    requested = {field.source for field in ensemble.fields} & _CATALOGUES
    for provider in requested - providers:
        blocked.add(provider)
        reasons.add(f"{provider}_evidence_missing")
    if "version_identity_conflict" in ensemble.reasons:
        blocked.update(providers | requested)
        reasons.add("version_identity_conflict")

    for provider in sorted(providers):
        for kind, column in _IDENTITY_COLUMNS[provider].items():
            identifiers = {
                identity.entity_id
                for item in evidence
                if item.provider == provider and (identity := getattr(item, kind)) is not None
            }
            current = current_values.get(column)
            known = str(current).strip() if current is not None else ""
            if len(identifiers) > 1 or (known and identifiers and known not in identifiers):
                blocked.add(provider)
                reasons.add(f"{provider}_{kind}_identity_conflict")

    fields = tuple(sorted({
        field.field_name
        for field in ensemble.fields
        if field.field_name in _SCALAR_FIELDS
        and field.field_name not in locked_fields
        and not (field.field_name == "artist" and protected_credits)
        and _qualified(field)
        and _scalar(field.value)
        and field.source not in blocked
    }))
    credits_provider = None
    allow_source_credits = False
    credits = ensemble.field("artist_credits")
    if not protected_credits and not ({"artist", "artist_credits"} & locked_fields) and _qualified(credits):
        if credits.source in providers - blocked and any(
            item.provider == credits.source
            and any(credit.role == "primary" for credit in item.recording_credits)
            for item in evidence
        ):
            credits_provider = credits.source
        elif credits.source == "youtube_title_parsed":
            allow_source_credits = isinstance(credits.value, (tuple, list)) and bool(credits.value)

    decision = {
        "policy_version": 1,
        "track_id": track_id,
        "expected_fingerprint": expected_fingerprint,
        "current_values": current_values,
        "locked_fields": locked_fields,
        "protected_credits": protected_credits,
        # These are the ensemble inputs this policy actually consults. Raw
        # candidate transport payloads and acquisition times are not decisions.
        "ensemble_fields": ensemble.fields,
        "ensemble_reasons": ensemble.reasons,
        "evidence": evidence,
        "rejected_providers": rejected_providers,
    }
    encoded = json.dumps(_canonical(decision), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return MetadataProposal(
        track_id=track_id,
        expected_fingerprint=expected_fingerprint,
        evidence=evidence,
        blocked_providers=tuple(sorted(blocked)),
        blocked_reasons=tuple(sorted(reasons)),
        field_names=fields,
        credits_provider=credits_provider,
        allow_source_credits=allow_source_credits,
        proposal_key="metadata-proposal-v1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )
