from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

from .sync_schema import PLAYLIST_TRACK_ORIGINS_TABLE, utc_now


class ManagedPlaylistError(RuntimeError):
    """Raised when an ordinary playlist action would break source management."""


@dataclass(frozen=True)
class PlaylistRemovalResult:
    playlist_id: int
    track_id: int
    manual_origin_removed: bool
    remains_visible: bool
    source_managed: bool
    managing_source_id: int | None


@dataclass(frozen=True)
class SourceDetachResult:
    source_id: int
    affected_playlist_ids: tuple[int, ...]
    preserved_track_count: int


class PlaylistMembershipService:
    """Materialize origin-aware membership into the legacy playlist_tracks table."""

    def __init__(self, db) -> None:
        self.db = db
        self.conn: sqlite3.Connection = db.conn

    def _require_playlist(self, playlist_id: int) -> None:
        if self.conn.execute(
            "SELECT 1 FROM playlists WHERE id=?", (int(playlist_id),)
        ).fetchone() is None:
            raise KeyError(f"Playlist {int(playlist_id)} does not exist.")

    def _source(self, source_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM sync_sources WHERE id=?", (int(source_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"Sync source {int(source_id)} does not exist.")
        return row

    def managed_source_for_playlist(self, playlist_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM sync_sources
            WHERE destination_kind='playlist'
              AND destination_playlist_id=?
              AND archived_at IS NULL
            ORDER BY id
            LIMIT 1
            """,
            (int(playlist_id),),
        ).fetchone()

    def assert_playlist_deletable(self, playlist_id: int) -> None:
        source = self.managed_source_for_playlist(playlist_id)
        if source is not None:
            raise ManagedPlaylistError(
                "This playlist is managed by a saved source. Detach the source before deleting it."
            )

    def _ordered_track_ids(self, playlist_id: int) -> list[int]:
        rows = self.conn.execute(
            f"""
            WITH origin_state AS (
                SELECT
                    origins.track_id AS track_id,
                    MIN(
                        CASE
                            WHEN origins.origin_kind='sync_source'
                             AND sources.id IS NOT NULL
                             AND sources.archived_at IS NULL
                             AND sources.destination_kind='playlist'
                             AND sources.destination_playlist_id=origins.playlist_id
                            THEN origins.origin_position
                        END
                    ) AS source_position,
                    MIN(
                        CASE WHEN origins.origin_kind='manual'
                             THEN origins.origin_position END
                    ) AS manual_position
                FROM {PLAYLIST_TRACK_ORIGINS_TABLE} AS origins
                LEFT JOIN sync_sources AS sources
                  ON sources.id=origins.sync_source_id
                WHERE origins.playlist_id=?
                GROUP BY origins.track_id
            )
            SELECT track_id
            FROM origin_state
            WHERE source_position IS NOT NULL OR manual_position IS NOT NULL
            ORDER BY
                CASE WHEN source_position IS NULL THEN 1 ELSE 0 END,
                source_position,
                manual_position,
                track_id
            """,
            (int(playlist_id),),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _materialize_playlist(self, playlist_id: int) -> list[int]:
        self._require_playlist(playlist_id)
        ordered = self._ordered_track_ids(playlist_id)
        self.conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id=?", (int(playlist_id),)
        )
        self.conn.executemany(
            """
            INSERT INTO playlist_tracks(playlist_id, track_id, position)
            VALUES (?, ?, ?)
            """,
            [
                (int(playlist_id), track_id, position)
                for position, track_id in enumerate(ordered)
            ],
        )
        return ordered

    def materialize_playlist(self, playlist_id: int, *, commit: bool = True) -> list[int]:
        if commit:
            with self.conn:
                return self._materialize_playlist(int(playlist_id))
        return self._materialize_playlist(int(playlist_id))

    def _next_manual_position(self, playlist_id: int) -> int:
        row = self.conn.execute(
            f"""
            SELECT COALESCE(MAX(origin_position), -1) + 1
            FROM {PLAYLIST_TRACK_ORIGINS_TABLE}
            WHERE playlist_id=? AND origin_kind='manual'
            """,
            (int(playlist_id),),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _add_manual_origin(
        self,
        playlist_id: int,
        track_id: int,
        *,
        origin_position: int | None = None,
        update_existing_position: bool = False,
    ) -> bool:
        self._require_playlist(playlist_id)
        if self.conn.execute(
            "SELECT 1 FROM tracks WHERE id=?", (int(track_id),)
        ).fetchone() is None:
            raise KeyError(f"Track {int(track_id)} does not exist.")
        existing = self.conn.execute(
            f"""
            SELECT id FROM {PLAYLIST_TRACK_ORIGINS_TABLE}
            WHERE playlist_id=? AND track_id=? AND origin_kind='manual'
            """,
            (int(playlist_id), int(track_id)),
        ).fetchone()
        timestamp = utc_now()
        position = (
            self._next_manual_position(playlist_id)
            if origin_position is None
            else max(0, int(origin_position))
        )
        if existing is not None:
            if update_existing_position:
                self.conn.execute(
                    f"""
                    UPDATE {PLAYLIST_TRACK_ORIGINS_TABLE}
                    SET origin_position=?, updated_at=?
                    WHERE id=?
                    """,
                    (position, timestamp, int(existing[0])),
                )
            return False
        self.conn.execute(
            f"""
            INSERT INTO {PLAYLIST_TRACK_ORIGINS_TABLE} (
                playlist_id, track_id, origin_kind, sync_source_id,
                origin_position, created_at, updated_at
            ) VALUES (?, ?, 'manual', NULL, ?, ?, ?)
            """,
            (int(playlist_id), int(track_id), position, timestamp, timestamp),
        )
        return True

    def add_manual_origin(
        self, playlist_id: int, track_id: int, *, commit: bool = True
    ) -> bool:
        def perform() -> bool:
            created = self._add_manual_origin(int(playlist_id), int(track_id))
            self._materialize_playlist(int(playlist_id))
            return created

        if commit:
            with self.conn:
                return perform()
        return perform()

    def remove_manual_origin(
        self, playlist_id: int, track_id: int, *, commit: bool = True
    ) -> PlaylistRemovalResult:
        def perform() -> PlaylistRemovalResult:
            cursor = self.conn.execute(
                f"""
                DELETE FROM {PLAYLIST_TRACK_ORIGINS_TABLE}
                WHERE playlist_id=? AND track_id=? AND origin_kind='manual'
                """,
                (int(playlist_id), int(track_id)),
            )
            self._materialize_playlist(int(playlist_id))
            source = self.conn.execute(
                f"""
                SELECT sources.id
                FROM {PLAYLIST_TRACK_ORIGINS_TABLE} AS origins
                JOIN sync_sources AS sources ON sources.id=origins.sync_source_id
                WHERE origins.playlist_id=?
                  AND origins.track_id=?
                  AND origins.origin_kind='sync_source'
                  AND sources.archived_at IS NULL
                  AND sources.destination_playlist_id=?
                ORDER BY sources.id
                LIMIT 1
                """,
                (int(playlist_id), int(track_id), int(playlist_id)),
            ).fetchone()
            remains = self.conn.execute(
                """
                SELECT 1 FROM playlist_tracks WHERE playlist_id=? AND track_id=?
                """,
                (int(playlist_id), int(track_id)),
            ).fetchone() is not None
            return PlaylistRemovalResult(
                playlist_id=int(playlist_id),
                track_id=int(track_id),
                manual_origin_removed=cursor.rowcount > 0,
                remains_visible=remains,
                source_managed=source is not None,
                managing_source_id=int(source[0]) if source is not None else None,
            )

        if commit:
            with self.conn:
                return perform()
        return perform()

    @staticmethod
    def _track_position(item: object) -> tuple[int, int]:
        if isinstance(item, Mapping):
            return int(item["track_id"]), max(0, int(item.get("source_position") or 0))
        if hasattr(item, "track_id"):
            return int(getattr(item, "track_id")), max(
                0, int(getattr(item, "source_position", 0) or 0)
            )
        track_id, position = item  # type: ignore[misc]
        return int(track_id), max(0, int(position or 0))

    def _set_source_origins(
        self,
        source_id: int,
        playlist_id: int,
        track_positions: Iterable[object],
    ) -> int:
        source = self._source(source_id)
        if source["archived_at"] is not None:
            raise ValueError("An archived source cannot manage playlist membership.")
        if (
            str(source["destination_kind"]) != "playlist"
            or source["destination_playlist_id"] is None
            or int(source["destination_playlist_id"]) != int(playlist_id)
        ):
            raise ValueError("The source is not linked to that destination playlist.")
        self._require_playlist(playlist_id)

        collapsed: dict[int, int] = {}
        for item in track_positions:
            track_id, position = self._track_position(item)
            collapsed[track_id] = min(position, collapsed.get(track_id, position))

        self.conn.execute(
            f"""
            DELETE FROM {PLAYLIST_TRACK_ORIGINS_TABLE}
            WHERE sync_source_id=? AND origin_kind='sync_source'
            """,
            (int(source_id),),
        )
        timestamp = utc_now()
        self.conn.executemany(
            f"""
            INSERT INTO {PLAYLIST_TRACK_ORIGINS_TABLE} (
                playlist_id, track_id, origin_kind, sync_source_id,
                origin_position, created_at, updated_at
            ) VALUES (?, ?, 'sync_source', ?, ?, ?, ?)
            """,
            [
                (
                    int(playlist_id),
                    track_id,
                    int(source_id),
                    position,
                    timestamp,
                    timestamp,
                )
                for track_id, position in sorted(
                    collapsed.items(), key=lambda pair: (pair[1], pair[0])
                )
            ],
        )
        self._materialize_playlist(int(playlist_id))
        return len(collapsed)

    def set_source_origins(
        self,
        source_id: int,
        playlist_id: int,
        track_positions: Iterable[object],
        *,
        commit: bool = True,
    ) -> int:
        if commit:
            with self.conn:
                return self._set_source_origins(source_id, playlist_id, track_positions)
        return self._set_source_origins(source_id, playlist_id, track_positions)

    def reconcile_source(
        self,
        source_id: int,
        snapshot_items: Iterable[object] | None = None,
        *,
        commit: bool = True,
    ) -> int:
        source = self._source(source_id)
        if str(source["destination_kind"]) != "playlist":
            return 0
        playlist_id = int(source["destination_playlist_id"])
        items: Iterable[object]
        if snapshot_items is None:
            items = self.conn.execute(
                """
                SELECT track_id, MIN(COALESCE(source_position, 0)) AS source_position
                FROM sync_source_items
                WHERE source_id=? AND removed_at IS NULL AND track_id IS NOT NULL
                GROUP BY track_id
                ORDER BY source_position, track_id
                """,
                (int(source_id),),
            ).fetchall()
        else:
            items = snapshot_items
        return self.set_source_origins(
            int(source_id), playlist_id, items, commit=commit
        )

    def detach_source(
        self,
        source_id: int,
        preserve_visible: bool = True,
        *,
        commit: bool = True,
    ) -> SourceDetachResult:
        if not preserve_visible:
            raise ValueError("Source detachment must preserve visible playlist contents.")

        def perform() -> SourceDetachResult:
            self._source(source_id)
            affected = {
                int(row[0])
                for row in self.conn.execute(
                    f"""
                    SELECT DISTINCT playlist_id
                    FROM {PLAYLIST_TRACK_ORIGINS_TABLE}
                    WHERE sync_source_id=?
                    """,
                    (int(source_id),),
                )
            }
            destination = self.conn.execute(
                "SELECT destination_playlist_id FROM sync_sources WHERE id=?",
                (int(source_id),),
            ).fetchone()
            if destination is not None and destination[0] is not None:
                affected.add(int(destination[0]))

            preserved: set[tuple[int, int]] = set()
            for playlist_id in sorted(affected):
                visible = self.conn.execute(
                    """
                    SELECT track_id, position FROM playlist_tracks
                    WHERE playlist_id=? ORDER BY position, track_id
                    """,
                    (playlist_id,),
                ).fetchall()
                for row in visible:
                    track_id = int(row["track_id"])
                    self._add_manual_origin(
                        playlist_id,
                        track_id,
                        origin_position=int(row["position"]),
                        update_existing_position=True,
                    )
                    preserved.add((playlist_id, track_id))

            self.conn.execute(
                f"""
                DELETE FROM {PLAYLIST_TRACK_ORIGINS_TABLE}
                WHERE sync_source_id=? AND origin_kind='sync_source'
                """,
                (int(source_id),),
            )
            self.conn.execute(
                """
                UPDATE sync_sources
                SET destination_kind='library', destination_playlist_id=NULL, updated_at=?
                WHERE id=?
                """,
                (utc_now(), int(source_id)),
            )
            for playlist_id in sorted(affected):
                self._materialize_playlist(playlist_id)
            return SourceDetachResult(
                source_id=int(source_id),
                affected_playlist_ids=tuple(sorted(affected)),
                preserved_track_count=len(preserved),
            )

        if commit:
            with self.conn:
                return perform()
        return perform()
