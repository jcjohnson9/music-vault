from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


CANONICAL_ALBUMS_TABLE = "canonical_albums"
TRACK_ALBUM_MEMBERSHIPS_TABLE = "track_album_memberships"
ARTIST_ALIASES_TABLE = "artist_aliases"
ARTIST_RELATIONSHIPS_TABLE = "artist_relationships"

ALBUM_KINDS = (
    "album",
    "live_album",
    "soundtrack",
    "score",
    "cast_recording",
    "compilation",
    "greatest_hits",
    "remix_album",
    "ep",
    "single",
    "demo_collection",
    "unknown",
)

ARTIST_ALIAS_KINDS = (
    "display_variant",
    "provider_alias",
    "legacy_credit_string",
    "source_title_variant",
    "corrected_version_suffix",
)

ARTIST_RELATIONSHIP_KINDS = (
    "member_of",
    "alias_of",
    "collaboration_project",
)

_SPACE_RE = re.compile(r"\s+")
_EDITION_LABELS = (
    r"super\s+deluxe(?:\s+edition)?",
    r"deluxe(?:\s+edition)?",
    r"expanded(?:\s+edition)?",
    r"(?:\d+(?:st|nd|rd|th)\s+)?anniversary\s+edition",
    r"(?:\d{4}\s+)?remaster(?:ed)?",
    r"reissue",
    r"bonus\s+edition",
    r"special\s+edition",
    r"collector['\u2019]?s\s+edition",
    r"alternate[-\s]+cover(?:\s+edition)?",
)
_EDITION_RE = re.compile(
    r"^(?P<title>.+?)\s*(?:"
    r"[\(\[]\s*(?P<bracket>" + "|".join(_EDITION_LABELS) + r")\s*[\)\]]"
    r"|(?:\s[-\u2013\u2014,:]\s+)(?P<separated>" + "|".join(_EDITION_LABELS) + r")"
    r")\s*$",
    re.IGNORECASE,
)


def _display(value: object) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def normalize_album_identity(value: object) -> str:
    """Normalize identity spelling without discarding meaningful punctuation."""

    return _display(value).casefold()


def split_edition_label(title: object) -> tuple[str, str | None]:
    """Split only a conservative, explicit edition suffix from an album title."""

    display = _display(title)
    match = _EDITION_RE.fullmatch(display)
    if match is None:
        return display, None
    base = _display(match.group("title"))
    edition = _display(match.group("bracket") or match.group("separated"))
    return (base, edition) if base else (display, None)


def classify_album_kind(
    title: object,
    *,
    release_format: object = None,
    explicit_kind: object = None,
) -> str:
    """Classify broad release identity without treating ordinary editions as works."""

    explicit = _display(explicit_kind).casefold().replace(" ", "_")
    if explicit in ALBUM_KINDS:
        return explicit
    title_text = _display(title).casefold()
    format_text = _display(release_format).casefold()
    text = f"{title_text} {format_text}"
    if re.search(r"\b(?:broadway|stage|film)\s+cast\b|\bcast\s+recording\b", text):
        return "cast_recording"
    if re.search(
        r"\b(?:original\s+)?(?:motion\s+picture|television|tv)\s+score\b|"
        r"\bfilm\s+score\b|\boriginal\s+score\b",
        text,
    ):
        return "score"
    if re.search(r"\bsoundtrack\b|\boriginal\s+motion\s+picture\s+soundtrack\b", text):
        return "soundtrack"
    if re.search(r"\bgreatest\s+hits\b|\bbest\s+of\b", text):
        return "greatest_hits"
    if re.search(r"\bremix(?:es)?\b", text):
        return "remix_album"
    if re.search(r"\bdemo(?:s|\s+collection)?\b", text):
        return "demo_collection"
    # ``live`` is a common verb/adjective in legitimate studio-album titles
    # (for example, "Live Through This").  Treat it as release identity only
    # when catalogue format says so or the title uses an explicit live-release
    # construction.
    if re.search(r"\blive\b", format_text) or re.search(
        r"(?:\blive\s+(?:at|in|from|on)\b|\blive\s+album\b|"
        r"\((?:recorded\s+)?live\)|[-:\u2013\u2014]\s*live\b|\blive$)",
        title_text,
    ):
        return "live_album"
    if re.search(r"\bcompilation\b|\banthology\b", text):
        return "compilation"
    if re.search(r"(?:^|[\s\(\[])ep(?:$|[\s\)\]])|\bextended\s+play\b", text):
        return "ep"
    if re.search(r"(?:^|[\s\(\[])single(?:$|[\s\)\]])", text):
        return "single"
    return "album" if _display(title) else "unknown"


@dataclass(frozen=True)
class CanonicalAlbumIdentity:
    canonical_key: str
    title: str
    normalized_title: str
    album_artist_display: str
    normalized_album_artist: str
    album_kind: str
    edition_label: str | None
    identity_kind: str


class CanonicalAlbumIdentityConflict(RuntimeError):
    """Raised when durable release identities point at different album rows."""


def canonical_album_identity(
    title: object,
    album_artist: object,
    *,
    album_kind: object = None,
    release_format: object = None,
    discogs_master_id: object = None,
    musicbrainz_release_group_id: object = None,
    provider_release_family_id: object = None,
) -> CanonicalAlbumIdentity:
    """Build a stable album key in provider-first priority order.

    Release dates, years, physical formats, countries, and artwork paths are
    intentionally not inputs to the fallback identity.
    """

    display_title, edition_label = split_edition_label(title)
    display_artist = _display(album_artist) or "Unknown Album Artist"
    normalized_title = normalize_album_identity(display_title)
    normalized_artist = normalize_album_identity(display_artist)
    kind = classify_album_kind(
        display_title,
        release_format=release_format,
        explicit_kind=album_kind,
    )
    master_id = _display(discogs_master_id)
    release_group_id = _display(musicbrainz_release_group_id)
    family_id = _display(provider_release_family_id)
    if master_id:
        key = f"discogs-master:{master_id.casefold()}"
        identity_kind = "discogs_master"
    elif release_group_id:
        key = f"musicbrainz-release-group:{release_group_id.casefold()}"
        identity_kind = "musicbrainz_release_group"
    elif family_id:
        key = f"provider-release-family:{family_id.casefold()}"
        identity_kind = "provider_release_family"
    else:
        key = f"fallback:{kind}:{normalized_artist}:{normalized_title}"
        identity_kind = "fallback"
    return CanonicalAlbumIdentity(
        canonical_key=key,
        title=display_title,
        normalized_title=normalized_title,
        album_artist_display=display_artist,
        normalized_album_artist=normalized_artist,
        album_kind=kind,
        edition_label=edition_label,
        identity_kind=identity_kind,
    )


def create_canonical_media_schema(conn: sqlite3.Connection) -> None:
    """Install additive schema-v7 album, alias, and relationship structures."""

    album_kinds = ", ".join(f"'{value}'" for value in ALBUM_KINDS)
    alias_kinds = ", ".join(f"'{value}'" for value in ARTIST_ALIAS_KINDS)
    relationship_kinds = ", ".join(
        f"'{value}'" for value in ARTIST_RELATIONSHIP_KINDS
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CANONICAL_ALBUMS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_key TEXT NOT NULL UNIQUE CHECK (TRIM(canonical_key) != ''),
            title TEXT NOT NULL CHECK (TRIM(title) != ''),
            normalized_title TEXT NOT NULL CHECK (TRIM(normalized_title) != ''),
            album_artist_display TEXT NOT NULL CHECK (TRIM(album_artist_display) != ''),
            normalized_album_artist TEXT NOT NULL CHECK (TRIM(normalized_album_artist) != ''),
            album_kind TEXT NOT NULL DEFAULT 'unknown'
                CHECK (album_kind IN ({album_kinds})),
            discogs_master_id TEXT,
            musicbrainz_release_group_id TEXT,
            provider_release_family_id TEXT,
            original_release_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    album_columns = _column_names(conn, CANONICAL_ALBUMS_TABLE)
    if "provider_release_family_id" not in album_columns:
        conn.execute(
            f"ALTER TABLE {CANONICAL_ALBUMS_TABLE} "
            "ADD COLUMN provider_release_family_id TEXT"
        )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TRACK_ALBUM_MEMBERSHIPS_TABLE} (
            track_id INTEGER PRIMARY KEY,
            canonical_album_id INTEGER NOT NULL,
            discogs_release_id TEXT,
            edition_label TEXT,
            edition_release_date TEXT,
            track_position TEXT,
            disc_number INTEGER CHECK (disc_number IS NULL OR disc_number > 0),
            provenance TEXT NOT NULL,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
            FOREIGN KEY (canonical_album_id) REFERENCES canonical_albums(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ARTIST_ALIASES_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id INTEGER NOT NULL,
            alias_name TEXT NOT NULL CHECK (TRIM(alias_name) != ''),
            normalized_alias TEXT NOT NULL CHECK (TRIM(normalized_alias) != ''),
            alias_kind TEXT NOT NULL CHECK (alias_kind IN ({alias_kinds})),
            provenance TEXT NOT NULL,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            created_at TEXT NOT NULL,
            UNIQUE (artist_id, normalized_alias, alias_kind),
            FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ARTIST_RELATIONSHIPS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_artist_id INTEGER NOT NULL,
            related_artist_id INTEGER NOT NULL,
            relationship_kind TEXT NOT NULL CHECK (relationship_kind IN ({relationship_kinds})),
            provenance TEXT NOT NULL,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (subject_artist_id != related_artist_id),
            UNIQUE (subject_artist_id, related_artist_id, relationship_kind),
            FOREIGN KEY (subject_artist_id) REFERENCES artists(id) ON DELETE RESTRICT,
            FOREIGN KEY (related_artist_id) REFERENCES artists(id) ON DELETE RESTRICT
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_canonical_albums_identity ON canonical_albums(normalized_album_artist, normalized_title, album_kind, id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_albums_discogs_master ON canonical_albums(discogs_master_id) WHERE discogs_master_id IS NOT NULL AND TRIM(discogs_master_id) != ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_albums_mb_release_group ON canonical_albums(musicbrainz_release_group_id) WHERE musicbrainz_release_group_id IS NOT NULL AND TRIM(musicbrainz_release_group_id) != ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_albums_provider_family ON canonical_albums(provider_release_family_id) WHERE provider_release_family_id IS NOT NULL AND TRIM(provider_release_family_id) != ''",
        "CREATE INDEX IF NOT EXISTS idx_track_album_memberships_album ON track_album_memberships(canonical_album_id, track_id)",
        "CREATE INDEX IF NOT EXISTS idx_track_album_memberships_discogs_release ON track_album_memberships(discogs_release_id, canonical_album_id)",
        "CREATE INDEX IF NOT EXISTS idx_artist_aliases_normalized ON artist_aliases(normalized_alias, artist_id)",
        "CREATE INDEX IF NOT EXISTS idx_artist_aliases_artist ON artist_aliases(artist_id, alias_kind, id)",
        "CREATE INDEX IF NOT EXISTS idx_artist_relationships_subject ON artist_relationships(subject_artist_id, relationship_kind, related_artist_id)",
        "CREATE INDEX IF NOT EXISTS idx_artist_relationships_related ON artist_relationships(related_artist_id, relationship_kind, subject_artist_id)",
    ):
        conn.execute(statement)

def required_canonical_media_indexes() -> tuple[str, ...]:
    return (
        "idx_canonical_albums_identity",
        "idx_canonical_albums_discogs_master",
        "idx_canonical_albums_mb_release_group",
        "idx_canonical_albums_provider_family",
        "idx_track_album_memberships_album",
        "idx_track_album_memberships_discogs_release",
        "idx_artist_aliases_normalized",
        "idx_artist_aliases_artist",
        "idx_artist_relationships_subject",
        "idx_artist_relationships_related",
    )


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _album_rows(
    conn: sqlite3.Connection,
    track_ids: Iterable[int] | None = None,
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    track_columns = _column_names(conn, "tracks")
    context_columns = _column_names(conn, "track_release_context")
    mb_group_sources: list[str] = []
    if "musicbrainz_release_group_id" in track_columns:
        mb_group_sources.append("NULLIF(TRIM(t.musicbrainz_release_group_id), '')")
    if "musicbrainz_release_group_id" in context_columns:
        mb_group_sources.append("NULLIF(TRIM(rc.musicbrainz_release_group_id), '')")
    mb_group_expression = (
        mb_group_sources[0]
        if len(mb_group_sources) == 1
        else f"COALESCE({', '.join(mb_group_sources)})"
        if mb_group_sources
        else "NULL"
    )
    family_expression = (
        "NULLIF(TRIM(rc.provider_release_family_id), '')"
        if "provider_release_family_id" in context_columns
        else "NULL"
    )
    ids = list(dict.fromkeys(int(value) for value in (track_ids or ())))
    if track_ids is not None and not ids:
        return []
    track_filter = (
        f"WHERE t.id IN ({','.join('?' for _ in ids)})"
        if track_ids is not None
        else ""
    )
    return conn.execute(
        f"""
        SELECT t.id AS track_id, t.album, t.album_artist, t.artist,
               t.release_date, t.original_release_date, t.cover_path,
               t.discogs_release_id, t.discogs_master_id,
               t.discogs_track_position,
               rc.discogs_release_id AS context_discogs_release_id,
               rc.discogs_master_id AS context_discogs_master_id,
               rc.release_format, rc.original_release_date AS context_original_release_date,
               rc.provider_reference, rc.confidence,
               {mb_group_expression} AS musicbrainz_release_group_id,
               {family_expression} AS provider_release_family_id,
               COALESCE((
                   SELECT field.is_manual
                   FROM track_metadata_fields field
                   WHERE field.track_id=t.id AND field.field_name='album'
               ), 0) AS album_is_manual,
               COALESCE((
                   SELECT field.is_locked
                   FROM track_metadata_fields field
                   WHERE field.track_id=t.id AND field.field_name='album'
               ), 0) AS album_is_locked,
               COALESCE((
                   SELECT field.is_manual
                   FROM track_metadata_fields field
                   WHERE field.track_id=t.id AND field.field_name='album_artist'
               ), 0) AS album_artist_is_manual,
               COALESCE((
                   SELECT field.is_locked
                   FROM track_metadata_fields field
                   WHERE field.track_id=t.id AND field.field_name='album_artist'
               ), 0) AS album_artist_is_locked,
               (
                   SELECT artist.display_name
                   FROM track_artist_credits credit
                   JOIN artists artist ON artist.id=credit.artist_id
                   WHERE credit.track_id=t.id AND credit.role='primary'
                   ORDER BY credit.credit_order, credit.id
                   LIMIT 1
               ) AS primary_artist_display
        FROM tracks t
        LEFT JOIN track_release_context rc ON rc.track_id=t.id
        {track_filter}
        ORDER BY t.id
        """,
        ids,
    ).fetchall()


def _prepared_album_rows(
    conn: sqlite3.Connection,
    track_ids: Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in _album_rows(conn, track_ids):
        if not _display(row["album"]):
            continue
        kind = classify_album_kind(
            row["album"], release_format=row["release_format"]
        )
        release_context_kind = kind in {
            "soundtrack",
            "score",
            "cast_recording",
            "compilation",
        }
        if release_context_kind:
            # Soundtracks, scores, cast recordings, and compilations often have
            # many performing artists. Keep a real saved album artist when
            # present; otherwise use neutral release context so the work does
            # not split into one fallback album per performer.
            album_artist = _display(row["album_artist"]) or "Various Artists"
        else:
            # For ordinary artist releases the consolidated structured primary
            # credit is the canonical fallback identity. Raw album-artist
            # punctuation/spacing variants must not recreate duplicate cards.
            authoritative_album_artist = bool(
                row["album_artist_is_manual"] or row["album_artist_is_locked"]
            )
            album_artist = (
                _display(row["album_artist"])
                if authoritative_album_artist and _display(row["album_artist"])
                else _display(row["primary_artist_display"])
                or _display(row["album_artist"])
                or _display(row["artist"])
            )
        identity = canonical_album_identity(
            row["album"],
            album_artist,
            album_kind=kind,
            release_format=row["release_format"],
            discogs_master_id=(
                row["discogs_master_id"] or row["context_discogs_master_id"]
            ),
            musicbrainz_release_group_id=row["musicbrainz_release_group_id"],
            provider_release_family_id=row["provider_release_family_id"],
        )
        discogs_release_id = (
            _display(row["discogs_release_id"])
            or _display(row["context_discogs_release_id"])
            or None
        )
        confidence = row["confidence"]
        if confidence is None and identity.identity_kind != "fallback":
            confidence = 100.0
        elif confidence is None:
            confidence = 50.0
        prepared.append(
            {
                "track_id": int(row["track_id"]),
                "identity": identity,
                "discogs_master_id": (
                    _display(row["discogs_master_id"] or row["context_discogs_master_id"])
                    or None
                ),
                "musicbrainz_release_group_id": (
                    _display(row["musicbrainz_release_group_id"]) or None
                ),
                "provider_release_family_id": (
                    _display(row["provider_release_family_id"]) or None
                ),
                "album_authoritative": bool(
                    row["album_is_manual"] or row["album_is_locked"]
                ),
                "album_artist_authoritative": bool(
                    row["album_artist_is_manual"] or row["album_artist_is_locked"]
                ),
                "original_release_date": (
                    _display(
                        row["original_release_date"]
                        or row["context_original_release_date"]
                        or row["release_date"]
                    )
                    or None
                ),
                "discogs_release_id": discogs_release_id,
                "edition_release_date": _display(row["release_date"]) or None,
                "track_position": _display(row["discogs_track_position"]) or None,
                "provenance": (
                    "discogs"
                    if identity.identity_kind == "discogs_master"
                    else "musicbrainz"
                    if identity.identity_kind == "musicbrainz_release_group"
                    else "provider"
                    if identity.identity_kind == "provider_release_family"
                    else "legacy_fallback"
                ),
                "provider_reference": _display(row["provider_reference"]) or None,
                "confidence": float(confidence),
            }
        )
    return prepared


def analyze_canonical_album_backfill(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return aggregate-only schema-v7 backfill diagnostics without writing."""

    prepared = _prepared_album_rows(conn)
    existing_ids = {
        int(row[0])
        for row in conn.execute(
            f"SELECT track_id FROM {TRACK_ALBUM_MEMBERSHIPS_TABLE}"
        ).fetchall()
    }
    kinds = Counter(item["identity"].album_kind for item in prepared)
    strategies = Counter(item["identity"].identity_kind for item in prepared)
    eligible_ids = {int(item["track_id"]) for item in prepared}
    pending = [item for item in prepared if int(item["track_id"]) not in existing_ids]
    safe_group_count, ambiguous_group_count = _preview_canonical_album_groups(
        conn, pending
    )
    track_count = int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
    return {
        "track_count": track_count,
        "eligible_track_count": len(eligible_ids),
        "missing_album_count": track_count - len(eligible_ids),
        "existing_membership_count": len(existing_ids),
        "proposed_membership_count": len(eligible_ids - existing_ids),
        "proposed_canonical_album_count": safe_group_count,
        "ambiguous_group_count": ambiguous_group_count,
        "edition_label_count": sum(
            1 for item in prepared if item["identity"].edition_label
        ),
        "identity_strategy_counts": dict(sorted(strategies.items())),
        "album_kind_counts": dict(sorted(kinds.items())),
        "would_modify_track_rows": 0,
        "would_modify_media_files": 0,
        "would_modify_artwork_files": 0,
    }


def _canonical_album_match_ids(
    conn: sqlite3.Connection,
    item: Mapping[str, Any],
) -> tuple[int, ...]:
    """Return durable-identity matches for aggregate conflict diagnostics."""

    identity: CanonicalAlbumIdentity = item["identity"]
    matches: set[int] = set()
    for column, value in (
        ("discogs_master_id", item.get("discogs_master_id")),
        (
            "musicbrainz_release_group_id",
            item.get("musicbrainz_release_group_id"),
        ),
        ("provider_release_family_id", item.get("provider_release_family_id")),
        ("canonical_key", identity.canonical_key),
    ):
        if value in (None, ""):
            continue
        row = conn.execute(
            f"SELECT id FROM {CANONICAL_ALBUMS_TABLE} WHERE {column}=?",
            (value,),
        ).fetchone()
        if row is not None:
            matches.add(int(row[0]))
    return tuple(sorted(matches))


def _preview_canonical_album_groups(
    conn: sqlite3.Connection,
    pending: Iterable[Mapping[str, Any]],
) -> tuple[int, int]:
    """Simulate canonical grouping in memory and return safe/ambiguous counts.

    The source connection is never mutated.  Reusing the real resolver on a
    scratch schema keeps partial provider coverage, fallback promotion, and
    durable-ID conflicts aligned with the migration path instead of counting
    raw provider/fallback keys that may converge to one card.
    """

    items = list(pending)
    if not items:
        return 0, 0

    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    try:
        create_canonical_media_schema(scratch)
        album_columns = (
            "id",
            "canonical_key",
            "title",
            "normalized_title",
            "album_artist_display",
            "normalized_album_artist",
            "album_kind",
            "discogs_master_id",
            "musicbrainz_release_group_id",
            "provider_release_family_id",
            "original_release_date",
            "created_at",
            "updated_at",
        )
        existing = conn.execute(
            f"SELECT {','.join(album_columns)} FROM {CANONICAL_ALBUMS_TABLE} "
            "ORDER BY id"
        ).fetchall()
        if existing:
            placeholders = ",".join("?" for _ in album_columns)
            scratch.executemany(
                f"INSERT INTO {CANONICAL_ALBUMS_TABLE} "
                f"({','.join(album_columns)}) VALUES ({placeholders})",
                (tuple(row[column] for column in album_columns) for row in existing),
            )

        resolved: list[tuple[tuple[str, str, str], int]] = []
        conflicts: list[tuple[tuple[str, str, str], tuple[int, ...]]] = []
        timestamp = "canonical-album-dry-run"
        for item in items:
            identity: CanonicalAlbumIdentity = item["identity"]
            normalized_group = (
                identity.normalized_title,
                identity.normalized_album_artist,
                identity.album_kind,
            )
            scratch.execute("SAVEPOINT canonical_album_preview")
            try:
                album_id = _get_or_create_canonical_album(
                    scratch,
                    item,
                    original_release_date=item.get("original_release_date"),
                    timestamp=timestamp,
                )
            except CanonicalAlbumIdentityConflict:
                match_ids = _canonical_album_match_ids(scratch, item)
                scratch.execute("ROLLBACK TO SAVEPOINT canonical_album_preview")
                scratch.execute("RELEASE SAVEPOINT canonical_album_preview")
                conflicts.append((normalized_group, match_ids))
                continue
            except Exception:
                scratch.execute("ROLLBACK TO SAVEPOINT canonical_album_preview")
                scratch.execute("RELEASE SAVEPOINT canonical_album_preview")
                raise
            scratch.execute("RELEASE SAVEPOINT canonical_album_preview")
            resolved.append((normalized_group, int(album_id)))

        ids_by_normalized_group: dict[tuple[str, str, str], set[int]] = {}
        for normalized_group, album_id in resolved:
            ids_by_normalized_group.setdefault(normalized_group, set()).add(album_id)
        ambiguous_normalized_groups = {
            group
            for group, album_ids in ids_by_normalized_group.items()
            if len(album_ids) > 1
        }
        ambiguous_album_ids = {
            album_id
            for group in ambiguous_normalized_groups
            for album_id in ids_by_normalized_group[group]
        }
        conflict_signatures: set[tuple[int, ...] | tuple[str, str, str]] = set()
        for normalized_group, match_ids in conflicts:
            ambiguous_album_ids.update(match_ids)
            if normalized_group not in ambiguous_normalized_groups:
                conflict_signatures.add(match_ids or normalized_group)

        resolved_album_ids = {album_id for _, album_id in resolved}
        safe_album_ids = resolved_album_ids - ambiguous_album_ids
        ambiguous_count = len(ambiguous_normalized_groups) + len(conflict_signatures)
        return len(safe_album_ids), ambiguous_count
    finally:
        scratch.close()


def _canonical_album_row_for_identity(
    conn: sqlite3.Connection,
    item: Mapping[str, Any],
) -> sqlite3.Row | None:
    """Resolve a card through every available identity, strongest first."""

    identity: CanonicalAlbumIdentity = item["identity"]
    matches: dict[int, sqlite3.Row] = {}
    for column, value in (
        ("discogs_master_id", item.get("discogs_master_id")),
        (
            "musicbrainz_release_group_id",
            item.get("musicbrainz_release_group_id"),
        ),
        ("provider_release_family_id", item.get("provider_release_family_id")),
        ("canonical_key", identity.canonical_key),
    ):
        if value in (None, ""):
            continue
        row = conn.execute(
            f"SELECT * FROM {CANONICAL_ALBUMS_TABLE} WHERE {column}=?",
            (value,),
        ).fetchone()
        if row is not None:
            matches[int(row["id"])] = row
    if len(matches) > 1:
        # Never guess when accepted strong identities disagree.  Callers keep
        # the existing membership/transaction intact and surface a stable
        # diagnostic instead of silently merging two releases.
        raise CanonicalAlbumIdentityConflict(
            "Conflicting durable release identities resolve to different canonical albums."
        )
    if matches:
        return next(iter(matches.values()))

    # Provider coverage is commonly partial across tracks from one album. A
    # fallback-only sibling may join a provider card when normalized title,
    # canonical artist, and kind identify exactly one row. Conversely, newly
    # accepted strong evidence may promote exactly one identity-free fallback
    # row. Multiple candidates or an established different provider identity
    # remain separate and therefore fail closed.
    normalized = conn.execute(
        f"""
        SELECT * FROM {CANONICAL_ALBUMS_TABLE}
        WHERE normalized_title=? AND normalized_album_artist=? AND album_kind=?
        ORDER BY id LIMIT 2
        """,
        (
            identity.normalized_title,
            identity.normalized_album_artist,
            identity.album_kind,
        ),
    ).fetchall()
    if len(normalized) != 1:
        return None
    candidate = normalized[0]
    if identity.identity_kind == "fallback":
        return candidate
    candidate_has_strong_identity = any(
        candidate[column] not in (None, "")
        for column in (
            "discogs_master_id",
            "musicbrainz_release_group_id",
            "provider_release_family_id",
        )
    )
    if (
        not candidate_has_strong_identity
        and str(candidate["canonical_key"]).startswith("fallback:")
    ):
        return candidate
    return None


def _identity_key_rank(value: object) -> int:
    key = str(value or "")
    if key.startswith("discogs-master:"):
        return 0
    if key.startswith("musicbrainz-release-group:"):
        return 1
    if key.startswith("provider-release-family:"):
        return 2
    return 3


def _get_or_create_canonical_album(
    conn: sqlite3.Connection,
    item: Mapping[str, Any],
    *,
    original_release_date: str | None,
    timestamp: str,
) -> int:
    """Return one card across overlapping strong identities.

    Later evidence may reveal a stronger key for an existing family (for
    example, a MusicBrainz release group after a provider-family match).  The
    row is promoted in place so memberships stay stable and a lower-priority
    identity can still find it on a future import.
    """

    identity: CanonicalAlbumIdentity = item["identity"]
    row = _canonical_album_row_for_identity(conn, item)
    if row is None:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {CANONICAL_ALBUMS_TABLE} (
                canonical_key, title, normalized_title, album_artist_display,
                normalized_album_artist, album_kind, discogs_master_id,
                musicbrainz_release_group_id, provider_release_family_id,
                original_release_date, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity.canonical_key,
                identity.title,
                identity.normalized_title,
                identity.album_artist_display,
                identity.normalized_album_artist,
                identity.album_kind,
                item.get("discogs_master_id"),
                item.get("musicbrainz_release_group_id"),
                item.get("provider_release_family_id"),
                original_release_date,
                timestamp,
                timestamp,
            ),
        )
        row = _canonical_album_row_for_identity(conn, item)
    if row is None:
        raise RuntimeError("Could not resolve the canonical album identity.")

    album_id = int(row["id"])
    updates: dict[str, object] = {}
    if _identity_key_rank(identity.canonical_key) < _identity_key_rank(
        row["canonical_key"]
    ):
        occupied = conn.execute(
            f"SELECT id FROM {CANONICAL_ALBUMS_TABLE} WHERE canonical_key=?",
            (identity.canonical_key,),
        ).fetchone()
        if occupied is None or int(occupied[0]) == album_id:
            updates["canonical_key"] = identity.canonical_key
    for column, value in (
        ("discogs_master_id", item.get("discogs_master_id")),
        (
            "musicbrainz_release_group_id",
            item.get("musicbrainz_release_group_id"),
        ),
        ("provider_release_family_id", item.get("provider_release_family_id")),
    ):
        if value in (None, "") or row[column] not in (None, ""):
            continue
        owner = conn.execute(
            f"SELECT id FROM {CANONICAL_ALBUMS_TABLE} WHERE {column}=?",
            (value,),
        ).fetchone()
        if owner is None or int(owner[0]) == album_id:
            updates[column] = value
    stored_date = _display(row["original_release_date"]) or None
    if original_release_date and (
        stored_date is None or str(original_release_date) < stored_date
    ):
        updates["original_release_date"] = original_release_date
    if updates:
        assignments = ",".join(f"{column}=?" for column in updates)
        conn.execute(
            f"UPDATE {CANONICAL_ALBUMS_TABLE} "
            f"SET {assignments},updated_at=? WHERE id=?",
            (*updates.values(), timestamp, album_id),
        )
    return album_id


def seed_existing_canonical_albums(conn: sqlite3.Connection) -> dict[str, Any]:
    """Backfill only missing memberships while preserving every legacy field."""

    diagnostics = analyze_canonical_album_backfill(conn)
    prepared = _prepared_album_rows(conn)
    existing_track_ids = {
        int(row[0])
        for row in conn.execute(
            f"SELECT track_id FROM {TRACK_ALBUM_MEMBERSHIPS_TABLE}"
        ).fetchall()
    }
    prepared = [
        item for item in prepared if int(item["track_id"]) not in existing_track_ids
    ]
    original_dates: dict[str, str] = {}
    for item in prepared:
        value = item["original_release_date"]
        key = item["identity"].canonical_key
        if value and (key not in original_dates or value < original_dates[key]):
            original_dates[key] = value
    timestamp = str(conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])
    for item in prepared:
        identity: CanonicalAlbumIdentity = item["identity"]
        album_id = _get_or_create_canonical_album(
            conn,
            item,
            original_release_date=original_dates.get(identity.canonical_key),
            timestamp=timestamp,
        )
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {TRACK_ALBUM_MEMBERSHIPS_TABLE} (
                track_id, canonical_album_id, discogs_release_id, edition_label,
                edition_release_date, track_position, disc_number, provenance,
                provider_reference, confidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                item["track_id"],
                album_id,
                item["discogs_release_id"],
                identity.edition_label,
                item["edition_release_date"],
                item["track_position"],
                item["provenance"],
                item["provider_reference"],
                item["confidence"],
                timestamp,
                timestamp,
            ),
        )
    return diagnostics


def _refresh_canonical_album_presentation(
    conn: sqlite3.Connection,
    album_id: int,
    item: Mapping[str, Any],
    *,
    timestamp: str,
) -> None:
    """Refresh a card only from safe same-identity presentation evidence.

    Automatic single-track corrections may update their own card.  A shared
    card changes only when the relevant field is manual/locked; this keeps one
    incidental edition string from renaming a multi-track provider family
    while still honoring an explicit correction.
    """

    row = conn.execute(
        f"""
        SELECT title,normalized_title,album_artist_display,
               normalized_album_artist,album_kind,discogs_master_id,
               musicbrainz_release_group_id,provider_release_family_id
        FROM {CANONICAL_ALBUMS_TABLE}
        WHERE id=?
        """,
        (int(album_id),),
    ).fetchone()
    if row is None:
        return
    member_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {TRACK_ALBUM_MEMBERSHIPS_TABLE} "
            "WHERE canonical_album_id=?",
            (int(album_id),),
        ).fetchone()[0]
    )
    identity: CanonicalAlbumIdentity = item["identity"]
    updates: dict[str, object] = {}
    if member_count <= 1 or bool(item.get("album_authoritative")):
        for column, value in (
            ("title", identity.title),
            ("normalized_title", identity.normalized_title),
            ("album_kind", identity.album_kind),
        ):
            if row[column] != value:
                updates[column] = value
    if member_count <= 1 or bool(item.get("album_artist_authoritative")):
        for column, value in (
            ("album_artist_display", identity.album_artist_display),
            ("normalized_album_artist", identity.normalized_album_artist),
        ):
            if row[column] != value:
                updates[column] = value
    for column, value in (
        ("discogs_master_id", item.get("discogs_master_id")),
        (
            "musicbrainz_release_group_id",
            item.get("musicbrainz_release_group_id"),
        ),
        ("provider_release_family_id", item.get("provider_release_family_id")),
    ):
        if value not in (None, "") and row[column] in (None, ""):
            updates[column] = value
    if not updates:
        return
    assignments = ",".join(f"{column}=?" for column in updates)
    conn.execute(
        f"UPDATE {CANONICAL_ALBUMS_TABLE} "
        f"SET {assignments},updated_at=? WHERE id=?",
        (*updates.values(), timestamp, int(album_id)),
    )


def upsert_track_canonical_album(
    conn: sqlite3.Connection,
    track_id: int,
) -> int | None:
    """Incrementally maintain one durable album membership for a track.

    A newly accepted provider family may promote a fallback membership.  A
    manual membership or conflicting provider identity is never displaced.
    Repeating the same input performs no timestamp-only write.
    """

    prepared = _prepared_album_rows(conn, (int(track_id),))
    existing = conn.execute(
        f"""
        SELECT membership.*, album.canonical_key
        FROM {TRACK_ALBUM_MEMBERSHIPS_TABLE} membership
        JOIN {CANONICAL_ALBUMS_TABLE} album
          ON album.id=membership.canonical_album_id
        WHERE membership.track_id=?
        """,
        (int(track_id),),
    ).fetchone()
    if not prepared:
        # The effective album field has been explicitly cleared.  Membership
        # is active browser state, so retaining it would leave a stale card.
        # Keep the canonical album row as harmless historical identity; the
        # browser joins through memberships and therefore retires it safely.
        if existing is not None:
            conn.execute(
                f"DELETE FROM {TRACK_ALBUM_MEMBERSHIPS_TABLE} WHERE track_id=?",
                (int(track_id),),
            )
        return None

    item = prepared[0]
    identity: CanonicalAlbumIdentity = item["identity"]
    if existing is not None and identity.canonical_key != str(existing["canonical_key"]):
        existing_key = str(existing["canonical_key"])
        linked = _canonical_album_row_for_identity(conn, item)
        linked_same_album = bool(
            linked is not None
            and int(linked["id"]) == int(existing["canonical_album_id"])
        )
        may_promote = (
            str(existing["provenance"]).casefold() != "manual"
            and (
                existing_key.startswith("fallback:")
                or (
                    linked_same_album
                    and _identity_key_rank(identity.canonical_key)
                    < _identity_key_rank(existing_key)
                )
            )
        )
        if not may_promote:
            return int(existing["canonical_album_id"])
    timestamp = str(conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])
    candidate_album_id = _get_or_create_canonical_album(
        conn,
        item,
        original_release_date=item["original_release_date"],
        timestamp=timestamp,
    )
    album_row = conn.execute(
        f"SELECT id,original_release_date FROM {CANONICAL_ALBUMS_TABLE} WHERE id=?",
        (candidate_album_id,),
    ).fetchone()
    assert album_row is not None
    if (
        existing is not None
        and int(existing["canonical_album_id"]) == candidate_album_id
    ):
        _refresh_canonical_album_presentation(
            conn,
            candidate_album_id,
            item,
            timestamp=timestamp,
        )
    original_date = item["original_release_date"]
    stored_original_date = _display(album_row["original_release_date"]) or None
    if original_date and (
        stored_original_date is None or str(original_date) < stored_original_date
    ):
        conn.execute(
            f"UPDATE {CANONICAL_ALBUMS_TABLE} "
            "SET original_release_date=?,updated_at=? WHERE id=?",
            (original_date, timestamp, candidate_album_id),
        )

    values = (
        candidate_album_id,
        item["discogs_release_id"],
        identity.edition_label,
        item["edition_release_date"],
        item["track_position"],
        item["provenance"],
        item["provider_reference"],
        item["confidence"],
    )
    if existing is None:
        conn.execute(
            f"""
            INSERT INTO {TRACK_ALBUM_MEMBERSHIPS_TABLE} (
                track_id,canonical_album_id,discogs_release_id,edition_label,
                edition_release_date,track_position,disc_number,provenance,
                provider_reference,confidence,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?)
            """,
            (int(track_id), *values, timestamp, timestamp),
        )
        return candidate_album_id

    existing_album_id = int(existing["canonical_album_id"])
    existing_key = str(existing["canonical_key"])
    may_reassign = (
        existing_album_id == candidate_album_id
        or (
            str(existing["provenance"]).casefold() != "manual"
            and existing_key.startswith("fallback:")
        )
    )
    if not may_reassign:
        return existing_album_id

    current_values = (
        existing_album_id,
        existing["discogs_release_id"],
        existing["edition_label"],
        existing["edition_release_date"],
        existing["track_position"],
        existing["provenance"],
        existing["provider_reference"],
        float(existing["confidence"]) if existing["confidence"] is not None else None,
    )
    if current_values != values:
        conn.execute(
            f"""
            UPDATE {TRACK_ALBUM_MEMBERSHIPS_TABLE} SET
                canonical_album_id=?,discogs_release_id=?,edition_label=?,
                edition_release_date=?,track_position=?,provenance=?,
                provider_reference=?,confidence=?,updated_at=?
            WHERE track_id=?
            """,
            (*values, timestamp, int(track_id)),
        )
    return candidate_album_id


def representative_album_covers(
    conn: sqlite3.Connection,
    canonical_album_ids: Iterable[int] | None = None,
) -> dict[int, str]:
    """Choose representative covers for many albums with one database query."""

    conn.row_factory = sqlite3.Row
    ids = list(dict.fromkeys(int(value) for value in (canonical_album_ids or ())))
    if canonical_album_ids is not None and not ids:
        return {}
    identity_filter = (
        f"AND membership.canonical_album_id IN ({','.join('?' for _ in ids)})"
        if canonical_album_ids is not None
        else ""
    )
    rows = conn.execute(
        f"""
        SELECT membership.canonical_album_id, t.id, t.cover_path,
               field.provenance, field.is_manual, field.is_locked
        FROM {TRACK_ALBUM_MEMBERSHIPS_TABLE} membership
        JOIN tracks t ON t.id=membership.track_id
        LEFT JOIN track_metadata_fields field
          ON field.track_id=t.id AND field.field_name='artwork'
        WHERE NULLIF(TRIM(t.cover_path), '') IS NOT NULL
          {identity_filter}
        ORDER BY membership.canonical_album_id, t.id
        """,
        ids,
    ).fetchall()

    def priority(row: Mapping[str, Any]) -> tuple[int, int]:
        provenance = _display(row["provenance"]).casefold()
        if bool(row["is_manual"]) or bool(row["is_locked"]):
            rank = 0
        elif provenance in {"manual", "embedded", "local", "musicbrainz_confirmed"}:
            rank = 1
        elif "discogs" in provenance:
            rank = 2
        elif provenance in {"cover_art_archive", "coverartarchive", "musicbrainz"}:
            rank = 3
        elif "youtube" in provenance:
            rank = 4
        else:
            rank = 5
        return rank, int(row["id"])

    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        if Path(str(row["cover_path"])).expanduser().is_file():
            grouped.setdefault(int(row["canonical_album_id"]), []).append(row)
    return {
        album_id: str(min(candidates, key=priority)["cover_path"])
        for album_id, candidates in grouped.items()
    }


def representative_album_cover(
    conn: sqlite3.Connection,
    canonical_album_id: int,
) -> str | None:
    """Choose the best valid existing track cover without changing any row/file."""

    return representative_album_covers(conn, (int(canonical_album_id),)).get(
        int(canonical_album_id)
    )


__all__ = [
    "ALBUM_KINDS",
    "ARTIST_ALIAS_KINDS",
    "ARTIST_RELATIONSHIP_KINDS",
    "CANONICAL_ALBUMS_TABLE",
    "TRACK_ALBUM_MEMBERSHIPS_TABLE",
    "ARTIST_ALIASES_TABLE",
    "ARTIST_RELATIONSHIPS_TABLE",
    "CanonicalAlbumIdentity",
    "CanonicalAlbumIdentityConflict",
    "analyze_canonical_album_backfill",
    "canonical_album_identity",
    "classify_album_kind",
    "create_canonical_media_schema",
    "normalize_album_identity",
    "representative_album_cover",
    "representative_album_covers",
    "required_canonical_media_indexes",
    "seed_existing_canonical_albums",
    "split_edition_label",
    "upsert_track_canonical_album",
]
