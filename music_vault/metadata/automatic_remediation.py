"""Immutable authority envelope for an already assessed remediation candidate.

This is not user-confirmed override authority and does not classify candidates.
The existing strict remediation assessment remains responsible for eligibility;
the metadata service can only withhold selected fields or incompatible IDs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Mapping

from .evidence import MetadataEvidence, normalize_candidate


@dataclass(frozen=True)
class AutomaticRemediationProposal:
    track_id: int
    expected_fingerprint: str
    values: Mapping[str, str]
    recording_id: str | None
    release_id: str | None
    release_group_id: str | None
    recording_reference: str | None
    release_reference: str | None
    confidence: float | None
    effective_change: bool
    evidence: tuple[MetadataEvidence, ...]
    proposal_key: str
    actor: str = "remediation"
    reason: str = "musicbrainz_high_confidence"
    protect_shared_catalogue: bool = True
    suppress_noop_writes: bool = True


def automatic_remediation_proposal(
    *, track_id: int, fingerprint: str, values: dict[str, str], recording_id: str | None,
    release_id: str | None, release_group_id: str | None, recording_reference: str | None,
    release_reference: str | None, confidence: float | None, effective_change: bool,
    proposal_revision: str | None,
) -> AutomaticRemediationProposal:
    """Serialize only bounded normalized facts, not HTTP payloads or media paths."""
    candidate = {name: value for name, value in values.items() if name != "artwork"}
    candidate.update(provider="musicbrainz", recording_id=recording_id,
                     release_id=release_id, release_group_id=release_group_id)
    evidence = (normalize_candidate(candidate),) if candidate.keys() - {"provider", "recording_id", "release_id", "release_group_id"} else ()
    payload = {
        "policy": "automatic-remediation-v1", "track_id": track_id, "fingerprint": fingerprint,
        "revision": proposal_revision, "values": values, "recording_id": recording_id,
        "release_id": release_id, "release_group_id": release_group_id,
        "recording_reference": recording_reference, "release_reference": release_reference,
        "confidence": confidence, "evidence": [item.evidence_key for item in evidence],
    }
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return AutomaticRemediationProposal(
        track_id, fingerprint, MappingProxyType(dict(values)), recording_id, release_id,
        release_group_id, recording_reference, release_reference, confidence, effective_change,
        evidence, "automatic-remediation-v1:" + key,
    )
