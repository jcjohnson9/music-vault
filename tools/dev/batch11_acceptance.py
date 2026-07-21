from __future__ import annotations

"""Privacy-safe preservation helpers for the Batch 11 E2E gate.

The module has no import-time side effects.  It never reads credential
contents and never emits track, playlist, source, media-path, or provider-ID
values.  Personal values used for preservation comparisons are reduced to
one-way aggregate digests before they enter an evidence manifest.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PRE_SCHEMA_VERSION = 7
POST_SCHEMA_VERSION = 8
EVIDENCE_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
TEMP_PREFIX = "MusicVault_Batch11_QualityE2E_"
EXPLICIT_BACKUP_PREFIX = "music_vault_batch11_explicit_rollback_"
QUALITY_TABLE = "track_media_quality"
QUALITY_INDEXES = frozenset(
    {
        "idx_track_media_quality_acquisition",
        "idx_track_media_quality_inspection",
        "idx_track_media_quality_stored_codec",
    }
)
EXPECTED_ADDITIVE_COLUMNS: Mapping[str, frozenset[str]] = {
    "sync_sources": frozenset({"download_quality_profile"}),
    "sync_source_runs": frozenset(
        {
            "source_preserved_count",
            "source_preserved_remux_count",
            "mp3_compatibility_transcode_count",
            "quality_failure_count",
            "total_stored_bytes",
        }
    ),
}
CONFIG_MIGRATION_KEYS = frozenset(
    {"download_quality_profile", "compatibility_mp3_bitrate_kbps"}
)
CREDENTIAL_NAMES = ("youtube_api_key.txt", "discogs_token.txt")
UNCHANGED_RUNTIME_FILES = (
    "youtube_download_archive.txt",
    "youtube_failed_ids.txt",
)
UNCHANGED_RUNTIME_DIRECTORIES = (
    "youtube_downloads",
    "covers",
    "artist_images",
    "provider_cache",
    "lyrics",
    "metadata_reports",
    "backups/metadata_jobs",
)
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class AcceptanceFailure(RuntimeError):
    """A deliberately non-identifying acceptance failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def safe_temporary_root(path: str | Path, *, must_exist: bool | None = None) -> Path:
    candidate = Path(path).expanduser().resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if (
        candidate == temporary
        or not is_within(candidate, temporary)
        or not candidate.name.startswith(TEMP_PREFIX)
        or candidate.is_symlink()
    ):
        raise AcceptanceFailure("unsafe_temporary_acceptance_root")
    if must_exist is True and not candidate.is_dir():
        raise AcceptanceFailure("temporary_acceptance_root_missing")
    if must_exist is False and candidate.exists():
        raise AcceptanceFailure("temporary_acceptance_root_already_exists")
    return candidate


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def read_json(path: str | Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        if not source.is_file() or source.stat().st_size > maximum_bytes:
            raise AcceptanceFailure("acceptance_json_unavailable")
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AcceptanceFailure("acceptance_json_invalid") from None
    if not isinstance(payload, dict):
        raise AcceptanceFailure("acceptance_json_invalid")
    return payload


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def aggregate_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(str(item) for item in values):
        encoded = value.encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


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


def quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def readonly(database: str | Path, *, immutable: bool = True) -> sqlite3.Connection:
    source = Path(database).expanduser().resolve()
    suffix = "&immutable=1" if immutable else ""
    connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro{suffix}",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({quote_identifier(table)})"
        )
    ]


def table_guard(
    connection: sqlite3.Connection,
    table: str,
    *,
    selected_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    selected = list(selected_columns) if selected_columns is not None else columns(connection, table)
    if not selected:
        return {"columns": [], "count": 0, "digest": aggregate_digest(())}
    rows = connection.execute(
        "SELECT "
        + ",".join(quote_identifier(name) for name in selected)
        + " FROM "
        + quote_identifier(table)
    ).fetchall()
    return {
        "columns": selected,
        "count": len(rows),
        "digest": aggregate_digest(row_digest(tuple(row)) for row in rows),
    }


def _path_token(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()


def file_guard(path: str | Path, *, content: bool) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        return {"exists": False}
    if not source.is_file() or source.is_symlink():
        raise AcceptanceFailure("guarded_runtime_file_invalid")
    stat = source.stat()
    result: dict[str, Any] = {
        "exists": True,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    if content:
        result["sha256"] = sha256_file(source)
    return result


def tree_guard(root: str | Path, *, content: bool) -> dict[str, Any]:
    directory = Path(root).expanduser().resolve()
    if not directory.exists():
        return {"exists": False, "file_count": 0, "total_bytes": 0, "digest": aggregate_digest(())}
    if not directory.is_dir() or directory.is_symlink():
        raise AcceptanceFailure("guarded_runtime_directory_invalid")
    entries: list[str] = []
    total = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise AcceptanceFailure("guarded_runtime_symlink_present")
        if not path.is_file():
            continue
        stat = path.stat()
        total += int(stat.st_size)
        relative = path.relative_to(directory).as_posix().casefold()
        payload = [relative, str(stat.st_size), str(stat.st_mtime_ns)]
        if content:
            payload.append(sha256_file(path))
        entries.append("|".join(payload))
    return {
        "exists": True,
        "file_count": len(entries),
        "total_bytes": total,
        "digest": aggregate_digest(entries),
    }


def media_guard(connection: sqlite3.Connection) -> dict[str, Any]:
    """Fingerprint track paths and media content without exposing either."""

    rows = connection.execute("SELECT id,path FROM tracks ORDER BY id").fetchall()
    path_entries: list[str] = []
    media_entries: list[str] = []
    missing = 0
    total = 0
    seen: set[Path] = set()
    for row in rows:
        path = Path(str(row[1])).expanduser().resolve()
        token = _path_token(path)
        path_entries.append(f"{int(row[0])}|{token}")
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file() or path.is_symlink():
            missing += 1
            media_entries.append(f"{token}|missing")
            continue
        stat = path.stat()
        total += int(stat.st_size)
        media_entries.append(
            "|".join(
                (
                    token,
                    str(stat.st_size),
                    str(stat.st_mtime_ns),
                    sha256_file(path),
                )
            )
        )
    return {
        "track_path_count": len(rows),
        "unique_media_count": len(seen),
        "missing_media_count": missing,
        "total_media_bytes": total,
        "path_digest": aggregate_digest(path_entries),
        "media_digest": aggregate_digest(media_entries),
    }


def config_guard(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        return {"exists": False, "stable_digest": aggregate_digest(())}
    payload = read_json(source, maximum_bytes=1024 * 1024)
    stable = {key: value for key, value in payload.items() if key not in CONFIG_MIGRATION_KEYS}
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "exists": True,
        "stable_digest": hashlib.sha256(encoded.encode("ascii")).hexdigest(),
        "full_digest": hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest(),
        "profile_present": "download_quality_profile" in payload,
        "compatibility_bitrate_present": "compatibility_mp3_bitrate_kbps" in payload,
    }


def database_guard(
    database: str | Path,
    *,
    expected_schema: int,
    include_media: bool = True,
) -> dict[str, Any]:
    connection = readonly(database)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema != int(expected_schema):
            raise AcceptanceFailure("unexpected_live_schema_version")
        names = table_names(connection)
        return {
            "schema_version": schema,
            "table_guards": {name: table_guard(connection, name) for name in names},
            "media": media_guard(connection) if include_media else None,
            "foreign_keys_enabled_for_audit": bool(
                int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
            ),
        }
    finally:
        connection.close()


def runtime_guard(
    project_root: str | Path,
    *,
    content: bool = True,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    data = root / "data"
    return {
        "config": (
            config_guard(data / "music_vault_config.json")
            if content
            else file_guard(data / "music_vault_config.json", content=False)
        ),
        "credentials": {
            name: file_guard(data / name, content=False) for name in CREDENTIAL_NAMES
        },
        "runtime_files": {
            name: file_guard(data / name, content=content)
            for name in UNCHANGED_RUNTIME_FILES
        },
        "runtime_trees": {
            name: tree_guard(data / name, content=content)
            for name in UNCHANGED_RUNTIME_DIRECTORIES
        },
        "status_before": file_guard(data / "music_vault_status.json", content=False),
        "backup_tree": tree_guard(data / "backups", content=content),
        "backup_file_tokens": sorted(
            _path_token(path)
            for path in (data / "backups").glob("*.sqlite3")
            if path.is_file() and not path.is_symlink()
        ),
    }


def create_verified_backup(
    database: str | Path,
    destination: str | Path,
    baseline_database: Mapping[str, Any],
) -> dict[str, Any]:
    source = Path(database).expanduser().resolve()
    backup = Path(destination).expanduser().resolve()
    if backup.exists() or not is_within(backup, source.parent / "backups"):
        raise AcceptanceFailure("unsafe_or_existing_rollback_backup")
    backup.parent.mkdir(parents=True, exist_ok=True)
    source_connection = readonly(source, immutable=False)
    target_connection = sqlite3.connect(backup)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    verified = database_guard(
        backup,
        expected_schema=PRE_SCHEMA_VERSION,
        include_media=False,
    )
    if verified["table_guards"] != baseline_database["table_guards"]:
        raise AcceptanceFailure("rollback_backup_logical_mismatch")
    return {
        "filename": backup.name,
        "sha256": sha256_file(backup),
        "size": int(backup.stat().st_size),
        "schema_version": PRE_SCHEMA_VERSION,
        "verified": True,
    }


def prepare_live_manifest(
    *,
    project_root: str | Path,
    evidence_root: str | Path,
) -> dict[str, Any]:
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() != "1":
        raise AcceptanceFailure("no_secret_mode_required")
    root = Path(project_root).expanduser().resolve()
    evidence = safe_temporary_root(evidence_root, must_exist=True)
    database = root / "data" / "music_vault.sqlite3"
    if not database.is_file():
        raise AcceptanceFailure("live_database_unavailable")
    sidecars = {
        suffix: file_guard(Path(str(database) + suffix), content=False)
        for suffix in SQLITE_SIDECAR_SUFFIXES
    }
    if any(value.get("exists") for value in sidecars.values()):
        raise AcceptanceFailure("live_database_not_quiescent")
    baseline_database = database_guard(database, expected_schema=PRE_SCHEMA_VERSION)
    baseline_runtime = runtime_guard(root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup = root / "data" / "backups" / f"{EXPLICIT_BACKUP_PREFIX}{timestamp}.sqlite3"
    backup_evidence = create_verified_backup(database, backup, baseline_database)
    manifest = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "prepared_at": utc_now(),
        "expected_pre_schema": PRE_SCHEMA_VERSION,
        "expected_post_schema": POST_SCHEMA_VERSION,
        "project_root_token": _path_token(root),
        "database_before": baseline_database,
        "database_file_before": file_guard(database, content=True),
        "database_sidecars_before": sidecars,
        "runtime_before": baseline_runtime,
        "explicit_backup": backup_evidence,
        "credential_contents_read": False,
        "personal_values_emitted": False,
    }
    atomic_write_json(evidence / "live-baseline.json", manifest)
    return manifest


def verify_network_report(path: str | Path) -> dict[str, Any]:
    payload = read_json(path, maximum_bytes=64 * 1024)
    required_zero = (
        "attempt_count",
        "provider_factory_invocation_count",
        "provider_task_dispatch_count",
    )
    if not (
        payload.get("guard_installed") is True
        and payload.get("outbound_blocked") is True
        and payload.get("finalized") is True
        and all(int(payload.get(name, -1)) == 0 for name in required_zero)
        and payload.get("request_details_recorded") is False
        and payload.get("credential_contents_read") is False
    ):
        raise AcceptanceFailure("acceptance_network_evidence_failed")
    return {
        "guard_installed": True,
        "attempt_count": 0,
        "provider_factory_invocation_count": 0,
        "provider_task_dispatch_count": 0,
        "finalized": True,
    }


def _verify_preserved_tables(
    connection: sqlite3.Connection,
    baseline: Mapping[str, Any],
) -> tuple[bool, dict[str, int]]:
    before = baseline.get("table_guards")
    if not isinstance(before, Mapping):
        raise AcceptanceFailure("baseline_table_guards_invalid")
    counts: dict[str, int] = {}
    current_names = set(table_names(connection))
    for name, guard_value in before.items():
        if name not in current_names or not isinstance(guard_value, Mapping):
            raise AcceptanceFailure("preexisting_table_missing_after_migration")
        selected = guard_value.get("columns")
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise AcceptanceFailure("baseline_table_guard_invalid")
        after = table_guard(connection, str(name), selected_columns=selected)
        if after != dict(guard_value):
            raise AcceptanceFailure("preexisting_database_rows_changed")
        counts[str(name)] = int(after["count"])
    return True, counts


def _verify_quality_rows(connection: sqlite3.Connection) -> dict[str, Any]:
    track_count = int(connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
    quality_count = int(
        connection.execute(f"SELECT COUNT(*) FROM {QUALITY_TABLE}").fetchone()[0]
    )
    orphan_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {QUALITY_TABLE} q "
            "LEFT JOIN tracks t ON t.id=q.track_id WHERE t.id IS NULL"
        ).fetchone()[0]
    )
    invented_source_facts = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {QUALITY_TABLE} WHERE "
            "source_format_id IS NOT NULL OR source_extension IS NOT NULL OR "
            "source_container IS NOT NULL OR source_codec IS NOT NULL OR "
            "source_bitrate_kbps IS NOT NULL OR source_sample_rate_hz IS NOT NULL OR "
            "source_channels IS NOT NULL OR source_filesize_bytes IS NOT NULL"
        ).fetchone()[0]
    )
    invented_stored_facts = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {QUALITY_TABLE} WHERE "
            "stored_container IS NOT NULL OR stored_codec IS NOT NULL OR "
            "stored_bitrate_kbps IS NOT NULL OR stored_sample_rate_hz IS NOT NULL OR "
            "stored_channels IS NOT NULL OR stored_filesize_bytes IS NOT NULL OR "
            "inspected_at IS NOT NULL"
        ).fetchone()[0]
    )
    nonlegacy_profile_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {QUALITY_TABLE} WHERE acquisition_profile "
            "IN ('best_original','mp3_320_compatibility')"
        ).fetchone()[0]
    )
    youtube_mp3_invalid = int(
        connection.execute(
            f"SELECT COUNT(*) FROM tracks t JOIN {QUALITY_TABLE} q ON q.track_id=t.id "
            "WHERE lower(COALESCE(t.source_kind,''))='youtube' "
            "AND lower(t.path) LIKE '%.mp3' AND NOT ("
            "q.acquisition_profile='legacy_youtube_mp3' AND "
            "q.transformation_kind='legacy_inferred_transcode' AND "
            "q.inspection_state='legacy_inferred')"
        ).fetchone()[0]
    )
    if not (
        quality_count == track_count
        and orphan_count == 0
        and invented_source_facts == 0
        and invented_stored_facts == 0
        and nonlegacy_profile_count == 0
        and youtube_mp3_invalid == 0
    ):
        raise AcceptanceFailure("conservative_quality_inventory_failed")
    profiles = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            f"SELECT acquisition_profile,COUNT(*) FROM {QUALITY_TABLE} "
            "GROUP BY acquisition_profile ORDER BY acquisition_profile"
        )
    }
    return {
        "track_count": track_count,
        "quality_row_count": quality_count,
        "orphan_quality_row_count": orphan_count,
        "invented_source_fact_count": invented_source_facts,
        "invented_stored_fact_count": invented_stored_facts,
        "nonlegacy_profile_count": nonlegacy_profile_count,
        "invalid_legacy_youtube_mp3_count": youtube_mp3_invalid,
        "profile_counts": profiles,
    }


def verify_live_migration(
    *,
    project_root: str | Path,
    manifest: Mapping[str, Any],
    network_report: str | Path,
    graceful_close_confirmed: bool,
    external_network_connection_observed: bool,
) -> dict[str, Any]:
    if not graceful_close_confirmed:
        raise AcceptanceFailure("graceful_close_not_confirmed")
    if external_network_connection_observed:
        raise AcceptanceFailure("external_network_connection_observed")
    root = Path(project_root).expanduser().resolve()
    if manifest.get("evidence_schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise AcceptanceFailure("live_manifest_version_invalid")
    if manifest.get("project_root_token") != _path_token(root):
        raise AcceptanceFailure("live_manifest_project_mismatch")
    database = root / "data" / "music_vault.sqlite3"
    sidecars_after = {
        suffix: file_guard(Path(str(database) + suffix), content=False)
        for suffix in SQLITE_SIDECAR_SUFFIXES
    }
    if sidecars_after != manifest.get("database_sidecars_before"):
        raise AcceptanceFailure("live_database_sidecar_state_changed")
    connection = readonly(database, immutable=False)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_failures = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        foreign_keys_enabled = int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        if schema != POST_SCHEMA_VERSION or integrity.casefold() != "ok":
            raise AcceptanceFailure("post_migration_database_health_failed")
        if foreign_key_failures or not foreign_keys_enabled:
            raise AcceptanceFailure("post_migration_foreign_key_check_failed")
        baseline_db = manifest.get("database_before")
        if not isinstance(baseline_db, Mapping):
            raise AcceptanceFailure("live_manifest_database_baseline_invalid")
        preserved, counts = _verify_preserved_tables(connection, baseline_db)
        media_after = media_guard(connection)
        if media_after != baseline_db.get("media"):
            raise AcceptanceFailure("live_media_or_path_fingerprint_changed")
        quality = _verify_quality_rows(connection)
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        if not QUALITY_INDEXES.issubset(indexes):
            raise AcceptanceFailure("required_quality_index_missing")
        for table, additions in EXPECTED_ADDITIVE_COLUMNS.items():
            if table in table_names(connection) and not additions.issubset(columns(connection, table)):
                raise AcceptanceFailure("required_additive_sync_column_missing")
        if "sync_sources" in table_names(connection):
            invalid_source_profiles = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sync_sources WHERE "
                    "download_quality_profile NOT IN "
                    "('inherit','best_original','mp3_320_compatibility')"
                ).fetchone()[0]
            )
            non_inherited_existing_sources = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sync_sources "
                    "WHERE download_quality_profile<>'inherit'"
                ).fetchone()[0]
            )
            if invalid_source_profiles or non_inherited_existing_sources:
                raise AcceptanceFailure("existing_source_quality_override_migration_failed")
    finally:
        connection.close()

    runtime_after = runtime_guard(root)
    runtime_before = manifest.get("runtime_before")
    if not isinstance(runtime_before, Mapping):
        raise AcceptanceFailure("live_manifest_runtime_baseline_invalid")
    for key in ("credentials", "runtime_files", "runtime_trees"):
        if runtime_after.get(key) != runtime_before.get(key):
            raise AcceptanceFailure("protected_runtime_state_changed")
    before_config = runtime_before.get("config")
    after_config = runtime_after.get("config")
    if not isinstance(before_config, Mapping) or not isinstance(after_config, Mapping):
        raise AcceptanceFailure("configuration_guard_invalid")
    if after_config.get("stable_digest") != before_config.get("stable_digest"):
        raise AcceptanceFailure("configuration_changed_outside_quality_migration")
    config_payload = read_json(root / "data" / "music_vault_config.json")
    if not (
        config_payload.get("download_quality_profile") == "best_original"
        and int(config_payload.get("compatibility_mp3_bitrate_kbps", 0)) == 320
    ):
        raise AcceptanceFailure("quality_configuration_migration_failed")

    backup_info = manifest.get("explicit_backup")
    if not isinstance(backup_info, Mapping):
        raise AcceptanceFailure("rollback_backup_manifest_invalid")
    backup_name = str(backup_info.get("filename") or "")
    backup = root / "data" / "backups" / backup_name
    if not (
        backup.name.startswith(EXPLICIT_BACKUP_PREFIX)
        and backup.is_file()
        and sha256_file(backup) == backup_info.get("sha256")
        and int(backup.stat().st_size) == int(backup_info.get("size", -1))
    ):
        raise AcceptanceFailure("rollback_backup_changed_or_missing")
    backup_guard = database_guard(
        backup,
        expected_schema=PRE_SCHEMA_VERSION,
        include_media=False,
    )
    if backup_guard["table_guards"] != manifest["database_before"]["table_guards"]:
        raise AcceptanceFailure("rollback_backup_no_longer_matches_baseline")

    prior_backup_tokens = set(runtime_before.get("backup_file_tokens") or ())
    automatic = [
        path
        for path in (root / "data" / "backups").glob("*schema*v8*.sqlite3")
        if (
            path.is_file()
            and path.name != backup.name
            and _path_token(path) not in prior_backup_tokens
        )
    ]
    if not automatic:
        raise AcceptanceFailure("automatic_pre_schema_v8_backup_missing")
    automatic_valid = False
    for path in automatic:
        try:
            guard = database_guard(
                path,
                expected_schema=PRE_SCHEMA_VERSION,
                include_media=False,
            )
        except (AcceptanceFailure, OSError, sqlite3.Error):
            continue
        if guard["table_guards"] == manifest["database_before"]["table_guards"]:
            automatic_valid = True
            break
    if not automatic_valid:
        raise AcceptanceFailure("automatic_pre_schema_v8_backup_invalid")
    new_backup_tokens = {
        _path_token(path)
        for path in (root / "data" / "backups").glob("*.sqlite3")
        if path.is_file() and _path_token(path) not in prior_backup_tokens
    }
    expected_new_backup_tokens = {_path_token(backup)} | {
        _path_token(path) for path in automatic
    }
    if new_backup_tokens != expected_new_backup_tokens:
        raise AcceptanceFailure("unexpected_live_database_backup_created")

    network = verify_network_report(network_report)
    wrong_data = root / "dist" / "MusicVault" / "data"
    if wrong_data.exists():
        raise AcceptanceFailure("distribution_runtime_data_folder_created")
    return {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "stage": "controlled_live_schema_7_to_8",
        "status": "passed",
        "schema_before": PRE_SCHEMA_VERSION,
        "schema_after": POST_SCHEMA_VERSION,
        "integrity_check": "ok",
        "foreign_keys_enabled": True,
        "foreign_key_failure_count": 0,
        "preexisting_tables_preserved": preserved,
        "preserved_table_counts": counts,
        "media_and_path_fingerprint_preserved": True,
        "quality_inventory": quality,
        "required_quality_indexes_present": True,
        "quality_config_migrated": True,
        "existing_source_overrides_defaulted_to_inherit": True,
        "explicit_rollback_backup_preserved": True,
        "automatic_pre_schema_backup_verified": True,
        "network": network,
        "credential_files_unchanged": True,
        "credential_contents_read": False,
        "runtime_files_unchanged": True,
        "metadata_and_artwork_unchanged": True,
        "provider_activity_observed": False,
        "distribution_data_folder_absent": True,
        "graceful_close_confirmed": True,
        "personal_values_emitted": False,
    }


def combine_summaries(
    stage_a: Mapping[str, Any],
    stage_b: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not (
        stage_a.get("status") == "passed"
        and stage_a.get("stage") == "isolated_packaged_quality_scenario"
        and int(stage_a.get("schema_version", -1)) == POST_SCHEMA_VERSION
    ):
        raise AcceptanceFailure("stage_a_not_passed")
    if stage_b is not None and not (
        stage_b.get("status") == "passed"
        and stage_b.get("stage") == "controlled_live_schema_7_to_8"
        and int(stage_b.get("schema_before", -1)) == PRE_SCHEMA_VERSION
        and int(stage_b.get("schema_after", -1)) == POST_SCHEMA_VERSION
    ):
        raise AcceptanceFailure("stage_b_not_passed")
    complete = stage_b is not None
    return {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "acceptance": "Music Vault Batch 11 essential E2E",
        "status": "passed" if complete else "pending_live_migration",
        "finished_at": utc_now(),
        "stage_a": dict(stage_a),
        "stage_b": dict(stage_b) if stage_b is not None else {
            "status": "pending_live_migration",
            "requires_explicit_live_flag": True,
        },
        "network_or_secret_access": False,
        "personal_values_emitted": False,
    }


__all__ = [
    "AcceptanceFailure",
    "EVIDENCE_SCHEMA_VERSION",
    "POST_SCHEMA_VERSION",
    "PRE_SCHEMA_VERSION",
    "TEMP_PREFIX",
    "aggregate_digest",
    "atomic_write_json",
    "combine_summaries",
    "config_guard",
    "database_guard",
    "file_guard",
    "media_guard",
    "prepare_live_manifest",
    "read_json",
    "runtime_guard",
    "safe_temporary_root",
    "sha256_file",
    "tree_guard",
    "verify_live_migration",
    "verify_network_report",
]
