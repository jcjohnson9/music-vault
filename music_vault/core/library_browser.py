from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


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


_UNTRUSTED_ARTIST_PROVENANCE_EXACT = frozenset({"youtube"})
_UNTRUSTED_ARTIST_PROVENANCE_MARKERS = (
    "uploader",
    "label",
    "distributor",
    "release_company",
)


def artist_credit_is_browser_visible(
    provenance: object,
    is_manual: object = False,
    is_locked: object = False,
) -> bool:
    """Return whether stored artist evidence may create a performer card.

    A raw YouTube import records its channel/label display as provenance
    ``youtube``.  That is useful source metadata, but it is not accepted
    performer identity.  Accepted title parsing, provider evidence, embedded
    tags, and manual/locked corrections use distinct provenance and remain
    visible.  Keeping this decision provenance-based avoids guessing from
    channel-name suffixes and preserves the truly blank Unknown Artist card.
    """

    if bool(is_manual) or bool(is_locked):
        return True
    normalized = str(provenance or "").strip().casefold()
    if normalized in _UNTRUSTED_ARTIST_PROVENANCE_EXACT:
        return False
    return not any(
        marker in normalized for marker in _UNTRUSTED_ARTIST_PROVENANCE_MARKERS
    )


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
    canonical_album_id: int | None = None
    virtual_kind: str = ""

    @property
    def browser_key(self) -> str:
        if self.virtual_kind:
            return _stable_browser_key("album", ("virtual", self.virtual_kind))
        if self.canonical_album_id is not None:
            return _stable_browser_key(
                "album", ("canonical_album_id", str(self.canonical_album_id))
            )
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
    cluster_artist_ids: tuple[int, ...] = field(default=(), compare=False)

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
    edition_count: int = 1
    album_kind: str = "unknown"

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
    group_appearance_track_count: int = 0
    entity_type: str = "unknown"
    canonical_artist_id: int | None = None
    discogs_artist_id: str | None = None
    musicbrainz_artist_id: str | None = None
    image_identity_name: str = ""
    historical_aliases: tuple[str, ...] = ()
    allow_normalized_name_cache: bool = False
    allow_historical_alias_cache: bool = False

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

    @property
    def group_appearance_count(self) -> int:
        return self.group_appearance_track_count


@dataclass(frozen=True, slots=True)
class ArtistTrackSections:
    """Tracks associated with one artist, separated by structured credit role."""

    tracks: tuple[sqlite3.Row, ...] = ()
    featured_on: tuple[sqlite3.Row, ...] = ()
    collaborations: tuple[sqlite3.Row, ...] = ()
    group_appearances: tuple[sqlite3.Row, ...] = ()

    @property
    def primary_tracks(self) -> tuple[sqlite3.Row, ...]:
        return self.tracks

    @property
    def featured_tracks(self) -> tuple[sqlite3.Row, ...]:
        return self.featured_on

    @property
    def collaboration_tracks(self) -> tuple[sqlite3.Row, ...]:
        return self.collaborations

    @property
    def group_tracks(self) -> tuple[sqlite3.Row, ...]:
        return self.group_appearances


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
    artist_alias_count: int = 0
    artist_relationship_count: int = 0
    max_artist_relationship_updated_at: str = ""
    canonical_album_count: int = 0
    album_membership_count: int = 0
    max_canonical_album_updated_at: str = ""
    max_album_membership_updated_at: str = ""


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
    CANONICAL_CONSOLIDATION = "canonical_consolidation"


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

_QUALIFIED_TRACK_SELECT = """
    tracks.id, tracks.title, tracks.artist, tracks.album, tracks.album_artist,
    tracks.year, tracks.path, tracks.cover_path, tracks.duration_seconds,
    tracks.created_at, tracks.source_kind, tracks.source_video_id,
    tracks.source_upload_date
"""

_SINGLES_UNCATALOGUED_KIND = "singles_uncatalogued"
_SINGLES_UNCATALOGUED_TITLE = "Singles & Uncatalogued"

_ALBUM_SUMMARY_SQL = """
WITH normalized AS (
    SELECT
        id,
        CASE
            WHEN mv_is_uncatalogued_album(album) = 1
            THEN 'Singles & Uncatalogued'
            ELSE TRIM(album)
        END AS album_title,
        CASE
            WHEN mv_is_uncatalogued_album(album) = 1 THEN ''
            ELSE COALESCE(
                NULLIF(TRIM(album_artist), ''),
                NULLIF(TRIM(artist), ''),
                'Unknown Artist'
            )
        END AS album_artist_name,
        CASE
            WHEN mv_is_uncatalogued_album(album) = 1 THEN ''
            ELSE mv_normalize_identity(album)
        END AS title_key,
        CASE
            WHEN mv_is_uncatalogued_album(album) = 1 THEN ''
            ELSE COALESCE(
                NULLIF(mv_normalize_identity(album_artist), ''),
                NULLIF(mv_normalize_identity(artist), ''),
                ''
            )
        END AS artist_key,
        CASE
            WHEN mv_is_uncatalogued_album(album) = 1 THEN ''
            ELSE COALESCE(NULLIF(TRIM(year), ''), '')
        END AS year_key,
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

_UNMAPPED_ALBUM_SUMMARY_SQL = _ALBUM_SUMMARY_SQL.replace(
    "    FROM tracks\n",
    """    FROM tracks
    WHERE NOT EXISTS (
        SELECT 1 FROM track_album_memberships membership
        WHERE membership.track_id=tracks.id
    )
""",
    1,
)

_CANONICAL_ALBUM_SUMMARY_SQL = """
SELECT
    album.id AS canonical_album_id,
    album.canonical_key,
    album.normalized_title AS title_key,
    album.normalized_album_artist AS artist_key,
    album.title AS album_title,
    album.album_artist_display AS album_artist,
    CASE
        WHEN album.original_release_date GLOB '[0-9][0-9][0-9][0-9]*'
        THEN SUBSTR(album.original_release_date, 1, 4)
    END AS canonical_year,
    album.album_kind,
    COUNT(DISTINCT membership.track_id) AS track_count,
    COUNT(DISTINCT COALESCE(
        NULLIF(TRIM(membership.discogs_release_id), ''),
        CASE
            WHEN NULLIF(TRIM(membership.edition_label), '') IS NOT NULL
              OR NULLIF(TRIM(membership.edition_release_date), '') IS NOT NULL
            THEN COALESCE(NULLIF(TRIM(membership.edition_label), ''), '')
                 || char(31) ||
                 COALESCE(
                     NULLIF(TRIM(membership.edition_release_date), ''),
                     ''
                 )
        END,
        'base'
    )) AS edition_count
FROM canonical_albums AS album
JOIN track_album_memberships AS membership
  ON membership.canonical_album_id=album.id
GROUP BY album.id
ORDER BY album.normalized_title, album.normalized_album_artist, album.id
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
      AND mv_is_various_artists(a.display_name) = 0
      AND mv_artist_credit_visible(
          tac.provenance, tac.is_manual, tac.is_locked
      ) = 1
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
    LEFT JOIN track_metadata_fields AS artist_field
      ON artist_field.track_id=t.id AND artist_field.field_name='artist'
    WHERE NOT EXISTS (
        SELECT 1
        FROM track_artist_credits AS existing_credit
        WHERE existing_credit.track_id = t.id
    )
      AND mv_is_various_artists(t.artist) = 0
      AND mv_artist_credit_visible(
          artist_field.provenance,
          COALESCE(artist_field.is_manual, 0),
          COALESCE(artist_field.is_locked, 0)
      ) = 1
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

_ARTIST_SUMMARY_V7_SQL = """
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
      AND mv_is_various_artists(a.display_name) = 0
      AND mv_artist_credit_visible(
          tac.provenance, tac.is_manual, tac.is_locked
      ) = 1
),
group_credits AS (
    SELECT
        member.id AS artist_id,
        member.display_name,
        member.normalized_name AS normalized_key,
        member.entity_type,
        member.discogs_artist_id,
        member.musicbrainz_artist_id,
        group_credit.track_id,
        'group_appearance' AS role,
        0 AS source_rank,
        'artist:' || CAST(member.id AS TEXT) AS identity_key
    FROM artist_relationships AS relation
    JOIN artists AS member ON member.id=relation.subject_artist_id
    JOIN track_artist_credits AS group_credit
      ON group_credit.artist_id=relation.related_artist_id
     AND group_credit.role='primary'
    WHERE relation.relationship_kind='member_of'
      AND mv_is_various_artists(member.display_name) = 0
      AND mv_artist_credit_visible(
          group_credit.provenance,
          group_credit.is_manual,
          group_credit.is_locked
      ) = 1
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
    LEFT JOIN track_metadata_fields AS artist_field
      ON artist_field.track_id=t.id AND artist_field.field_name='artist'
    WHERE NOT EXISTS (
        SELECT 1
        FROM track_artist_credits AS existing_credit
        WHERE existing_credit.track_id = t.id
    )
      AND mv_is_various_artists(t.artist) = 0
      AND mv_artist_credit_visible(
          artist_field.provenance,
          COALESCE(artist_field.is_manual, 0),
          COALESCE(artist_field.is_locked, 0)
      ) = 1
),
combined AS (
    SELECT * FROM structured_credits
    UNION ALL
    SELECT * FROM group_credits
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
        AS collaboration_track_count,
    COUNT(DISTINCT CASE WHEN role = 'group_appearance' THEN track_id END)
        AS group_appearance_track_count
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
      AND mv_artist_credit_visible(
          tac.provenance, tac.is_manual, tac.is_locked
      ) = 1
),
legacy_fallback AS (
    SELECT t.id AS track_id, 'primary' AS role
    FROM tracks AS t
    LEFT JOIN track_metadata_fields AS artist_field
      ON artist_field.track_id=t.id AND artist_field.field_name='artist'
    WHERE ? IS NULL
      AND mv_normalize_identity(t.artist) = ?
      AND NOT EXISTS (
          SELECT 1
          FROM track_artist_credits AS existing_credit
          WHERE existing_credit.track_id = t.id
      )
      AND mv_artist_credit_visible(
          artist_field.provenance,
          COALESCE(artist_field.is_manual, 0),
          COALESCE(artist_field.is_locked, 0)
      ) = 1
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

_ARTIST_TRACKS_V7_SQL = """
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
      AND mv_artist_credit_visible(
          tac.provenance, tac.is_manual, tac.is_locked
      ) = 1
),
group_tracks AS (
    SELECT group_credit.track_id, 'group_appearance' AS role
    FROM resolved_artist AS a
    JOIN artist_relationships AS relation
      ON relation.subject_artist_id=a.id
     AND relation.relationship_kind='member_of'
    JOIN track_artist_credits AS group_credit
      ON group_credit.artist_id=relation.related_artist_id
     AND group_credit.role='primary'
    WHERE mv_artist_credit_visible(
        group_credit.provenance,
        group_credit.is_manual,
        group_credit.is_locked
    ) = 1
),
legacy_fallback AS (
    SELECT t.id AS track_id, 'primary' AS role
    FROM tracks AS t
    LEFT JOIN track_metadata_fields AS artist_field
      ON artist_field.track_id=t.id AND artist_field.field_name='artist'
    WHERE ? IS NULL
      AND mv_normalize_identity(t.artist) = ?
      AND NOT EXISTS (
          SELECT 1
          FROM track_artist_credits AS existing_credit
          WHERE existing_credit.track_id = t.id
      )
      AND mv_is_various_artists(t.artist) = 0
      AND mv_artist_credit_visible(
          artist_field.provenance,
          COALESCE(artist_field.is_manual, 0),
          COALESCE(artist_field.is_locked, 0)
      ) = 1
),
artist_tracks AS (
    SELECT track_id, role FROM structured_tracks
    UNION
    SELECT track_id, role FROM group_tracks
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
        WHEN 'group_appearance' THEN 3
        ELSE 4
    END,
    t.album COLLATE NOCASE,
    t.title COLLATE NOCASE,
    t.id
"""


def configure_browser_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    from music_vault.metadata.canonical_albums import is_uncatalogued_album
    from music_vault.metadata.soundtrack import is_various_artists

    conn.row_factory = sqlite3.Row
    conn.create_function(
        "mv_normalize_identity",
        1,
        normalize_identity,
        deterministic=True,
    )
    conn.create_function(
        "mv_is_various_artists",
        1,
        lambda value: int(is_various_artists(value)),
        deterministic=True,
    )
    conn.create_function(
        "mv_is_uncatalogued_album",
        1,
        lambda value: int(is_uncatalogued_album(value)),
        deterministic=True,
    )
    conn.create_function(
        "mv_artist_credit_visible",
        3,
        lambda provenance, is_manual, is_locked: int(
            artist_credit_is_browser_visible(
                provenance,
                is_manual,
                is_locked,
            )
        ),
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


def _has_canonical_artist_schema(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN (
              'artists', 'track_artist_credits',
              'artist_aliases', 'artist_relationships'
          )
        """
    ).fetchall()
    return {str(row[0]) for row in rows} == {
        "artists",
        "track_artist_credits",
        "artist_aliases",
        "artist_relationships",
    }


def _has_canonical_album_schema(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table'
          AND name IN ('canonical_albums', 'track_album_memberships')
        """
    ).fetchall()
    return {str(row[0]) for row in rows} == {
        "canonical_albums",
        "track_album_memberships",
    }


_ARTIST_PRESENTATION_RE = re.compile(r"[\s\-_.\u2010-\u2015'\u2018\u2019]+")
_GROUP_ARTIST_TYPES = frozenset({"group", "band", "duo", "orchestra", "collective"})
_PERSON_ARTIST_TYPES = frozenset({"person"})


def _artist_presentation_key(value: object) -> str:
    """Normalize presentation without treating credit punctuation as separators."""

    display = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(_ARTIST_PRESENTATION_RE.sub(" ", display).split())


def _clean_provider_identity(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _artist_rows_for_clustering(
    conn: sqlite3.Connection,
    artist_ids: Iterable[int] | None = None,
) -> tuple[sqlite3.Row, ...]:
    ids = tuple(dict.fromkeys(int(value) for value in (artist_ids or ())))
    where = ""
    parameters: tuple[object, ...] = ()
    if artist_ids is not None:
        if not ids:
            return ()
        where = f"WHERE artist.id IN ({','.join('?' for _ in ids)})"
        parameters = ids
    return tuple(
        conn.execute(
            f"""
            SELECT artist.*,
                   COUNT(DISTINCT CASE WHEN credit.role='primary'
                                       THEN credit.track_id END) AS primary_usage
            FROM artists AS artist
            LEFT JOIN track_artist_credits AS credit ON credit.artist_id=artist.id
            {where}
            GROUP BY artist.id
            ORDER BY artist.id
            """,
            parameters,
        ).fetchall()
    )


def _artist_clusters(
    conn: sqlite3.Connection,
    artist_ids: Iterable[int] | None = None,
) -> tuple[tuple[sqlite3.Row, ...], ...]:
    """Return deterministic, conflict-aware canonical browser clusters.

    Same-name name-only rows and complementary provider rows belong together.
    A conflicting ID from the *same* provider is a hard boundary.  Greedy,
    conflict-aware union prevents an unqualified legacy row from accidentally
    bridging two proven same-name artists.
    """

    rows = _artist_rows_for_clustering(conn, artist_ids)
    if not rows:
        return ()
    row_by_id = {int(row["id"]): row for row in rows}
    parent = {artist_id: artist_id for artist_id in row_by_id}
    members = {artist_id: {artist_id} for artist_id in row_by_id}

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def provider_values(ids: Iterable[int], column: str) -> set[str]:
        return {
            identity
            for artist_id in ids
            if (identity := _clean_provider_identity(row_by_id[artist_id][column]))
            is not None
        }

    def compatible(left_ids: set[int], right_ids: set[int]) -> bool:
        combined = left_ids | right_ids
        if len(provider_values(combined, "discogs_artist_id")) > 1:
            return False
        if len(provider_values(combined, "musicbrainz_artist_id")) > 1:
            return False
        kinds = {
            str(row_by_id[value]["entity_type"] or "unknown").casefold()
            for value in combined
        }
        return not (kinds & _GROUP_ARTIST_TYPES and kinds & _PERSON_ARTIST_TYPES)

    def union(left: int, right: int) -> None:
        first, second = find(left), find(right)
        if first == second or not compatible(members[first], members[second]):
            return
        # Keep the root stable so browser keys do not depend on query order.
        target, source = (first, second) if first < second else (second, first)
        parent[source] = target
        members[target].update(members.pop(source))

    edges: set[tuple[int, int, int]] = set()

    def connect_group(values: Iterable[int], strength: int) -> None:
        ordered = sorted(set(int(value) for value in values))
        if len(ordered) < 2:
            return
        # One deterministic star is sufficient for union-find connectivity.
        # Pairing every same-presentation row with every other row made a
        # pathological legacy duplicate group quadratic without changing the
        # conflict-aware result.
        anchor = ordered[0]
        edges.update((strength, anchor, value) for value in ordered[1:])

    for column in ("discogs_artist_id", "musicbrainz_artist_id"):
        identities: dict[str, list[int]] = {}
        for row in rows:
            identity = _clean_provider_identity(row[column])
            if identity:
                identities.setdefault(identity.casefold(), []).append(int(row["id"]))
        for values in identities.values():
            connect_group(values, 0)

    presentations: dict[str, list[int]] = {}
    normalized_names: dict[str, list[int]] = {}
    for row in rows:
        artist_id = int(row["id"])
        presentations.setdefault(
            _artist_presentation_key(row["display_name"]), []
        ).append(artist_id)
        normalized_names.setdefault(str(row["normalized_name"]), []).append(artist_id)
    for values in presentations.values():
        group_ids = sorted(set(values))
        discogs_values = provider_values(group_ids, "discogs_artist_id")
        musicbrainz_values = provider_values(group_ids, "musicbrainz_artist_id")
        kinds = {
            str(row_by_id[value]["entity_type"] or "unknown").casefold()
            for value in group_ids
        }
        proven_conflict = bool(
            len(discogs_values) > 1
            or len(musicbrainz_values) > 1
            or (kinds & _GROUP_ARTIST_TYPES and kinds & _PERSON_ARTIST_TYPES)
        )
        if proven_conflict:
            # Never let an unqualified structured row bridge two proven
            # same-name identities.  Keep all such legacy rows together as a
            # visibly disambiguated, unassigned cluster.
            unqualified = [
                value
                for value in group_ids
                if not _clean_provider_identity(row_by_id[value]["discogs_artist_id"])
                and not _clean_provider_identity(
                    row_by_id[value]["musicbrainz_artist_id"]
                )
            ]
            connect_group(unqualified, 2)
        else:
            connect_group(group_ids, 2)

    if _has_canonical_artist_schema(conn):
        aliases = conn.execute(
            "SELECT artist_id,normalized_alias FROM artist_aliases ORDER BY id"
        ).fetchall()
        for alias in aliases:
            owner = int(alias["artist_id"])
            if owner not in row_by_id:
                continue
            for candidate in normalized_names.get(str(alias["normalized_alias"]), ()):
                if candidate != owner:
                    edges.add((1, min(owner, candidate), max(owner, candidate)))

    # Strong provider/alias evidence is considered before presentation-only
    # evidence.  Within one class the stable IDs make the result deterministic.
    for _strength, left, right in sorted(edges):
        union(left, right)

    grouped: dict[int, list[sqlite3.Row]] = {}
    for artist_id, row in row_by_id.items():
        grouped.setdefault(find(artist_id), []).append(row)
    return tuple(
        tuple(sorted(group, key=lambda row: int(row["id"])))
        for _root, group in sorted(grouped.items())
    )


def _canonical_artist_row(rows: Sequence[sqlite3.Row]) -> sqlite3.Row:
    return min(
        rows,
        key=lambda row: (
            -int(
                bool(_clean_provider_identity(row["discogs_artist_id"]))
                and bool(_clean_provider_identity(row["musicbrainz_artist_id"]))
            ),
            -int(bool(_clean_provider_identity(row["discogs_artist_id"]))),
            -int(bool(_clean_provider_identity(row["musicbrainz_artist_id"]))),
            -int(row["primary_usage"] or 0),
            int(row["id"]),
        ),
    )


def resolve_artist_cluster_ids(
    conn: sqlite3.Connection,
    key: ArtistKey,
) -> tuple[int, ...]:
    """Resolve an artist page key to every compatible canonical entity ID."""

    if key.cluster_artist_ids:
        requested = tuple(dict.fromkeys(int(value) for value in key.cluster_artist_ids))
        if not requested:
            return ()
        existing = {
            int(row[0])
            for row in conn.execute(
                f"SELECT id FROM artists WHERE id IN ({','.join('?' for _ in requested)})",
                requested,
            ).fetchall()
        }
        return tuple(value for value in requested if value in existing)

    # An identity-key card represents a materialized legacy credit with no
    # structured owner.  It must not be captured by an arbitrary same-name
    # provider cluster when several real artists share that presentation.
    if key.identity_key and key.artist_id is None and not key.provider_identity:
        return ()

    clusters = _artist_clusters(conn)
    if key.artist_id is not None:
        for cluster in clusters:
            ids = tuple(int(row["id"]) for row in cluster)
            if int(key.artist_id) in ids:
                return ids
        return ()

    provider_kind, separator, provider_id = key.provider_identity.partition(":")
    if separator and provider_kind in {"discogs", "musicbrainz"} and provider_id:
        column = f"{provider_kind}_artist_id"
        for cluster in clusters:
            if any(str(row[column] or "") == provider_id for row in cluster):
                return tuple(int(row["id"]) for row in cluster)

    matching: list[tuple[int, ...]] = []
    for cluster in clusters:
        normalized = {str(row["normalized_name"]) for row in cluster}
        if key.normalized_name in normalized:
            matching.append(tuple(int(row["id"]) for row in cluster))
    if matching:
        return min(matching, key=lambda values: (min(values), values))

    if _has_canonical_artist_schema(conn):
        alias_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT artist_id FROM artist_aliases WHERE normalized_alias=?",
                (key.normalized_name,),
            ).fetchall()
        }
        if alias_ids:
            for cluster in clusters:
                ids = tuple(int(row["id"]) for row in cluster)
                if alias_ids.intersection(ids):
                    return ids
    return ()


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
    artist_alias_count = 0
    artist_relationship_count = 0
    max_artist_relationship_updated_at = ""
    canonical_album_count = 0
    album_membership_count = 0
    max_canonical_album_updated_at = ""
    max_album_membership_updated_at = ""
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
        if _has_canonical_artist_schema(conn):
            alias_row = conn.execute(
                "SELECT COUNT(*) AS alias_count FROM artist_aliases"
            ).fetchone()
            relationship_columns = {
                str(item[1])
                for item in conn.execute("PRAGMA table_info('artist_relationships')")
            }
            updated_expression = (
                "COALESCE(MAX(updated_at), '')"
                if "updated_at" in relationship_columns
                else "COALESCE(MAX(created_at), '')"
            )
            relationship_row = conn.execute(
                "SELECT COUNT(*) AS relationship_count, "
                + updated_expression
                + " AS max_relationship_updated_at FROM artist_relationships"
            ).fetchone()
            artist_alias_count = int(alias_row["alias_count"])
            artist_relationship_count = int(relationship_row["relationship_count"])
            max_artist_relationship_updated_at = str(
                relationship_row["max_relationship_updated_at"]
            )
    if _has_canonical_album_schema(conn):
        canonical_row = conn.execute(
            """
            SELECT COUNT(*) AS album_count,
                   COALESCE(MAX(updated_at), '') AS max_updated_at
            FROM canonical_albums
            """
        ).fetchone()
        membership_row = conn.execute(
            """
            SELECT COUNT(*) AS membership_count,
                   COALESCE(MAX(updated_at), '') AS max_updated_at
            FROM track_album_memberships
            """
        ).fetchone()
        canonical_album_count = int(canonical_row["album_count"])
        album_membership_count = int(membership_row["membership_count"])
        max_canonical_album_updated_at = str(canonical_row["max_updated_at"])
        max_album_membership_updated_at = str(membership_row["max_updated_at"])
    return BrowserRevision(
        track_count=int(row["track_count"]),
        max_track_id=int(row["max_track_id"]),
        max_updated_at=str(row["max_updated_at"]),
        artwork_count=int(row["artwork_count"]),
        artist_count=artist_count,
        artist_credit_count=artist_credit_count,
        max_artist_updated_at=max_artist_updated_at,
        max_artist_credit_updated_at=max_artist_credit_updated_at,
        artist_alias_count=artist_alias_count,
        artist_relationship_count=artist_relationship_count,
        max_artist_relationship_updated_at=max_artist_relationship_updated_at,
        canonical_album_count=canonical_album_count,
        album_membership_count=album_membership_count,
        max_canonical_album_updated_at=max_canonical_album_updated_at,
        max_album_membership_updated_at=max_album_membership_updated_at,
    )


def query_album_summaries(conn: sqlite3.Connection) -> tuple[AlbumSummary, ...]:
    configure_browser_connection(conn)
    canonical = _has_canonical_album_schema(conn)
    canonical_rows = (
        conn.execute(_CANONICAL_ALBUM_SUMMARY_SQL).fetchall() if canonical else ()
    )
    legacy_rows = conn.execute(
        _UNMAPPED_ALBUM_SUMMARY_SQL if canonical else _ALBUM_SUMMARY_SQL
    ).fetchall()
    covers: Mapping[int, str] = {}
    if canonical:
        from music_vault.metadata.canonical_albums import representative_album_covers

        covers = representative_album_covers(
            conn, (int(row["canonical_album_id"]) for row in canonical_rows)
        )
    summaries: list[AlbumSummary] = [
        AlbumSummary(
            key=AlbumKey(
                title_key=str(row["title_key"]),
                artist_key=str(row["artist_key"]),
                year_key=str(row["canonical_year"] or ""),
                canonical_album_id=int(row["canonical_album_id"]),
            ),
            album_title=str(row["album_title"]),
            album_artist=str(row["album_artist"]),
            canonical_year=(
                str(row["canonical_year"])
                if row["canonical_year"] is not None
                else None
            ),
            track_count=int(row["track_count"]),
            representative_cover_path=covers.get(int(row["canonical_album_id"])),
            edition_count=int(row["edition_count"]),
            album_kind=str(row["album_kind"]),
        )
        for row in canonical_rows
    ]
    summaries.extend(
        AlbumSummary(
            key=AlbumKey(
                title_key=str(row["title_key"]),
                artist_key=str(row["artist_key"]),
                year_key=str(row["year_key"]),
                virtual_kind=(
                    _SINGLES_UNCATALOGUED_KIND
                    if str(row["title_key"]) == ""
                    else ""
                ),
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
                None
                if str(row["title_key"]) == ""
                else str(row["cover_path"])
                if row["cover_path"] is not None
                else None
            ),
        )
        for row in legacy_rows
    )
    return tuple(sorted(summaries, key=lambda item: item.sort_value))


def query_artist_summaries(
    conn: sqlite3.Connection,
    *,
    image_states: Mapping[str, str] | None = None,
) -> tuple[ArtistSummary, ...]:
    configure_browser_connection(conn)
    structured = _has_structured_artist_schema(conn)
    canonical = structured and _has_canonical_artist_schema(conn)
    rows = conn.execute(
        _ARTIST_SUMMARY_V7_SQL
        if canonical
        else (_ARTIST_SUMMARY_V6_SQL if structured else _LEGACY_ARTIST_SUMMARY_SQL)
    ).fetchall()
    states = image_states or {}
    if not structured:
        return tuple(
            ArtistSummary(
                key=ArtistKey(str(row["normalized_key"])),
                display_name=str(row["display_name"]),
                track_count=int(row["track_count"]),
                image_state=str(
                    states.get(str(row["normalized_key"]), "not_cached")
                ),
            )
            for row in rows
        )

    structured_rows = {
        int(row["artist_id"]): row
        for row in rows
        if row["artist_id"] is not None
    }
    legacy_rows = [row for row in rows if row["artist_id"] is None]
    clusters = _artist_clusters(conn, structured_rows)
    cluster_by_id: dict[int, int] = {}
    for cluster_index, cluster in enumerate(clusters):
        for artist in cluster:
            cluster_by_id[int(artist["id"])] = cluster_index

    role_tracks: dict[int, dict[str, set[int]]] = {
        index: {
            "primary": set(),
            "featured": set(),
            "collaborator": set(),
            "group_appearance": set(),
        }
        for index in range(len(clusters))
    }
    if structured_rows:
        placeholders = ",".join("?" for _ in structured_rows)
        credit_rows = conn.execute(
            f"""
            SELECT artist_id,track_id,role
            FROM track_artist_credits
            WHERE artist_id IN ({placeholders})
              AND role IN ('primary','featured','collaborator')
              AND mv_artist_credit_visible(provenance,is_manual,is_locked)=1
            """,
            tuple(structured_rows),
        ).fetchall()
        for credit in credit_rows:
            cluster_index = cluster_by_id.get(int(credit["artist_id"]))
            if cluster_index is not None:
                role_tracks[cluster_index][str(credit["role"])].add(
                    int(credit["track_id"])
                )
        if canonical:
            group_rows = conn.execute(
                f"""
                SELECT relation.subject_artist_id AS artist_id,
                       group_credit.track_id
                FROM artist_relationships AS relation
                JOIN track_artist_credits AS group_credit
                  ON group_credit.artist_id=relation.related_artist_id
                 AND group_credit.role='primary'
                WHERE relation.relationship_kind='member_of'
                  AND relation.subject_artist_id IN ({placeholders})
                  AND mv_artist_credit_visible(
                      group_credit.provenance,
                      group_credit.is_manual,
                      group_credit.is_locked
                  )=1
                """,
                tuple(structured_rows),
            ).fetchall()
            for appearance in group_rows:
                cluster_index = cluster_by_id.get(int(appearance["artist_id"]))
                if cluster_index is not None:
                    role_tracks[cluster_index]["group_appearance"].add(
                        int(appearance["track_id"])
                    )

    alias_names: dict[int, set[str]] = {index: set() for index in range(len(clusters))}
    alias_owners: dict[str, set[int]] = {}
    if canonical and structured_rows:
        placeholders = ",".join("?" for _ in structured_rows)
        for alias in conn.execute(
            f"""
            SELECT artist_id,normalized_alias FROM artist_aliases
            WHERE artist_id IN ({placeholders})
            """,
            tuple(structured_rows),
        ).fetchall():
            cluster_index = cluster_by_id.get(int(alias["artist_id"]))
            if cluster_index is not None:
                normalized_alias = str(alias["normalized_alias"])
                alias_names[cluster_index].add(normalized_alias)
                alias_owners.setdefault(normalized_alias, set()).add(cluster_index)

    legacy_by_cluster: dict[int, list[sqlite3.Row]] = {
        index: [] for index in range(len(clusters))
    }
    unattached_legacy: list[sqlite3.Row] = []
    ambiguous_legacy_keys: set[str] = set()
    for legacy in legacy_rows:
        normalized = str(legacy["normalized_key"])
        candidates = [
            index
            for index, cluster in enumerate(clusters)
            if normalized in alias_names[index]
            or any(str(row["normalized_name"]) == normalized for row in cluster)
        ]
        if len(candidates) == 1:
            legacy_by_cluster[candidates[0]].append(legacy)
        else:
            unattached_legacy.append(legacy)
            if len(candidates) > 1:
                ambiguous_legacy_keys.add(str(legacy["identity_key"]))

    presentation_counts: dict[str, int] = {}
    canonical_rows: dict[int, sqlite3.Row] = {}
    for index, cluster in enumerate(clusters):
        chosen = _canonical_artist_row(cluster)
        canonical_rows[index] = chosen
        presentation = _artist_presentation_key(chosen["display_name"])
        presentation_counts[presentation] = presentation_counts.get(presentation, 0) + 1

    summaries: list[ArtistSummary] = []
    for index, cluster in enumerate(clusters):
        chosen = canonical_rows[index]
        artist_ids = tuple(int(row["id"]) for row in cluster)
        discogs_id = next(
            (
                str(row["discogs_artist_id"])
                for row in (chosen, *cluster)
                if _clean_provider_identity(row["discogs_artist_id"])
            ),
            "",
        )
        musicbrainz_id = next(
            (
                str(row["musicbrainz_artist_id"])
                for row in (chosen, *cluster)
                if _clean_provider_identity(row["musicbrainz_artist_id"])
            ),
            "",
        )
        provider_identity = (
            f"discogs:{discogs_id}"
            if discogs_id
            else (f"musicbrainz:{musicbrainz_id}" if musicbrainz_id else "")
        )
        normalized_name = str(chosen["normalized_name"])
        canonical_id = int(chosen["id"])
        presentation_conflict = (
            presentation_counts[_artist_presentation_key(chosen["display_name"])] > 1
        )
        key_artist_id = (
            canonical_id
            if provider_identity or len(artist_ids) > 1 or presentation_conflict
            else None
        )
        key = ArtistKey(
            normalized_name,
            artist_id=key_artist_id,
            provider_identity=provider_identity,
            cluster_artist_ids=artist_ids,
        )
        state_candidates = [
            key.browser_key,
            *(ArtistKey(str(row["normalized_name"]), artist_id=int(row["id"])).browser_key for row in cluster),
            normalized_name,
            *sorted(alias_names[index]),
        ]
        state = next(
            (str(states[value]) for value in state_candidates if value in states),
            "not_cached",
        )
        display_name = str(chosen["display_name"])
        if presentation_conflict:
            if discogs_id:
                display_name = f"{display_name} (Discogs {discogs_id})"
            elif musicbrainz_id:
                display_name = f"{display_name} (MusicBrainz {musicbrainz_id[:8]})"
            else:
                display_name = f"{display_name} (legacy unassigned)"
        primary_count = len(role_tracks[index]["primary"]) + sum(
            int(row["track_count"]) for row in legacy_by_cluster[index]
        )
        summaries.append(
            ArtistSummary(
                key=key,
                display_name=display_name,
                track_count=primary_count,
                image_state=state,
                featured_track_count=len(role_tracks[index]["featured"]),
                collaboration_track_count=len(role_tracks[index]["collaborator"]),
                group_appearance_track_count=len(
                    role_tracks[index]["group_appearance"]
                ),
                entity_type=str(chosen["entity_type"]),
                canonical_artist_id=canonical_id,
                discogs_artist_id=discogs_id or None,
                musicbrainz_artist_id=musicbrainz_id or None,
                image_identity_name=str(chosen["display_name"]),
                historical_aliases=tuple(
                    sorted(
                        alias
                        for alias in alias_names[index]
                        if len(alias_owners.get(alias, ())) == 1
                    )
                ),
                allow_normalized_name_cache=not presentation_conflict,
                allow_historical_alias_cache=True,
            )
        )

    for row in unattached_legacy:
        normalized_name = str(row["normalized_key"])
        key = ArtistKey(
            normalized_name,
            identity_key=str(row["identity_key"]),
        )
        display_name = str(row["display_name"])
        if str(row["identity_key"]) in ambiguous_legacy_keys:
            display_name = f"{display_name} (legacy unassigned)"
        summaries.append(
            ArtistSummary(
                key=key,
                display_name=display_name,
                track_count=int(row["track_count"]),
                image_state=str(
                    states.get(key.browser_key, states.get(normalized_name, "not_cached"))
                ),
            )
        )
    return tuple(sorted(summaries, key=lambda item: (item.sort_value, item.browser_key)))


def query_album_tracks(
    conn: sqlite3.Connection,
    key: AlbumKey,
) -> tuple[sqlite3.Row, ...]:
    configure_browser_connection(conn)
    if key.virtual_kind == _SINGLES_UNCATALOGUED_KIND:
        unmapped_filter = (
            """
              AND NOT EXISTS (
                  SELECT 1 FROM track_album_memberships AS membership
                  WHERE membership.track_id=tracks.id
              )
            """
            if _has_canonical_album_schema(conn)
            else ""
        )
        return tuple(
            conn.execute(
                f"""
                SELECT {_TRACK_SELECT}
                FROM tracks
                WHERE mv_is_uncatalogued_album(album) = 1
                {unmapped_filter}
                ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, id
                """
            ).fetchall()
        )
    if key.canonical_album_id is not None and _has_canonical_album_schema(conn):
        return tuple(
            conn.execute(
                f"""
                SELECT {_QUALIFIED_TRACK_SELECT}
                FROM track_album_memberships membership
                JOIN tracks ON tracks.id=membership.track_id
                WHERE membership.canonical_album_id=?
                ORDER BY
                    COALESCE(membership.disc_number, 1),
                    membership.track_position COLLATE NOCASE,
                    tracks.artist COLLATE NOCASE,
                    tracks.title COLLATE NOCASE,
                    tracks.id
                """,
                (key.canonical_album_id,),
            ).fetchall()
        )
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
    canonical = _has_canonical_artist_schema(conn)
    artist_ids = resolve_artist_cluster_ids(conn, key)
    if artist_ids:
        placeholders = ",".join("?" for _ in artist_ids)
        group_union = (
            f"""
            UNION
            SELECT group_credit.track_id, 'group_appearance' AS role
            FROM artist_relationships AS relation
            JOIN track_artist_credits AS group_credit
              ON group_credit.artist_id=relation.related_artist_id
             AND group_credit.role='primary'
            WHERE relation.subject_artist_id IN ({placeholders})
              AND relation.relationship_kind='member_of'
              AND mv_artist_credit_visible(
                  group_credit.provenance,
                  group_credit.is_manual,
                  group_credit.is_locked
              )=1
            """
            if canonical
            else ""
        )
        clusters = _artist_clusters(conn)
        matching_clusters = [
            cluster
            for cluster in clusters
            if key.normalized_name
            in {str(row["normalized_name"]) for row in cluster}
        ]
        include_legacy_fallback = len(matching_clusters) == 1
        legacy_union = (
            """
                UNION
                SELECT track.id, 'primary' AS role
                FROM tracks AS track
                LEFT JOIN track_metadata_fields AS artist_field
                  ON artist_field.track_id=track.id
                 AND artist_field.field_name='artist'
                WHERE mv_normalize_identity(track.artist)=?
                  AND NOT EXISTS (
                      SELECT 1 FROM track_artist_credits AS existing
                      WHERE existing.track_id=track.id
                  )
                  AND mv_artist_credit_visible(
                      artist_field.provenance,
                      COALESCE(artist_field.is_manual,0),
                      COALESCE(artist_field.is_locked,0)
                  )=1
            """
            if include_legacy_fallback
            else ""
        )
        parameters: tuple[object, ...] = (
            *artist_ids,
            *((artist_ids) if canonical else ()),
            *((key.normalized_name,) if include_legacy_fallback else ()),
        )
        rows = conn.execute(
            f"""
            WITH artist_tracks AS (
                SELECT credit.track_id, credit.role
                FROM track_artist_credits AS credit
                WHERE credit.artist_id IN ({placeholders})
                  AND credit.role IN ('primary','featured','collaborator')
                  AND mv_artist_credit_visible(
                      credit.provenance,credit.is_manual,credit.is_locked
                  )=1
                {group_union}
                {legacy_union}
            )
            SELECT
                track.id,track.title,track.artist,track.album,track.album_artist,
                track.year,track.path,track.cover_path,track.duration_seconds,
                track.created_at,track.source_kind,track.source_video_id,
                track.source_upload_date,
                artist_tracks.role AS artist_browser_role
            FROM artist_tracks
            JOIN tracks AS track ON track.id=artist_tracks.track_id
            ORDER BY
                CASE artist_tracks.role
                    WHEN 'primary' THEN 0
                    WHEN 'featured' THEN 1
                    WHEN 'collaborator' THEN 2
                    WHEN 'group_appearance' THEN 3
                    ELSE 4
                END,
                track.album COLLATE NOCASE,
                track.title COLLATE NOCASE,
                track.id
            """,
            parameters,
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_QUALIFIED_TRACK_SELECT}, 'primary' AS artist_browser_role
            FROM tracks
            LEFT JOIN track_metadata_fields AS artist_field
              ON artist_field.track_id=tracks.id
             AND artist_field.field_name='artist'
            WHERE mv_normalize_identity(tracks.artist)=?
              AND NOT EXISTS (
                  SELECT 1 FROM track_artist_credits AS existing
                  WHERE existing.track_id=tracks.id
              )
              AND mv_artist_credit_visible(
                  artist_field.provenance,
                  COALESCE(artist_field.is_manual,0),
                  COALESCE(artist_field.is_locked,0)
              )=1
            ORDER BY tracks.album COLLATE NOCASE,
                     tracks.title COLLATE NOCASE,tracks.id
            """,
            (key.normalized_name,),
        ).fetchall()
    by_role: dict[str, list[sqlite3.Row]] = {
        "primary": [],
        "featured": [],
        "collaborator": [],
        "group_appearance": [],
    }
    seen: dict[str, set[int]] = {role: set() for role in by_role}
    for row in rows:
        role = str(row["artist_browser_role"])
        track_id = int(row["id"])
        if role in by_role and track_id not in seen[role]:
            by_role[role].append(row)
            seen[role].add(track_id)
    return ArtistTrackSections(
        tracks=tuple(by_role["primary"]),
        featured_on=tuple(by_role["featured"]),
        collaborations=tuple(by_role["collaborator"]),
        group_appearances=tuple(by_role["group_appearance"]),
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
        BrowserInvalidationReason.CANONICAL_CONSOLIDATION,
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
