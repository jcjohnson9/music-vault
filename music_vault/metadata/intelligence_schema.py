from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from music_vault.core.safety import sanitize_error_text

from .schema import EDITABLE_METADATA_FIELDS


ARTISTS_TABLE = "artists"
TRACK_ARTIST_CREDITS_TABLE = "track_artist_credits"
TRACK_RELEASE_CONTEXT_TABLE = "track_release_context"
INTELLIGENCE_JOBS_TABLE = "metadata_intelligence_jobs"
INTELLIGENCE_ITEMS_TABLE = "metadata_intelligence_items"

ARTIST_ENTITY_TYPES = (
    "person",
    "group",
    "band",
    "duo",
    "orchestra",
    "fictional",
    "collective",
    "unknown",
)
ARTIST_CREDIT_ROLES = ("primary", "featured", "collaborator", "remixer", "performer")
JOB_KINDS = ("new_import", "existing_library")
JOB_STATUSES = (
    "created",
    "analyzing",
    "paused",
    "ready",
    "applying",
    "complete",
    "complete_with_issues",
    "failed",
    "cancelled",
)
ITEM_STATES = (
    "queued",
    "analyzing",
    "review",
    "ready",
    "applied",
    "no_match",
    "skipped",
    "failed",
    "cancelled",
)
PROVIDER_AGREEMENTS = (
    "unknown",
    "agreed",
    "conflict",
    "discogs_only",
    "musicbrainz_only",
    "none",
)

_AUTOMATIC_JOB_ID = "automatic-new-imports"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _has_unique_normalized_artist_name(conn: sqlite3.Connection) -> bool:
    """Detect the early schema-v6 artist-name uniqueness constraint.

    Provider identities, not display-name spelling, are authoritative.  An
    early prerelease v6 schema made ``normalized_name`` unique, which could
    silently collapse two provider-backed artists with the same public name.
    """

    for row in conn.execute("PRAGMA index_list('artists')").fetchall():
        if not bool(row[2]):
            continue
        index_name = str(row[1]).replace('"', '""')
        columns = [
            str(column[2])
            for column in conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        ]
        if columns == ["normalized_name"]:
            return True
    return False


def _remove_legacy_artist_name_uniqueness(
    conn: sqlite3.Connection,
    entity_types: str,
    credit_roles: str,
) -> None:
    """Upgrade an early v6 artist graph without changing IDs or credits."""

    if not _has_unique_normalized_artist_name(conn):
        return

    # Rebuild the parent and its only child together.  Deferring foreign-key
    # checks keeps this safe inside MusicVaultDB's existing migration
    # transaction, and the final integrity gate verifies the reconstructed
    # relationship before commit.
    conn.execute("PRAGMA defer_foreign_keys=ON")
    conn.execute(
        f"""
        CREATE TABLE artists_identity_upgrade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL CHECK (TRIM(display_name) != ''),
            normalized_name TEXT NOT NULL CHECK (TRIM(normalized_name) != ''),
            sort_name TEXT NOT NULL CHECK (TRIM(sort_name) != ''),
            entity_type TEXT NOT NULL DEFAULT 'unknown'
                CHECK (entity_type IN ({entity_types})),
            discogs_artist_id TEXT,
            musicbrainz_artist_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE track_artist_credits_identity_upgrade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            artist_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ({credit_roles})),
            credit_order INTEGER NOT NULL CHECK (credit_order >= 0),
            join_phrase TEXT NOT NULL DEFAULT '',
            provenance TEXT NOT NULL,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            is_manual INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0, 1)),
            is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (track_id, artist_id, role),
            UNIQUE (track_id, credit_order),
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
            FOREIGN KEY (artist_id) REFERENCES artists_identity_upgrade(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO artists_identity_upgrade (
            id, display_name, normalized_name, sort_name, entity_type,
            discogs_artist_id, musicbrainz_artist_id, created_at, updated_at
        )
        SELECT id, display_name, normalized_name, sort_name, entity_type,
               discogs_artist_id, musicbrainz_artist_id, created_at, updated_at
        FROM artists
        """
    )
    conn.execute(
        """
        INSERT INTO track_artist_credits_identity_upgrade (
            id, track_id, artist_id, role, credit_order, join_phrase,
            provenance, provider_reference, confidence, is_manual, is_locked,
            created_at, updated_at
        )
        SELECT id, track_id, artist_id, role, credit_order, join_phrase,
               provenance, provider_reference, confidence, is_manual, is_locked,
               created_at, updated_at
        FROM track_artist_credits
        """
    )
    conn.execute("DROP TABLE track_artist_credits")
    conn.execute("DROP TABLE artists")
    conn.execute("ALTER TABLE artists_identity_upgrade RENAME TO artists")
    conn.execute(
        "ALTER TABLE track_artist_credits_identity_upgrade "
        "RENAME TO track_artist_credits"
    )


def create_metadata_intelligence_schema(conn: sqlite3.Connection) -> None:
    entity_types = ", ".join(f"'{value}'" for value in ARTIST_ENTITY_TYPES)
    credit_roles = ", ".join(f"'{value}'" for value in ARTIST_CREDIT_ROLES)
    job_kinds = ", ".join(f"'{value}'" for value in JOB_KINDS)
    job_statuses = ", ".join(f"'{value}'" for value in JOB_STATUSES)
    item_states = ", ".join(f"'{value}'" for value in ITEM_STATES)
    agreements = ", ".join(f"'{value}'" for value in PROVIDER_AGREEMENTS)

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ARTISTS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL CHECK (TRIM(display_name) != ''),
            normalized_name TEXT NOT NULL CHECK (TRIM(normalized_name) != ''),
            sort_name TEXT NOT NULL CHECK (TRIM(sort_name) != ''),
            entity_type TEXT NOT NULL DEFAULT 'unknown'
                CHECK (entity_type IN ({entity_types})),
            discogs_artist_id TEXT,
            musicbrainz_artist_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRACK_ARTIST_CREDITS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            artist_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ({credit_roles})),
            credit_order INTEGER NOT NULL CHECK (credit_order >= 0),
            join_phrase TEXT NOT NULL DEFAULT '',
            provenance TEXT NOT NULL,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            is_manual INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0, 1)),
            is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (track_id, artist_id, role),
            UNIQUE (track_id, credit_order),
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
            FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE RESTRICT
        )
        """
    )
    _remove_legacy_artist_name_uniqueness(conn, entity_types, credit_roles)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRACK_RELEASE_CONTEXT_TABLE} (
            track_id INTEGER PRIMARY KEY,
            discogs_release_id TEXT,
            discogs_master_id TEXT,
            release_title TEXT,
            release_country TEXT,
            release_format TEXT,
            catalog_number TEXT,
            label_name TEXT,
            release_date TEXT,
            original_release_date TEXT,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            updated_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {INTELLIGENCE_JOBS_TABLE} (
            id TEXT PRIMARY KEY,
            job_kind TEXT NOT NULL CHECK (job_kind IN ({job_kinds})),
            status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ({job_statuses})),
            provider_policy TEXT NOT NULL DEFAULT 'discogs_first',
            total_items INTEGER NOT NULL DEFAULT 0 CHECK (total_items >= 0),
            analyzed_items INTEGER NOT NULL DEFAULT 0 CHECK (analyzed_items >= 0),
            review_items INTEGER NOT NULL DEFAULT 0 CHECK (review_items >= 0),
            applied_items INTEGER NOT NULL DEFAULT 0 CHECK (applied_items >= 0),
            no_match_items INTEGER NOT NULL DEFAULT 0 CHECK (no_match_items >= 0),
            failed_items INTEGER NOT NULL DEFAULT 0 CHECK (failed_items >= 0),
            skipped_items INTEGER NOT NULL DEFAULT 0 CHECK (skipped_items >= 0),
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
            last_error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {INTELLIGENCE_ITEMS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ({item_states})),
            reason TEXT NOT NULL DEFAULT 'unspecified',
            priority INTEGER NOT NULL DEFAULT 0,
            parsed_hints TEXT NOT NULL DEFAULT '{{}}',
            discogs_release_id TEXT,
            discogs_master_id TEXT,
            musicbrainz_recording_id TEXT,
            musicbrainz_release_id TEXT,
            field_proposal TEXT NOT NULL DEFAULT '{{}}',
            field_confidence TEXT NOT NULL DEFAULT '{{}}',
            provider_agreement TEXT NOT NULL DEFAULT 'unknown'
                CHECK (provider_agreement IN ({agreements})),
            review_reason TEXT,
            applied_history_group TEXT,
            file_write_result TEXT,
            artwork_result TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE (job_id, track_id),
            FOREIGN KEY (job_id) REFERENCES metadata_intelligence_jobs(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_artists_normalized_name ON artists(normalized_name, id)",
        "CREATE INDEX IF NOT EXISTS idx_artists_sort_name ON artists(sort_name, id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_artists_discogs_id ON artists(discogs_artist_id) WHERE discogs_artist_id IS NOT NULL AND TRIM(discogs_artist_id) != ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_artists_musicbrainz_id ON artists(musicbrainz_artist_id) WHERE musicbrainz_artist_id IS NOT NULL AND TRIM(musicbrainz_artist_id) != ''",
        "CREATE INDEX IF NOT EXISTS idx_artist_credits_artist_role_track ON track_artist_credits(artist_id, role, track_id)",
        "CREATE INDEX IF NOT EXISTS idx_artist_credits_track_order ON track_artist_credits(track_id, credit_order)",
        "CREATE INDEX IF NOT EXISTS idx_release_context_discogs_release ON track_release_context(discogs_release_id, track_id)",
        "CREATE INDEX IF NOT EXISTS idx_release_context_discogs_master ON track_release_context(discogs_master_id, track_id)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_discogs_release_id ON tracks(discogs_release_id)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_discogs_master_id ON tracks(discogs_master_id)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_recording_group_key ON tracks(recording_group_key)",
        "CREATE INDEX IF NOT EXISTS idx_intelligence_jobs_status_updated ON metadata_intelligence_jobs(status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_intelligence_items_claim ON metadata_intelligence_items(state, priority DESC, id)",
        "CREATE INDEX IF NOT EXISTS idx_intelligence_items_job_state ON metadata_intelligence_items(job_id, state, id)",
        "CREATE INDEX IF NOT EXISTS idx_intelligence_items_track ON metadata_intelligence_items(track_id, id DESC)",
    ):
        conn.execute(statement)


def seed_existing_metadata_field_extensions(conn: sqlite3.Connection) -> None:
    """Add the three v6 field-state rows without rewriting existing state."""

    timestamp = _utc_now()
    for field_name, column in (
        ("original_release_date", "original_release_date"),
        ("version_type", "version_type"),
        ("version_label", "version_label"),
    ):
        conn.execute(
            f"""
            INSERT OR IGNORE INTO track_metadata_fields (
                track_id, field_name, value, provenance, provider_reference,
                confidence, is_manual, is_locked, updated_at
            )
            SELECT id, ?, {column},
                   CASE WHEN {column} IS NULL OR TRIM({column}) = ''
                        THEN 'unknown' ELSE 'embedded' END,
                   NULL, NULL, 0, 0,
                   COALESCE(metadata_updated_at, updated_at, ?)
            FROM tracks
            """,
            (field_name, timestamp),
        )


def required_intelligence_indexes() -> Iterable[str]:
    return (
        "idx_artists_normalized_name",
        "idx_artists_sort_name",
        "idx_artists_discogs_id",
        "idx_artists_musicbrainz_id",
        "idx_artist_credits_artist_role_track",
        "idx_artist_credits_track_order",
        "idx_release_context_discogs_release",
        "idx_release_context_discogs_master",
        "idx_tracks_discogs_release_id",
        "idx_tracks_discogs_master_id",
        "idx_tracks_recording_group_key",
        "idx_intelligence_jobs_status_updated",
        "idx_intelligence_items_claim",
        "idx_intelligence_items_job_state",
        "idx_intelligence_items_track",
    )


@dataclass(frozen=True)
class IntelligenceItem:
    id: int
    job_id: str
    track_id: int
    state: str
    reason: str
    priority: int
    attempt_count: int


@dataclass(frozen=True)
class IntelligenceJobSummary:
    id: str
    job_kind: str
    status: str
    total_items: int
    analyzed_items: int
    review_items: int
    applied_items: int
    no_match_items: int
    failed_items: int
    skipped_items: int


class MetadataIntelligenceJobStore:
    """Small persistence boundary for resumable, provider-independent work."""

    def __init__(self, database: Any) -> None:
        self.conn: sqlite3.Connection = getattr(database, "conn", database)
        if not isinstance(self.conn, sqlite3.Connection):
            raise TypeError("MetadataIntelligenceJobStore requires a SQLite connection.")
        self.conn.row_factory = sqlite3.Row

    @contextmanager
    def _transaction(self):
        if not self.conn.in_transaction:
            with self.conn:
                yield
            return
        name = f"intelligence_{uuid.uuid4().hex}"
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
    def _item(row: sqlite3.Row) -> IntelligenceItem:
        return IntelligenceItem(
            id=int(row["id"]),
            job_id=str(row["job_id"]),
            track_id=int(row["track_id"]),
            state=str(row["state"]),
            reason=str(row["reason"]),
            priority=int(row["priority"]),
            attempt_count=int(row["attempt_count"]),
        )

    def _ensure_track(self, track_id: int) -> None:
        if self.conn.execute("SELECT 1 FROM tracks WHERE id=?", (int(track_id),)).fetchone() is None:
            raise KeyError(f"Track {track_id} does not exist.")

    def enqueue_track(
        self,
        track_id: int,
        reason: str = "new_import",
        priority: int = 0,
    ) -> IntelligenceItem:
        self._ensure_track(track_id)
        now = _utc_now()
        with self._transaction():
            self.conn.execute(
                f"""
                INSERT INTO {INTELLIGENCE_JOBS_TABLE} (
                    id, job_kind, status, created_at, updated_at
                ) VALUES (?, 'new_import', 'created', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=CASE
                        WHEN metadata_intelligence_jobs.status IN ('complete','complete_with_issues','cancelled')
                        THEN 'created' ELSE metadata_intelligence_jobs.status END,
                    cancel_requested=0,
                    completed_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (_AUTOMATIC_JOB_ID, now, now),
            )
            self.conn.execute(
                f"""
                INSERT INTO {INTELLIGENCE_ITEMS_TABLE} (
                    job_id, track_id, state, reason, priority, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
                ON CONFLICT(job_id, track_id) DO UPDATE SET
                    state=CASE
                        WHEN metadata_intelligence_items.state IN ('failed','cancelled')
                        THEN 'queued' ELSE metadata_intelligence_items.state END,
                    reason=excluded.reason,
                    priority=MAX(metadata_intelligence_items.priority, excluded.priority),
                    completed_at=CASE
                        WHEN metadata_intelligence_items.state IN ('failed','cancelled')
                        THEN NULL ELSE metadata_intelligence_items.completed_at END,
                    updated_at=excluded.updated_at
                """,
                (
                    _AUTOMATIC_JOB_ID,
                    int(track_id),
                    sanitize_error_text(reason, max_length=200),
                    int(priority),
                    now,
                    now,
                ),
            )
            self._refresh_job(_AUTOMATIC_JOB_ID, preserve_active_status=True)
            row = self.conn.execute(
                f"SELECT * FROM {INTELLIGENCE_ITEMS_TABLE} WHERE job_id=? AND track_id=?",
                (_AUTOMATIC_JOB_ID, int(track_id)),
            ).fetchone()
        assert row is not None
        return self._item(row)

    def _canonical_library_track_ids(self) -> list[int]:
        has_identity = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_track_identities'"
        ).fetchone() is not None
        if not has_identity:
            return [int(row[0]) for row in self.conn.execute("SELECT id FROM tracks ORDER BY id")]
        rows = self.conn.execute(
            """
            SELECT t.id
            FROM tracks t
            WHERE NULLIF(TRIM(t.source_video_id), '') IS NULL
               OR EXISTS (
                    SELECT 1 FROM source_track_identities identity
                    WHERE identity.source_kind='youtube'
                      AND identity.external_track_id=t.source_video_id
                      AND identity.track_id=t.id
               )
               OR NOT EXISTS (
                    SELECT 1 FROM source_track_identities identity
                    WHERE identity.source_kind='youtube'
                      AND identity.external_track_id=t.source_video_id
               )
            ORDER BY t.id
            """
        ).fetchall()
        return [int(row[0]) for row in rows]

    def create_existing_library_job(
        self,
        track_ids: Sequence[int] | None = None,
    ) -> str:
        source_ids = (
            self._canonical_library_track_ids()
            if track_ids is None
            else track_ids
        )
        ids = list(dict.fromkeys(int(value) for value in source_ids))
        for track_id in ids:
            self._ensure_track(track_id)
        job_id = str(uuid.uuid4())
        now = _utc_now()
        with self._transaction():
            self.conn.execute(
                f"""
                INSERT INTO {INTELLIGENCE_JOBS_TABLE} (
                    id, job_kind, status, total_items, created_at, updated_at
                ) VALUES (?, 'existing_library', 'created', ?, ?, ?)
                """,
                (job_id, len(ids), now, now),
            )
            self.conn.executemany(
                f"""
                INSERT INTO {INTELLIGENCE_ITEMS_TABLE} (
                    job_id, track_id, state, reason, priority, created_at, updated_at
                ) VALUES (?, ?, 'queued', 'existing_library', 0, ?, ?)
                """,
                ((job_id, track_id, now, now) for track_id in ids),
            )
            self._refresh_job(job_id, preserve_active_status=True)
        return job_id

    def claim_next_item(self, job_id: str | None = None) -> IntelligenceItem | None:
        now = _utc_now()
        parameters: list[object] = []
        where = "item.state='queued' AND job.status IN ('created','analyzing','applying') AND job.cancel_requested=0"
        if job_id is not None:
            where += " AND item.job_id=?"
            parameters.append(str(job_id))
        with self._transaction():
            row = self.conn.execute(
                f"""
                SELECT item.* FROM {INTELLIGENCE_ITEMS_TABLE} item
                JOIN {INTELLIGENCE_JOBS_TABLE} job ON job.id=item.job_id
                WHERE {where}
                ORDER BY item.priority DESC, item.id
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            changed = self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_ITEMS_TABLE}
                SET state='analyzing', attempt_count=attempt_count+1,
                    started_at=?, completed_at=NULL, last_error=NULL, updated_at=?
                WHERE id=? AND state='queued'
                """,
                (now, now, int(row["id"])),
            ).rowcount
            if changed != 1:
                return None
            self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_JOBS_TABLE}
                SET status='analyzing', started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=?
                """,
                (now, now, str(row["job_id"])),
            )
            claimed = self.conn.execute(
                f"SELECT * FROM {INTELLIGENCE_ITEMS_TABLE} WHERE id=?",
                (int(row["id"]),),
            ).fetchone()
        return self._item(claimed) if claimed is not None else None

    @staticmethod
    def _json_summary(value: Mapping[str, object] | None) -> str:
        if value is None:
            return "{}"
        if not isinstance(value, Mapping):
            raise TypeError("Persisted intelligence summaries must be mappings.")
        forbidden = {
            "raw_response",
            "raw_responses",
            "response_body",
            "response_headers",
            "token",
            "access_token",
            "api_key",
            "authorization",
        }

        def reject_private_payload(candidate: object) -> None:
            if isinstance(candidate, Mapping):
                for key, nested in candidate.items():
                    if str(key).strip().casefold() in forbidden:
                        raise ValueError(
                            "Raw provider responses and credentials cannot be persisted."
                        )
                    reject_private_payload(nested)
            elif isinstance(candidate, Sequence) and not isinstance(
                candidate, (str, bytes, bytearray)
            ):
                for nested in candidate:
                    reject_private_payload(nested)

        reject_private_payload(value)
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 64 * 1024:
            raise ValueError("The normalized intelligence summary is too large.")
        return payload

    def mark_item(
        self,
        item_id: int,
        state: str,
        *,
        parsed_hints: Mapping[str, object] | None = None,
        discogs_release_id: object = None,
        discogs_master_id: object = None,
        musicbrainz_recording_id: object = None,
        musicbrainz_release_id: object = None,
        field_proposal: Mapping[str, object] | None = None,
        field_confidence: Mapping[str, object] | None = None,
        provider_agreement: str = "unknown",
        review_reason: object = None,
        applied_history_group: object = None,
        file_write_result: object = None,
        artwork_result: object = None,
        error: object = None,
    ) -> IntelligenceItem:
        normalized_state = str(state).strip().casefold()
        if normalized_state not in ITEM_STATES or normalized_state in {"queued", "analyzing"}:
            raise ValueError(f"Unsupported completed item state: {state}")
        agreement = str(provider_agreement or "unknown").strip().casefold()
        if agreement not in PROVIDER_AGREEMENTS:
            raise ValueError(f"Unsupported provider agreement: {provider_agreement}")
        row = self.conn.execute(
            f"SELECT * FROM {INTELLIGENCE_ITEMS_TABLE} WHERE id=?",
            (int(item_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"Intelligence item {item_id} does not exist.")
        now = _utc_now()
        clean = lambda value, length=1000: (
            sanitize_error_text(value, max_length=length) if value not in (None, "") else None
        )
        with self._transaction():
            self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_ITEMS_TABLE}
                SET state=?, parsed_hints=?, discogs_release_id=?, discogs_master_id=?,
                    musicbrainz_recording_id=?, musicbrainz_release_id=?,
                    field_proposal=?, field_confidence=?, provider_agreement=?,
                    review_reason=?, applied_history_group=?, file_write_result=?,
                    artwork_result=?, last_error=?, completed_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    normalized_state,
                    self._json_summary(parsed_hints),
                    clean(discogs_release_id, 100),
                    clean(discogs_master_id, 100),
                    clean(musicbrainz_recording_id, 100),
                    clean(musicbrainz_release_id, 100),
                    self._json_summary(field_proposal),
                    self._json_summary(field_confidence),
                    agreement,
                    clean(review_reason),
                    clean(applied_history_group, 100),
                    clean(file_write_result),
                    clean(artwork_result),
                    clean(error),
                    now,
                    now,
                    int(item_id),
                ),
            )
            self._refresh_job(str(row["job_id"]))
            updated = self.conn.execute(
                f"SELECT * FROM {INTELLIGENCE_ITEMS_TABLE} WHERE id=?",
                (int(item_id),),
            ).fetchone()
        assert updated is not None
        return self._item(updated)

    def _refresh_job(self, job_id: str, *, preserve_active_status: bool = False) -> None:
        counts = {
            str(row["state"]): int(row["count"])
            for row in self.conn.execute(
                f"SELECT state, COUNT(*) AS count FROM {INTELLIGENCE_ITEMS_TABLE} WHERE job_id=? GROUP BY state",
                (job_id,),
            )
        }
        total = sum(counts.values())
        analyzed = total - counts.get("queued", 0) - counts.get("analyzing", 0)
        job = self.conn.execute(
            f"SELECT status,cancel_requested FROM {INTELLIGENCE_JOBS_TABLE} WHERE id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            return
        status = str(job["status"])
        completed_at: str | None = None
        if bool(job["cancel_requested"]) or status == "cancelled":
            status = "cancelled"
        elif status == "paused" and (
            counts.get("queued", 0) or counts.get("analyzing", 0)
        ):
            # Finishing an already-claimed item must not silently unpause the
            # persisted remainder of a user-paused scan.
            status = "paused"
        elif counts.get("analyzing", 0):
            status = "analyzing"
        elif counts.get("queued", 0):
            if not preserve_active_status or status not in {"paused", "applying"}:
                status = "created" if analyzed == 0 else "analyzing"
        elif counts.get("review", 0) or counts.get("ready", 0):
            status = "ready"
        elif counts.get("failed", 0) or counts.get("no_match", 0):
            status = "complete_with_issues"
            completed_at = _utc_now()
        else:
            status = "complete"
            completed_at = _utc_now()
        now = _utc_now()
        self.conn.execute(
            f"""
            UPDATE {INTELLIGENCE_JOBS_TABLE}
            SET status=?, total_items=?, analyzed_items=?, review_items=?,
                applied_items=?, no_match_items=?, failed_items=?, skipped_items=?,
                completed_at=?, updated_at=?
            WHERE id=?
            """,
            (
                status,
                total,
                analyzed,
                counts.get("review", 0) + counts.get("ready", 0),
                counts.get("applied", 0),
                counts.get("no_match", 0),
                counts.get("failed", 0),
                counts.get("skipped", 0),
                completed_at,
                now,
                job_id,
            ),
        )

    def pause(self, job_id: str) -> None:
        now = _utc_now()
        with self._transaction():
            changed = self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_JOBS_TABLE}
                SET status='paused', updated_at=?
                WHERE id=? AND status IN ('created','analyzing','ready','applying')
                  AND cancel_requested=0
                """,
                (now, str(job_id)),
            ).rowcount
            if changed != 1:
                raise ValueError("Only an active metadata-intelligence job can pause.")

    def resume(self, job_id: str) -> None:
        with self._transaction():
            changed = self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_JOBS_TABLE}
                SET status='analyzing', cancel_requested=0, completed_at=NULL, updated_at=?
                WHERE id=? AND status='paused'
                """,
                (_utc_now(), str(job_id)),
            ).rowcount
            if changed != 1:
                raise ValueError("Only a paused metadata-intelligence job can resume.")

    def recover_interrupted(self, job_id: str) -> int:
        """Requeue items left in-flight by an interrupted process.

        Recovery is explicit so an active worker is never duplicated merely by
        opening a dashboard.  Attempt counts remain intact and will increment
        when each recovered item is claimed again.
        """

        now = _utc_now()
        with self._transaction():
            job = self.conn.execute(
                f"SELECT status,cancel_requested FROM {INTELLIGENCE_JOBS_TABLE} WHERE id=?",
                (str(job_id),),
            ).fetchone()
            if job is None:
                raise KeyError(f"Metadata-intelligence job {job_id} does not exist.")
            if bool(job["cancel_requested"]) or str(job["status"]) == "cancelled":
                raise ValueError("A cancelled metadata-intelligence job cannot recover.")
            recovered = self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_ITEMS_TABLE}
                SET state='queued', started_at=NULL, completed_at=NULL,
                    last_error=NULL, updated_at=?
                WHERE job_id=? AND state='analyzing'
                """,
                (now, str(job_id)),
            ).rowcount
            if recovered:
                self.conn.execute(
                    f"""
                    UPDATE {INTELLIGENCE_JOBS_TABLE}
                    SET status='analyzing', completed_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (now, str(job_id)),
                )
            self._refresh_job(str(job_id), preserve_active_status=True)
        return int(recovered)

    def cancel(self, job_id: str) -> None:
        now = _utc_now()
        with self._transaction():
            changed = self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_JOBS_TABLE}
                SET status='cancelled', cancel_requested=1, completed_at=?, updated_at=?
                WHERE id=? AND status NOT IN ('complete','complete_with_issues','cancelled')
                """,
                (now, now, str(job_id)),
            ).rowcount
            if changed != 1:
                raise ValueError("The metadata-intelligence job cannot be cancelled.")
            self.conn.execute(
                f"""
                UPDATE {INTELLIGENCE_ITEMS_TABLE}
                SET state='cancelled', completed_at=?, updated_at=?
                WHERE job_id=? AND state='queued'
                """,
                (now, now, str(job_id)),
            )
            self._refresh_job(str(job_id))

    def _set_job_status(self, job_id: str, status: str) -> None:
        if status not in JOB_STATUSES:
            raise ValueError(f"Unsupported job status: {status}")
        with self._transaction():
            changed = self.conn.execute(
                f"UPDATE {INTELLIGENCE_JOBS_TABLE} SET status=?, updated_at=? WHERE id=?",
                (status, _utc_now(), str(job_id)),
            ).rowcount
            if changed != 1:
                raise KeyError(f"Metadata-intelligence job {job_id} does not exist.")

    def aggregate_counts(self, job_id: str | None = None) -> dict[str, int]:
        parameters: tuple[object, ...] = () if job_id is None else (str(job_id),)
        where = "" if job_id is None else "WHERE job_id=?"
        counts = {state: 0 for state in ITEM_STATES}
        for row in self.conn.execute(
            f"SELECT state, COUNT(*) AS count FROM {INTELLIGENCE_ITEMS_TABLE} {where} GROUP BY state",
            parameters,
        ):
            counts[str(row["state"])] = int(row["count"])
        counts["total"] = sum(counts[state] for state in ITEM_STATES)
        return counts

    def job_summary(self, job_id: str) -> IntelligenceJobSummary:
        row = self.conn.execute(
            f"SELECT * FROM {INTELLIGENCE_JOBS_TABLE} WHERE id=?",
            (str(job_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"Metadata-intelligence job {job_id} does not exist.")
        return IntelligenceJobSummary(
            id=str(row["id"]),
            job_kind=str(row["job_kind"]),
            status=str(row["status"]),
            total_items=int(row["total_items"]),
            analyzed_items=int(row["analyzed_items"]),
            review_items=int(row["review_items"]),
            applied_items=int(row["applied_items"]),
            no_match_items=int(row["no_match_items"]),
            failed_items=int(row["failed_items"]),
            skipped_items=int(row["skipped_items"]),
        )


__all__ = [
    "ARTISTS_TABLE",
    "TRACK_ARTIST_CREDITS_TABLE",
    "TRACK_RELEASE_CONTEXT_TABLE",
    "INTELLIGENCE_JOBS_TABLE",
    "INTELLIGENCE_ITEMS_TABLE",
    "ARTIST_ENTITY_TYPES",
    "ARTIST_CREDIT_ROLES",
    "JOB_STATUSES",
    "ITEM_STATES",
    "IntelligenceItem",
    "IntelligenceJobSummary",
    "MetadataIntelligenceJobStore",
    "create_metadata_intelligence_schema",
    "required_intelligence_indexes",
    "seed_existing_metadata_field_extensions",
]
