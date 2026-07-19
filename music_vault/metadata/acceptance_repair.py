"""Offline, transaction-aware Batch 10.5 stored-evidence repair.

This production-layer orchestrator is intentionally provider-free.  It is
used by future pre-schema-7 migrations and by explicit acceptance tooling; it
is not called during ordinary schema-7 startup.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from .artist_consolidation import ArtistConsolidationService
from .artist_relationships import import_accepted_saved_artist_relationships
from .canonical_albums import upsert_track_canonical_album
from .review_reclassification import best_available_reclassify


METADATA_ACCEPTANCE_REPAIR_MARKER = "batch10_5_metadata_acceptance_repair_v1"
_NONTERMINAL_STORED_EVIDENCE_STATES = ("review", "ready", "no_match")
SAFE_DIAGNOSTIC_ARTIST_CONFLICT_REASONS = frozenset(
    {
        "discogs_id_conflict",
        "musicbrainz_id_conflict",
        "accepted_discogs_artist_id_conflict",
        "accepted_musicbrainz_artist_id_conflict",
        "accepted_provider_context_ambiguous",
        "ambiguous_exact_same_name",
        "person_group_conflict",
        "relationship_evidence_conflict",
        "credit_collision",
    }
)


def unexpected_artist_identity_conflict_count(plan: object) -> int:
    return sum(
        1
        for conflict in getattr(plan, "conflicts", ())
        if str(getattr(conflict, "reason", ""))
        not in SAFE_DIAGNOSTIC_ARTIST_CONFLICT_REASONS
    )


def _assert_expected_identity_conflicts(plan: object) -> None:
    if unexpected_artist_identity_conflict_count(plan):
        raise RuntimeError("Stored artist identity evidence is ambiguous.")


@dataclass(frozen=True, slots=True)
class MetadataAcceptanceRepairReport:
    no_op: bool
    marker_written: bool
    merged_artist_count: int = 0
    reassigned_credit_count: int = 0
    aliases_preserved: int = 0
    version_repairs: int = 0
    full_credit_repairs: int = 0
    identity_conflicts: int = 0
    deleted_artist_count: int = 0
    deleted_credit_count: int = 0
    review_items_reclassified: int = 0
    reversed_orientation_repairs: int = 0
    metadata_fields_applied: int = 0
    album_fields_applied: int = 0
    canonical_album_tracks_processed: int = 0
    imported_relationships: int = 0
    operational_failures: int = 0


def _connection(database: object) -> sqlite3.Connection:
    conn = getattr(database, "conn", database)
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("Metadata acceptance repair requires a SQLite database.")
    conn.row_factory = sqlite3.Row
    return conn


def metadata_acceptance_repair_applied(database: object) -> bool:
    conn = _connection(database)
    return conn.execute(
        "SELECT 1 FROM app_meta WHERE key=?",
        (METADATA_ACCEPTANCE_REPAIR_MARKER,),
    ).fetchone() is not None


@contextmanager
def _transaction(conn: sqlite3.Connection):
    if not conn.in_transaction:
        with conn:
            yield
        return
    name = f"metadata_acceptance_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def apply_metadata_acceptance_repair(
    database: object,
) -> MetadataAcceptanceRepairReport:
    """Apply stored-evidence corrections atomically and mark completion.

    Provider clients, media files, embedded tags, and artist-image files are
    outside this function's dependency boundary.  A surrounding migration
    transaction remains owned by its caller through a savepoint.
    """

    conn = _connection(database)
    if metadata_acceptance_repair_applied(database):
        return MetadataAcceptanceRepairReport(no_op=True, marker_written=False)

    service = ArtistConsolidationService(database)
    initial_plan = service.plan()
    _assert_expected_identity_conflicts(initial_plan)
    with _transaction(conn):
        initial_consolidation = service.apply(initial_plan)
        review = best_available_reclassify(database, apply=True)
        # A reported operational failure is an intentionally terminal Failed
        # classification. Raised exceptions still abort this transaction.
        # Legacy Review rows become eligible for structured-credit repair only
        # after terminalization. Re-plan inside the same transaction so saved
        # provider credits cannot leave a combined full-credit artist behind.
        final_service = ArtistConsolidationService(database)
        final_plan = final_service.plan()
        _assert_expected_identity_conflicts(final_plan)
        final_consolidation = final_service.apply(final_plan)
        imported_relationships = import_accepted_saved_artist_relationships(conn)
        track_ids = tuple(
            int(row[0]) for row in conn.execute("SELECT id FROM tracks ORDER BY id")
        )
        for track_id in track_ids:
            upsert_track_canonical_album(conn, track_id)

        placeholder_memberships = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM track_album_memberships AS membership
                JOIN tracks AS track ON track.id=membership.track_id
                WHERE LOWER(TRIM(COALESCE(track.album, ''))) IN (
                    '', 'unknown', 'unknown album'
                )
                """
            ).fetchone()[0]
        )
        if placeholder_memberships:
            raise RuntimeError(
                "Stored metadata acceptance left uncatalogued album memberships."
            )

        placeholders = ",".join("?" for _ in _NONTERMINAL_STORED_EVIDENCE_STATES)
        remaining = int(
            conn.execute(
                f"SELECT COUNT(*) FROM metadata_intelligence_items "
                f"WHERE state IN ({placeholders})",
                _NONTERMINAL_STORED_EVIDENCE_STATES,
            ).fetchone()[0]
        )
        if remaining:
            raise RuntimeError(
                "Stored metadata acceptance left nonterminal evidence rows."
            )
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(?,?)",
            (METADATA_ACCEPTANCE_REPAIR_MARKER, "1"),
        )

    relationship_count = int(imported_relationships.accepted_relationship_count)
    consolidations = (initial_consolidation, final_consolidation)
    return MetadataAcceptanceRepairReport(
        no_op=False,
        marker_written=True,
        merged_artist_count=sum(int(item.merged_artist_count) for item in consolidations),
        reassigned_credit_count=sum(
            int(item.reassigned_credit_count) for item in consolidations
        ),
        aliases_preserved=sum(int(item.aliases_preserved) for item in consolidations),
        version_repairs=sum(int(item.version_repairs) for item in consolidations),
        full_credit_repairs=sum(
            int(item.full_credit_repairs) for item in consolidations
        ),
        identity_conflicts=sum(int(item.conflict_count) for item in consolidations),
        deleted_artist_count=sum(
            int(item.deleted_artist_count) for item in consolidations
        ),
        deleted_credit_count=sum(
            int(item.deleted_credit_count) for item in consolidations
        ),
        review_items_reclassified=int(review.changed),
        reversed_orientation_repairs=int(review.reversed_orientation_repairs),
        metadata_fields_applied=int(review.safe_fields_applied),
        album_fields_applied=int(review.album_fields_applied),
        canonical_album_tracks_processed=len(track_ids),
        imported_relationships=relationship_count,
        operational_failures=int(review.operational_failures),
    )


__all__ = [
    "METADATA_ACCEPTANCE_REPAIR_MARKER",
    "MetadataAcceptanceRepairReport",
    "apply_metadata_acceptance_repair",
    "metadata_acceptance_repair_applied",
    "SAFE_DIAGNOSTIC_ARTIST_CONFLICT_REASONS",
    "unexpected_artist_identity_conflict_count",
]
