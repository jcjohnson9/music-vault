from __future__ import annotations

"""Prove the schema-5 to schema-6 migration on a disposable database copy.

The input is treated as an immutable backup.  Its bytes are copied and
verified before the temporary database's media and artwork paths are replaced
with nonexistent paths inside the disposable root.  Only aggregate counts,
booleans, and cryptographic digests are returned; library values are never
printed.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.core import paths as runtime_paths  # noqa: E402
from music_vault.core.app_status import write_app_status  # noqa: E402
from music_vault.core.db import MusicVaultDB  # noqa: E402
from music_vault.metadata.artist_credits import normalize_artist_name  # noqa: E402
from tools.dev import verify_batch10_1_live_migration as live_gate  # noqa: E402


TEMP_PREFIX = "MusicVault_Batch10_2_SourceMigrationProof_"
PRE_SCHEMA_VERSION = 5
POST_SCHEMA_VERSION = 6
EXTENSION_FIELDS = {
    "original_release_date": "original_release_date",
    "version_type": "version_type",
    "version_label": "version_label",
}
FIELD_COLUMNS = (
    "track_id",
    "field_name",
    "value",
    "provenance",
    "provider_reference",
    "confidence",
    "is_manual",
    "is_locked",
    "updated_at",
)
IDENTITY_COLUMNS = (
    "source_kind",
    "external_track_id",
    "track_id",
    "first_seen_at",
    "updated_at",
)
PROVIDER_STATE_TABLES = (
    "track_metadata_observations",
    "metadata_provider_cache",
    "metadata_intelligence_jobs",
    "metadata_intelligence_items",
    "track_release_context",
)
AUTOMATIC_BACKUP_PATTERN = re.compile(
    r"music_vault_pre_schema_v6_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"
    r"(?:_\d+)?\.sqlite3"
)


class ProofFailure(RuntimeError):
    """A fail-closed, non-identifying proof failure."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _quoted(value: str) -> str:
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


def _row_digest(row: Sequence[object]) -> str:
    digest = hashlib.sha256()
    for value in row:
        encoded = _encoded_value(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _aggregate_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _readonly(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_names(connection: sqlite3.Connection, *, include_internal: bool = False) -> list[str]:
    clause = "" if include_internal else " AND name NOT LIKE 'sqlite_%'"
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'" + clause + " ORDER BY name"
        )
    ]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")]


def _table_digest(
    connection: sqlite3.Connection,
    table: str,
    *,
    columns: Sequence[str] | None = None,
) -> tuple[int, str]:
    selected = list(columns) if columns is not None else _columns(connection, table)
    if not selected:
        return 0, _aggregate_digest(())
    rows = connection.execute(
        "SELECT " + ",".join(_quoted(name) for name in selected) + " FROM " + _quoted(table)
    ).fetchall()
    return len(rows), _aggregate_digest(_row_digest(tuple(row)) for row in rows)


def _logical_database_state(database: Path) -> dict[str, Any]:
    """Return full logical schema/table digests, independent of file headers."""

    connection = _readonly(database)
    try:
        schema_rows = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        schema_digest = _aggregate_digest(_row_digest(tuple(row)) for row in schema_rows)
        tables: dict[str, dict[str, Any]] = {}
        total_rows = 0
        for table in _table_names(connection, include_internal=True):
            count, digest = _table_digest(connection, table)
            tables[table] = {"count": count, "digest": digest}
            total_rows += count
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    combined = _aggregate_digest(
        [f"schema:{schema_digest}", f"version:{user_version}"]
        + [
            hashlib.sha256(
                f"{name}:{state['count']}:{state['digest']}".encode("utf-8")
            ).hexdigest()
            for name, state in sorted(tables.items())
        ]
    )
    return {
        "schema_version": user_version,
        "schema_digest": schema_digest,
        "tables": tables,
        "table_count": len(tables),
        "row_count": total_rows,
        "combined_digest": combined,
    }


def _validate_schema5_backup(path: Path, expected_sha256: str) -> dict[str, Any]:
    expected = str(expected_sha256).strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ProofFailure("expected_sha256_invalid")
    if not path.is_file() or path.stat().st_size <= 0:
        raise ProofFailure("schema5_backup_unavailable")
    digest = _sha256_file(path)
    if digest.casefold() != expected:
        raise ProofFailure("schema5_backup_hash_mismatch")
    connection = _readonly(path)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_issues = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        track_count = int(connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
    finally:
        connection.close()
    if schema != PRE_SCHEMA_VERSION:
        raise ProofFailure("schema5_backup_version_mismatch")
    if integrity.casefold() != "ok" or foreign_key_issues:
        raise ProofFailure("schema5_backup_integrity_failed")
    stat = path.stat()
    return {
        "sha256": digest,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "track_count": track_count,
    }


def _sanitization_guards(connection: sqlite3.Connection) -> dict[str, Any]:
    guards: dict[str, Any] = {}
    for table in _table_names(connection, include_internal=True):
        selected = _columns(connection, table)
        if table == "tracks":
            selected = [name for name in selected if name not in {"path", "cover_path"}]
        count, digest = _table_digest(connection, table, columns=selected)
        guards[table] = {"columns": selected, "count": count, "digest": digest}
    schema = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    return {
        "schema": _aggregate_digest(_row_digest(tuple(row)) for row in schema),
        "tables": guards,
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
    }


def _sanitize_temporary_paths(database: Path, temporary_root: Path) -> dict[str, int]:
    if not _is_within(database, temporary_root):
        raise ProofFailure("temporary_database_scope_violation")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != PRE_SCHEMA_VERSION:
            raise ProofFailure("temporary_database_not_schema5")
        before = _sanitization_guards(connection)
        track_ids = [int(row[0]) for row in connection.execute("SELECT id FROM tracks ORDER BY id")]
        replacements = [
            (str((temporary_root / "isolated_media" / f"track-{track_id}.missing").resolve()), track_id)
            for track_id in track_ids
        ]
        with connection:
            connection.executemany("UPDATE tracks SET path=?,cover_path=NULL WHERE id=?", replacements)
        after = _sanitization_guards(connection)
        if before != after:
            raise ProofFailure("temporary_path_sanitization_changed_nonpath_state")
        rows = connection.execute("SELECT path,cover_path FROM tracks").fetchall()
        safe_paths = all(
            _is_within(Path(str(row[0])), temporary_root)
            and not Path(str(row[0])).exists()
            and row[1] is None
            for row in rows
        )
        distinct_paths = len({str(row[0]) for row in rows}) == len(rows)
        if not safe_paths or not distinct_paths:
            raise ProofFailure("temporary_path_sanitization_failed")
    finally:
        connection.close()
    return {"track_count": len(track_ids), "missing_media_count": len(track_ids)}


@contextlib.contextmanager
def _isolated_runtime_environment(root: Path):
    previous_root = os.environ.get("MUSIC_VAULT_PROJECT_ROOT")
    previous_no_secrets = os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS")
    previous_configured_data = runtime_paths._configured_data_directory
    os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(root)
    os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = "1"
    runtime_paths._configured_data_directory = None
    runtime_paths._resolved_project_root.cache_clear()
    try:
        if runtime_paths.project_root().resolve() != root.resolve():
            raise ProofFailure("temporary_runtime_root_not_resolved")
        if not _is_within(runtime_paths.data_dir(), root):
            raise ProofFailure("temporary_data_scope_violation")
        yield
    finally:
        runtime_paths._configured_data_directory = previous_configured_data
        if previous_root is None:
            os.environ.pop("MUSIC_VAULT_PROJECT_ROOT", None)
        else:
            os.environ["MUSIC_VAULT_PROJECT_ROOT"] = previous_root
        if previous_no_secrets is None:
            os.environ.pop("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", None)
        else:
            os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = previous_no_secrets
        runtime_paths._resolved_project_root.cache_clear()


@contextlib.contextmanager
def _offline_guard():
    attempts = {"count": 0}

    def blocked(*_args, **_kwargs):
        attempts["count"] += 1
        raise ProofFailure("network_access_blocked")

    originals = (
        socket.create_connection,
        socket.getaddrinfo,
        socket.socket.connect,
        urllib.request.urlopen,
    )
    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.socket.connect = blocked
    urllib.request.urlopen = blocked
    try:
        yield attempts
    finally:
        (
            socket.create_connection,
            socket.getaddrinfo,
            socket.socket.connect,
            urllib.request.urlopen,
        ) = originals


def _identity_rows(connection: sqlite3.Connection) -> dict[tuple[str, str], tuple[object, ...]]:
    columns = set(_columns(connection, "source_track_identities"))
    if not set(IDENTITY_COLUMNS) <= columns:
        raise ProofFailure("source_identity_shape_changed")
    rows = connection.execute(
        "SELECT " + ",".join(IDENTITY_COLUMNS) + " FROM source_track_identities"
    ).fetchall()
    result: dict[tuple[str, str], tuple[object, ...]] = {}
    for row in rows:
        values = tuple(row)
        key = (str(values[0]), str(values[1]))
        if key in result:
            raise ProofFailure("duplicate_source_identity")
        result[key] = values
    return result


def _field_rows(connection: sqlite3.Connection) -> dict[tuple[int, str], tuple[object, ...]]:
    columns = set(_columns(connection, "track_metadata_fields"))
    if not set(FIELD_COLUMNS) <= columns:
        raise ProofFailure("metadata_field_shape_changed")
    rows = connection.execute(
        "SELECT " + ",".join(FIELD_COLUMNS) + " FROM track_metadata_fields"
    ).fetchall()
    result: dict[tuple[int, str], tuple[object, ...]] = {}
    for row in rows:
        values = tuple(row)
        key = (int(values[0]), str(values[1]))
        if key in result:
            raise ProofFailure("duplicate_metadata_field")
        result[key] = values
    return result


def _independent_identity_checks(
    current: sqlite3.Connection,
    reference: sqlite3.Connection,
    expected_identity_count: int,
) -> tuple[dict[str, bool], dict[str, int]]:
    before = _identity_rows(reference)
    after = _identity_rows(current)
    exact = before == after
    checks = {
        "identity_count_exact": len(before) == len(after) == expected_identity_count,
        "identity_keys_exact": set(before) == set(after),
        "identity_track_mappings_exact": all(
            after.get(key, (None,) * 5)[2] == row[2] for key, row in before.items()
        ),
        "identity_first_seen_exact": all(
            after.get(key, (None,) * 5)[3] == row[3] for key, row in before.items()
        ),
        "identity_updated_at_exact": all(
            after.get(key, (None,) * 5)[4] == row[4] for key, row in before.items()
        ),
        "identity_rows_byte_identical": exact,
    }
    return checks, {"source_identity_count": len(after)}


def _track_extension_rows(connection: sqlite3.Connection) -> dict[int, dict[str, object]]:
    available = set(_columns(connection, "tracks"))
    selected = [
        name
        for name in ("id", "updated_at", "metadata_updated_at", *EXTENSION_FIELDS.values())
        if name in available
    ]
    rows = connection.execute(
        "SELECT " + ",".join(selected) + " FROM tracks"
    ).fetchall()
    return {
        int(row[0]): {name: row[index] for index, name in enumerate(selected)}
        for row in rows
    }


def _independent_field_checks(
    current: sqlite3.Connection,
    reference: sqlite3.Connection,
    *,
    expected_track_count: int,
    expected_old_field_count: int,
    expected_new_field_count: int,
) -> tuple[dict[str, bool], dict[str, int]]:
    before = _field_rows(reference)
    after = _field_rows(current)
    track_before = _track_extension_rows(reference)
    track_after = _track_extension_rows(current)
    additions = set(after) - set(before)
    expected_additions = {
        (track_id, field_name)
        for track_id in track_before
        for field_name in EXTENSION_FIELDS
    }
    safe_addition_count = 0
    materialized_preserved_count = 0
    for track_id, field_name in expected_additions:
        row = after.get((track_id, field_name))
        if row is None:
            continue
        column = EXTENSION_FIELDS[field_name]
        old_value = track_before[track_id].get(column)
        new_value = track_after[track_id].get(column)
        if old_value == new_value:
            materialized_preserved_count += 1
        expected_provenance = (
            "unknown" if new_value is None or not str(new_value).strip() else "embedded"
        )
        expected_timestamp = (
            track_after[track_id].get("metadata_updated_at")
            or track_after[track_id].get("updated_at")
        )
        timestamp_safe = (
            row[8] == expected_timestamp if expected_timestamp is not None else bool(row[8])
        )
        if (
            row[2] == new_value
            and str(row[3]) == expected_provenance
            and row[4] is None
            and row[5] is None
            and int(row[6]) == 0
            and int(row[7]) == 0
            and timestamp_safe
        ):
            safe_addition_count += 1
    old_preserved_count = sum(after.get(key) == row for key, row in before.items())
    per_field = {
        field: sum(name == field for _track, name in additions)
        for field in EXTENSION_FIELDS
    }
    checks = {
        "track_count_exact": len(track_before) == len(track_after) == expected_track_count,
        "old_field_count_exact": len(before) == expected_old_field_count,
        "all_old_field_rows_byte_identical": old_preserved_count == len(before),
        "new_field_count_exact": len(additions) == expected_new_field_count,
        "new_fields_are_exact_extension_set": additions == expected_additions,
        "new_fields_are_safe_inert_defaults": safe_addition_count == len(expected_additions),
        "materialized_date_and_version_values_preserved": (
            materialized_preserved_count == len(expected_additions)
        ),
        "no_field_rows_removed": set(before) <= set(after),
        "no_unexpected_field_rows": set(after) == set(before) | expected_additions,
    }
    return checks, {
        "old_field_count": len(before),
        "old_field_preserved_count": old_preserved_count,
        "new_field_count": len(additions),
        "safe_new_field_count": safe_addition_count,
        "new_original_release_date_count": per_field["original_release_date"],
        "new_version_type_count": per_field["version_type"],
        "new_version_label_count": per_field["version_label"],
    }


def _independent_artist_checks(
    current: sqlite3.Connection,
    reference: sqlite3.Connection,
) -> tuple[dict[str, bool], dict[str, int]]:
    before_tracks = {
        int(row[0]): row[1] for row in reference.execute("SELECT id,artist FROM tracks")
    }
    after_tracks = {
        int(row[0]): row[1] for row in current.execute("SELECT id,artist FROM tracks")
    }
    baseline_artists = {
        track_id: str(value)
        for track_id, value in before_tracks.items()
        if value is not None and str(value).strip()
    }
    expected_normalized: dict[int, str] = {}
    normalization_failed = False
    for track_id, display in baseline_artists.items():
        try:
            expected_normalized[track_id] = normalize_artist_name(display)
        except ValueError:
            normalization_failed = True
    rows = current.execute(
        """
        SELECT c.track_id,c.artist_id,c.role,c.credit_order,c.join_phrase,
               c.provenance,a.display_name,a.normalized_name
        FROM track_artist_credits c
        JOIN artists a ON a.id=c.artist_id
        ORDER BY c.track_id,c.credit_order,c.id
        """
    ).fetchall()
    by_track: dict[int, list[sqlite3.Row]] = {}
    normalized_to_ids: dict[str, set[int]] = {}
    entity_display_normalized = True
    for row in rows:
        by_track.setdefault(int(row[0]), []).append(row)
        normalized_to_ids.setdefault(str(row[7]), set()).add(int(row[1]))
        try:
            if normalize_artist_name(row[6]) != str(row[7]):
                entity_display_normalized = False
        except ValueError:
            entity_display_normalized = False
    valid_credit_count = 0
    normalized_match_count = 0
    for track_id, normalized in expected_normalized.items():
        credits = by_track.get(track_id, [])
        if len(credits) != 1:
            continue
        credit = credits[0]
        if (
            str(credit[2]) == "primary"
            and int(credit[3]) == 0
            and str(credit[4]) == ""
            and bool(str(credit[5] or "").strip())
        ):
            valid_credit_count += 1
        if str(credit[7]) == normalized:
            normalized_match_count += 1
    unreferenced = int(
        current.execute(
            "SELECT COUNT(*) FROM artists a WHERE NOT EXISTS "
            "(SELECT 1 FROM track_artist_credits c WHERE c.artist_id=a.id)"
        ).fetchone()[0]
    )
    artist_rows = current.execute("SELECT id,normalized_name FROM artists").fetchall()
    known_normalized = set(expected_normalized.values())
    checks = {
        "track_artist_display_strings_exact": before_tracks == after_tracks,
        "artist_normalization_succeeded": not normalization_failed,
        "one_conservative_credit_per_nonempty_artist": (
            len(rows) == len(baseline_artists)
            and valid_credit_count == len(baseline_artists)
            and set(by_track) == set(baseline_artists)
        ),
        "credit_normalized_identity_matches_display": (
            normalized_match_count == len(baseline_artists)
        ),
        "normalized_fallback_entities_reused": (
            all(len(ids) == 1 for ids in normalized_to_ids.values())
            and len(artist_rows) == len(known_normalized)
        ),
        "ampersands_not_split": len(rows) == len(baseline_artists),
        "no_label_or_unrelated_artist_fabricated": (
            not unreferenced
            and all(str(row[1]) in known_normalized for row in artist_rows)
            and entity_display_normalized
        ),
    }
    return checks, {
        "nonempty_artist_string_count": len(baseline_artists),
        "artist_credit_count": len(rows),
        "artist_entity_count": len(artist_rows),
        "unreferenced_artist_count": unreferenced,
    }


def _provider_state(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    tables = set(_table_names(connection))
    result: dict[str, dict[str, Any]] = {}
    for table in PROVIDER_STATE_TABLES:
        if table not in tables:
            result[table] = {"exists": False, "count": 0, "digest": _aggregate_digest(())}
        else:
            count, digest = _table_digest(connection, table)
            result[table] = {"exists": True, "count": count, "digest": digest}
    return result


def _independent_database_checks(
    database: Path,
    reference_database: Path,
    *,
    expected_track_count: int,
    expected_identity_count: int,
    expected_old_field_count: int,
    expected_new_field_count: int,
) -> tuple[dict[str, bool], dict[str, int]]:
    current = _readonly(database)
    reference = _readonly(reference_database)
    try:
        identity_checks, identity_counts = _independent_identity_checks(
            current, reference, expected_identity_count
        )
        field_checks, field_counts = _independent_field_checks(
            current,
            reference,
            expected_track_count=expected_track_count,
            expected_old_field_count=expected_old_field_count,
            expected_new_field_count=expected_new_field_count,
        )
        artist_checks, artist_counts = _independent_artist_checks(current, reference)
        provider_before = _provider_state(reference)
        provider_after = _provider_state(current)
        index_names = {
            str(row[0])
            for row in current.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        required_indexes = set(live_gate.V6_REQUIRED_INDEXES)
        schema = int(current.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(current.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys_enabled = int(current.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        foreign_key_issues = len(current.execute("PRAGMA foreign_key_check").fetchall())
        tracks = current.execute("SELECT path,cover_path FROM tracks").fetchall()
        jobs = provider_after["metadata_intelligence_jobs"]["count"]
        items = provider_after["metadata_intelligence_items"]["count"]
        cache_before = provider_before["metadata_provider_cache"]
        cache_after = provider_after["metadata_provider_cache"]
        observations_before = provider_before["track_metadata_observations"]
        observations_after = provider_after["track_metadata_observations"]
    finally:
        reference.close()
        current.close()
    checks = {
        **identity_checks,
        **field_checks,
        **artist_checks,
        "schema_is_6": schema == POST_SCHEMA_VERSION,
        "integrity_ok": integrity.casefold() == "ok",
        "foreign_keys_enabled": foreign_keys_enabled,
        "foreign_key_check_clean": foreign_key_issues == 0,
        "required_indexes_present": required_indexes <= index_names,
        "provider_observations_unchanged": observations_before == observations_after,
        "provider_cache_unchanged": cache_before == cache_after,
        "no_intelligence_jobs_or_items": jobs == items == 0,
        "temporary_media_and_covers_absent": all(
            not Path(str(row[0])).exists() and row[1] is None for row in tracks
        ),
    }
    counts = {
        **identity_counts,
        **field_counts,
        **artist_counts,
        "track_count": len(tracks),
        "intelligence_job_count": jobs,
        "intelligence_item_count": items,
        "provider_cache_count": int(cache_after["count"]),
        "network_provider_observation_count": int(observations_after["count"]),
        "missing_required_index_count": len(required_indexes - index_names),
        "foreign_key_issue_count": foreign_key_issues,
    }
    return checks, counts


def run_source_migration_proof(
    *,
    schema5_backup: str | Path,
    expected_sha256: str,
    expected_track_count: int,
    expected_identity_count: int,
    expected_old_field_count: int,
    expected_new_field_count: int,
    temporary_parent: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(schema5_backup).expanduser().resolve()
    source_guard = _validate_schema5_backup(source, expected_sha256)
    for value, name in (
        (expected_track_count, "expected_track_count"),
        (expected_identity_count, "expected_identity_count"),
        (expected_old_field_count, "expected_old_field_count"),
        (expected_new_field_count, "expected_new_field_count"),
    ):
        if int(value) < 0:
            raise ProofFailure(name + "_invalid")
    parent = Path(temporary_parent).expanduser().resolve() if temporary_parent else None
    temporary_root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent)).resolve()
    if _is_within(temporary_root, PROJECT_ROOT):
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise ProofFailure("temporary_root_inside_repository")
    try:
        (temporary_root / "music_vault").mkdir()
        (temporary_root / "run.py").write_text(
            "# disposable Batch 10.2 migration proof marker\n", encoding="utf-8"
        )
        data_dir = temporary_root / "data"
        data_dir.mkdir()
        database = data_dir / "music_vault.sqlite3"
        shutil.copy2(source, database)
        copied_sha = _sha256_file(database)
        if copied_sha.casefold() != source_guard["sha256"].casefold():
            raise ProofFailure("pristine_copy_hash_mismatch")
        sanitized = _sanitize_temporary_paths(database, temporary_root)
        if sanitized["track_count"] != int(expected_track_count):
            raise ProofFailure("sanitized_track_count_mismatch")

        with _isolated_runtime_environment(temporary_root), _offline_guard() as network:
            baseline = live_gate.capture_baseline(
                project_root=temporary_root,
                data_dir=data_dir,
                database=database,
            )
            if int(baseline["database"]["schema_version"]) != PRE_SCHEMA_VERSION:
                raise ProofFailure("sanitized_baseline_not_schema5")
            if int(baseline["database"]["track_count"]) != int(expected_track_count):
                raise ProofFailure("baseline_track_count_mismatch")
            if int(baseline["media"]["count"]) != 0:
                raise ProofFailure("temporary_media_unexpectedly_exists")

            explicit_backup = data_dir / "backups" / "explicit-schema5-reference.sqlite3"
            explicit_result = live_gate.create_verified_backup(
                database=database,
                backup_dir=data_dir / "backups",
                baseline=baseline,
                backup_path=explicit_backup,
            )
            if not explicit_result.get("ok") or not explicit_result.get("verified"):
                raise ProofFailure("explicit_schema5_reference_failed")

            migrated = MusicVaultDB(database, backup_dir=data_dir / "backups")
            automatic_backup = migrated.last_migration_backup
            try:
                status_file = write_app_status(migrated, {})
            finally:
                migrated.close()

            verifier_first = live_gate.verify_migration(
                baseline=baseline,
                project_root=temporary_root,
                data_dir=data_dir,
                database=database,
                backup_path=explicit_backup,
            )
            independent_checks, counts = _independent_database_checks(
                database,
                explicit_backup,
                expected_track_count=int(expected_track_count),
                expected_identity_count=int(expected_identity_count),
                expected_old_field_count=int(expected_old_field_count),
                expected_new_field_count=int(expected_new_field_count),
            )
            automatic_candidates = [
                path
                for path in (data_dir / "backups").glob("*.sqlite3")
                if AUTOMATIC_BACKUP_PATTERN.fullmatch(path.name)
            ]
            automatic_verified = bool(
                automatic_backup is not None
                and automatic_backup in automatic_candidates
                and live_gate._verify_backup(automatic_backup, baseline)
            )
            logical_first = _logical_database_state(database)
            backup_names_first = {
                path.name for path in (data_dir / "backups").glob("*.sqlite3")
            }

            reopened = MusicVaultDB(database, backup_dir=data_dir / "backups")
            reopened_backup_absent = reopened.last_migration_backup is None
            reopened.close()
            logical_second = _logical_database_state(database)
            backup_names_second = {
                path.name for path in (data_dir / "backups").glob("*.sqlite3")
            }
            verifier_second = live_gate.verify_migration(
                baseline=baseline,
                project_root=temporary_root,
                data_dir=data_dir,
                database=database,
                backup_path=explicit_backup,
            )

            credentials_absent = all(
                not (data_dir / name).exists()
                for name in ("youtube_api_key.txt", "discogs_token.txt")
            )
            checks = {
                "input_backup_hash_exact": copied_sha.casefold()
                == str(expected_sha256).strip().casefold(),
                "input_backup_track_count_exact": source_guard["track_count"]
                == int(expected_track_count),
                "temporary_root_outside_repository": not _is_within(
                    temporary_root, PROJECT_ROOT
                ),
                "all_runtime_writes_under_temporary_root": all(
                    _is_within(path, temporary_root)
                    for path in (
                        database,
                        explicit_backup,
                        data_dir / "backups",
                        status_file,
                        *(automatic_candidates or ()),
                    )
                ),
                "temporary_paths_sanitized_before_baseline": (
                    sanitized["missing_media_count"] == int(expected_track_count)
                    and int(baseline["media"]["count"]) == 0
                    and int(baseline["media"]["missing_count"])
                    == int(expected_track_count)
                ),
                "explicit_schema5_reference_created_and_verified": bool(
                    explicit_result.get("verified")
                ),
                "corrected_live_migration_verifier_passed": bool(
                    verifier_first.get("ok")
                ),
                "preexisting_tables_and_counts_preserved": bool(
                    verifier_first.get("checks", {}).get(
                        "all_preexisting_table_rows_and_values_preserved"
                    )
                ),
                "corrected_live_migration_verifier_passed_after_reopen": bool(
                    verifier_second.get("ok")
                ),
                "automatic_schema5_backup_created_and_verified": (
                    len(automatic_candidates) == 1 and automatic_verified
                ),
                "schema6_reopen_created_no_backup": reopened_backup_absent,
                "schema6_reopen_created_no_extra_backup": (
                    backup_names_first == backup_names_second
                ),
                "schema6_reopen_logically_idempotent": logical_first == logical_second,
                "credentials_not_copied": credentials_absent,
                "network_and_provider_calls_blocked_and_absent": network["count"] == 0,
                "source_backup_unchanged": (
                    _sha256_file(source) == source_guard["sha256"]
                    and int(source.stat().st_size) == source_guard["size"]
                    and int(source.stat().st_mtime_ns) == source_guard["mtime_ns"]
                ),
                **independent_checks,
            }
            result = {
                "ok": all(checks.values()),
                "checks": checks,
                "counts": {
                    **counts,
                    "automatic_backup_count": len(automatic_candidates),
                    "total_temporary_backup_count": len(backup_names_second),
                    "network_attempt_count": int(network["count"]),
                    "logical_table_count": int(logical_second["table_count"]),
                    "logical_row_count": int(logical_second["row_count"]),
                },
                "digests": {
                    "verified_input_sha256": source_guard["sha256"],
                    "post_migration_logical_sha256": logical_second["combined_digest"],
                    "post_migration_schema_sha256": logical_second["schema_digest"],
                },
            }
            return result
    finally:
        shutil.rmtree(temporary_root)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema5-backup", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-track-count", type=int, default=304)
    parser.add_argument("--expected-identity-count", type=int, default=304)
    parser.add_argument("--expected-old-field-count", type=int, default=1824)
    parser.add_argument("--expected-new-field-count", type=int, default=912)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_source_migration_proof(
            schema5_backup=args.schema5_backup,
            expected_sha256=args.expected_sha256,
            expected_track_count=args.expected_track_count,
            expected_identity_count=args.expected_identity_count,
            expected_old_field_count=args.expected_old_field_count,
            expected_new_field_count=args.expected_new_field_count,
        )
    except (ProofFailure, live_gate.GateFailure, OSError, sqlite3.Error, ValueError, TypeError):
        print(
            json.dumps(
                {"error_code": "batch10_2_source_migration_proof_failed", "ok": False}
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
