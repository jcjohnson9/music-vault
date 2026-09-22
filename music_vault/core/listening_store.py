"""Small transaction-scoped store for canonical favorites and observed playback.

Only the two listening tables are writable here. This module never opens media,
inspects sources, changes metadata, restores queues, or contacts a provider.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import sqlite3

from .listening_schema import END_REASONS, PLAYBACK_ORIGINS


def _utc(value=None) -> str:
    moment = datetime.now(timezone.utc) if value is None else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Listening timestamps must include a timezone.")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _limit(value: int) -> int:
    return max(1, min(1000, int(value)))


class ListeningStore:
    def __init__(self, conn: sqlite3.Connection, *, clock=None):
        self.conn = conn
        self._clock = clock or (lambda: _utc())
        self.write_count = 0
        self.revision = 0

    def _rows(self, sql, parameters=()) -> list[dict]:
        cursor = self.conn.cursor()
        cursor.row_factory = sqlite3.Row
        return [dict(row) for row in cursor.execute(sql, parameters)]

    def _write(self, operation) -> bool:
        if self.conn.in_transaction:
            raise RuntimeError("Listening writes require an independent transaction.")
        before = self.conn.total_changes
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            with self.conn:
                changed = bool(operation())
        finally:
            # SQLite total_changes includes rolled-back writes. Subtract all
            # listening-only work from a coarse library cache stamp, but emit
            # UI invalidation only after a successfully committed mutation.
            self.write_count += self.conn.total_changes - before
        if changed:
            self.revision += 1
        return changed

    def set_favorite(self, track_id: int, desired: bool) -> bool:
        track_id = int(track_id)

        def change():
            if not desired:
                return self.conn.execute("DELETE FROM track_favorites WHERE track_id=?", (track_id,)).rowcount > 0
            row = self._rows("SELECT title,artist FROM tracks WHERE id=?", (track_id,))
            if not row:
                return False
            return self.conn.execute("""
                INSERT INTO track_favorites(track_id,recorded_track_id,title_at_favorite,artist_at_favorite,favorited_at)
                VALUES (?,?,?,?,?) ON CONFLICT(track_id) DO NOTHING
            """, (track_id, track_id, str(row[0]["title"] or "")[:1024], str(row[0]["artist"] or "")[:1024], _utc(self._clock()))).rowcount > 0

        return self._write(change)

    def remove_favorite(self, favorite_row_id: int) -> bool:
        return self._write(lambda: self.conn.execute("DELETE FROM track_favorites WHERE id=?", (int(favorite_row_id),)).rowcount > 0)

    def is_favorite(self, track_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM track_favorites WHERE track_id=?", (int(track_id),)).fetchone() is not None

    def favorite_ids(self, track_ids: Iterable[int] | None = None) -> set[int]:
        if track_ids is None:
            return {int(row[0]) for row in self.conn.execute("SELECT track_id FROM track_favorites WHERE track_id IS NOT NULL")}
        ids = sorted({int(value) for value in track_ids})
        found = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update(int(row[0]) for row in self.conn.execute(
                f"SELECT track_id FROM track_favorites WHERE track_id IN ({','.join('?' for _ in chunk)})", chunk,
            ))
        return found

    def liked_tracks(self, *, limit=200, offset=0, include_unavailable=False) -> list[dict]:
        where = "" if include_unavailable else "WHERE t.id IS NOT NULL"
        rows = self._rows(f"""
            SELECT t.*,f.id AS favorite_id,f.recorded_track_id,f.favorited_at,
                f.title_at_favorite,f.artist_at_favorite,t.id IS NOT NULL AS available
            FROM track_favorites f LEFT JOIN tracks t ON t.id=f.track_id
            {where} ORDER BY f.favorited_at DESC,f.id DESC LIMIT ? OFFSET ?
        """, (-1 if limit is None else _limit(limit), max(0, int(offset))))
        for row in rows:
            if not row["available"]:
                row["title"], row["artist"] = row["title_at_favorite"], row["artist_at_favorite"]
        return rows

    def unavailable_favorites(self, *, limit=100, offset=0) -> list[dict]:
        return self._rows("""
            SELECT id AS favorite_id,recorded_track_id,favorited_at,
                title_at_favorite AS title,artist_at_favorite AS artist,
                NULL AS track_id,0 AS available
            FROM track_favorites WHERE track_id IS NULL
            ORDER BY favorited_at DESC,id DESC LIMIT ? OFFSET ?
        """, (_limit(limit), max(0, int(offset))))

    def save_event(self, snapshot: Mapping) -> bool:
        record = dict(snapshot)
        for key in ("event_id", "run_id"):
            record[key] = str(record[key])
            if not 1 <= len(record[key]) <= 128:
                raise ValueError("Invalid listening occurrence identity.")
        record["recorded_track_id"] = int(record["recorded_track_id"])
        record["track_id"] = int(record["track_id"]) if record.get("track_id") is not None else None
        if record["recorded_track_id"] <= 0 or record["track_id"] not in (None, record["recorded_track_id"]):
            raise ValueError("Listening occurrence must retain one canonical track identity.")
        for key in ("title_at_start", "artist_at_start", "album_at_start"):
            record[key] = str(record.get(key) or "")[:1024]
        for key in ("started_at", "last_observed_at"):
            record[key] = _utc(record[key])
        for key in ("ended_at", "qualified_at"):
            record[key] = _utc(record[key]) if record.get(key) is not None else None
        record["listened_ms"] = int(record["listened_ms"])
        record["update_sequence"] = int(record["update_sequence"])
        record["duration_ms"] = int(record["duration_ms"]) if record.get("duration_ms") else None
        record["end_reason"] = record.get("end_reason")
        if (record["listened_ms"] < 0 or record["update_sequence"] < 0
                or (record["duration_ms"] is not None and record["duration_ms"] <= 0)
                or record["playback_origin"] not in PLAYBACK_ORIGINS
                or record["end_reason"] not in END_REASONS | {None}
                or bool(record["ended_at"]) != bool(record["end_reason"])):
            raise ValueError("Invalid cumulative listening state.")
        for key, bound in (("context_kind", 80), ("context_label", 256)):
            record[key] = str(record[key])[:bound] if record.get(key) is not None else None
        record["context_playlist_id"] = int(record["context_playlist_id"]) if record.get("context_playlist_id") is not None else None
        keys = (
            "event_id", "run_id", "track_id", "recorded_track_id", "title_at_start", "artist_at_start", "album_at_start",
            "started_at", "last_observed_at", "ended_at", "qualified_at", "listened_ms", "duration_ms", "end_reason",
            "playback_origin", "context_kind", "context_playlist_id", "context_label", "update_sequence",
        )

        def change():
            if record["track_id"] is not None and not self.conn.execute("SELECT 1 FROM tracks WHERE id=?", (record["track_id"],)).fetchone():
                record["track_id"] = None
            if record["context_playlist_id"] is not None and not self.conn.execute("SELECT 1 FROM playlists WHERE id=?", (record["context_playlist_id"],)).fetchone():
                record["context_playlist_id"] = None
            return self.conn.execute(f"""
                INSERT INTO listening_events ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})
                ON CONFLICT(event_id) DO UPDATE SET
                    last_observed_at=excluded.last_observed_at,
                    listened_ms=excluded.listened_ms,
                    duration_ms=COALESCE(excluded.duration_ms,listening_events.duration_ms),
                    qualified_at=COALESCE(listening_events.qualified_at,excluded.qualified_at),
                    ended_at=excluded.ended_at,end_reason=excluded.end_reason,
                    update_sequence=excluded.update_sequence
                WHERE listening_events.ended_at IS NULL
                    AND excluded.update_sequence > listening_events.update_sequence
                    AND excluded.listened_ms >= listening_events.listened_ms
                    AND excluded.run_id=listening_events.run_id
                    AND excluded.recorded_track_id=listening_events.recorded_track_id
                    AND excluded.started_at=listening_events.started_at
            """, tuple(record[key] for key in keys)).rowcount > 0

        return self._write(change)

    def history_page(self, *, limit=100, before=None, run_id=None) -> list[dict]:
        parameters = []
        where = ""
        if before is not None:
            where = "WHERE (e.started_at,e.event_id) < (?,?)"
            parameters.extend((_utc(before[0]), str(before[1])))
        rows = self._rows(f"""
            SELECT e.*,t.id IS NOT NULL AS available,
                CASE WHEN t.id IS NULL THEN e.title_at_start ELSE COALESCE(t.title,'') END AS title,
                CASE WHEN t.id IS NULL THEN e.artist_at_start ELSE COALESCE(t.artist,'') END AS artist,
                CASE WHEN t.id IS NULL THEN e.album_at_start ELSE COALESCE(t.album,'') END AS album
            FROM listening_events e LEFT JOIN tracks t ON t.id=e.track_id
            {where} ORDER BY e.started_at DESC,e.event_id DESC LIMIT ?
        """, (*parameters, _limit(limit)))
        for row in rows:
            row["interrupted"] = row["ended_at"] is None and (run_id is None or row["run_id"] != run_id)
        return rows

    def recently_played_tracks(self, *, limit=200, offset=0) -> list[dict]:
        return self._rows("""
            SELECT t.*,h.last_played_at,h.qualified_listen_count FROM tracks t JOIN (
                SELECT track_id,MAX(started_at) AS last_played_at,
                    COUNT(qualified_at) AS qualified_listen_count
                FROM listening_events WHERE track_id IS NOT NULL GROUP BY track_id
            ) h ON h.track_id=t.id
            ORDER BY h.last_played_at DESC,t.id DESC LIMIT ? OFFSET ?
        """, (_limit(limit), max(0, int(offset))))

    def rediscover_tracks(self, kind="favorites_to_revisit", *, limit=50, offset=0) -> list[dict]:
        if kind == "favorites_to_revisit":
            sql = """
                SELECT t.*,f.id AS favorite_id,f.favorited_at,
                    (SELECT MAX(e.started_at) FROM listening_events e WHERE e.track_id=t.id) AS last_played_at,
                    'favorites_to_revisit' AS reason
                FROM track_favorites f JOIN tracks t ON t.id=f.track_id
                ORDER BY last_played_at ASC,f.favorited_at ASC,t.id ASC LIMIT ? OFFSET ?
            """
        elif kind == "no_recorded_plays":
            sql = """
                SELECT t.*,'no_recorded_plays' AS reason FROM tracks t
                WHERE NOT EXISTS (SELECT 1 FROM listening_events e WHERE e.track_id=t.id)
                ORDER BY t.id ASC LIMIT ? OFFSET ?
            """
        else:
            raise ValueError("Unknown local rediscovery category.")
        return self._rows(sql, (_limit(limit), max(0, int(offset))))
