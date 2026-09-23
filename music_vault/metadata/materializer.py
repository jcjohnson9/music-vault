"""Atomic, revision-checked materialization of reviewed metadata proposals.

Only SQLite metadata is touched here. Provider work and image acquisition must
finish before entering this boundary; audio files and tags are never opened.
The journal retains the structured state needed for conflict-aware reversal.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import uuid

from .schema import MATERIALIZED_COLUMNS, utc_now


class StaleMetadataProposal(ValueError):
    """The library changed after analysis; fresh analysis is required."""


TRACK_COLUMNS = tuple(dict.fromkeys((
    *MATERIALIZED_COLUMNS.values(), "year", "musicbrainz_recording_id",
    "musicbrainz_release_id", "discogs_release_id", "discogs_master_id",
    "discogs_track_position", "recording_group_key", "metadata_updated_at",
    "updated_at",
)))
TRACK_TABLES = (
    "track_metadata_fields", "track_artist_credits", "track_release_context",
    "track_album_memberships",
)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def state_fingerprint(state: dict) -> str:
    return hashlib.sha256(_json(state).encode("utf-8")).hexdigest()


def structural_change_fields(before: dict, after: dict) -> frozenset[str]:
    """Map identity changes to existing consumer invalidation categories."""
    changed = set()
    if before["track_artist_credits"] != after["track_artist_credits"] or before["artists"] != after["artists"]:
        changed.add("artist")
    if any(before[name] != after[name] for name in (
        "track_release_context", "track_album_memberships", "canonical_albums",
    )) or any(before["track"][name] != after["track"][name] for name in (
        "discogs_release_id", "discogs_master_id", "discogs_track_position", "musicbrainz_release_id",
    )):
        changed.add("album")
    if any(before["track"][name] != after["track"][name] for name in (
        "musicbrainz_recording_id", "recording_group_key",
    )):
        changed.add("title")
    return frozenset(changed)


def capture_metadata_state(conn: sqlite3.Connection, track_id: int) -> dict:
    """Read only this track's metadata and its referenced catalogue entities."""
    track = conn.execute(
        f"SELECT {','.join(TRACK_COLUMNS)} FROM tracks WHERE id=?", (int(track_id),)
    ).fetchone()
    if track is None:
        raise KeyError("metadata_track_unavailable")
    result = {"track": dict(zip(TRACK_COLUMNS, tuple(track), strict=True))}
    for table in TRACK_TABLES:
        cursor = conn.execute(f"SELECT * FROM {table} WHERE track_id=? ORDER BY rowid", (int(track_id),))
        names = [column[0] for column in cursor.description]
        result[table] = [dict(zip(names, tuple(row), strict=True)) for row in cursor]
    for table, ids in (
        ("artists", {row["artist_id"] for row in result["track_artist_credits"]}),
        ("canonical_albums", {row["canonical_album_id"] for row in result["track_album_memberships"]}),
    ):
        result[table] = []
        for entity_id in sorted(ids):
            cursor = conn.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,))
            names = [column[0] for column in cursor.description]
            row = cursor.fetchone()
            if row is not None:
                result[table].append(dict(zip(names, tuple(row), strict=True)))
    return result


@dataclass
class MaterializationTransaction:
    identifier: str
    before: dict
    already_applied: bool = False
    changed: bool = False


@dataclass(frozen=True)
class MetadataWriteIntent:
    """Explicit user write authority, distinct from automatic resolver policy."""

    track_id: int
    expected_fingerprint: str
    evidence: tuple
    proposal_key: str
    actor: str = "user"
    reason: str = "manual_metadata_edit"


def user_write_intent(track_id: int, fingerprint: str, *, mode: str, selection: dict, evidence: tuple = (), undo_revision: tuple = ()) -> MetadataWriteIntent:
    if mode not in {"manual", "confirmed"}:
        raise ValueError("Invalid explicit metadata write mode.")
    payload = {"version": 1, "track_id": int(track_id), "fingerprint": fingerprint,
               "mode": mode, "selection": selection, "evidence": [item.evidence_key for item in evidence],
               "undo_revision": undo_revision}
    key = "metadata-user-intent-v1:" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    return MetadataWriteIntent(int(track_id), fingerprint, evidence, key,
                               reason="musicbrainz_confirmed" if mode == "confirmed" else "manual_metadata_edit")


def _shared_album_states(conn: sqlite3.Connection, track_id: int) -> dict:
    """Catalogue rows owned by another track are outside a track edit's scope.

    Include possible destination albums, not just the edited track's current
    graph: selecting an existing family may otherwise rename its shared card.
    This read occurs once inside the user edit's writer transaction, never in
    browser rendering or playback updates.
    """
    cursor = conn.execute(
        "SELECT album.* FROM canonical_albums album WHERE EXISTS ("
        "SELECT 1 FROM track_album_memberships member "
        "WHERE member.canonical_album_id=album.id AND member.track_id<>?) ORDER BY album.id",
        (int(track_id),),
    )
    names = [column[0] for column in cursor.description]
    return {row[0]: dict(zip(names, tuple(row), strict=True)) for row in cursor}


@contextmanager
def _writer(conn: sqlite3.Connection):
    nested = conn.in_transaction
    name = "metadata_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
        else:
            conn.rollback()
        raise
    else:
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {name}")
        else:
            conn.commit()


@contextmanager
def materialize_proposal(conn: sqlite3.Connection, proposal):
    """Guard one proposal's complete field/identity/credit/album transaction.

    Callers skip their write callback when ``already_applied`` is true. A retry
    of the same proposal is a genuine no-op, including evidence/history dates.
    Caller-owned outer transactions remain caller-owned.
    """
    from .service import MetadataService

    with _writer(conn):
        before = capture_metadata_state(conn, proposal.track_id)
        fingerprint = state_fingerprint(before)
        existing = conn.execute(
            "SELECT id,after_json,undone_at FROM metadata_materializations WHERE proposal_key=?",
            (proposal.proposal_key,),
        ).fetchone()
        if existing is not None:
            if existing[2] is not None or _json(before) != existing[1]:
                raise StaleMetadataProposal("metadata_proposal_already_changed")
            yield MaterializationTransaction(str(existing[0]), before, already_applied=True)
            return
        if fingerprint != proposal.expected_fingerprint:
            raise StaleMetadataProposal("metadata_changed_since_analysis")
        transaction = MaterializationTransaction(str(uuid.uuid4()), before)
        shared_albums = _shared_album_states(conn, proposal.track_id) if isinstance(proposal, MetadataWriteIntent) else None
        user_noop_savepoint = "user_metadata_" + uuid.uuid4().hex if isinstance(proposal, MetadataWriteIntent) else None
        if user_noop_savepoint:
            conn.execute(f"SAVEPOINT {user_noop_savepoint}")
        for evidence in proposal.evidence:
            conn.execute(
                "INSERT OR IGNORE INTO metadata_evidence_bundles "
                "(track_id,evidence_key,provider,schema_version,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (proposal.track_id, evidence.evidence_key, evidence.provider,
                 evidence.schema_version, _json(evidence.to_dict()), utc_now()),
            )
        with MetadataService(conn).defer_reconciliation(proposal.track_id, transaction.identifier):
            yield transaction
        if shared_albums is not None and _shared_album_states(conn, proposal.track_id) != shared_albums:
            # A title/date change on a shared card would affect other tracks
            # and make immediate conflict-aware Undo impossible. Refuse the
            # whole edit, including observations and membership rebindings.
            raise ValueError("Shared album facts require a separate catalogue-wide edit; this track's edit was not applied.")
        after = capture_metadata_state(conn, proposal.track_id)
        transaction.changed = before != after
        if user_noop_savepoint:
            if not transaction.changed:
                # Equivalent explicit user saves are true no-ops, including
                # observation freshness, evidence and journal creation dates.
                conn.execute(f"ROLLBACK TO SAVEPOINT {user_noop_savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {user_noop_savepoint}")
            if not transaction.changed:
                return
        # Identity-only acceptance must appear in the existing history/Undo UI
        # too. Recording an audit entry does not rewrite scalar field state.
        metadata = MetadataService(conn)
        audited_fields = {row[0] for row in conn.execute(
            "SELECT field_name FROM track_metadata_history WHERE track_id=? AND change_group_id=?",
            (proposal.track_id, transaction.identifier),
        )}
        before_fields = {row["field_name"]: row for row in before["track_metadata_fields"]}
        after_fields = {row["field_name"]: row for row in after["track_metadata_fields"]}
        for name in structural_change_fields(before, after) - audited_fields:
            if name in before_fields and name in after_fields:
                metadata._write_history(
                    track_id=proposal.track_id, group_id=transaction.identifier,
                    old=metadata._state_from_row(before_fields[name]),
                    new=metadata._state_from_row(after_fields[name]),
                    actor=getattr(proposal, "actor", "metadata_intelligence"),
                    reason=getattr(proposal, "reason", "structured_metadata_identity"),
                    changed_at=utc_now(),
                )
        conn.execute(
            "INSERT INTO metadata_materializations "
            "(id,track_id,proposal_key,expected_fingerprint,before_json,after_json,evidence_keys_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (transaction.identifier, proposal.track_id, proposal.proposal_key,
             fingerprint, _json(before), _json(after),
             _json([e.evidence_key for e in proposal.evidence]), utc_now()),
        )


def _restore_rows(conn, table: str, track_id: int, rows: list[dict]) -> None:
    if table not in TRACK_TABLES:
        raise ValueError("metadata_restore_table_rejected")
    conn.execute(f"DELETE FROM {table} WHERE track_id=?", (int(track_id),))
    for row in rows:
        columns = tuple(row)
        conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(row[name] for name in columns),
        )


def undo_materialization(conn: sqlite3.Connection, identifier: str) -> bool:
    """Restore a whole accepted metadata state only if it is still current.

    Shared artist/album entities are not renamed or deleted during reversal.
    An exclusively referenced entity can be restored when its entire current
    row still matches this acceptance's after-state. Shared or later changes
    are refused rather than overwriting another track's correction.
    """
    with _writer(conn):
        row = conn.execute(
            "SELECT track_id,before_json,after_json,undone_at FROM metadata_materializations WHERE id=?",
            (str(identifier),),
        ).fetchone()
        if row is None or row[3] is not None:
            return False
        track_id = int(row[0])
        before, after = json.loads(row[1]), json.loads(row[2])
        if capture_metadata_state(conn, track_id) != after:
            raise StaleMetadataProposal("metadata_changed_since_materialization")
        for table in ("artists", "canonical_albums"):
            accepted_entities = {entity["id"]: entity for entity in after[table]}
            for entity in before[table]:
                cursor = conn.execute(f"SELECT * FROM {table} WHERE id=?", (entity["id"],))
                columns = [column[0] for column in cursor.description]
                current = cursor.fetchone()
                current_entity = dict(zip(columns, tuple(current), strict=True)) if current is not None else None
                if current_entity == entity:
                    continue
                if current_entity is None or current_entity != accepted_entities.get(entity["id"]):
                    raise StaleMetadataProposal("metadata_shared_identity_changed")
                reference_table, reference_column = (
                    ("track_artist_credits", "artist_id") if table == "artists"
                    else ("track_album_memberships", "canonical_album_id")
                )
                if conn.execute(
                    f"SELECT 1 FROM {reference_table} WHERE {reference_column}=? AND track_id<>? LIMIT 1",
                    (entity["id"], track_id),
                ).fetchone() is not None:
                    raise StaleMetadataProposal("metadata_shared_identity_changed")
                if table == "artists" and conn.execute(
                    "SELECT 1 FROM artist_relationships WHERE subject_artist_id=? OR related_artist_id=? LIMIT 1",
                    (entity["id"], entity["id"]),
                ).fetchone() is not None:
                    raise StaleMetadataProposal("metadata_shared_identity_changed")
                names = [name for name in columns if name != "id"]
                conn.execute(
                    f"UPDATE {table} SET {','.join(name + '=?' for name in names)} WHERE id=?",
                    (*[entity[name] for name in names], entity["id"]),
                )
        assignments = ",".join(f"{name}=?" for name in TRACK_COLUMNS)
        conn.execute(
            f"UPDATE tracks SET {assignments} WHERE id=?",
            (*[before["track"][name] for name in TRACK_COLUMNS], track_id),
        )
        for table in TRACK_TABLES:
            _restore_rows(conn, table, track_id, before[table])
        conn.execute("UPDATE metadata_materializations SET undone_at=? WHERE id=?", (utc_now(), identifier))
        return True
