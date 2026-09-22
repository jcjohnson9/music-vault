"""Additive private listening state, independent of metadata and playlists."""
from __future__ import annotations

import sqlite3


LISTENING_TABLES = ("track_favorites", "listening_events")
END_REASONS = frozenset({"ended", "next", "previous", "replaced", "error", "stopped", "app_closed"})
PLAYBACK_ORIGINS = frozenset({"manual", "manual_queue", "base", "repeat"})


def required_listening_indexes() -> tuple[str, ...]:
    return (
        "idx_track_favorites_recency", "idx_listening_events_recency",
        "idx_listening_events_track_recency", "idx_listening_events_qualified",
    )


def create_listening_schema(conn: sqlite3.Connection) -> None:
    """Execute DDL only. The caller owns backup, transaction and verification."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_favorites (
            id INTEGER PRIMARY KEY,
            track_id INTEGER UNIQUE REFERENCES tracks(id) ON DELETE SET NULL,
            recorded_track_id INTEGER NOT NULL CHECK (recorded_track_id > 0),
            title_at_favorite TEXT NOT NULL CHECK (length(title_at_favorite) <= 1024),
            artist_at_favorite TEXT NOT NULL CHECK (length(artist_at_favorite) <= 1024),
            favorited_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listening_events (
            event_id TEXT PRIMARY KEY NOT NULL CHECK (length(event_id) BETWEEN 1 AND 128),
            run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
            track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
            recorded_track_id INTEGER NOT NULL CHECK (recorded_track_id > 0),
            title_at_start TEXT NOT NULL CHECK (length(title_at_start) <= 1024),
            artist_at_start TEXT NOT NULL CHECK (length(artist_at_start) <= 1024),
            album_at_start TEXT NOT NULL CHECK (length(album_at_start) <= 1024),
            started_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            ended_at TEXT,
            qualified_at TEXT,
            listened_ms INTEGER NOT NULL CHECK (listened_ms >= 0),
            duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms > 0),
            end_reason TEXT CHECK (end_reason IS NULL OR end_reason IN (
                'ended','next','previous','replaced','error','stopped','app_closed'
            )),
            playback_origin TEXT NOT NULL CHECK (playback_origin IN ('manual','manual_queue','base','repeat')),
            context_kind TEXT CHECK (context_kind IS NULL OR length(context_kind) <= 80),
            context_playlist_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
            context_label TEXT CHECK (context_label IS NULL OR length(context_label) <= 256),
            update_sequence INTEGER NOT NULL CHECK (update_sequence >= 0),
            CHECK ((ended_at IS NULL AND end_reason IS NULL) OR
                   (ended_at IS NOT NULL AND end_reason IS NOT NULL))
        )
    """)
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_track_favorites_recency ON track_favorites(favorited_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_listening_events_recency ON listening_events(started_at DESC, event_id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_listening_events_track_recency ON listening_events(track_id, started_at DESC, event_id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_listening_events_qualified ON listening_events(track_id, qualified_at DESC) WHERE qualified_at IS NOT NULL",
    ):
        conn.execute(statement)
