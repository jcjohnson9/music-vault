from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from .safety import sanitize_error_text
from .sync_result import SyncResult, utc_now


SOURCE_KIND_YOUTUBE_PLAYLIST = "youtube_playlist"
DESTINATION_LIBRARY = "library"
DESTINATION_PLAYLIST = "playlist"
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,150}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}
_UNSET = object()


class SyncSourceError(ValueError):
    pass


class SyncSourceNotFoundError(SyncSourceError):
    pass


class SyncSourceDestinationError(SyncSourceError):
    pass


@dataclass(frozen=True)
class NormalizedYouTubeSource:
    external_id: str
    source_url: str


@dataclass(frozen=True)
class SyncSource:
    id: int
    source_kind: str
    external_id: str
    source_url: str
    label: str | None
    remote_title: str | None
    enabled: bool
    sort_order: int
    destination_kind: str
    destination_playlist_id: int | None
    storage_key: str
    last_sync_at: str | None
    last_sync_status: str | None
    last_visible_count: int
    last_new_count: int
    last_downloaded_count: int
    last_imported_count: int
    last_existing_count: int
    last_failed_count: int
    last_error: str | None
    created_at: str
    updated_at: str
    archived_at: str | None

    @property
    def display_label(self) -> str:
        return self.label or self.remote_title or f"YouTube {self.external_id[:12]}"


def normalize_youtube_playlist_source(value: object) -> NormalizedYouTubeSource:
    """Normalize a public/unlisted YouTube playlist URL or raw playlist ID."""

    text = str(value or "").strip()
    if not text:
        raise SyncSourceError("A YouTube playlist URL or ID is required.")

    if "://" not in text:
        if not _PLAYLIST_ID_RE.fullmatch(text):
            raise SyncSourceError("The YouTube playlist ID is malformed.")
        external_id = text
    else:
        parsed = urlparse(text)
        if parsed.scheme.casefold() != "https":
            raise SyncSourceError("Only HTTPS YouTube playlist URLs are supported.")
        if parsed.username or parsed.password:
            raise SyncSourceError("Credential-bearing playlist URLs are not supported.")
        if (parsed.hostname or "").casefold() not in _YOUTUBE_HOSTS:
            raise SyncSourceError("The source must be a YouTube playlist URL.")
        try:
            if parsed.port not in (None, 443):
                raise SyncSourceError("Custom ports are not supported for YouTube sources.")
        except ValueError as exc:
            raise SyncSourceError("The YouTube playlist URL is malformed.") from exc
        external_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
        if not _PLAYLIST_ID_RE.fullmatch(external_id):
            raise SyncSourceError(
                "The YouTube URL must contain a valid playlist ID in list=."
            )

    return NormalizedYouTubeSource(
        external_id=external_id,
        source_url=f"https://www.youtube.com/playlist?list={external_id}",
    )


def stable_source_storage_key(
    source_kind: str,
    external_id: str,
    *,
    max_length: int = 64,
) -> str:
    """Return a stable, bounded Windows-safe key derived only from identity."""

    normalized_kind = re.sub(r"[^a-z0-9]+", "_", str(source_kind).casefold()).strip("_")
    normalized_kind = normalized_kind or "source"
    identity = str(external_id).strip()
    safe_identity = re.sub(r"[^A-Za-z0-9_-]+", "_", identity).strip(" ._")
    digest = hashlib.sha256(f"{normalized_kind}:{identity}".encode("utf-8")).hexdigest()[:10]
    suffix = f"_{digest}"
    prefix = "youtube" if normalized_kind == SOURCE_KIND_YOUTUBE_PLAYLIST else normalized_kind
    available = max(1, max_length - len(prefix) - 1 - len(suffix))
    readable = (safe_identity[:available].rstrip(" ._") or "source")
    return f"{prefix}_{readable}{suffix}"[:max_length].rstrip(" .")


class SyncSourceService:
    """Persistent source CRUD and source-card state, separate from the GUI."""

    def __init__(self, db, membership_service=None) -> None:
        self.db = db
        self.conn: sqlite3.Connection = db.conn
        self.membership_service = membership_service

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SyncSource:
        return SyncSource(
            id=int(row["id"]),
            source_kind=str(row["source_kind"]),
            external_id=str(row["external_id"]),
            source_url=str(row["source_url"]),
            label=str(row["label"]) if row["label"] is not None else None,
            remote_title=(
                str(row["remote_title"]) if row["remote_title"] is not None else None
            ),
            enabled=bool(row["enabled"]),
            sort_order=int(row["sort_order"]),
            destination_kind=str(row["destination_kind"]),
            destination_playlist_id=(
                int(row["destination_playlist_id"])
                if row["destination_playlist_id"] is not None
                else None
            ),
            storage_key=str(row["storage_key"]),
            last_sync_at=row["last_sync_at"],
            last_sync_status=row["last_sync_status"],
            last_visible_count=int(row["last_visible_count"] or 0),
            last_new_count=int(row["last_new_count"] or 0),
            last_downloaded_count=int(row["last_downloaded_count"] or 0),
            last_imported_count=int(row["last_imported_count"] or 0),
            last_existing_count=int(row["last_existing_count"] or 0),
            last_failed_count=int(row["last_failed_count"] or 0),
            last_error=row["last_error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            archived_at=row["archived_at"],
        )

    def get(self, source_id: int, *, include_archived: bool = False) -> SyncSource:
        query = "SELECT * FROM sync_sources WHERE id=?"
        parameters: tuple[object, ...] = (int(source_id),)
        if not include_archived:
            query += " AND archived_at IS NULL"
        row = self.conn.execute(query, parameters).fetchone()
        if row is None:
            raise SyncSourceNotFoundError("The saved synchronization source was not found.")
        return self._from_row(row)

    def list_active(self, *, enabled_only: bool = False) -> list[SyncSource]:
        query = "SELECT * FROM sync_sources WHERE archived_at IS NULL"
        if enabled_only:
            query += " AND enabled=1"
        query += " ORDER BY sort_order, id"
        return [self._from_row(row) for row in self.conn.execute(query)]

    def list_archived(self) -> list[SyncSource]:
        return [
            self._from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM sync_sources WHERE archived_at IS NOT NULL "
                "ORDER BY archived_at DESC, id DESC"
            )
        ]

    def _next_sort_order(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM sync_sources "
            "WHERE archived_at IS NULL"
        ).fetchone()
        return int(row[0])

    def _validate_destination(
        self,
        destination_kind: str,
        destination_playlist_id: int | None,
        *,
        excluding_source_id: int | None = None,
    ) -> tuple[str, int | None]:
        kind = str(destination_kind or "").strip().casefold()
        if kind == DESTINATION_LIBRARY:
            if destination_playlist_id is not None:
                raise SyncSourceDestinationError(
                    "Library Only sources cannot have a destination playlist."
                )
            return kind, None
        if kind != DESTINATION_PLAYLIST or destination_playlist_id is None:
            raise SyncSourceDestinationError(
                "Managed Local Playlist sources require a destination playlist."
            )
        playlist_id = int(destination_playlist_id)
        if self.conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone() is None:
            raise SyncSourceDestinationError("The destination playlist does not exist.")
        manager = self.conn.execute(
            """
            SELECT id FROM sync_sources
            WHERE archived_at IS NULL AND destination_kind='playlist'
              AND destination_playlist_id=? AND id<>COALESCE(?, -1)
            LIMIT 1
            """,
            (playlist_id, excluding_source_id),
        ).fetchone()
        if manager is not None:
            raise SyncSourceDestinationError(
                "That local playlist is already managed by another active source."
            )
        return kind, playlist_id

    def create_source(
        self,
        value: object,
        *,
        label: str | None = None,
        enabled: bool = True,
        destination_kind: str = DESTINATION_LIBRARY,
        destination_playlist_id: int | None = None,
    ) -> SyncSource:
        normalized = normalize_youtube_playlist_source(value)
        clean_label = str(label or "").strip() or None
        existing = self.conn.execute(
            "SELECT * FROM sync_sources WHERE source_kind=? AND external_id=?",
            (SOURCE_KIND_YOUTUBE_PLAYLIST, normalized.external_id),
        ).fetchone()
        excluding = int(existing["id"]) if existing is not None else None
        destination_kind, destination_playlist_id = self._validate_destination(
            destination_kind,
            destination_playlist_id,
            excluding_source_id=excluding,
        )
        timestamp = utc_now()
        with self.conn:
            if existing is not None:
                source_id = int(existing["id"])
                if existing["archived_at"] is None:
                    return self._from_row(existing)
                self.conn.execute(
                    """
                    UPDATE sync_sources
                    SET source_url=?, label=COALESCE(?, label), enabled=?,
                        sort_order=?, destination_kind=?, destination_playlist_id=?,
                        archived_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        normalized.source_url,
                        clean_label,
                        int(bool(enabled)),
                        self._next_sort_order(),
                        destination_kind,
                        destination_playlist_id,
                        timestamp,
                        source_id,
                    ),
                )
            else:
                storage_key = stable_source_storage_key(
                    SOURCE_KIND_YOUTUBE_PLAYLIST, normalized.external_id
                )
                cursor = self.conn.execute(
                    """
                    INSERT INTO sync_sources (
                        source_kind, external_id, source_url, label, remote_title,
                        enabled, sort_order, destination_kind, destination_playlist_id,
                        storage_key, last_visible_count, last_new_count,
                        last_downloaded_count, last_imported_count, last_existing_count,
                        last_failed_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?, ?)
                    """,
                    (
                        SOURCE_KIND_YOUTUBE_PLAYLIST,
                        normalized.external_id,
                        normalized.source_url,
                        clean_label,
                        int(bool(enabled)),
                        self._next_sort_order(),
                        destination_kind,
                        destination_playlist_id,
                        storage_key,
                        timestamp,
                        timestamp,
                    ),
                )
                source_id = int(cursor.lastrowid)
            self.conn.execute(
                "UPDATE sync_failures SET sync_source_id=? "
                "WHERE sync_source_id IS NULL AND playlist_id=?",
                (source_id, normalized.external_id),
            )
            self._normalize_order()
        return self.get(source_id)

    def update_source(
        self,
        source_id: int,
        *,
        label: str | None | object = _UNSET,
        enabled: bool | object = _UNSET,
        destination_kind: str | object = _UNSET,
        destination_playlist_id: int | None | object = _UNSET,
    ) -> SyncSource:
        source = self.get(source_id)
        new_kind = (
            source.destination_kind
            if destination_kind is _UNSET
            else str(destination_kind)
        )
        new_playlist_id = (
            source.destination_playlist_id
            if destination_playlist_id is _UNSET
            else destination_playlist_id
        )
        if destination_kind is not _UNSET and str(new_kind).strip().casefold() == DESTINATION_LIBRARY:
            new_playlist_id = None
        new_kind, new_playlist_id = self._validate_destination(
            new_kind, new_playlist_id, excluding_source_id=source.id
        )
        destination_changed = (
            new_kind != source.destination_kind
            or new_playlist_id != source.destination_playlist_id
        )
        values = {
            "label": source.label if label is _UNSET else (str(label or "").strip() or None),
            "enabled": int(source.enabled if enabled is _UNSET else bool(enabled)),
            "destination_kind": new_kind,
            "destination_playlist_id": new_playlist_id,
            "updated_at": utc_now(),
        }
        with self.conn:
            if destination_changed and source.destination_playlist_id is not None:
                self._detach_membership(source.id, commit=False)
            self.conn.execute(
                """
                UPDATE sync_sources
                SET label=:label, enabled=:enabled, destination_kind=:destination_kind,
                    destination_playlist_id=:destination_playlist_id,
                    updated_at=:updated_at
                WHERE id=:source_id AND archived_at IS NULL
                """,
                {**values, "source_id": source.id},
            )
        return self.get(source.id)

    def set_enabled(self, source_id: int, enabled: bool) -> SyncSource:
        return self.update_source(source_id, enabled=enabled)

    def _normalize_order(self) -> None:
        rows = self.conn.execute(
            "SELECT id, sort_order FROM sync_sources WHERE archived_at IS NULL "
            "ORDER BY sort_order, id"
        ).fetchall()
        timestamp = utc_now()
        for order, row in enumerate(rows):
            if int(row["sort_order"]) != order:
                self.conn.execute(
                    "UPDATE sync_sources SET sort_order=?, updated_at=? WHERE id=?",
                    (order, timestamp, int(row["id"])),
                )

    def reorder(self, ordered_source_ids: Iterable[int]) -> list[SyncSource]:
        requested = [int(value) for value in ordered_source_ids]
        current = [source.id for source in self.list_active()]
        if len(requested) != len(set(requested)) or set(requested) != set(current):
            raise SyncSourceError("Source ordering must contain every active source exactly once.")
        timestamp = utc_now()
        with self.conn:
            for order, source_id in enumerate(requested):
                self.conn.execute(
                    "UPDATE sync_sources SET sort_order=?, updated_at=? WHERE id=?",
                    (order, timestamp, source_id),
                )
        return self.list_active()

    def move(self, source_id: int, direction: int) -> list[SyncSource]:
        current = [source.id for source in self.list_active()]
        try:
            index = current.index(int(source_id))
        except ValueError as exc:
            raise SyncSourceNotFoundError("The saved synchronization source was not found.") from exc
        target = max(0, min(len(current) - 1, index + (-1 if direction < 0 else 1)))
        if target != index:
            current[index], current[target] = current[target], current[index]
        return self.reorder(current)

    def _detach_membership(self, source_id: int, *, commit: bool = True) -> None:
        if self.membership_service is None:
            from .playlist_membership import PlaylistMembershipService

            self.membership_service = PlaylistMembershipService(self.db)
        self.membership_service.detach_source(int(source_id), commit=commit)

    def detach(self, source_id: int) -> SyncSource:
        source = self.get(source_id)
        with self.conn:
            if source.destination_playlist_id is not None:
                self._detach_membership(source.id, commit=False)
            self.conn.execute(
                """
                UPDATE sync_sources
                SET destination_kind='library', destination_playlist_id=NULL, updated_at=?
                WHERE id=? AND archived_at IS NULL
                """,
                (utc_now(), source.id),
            )
        return self.get(source.id)

    def archive(self, source_id: int) -> SyncSource:
        source = self.get(source_id)
        timestamp = utc_now()
        with self.conn:
            if source.destination_playlist_id is not None:
                self._detach_membership(source.id, commit=False)
            self.conn.execute(
                """
                UPDATE sync_sources
                SET enabled=0, destination_kind='library', destination_playlist_id=NULL,
                    archived_at=?, updated_at=? WHERE id=?
                """,
                (timestamp, timestamp, source.id),
            )
            self._normalize_order()
        return self.get(source.id, include_archived=True)

    def update_remote_title(self, source_id: int, title: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sync_sources SET remote_title=?, updated_at=? WHERE id=?",
                (str(title or "").strip() or None, utc_now(), int(source_id)),
            )

    def update_last_sync(
        self,
        source_id: int,
        result: SyncResult,
        *,
        commit: bool = True,
    ) -> None:
        first_error = result.failures[0].reason if result.failures else None

        def update() -> None:
            self.conn.execute(
                """
                UPDATE sync_sources
                SET remote_title=COALESCE(?, remote_title), last_sync_at=?,
                    last_sync_status=?, last_visible_count=?, last_new_count=?,
                    last_downloaded_count=?, last_imported_count=?,
                    last_existing_count=?, last_failed_count=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    result.playlist_title,
                    result.finished_at,
                    result.status,
                    result.visible_item_count,
                    result.new_item_count,
                    result.downloaded_count,
                    result.imported_count,
                    result.existing_count,
                    result.failed_count,
                    sanitize_error_text(first_error) if first_error else None,
                    utc_now(),
                    int(source_id),
                ),
            )

        if commit:
            with self.conn:
                update()
        else:
            update()

    def unresolved_failure_count(self, source_id: int | None = None) -> int:
        return int(self.db.unresolved_failure_count(source_id))

    def list_unresolved_failures(
        self,
        source_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Return structured and occurrence-only failures without duplicates."""

        failure_query = "SELECT * FROM sync_failures WHERE status='unresolved'"
        failure_parameters: tuple[object, ...] = ()
        if source_id is not None:
            failure_query += " AND sync_source_id=?"
            failure_parameters = (int(source_id),)
        structured = [
            {**dict(row), "failure_origin": "sync_failure"}
            for row in self.conn.execute(failure_query, failure_parameters)
        ]

        item_query = """
            SELECT
                item.id AS item_row_id,
                item.source_id,
                item.source_item_id,
                item.video_id,
                item.source_title,
                item.availability_status,
                item.first_seen_at,
                item.last_seen_at,
                item.updated_at,
                item.last_error,
                source.external_id,
                source.remote_title
            FROM sync_source_items AS item
            JOIN sync_sources AS source ON source.id=item.source_id
            WHERE item.removed_at IS NULL
              AND NULLIF(TRIM(item.last_error), '') IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM sync_failures AS failure
                  WHERE failure.status='unresolved'
                    AND failure.sync_source_id=item.source_id
                    AND (
                        (
                            NULLIF(TRIM(failure.source_item_id), '') IS NOT NULL
                            AND failure.source_item_id=item.source_item_id
                        )
                        OR (
                            NULLIF(TRIM(failure.source_item_id), '') IS NULL
                            AND NULLIF(TRIM(failure.video_id), '') IS NOT NULL
                            AND NULLIF(TRIM(item.video_id), '') IS NOT NULL
                            AND failure.video_id=item.video_id
                        )
                    )
              )
        """
        item_parameters: tuple[object, ...] = ()
        if source_id is not None:
            item_query += " AND item.source_id=?"
            item_parameters = (int(source_id),)

        occurrence_only: list[dict[str, object]] = []
        for row in self.conn.execute(item_query, item_parameters):
            occurrence_only.append(
                {
                    "id": -int(row["item_row_id"]),
                    "playlist_id": row["external_id"],
                    "playlist_title": row["remote_title"],
                    "video_id": row["video_id"],
                    "title": row["source_title"] or "Unavailable source item",
                    "reason": row["last_error"],
                    "error_category": row["availability_status"] or "unavailable",
                    "attempt_count": 1,
                    "first_attempt_at": row["first_seen_at"],
                    "last_attempt_at": row["updated_at"] or row["last_seen_at"],
                    "status": "unresolved",
                    "resolved_at": None,
                    "sync_source_id": int(row["source_id"]),
                    "source_item_id": row["source_item_id"],
                    "failure_origin": "source_item",
                }
            )

        combined = [*structured, *occurrence_only]
        combined.sort(
            key=lambda failure: (
                str(failure.get("last_attempt_at") or ""),
                int(failure.get("id") or 0),
            ),
            reverse=True,
        )
        return combined

    def clear_failure_history(self, source_id: int) -> None:
        self.db.clear_failure_history(int(source_id))

    def recent_runs(self, source_id: int, *, limit: int = 25) -> list[sqlite3.Row]:
        bounded = max(1, min(int(limit), 100))
        return list(
            self.conn.execute(
                "SELECT * FROM sync_source_runs WHERE source_id=? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (int(source_id), bounded),
            )
        )

    def identity_conflict_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM source_identity_conflicts WHERE resolved_at IS NULL"
        ).fetchone()
        return int(row[0]) if row else 0
