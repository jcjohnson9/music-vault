from __future__ import annotations

"""Batch 10.3 live schema-7 preservation gate.

The gate has four explicit phases: immutable baseline capture, verified SQLite
backup creation, disposable-clone dry-run analysis, and post-launch
verification.  It never launches Music Vault and never performs a provider
request.  Its JSON output is aggregate-only.
"""

import argparse
import contextlib
import gc
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.core.db import MusicVaultDB  # noqa: E402
from music_vault.metadata.artist_consolidation import (  # noqa: E402
    analyze_existing_artist_consolidation,
)
from music_vault.metadata.canonical_albums import (  # noqa: E402
    analyze_canonical_album_backfill,
    create_canonical_media_schema,
)
from music_vault.metadata.review_reclassification import (  # noqa: E402
    reclassify_stored_review_items,
)
from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402


ACKNOWLEDGEMENT = "batch10.3-live-schema-6-to-7"
CLONE_PREFIX = "MusicVault_Batch10_3_LiveDryRun_"
AUTOMATIC_BACKUP_PATTERN = re.compile(
    r"music_vault_pre_schema_v7_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"
    r"(?:_\d+)?\.sqlite3"
)


class GateFailure(acceptance.AcceptanceFailure):
    """A stable, non-identifying live-gate failure."""


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
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateFailure("process_check_failed") from exc
    return "musicvault.exe" in result.stdout.casefold()


def _execution_guard(acknowledgement: str) -> None:
    if acknowledgement != ACKNOWLEDGEMENT:
        raise GateFailure("live_acknowledgement_missing")
    acceptance.ensure_no_secret_mode()
    if _music_vault_running():
        raise GateFailure("music_vault_process_running")


@contextlib.contextmanager
def _offline_guard():
    attempts = {"count": 0}

    def blocked(*_args, **_kwargs):
        attempts["count"] += 1
        raise GateFailure("network_access_blocked")

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


def _baseline_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    # Creating the explicitly requested rollback after baseline capture changes
    # only the backup inventory.  The live database, media and runtime guards
    # must still match exactly before clone analysis begins.
    required = (
        "baseline_format_version",
        "database",
        "media",
        "artwork",
        "runtime_guards",
        "credential_metadata",
        "raw_library_values_emitted",
        "credential_contents_read",
        "media_contents_read",
        "artwork_contents_hashed",
    )
    return all(left.get(key) == right.get(key) for key in required)


def capture_baseline(*, project_root: Path, data_dir: Path, database: Path) -> dict[str, Any]:
    return acceptance.capture_database_baseline(
        project_root=project_root,
        data_dir=data_dir,
        database=database,
        expected_schema=acceptance.PRE_SCHEMA_VERSION,
    )


def _utc_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def create_verified_backup(
    *,
    database: Path,
    backup_dir: Path,
    baseline: Mapping[str, Any],
    backup_path: Path | None = None,
) -> dict[str, Any]:
    destination = backup_path or (
        Path(backup_dir) / f"music_vault_batch10_3_pre_schema7_{_utc_stamp()}.sqlite3"
    )
    result = acceptance.create_verified_sqlite_backup(
        database=database,
        backup=destination,
        baseline=baseline,
    )
    return {
        **result,
        "backup_name_digest": hashlib.sha256(destination.name.encode("utf-8")).hexdigest(),
        "path_emitted": False,
    }


def _clone_database(source: Path, destination: Path) -> None:
    # sqlite3.Connection.__exit__ commits or rolls back but does not close the
    # handle.  Explicitly close read-only handles so Windows can immediately
    # replace/delete disposable clones during the same acceptance run.
    with contextlib.closing(
        acceptance.readonly(source, immutable=False)
    ) as source_connection:
        target = sqlite3.connect(destination)
        try:
            source_connection.backup(target)
            target.commit()
        finally:
            target.close()


def clone_dry_run(
    *,
    project_root: Path,
    data_dir: Path,
    database: Path,
    baseline: Mapping[str, Any],
    temporary_parent: Path | None = None,
) -> dict[str, Any]:
    source = Path(database).expanduser().resolve()
    source_hash = acceptance.sha256_file(source)
    source_stat = (source.stat().st_size, source.stat().st_mtime_ns)
    current = capture_baseline(project_root=project_root, data_dir=data_dir, database=source)
    if not _baseline_matches(current, baseline):
        raise GateFailure("live_database_changed_since_baseline")

    temporary_root = Path(
        tempfile.mkdtemp(prefix=CLONE_PREFIX, dir=temporary_parent)
    ).resolve()
    if acceptance.is_within(temporary_root, Path(project_root).resolve()):
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise GateFailure("temporary_clone_inside_repository")
    clone_data = temporary_root / "data"
    clone_data.mkdir()
    clone = clone_data / "schema6-clone.sqlite3"
    try:
        _clone_database(source, clone)
        clone_baseline = acceptance.capture_database_baseline(
            project_root=temporary_root,
            data_dir=clone_data,
            database=clone,
            expected_schema=acceptance.PRE_SCHEMA_VERSION,
        )
        if clone_baseline["database"]["tables"] != baseline["database"]["tables"]:
            raise GateFailure("temporary_clone_logical_mismatch")
        connection = sqlite3.connect(clone)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                create_canonical_media_schema(connection)
            with _offline_guard() as analysis_attempts:
                album = analyze_canonical_album_backfill(connection)
                artist_plan = analyze_existing_artist_consolidation(connection)
                review = reclassify_stored_review_items(connection, apply=False)
        finally:
            connection.close()
        if int(analysis_attempts["count"]) != 0:
            raise GateFailure("provider_or_network_access_observed")

        # Recreate an untouched schema-6 clone before running the real database
        # migration.  The analysis schema above exists only to let every planner
        # inspect the future alias/relationship tables; it must not contaminate
        # the pre-v7 backup or the migration proof.
        clone.unlink()
        _clone_database(source, clone)
        with _offline_guard() as migration_attempts:
            migrated = MusicVaultDB(clone, backup_dir=clone_data / "backups")
            migrated.close()
        if int(migration_attempts["count"]) != 0:
            raise GateFailure("provider_or_network_access_observed")
        with contextlib.closing(
            acceptance.readonly(clone, immutable=False)
        ) as migrated_connection:
            health = acceptance.database_health(migrated_connection)
            expected_post_state = acceptance.capture_post_migration_semantics(
                migrated_connection
            )
        if (
            int(health["schema_version"]) != acceptance.POST_SCHEMA_VERSION
            or health["integrity_ok"] is not True
            or int(health["foreign_key_issue_count"]) != 0
        ):
            raise GateFailure("temporary_clone_migration_failed")
        planned_change_counts = {
            "safe_album_group_count": int(
                album.get("proposed_canonical_album_count", 0)
            ),
            "ambiguous_album_group_count": int(album.get("ambiguous_group_count", 0)),
            "eligible_album_track_count": int(album.get("eligible_track_count", 0)),
            "safe_artist_merge_group_count": len(artist_plan.merges),
            "safe_artist_merge_count": int(artist_plan.duplicate_artist_count),
            "artist_conflict_count": len(artist_plan.conflicts),
            "malformed_version_artist_repair_count": len(artist_plan.version_repairs),
            "full_credit_repair_count": len(artist_plan.full_credit_repairs),
            "review_scanned_count": int(review.scanned),
            "review_applied_count": int(review.applied),
            "review_applied_with_gaps_count": int(review.applied_with_gaps),
            "review_source_fallback_count": int(review.source_fallback),
            "review_needs_review_count": int(review.needs_review),
        }
        result = {
            "ok": True,
            **planned_change_counts,
            "dry_run_format_version": 1,
            "baseline_fingerprint": acceptance.baseline_fingerprint(baseline),
            "planned_change_counts": planned_change_counts,
            "expected_post_state": expected_post_state,
            "network_attempt_count": 0,
            "clone_schema_version": int(health["schema_version"]),
            "clone_integrity_ok": bool(health["integrity_ok"]),
            "clone_foreign_key_issue_count": int(health["foreign_key_issue_count"]),
            "temporary_root_outside_repository": True,
            "temporary_root_deleted": True,
            "source_database_unchanged": False,
            "raw_library_values_emitted": False,
        }
    finally:
        gc.collect()
        shutil.rmtree(temporary_root, ignore_errors=False)
    unchanged = (
        acceptance.sha256_file(source) == source_hash
        and (source.stat().st_size, source.stat().st_mtime_ns) == source_stat
    )
    if not unchanged:
        raise GateFailure("live_database_changed_during_clone_dry_run")
    result["source_database_unchanged"] = True
    return result


def _current_backup_inventory(data_dir: Path) -> list[dict[str, Any]]:
    directory = Path(data_dir) / "backups"
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.sqlite3"), key=lambda item: item.name):
        stat = path.stat()
        result.append(
            {
                "name_digest": hashlib.sha256(path.name.encode("utf-8")).hexdigest(),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "path": path,
            }
        )
    return result


def _status_safe(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    # Identity-bearing legacy fields and personal filesystem locations must
    # remain neutral while their established schema keys stay compatible.
    playback = payload.get("playback")
    sync = payload.get("sync")
    paths = payload.get("paths")
    if (
        not isinstance(playback, dict)
        or not isinstance(sync, dict)
        or not isinstance(paths, dict)
    ):
        return False
    for key in ("currently_playing", "current_title", "current_artist", "current_album"):
        if playback.get(key) is not None:
            return False
    for key in ("last_sync_playlist_title", "last_sync_playlist_id", "last_sync_error"):
        if sync.get(key) is not None:
            return False
    for key in (
        "project_root",
        "data_dir",
        "database",
        "downloads",
        "config",
        "status_file",
    ):
        if key not in paths or paths.get(key) is not None:
            return False
    encoded_keys: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            encoded_keys.extend(str(key).casefold() for key in value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    forbidden = ("provider_reference", "source_url", "image_url", "discogs_artist_id")
    return not any(any(part in key for part in forbidden) for key in encoded_keys)


def _verify_history_subset(
    baseline_hashes: Sequence[str], current_hashes: Sequence[str]
) -> bool:
    return set(baseline_hashes) <= set(current_hashes)


def _verify_credit_semantic_subset(
    baseline_hashes: Sequence[str], current_hashes: Sequence[str]
) -> bool:
    return set(baseline_hashes) <= set(current_hashes)


def _preservation_counts_match(
    baseline_counts: Mapping[str, Any], current_counts: Mapping[str, Any]
) -> bool:
    # Metadata history/observations may grow when schema 7 materializes its
    # conservative stored-evidence decisions. Artist-credit rows may shrink
    # only when consolidation removes an exact duplicate. Exact permitted
    # outcomes are independently bound to the disposable-clone digests.
    exact_names = set(baseline_counts) - {
        "track_metadata_history",
        "track_metadata_observations",
        "track_artist_credits",
    }
    return all(
        current_counts.get(name) == baseline_counts[name] for name in exact_names
    )


def _validated_expected_post_state(
    baseline: Mapping[str, Any], dry_run: Mapping[str, Any]
) -> Mapping[str, Any]:
    if (
        dry_run.get("ok") is not True
        or dry_run.get("dry_run_format_version") != 1
        or dry_run.get("source_database_unchanged") is not True
        or dry_run.get("temporary_root_deleted") is not True
        or dry_run.get("network_attempt_count") != 0
        or dry_run.get("baseline_fingerprint")
        != acceptance.baseline_fingerprint(baseline)
    ):
        raise GateFailure("dry_run_artifact_invalid")
    expected_post_state = dry_run.get("expected_post_state")
    if (
        not isinstance(expected_post_state, dict)
        or expected_post_state.get("semantic_format_version")
        != acceptance.POST_MIGRATION_SEMANTIC_FORMAT_VERSION
        or expected_post_state.get("schema_version") != acceptance.POST_SCHEMA_VERSION
        or expected_post_state.get("aggregate_only") is not True
        or not isinstance(expected_post_state.get("guards"), dict)
    ):
        raise GateFailure("dry_run_expected_state_invalid")
    return expected_post_state


def validate_launch_preflight(
    *,
    baseline: Mapping[str, Any],
    dry_run: Mapping[str, Any],
    project_root: Path,
    data_dir: Path,
    database: Path,
    backup_path: Path,
) -> dict[str, Any]:
    """Fail closed before the controlled EXE is allowed to touch schema 6."""

    _validated_expected_post_state(baseline, dry_run)
    current = capture_baseline(
        project_root=project_root,
        data_dir=data_dir,
        database=database,
    )
    if not _baseline_matches(current, baseline):
        raise GateFailure("live_database_changed_since_dry_run")
    backup = acceptance.verify_sqlite_backup(
        backup=backup_path,
        baseline=baseline,
        expected_schema=acceptance.PRE_SCHEMA_VERSION,
    )
    return {
        "ok": True,
        "baseline_matches": True,
        "dry_run_bound_to_baseline": True,
        "rollback_backup_verified": backup["verified"] is True,
        "schema_version": acceptance.PRE_SCHEMA_VERSION,
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def verify_migration(
    *,
    baseline: Mapping[str, Any],
    dry_run: Mapping[str, Any],
    project_root: Path,
    data_dir: Path,
    database: Path,
    backup_path: Path,
    network_report: Path,
) -> dict[str, Any]:
    if baseline.get("baseline_format_version") != acceptance.BASELINE_FORMAT_VERSION:
        raise GateFailure("baseline_version_unsupported")
    expected_post_state = _validated_expected_post_state(baseline, dry_run)
    network_evidence = acceptance.verify_acceptance_network_report(network_report)
    root = Path(project_root).expanduser().resolve()
    data = Path(data_dir).expanduser().resolve()
    current = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_schema=acceptance.POST_SCHEMA_VERSION,
    )
    before_db = baseline["database"]
    after_db = current["database"]
    before_tables = before_db["tables"]
    after_tables = after_db["tables"]
    before_names = set(before_tables)
    after_names = set(after_tables)

    exact_protected = all(
        after_db["protected_tables"].get(name) == guard
        for name, guard in before_db["protected_tables"].items()
    )
    preservation_counts = before_db["preservation_counts"]
    current_counts = after_db["preservation_counts"]
    counts_exact = _preservation_counts_match(preservation_counts, current_counts)
    history_count_preserved = current_counts.get("track_metadata_history", 0) >= preservation_counts.get(
        "track_metadata_history", 0
    )
    history_rows_preserved = _verify_history_subset(
        before_db["metadata_history_row_hashes"],
        after_db["metadata_history_row_hashes"],
    )
    metadata_field_keys_preserved = set(before_db["metadata_field_key_hashes"]) <= set(
        after_db["metadata_field_key_hashes"]
    )
    protected_metadata_fields_exact = (
        after_db["protected_metadata_field_guard"]
        == before_db["protected_metadata_field_guard"]
    )
    manual_locked_metadata_fields_exact = (
        after_db["manual_locked_metadata_field_guard"]
        == before_db["manual_locked_metadata_field_guard"]
    )
    protected_credit_semantics_preserved = set(
        before_db["protected_artist_credit_semantic_hashes"]
    ) <= set(after_db["protected_artist_credit_semantic_hashes"])
    credit_semantics_preserved = _verify_credit_semantic_subset(
        before_db["artist_credit_semantic_hashes"],
        after_db["artist_credit_semantic_hashes"],
    )
    credited_tracks_preserved = set(before_db["credited_track_hashes"]) <= set(
        after_db["credited_track_hashes"]
    )

    explicit_backup = acceptance.verify_sqlite_backup(
        backup=backup_path,
        baseline=baseline,
        expected_schema=acceptance.PRE_SCHEMA_VERSION,
    )
    before_inventory = {
        item["name_digest"] for item in baseline.get("backup_inventory", [])
    }
    automatic_candidates = [
        item
        for item in _current_backup_inventory(data)
        if item["name_digest"] not in before_inventory
        and AUTOMATIC_BACKUP_PATTERN.fullmatch(item["path"].name)
    ]
    verified_automatic = 0
    for item in automatic_candidates:
        try:
            acceptance.verify_sqlite_backup(
                backup=item["path"],
                baseline=baseline,
                expected_schema=acceptance.PRE_SCHEMA_VERSION,
            )
        except acceptance.AcceptanceFailure:
            continue
        verified_automatic += 1

    with contextlib.closing(
        acceptance.readonly(Path(database), immutable=False)
    ) as connection:
        actual_post_state = acceptance.capture_post_migration_semantics(connection)
        canonical_count = int(connection.execute("SELECT COUNT(*) FROM canonical_albums").fetchone()[0])
        membership_count = int(
            connection.execute("SELECT COUNT(*) FROM track_album_memberships").fetchone()[0]
        )
        artist_count = int(connection.execute("SELECT COUNT(*) FROM artists").fetchone()[0])
        alias_count = int(connection.execute("SELECT COUNT(*) FROM artist_aliases").fetchone()[0])
        relationship_count = int(
            connection.execute("SELECT COUNT(*) FROM artist_relationships").fetchone()[0]
        )
        duplicate_memberships = int(
            connection.execute(
                "SELECT COUNT(*) FROM (SELECT track_id FROM track_album_memberships "
                "GROUP BY track_id HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
        orphan_credits = int(
            connection.execute(
                "SELECT COUNT(*) FROM track_artist_credits c LEFT JOIN artists a ON a.id=c.artist_id "
                "LEFT JOIN tracks t ON t.id=c.track_id WHERE a.id IS NULL OR t.id IS NULL"
            ).fetchone()[0]
        )
        provider_observation_count = sum(
            int(connection.execute(f"SELECT COUNT(*) FROM {acceptance.quote_identifier(name)}").fetchone()[0])
            for name in acceptance.PROVIDER_TABLES
            if name in after_names
        )

    required_indexes = acceptance.REQUIRED_V7_INDEXES <= set(after_db["indexes"])
    baseline_indexes_preserved = set(before_db["indexes"]) <= set(after_db["indexes"])
    expected_guards = expected_post_state["guards"]
    actual_guards = actual_post_state["guards"]
    provider_rows_preserved = all(
        set(hashes) <= set(after_db.get("provider_row_hashes", {}).get(name, ()))
        for name, hashes in before_db.get("provider_row_hashes", {}).items()
    )
    semantic_checks = {
        f"dry_run_{name}_exact": actual_guards.get(name) == guard
        for name, guard in expected_guards.items()
    }
    checks = {
        "dry_run_bound_to_exact_baseline": True,
        "dry_run_semantic_guard_set_exact": (
            set(expected_guards) == set(actual_guards)
        ),
        "dry_run_post_state_schema_exact": (
            actual_post_state.get("schema_version")
            == expected_post_state.get("schema_version")
        ),
        **semantic_checks,
        "schema_migrated_6_to_7": after_db["health"]["schema_version"] == 7,
        "integrity_ok": after_db["health"]["integrity_ok"] is True,
        "foreign_keys_enabled": after_db["health"]["foreign_keys_enabled"] is True,
        "foreign_key_check_clean": after_db["health"]["foreign_key_issue_count"] == 0,
        "all_schema6_tables_remain": before_names <= after_names,
        "only_expected_v7_tables_added": after_names == before_names | set(acceptance.V7_TABLES),
        "all_protected_tables_exact": exact_protected,
        "track_ids_exact": after_db["track_id_guard"] == before_db["track_id_guard"],
        "track_stable_values_exact": after_db["track_stable_guard"] == before_db["track_stable_guard"],
        "track_release_context_stable_values_exact": (
            after_db["track_release_context_stable_guard"]
            == before_db["track_release_context_stable_guard"]
        ),
        "track_paths_exact": after_db["track_path_digest"] == before_db["track_path_digest"],
        "track_cover_paths_exact": (
            after_db["track_cover_path_digest"] == before_db["track_cover_path_digest"]
        ),
        "preservation_counts_exact": counts_exact,
        "metadata_history_count_not_reduced": history_count_preserved,
        "metadata_history_rows_preserved": history_rows_preserved,
        "metadata_field_keys_not_removed": metadata_field_keys_preserved,
        "non_artist_version_metadata_fields_exact": protected_metadata_fields_exact,
        "manual_and_locked_metadata_fields_exact": manual_locked_metadata_fields_exact,
        "artist_credit_semantic_evidence_preserved": credit_semantics_preserved,
        "manual_and_locked_artist_credits_preserved": protected_credit_semantics_preserved,
        "credited_tracks_remain_credited": credited_tracks_preserved,
        "artist_provider_identity_set_preserved": (
            after_db["artist_provider_id_guard"] == before_db["artist_provider_id_guard"]
        ),
        "provider_rows_preserved": provider_rows_preserved,
        "intelligence_item_identity_and_evidence_preserved": (
            after_db["intelligence_stable"].get("items")
            == before_db["intelligence_stable"].get("items")
        ),
        "intelligence_job_identity_preserved": (
            after_db["intelligence_stable"].get("jobs")
            == before_db["intelligence_stable"].get("jobs")
        ),
        "media_metadata_unchanged": current["media"] == baseline["media"],
        "referenced_cover_files_unchanged": (
            current["artwork"]["referenced_cover_files"]
            == baseline["artwork"]["referenced_cover_files"]
        ),
        "artist_image_tree_unchanged": (
            current["artwork"]["artist_image_tree"]
            == baseline["artwork"]["artist_image_tree"]
        ),
        "runtime_config_archive_failure_metadata_unchanged": (
            current["runtime_guards"] == baseline["runtime_guards"]
        ),
        "credential_metadata_unchanged_without_reading_contents": (
            current["credential_metadata"] == baseline["credential_metadata"]
            and current["credential_contents_read"] is False
        ),
        "required_v7_indexes_present": required_indexes,
        "baseline_indexes_preserved": baseline_indexes_preserved,
        "canonical_membership_count_expected": (
            membership_count == int(before_db["eligible_album_track_count"])
        ),
        "one_membership_per_track": duplicate_memberships == 0,
        "artist_credits_have_no_orphans": orphan_credits == 0,
        "blank_artist_display_count_not_increased": (
            int(after_db["blank_artist_display_count"])
            <= int(before_db["blank_artist_display_count"])
        ),
        "explicit_rollback_backup_verified": explicit_backup["verified"] is True,
        "automatic_schema7_backup_created_and_verified": verified_automatic >= 1,
        "acceptance_network_guard_verified": network_evidence["verified"] is True,
        "no_provider_or_network_request_observed": (
            int(network_evidence["attempt_count"]) == 0
        ),
        "app_status_aggregate_only": _status_safe(data / "music_vault_status.json"),
        "sqlite_sidecars_absent": not any(
            Path(str(Path(database)) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        ),
        "packaged_data_folder_absent": not (root / "dist" / "MusicVault" / "data").exists(),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "counts": {
            "track_count": int(after_db["tables"]["tracks"]["count"]),
            "canonical_album_count": canonical_count,
            "canonical_album_membership_count": membership_count,
            "canonical_artist_count": artist_count,
            "artist_alias_count": alias_count,
            "artist_relationship_count": relationship_count,
            "legacy_album_card_count_before": int(before_db["legacy_album_card_count"]),
            "canonical_album_card_count_after": canonical_count,
            "artist_card_count_before": int(before_db["artist_card_count"]),
            "artist_card_count_after": int(after_db["artist_card_count"]),
            "review_counts_before": dict(before_db["review_counts"]),
            "review_counts_after": dict(after_db["review_counts"]),
            "provider_observation_count": provider_observation_count,
            "automatic_schema7_backup_count": len(automatic_candidates),
            "verified_automatic_schema7_backup_count": verified_automatic,
            "network_attempt_count": int(network_evidence["attempt_count"]),
        },
        "planned_change_counts": dict(dry_run.get("planned_change_counts") or {}),
        "semantic_post_state_matched": all(semantic_checks.values()),
        "backup": {
            "explicit_verified": True,
            "explicit_size": int(explicit_backup["size"]),
            "explicit_sha256": str(explicit_backup["sha256"]),
        },
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("baseline", "create-backup", "clone-dry-run", "launch-preflight", "verify"),
    )
    parser.add_argument("--acknowledge-live-library", required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--dry-run", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--network-report", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = _parser().parse_args(argv)
    _execution_guard(args.acknowledge_live_library)
    root = args.project_root.expanduser().resolve()
    data = (args.data_dir or root / "data").expanduser().resolve()
    database = (args.database or data / "music_vault.sqlite3").expanduser().resolve()
    if args.mode == "baseline":
        result = capture_baseline(project_root=root, data_dir=data, database=database)
    else:
        if args.baseline is None:
            raise GateFailure("baseline_path_required")
        baseline = acceptance.read_json(args.baseline)
        if args.mode == "create-backup":
            if args.backup is None:
                raise GateFailure("backup_path_required")
            result = create_verified_backup(
                database=database,
                backup_dir=args.backup.parent,
                backup_path=args.backup,
                baseline=baseline,
            )
        elif args.mode == "clone-dry-run":
            result = clone_dry_run(
                project_root=root,
                data_dir=data,
                database=database,
                baseline=baseline,
            )
        elif args.mode == "launch-preflight":
            if args.backup is None or args.dry_run is None:
                raise GateFailure("launch_preflight_evidence_path_required")
            result = validate_launch_preflight(
                baseline=baseline,
                dry_run=acceptance.read_json(args.dry_run),
                project_root=root,
                data_dir=data,
                database=database,
                backup_path=args.backup,
            )
        else:
            if args.backup is None or args.dry_run is None or args.network_report is None:
                raise GateFailure("verify_evidence_path_required")
            result = verify_migration(
                baseline=baseline,
                dry_run=acceptance.read_json(args.dry_run),
                project_root=root,
                data_dir=data,
                database=database,
                backup_path=args.backup,
                network_report=args.network_report,
            )
    if args.output is not None:
        acceptance.atomic_write_json(args.output, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(argv)
    except (acceptance.AcceptanceFailure, OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        print(json.dumps({"ok": False, "error_code": "batch10_3_live_gate_failed"}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("ok", True) is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
