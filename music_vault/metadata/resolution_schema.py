"""Additive evidence and materialization journal storage; no data backfills."""
from __future__ import annotations

import sqlite3


RESOLUTION_TABLES = ("metadata_evidence_bundles", "metadata_materializations")


def required_resolution_indexes() -> tuple[str, ...]:
    return ("idx_metadata_materializations_track_created",)


def create_resolution_schema(conn: sqlite3.Connection) -> None:
    """Install DDL only; the caller owns transaction, backup and verification."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(track_artist_credits)")}
    if "credited_as" not in columns:
        conn.execute("ALTER TABLE track_artist_credits ADD COLUMN credited_as TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata_evidence_bundles (
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            evidence_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (track_id, evidence_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata_materializations (
            id TEXT PRIMARY KEY,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            proposal_key TEXT NOT NULL UNIQUE,
            expected_fingerprint TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            evidence_keys_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            undone_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_metadata_materializations_track_created
        ON metadata_materializations(track_id, created_at, id)
    """)
