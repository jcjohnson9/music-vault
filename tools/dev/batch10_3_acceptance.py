from __future__ import annotations

"""Aggregate-only preservation primitives for the Batch 10.3 gates.

The helpers in this module deliberately return counts, booleans and one-way
digests only.  They never return library strings, provider identifiers, media
paths, or credential contents.  Credential files are inspected with ``stat``
only; media files are likewise checked for size and modification time without
opening their content.
"""

import contextlib
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PRE_SCHEMA_VERSION = 6
POST_SCHEMA_VERSION = 7
BASELINE_FORMAT_VERSION = 2
POST_MIGRATION_SEMANTIC_FORMAT_VERSION = 1
NETWORK_REPORT_FORMAT_VERSION = 2
NO_SECRETS_ENVIRONMENT = "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"

V7_TABLES = frozenset(
    {
        "canonical_albums",
        "track_album_memberships",
        "artist_aliases",
        "artist_relationships",
    }
)

# These schema-6 tables may change in a narrowly defined way during the
# stored-evidence-only migration.  Every other pre-existing table must remain
# byte-for-byte logically equivalent.
MUTABLE_TABLES = frozenset(
    {
        "tracks",
        "artists",
        "track_artist_credits",
        "track_metadata_fields",
        "track_metadata_history",
        # The schema-7 stored-evidence migration may materialize conservative
        # derived observations.  Their exact semantic result is bound to the
        # disposable clone (timestamps/row IDs excluded) below.
        "track_metadata_observations",
        # Schema 7 adds two nullable release-family identity columns to this
        # otherwise protected schema-6 table.  Its pre-existing columns are
        # guarded separately below so the additive migration cannot disguise
        # a change to established release context.
        "track_release_context",
        "metadata_intelligence_jobs",
        "metadata_intelligence_items",
        # Version-identity repair records one confirmed local observation. The
        # exact dry-run post-state guards all resulting rows, while a separate
        # opaque subset guard below proves every prior provider row survived.
        "track_metadata_observations",
        "metadata_provider_cache",
        "sqlite_sequence",
    }
)

RELEASE_CONTEXT_ADDITIVE_COLUMNS = frozenset(
    {
        "musicbrainz_release_group_id",
        "provider_release_family_id",
    }
)

TRACK_MUTABLE_COLUMNS = frozenset(
    {
        "title",
        "artist",
        "version_type",
        "version_label",
        "metadata_updated_at",
        "updated_at",
    }
)

INTELLIGENCE_ITEM_MUTABLE_COLUMNS = frozenset(
    {
        "state",
        "review_reason",
        "applied_history_group",
        "completed_at",
        "updated_at",
    }
)

INTELLIGENCE_JOB_MUTABLE_COLUMNS = frozenset(
    {
        "status",
        "analyzed_items",
        "applied_items",
        "review_items",
        "failed_items",
        "applied_with_gaps_items",
        "source_fallback_items",
        "completed_at",
        "updated_at",
    }
)

REQUIRED_V7_INDEXES = frozenset(
    {
        "idx_canonical_albums_identity",
        "idx_canonical_albums_discogs_master",
        "idx_canonical_albums_mb_release_group",
        "idx_canonical_albums_provider_family",
        "idx_release_context_mb_release_group",
        "idx_release_context_provider_family",
        "idx_track_album_memberships_album",
        "idx_track_album_memberships_discogs_release",
        "idx_artist_aliases_normalized",
        "idx_artist_aliases_artist",
        "idx_artist_relationships_subject",
        "idx_artist_relationships_related",
    }
)

PROVIDER_TABLES = frozenset(
    {
        "track_metadata_observations",
        "metadata_provider_cache",
    }
)

PRESERVATION_COUNT_TABLES = frozenset(
    {
        "tracks",
        "playlists",
        "playlist_tracks",
        "playlist_origins",
        "sync_sources",
        "sync_source_items",
        "track_source_memberships",
        "source_track_identities",
        "source_identity_conflicts",
        "track_metadata_history",
        "metadata_remediation_jobs",
        "metadata_remediation_items",
        "metadata_intelligence_jobs",
        "metadata_intelligence_items",
    }
)

RUNTIME_GUARD_FILES = {
    "config": "music_vault_config.json",
    "download_archive": "youtube_download_archive.txt",
    "failed_ids": "youtube_failed_ids.txt",
}

CREDENTIAL_FILE_NAMES = {
    "youtube_api_key": "youtube_api_key.txt",
    "discogs_token": "discogs_token.txt",
}


class AcceptanceFailure(RuntimeError):
    """A stable, non-identifying acceptance-gate failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _encoded_value(value: object) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bytes):
        return b"B" + value
    if isinstance(value, float):
        return b"F" + value.hex().encode("ascii")
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii")
    return b"T" + str(value).encode("utf-8", errors="surrogatepass")


def row_digest(row: Sequence[object]) -> str:
    digest = hashlib.sha256()
    for value in row:
        encoded = _encoded_value(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def aggregate_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def readonly(database: Path, *, immutable: bool = True) -> sqlite3.Connection:
    path = Path(database).expanduser().resolve()
    suffix = "&immutable=1" if immutable else ""
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro{suffix}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def table_names(connection: sqlite3.Connection, *, include_internal: bool = False) -> list[str]:
    clause = "" if include_internal else " AND name NOT LIKE 'sqlite_%'"
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'" + clause + " ORDER BY name"
        )
    ]


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
    ]


def table_guard(
    connection: sqlite3.Connection,
    table: str,
    *,
    selected_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    selected = list(selected_columns) if selected_columns is not None else columns(connection, table)
    if not selected:
        return {"count": 0, "columns": [], "digest": aggregate_digest(())}
    rows = connection.execute(
        "SELECT "
        + ",".join(quote_identifier(name) for name in selected)
        + " FROM "
        + quote_identifier(table)
    ).fetchall()
    digests = [row_digest(tuple(row)) for row in rows]
    return {
        "count": len(rows),
        "columns": selected,
        "digest": aggregate_digest(digests),
    }


def stable_table_guard(
    connection: sqlite3.Connection,
    table: str,
    mutable_columns: Iterable[str],
) -> dict[str, Any]:
    ignored = set(mutable_columns)
    selected = [name for name in columns(connection, table) if name not in ignored]
    return table_guard(connection, table, selected_columns=selected)


def query_guard(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[object] = (),
) -> dict[str, Any]:
    rows = connection.execute(query, tuple(parameters)).fetchall()
    return {
        "count": len(rows),
        "digest": aggregate_digest(row_digest(tuple(row)) for row in rows),
    }


def baseline_fingerprint(baseline: Mapping[str, Any]) -> str:
    """Return one opaque identifier for the exact pre-migration logical state."""

    tables = baseline.get("database", {}).get("tables")
    if not isinstance(tables, dict):
        raise AcceptanceFailure("baseline_tables_unavailable")
    encoded = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_post_migration_semantics(connection: sqlite3.Connection) -> dict[str, Any]:
    """Capture the permitted schema-v7 result without emitting library values.

    Timestamps and generated history-group UUIDs are deliberately excluded so
    an isolated clone and a later controlled startup can be compared exactly.
    Entity IDs remain inside one-way aggregate digests because they define
    credit, alias, relationship, and membership linkage.
    """

    guards = {
        "canonical_albums": query_guard(
            connection,
            "SELECT id,canonical_key,title,normalized_title,album_artist_display,"
            "normalized_album_artist,album_kind,discogs_master_id,"
            "musicbrainz_release_group_id,provider_release_family_id,"
            "original_release_date FROM canonical_albums",
        ),
        "canonical_album_memberships": query_guard(
            connection,
            "SELECT track_id,canonical_album_id,discogs_release_id,edition_label,"
            "edition_release_date,track_position,disc_number,provenance,"
            "provider_reference,confidence FROM track_album_memberships",
        ),
        "artist_identities": query_guard(
            connection,
            "SELECT id,display_name,normalized_name,sort_name,entity_type,"
            "discogs_artist_id,musicbrainz_artist_id FROM artists",
        ),
        "artist_aliases": query_guard(
            connection,
            "SELECT artist_id,alias_name,normalized_alias,alias_kind,provenance,"
            "provider_reference,confidence FROM artist_aliases",
        ),
        "artist_relationships": query_guard(
            connection,
            "SELECT subject_artist_id,related_artist_id,relationship_kind,provenance,"
            "provider_reference,confidence FROM artist_relationships",
        ),
        "artist_credits": query_guard(
            connection,
            "SELECT track_id,artist_id,role,credit_order,join_phrase,provenance,"
            "provider_reference,confidence,is_manual,is_locked "
            "FROM track_artist_credits",
        ),
        "permitted_track_fields": query_guard(
            connection,
            "SELECT id,title,artist,version_type,version_label FROM tracks",
        ),
        "critical_metadata_authority": query_guard(
            connection,
            "SELECT track_id,field_name,value,provenance,provider_reference,"
            "confidence,is_manual,is_locked FROM track_metadata_fields "
            "WHERE field_name IN ('title','artist','version_type','version_label')",
        ),
        "metadata_observations": query_guard(
            connection,
            "SELECT observation_key,track_id,provider,field_name,value,"
            "provider_reference,confidence FROM track_metadata_observations",
        ),
        "metadata_history_semantics": query_guard(
            connection,
            "SELECT track_id,field_name,old_value,new_value,old_provenance,"
            "new_provenance,old_provider_reference,new_provider_reference,"
            "old_confidence,new_confidence,old_is_manual,new_is_manual,"
            "old_is_locked,new_is_locked,actor,reason FROM track_metadata_history",
        ),
        "provider_observations": query_guard(
            connection,
            "SELECT observation_key,track_id,provider,field_name,value,"
            "provider_reference,confidence FROM track_metadata_observations",
        ),
        "provider_cache": query_guard(
            connection,
            "SELECT * FROM metadata_provider_cache",
        ),
        "release_family_context": query_guard(
            connection,
            "SELECT track_id,musicbrainz_release_group_id,provider_release_family_id "
            "FROM track_release_context",
        ),
        "review_item_outcomes": query_guard(
            connection,
            "SELECT id,job_id,track_id,state,review_reason FROM metadata_intelligence_items",
        ),
        "review_job_outcomes": query_guard(
            connection,
            "SELECT id,status,total_items,analyzed_items,review_items,applied_items,"
            "applied_with_gaps_items,source_fallback_items,no_match_items,failed_items,"
            "skipped_items,cancel_requested,last_error FROM metadata_intelligence_jobs",
        ),
    }
    return {
        "semantic_format_version": POST_MIGRATION_SEMANTIC_FORMAT_VERSION,
        "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "guards": guards,
        "aggregate_only": True,
    }


def database_health(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "foreign_keys_enabled": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1,
        "foreign_key_issue_count": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        "integrity_ok": str(connection.execute("PRAGMA integrity_check").fetchone()[0]).casefold()
        == "ok",
    }


def _schema_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    return aggregate_digest(row_digest(tuple(row)) for row in rows)


def _index_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _column_digest(connection: sqlite3.Connection, table: str, selected: Sequence[str]) -> str:
    return table_guard(connection, table, selected_columns=selected)["digest"]


def _file_metadata(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False, "size": 0, "mtime_ns": 0}
    if not path.is_file():
        raise AcceptanceFailure("runtime_guard_not_regular_file")
    return {"exists": True, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _media_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute("SELECT id,path FROM tracks ORDER BY id").fetchall()
    records: list[str] = []
    existing = missing = total_bytes = 0
    for row in rows:
        candidate = Path(str(row[1])).expanduser()
        try:
            stat = candidate.stat()
        except (FileNotFoundError, OSError):
            missing += 1
            records.append(row_digest((int(row[0]), False, 0, 0)))
            continue
        if not candidate.is_file():
            missing += 1
            records.append(row_digest((int(row[0]), False, 0, 0)))
            continue
        existing += 1
        total_bytes += int(stat.st_size)
        records.append(row_digest((int(row[0]), True, int(stat.st_size), int(stat.st_mtime_ns))))
    return {
        "track_path_count": len(rows),
        "existing_media_count": existing,
        "missing_media_count": missing,
        "total_media_bytes": total_bytes,
        "metadata_digest": aggregate_digest(records),
    }


def _private_path_digest(path: Path) -> str:
    normalized = os.path.normcase(str(path.expanduser().resolve(strict=False)))
    return hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()


def _referenced_cover_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    values = {
        str(row[0]).strip()
        for row in connection.execute(
            "SELECT cover_path FROM tracks WHERE NULLIF(TRIM(cover_path),'') IS NOT NULL"
        ).fetchall()
        if str(row[0] or "").strip()
    }
    records: list[str] = []
    existing = missing = total_bytes = 0
    for value in values:
        candidate = Path(value).expanduser()
        path_digest = _private_path_digest(candidate)
        try:
            stat = candidate.stat()
            if not candidate.is_file():
                raise FileNotFoundError
            content_digest = sha256_file(candidate)
        except (FileNotFoundError, OSError):
            missing += 1
            records.append(row_digest((path_digest, False, 0, 0, None)))
            continue
        existing += 1
        total_bytes += int(stat.st_size)
        records.append(
            row_digest(
                (
                    path_digest,
                    True,
                    int(stat.st_size),
                    int(stat.st_mtime_ns),
                    content_digest,
                )
            )
        )
    return {
        "referenced_path_count": len(values),
        "existing_file_count": existing,
        "missing_file_count": missing,
        "total_bytes": total_bytes,
        "inventory_digest": aggregate_digest(records),
    }


def _private_tree_inventory(root: Path) -> dict[str, Any]:
    directory = Path(root).expanduser()
    if not directory.exists():
        return {
            "exists": False,
            "file_count": 0,
            "total_bytes": 0,
            "inventory_digest": aggregate_digest(()),
        }
    if directory.is_symlink() or not directory.is_dir():
        raise AcceptanceFailure("private_artwork_tree_invalid")
    records: list[str] = []
    total_bytes = 0
    file_count = 0
    for candidate in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            raise AcceptanceFailure("private_artwork_tree_entry_invalid")
        if not candidate.is_file():
            continue
        relative_digest = hashlib.sha256(
            candidate.relative_to(directory).as_posix().encode("utf-8")
        ).hexdigest()
        stat = candidate.stat()
        content_digest = sha256_file(candidate)
        file_count += 1
        total_bytes += int(stat.st_size)
        records.append(
            row_digest(
                (
                    relative_digest,
                    int(stat.st_size),
                    int(stat.st_mtime_ns),
                    content_digest,
                )
            )
        )
    return {
        "exists": True,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "inventory_digest": aggregate_digest(records),
    }


def _review_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = set(table_names(connection))
    if "metadata_intelligence_items" not in tables:
        return {}
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT state,COUNT(*) FROM metadata_intelligence_items GROUP BY state ORDER BY state"
        )
    }


def _legacy_album_card_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT 1 FROM tracks WHERE NULLIF(TRIM(album),'') IS NOT NULL "
            "GROUP BY LOWER(TRIM(album)),LOWER(TRIM(COALESCE(NULLIF(album_artist,''),artist,''))),"
            "COALESCE(NULLIF(TRIM(year),''),NULLIF(SUBSTR(release_date,1,4),''),'')"
            ")"
        ).fetchone()[0]
    )


def _artist_card_count(connection: sqlite3.Connection) -> int:
    tables = set(table_names(connection))
    if "artists" not in tables:
        return 0
    return int(
        connection.execute(
            "SELECT COUNT(DISTINCT a.id) FROM artists a "
            "JOIN track_artist_credits c ON c.artist_id=a.id"
        ).fetchone()[0]
    )


def _backup_inventory(data_dir: Path) -> list[dict[str, Any]]:
    directory = data_dir / "backups"
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.sqlite3"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise AcceptanceFailure("backup_inventory_entry_invalid")
        stat = path.stat()
        result.append(
            {
                "name_digest": hashlib.sha256(path.name.encode("utf-8")).hexdigest(),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return result


def capture_database_baseline(
    *,
    project_root: Path,
    data_dir: Path,
    database: Path,
    expected_schema: int = PRE_SCHEMA_VERSION,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    data = Path(data_dir).expanduser().resolve()
    db_path = Path(database).expanduser().resolve()
    if not is_within(data, root) or not is_within(db_path, data) or not db_path.is_file():
        raise AcceptanceFailure("live_database_scope_invalid")
    sidecars = [Path(str(db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(path.exists() for path in sidecars):
        raise AcceptanceFailure("sqlite_sidecar_present")
    with contextlib.closing(readonly(db_path, immutable=False)) as connection:
        health = database_health(connection)
        if health != {
            "schema_version": int(expected_schema),
            "foreign_keys_enabled": True,
            "foreign_key_issue_count": 0,
            "integrity_ok": True,
        }:
            raise AcceptanceFailure("database_health_failed")
        names = table_names(connection, include_internal=True)
        guards = {name: table_guard(connection, name) for name in names}
        track_columns = columns(connection, "tracks")
        track_id_guard = table_guard(connection, "tracks", selected_columns=["id"])
        track_stable_guard = stable_table_guard(connection, "tracks", TRACK_MUTABLE_COLUMNS)
        release_context_stable_guard = stable_table_guard(
            connection,
            "track_release_context",
            RELEASE_CONTEXT_ADDITIVE_COLUMNS,
        )
        cover_guard = _column_digest(connection, "tracks", ["id", "cover_path"])
        path_guard = _column_digest(connection, "tracks", ["id", "path"])
        provider_guards = {
            name: guards[name] for name in sorted(PROVIDER_TABLES & set(names))
        }
        provider_row_hashes: dict[str, list[str]] = {}
        for name in sorted(PROVIDER_TABLES & set(names)):
            selected = columns(connection, name)
            if name == "track_metadata_observations":
                selected = [value for value in selected if value != "observed_at"]
            provider_row_hashes[name] = sorted(
                row_digest(tuple(row))
                for row in connection.execute(
                    f"SELECT {','.join(quote_identifier(value) for value in selected)} "
                    f"FROM {quote_identifier(name)}"
                ).fetchall()
            )
        protected_tables = {
            name: guard
            for name, guard in guards.items()
            if name not in MUTABLE_TABLES
        }
        intelligence_stable = {}
        if "metadata_intelligence_items" in names:
            intelligence_stable["items"] = stable_table_guard(
                connection,
                "metadata_intelligence_items",
                INTELLIGENCE_ITEM_MUTABLE_COLUMNS,
            )
        if "metadata_intelligence_jobs" in names:
            intelligence_stable["jobs"] = stable_table_guard(
                connection,
                "metadata_intelligence_jobs",
                INTELLIGENCE_JOB_MUTABLE_COLUMNS,
            )
        history_rows = []
        if "track_metadata_history" in names:
            history_rows = [
                row_digest(tuple(row))
                for row in connection.execute(
                    "SELECT * FROM track_metadata_history ORDER BY id"
                ).fetchall()
            ]
        metadata_field_keys: list[str] = []
        protected_metadata_fields = {"count": 0, "digest": aggregate_digest(())}
        manual_locked_metadata_fields = {"count": 0, "digest": aggregate_digest(())}
        if "track_metadata_fields" in names:
            metadata_field_keys = [
                row_digest((row[0], row[1]))
                for row in connection.execute(
                    "SELECT track_id,field_name FROM track_metadata_fields "
                    "ORDER BY track_id,field_name"
                ).fetchall()
            ]
            protected_metadata_fields = query_guard(
                connection,
                "SELECT * FROM track_metadata_fields "
                "WHERE field_name NOT IN ('title','artist','version_type','version_label') "
                "ORDER BY track_id,field_name",
            )
            manual_locked_metadata_fields = query_guard(
                connection,
                "SELECT track_id,field_name,value,provenance,provider_reference,"
                "confidence,is_manual,is_locked FROM track_metadata_fields "
                "WHERE is_manual=1 OR is_locked=1 ORDER BY track_id,field_name",
            )
        credit_semantics: list[str] = []
        protected_credit_semantics: list[str] = []
        credited_track_hashes: list[str] = []
        if "track_artist_credits" in names:
            credit_semantics = sorted(
                {
                    row_digest(tuple(row))
                    for row in connection.execute(
                        "SELECT track_id,role,credit_order,join_phrase,provenance,"
                        "provider_reference,confidence,is_manual,is_locked "
                        "FROM track_artist_credits ORDER BY track_id,credit_order,id"
                    ).fetchall()
                }
            )
            protected_credit_semantics = sorted(
                {
                    row_digest(tuple(row))
                    for row in connection.execute(
                        "SELECT track_id,role,credit_order,join_phrase,provenance,"
                        "provider_reference,confidence,is_manual,is_locked "
                        "FROM track_artist_credits WHERE is_manual=1 OR is_locked=1 "
                        "ORDER BY track_id,credit_order,id"
                    ).fetchall()
                }
            )
            credited_track_hashes = sorted(
                {
                    row_digest((int(row[0]),))
                    for row in connection.execute(
                        "SELECT DISTINCT track_id FROM track_artist_credits ORDER BY track_id"
                    ).fetchall()
                }
            )
        artist_provider_id_guard = {"count": 0, "digest": aggregate_digest(())}
        if "artists" in names:
            artist_provider_id_guard = query_guard(
                connection,
                "SELECT provider_kind,provider_id FROM ("
                "SELECT 'discogs' AS provider_kind,discogs_artist_id AS provider_id FROM artists "
                "WHERE NULLIF(TRIM(discogs_artist_id),'') IS NOT NULL UNION ALL "
                "SELECT 'musicbrainz',musicbrainz_artist_id FROM artists "
                "WHERE NULLIF(TRIM(musicbrainz_artist_id),'') IS NOT NULL"
                ") ORDER BY provider_kind,provider_id",
            )
        counts = {
            name: int(guards[name]["count"])
            for name in sorted(PRESERVATION_COUNT_TABLES & set(names))
        }
        database_state = {
            "health": health,
            "schema_digest": _schema_digest(connection),
            "indexes": _index_names(connection),
            "tables": guards,
            "protected_tables": protected_tables,
            "track_id_guard": track_id_guard,
            "track_stable_guard": track_stable_guard,
            "track_release_context_stable_guard": release_context_stable_guard,
            "track_path_digest": path_guard,
            "track_cover_path_digest": cover_guard,
            "provider_guards": provider_guards,
            "provider_row_hashes": provider_row_hashes,
            "intelligence_stable": intelligence_stable,
            "metadata_history_row_hashes": history_rows,
            "metadata_field_key_hashes": metadata_field_keys,
            "protected_metadata_field_guard": protected_metadata_fields,
            "manual_locked_metadata_field_guard": manual_locked_metadata_fields,
            "artist_credit_semantic_hashes": credit_semantics,
            "protected_artist_credit_semantic_hashes": protected_credit_semantics,
            "credited_track_hashes": credited_track_hashes,
            "artist_provider_id_guard": artist_provider_id_guard,
            "preservation_counts": counts,
            "legacy_album_card_count": _legacy_album_card_count(connection),
            "artist_card_count": _artist_card_count(connection),
            "eligible_album_track_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM tracks WHERE NULLIF(TRIM(album),'') IS NOT NULL"
                ).fetchone()[0]
            ),
            "blank_artist_display_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM tracks WHERE NULLIF(TRIM(artist),'') IS NULL"
                ).fetchone()[0]
            ),
            "review_counts": _review_counts(connection),
            "track_column_count": len(track_columns),
        }
        media = _media_metadata(connection)
        referenced_covers = _referenced_cover_inventory(connection)
    return {
        "baseline_format_version": BASELINE_FORMAT_VERSION,
        "database": database_state,
        "media": media,
        "artwork": {
            "referenced_cover_files": referenced_covers,
            "artist_image_tree": _private_tree_inventory(data / "artist_images"),
        },
        "runtime_guards": {
            key: _file_metadata(data / filename)
            for key, filename in RUNTIME_GUARD_FILES.items()
        },
        "credential_metadata": {
            key: _file_metadata(data / filename)
            for key, filename in CREDENTIAL_FILE_NAMES.items()
        },
        "backup_inventory": _backup_inventory(data),
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
        "artwork_contents_hashed": True,
    }


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure("acceptance_report_unavailable") from exc
    if not isinstance(value, dict):
        raise AcceptanceFailure("acceptance_report_invalid")
    return value


def verify_acceptance_network_report(path: Path) -> dict[str, Any]:
    """Validate finalized, aggregate-only evidence from the startup guard."""

    payload = read_json(path)
    expected_keys = {
        "schema_version",
        "guard_installed",
        "outbound_blocked",
        "attempt_count",
        "provider_factory_invocation_count",
        "provider_task_dispatch_count",
        "finalized",
        "request_details_recorded",
        "credential_contents_read",
    }
    if set(payload) != expected_keys:
        raise AcceptanceFailure("network_report_shape_invalid")
    if (
        payload.get("schema_version") != NETWORK_REPORT_FORMAT_VERSION
        or payload.get("guard_installed") is not True
        or payload.get("outbound_blocked") is not True
        or payload.get("finalized") is not True
        or payload.get("request_details_recorded") is not False
        or payload.get("credential_contents_read") is not False
    ):
        raise AcceptanceFailure("network_report_policy_failed")
    try:
        attempts = int(payload.get("attempt_count"))
        provider_factories = int(payload.get("provider_factory_invocation_count"))
        provider_tasks = int(payload.get("provider_task_dispatch_count"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AcceptanceFailure("network_report_attempt_count_invalid") from exc
    if attempts != 0 or provider_factories != 0 or provider_tasks != 0:
        raise AcceptanceFailure("provider_or_network_access_observed")
    return {
        "verified": True,
        "guard_installed": True,
        "outbound_blocked": True,
        "attempt_count": attempts,
        "provider_factory_invocation_count": provider_factories,
        "provider_task_dispatch_count": provider_tasks,
        "request_details_recorded": False,
        "credential_contents_read": False,
    }


def verify_sqlite_backup(
    *,
    backup: Path,
    baseline: Mapping[str, Any],
    expected_schema: int = PRE_SCHEMA_VERSION,
) -> dict[str, Any]:
    path = Path(backup).expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise AcceptanceFailure("backup_unavailable")
    with contextlib.closing(readonly(path, immutable=False)) as connection:
        health = database_health(connection)
        guards = {
            name: table_guard(connection, name)
            for name in table_names(connection, include_internal=True)
        }
    if health != {
        "schema_version": int(expected_schema),
        "foreign_keys_enabled": True,
        "foreign_key_issue_count": 0,
        "integrity_ok": True,
    }:
        raise AcceptanceFailure("backup_health_failed")
    if guards != baseline.get("database", {}).get("tables"):
        raise AcceptanceFailure("backup_logical_mismatch")
    return {
        "verified": True,
        "schema_version": int(expected_schema),
        "size": int(path.stat().st_size),
        "sha256": sha256_file(path),
    }


def create_verified_sqlite_backup(
    *,
    database: Path,
    backup: Path,
    baseline: Mapping[str, Any],
    expected_schema: int = PRE_SCHEMA_VERSION,
) -> dict[str, Any]:
    source = Path(database).expanduser().resolve()
    destination_path = Path(backup).expanduser().resolve()
    if source == destination_path or destination_path.exists():
        raise AcceptanceFailure("backup_destination_invalid")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with contextlib.closing(
            readonly(source, immutable=False)
        ) as source_connection:
            destination = sqlite3.connect(destination_path)
            try:
                source_connection.backup(destination)
                destination.commit()
            finally:
                destination.close()
        return verify_sqlite_backup(
            backup=destination_path,
            baseline=baseline,
            expected_schema=expected_schema,
        )
    except Exception:
        if destination_path.exists():
            destination_path.unlink()
        raise


def ensure_no_secret_mode() -> None:
    if os.environ.get(NO_SECRETS_ENVIRONMENT) != "1":
        raise AcceptanceFailure("no_secrets_environment_missing")


__all__ = [
    "AcceptanceFailure",
    "BASELINE_FORMAT_VERSION",
    "CREDENTIAL_FILE_NAMES",
    "INTELLIGENCE_ITEM_MUTABLE_COLUMNS",
    "INTELLIGENCE_JOB_MUTABLE_COLUMNS",
    "MUTABLE_TABLES",
    "NO_SECRETS_ENVIRONMENT",
    "NETWORK_REPORT_FORMAT_VERSION",
    "POST_MIGRATION_SEMANTIC_FORMAT_VERSION",
    "POST_SCHEMA_VERSION",
    "PRE_SCHEMA_VERSION",
    "RELEASE_CONTEXT_ADDITIVE_COLUMNS",
    "PROVIDER_TABLES",
    "REQUIRED_V7_INDEXES",
    "RUNTIME_GUARD_FILES",
    "TRACK_MUTABLE_COLUMNS",
    "V7_TABLES",
    "aggregate_digest",
    "atomic_write_json",
    "baseline_fingerprint",
    "capture_database_baseline",
    "capture_post_migration_semantics",
    "columns",
    "create_verified_sqlite_backup",
    "database_health",
    "ensure_no_secret_mode",
    "is_within",
    "quote_identifier",
    "query_guard",
    "read_json",
    "readonly",
    "row_digest",
    "sha256_file",
    "stable_table_guard",
    "table_guard",
    "table_names",
    "verify_sqlite_backup",
    "verify_acceptance_network_report",
]
