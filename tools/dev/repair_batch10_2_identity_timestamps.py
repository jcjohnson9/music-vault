from __future__ import annotations

"""Fail-closed proof and repair tool for the Batch 10.2 timestamp correction.

The tool never imports Music Vault application code, opens credentials, touches
media, or calls a provider.  Identity values are used only in memory and are
never included in its aggregate JSON reports.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
IDENTITY_TABLE = "source_track_identities"
IDENTITY_KEY_COLUMNS = ("source_kind", "external_track_id")
IDENTITY_MAPPING_COLUMNS = (*IDENTITY_KEY_COLUMNS, "track_id")
IDENTITY_REQUIRED_COLUMNS = (
    *IDENTITY_MAPPING_COLUMNS,
    "first_seen_at",
    "updated_at",
)
EXPECTED_TARGET_SCHEMA = 6
EXPECTED_REFERENCE_SCHEMA = 5
LIVE_ACKNOWLEDGEMENT = "batch10.2-live-identity-timestamp-repair"
NO_SECRETS_ENVIRONMENT = "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"
LIVE_EXPECTED_IDENTITY_COUNT = 304
LIVE_EXPECTED_REPAIR_COUNT = 304


class RepairFailure(RuntimeError):
    """Deliberately aggregate-only failure safe for command-line reporting."""


@dataclass(frozen=True)
class _IdentityComparison:
    report: dict[str, Any]
    reference_by_mapping: Mapping[tuple[object, ...], tuple[object, ...]]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _aggregate_digest(records: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records):
        digest.update(record.encode("ascii", errors="strict"))
        digest.update(b"\n")
    return digest.hexdigest()


def _value_digest(value: object) -> str:
    digest = hashlib.sha256()
    if value is None:
        payload = b"N"
    elif isinstance(value, bytes):
        payload = b"B" + hashlib.sha256(value).digest()
    elif isinstance(value, float):
        payload = b"F" + value.hex().encode("ascii")
    elif isinstance(value, int):
        payload = b"I" + str(value).encode("ascii")
    else:
        payload = b"T" + str(value).encode("utf-8", errors="surrogatepass")
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
    return digest.hexdigest()


def _row_digest(row: Sequence[object]) -> str:
    return _aggregate_digest(
        f"{index:08d}:{_value_digest(value)}" for index, value in enumerate(row)
    )


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _qualified(database: str, object_name: str) -> str:
    return f"{_quote_identifier(database)}.{_quote_identifier(object_name)}"


def _pragma_database(database: str) -> str:
    if database not in {"main", "reference_db"}:
        raise RepairFailure("database_alias_invalid")
    return database


def _sidecar_paths(database: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _require_no_sidecars(database: Path) -> None:
    if any(path.exists() for path in _sidecar_paths(database)):
        raise RepairFailure("database_sidecar_present")


def _require_database(path: Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file() or candidate.is_symlink():
        raise RepairFailure("database_unavailable")
    _require_no_sidecars(candidate)
    return candidate


@contextmanager
def _readonly(
    database: Path,
    *,
    immutable: bool = True,
) -> Iterator[sqlite3.Connection]:
    candidate = _require_database(database)
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    try:
        connection = sqlite3.connect(candidate.as_uri() + suffix, uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RepairFailure("database_read_failed") from exc


def _schema_version(connection: sqlite3.Connection, database: str = "main") -> int:
    return int(
        connection.execute(f"PRAGMA {_pragma_database(database)}.user_version").fetchone()[0]
    )


def _table_names(connection: sqlite3.Connection, database: str = "main") -> list[str]:
    return sorted(
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM {_qualified(database, 'sqlite_master')} "
            "WHERE type='table' ORDER BY name"
        )
    )


def _columns(connection: sqlite3.Connection, table: str, database: str = "main") -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(
            f"PRAGMA {_pragma_database(database)}.table_info({_quote_identifier(table)})"
        )
    ]


def _database_health(
    connection: sqlite3.Connection,
    *,
    database: str = "main",
    expected_schema: int,
) -> dict[str, Any]:
    schema_version = _schema_version(connection, database)
    integrity_rows = [
        str(row[0])
        for row in connection.execute(
            f"PRAGMA {_pragma_database(database)}.integrity_check"
        )
    ]
    foreign_key_rows = list(
        connection.execute(f"PRAGMA {_pragma_database(database)}.foreign_key_check")
    )
    health = {
        "schema_version": schema_version,
        "integrity_ok": integrity_rows == ["ok"],
        "foreign_keys_ok": not foreign_key_rows,
    }
    if schema_version != expected_schema:
        raise RepairFailure("database_schema_mismatch")
    if not health["integrity_ok"]:
        raise RepairFailure("database_integrity_failed")
    if not health["foreign_keys_ok"]:
        raise RepairFailure("database_foreign_key_failed")
    return health


def capture_logical_snapshot(
    connection: sqlite3.Connection,
    *,
    database: str = "main",
) -> dict[str, Any]:
    """Hash every table row and every individual column without exposing values."""

    schema_records = []
    for row in connection.execute(
        f"SELECT type, name, tbl_name, sql FROM {_qualified(database, 'sqlite_master')} "
        "ORDER BY type, name"
    ):
        schema_records.append(_row_digest(tuple(row)))

    tables: dict[str, dict[str, Any]] = {}
    for table in _table_names(connection, database):
        columns = _columns(connection, table, database)
        if not columns:
            raise RepairFailure("database_table_shape_invalid")
        select_list = ",".join(_quote_identifier(column) for column in columns)
        rows = [
            tuple(row)
            for row in connection.execute(
                f"SELECT {select_list} FROM {_qualified(database, table)}"
            )
        ]
        tables[table] = {
            "columns": columns,
            "count": len(rows),
            "table_digest": _aggregate_digest(_row_digest(row) for row in rows),
            "column_digests": {
                column: _aggregate_digest(_value_digest(row[index]) for row in rows)
                for index, column in enumerate(columns)
            },
        }
    summary_digest = _aggregate_digest(
        [
            _aggregate_digest(schema_records),
            *(
                _aggregate_digest(
                    (
                        _value_digest(name),
                        str(table["count"]),
                        str(table["table_digest"]),
                    )
                )
                for name, table in tables.items()
            ),
        ]
    )
    return {
        "schema_version": _schema_version(connection, database),
        "schema_digest": _aggregate_digest(schema_records),
        "table_count": len(tables),
        "tables": tables,
        "summary_digest": summary_digest,
    }


def _identity_rows(connection: sqlite3.Connection, database: str) -> tuple[list[str], list[tuple[object, ...]]]:
    tables = set(_table_names(connection, database))
    if IDENTITY_TABLE not in tables:
        raise RepairFailure("identity_table_missing")
    columns = _columns(connection, IDENTITY_TABLE, database)
    if not set(IDENTITY_REQUIRED_COLUMNS).issubset(columns):
        raise RepairFailure("identity_table_shape_invalid")
    selected = ",".join(_quote_identifier(column) for column in columns)
    rows = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {selected} FROM {_qualified(database, IDENTITY_TABLE)}"
        )
    ]
    return columns, rows


def _compare_identity_connections(
    target: sqlite3.Connection,
    reference: sqlite3.Connection,
    *,
    target_database: str = "main",
    reference_database: str = "main",
) -> _IdentityComparison:
    target_columns, target_rows = _identity_rows(target, target_database)
    reference_columns, reference_rows = _identity_rows(reference, reference_database)
    if target_columns != reference_columns:
        raise RepairFailure("identity_table_shape_mismatch")

    indexes = {column: target_columns.index(column) for column in target_columns}
    key_indexes = tuple(indexes[column] for column in IDENTITY_KEY_COLUMNS)
    mapping_indexes = tuple(indexes[column] for column in IDENTITY_MAPPING_COLUMNS)
    first_seen_index = indexes["first_seen_at"]
    updated_index = indexes["updated_at"]
    other_indexes = tuple(
        index
        for index, column in enumerate(target_columns)
        if column not in {"updated_at", "first_seen_at", *IDENTITY_MAPPING_COLUMNS}
    )

    def inspect(rows: Sequence[tuple[object, ...]]) -> tuple[
        dict[tuple[object, ...], tuple[object, ...]], int, int
    ]:
        records: dict[tuple[object, ...], tuple[object, ...]] = {}
        null_or_empty = 0
        duplicates = 0
        for row in rows:
            key = tuple(row[index] for index in key_indexes)
            mapping = tuple(row[index] for index in mapping_indexes)
            if any(value is None or (isinstance(value, str) and not value.strip()) for value in mapping):
                null_or_empty += 1
            if key in records:
                duplicates += 1
            else:
                records[key] = row
        return records, null_or_empty, duplicates

    target_by_key, target_nulls, target_duplicates = inspect(target_rows)
    reference_by_key, reference_nulls, reference_duplicates = inspect(reference_rows)
    target_keys = set(target_by_key)
    reference_keys = set(reference_by_key)
    common = target_keys & reference_keys

    mapping_conflicts = 0
    first_seen_mismatches = 0
    other_mismatches = 0
    repair_count = 0
    matching_count = 0
    reference_by_mapping: dict[tuple[object, ...], tuple[object, ...]] = {}
    for key in common:
        target_row = target_by_key[key]
        reference_row = reference_by_key[key]
        target_mapping = tuple(target_row[index] for index in mapping_indexes)
        reference_mapping = tuple(reference_row[index] for index in mapping_indexes)
        if target_mapping != reference_mapping:
            mapping_conflicts += 1
            continue
        reference_by_mapping[reference_mapping] = reference_row
        if target_row[first_seen_index] != reference_row[first_seen_index]:
            first_seen_mismatches += 1
            continue
        if any(target_row[index] != reference_row[index] for index in other_indexes):
            other_mismatches += 1
            continue
        if target_row[updated_index] == reference_row[updated_index]:
            matching_count += 1
        else:
            repair_count += 1

    report = {
        "target_identity_count": len(target_rows),
        "reference_identity_count": len(reference_rows),
        "rows_compared": len(common),
        "timestamp_repair_count": repair_count,
        "already_matching_count": matching_count,
        "mapping_conflict_count": mapping_conflicts,
        "first_seen_mismatch_count": first_seen_mismatches,
        "other_column_mismatch_count": other_mismatches,
        "missing_count": len(reference_keys - target_keys),
        "extra_count": len(target_keys - reference_keys),
        "null_or_empty_key_count": target_nulls + reference_nulls,
        "duplicate_key_count": target_duplicates + reference_duplicates,
    }
    report["only_updated_at_differs"] = not any(
        report[name]
        for name in (
            "mapping_conflict_count",
            "first_seen_mismatch_count",
            "other_column_mismatch_count",
            "missing_count",
            "extra_count",
            "null_or_empty_key_count",
            "duplicate_key_count",
        )
    )
    return _IdentityComparison(report=report, reference_by_mapping=reference_by_mapping)


def _assert_expected_comparison(
    comparison: _IdentityComparison,
    *,
    expected_identity_count: int,
    expected_repair_count: int,
) -> None:
    report = comparison.report
    if expected_identity_count < 0 or expected_repair_count < 0:
        raise RepairFailure("expected_count_invalid")
    if not report["only_updated_at_differs"]:
        raise RepairFailure("identity_relationship_mismatch")
    if (
        report["target_identity_count"] != expected_identity_count
        or report["reference_identity_count"] != expected_identity_count
        or report["rows_compared"] != expected_identity_count
    ):
        raise RepairFailure("identity_count_mismatch")
    if report["timestamp_repair_count"] != expected_repair_count:
        raise RepairFailure("repair_count_mismatch")


def _assert_reference_hash(reference: Path, expected_sha256: str) -> str:
    expected = str(expected_sha256).strip().casefold()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise RepairFailure("reference_hash_invalid")
    actual = _sha256_file(reference)
    if actual.casefold() != expected:
        raise RepairFailure("reference_hash_mismatch")
    return actual


def compare_identity_timestamps(
    *,
    target_database: Path,
    reference_backup: Path,
    expected_reference_sha256: str,
    expected_identity_count: int,
    expected_repair_count: int,
) -> dict[str, Any]:
    """Return a value-free aggregate comparison and fail on any unsafe delta."""

    target_path = _require_database(target_database)
    reference_path = _require_database(reference_backup)
    if target_path == reference_path:
        raise RepairFailure("database_paths_not_distinct")
    reference_hash = _assert_reference_hash(reference_path, expected_reference_sha256)
    with _readonly(target_path) as target, _readonly(reference_path) as reference:
        target_health = _database_health(
            target, expected_schema=EXPECTED_TARGET_SCHEMA
        )
        reference_health = _database_health(
            reference, expected_schema=EXPECTED_REFERENCE_SCHEMA
        )
        comparison = _compare_identity_connections(target, reference)
    _assert_expected_comparison(
        comparison,
        expected_identity_count=expected_identity_count,
        expected_repair_count=expected_repair_count,
    )
    return {
        "ok": True,
        "mode": "compare",
        "target_health": target_health,
        "reference_health": reference_health,
        "reference_sha256": reference_hash,
        "identity_comparison": comparison.report,
        "raw_identity_values_emitted": False,
    }


def _snapshot_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return dict(left) == dict(right)


def _verified_schema6_backup(
    *,
    target_database: Path,
    backup_directory: Path,
    expected_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    directory = Path(backup_directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir() or directory.is_symlink():
        raise RepairFailure("backup_directory_invalid")
    candidate = directory / (
        f"music_vault_batch10_2_pre_timestamp_repair_{_utc_stamp()}.sqlite3"
    )
    if candidate.exists():
        raise RepairFailure("backup_collision")
    try:
        with _readonly(target_database, immutable=False) as source:
            destination = sqlite3.connect(candidate)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
        with _readonly(candidate) as verified:
            health = _database_health(
                verified, expected_schema=EXPECTED_TARGET_SCHEMA
            )
            snapshot = capture_logical_snapshot(verified)
        if not _snapshot_equal(snapshot, expected_snapshot):
            raise RepairFailure("backup_logical_mismatch")
        return {
            "path": str(candidate),
            "name": candidate.name,
            "sha256": _sha256_file(candidate),
            "size": candidate.stat().st_size,
            "health": health,
            "logical_snapshot_verified": True,
        }
    except Exception:
        if candidate.exists():
            candidate.unlink()
        raise


def _identity_only_updated_at_changed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    if before["schema_digest"] != after["schema_digest"]:
        raise RepairFailure("schema_changed_during_repair")
    before_tables = before["tables"]
    after_tables = after["tables"]
    if set(before_tables) != set(after_tables):
        raise RepairFailure("table_set_changed_during_repair")
    checked_tables = 0
    checked_columns = 0
    for table, before_table in before_tables.items():
        after_table = after_tables[table]
        checked_tables += 1
        if before_table["columns"] != after_table["columns"]:
            raise RepairFailure("table_shape_changed_during_repair")
        if before_table["count"] != after_table["count"]:
            raise RepairFailure("table_count_changed_during_repair")
        if table != IDENTITY_TABLE:
            if before_table != after_table:
                raise RepairFailure("non_identity_table_changed")
            checked_columns += len(before_table["columns"])
            continue
        for column in before_table["columns"]:
            checked_columns += 1
            if column == "updated_at":
                if (
                    before_table["column_digests"][column]
                    == after_table["column_digests"][column]
                ):
                    raise RepairFailure("timestamp_column_did_not_change")
                continue
            if (
                before_table["column_digests"][column]
                != after_table["column_digests"][column]
            ):
                raise RepairFailure("non_timestamp_identity_column_changed")
    return {
        "no_other_values_changed": True,
        "checked_table_count": checked_tables,
        "checked_column_count": checked_columns,
        "changed_table_count": 1,
        "changed_column_count": 1,
        "changed_table": IDENTITY_TABLE,
        "changed_column": "updated_at",
    }


def repair_identity_timestamps(
    *,
    target_database: Path,
    reference_backup: Path,
    backup_directory: Path,
    expected_reference_sha256: str,
    expected_identity_count: int,
    expected_repair_count: int,
) -> dict[str, Any]:
    """Create a verified backup, then restore only exact-mapping timestamps."""

    target_path = _require_database(target_database)
    reference_path = _require_database(reference_backup)
    if expected_repair_count <= 0:
        raise RepairFailure("repair_count_invalid")
    if target_path == reference_path:
        raise RepairFailure("database_paths_not_distinct")
    reference_hash = _assert_reference_hash(reference_path, expected_reference_sha256)

    with _readonly(target_path) as target, _readonly(reference_path) as reference:
        target_health = _database_health(target, expected_schema=EXPECTED_TARGET_SCHEMA)
        _database_health(reference, expected_schema=EXPECTED_REFERENCE_SCHEMA)
        dry_run = _compare_identity_connections(target, reference)
        _assert_expected_comparison(
            dry_run,
            expected_identity_count=expected_identity_count,
            expected_repair_count=expected_repair_count,
        )
        pre_snapshot = capture_logical_snapshot(target)

    backup = _verified_schema6_backup(
        target_database=target_path,
        backup_directory=backup_directory,
        expected_snapshot=pre_snapshot,
    )
    backup_hash = str(backup["sha256"])

    connection: sqlite3.Connection | None = None
    committed = False
    try:
        connection = sqlite3.connect(
            target_path.as_uri() + "?mode=rw",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        reference_uri = reference_path.as_uri() + "?mode=ro&immutable=1"
        connection.execute("ATTACH DATABASE ? AS reference_db", (reference_uri,))
        connection.execute("BEGIN EXCLUSIVE")
        _assert_reference_hash(reference_path, expected_reference_sha256)
        _database_health(connection, expected_schema=EXPECTED_TARGET_SCHEMA)
        _database_health(
            connection,
            database="reference_db",
            expected_schema=EXPECTED_REFERENCE_SCHEMA,
        )
        transaction_snapshot = capture_logical_snapshot(connection)
        if not _snapshot_equal(transaction_snapshot, pre_snapshot):
            raise RepairFailure("target_changed_after_backup")
        with _readonly(Path(str(backup["path"]))) as backup_connection:
            backup_snapshot = capture_logical_snapshot(backup_connection)
        if not _snapshot_equal(transaction_snapshot, backup_snapshot):
            raise RepairFailure("verified_backup_no_longer_matches")

        transaction_compare = _compare_identity_connections(
            connection,
            connection,
            target_database="main",
            reference_database="reference_db",
        )
        _assert_expected_comparison(
            transaction_compare,
            expected_identity_count=expected_identity_count,
            expected_repair_count=expected_repair_count,
        )
        cursor = connection.execute(
            f"""
            UPDATE {_quote_identifier(IDENTITY_TABLE)} AS live
            SET updated_at = (
                SELECT reference.updated_at
                FROM {_qualified('reference_db', IDENTITY_TABLE)} AS reference
                WHERE reference.source_kind = live.source_kind
                  AND reference.external_track_id = live.external_track_id
                  AND reference.track_id = live.track_id
            )
            WHERE live.updated_at IS NOT (
                SELECT reference.updated_at
                FROM {_qualified('reference_db', IDENTITY_TABLE)} AS reference
                WHERE reference.source_kind = live.source_kind
                  AND reference.external_track_id = live.external_track_id
                  AND reference.track_id = live.track_id
            )
              AND EXISTS (
                SELECT 1
                FROM {_qualified('reference_db', IDENTITY_TABLE)} AS reference
                WHERE reference.source_kind = live.source_kind
                  AND reference.external_track_id = live.external_track_id
                  AND reference.track_id = live.track_id
            )
            """
        )
        affected = int(cursor.rowcount)
        if affected != expected_repair_count:
            raise RepairFailure("affected_row_count_mismatch")

        post_compare = _compare_identity_connections(
            connection,
            connection,
            target_database="main",
            reference_database="reference_db",
        )
        _assert_expected_comparison(
            post_compare,
            expected_identity_count=expected_identity_count,
            expected_repair_count=0,
        )
        post_snapshot = capture_logical_snapshot(connection)
        digest_proof = _identity_only_updated_at_changed(pre_snapshot, post_snapshot)
        post_health = _database_health(connection, expected_schema=EXPECTED_TARGET_SCHEMA)
        connection.execute("COMMIT")
        committed = True
        connection.execute("DETACH DATABASE reference_db")
    except Exception:
        if connection is not None and not committed:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        if connection is not None:
            connection.close()

    with _readonly(target_path) as final_target, _readonly(reference_path) as final_reference:
        final_health = _database_health(
            final_target, expected_schema=EXPECTED_TARGET_SCHEMA
        )
        final_compare = _compare_identity_connections(final_target, final_reference)
        _assert_expected_comparison(
            final_compare,
            expected_identity_count=expected_identity_count,
            expected_repair_count=0,
        )
        final_snapshot = capture_logical_snapshot(final_target)
    if not _snapshot_equal(final_snapshot, post_snapshot):
        raise RepairFailure("post_commit_snapshot_mismatch")
    if _sha256_file(Path(str(backup["path"]))) != backup_hash:
        raise RepairFailure("verified_backup_changed")
    _assert_reference_hash(reference_path, reference_hash)
    _require_no_sidecars(target_path)

    return {
        "ok": True,
        "mode": "repair",
        "target_health_before": target_health,
        "target_health_after": final_health,
        "dry_run": dry_run.report,
        "updated_row_count": expected_repair_count,
        "final_identity_comparison": final_compare.report,
        "backup": backup,
        "digest_proof": digest_proof,
        "pre_repair_summary_digest": pre_snapshot["summary_digest"],
        "post_repair_summary_digest": final_snapshot["summary_digest"],
        "raw_identity_values_emitted": False,
        "provider_access_count": 0,
        "secret_file_read_count": 0,
        "media_file_access_count": 0,
    }


def _clone_database(source_path: Path, destination_path: Path) -> None:
    with _readonly(source_path, immutable=False) as source:
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()


def prove_repair_on_disposable_clone(
    *,
    target_database: Path,
    reference_backup: Path,
    expected_reference_sha256: str,
    expected_identity_count: int,
    expected_repair_count: int,
) -> dict[str, Any]:
    """Prove the complete repair against a temporary clone outside the repo."""

    target_path = _require_database(target_database)
    reference_path = _require_database(reference_backup)
    target_hash_before = _sha256_file(target_path)
    target_stat_before = (target_path.stat().st_size, target_path.stat().st_mtime_ns)
    reference_hash_before = _assert_reference_hash(
        reference_path, expected_reference_sha256
    )
    with _readonly(target_path) as target:
        source_snapshot = capture_logical_snapshot(target)

    temporary_root = Path(tempfile.mkdtemp(prefix="MusicVault_Batch10_2_RepairProof_"))
    try:
        try:
            temporary_root.resolve().relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            pass
        else:
            raise RepairFailure("temporary_clone_inside_repository")
        clone = temporary_root / "schema6-clone.sqlite3"
        _clone_database(target_path, clone)
        with _readonly(clone) as clone_connection:
            if not _snapshot_equal(
                capture_logical_snapshot(clone_connection), source_snapshot
            ):
                raise RepairFailure("temporary_clone_mismatch")
        result = repair_identity_timestamps(
            target_database=clone,
            reference_backup=reference_path,
            backup_directory=temporary_root / "backups",
            expected_reference_sha256=reference_hash_before,
            expected_identity_count=expected_identity_count,
            expected_repair_count=expected_repair_count,
        )
        proof = {
            "ok": True,
            "mode": "clone-proof",
            "repair": {
                "updated_row_count": result["updated_row_count"],
                "digest_proof": result["digest_proof"],
                "final_identity_comparison": result["final_identity_comparison"],
            },
            "temporary_root_outside_repository": True,
            "temporary_root_deleted": True,
            "source_target_unchanged": False,
            "reference_backup_unchanged": False,
            "raw_identity_values_emitted": False,
        }
    finally:
        shutil.rmtree(temporary_root, ignore_errors=False)

    target_unchanged = (
        _sha256_file(target_path) == target_hash_before
        and (target_path.stat().st_size, target_path.stat().st_mtime_ns)
        == target_stat_before
    )
    reference_unchanged = _sha256_file(reference_path) == reference_hash_before
    if not target_unchanged:
        raise RepairFailure("source_target_changed_during_clone_proof")
    if not reference_unchanged:
        raise RepairFailure("reference_changed_during_clone_proof")
    proof["source_target_unchanged"] = True
    proof["reference_backup_unchanged"] = True
    return proof


def _music_vault_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MusicVault.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise RepairFailure("process_check_failed")
    return "musicvault.exe" in result.stdout.casefold()


def _live_execution_guard(
    acknowledgement: str,
    *,
    expected_identity_count: int,
    expected_repair_count: int,
) -> None:
    if acknowledgement != LIVE_ACKNOWLEDGEMENT:
        raise RepairFailure("live_acknowledgement_missing")
    if (
        expected_identity_count != LIVE_EXPECTED_IDENTITY_COUNT
        or expected_repair_count != LIVE_EXPECTED_REPAIR_COUNT
    ):
        raise RepairFailure("live_expected_count_mismatch")
    if os.environ.get(NO_SECRETS_ENVIRONMENT) != "1":
        raise RepairFailure("no_secrets_environment_missing")
    if _music_vault_running():
        raise RepairFailure("music_vault_process_running")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate-only Batch 10.2 identity timestamp proof and repair"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target-database", type=Path, required=True)
        command.add_argument("--reference-backup", type=Path, required=True)
        command.add_argument("--reference-sha256", required=True)
        command.add_argument("--expected-identity-count", type=int, required=True)
        command.add_argument("--expected-repair-count", type=int, required=True)
        command.add_argument("--output", type=Path)

    compare = subparsers.add_parser("compare")
    common(compare)
    clone = subparsers.add_parser("clone-proof")
    common(clone)
    repair = subparsers.add_parser("repair")
    common(repair)
    repair.add_argument("--backup-directory", type=Path, required=True)
    repair.add_argument("--acknowledge-live-repair", required=True)
    return parser


def _write_report(report: Mapping[str, Any], output: Path | None) -> None:
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if output is not None:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(destination)
    print(encoded)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        common = {
            "target_database": args.target_database,
            "reference_backup": args.reference_backup,
            "expected_reference_sha256": args.reference_sha256,
            "expected_identity_count": args.expected_identity_count,
            "expected_repair_count": args.expected_repair_count,
        }
        if args.mode == "compare":
            report = compare_identity_timestamps(**common)
        elif args.mode == "clone-proof":
            report = prove_repair_on_disposable_clone(**common)
        else:
            _live_execution_guard(
                args.acknowledge_live_repair,
                expected_identity_count=args.expected_identity_count,
                expected_repair_count=args.expected_repair_count,
            )
            report = repair_identity_timestamps(
                **common,
                backup_directory=args.backup_directory,
            )
        _write_report(report, args.output)
        return 0
    except RepairFailure as exc:
        _write_report({"ok": False, "error_code": str(exc)}, args.output)
        return 2
    except Exception:
        _write_report({"ok": False, "error_code": "unexpected_repair_failure"}, args.output)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
