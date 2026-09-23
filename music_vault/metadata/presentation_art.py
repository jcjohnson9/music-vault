"""Identity-qualified, reversible presentation artwork (never embedded tags).

An image candidate is not acceptance authority. A surviving materialization
which introduced the currently accepted edition supplies that authority. Older
libraries without such a witness conservatively keep their existing image.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .artwork import ArtworkError, MAX_ARTWORK_BYTES, prepare_artwork_bytes
from .discogs_artwork import (
    ATTRIBUTION_STATE, DiscogsArtworkRecord, validate_discogs_image_url,
    validate_discogs_release_url,
)
from .evidence import MetadataEvidence
from .materializer import (
    StaleMetadataProposal, _writer, capture_metadata_state, materialize_proposal, state_fingerprint,
)
from .schema import MATERIALIZED_COLUMNS
from .service import MetadataChangeResult, MetadataService


@dataclass(frozen=True)
class ArtworkAssetInfo:
    reference: str | None
    sha256: str | None
    width: int = 0
    height: int = 0
    status: str = "missing"


def current_asset_info(reference: str | Path | None) -> ArtworkAssetInfo:
    """Bounded image-only read; missing and corrupt images remain distinguishable."""
    value = str(reference) if reference else None
    if not value:
        return ArtworkAssetInfo(None, None)
    path = Path(value)
    try:
        if path.is_symlink():
            return ArtworkAssetInfo(value, None, status="unavailable")
        with path.open("rb") as stream:
            payload = stream.read(MAX_ARTWORK_BYTES + 1)
    except FileNotFoundError:
        return ArtworkAssetInfo(value, None)
    except OSError:
        return ArtworkAssetInfo(value, None, status="unavailable")
    if len(payload) > MAX_ARTWORK_BYTES:
        return ArtworkAssetInfo(value, None, status="unavailable")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        image = prepare_artwork_bytes(payload)
    except ArtworkError:
        return ArtworkAssetInfo(value, digest, status="invalid")
    return ArtworkAssetInfo(value, digest, image.width, image.height, "valid")


def _identity_state(state: dict) -> dict:
    # Artwork and bookkeeping freshness do not establish a catalogue identity.
    # Everything else in the accepted graph must still agree, including manual
    # flags, credits, editions, album memberships and shared entity facts.
    result = json.loads(json.dumps(state))
    result["track"].pop(MATERIALIZED_COLUMNS["artwork"], None)
    result["track_metadata_fields"] = [
        row for row in result["track_metadata_fields"] if row["field_name"] != "artwork"
    ]
    for rows in result.values():
        for row in rows if isinstance(rows, list) else (rows,):
            for name in ("updated_at", "metadata_updated_at"):
                row.pop(name, None)
    return result


@dataclass(frozen=True)
class AcceptedReleaseWitness:
    track_id: int
    release_id: str
    master_id: str | None
    journal_id: str
    evidence_key: str
    identity_fingerprint: str


def accepted_release_witness(conn, track_id: int, release_id: str) -> AcceptedReleaseWitness | None:
    """Require accepted identity, not merely stored or rejected provider evidence."""
    state = capture_metadata_state(conn, track_id)
    identity = str(release_id or "")
    if str(state["track"]["discogs_release_id"] or "") != identity or not identity:
        return None
    current = state_fingerprint(_identity_state(state))
    master = state["track"]["discogs_master_id"]
    master = str(master) if master is not None else None
    for row in conn.execute(
        "SELECT id,before_json,after_json,evidence_keys_json FROM metadata_materializations "
        "WHERE track_id=? AND undone_at IS NULL ORDER BY rowid DESC", (int(track_id),),
    ):
        try:
            before, after, keys = (json.loads(row[index]) for index in (1, 2, 3))
            # An unchanged legacy ID plus newly saved rejected evidence is not
            # an accepted-release witness. Require the recorded introduction.
            if str(before["track"]["discogs_release_id"] or "") == identity:
                continue
            if str(after["track"]["discogs_release_id"] or "") != identity:
                continue
            if state_fingerprint(_identity_state(after)) != current:
                continue
            for key in keys:
                saved = conn.execute(
                    "SELECT payload_json FROM metadata_evidence_bundles WHERE track_id=? AND evidence_key=?",
                    (int(track_id), key),
                ).fetchone()
                if saved is None:
                    continue
                evidence = MetadataEvidence.from_dict(json.loads(saved[0]))
                if (evidence.evidence_key != key or evidence.provider != "discogs"
                        or evidence.edition is None or evidence.edition.entity_id != identity):
                    continue
                family = evidence.release_family.entity_id if evidence.release_family else None
                if family != master:
                    continue
                return AcceptedReleaseWitness(int(track_id), identity, master, str(row[0]), key, current)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return None


@dataclass(frozen=True)
class ArtworkUpgradeDecision:
    reason: str
    track_id: int | None = None
    expected_fingerprint: str | None = None
    witness: AcceptedReleaseWitness | None = None
    current_asset: ArtworkAssetInfo | None = None
    replacement_class: str | None = None

    @property
    def allowed(self) -> bool:
        return self.reason == "eligible"


def prepare_artwork_upgrade(state: dict, witness: AcceptedReleaseWitness | None,
                            asset: ArtworkAssetInfo) -> ArtworkUpgradeDecision:
    """Pure eligibility: manual authority first, then identity, then image class."""
    fields = {row["field_name"]: row for row in state["track_metadata_fields"]}
    art = fields.get("artwork")
    if art is None:
        return ArtworkUpgradeDecision("artwork_state_unavailable")
    if (art["is_manual"] or art["is_locked"] or art["provenance"] == "manual"
            or str(art["provenance"]).endswith("_confirmed")):
        return ArtworkUpgradeDecision("preserved_manual_artwork")
    if (witness is None or int(art["track_id"]) != witness.track_id
            or state_fingerprint(_identity_state(state)) != witness.identity_fingerprint):
        return ArtworkUpgradeDecision("accepted_release_witness_unavailable")
    if art["value"] != state["track"][MATERIALIZED_COLUMNS["artwork"]]:
        return ArtworkUpgradeDecision("artwork_state_inconsistent")
    if asset.reference != art["value"] or asset.status == "unavailable":
        return ArtworkUpgradeDecision("artwork_asset_unavailable")
    if asset.status in {"missing", "invalid"}:
        kind = "gap"
    elif art["provenance"] == "youtube_thumbnail":
        kind = "source_thumbnail"
    else:
        return ArtworkUpgradeDecision("preserved_existing_artwork")
    return ArtworkUpgradeDecision("eligible", witness.track_id, state_fingerprint(state), witness, asset, kind)


@dataclass(frozen=True)
class _ArtworkIntent:
    track_id: int
    expected_fingerprint: str
    proposal_key: str
    evidence: tuple = ()
    protect_shared_catalogue: bool = True
    suppress_noop_writes: bool = True
    actor: str = "metadata_intelligence"
    reason: str = "accepted_release_presentation_art"


def validate_staged_artwork(record: DiscogsArtworkRecord, decision: ArtworkUpgradeDecision) -> ArtworkAssetInfo:
    if not isinstance(record, DiscogsArtworkRecord) or record.release_id != decision.witness.release_id:
        raise ValueError("artwork_release_mismatch")
    validate_discogs_release_url(record.provider_page_url, record.release_id)
    validate_discogs_image_url(record.source_url, resolve_dns=False)
    validate_discogs_image_url(record.delivery_url, resolve_dns=False)
    if record.attribution_state != ATTRIBUTION_STATE or not record.attribution_text:
        raise ValueError("artwork_attribution_required")
    asset = current_asset_info(record.path)
    if (asset.status != "valid" or asset.sha256 != record.sha256
            or (asset.width, asset.height) != (record.width, record.height)
            or Path(record.path).stem != record.sha256):
        raise ValueError("artwork_staged_asset_changed")
    # A release front is a semantic upgrade over a source thumbnail; it need
    # not exceed a video frame's resolution. Reject tiny or banner-like fronts
    # and retain at least the old short edge up to a useful 600-pixel cover.
    minimum = max(320, min(600, min(decision.current_asset.width, decision.current_asset.height)))
    if min(asset.width, asset.height) < minimum or max(asset.width, asset.height) > min(asset.width, asset.height) * 1.5:
        raise ValueError("artwork_front_quality_insufficient")
    return asset


def apply_artwork_upgrade(database, decision: ArtworkUpgradeDecision,
                          staged: DiscogsArtworkRecord) -> MetadataChangeResult:
    """Recheck under the writer and journal only the effective artwork pointer."""
    if not decision.allowed or decision.witness is None or decision.current_asset is None:
        raise ValueError("artwork_upgrade_not_authorized")
    conn = database.conn if hasattr(database, "conn") else database
    metadata = MetadataService(conn)
    before = metadata.snapshot(decision.track_id)
    validate_staged_artwork(staged, decision)
    key = state_fingerprint({"decision": decision.expected_fingerprint, "witness": decision.witness.__dict__,
                             "source": decision.current_asset.__dict__, "image": staged.sha256,
                             "reference": staged.provider_page_url})
    intent = _ArtworkIntent(decision.track_id, decision.expected_fingerprint, "presentation-art-v1:" + key)
    with _writer(conn):
        previous = conn.execute(
            "SELECT undone_at FROM metadata_materializations WHERE proposal_key=?", (intent.proposal_key,),
        ).fetchone()
        if previous is not None and previous[0] is not None:
            restored = capture_metadata_state(conn, decision.track_id)
            witness = accepted_release_witness(conn, decision.track_id, decision.witness.release_id)
            if (state_fingerprint(restored) != decision.expected_fingerprint or witness != decision.witness
                    or current_asset_info(decision.current_asset.reference) != decision.current_asset
                    or not prepare_artwork_upgrade(restored, witness, decision.current_asset).allowed):
                raise StaleMetadataProposal("artwork_undone_proposal_changed")
            validate_staged_artwork(staged, decision)
            # Undo is not permission for an automatic rescan to redo the same
            # image decision. Preserve the restored graph without new history.
            current = metadata.snapshot(decision.track_id)
            return MetadataChangeResult(decision.track_id, None, frozenset(), current, current)
        return _materialize_artwork(conn, metadata, before, intent, decision, staged)


def _materialize_artwork(conn, metadata, before, intent, decision, staged):
    with materialize_proposal(conn, intent) as transaction:
        if transaction.already_applied:
            return MetadataChangeResult(decision.track_id, None, frozenset(), before, before)
        witness = accepted_release_witness(conn, decision.track_id, decision.witness.release_id)
        if witness != decision.witness:
            raise StaleMetadataProposal("artwork_accepted_release_changed")
        if current_asset_info(decision.current_asset.reference) != decision.current_asset:
            raise StaleMetadataProposal("artwork_source_asset_changed")
        if not prepare_artwork_upgrade(transaction.before, witness, decision.current_asset).allowed:
            raise StaleMetadataProposal("artwork_authority_changed")
        validate_staged_artwork(staged, decision)
        if decision.current_asset.sha256 == staged.sha256:
            return MetadataChangeResult(decision.track_id, None, frozenset(), before, before)
        result = metadata.record_source_observations(
            decision.track_id, provider="discogs_high_confidence", values={"artwork": str(staged.path)},
            provider_reference=staged.provider_page_url, confidence=85.0,
            actor=intent.actor, reason=intent.reason, commit=False,
        )
        if result.changed_fields != frozenset({"artwork"}):
            raise ValueError("artwork_effective_authority_rejected")
        if _identity_state(capture_metadata_state(conn, decision.track_id)) != _identity_state(transaction.before):
            raise ValueError("artwork_changed_non_artwork_metadata")
    return MetadataChangeResult(decision.track_id, transaction.identifier, result.changed_fields, before, result.after)
