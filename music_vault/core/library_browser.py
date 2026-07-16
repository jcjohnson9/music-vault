from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping, Sequence


def normalize_identity(value: object) -> str:
    """Return the conservative identity used by the media browsers.

    Identity normalization intentionally does no credit splitting or fuzzy
    matching. Unicode presentation, meaningless whitespace, and case are the
    only distinctions that are ignored.
    """

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    return " ".join(normalized.split()).casefold()


def _stable_browser_key(kind: str, parts: Sequence[str]) -> str:
    payload = json.dumps(
        [kind, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{kind}:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class AlbumKey:
    title_key: str
    artist_key: str
    year_key: str = ""

    @property
    def browser_key(self) -> str:
        return _stable_browser_key(
            "album",
            (self.title_key, self.artist_key, self.year_key),
        )


@dataclass(frozen=True, slots=True)
class ArtistKey:
    normalized_name: str
    artist_id: int | None = None
    provider_identity: str = ""
    identity_key: str = ""

    @property
    def browser_key(self) -> str:
        if self.artist_id is not None:
            return _stable_browser_key("artist", ("artist_id", str(self.artist_id)))
        if self.identity_key:
            return _stable_browser_key("artist", (self.identity_key,))
        return _stable_browser_key("artist", (self.normalized_name,))


@dataclass(frozen=True, slots=True)
class AlbumSummary:
    key: AlbumKey
    album_title: str
    album_artist: str
    canonical_year: str | None
    track_count: int
    representative_cover_path: str | None

    @property
    def browser_key(self) -> str:
        return self.key.browser_key

    @property
    def sort_value(self) -> tuple[str, str, str]:
        return (self.key.title_key, self.key.artist_key, self.key.year_key)


@dataclass(frozen=True, slots=True)
class ArtistSummary:
    key: ArtistKey
    display_name: str
    track_count: int
    image_state: str = "not_cached"
    featured_track_count: int = 0
    collaboration_track_count: int = 0
    entity_type: str = "unknown"

    @property
    def browser_key(self) -> str:
        return self.key.browser_key

    @property
    def normalized_identity_key(self) -> str:
        return self.key.normalized_name

    @property
    def sort_value(self) -> str:
        return self.key.normalized_name

    @property
    def primary_track_count(self) -> int:
        """Return the ordinary-track count without breaking the legacy API."""

        return self.track_count

    @property
    def featured_on_count(self) -> int:
        return self.featured_track_count

    @property
    def collaboration_count(self) -> int:
        return self.collaboration_track_count


@dataclass(frozen=True, slots=True)
class ArtistTrackSections:
    """Tracks associated with one artist, separated by structured credit role."""

    tracks: tuple[sqlite3.Row, ...] = ()
    featured_on: tuple[sqlite3.Row, ...] = ()
    collaborations: tuple[sqlite3.Row, ...] = ()

    @property
    def primary_tracks(self) -> tuple[sqlite3.Row, ...]:
        return self.tracks

    @property
    def featured_tracks(self) -> tuple[sqlite3.Row, ...]:
        return self.featured_on

    @property
    def collaboration_tracks(self) -> tuple[sqlite3.Row, ...]:
        return self.collaborations


@dataclass(frozen=True, slots=True)
class BrowserRevision:
    track_count: int
    max_track_id: int
    max_updated_at: str
    artwork_count: int
    artist_count: int = 0
    artist_credit_count: int = 0
    max_artist_updated_at: str = ""
    max_artist_credit_updated_at: str = ""


class BrowserKind(str, Enum):
    ALBUMS = "albums"
    ARTISTS = "artists"


class BrowserInvalidationReason(str, Enum):
    IMPORT_FOLDER = "import_folder"
    YOUTUBE_IMPORT = "youtube_import"
    REMOVE_MISSING = "remove_missing"
    METADATA_ENRICHMENT = "metadata_enrichment"
    ARTWORK_REFRESH = "artwork_refresh"
    FUTURE_METADATA = "future_metadata"
    ARTIST_IMAGE_CACHE = "artist_image_cache"


@dataclass(frozen=True, slots=True)
class BrowserInvalidationPlan:
    album_summaries: bool = False
    artist_summaries: bool = False
    album_thumbnails: bool = False
    artist_thumbnails: bool = False


@dataclass(frozen=True, slots=True)
class BrowserCacheToken:
    kind: BrowserKind
    revision: BrowserRevision
    generation: int


@dataclass(frozen=True, slots=True)
class BrowserCacheStats:
    hits: int
    misses: int


BrowserSummary = AlbumSummary | ArtistSummary
BrowserSummaryItems = tuple[BrowserSummary, ...]


_TRACK_SELECT = """
    id, title, artist, album, album_artist, year, path, cover_path,
    duration_seconds, created_at, source_kind, source_video_id,
    source_upload_date
"""

_ALBUM_SUMMARY_SQL = """
WITH normalized AS (
    SELECT
        id,
        COALESCE(NULLIF(TRIM(album), ''), 'Unknown Album') AS album_title,
        COALESCE(
            NULLIF(TRIM(album_artist), ''),
            NULLIF(TRIM(artist), ''),
            'Unknown Artist'
        ) AS album_artist_name,
        mv_normalize_identity(album) AS title_key,
        COALESCE(
            NULLIF(mv_normalize_identity(album_artist), ''),
            NULLIF(mv_normalize_identity(artist), ''),
            ''
        ) AS artist_key,
        COALESCE(NULLIF(TRIM(year), ''), '') AS year_key,
        CASE
            WHEN cover_path IS NOT NULL AND TRIM(cover_path) <> ''
            THEN cover_path
        END AS usable_cover_path
    FROM tracks
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY title_key, artist_key, year_key
            ORDER BY id
        ) AS identity_rank,
        ROW_NUMBER() OVER (
            PARTITION BY title_key, artist_key, year_key
            ORDER BY
                CASE WHEN usable_cover_path IS NULL THEN 1 ELSE 0 END,
                id
        ) AS cover_rank
    FROM normalized
)
SELECT
    title_key,
    artist_key,
    year_key,
    MAX(CASE WHEN identity_rank = 1 THEN album_title END) AS album_title,
    MAX(CASE WHEN identity_rank = 1 THEN album_artist_name END) AS album_artist,
    NULLIF(year_key, '') AS canonical_year,
    COUNT(*) AS track_count,
    MAX(CASE WHEN cover_rank = 1 THEN usable_cover_path END) AS cover_path
FROM ranked
GROUP BY title_key, artist_key, year_key
ORDER BY
    CASE WHEN title_key = '' THEN 1 ELSE 0 END,
    title_key,
    artist_key,
    year_key
"""

_LEGACY_ARTIST_SUMMARY_SQL = """
WITH normalized AS (
    SELECT
        id,
        COALESCE(NULLIF(TRIM(artist), ''), 'Unknown Artist') AS display_name,
        mv_normalize_identity(artist) AS normalized_key
    FROM tracks
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY normalized_key
            ORDER BY id
        ) AS identity_rank
    FROM normalized
)
SELECT
    normalized_key,
    MAX(CASE WHEN identity_rank = 1 THEN display_name END) AS display_name,
    COUNT(*) AS track_count
FROM ranked
GROUP BY normalized_key
ORDER BY
    CASE WHEN normalized_key = '' THEN 1 ELSE 0 END,
    normalized_key
"""

_ARTIST_SUMMARY_V6_SQL = """
WITH structured_credits AS (
    SELECT
        a.id AS artist_id,
        a.display_name,
        a.normalized_name AS normalized_key,
        a.entity_type,
        a.discogs_artist_id,
        a.musicbrainz_artist_id,
        tac.track_id,
        tac.role,
        0 AS source_rank,
        'artist:' || CAST(a.id AS TEXT) AS identity_key
    FROM artists AS a
    JOIN track_artist_credits AS tac ON tac.artist_id = a.id
    WHERE tac.role IN ('primary', 'featured', 'collaborator')
),
legacy_fallback AS (
    SELECT
        NULL AS artist_id,
        COALESCE(NULLIF(TRIM(t.artist), ''), 'Unknown Artist') AS display_name,
        mv_normalize_identity(t.artist) AS normalized_key,
        'unknown' AS entity_type,
        NULL AS discogs_artist_id,
        NULL AS musicbrainz_artist_id,
        t.id AS track_id,
        'primary' AS role,
        1 AS source_rank,
        'legacy:' || mv_normalize_identity(t.artist) AS identity_key
    FROM tracks AS t
    WHERE NOT EXISTS (
        SELECT 1
        FROM track_artist_credits AS existing_credit
        WHERE existing_credit.track_id = t.id
    )
),
combined AS (
    SELECT * FROM structured_credits
    UNION ALL
    SELECT * FROM legacy_fallback
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY identity_key
            ORDER BY
                source_rank,
                CASE WHEN artist_id IS NULL THEN 1 ELSE 0 END,
                artist_id,
                track_id
        ) AS identity_rank
    FROM combined
)
SELECT
    identity_key,
    MAX(CASE WHEN identity_rank = 1 THEN artist_id END) AS artist_id,
    MAX(CASE WHEN identity_rank = 1 THEN normalized_key END) AS normalized_key,
    MAX(CASE WHEN identity_rank = 1 THEN display_name END) AS display_name,
    MAX(CASE WHEN identity_rank = 1 THEN entity_type END) AS entity_type,
    MAX(CASE WHEN identity_rank = 1 THEN discogs_artist_id END)
        AS discogs_artist_id,
    MAX(CASE WHEN identity_rank = 1 THEN musicbrainz_artist_id END)
        AS musicbrainz_artist_id,
    COUNT(DISTINCT CASE WHEN role = 'primary' THEN track_id END) AS track_count,
    COUNT(DISTINCT CASE WHEN role = 'featured' THEN track_id END)
        AS featured_track_count,
    COUNT(DISTINCT CASE WHEN role = 'collaborator' THEN track_id END)
        AS collaboration_track_count
FROM ranked
GROUP BY identity_key
ORDER BY
    CASE WHEN normalized_key = '' THEN 1 ELSE 0 END,
    normalized_key,
    identity_key
"""

_ARTIST_TRACKS_V6_SQL = """
WITH resolved_artist AS (
    SELECT id
    FROM artists
    WHERE ? = 1
      AND ((? IS NOT NULL AND id = ?)
       OR (? IS NULL AND normalized_name = ?))
    ORDER BY
        CASE
            WHEN NULLIF(TRIM(discogs_artist_id), '') IS NULL
             AND NULLIF(TRIM(musicbrainz_artist_id), '') IS NULL
            THEN 0 ELSE 1
        END,
        id
    LIMIT 1
),
structured_tracks AS (
    SELECT tac.track_id, tac.role
    FROM resolved_artist AS a
    JOIN track_artist_credits AS tac ON tac.artist_id = a.id
    WHERE tac.role IN ('primary', 'featured', 'collaborator')
),
legacy_fallback AS (
    SELECT t.id AS track_id, 'primary' AS role
    FROM tracks AS t
    WHERE ? IS NULL
      AND mv_normalize_identity(t.artist) = ?
      AND NOT EXISTS (
          SELECT 1
          FROM track_artist_credits AS existing_credit
          WHERE existing_credit.track_id = t.id
      )
),
artist_tracks AS (
    SELECT track_id, role FROM structured_tracks
    UNION
    SELECT track_id, role FROM legacy_fallback
)
SELECT
    t.id, t.title, t.artist, t.album, t.album_artist, t.year, t.path,
    t.cover_path, t.duration_seconds, t.created_at, t.source_kind,
    t.source_video_id, t.source_upload_date,
    artist_tracks.role AS artist_browser_role
FROM artist_tracks
JOIN tracks AS t ON t.id = artist_tracks.track_id
ORDER BY
    CASE artist_tracks.role
        WHEN 'primary' THEN 0
        WHEN 'featured' THEN 1
        WHEN 'collaborator' THEN 2
        ELSE 3
    END,
    t.album COLLATE NOCASE,
    t.title COLLATE NOCASE,
    t.id
"""


def configure_browser_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.create_function(
        "mv_normalize_identity",
        1,
        normalize_identity,
        deterministic=True,
    )
    return conn


def _has_structured_artist_schema(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ('artists', 'track_artist_credits')
        """
    ).fetchall()
    return {str(row[0]) for row in rows} == {"artists", "track_artist_credits"}


@contextmanager
def open_readonly_database(
    db_path: str | Path,
    *,
    timeout: float = 5.0,
) -> Iterator[sqlite3.Connection]:
    """Open one short-lived, query-only connection suitable for a worker."""

    path = Path(db_path).expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        configure_browser_connection(conn)
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def browser_revision(conn: sqlite3.Connection) -> BrowserRevision:
    configure_browser_connection(conn)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS track_count,
            COALESCE(MAX(id), 0) AS max_track_id,
            COALESCE(MAX(metadata_updated_at), '') AS max_updated_at,
            COUNT(CASE
                WHEN cover_path IS NOT NULL AND TRIM(cover_path) <> '' THEN 1
            END) AS artwork_count
        FROM tracks
        """
    ).fetchone()
    artist_count = 0
    artist_credit_count = 0
    max_artist_updated_at = ""
    max_artist_credit_updated_at = ""
    if _has_structured_artist_schema(conn):
        artist_row = conn.execute(
            """
            SELECT
                COUNT(*) AS artist_count,
                COALESCE(MAX(updated_at), '') AS max_artist_updated_at
            FROM artists
            """
        ).fetchone()
        credit_row = conn.execute(
            """
            SELECT
                COUNT(*) AS artist_credit_count,
                COALESCE(MAX(updated_at), '') AS max_artist_credit_updated_at
            FROM track_artist_credits
            """
        ).fetchone()
        artist_count = int(artist_row["artist_count"])
        artist_credit_count = int(credit_row["artist_credit_count"])
        max_artist_updated_at = str(artist_row["max_artist_updated_at"])
        max_artist_credit_updated_at = str(
            credit_row["max_artist_credit_updated_at"]
        )
    return BrowserRevision(
        track_count=int(row["track_count"]),
        max_track_id=int(row["max_track_id"]),
        max_updated_at=str(row["max_updated_at"]),
        artwork_count=int(row["artwork_count"]),
        artist_count=artist_count,
        artist_credit_count=artist_credit_count,
        max_artist_updated_at=max_artist_updated_at,
        max_artist_credit_updated_at=max_artist_credit_updated_at,
    )


def query_album_summaries(conn: sqlite3.Connection) -> tuple[AlbumSummary, ...]:
    configure_browser_connection(conn)
    rows = conn.execute(_ALBUM_SUMMARY_SQL).fetchall()
    return tuple(
        AlbumSummary(
            key=AlbumKey(
                title_key=str(row["title_key"]),
                artist_key=str(row["artist_key"]),
                year_key=str(row["year_key"]),
            ),
            album_title=str(row["album_title"]),
            album_artist=str(row["album_artist"]),
            canonical_year=(
                str(row["canonical_year"])
                if row["canonical_year"] is not None
                else None
            ),
            track_count=int(row["track_count"]),
            representative_cover_path=(
                str(row["cover_path"]) if row["cover_path"] is not None else None
            ),
        )
        for row in rows
    )


def query_artist_summaries(
    conn: sqlite3.Connection,
    *,
    image_states: Mapping[str, str] | None = None,
) -> tuple[ArtistSummary, ...]:
    configure_browser_connection(conn)
    structured = _has_structured_artist_schema(conn)
    rows = conn.execute(
        _ARTIST_SUMMARY_V6_SQL if structured else _LEGACY_ARTIST_SUMMARY_SQL
    ).fetchall()
    states = image_states or {}
    summaries: list[ArtistSummary] = []
    structured_name_counts: dict[str, int] = {}
    if structured:
        for row in rows:
            if row["artist_id"] is None:
                continue
            name = str(row["normalized_key"])
            structured_name_counts[name] = structured_name_counts.get(name, 0) + 1
    for row in rows:
        discogs_id = (
            str(row["discogs_artist_id"])
            if structured and row["discogs_artist_id"] not in (None, "")
            else ""
        )
        musicbrainz_id = (
            str(row["musicbrainz_artist_id"])
            if structured and row["musicbrainz_artist_id"] not in (None, "")
            else ""
        )
        provider_identity = (
            f"discogs:{discogs_id}"
            if discogs_id
            else (f"musicbrainz:{musicbrainz_id}" if musicbrainz_id else "")
        )
        normalized_name = str(row["normalized_key"])
        stored_artist_id = (
            int(row["artist_id"])
            if structured and row["artist_id"] is not None
            else None
        )
        artist_id = stored_artist_id if (
            stored_artist_id is not None
            and (
                bool(provider_identity)
                or structured_name_counts.get(normalized_name, 0) > 1
            )
        ) else None
        identity_key = (
            str(row["identity_key"])
            if structured
            and stored_artist_id is None
            and normalized_name in structured_name_counts
            else ""
        )
        key = ArtistKey(
            normalized_name,
            artist_id=artist_id,
            provider_identity=provider_identity,
            identity_key=identity_key,
        )
        state = states.get(key.browser_key, states.get(key.normalized_name, "not_cached"))
        summaries.append(
            ArtistSummary(
                key=key,
                display_name=str(row["display_name"]),
                track_count=int(row["track_count"]),
                image_state=str(state),
                featured_track_count=(
                    int(row["featured_track_count"]) if structured else 0
                ),
                collaboration_track_count=(
                    int(row["collaboration_track_count"]) if structured else 0
                ),
                entity_type=(str(row["entity_type"]) if structured else "unknown"),
            )
        )
    return tuple(summaries)


def query_album_tracks(
    conn: sqlite3.Connection,
    key: AlbumKey,
) -> tuple[sqlite3.Row, ...]:
    configure_browser_connection(conn)
    return tuple(
        conn.execute(
            f"""
            SELECT {_TRACK_SELECT}
            FROM tracks
            WHERE mv_normalize_identity(album) = ?
              AND COALESCE(
                    NULLIF(mv_normalize_identity(album_artist), ''),
                    NULLIF(mv_normalize_identity(artist), ''),
                    ''
                  ) = ?
              AND COALESCE(NULLIF(TRIM(year), ''), '') = ?
            ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, id
            """,
            (key.title_key, key.artist_key, key.year_key),
        ).fetchall()
    )


def query_artist_tracks(
    conn: sqlite3.Connection,
    key: ArtistKey,
) -> tuple[sqlite3.Row, ...]:
    configure_browser_connection(conn)
    if _has_structured_artist_schema(conn):
        return _query_artist_track_sections_v6(conn, key).tracks
    return tuple(
        conn.execute(
            f"""
            SELECT {_TRACK_SELECT}
            FROM tracks
            WHERE mv_normalize_identity(artist) = ?
            ORDER BY album COLLATE NOCASE, title COLLATE NOCASE, id
            """,
            (key.normalized_name,),
        ).fetchall()
    )


def _query_artist_track_sections_v6(
    conn: sqlite3.Connection,
    key: ArtistKey,
) -> ArtistTrackSections:
    rows = conn.execute(
        _ARTIST_TRACKS_V6_SQL,
        (
            int(not key.identity_key.startswith("legacy:")),
            key.artist_id,
            key.artist_id,
            key.artist_id,
            key.normalized_name,
            key.artist_id,
            key.normalized_name,
        ),
    ).fetchall()
    by_role: dict[str, list[sqlite3.Row]] = {
        "primary": [],
        "featured": [],
        "collaborator": [],
    }
    for row in rows:
        role = str(row["artist_browser_role"])
        if role in by_role:
            by_role[role].append(row)
    return ArtistTrackSections(
        tracks=tuple(by_role["primary"]),
        featured_on=tuple(by_role["featured"]),
        collaborations=tuple(by_role["collaborator"]),
    )


def query_artist_track_sections(
    conn: sqlite3.Connection,
    key: ArtistKey,
) -> ArtistTrackSections:
    """Load an artist page in one set-based query, partitioned by credit role.

    Schema-v6 credits are authoritative. Tracks with no structured credits use
    the materialized artist string as a compatibility fallback, which also
    keeps the blank-artist ``Unknown Artist`` card available. Older databases
    retain their original single-section behavior.
    """

    configure_browser_connection(conn)
    if not _has_structured_artist_schema(conn):
        return ArtistTrackSections(tracks=query_artist_tracks(conn, key))
    return _query_artist_track_sections_v6(conn, key)


def load_album_summaries(db_path: str | Path) -> tuple[AlbumSummary, ...]:
    with open_readonly_database(db_path) as conn:
        return query_album_summaries(conn)


def load_artist_summaries(
    db_path: str | Path,
    *,
    image_states: Mapping[str, str] | None = None,
) -> tuple[ArtistSummary, ...]:
    with open_readonly_database(db_path) as conn:
        return query_artist_summaries(conn, image_states=image_states)


def load_album_tracks(
    db_path: str | Path,
    key: AlbumKey,
) -> tuple[sqlite3.Row, ...]:
    with open_readonly_database(db_path) as conn:
        return query_album_tracks(conn, key)


def load_artist_tracks(
    db_path: str | Path,
    key: ArtistKey,
) -> tuple[sqlite3.Row, ...]:
    with open_readonly_database(db_path) as conn:
        return query_artist_tracks(conn, key)


def load_artist_track_sections(
    db_path: str | Path,
    key: ArtistKey,
) -> ArtistTrackSections:
    with open_readonly_database(db_path) as conn:
        return query_artist_track_sections(conn, key)


def invalidation_plan(
    reason: BrowserInvalidationReason | str,
) -> BrowserInvalidationPlan:
    reason = BrowserInvalidationReason(reason)
    if reason in {
        BrowserInvalidationReason.IMPORT_FOLDER,
        BrowserInvalidationReason.YOUTUBE_IMPORT,
        BrowserInvalidationReason.METADATA_ENRICHMENT,
        BrowserInvalidationReason.FUTURE_METADATA,
    }:
        return BrowserInvalidationPlan(True, True, True, False)
    if reason is BrowserInvalidationReason.REMOVE_MISSING:
        return BrowserInvalidationPlan(True, True, False, False)
    if reason is BrowserInvalidationReason.ARTWORK_REFRESH:
        return BrowserInvalidationPlan(True, False, True, False)
    if reason is BrowserInvalidationReason.ARTIST_IMAGE_CACHE:
        return BrowserInvalidationPlan(False, False, False, True)
    raise AssertionError(f"Unhandled browser invalidation reason: {reason}")


class BrowserSummaryCache:
    """Small revision-aware cache with stale-worker generation protection."""

    def __init__(self) -> None:
        self._entries: dict[BrowserKind, tuple[BrowserRevision, BrowserSummaryItems]] = {}
        self._generations = {kind: 0 for kind in BrowserKind}
        self._hits = 0
        self._misses = 0
        self._lock = threading.RLock()

    @staticmethod
    def _kind(value: BrowserKind | str) -> BrowserKind:
        return BrowserKind(value)

    def token(
        self,
        kind: BrowserKind | str,
        revision: BrowserRevision,
    ) -> BrowserCacheToken:
        browser_kind = self._kind(kind)
        with self._lock:
            return BrowserCacheToken(
                browser_kind,
                revision,
                self._generations[browser_kind],
            )

    def get(
        self,
        kind: BrowserKind | str,
        revision: BrowserRevision,
    ) -> BrowserSummaryItems | None:
        browser_kind = self._kind(kind)
        with self._lock:
            entry = self._entries.get(browser_kind)
            if entry is None or entry[0] != revision:
                self._misses += 1
                return None
            self._hits += 1
            return entry[1]

    def put(
        self,
        token: BrowserCacheToken,
        summaries: Sequence[BrowserSummary],
    ) -> bool:
        with self._lock:
            if token.generation != self._generations[token.kind]:
                return False
            self._entries[token.kind] = (token.revision, tuple(summaries))
            return True

    def invalidate(
        self,
        reason: BrowserInvalidationReason | str,
    ) -> BrowserInvalidationPlan:
        plan = invalidation_plan(reason)
        with self._lock:
            if plan.album_summaries:
                self._entries.pop(BrowserKind.ALBUMS, None)
                self._generations[BrowserKind.ALBUMS] += 1
            if plan.artist_summaries:
                self._entries.pop(BrowserKind.ARTISTS, None)
                self._generations[BrowserKind.ARTISTS] += 1
        return plan

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            for kind in BrowserKind:
                self._generations[kind] += 1

    @property
    def stats(self) -> BrowserCacheStats:
        with self._lock:
            return BrowserCacheStats(self._hits, self._misses)
