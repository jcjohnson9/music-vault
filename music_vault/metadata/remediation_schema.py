from __future__ import annotations

import sqlite3
from typing import Iterable


REMEDIATION_JOBS_TABLE = "metadata_remediation_jobs"
REMEDIATION_ITEMS_TABLE = "metadata_remediation_items"
PROVIDER_CACHE_TABLE = "metadata_provider_cache"

REMEDIATION_JOB_STATUSES = (
    "created",
    "analyzing",
    "paused",
    "ready",
    "applying",
    "complete",
    "complete_with_issues",
    "cancelled",
    "failed",
    "rolling_back",
    "rolled_back",
)

REMEDIATION_ITEM_STATUSES = (
    "pending",
    "analyzing",
    "high_confidence",
    "needs_review",
    "ambiguous",
    "no_match",
    "skipped",
    "failed",
    "approved",
    "applying",
    "applied",
    "apply_failed",
    "rollback_pending",
    "rolled_back",
    "cancelled",
    "conflict",
)

REMEDIATION_CONFIDENCE_CLASSES = (
    "high_confidence",
    "needs_review",
    "ambiguous",
    "no_match",
    "skipped",
    "failed",
)

FILE_WRITE_STATUSES = (
    "pending",
    "not_requested",
    "unsupported",
    "prepared",
    "written",
    "verified",
    "failed",
    "restored",
    "conflict",
)

PROVIDER_CACHE_RESPONSE_STATUSES = (
    "success",
    "no_match",
    "failed",
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def create_remediation_schema(conn: sqlite3.Connection) -> None:
    """Create the additive Batch 7 job, item, and provider-cache schema."""

    job_statuses = _sql_values(REMEDIATION_JOB_STATUSES)
    item_statuses = _sql_values(REMEDIATION_ITEM_STATUSES)
    confidence_classes = _sql_values(REMEDIATION_CONFIDENCE_CLASSES)
    file_statuses = _sql_values(FILE_WRITE_STATUSES)
    cache_statuses = _sql_values(PROVIDER_CACHE_RESPONSE_STATUSES)

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REMEDIATION_JOBS_TABLE} (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'created'
                CHECK (status IN ({job_statuses})),
            mode TEXT NOT NULL CHECK (length(trim(mode)) > 0),
            provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
            library_revision TEXT NOT NULL CHECK (length(trim(library_revision)) > 0),
            total_items INTEGER NOT NULL DEFAULT 0 CHECK (total_items >= 0),
            analyzed_items INTEGER NOT NULL DEFAULT 0 CHECK (analyzed_items >= 0),
            high_confidence_items INTEGER NOT NULL DEFAULT 0
                CHECK (high_confidence_items >= 0),
            review_items INTEGER NOT NULL DEFAULT 0 CHECK (review_items >= 0),
            ambiguous_items INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous_items >= 0),
            no_match_items INTEGER NOT NULL DEFAULT 0 CHECK (no_match_items >= 0),
            skipped_items INTEGER NOT NULL DEFAULT 0 CHECK (skipped_items >= 0),
            failed_items INTEGER NOT NULL DEFAULT 0 CHECK (failed_items >= 0),
            applied_items INTEGER NOT NULL DEFAULT 0 CHECK (applied_items >= 0),
            file_written_items INTEGER NOT NULL DEFAULT 0 CHECK (file_written_items >= 0),
            rolled_back_items INTEGER NOT NULL DEFAULT 0 CHECK (rolled_back_items >= 0),
            last_error TEXT,
            private_report_path TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REMEDIATION_ITEMS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ({item_statuses})),
            current_snapshot TEXT NOT NULL,
            proposed_patch TEXT,
            candidate_snapshot TEXT,
            confidence_score REAL
                CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0 AND 100),
            confidence_class TEXT
                CHECK (confidence_class IS NULL OR confidence_class IN ({confidence_classes})),
            match_reasons TEXT,
            provider_recording_id TEXT,
            provider_release_id TEXT,
            artwork_candidate TEXT,
            review_reason TEXT,
            approved_fields TEXT,
            apply_error TEXT,
            file_write_status TEXT NOT NULL DEFAULT 'not_requested'
                CHECK (file_write_status IN ({file_statuses})),
            original_file_hash TEXT,
            original_audio_payload_hash TEXT,
            backup_file TEXT,
            prepared_file TEXT,
            updated_file_hash TEXT,
            updated_audio_payload_hash TEXT,
            applied_change_group_id TEXT,
            rollback_change_group_id TEXT,
            applied_snapshot TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (job_id, track_id),
            FOREIGN KEY (job_id) REFERENCES {REMEDIATION_JOBS_TABLE}(id)
                ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id)
                ON DELETE CASCADE
        )
        """
    )
    item_columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({REMEDIATION_ITEMS_TABLE})")
    }
    if "rollback_change_group_id" not in item_columns:
        conn.execute(
            f"ALTER TABLE {REMEDIATION_ITEMS_TABLE} ADD COLUMN rollback_change_group_id TEXT"
        )
    if "prepared_file" not in item_columns:
        conn.execute(
            f"ALTER TABLE {REMEDIATION_ITEMS_TABLE} ADD COLUMN prepared_file TEXT"
        )
    if "applied_snapshot" not in item_columns:
        conn.execute(
            f"ALTER TABLE {REMEDIATION_ITEMS_TABLE} ADD COLUMN applied_snapshot TEXT"
        )
    if "approved_fields" not in item_columns:
        conn.execute(
            f"ALTER TABLE {REMEDIATION_ITEMS_TABLE} ADD COLUMN approved_fields TEXT"
        )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROVIDER_CACHE_TABLE} (
            provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
            normalized_query_key TEXT NOT NULL
                CHECK (length(trim(normalized_query_key)) > 0),
            response_status TEXT NOT NULL
                CHECK (response_status IN ({cache_statuses})),
            candidate_data TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (provider, normalized_query_key)
        )
        """
    )

    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_remediation_jobs_status_updated "
        f"ON {REMEDIATION_JOBS_TABLE}(status, updated_at DESC)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_remediation_jobs_library_revision "
        f"ON {REMEDIATION_JOBS_TABLE}(library_revision)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_remediation_items_job_status "
        f"ON {REMEDIATION_ITEMS_TABLE}(job_id, status, id)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_remediation_items_track "
        f"ON {REMEDIATION_ITEMS_TABLE}(track_id)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_remediation_items_confidence "
        f"ON {REMEDIATION_ITEMS_TABLE}(job_id, confidence_class, confidence_score DESC)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_remediation_items_file_status "
        f"ON {REMEDIATION_ITEMS_TABLE}(job_id, file_write_status)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_provider_cache_expires "
        f"ON {PROVIDER_CACHE_TABLE}(expires_at)"
    )


def required_remediation_indexes() -> Iterable[str]:
    return (
        "idx_remediation_jobs_status_updated",
        "idx_remediation_jobs_library_revision",
        "idx_remediation_items_job_status",
        "idx_remediation_items_track",
        "idx_remediation_items_confidence",
        "idx_remediation_items_file_status",
        "idx_provider_cache_expires",
    )
