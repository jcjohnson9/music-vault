from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


_PROVIDER_ID_COLUMNS = {
    "discogs": "discogs_artist_id",
    "musicbrainz": "musicbrainz_artist_id",
}
_ACCEPTED_INTELLIGENCE_STATES = {"applied", "applied_with_gaps"}
_GROUP_ENTITY_TYPES = {"group", "band", "duo", "orchestra", "collective"}
_MINIMUM_PROVIDER_CONFIDENCE = 85.0


class ArtistRelationshipEvidenceError(ValueError):
    """Raised when relationship evidence cannot identify both artists safely."""


@dataclass(frozen=True)
class ArtistIdentityEvidence:
    """Stable artist identity references; display names are deliberately absent."""

    artist_id: int | None = None
    discogs_artist_id: str | None = None
    musicbrainz_artist_id: str | None = None


@dataclass(frozen=True)
class MemberOfEvidence:
    member: ArtistIdentityEvidence
    group: ArtistIdentityEvidence
    provenance: str
    provider_reference: str
    confidence: float
    manual_confirmation: bool = False


@dataclass(frozen=True)
class ArtistRelationship:
    id: int
    subject_artist_id: int
    related_artist_id: int
    relationship_kind: str
    provenance: str
    provider_reference: str | None
    confidence: float | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SavedRelationshipImportReport:
    scanned_item_count: int
    accepted_relationship_count: int
    rejected_item_count: int


def _clean_identifier(value: object) -> str | None:
    identifier = str(value or "").strip()
    return identifier or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ArtistRelationshipService:
    """Persist verified artist relationships without inferring them from names.

    Automatic relationships require stable provider IDs for both endpoints.
    The manual path requires explicit database artist IDs and a confirmation
    reference.  Saved provider evidence is accepted only from an already
    applied intelligence item and is resolved through the same ID checks.
    """

    def __init__(self, database: Any) -> None:
        self.conn: sqlite3.Connection = getattr(database, "conn", database)
        if not isinstance(self.conn, sqlite3.Connection):
            raise TypeError("ArtistRelationshipService requires a SQLite connection.")
        self.conn.row_factory = sqlite3.Row
        required = {"artists", "artist_relationships"}
        present = {
            str(row[0])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(
                "Artist relationship schema is unavailable: " + ", ".join(missing)
            )

    @contextmanager
    def _transaction(self, *, commit: bool = True):
        if not commit:
            yield
            return
        if not self.conn.in_transaction:
            with self.conn:
                yield
            return
        name = f"artist_relationship_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {name}")

    @staticmethod
    def _relationship(row: sqlite3.Row) -> ArtistRelationship:
        return ArtistRelationship(
            id=int(row["id"]),
            subject_artist_id=int(row["subject_artist_id"]),
            related_artist_id=int(row["related_artist_id"]),
            relationship_kind=str(row["relationship_kind"]),
            provenance=str(row["provenance"]),
            provider_reference=row["provider_reference"],
            confidence=(
                float(row["confidence"]) if row["confidence"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _identity_from_mapping(value: object) -> ArtistIdentityEvidence:
        if not isinstance(value, Mapping):
            raise ArtistRelationshipEvidenceError(
                "Relationship endpoints must be structured identity mappings."
            )
        raw_artist_id = value.get("artist_id")
        try:
            artist_id = int(raw_artist_id) if raw_artist_id not in (None, "") else None
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArtistRelationshipEvidenceError(
                "Relationship database artist ID is invalid."
            ) from exc
        return ArtistIdentityEvidence(
            artist_id=artist_id,
            discogs_artist_id=_clean_identifier(value.get("discogs_artist_id")),
            musicbrainz_artist_id=_clean_identifier(
                value.get("musicbrainz_artist_id")
            ),
        )

    def _resolve_identity(
        self,
        evidence: ArtistIdentityEvidence,
        *,
        required_provider: str | None,
    ) -> sqlite3.Row:
        references: list[tuple[str, object]] = []
        if evidence.artist_id is not None:
            references.append(("id", int(evidence.artist_id)))
        if _clean_identifier(evidence.discogs_artist_id) is not None:
            references.append(
                ("discogs_artist_id", _clean_identifier(evidence.discogs_artist_id))
            )
        if _clean_identifier(evidence.musicbrainz_artist_id) is not None:
            references.append(
                (
                    "musicbrainz_artist_id",
                    _clean_identifier(evidence.musicbrainz_artist_id),
                )
            )
        if not references:
            raise ArtistRelationshipEvidenceError(
                "A relationship endpoint requires a database or provider artist ID."
            )
        if required_provider is not None:
            required_column = _PROVIDER_ID_COLUMNS[required_provider]
            if not any(column == required_column for column, _ in references):
                raise ArtistRelationshipEvidenceError(
                    f"{required_provider.title()} evidence requires IDs for both artists."
                )

        resolved: list[sqlite3.Row] = []
        for column, identifier in references:
            row = self.conn.execute(
                f"SELECT * FROM artists WHERE {column}=?", (identifier,)
            ).fetchone()
            if row is None:
                raise ArtistRelationshipEvidenceError(
                    "Relationship evidence references an unknown artist identity."
                )
            resolved.append(row)
        resolved_ids = {int(row["id"]) for row in resolved}
        if len(resolved_ids) != 1:
            raise ArtistRelationshipEvidenceError(
                "Relationship evidence resolves to conflicting artist identities."
            )
        return resolved[0]

    def record_member_of(
        self,
        evidence: MemberOfEvidence,
        *,
        commit: bool = True,
    ) -> ArtistRelationship:
        provenance = str(evidence.provenance or "").strip().casefold()
        reference = str(evidence.provider_reference or "").strip()
        if not reference or len(reference) > 1000:
            raise ArtistRelationshipEvidenceError(
                "Relationship evidence requires a bounded audit reference."
            )
        try:
            confidence = float(evidence.confidence)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArtistRelationshipEvidenceError(
                "Relationship confidence is invalid."
            ) from exc
        if not 0 <= confidence <= 100:
            raise ArtistRelationshipEvidenceError(
                "Relationship confidence must be between 0 and 100."
            )

        if evidence.manual_confirmation:
            if provenance != "manual":
                raise ArtistRelationshipEvidenceError(
                    "Manual relationship confirmation must use manual provenance."
                )
            if evidence.member.artist_id is None or evidence.group.artist_id is None:
                raise ArtistRelationshipEvidenceError(
                    "Manual relationship confirmation requires both database artist IDs."
                )
            required_provider = None
            confidence = 100.0
        else:
            if provenance not in _PROVIDER_ID_COLUMNS:
                raise ArtistRelationshipEvidenceError(
                    "Automatic relationships require Discogs or MusicBrainz evidence."
                )
            if confidence < _MINIMUM_PROVIDER_CONFIDENCE:
                raise ArtistRelationshipEvidenceError(
                    "Provider relationship confidence is below the safe threshold."
                )
            required_provider = provenance

        member = self._resolve_identity(
            evidence.member, required_provider=required_provider
        )
        group = self._resolve_identity(evidence.group, required_provider=required_provider)
        member_id = int(member["id"])
        group_id = int(group["id"])
        if member_id == group_id:
            raise ArtistRelationshipEvidenceError("An artist cannot be a member of itself.")
        if str(group["entity_type"] or "unknown").casefold() not in _GROUP_ENTITY_TYPES:
            raise ArtistRelationshipEvidenceError(
                "The related artist is not stored as a group identity."
            )
        reciprocal = self.conn.execute(
            """
            SELECT 1 FROM artist_relationships
            WHERE subject_artist_id=? AND related_artist_id=?
              AND relationship_kind='member_of'
            """,
            (group_id, member_id),
        ).fetchone()
        if reciprocal is not None:
            raise ArtistRelationshipEvidenceError(
                "Reciprocal member-of evidence is ambiguous."
            )

        now = _now()
        with self._transaction(commit=commit):
            existing = self.conn.execute(
                """
                SELECT * FROM artist_relationships
                WHERE subject_artist_id=? AND related_artist_id=?
                  AND relationship_kind='member_of'
                """,
                (member_id, group_id),
            ).fetchone()
            if existing is None:
                relationship_id = int(
                    self.conn.execute(
                        """
                        INSERT INTO artist_relationships (
                            subject_artist_id, related_artist_id,
                            relationship_kind, provenance, provider_reference,
                            confidence, created_at, updated_at
                        ) VALUES (?, ?, 'member_of', ?, ?, ?, ?, ?)
                        """,
                        (
                            member_id,
                            group_id,
                            provenance,
                            reference,
                            confidence,
                            now,
                            now,
                        ),
                    ).lastrowid
                )
            else:
                relationship_id = int(existing["id"])
                existing_manual = str(existing["provenance"]).casefold() == "manual"
                # Manual confirmation is authoritative. Provider evidence may
                # strengthen the same stored fact, but never replaces a manual
                # audit trail or rewrites one provider reference as another.
                if evidence.manual_confirmation and not existing_manual:
                    self.conn.execute(
                        """
                        UPDATE artist_relationships
                        SET provenance='manual', provider_reference=?, confidence=100,
                            updated_at=?
                        WHERE id=?
                        """,
                        (reference, now, relationship_id),
                    )
                elif (
                    not existing_manual
                    and str(existing["provenance"]) == provenance
                    and confidence > float(existing["confidence"] or 0)
                ):
                    self.conn.execute(
                        """
                        UPDATE artist_relationships
                        SET confidence=MAX(COALESCE(confidence, 0), ?), updated_at=?
                        WHERE id=?
                        """,
                        (confidence, now, relationship_id),
                    )
            stored = self.conn.execute(
                "SELECT * FROM artist_relationships WHERE id=?",
                (relationship_id,),
            ).fetchone()
        assert stored is not None
        return self._relationship(stored)

    def record_provider_member_of(
        self,
        *,
        provider: str,
        member_provider_id: object,
        group_provider_id: object,
        provider_reference: object,
        confidence: float,
        commit: bool = True,
    ) -> ArtistRelationship:
        normalized_provider = str(provider or "").strip().casefold()
        if normalized_provider not in _PROVIDER_ID_COLUMNS:
            raise ArtistRelationshipEvidenceError(
                "Only Discogs or MusicBrainz provider identities are supported."
            )
        member_id = _clean_identifier(member_provider_id)
        group_id = _clean_identifier(group_provider_id)
        if member_id is None or group_id is None:
            raise ArtistRelationshipEvidenceError(
                "Provider relationships require IDs for both artists."
            )
        member = ArtistIdentityEvidence(
            **{_PROVIDER_ID_COLUMNS[normalized_provider]: member_id}
        )
        group = ArtistIdentityEvidence(
            **{_PROVIDER_ID_COLUMNS[normalized_provider]: group_id}
        )
        return self.record_member_of(
            MemberOfEvidence(
                member=member,
                group=group,
                provenance=normalized_provider,
                provider_reference=str(provider_reference or ""),
                confidence=confidence,
            ),
            commit=commit,
        )

    def record_manual_member_of(
        self,
        *,
        member_artist_id: int,
        group_artist_id: int,
        confirmation_reference: object,
        commit: bool = True,
    ) -> ArtistRelationship:
        return self.record_member_of(
            MemberOfEvidence(
                member=ArtistIdentityEvidence(artist_id=int(member_artist_id)),
                group=ArtistIdentityEvidence(artist_id=int(group_artist_id)),
                provenance="manual",
                provider_reference=str(confirmation_reference or ""),
                confidence=100.0,
                manual_confirmation=True,
            ),
            commit=commit,
        )

    def record_member_of_from_saved_evidence(
        self,
        intelligence_item_id: int,
        *,
        commit: bool = True,
    ) -> tuple[ArtistRelationship, ...]:
        """Import normalized relationships from one accepted saved proposal.

        Expected provider summaries use ``artist_relationships`` with nested
        ``member`` and ``group`` identity mappings.  Names are ignored and
        cannot identify an endpoint.
        """

        row = self.conn.execute(
            """
            SELECT state, field_proposal, field_confidence, provider_agreement
            FROM metadata_intelligence_items WHERE id=?
            """,
            (int(intelligence_item_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"Intelligence item {intelligence_item_id} does not exist.")
        if str(row["state"]).casefold() not in _ACCEPTED_INTELLIGENCE_STATES:
            raise ArtistRelationshipEvidenceError(
                "Saved relationship evidence has not been accepted."
            )
        provider_agreement = str(row["provider_agreement"] or "").casefold()
        if provider_agreement in {
            "conflict",
            "disagree",
            "provider_disagreement",
        }:
            raise ArtistRelationshipEvidenceError(
                "Conflicting provider evidence cannot create a relationship."
            )
        try:
            proposal = json.loads(str(row["field_proposal"] or "{}"))
            confidence_payload = json.loads(str(row["field_confidence"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtistRelationshipEvidenceError(
                "Saved relationship evidence is not valid JSON."
            ) from exc
        if not isinstance(proposal, Mapping) or not isinstance(
            confidence_payload, Mapping
        ):
            raise ArtistRelationshipEvidenceError(
                "Saved relationship evidence must be structured mappings."
            )
        try:
            default_confidence = float(
                confidence_payload.get("artist_relationships", 0)
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArtistRelationshipEvidenceError(
                "Saved relationship confidence is invalid."
            ) from exc

        pending: list[MemberOfEvidence] = []
        for provider in _PROVIDER_ID_COLUMNS:
            provider_payload = proposal.get(f"_{provider}")
            if provider_payload in (None, {}):
                continue
            if not isinstance(provider_payload, Mapping):
                raise ArtistRelationshipEvidenceError(
                    "Saved provider relationship evidence must be a mapping."
                )
            relationships = provider_payload.get("artist_relationships", ())
            if relationships in (None, (), []):
                continue
            accepted_agreements = {"agreed", f"{provider}_only"}
            if provider_agreement not in accepted_agreements:
                raise ArtistRelationshipEvidenceError(
                    "Saved relationship provenance is not accepted for this provider."
                )
            if not isinstance(relationships, Sequence) or isinstance(
                relationships, (str, bytes, bytearray)
            ):
                raise ArtistRelationshipEvidenceError(
                    "Saved artist relationships must be a sequence."
                )
            for value in relationships:
                if not isinstance(value, Mapping):
                    raise ArtistRelationshipEvidenceError(
                        "Saved artist relationship entries must be mappings."
                    )
                kind = str(
                    value.get("relationship_kind") or value.get("kind") or ""
                ).strip().casefold()
                if kind != "member_of":
                    continue
                member = self._identity_from_mapping(value.get("member"))
                group = self._identity_from_mapping(value.get("group"))
                try:
                    confidence = float(value.get("confidence", default_confidence))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ArtistRelationshipEvidenceError(
                        "Saved relationship confidence is invalid."
                    ) from exc
                reference = str(
                    value.get("provider_reference")
                    or provider_payload.get("provider_reference")
                    or ""
                ).strip()
                pending.append(
                    MemberOfEvidence(
                        member=member,
                        group=group,
                        provenance=provider,
                        provider_reference=reference,
                        confidence=confidence,
                    )
                )

        if not pending:
            return ()
        relationships: list[ArtistRelationship] = []
        with self._transaction(commit=commit):
            for evidence in pending:
                relationships.append(self.record_member_of(evidence, commit=False))
        return tuple(relationships)


def import_accepted_saved_artist_relationships(
    database: Any,
    intelligence_item_ids: Sequence[int] | None = None,
) -> SavedRelationshipImportReport:
    """Import accepted stored relationship evidence without provider access.

    Each intelligence item is its own atomic unit. Invalid, ambiguous, or
    conflicting evidence is rejected without preventing another accepted item
    from being imported. Unexpected SQLite/schema failures still propagate.
    """

    conn: sqlite3.Connection = getattr(database, "conn", database)
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("Saved relationship import requires a SQLite connection.")
    conn.row_factory = sqlite3.Row
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {"artists", "artist_relationships", "metadata_intelligence_items"}
    if not required <= tables:
        return SavedRelationshipImportReport(0, 0, 0)

    ids = list(
        dict.fromkeys(int(value) for value in (intelligence_item_ids or ()))
    )
    if intelligence_item_ids is not None and not ids:
        return SavedRelationshipImportReport(0, 0, 0)
    parameters: list[object] = []
    item_filter = ""
    if intelligence_item_ids is not None:
        item_filter = f"AND id IN ({','.join('?' for _ in ids)})"
        parameters.extend(ids)
    rows = conn.execute(
        f"""
        SELECT id
        FROM metadata_intelligence_items
        WHERE state IN ('applied', 'applied_with_gaps')
          {item_filter}
        ORDER BY id
        """,
        parameters,
    ).fetchall()
    service = ArtistRelationshipService(conn)
    accepted = rejected = 0
    for row in rows:
        try:
            relationships = service.record_member_of_from_saved_evidence(
                int(row["id"])
            )
        except ArtistRelationshipEvidenceError:
            rejected += 1
            continue
        accepted += len(relationships)
    return SavedRelationshipImportReport(len(rows), accepted, rejected)


__all__ = [
    "ArtistIdentityEvidence",
    "ArtistRelationship",
    "ArtistRelationshipEvidenceError",
    "ArtistRelationshipService",
    "MemberOfEvidence",
    "SavedRelationshipImportReport",
    "import_accepted_saved_artist_relationships",
]
