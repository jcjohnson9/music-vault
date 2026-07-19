from __future__ import annotations

"""Aggregate-only Batch 10.5 metadata-acceptance repair gate.

The module has no import-time side effects.  Its dry-run path applies the
complete repair to a disposable SQLite backup, while the live path is guarded
by explicit acknowledgement, no-secret/no-network policy, verified backups,
one database transaction, and preservation fingerprints.  Reports contain
only counts, booleans, hashes, and backup file names--never library values,
provider identifiers, media paths, or credential contents.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.metadata.artist_consolidation import (  # noqa: E402
    ArtistConsolidationPlan,
    ArtistConsolidationService,
)
from music_vault.metadata.acceptance_repair import (  # noqa: E402
    METADATA_ACCEPTANCE_REPAIR_MARKER,
    SAFE_DIAGNOSTIC_ARTIST_CONFLICT_REASONS,
    apply_metadata_acceptance_repair,
    unexpected_artist_identity_conflict_count,
)
from music_vault.metadata.artist_images import (  # noqa: E402
    ArtistIdentity,
    ArtistImageCache,
)
from music_vault.metadata.canonical_albums import (  # noqa: E402
    analyze_canonical_album_backfill,
    required_canonical_media_indexes,
)
from music_vault.metadata.review_reclassification import (  # noqa: E402
    best_available_reclassify,
)
from music_vault.core import acceptance_network  # noqa: E402
from music_vault.core.library_browser import (  # noqa: E402
    query_album_summaries,
    query_artist_summaries,
)
from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402


SCHEMA_VERSION = 7
REPORT_FORMAT_VERSION = 1
REPAIR_MARKER = METADATA_ACCEPTANCE_REPAIR_MARKER
LIVE_ACKNOWLEDGEMENT = "batch10.5-live-metadata-acceptance-repair"
NO_NETWORK_ENVIRONMENT = "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK"
DATABASE_BACKUP_PREFIX = "music_vault_batch10_5_pre_metadata_acceptance_repair_"
INDEX_BACKUP_PREFIX = "artist_images_index_batch10_5_"

PROTECTED_TABLES = frozenset(
    {
        "playlists",
        "playlist_tracks",
        "playlist_origins",
        "sync_sources",
        "sync_source_items",
        "track_source_memberships",
        "source_track_identities",
        "source_identity_conflicts",
        "metadata_remediation_jobs",
        "metadata_remediation_items",
    }
)
COUNT_TABLES = frozenset(
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
        "artists",
        "artist_aliases",
        "artist_relationships",
        "track_artist_credits",
        "canonical_albums",
        "track_album_memberships",
        "track_metadata_fields",
        "track_metadata_history",
        "track_metadata_observations",
        "metadata_intelligence_jobs",
        "metadata_intelligence_items",
        "metadata_remediation_jobs",
        "metadata_remediation_items",
    }
)
RUNTIME_GUARD_FILES = (
    "music_vault_config.json",
    "youtube_download_archive.txt",
    "youtube_failed_ids.txt",
    "music_vault_status.json",
)
CREDENTIAL_FILES = ("youtube_api_key.txt", "discogs_token.txt")


class Batch105Failure(acceptance.AcceptanceFailure):
    """A stable non-identifying acceptance failure."""


class _DatabaseAdapter:
    """The narrow MusicVaultDB protocol needed by metadata application."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection

    def get_track(self, track_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tracks WHERE id=?", (int(track_id),)
        ).fetchone()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _aggregate_digest(records: Iterable[str]) -> str:
    return acceptance.aggregate_digest(records)


def _path_digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", "surrogatepass")).hexdigest()


def _file_stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False, "size": 0, "mtime_ns": 0}
    return {
        "exists": path.is_file(),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _marker_present(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "app_meta"):
        return False
    return conn.execute(
        "SELECT 1 FROM app_meta WHERE key=?", (REPAIR_MARKER,)
    ).fetchone() is not None


def _health(conn: sqlite3.Connection) -> dict[str, Any]:
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_key_issues = int(len(conn.execute("PRAGMA foreign_key_check").fetchall()))
    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
    }
    required = set(required_canonical_media_indexes()) | set(
        acceptance.REQUIRED_V7_INDEXES
    )
    return {
        "schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "integrity_ok": integrity.casefold() == "ok",
        "foreign_key_issue_count": foreign_key_issues,
        "required_index_count": len(required),
        "missing_required_index_count": len(required - indexes),
    }


def _database_guards(conn: sqlite3.Connection) -> dict[str, Any]:
    names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    }
    protected = {
        name: acceptance.table_guard(conn, name)
        for name in sorted(PROTECTED_TABLES & names)
    }
    counts = {
        name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name in sorted(COUNT_TABLES & names)
    }
    track_identity = acceptance.query_guard(
        conn, "SELECT id,path,cover_path FROM tracks ORDER BY id"
    )
    track_ids = acceptance.query_guard(conn, "SELECT id FROM tracks ORDER BY id")
    manual_locked_fields = (
        acceptance.query_guard(
            conn,
            "SELECT track_id,field_name,value,provenance,provider_reference,"
            "confidence,is_manual,is_locked FROM track_metadata_fields "
            "WHERE is_manual=1 OR is_locked=1 ORDER BY track_id,field_name",
        )
        if "track_metadata_fields" in names
        else {"count": 0, "digest": _aggregate_digest(())}
    )
    manual_locked_credits = (
        acceptance.query_guard(
            conn,
            "SELECT track_id,artist_id,role,credit_order,join_phrase,provenance,"
            "provider_reference,confidence,is_manual,is_locked "
            "FROM track_artist_credits WHERE is_manual=1 OR is_locked=1 "
            "ORDER BY track_id,credit_order,id",
        )
        if "track_artist_credits" in names
        else {"count": 0, "digest": _aggregate_digest(())}
    )
    review_count = (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items "
                "WHERE state IN ('review','ready','no_match')"
            ).fetchone()[0]
        )
        if "metadata_intelligence_items" in names
        else 0
    )
    evidence_subsets: dict[str, dict[str, Any]] = {}

    def evidence_guard(table: str, selected: Sequence[str] | None = None) -> None:
        if table not in names:
            return
        columns = selected or acceptance.columns(conn, table)
        rows = conn.execute(
            "SELECT "
            + ",".join(acceptance.quote_identifier(value) for value in columns)
            + " FROM "
            + acceptance.quote_identifier(table)
        ).fetchall()
        hashes = sorted(acceptance.row_digest(tuple(row)) for row in rows)
        evidence_subsets[table] = {
            "count": len(hashes),
            "digest": _aggregate_digest(hashes),
            "row_hashes": hashes,
        }

    evidence_guard("track_metadata_history")
    evidence_guard(
        "track_metadata_observations",
        (
            "id",
            "observation_key",
            "track_id",
            "provider",
            "field_name",
            "value",
            "provider_reference",
            "confidence",
        ),
    )
    evidence_guard("metadata_provider_cache")
    if "artist_relationships" in names:
        evidence_guard(
            "artist_relationships",
            tuple(
                column
                for column in (
                    "relationship_kind",
                    "provenance",
                    "provider_reference",
                    "confidence",
                    "created_at",
                )
                if column in acceptance.columns(conn, "artist_relationships")
            ),
        )
    intelligence_evidence_columns = [
        column
        for column in (
            "id",
            "job_id",
            "track_id",
            "field_proposal",
            "field_confidence",
            "provider_agreement",
            "parsed_hints",
        )
        if "metadata_intelligence_items" in names
        and column in acceptance.columns(conn, "metadata_intelligence_items")
    ]
    if intelligence_evidence_columns:
        evidence_guard("metadata_intelligence_items", intelligence_evidence_columns)

    terminal_states = (
        {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT state,COUNT(*) FROM metadata_intelligence_items "
                "GROUP BY state ORDER BY state"
            ).fetchall()
        }
        if "metadata_intelligence_items" in names
        else {}
    )
    artist_cards = query_artist_summaries(conn)
    album_cards = query_album_summaries(conn)
    normalized_displays = Counter(
        " ".join(str(card.display_name).casefold().split()) for card in artist_cards
    )
    browser = {
        "top_level_artist_card_count": len(artist_cards),
        "duplicate_normalized_undisambiguated_artist_card_count": sum(
            count - 1 for count in normalized_displays.values() if count > 1
        ),
        "album_card_count": len(album_cards),
        "per_artist_unknown_album_card_count": sum(
            1
            for card in album_cards
            if str(card.album_title).strip().casefold() in {"unknown", "unknown album"}
            and bool(str(card.album_artist).strip())
        ),
        "virtual_singles_uncatalogued_card_count": sum(
            1
            for card in album_cards
            if str(getattr(card.key, "virtual_kind", "")) == "singles_uncatalogued"
        ),
    }
    return {
        "health": _health(conn),
        "counts": counts,
        "protected_tables": protected,
        "track_ids": track_ids,
        "track_identity": track_identity,
        "manual_locked_fields": manual_locked_fields,
        "manual_locked_credits": manual_locked_credits,
        "review_count": review_count,
        "terminal_state_counts": terminal_states,
        "browser_outcomes": browser,
        "evidence_subsets": evidence_subsets,
        "marker_present": _marker_present(conn),
    }


def _inventory_from_paths(paths: Iterable[Path]) -> dict[str, Any]:
    records: list[str] = []
    count = total_bytes = missing = symlinks = 0
    for path in paths:
        candidate = Path(path).expanduser()
        if candidate.is_symlink():
            symlinks += 1
            records.append(acceptance.row_digest((_path_digest(candidate), "symlink")))
            continue
        try:
            stat = candidate.stat()
        except OSError:
            missing += 1
            records.append(acceptance.row_digest((_path_digest(candidate), "missing")))
            continue
        if not candidate.is_file():
            missing += 1
            records.append(acceptance.row_digest((_path_digest(candidate), "not_file")))
            continue
        content_digest = _sha256_file(candidate)
        count += 1
        total_bytes += int(stat.st_size)
        records.append(
            acceptance.row_digest(
                (
                    _path_digest(candidate),
                    int(stat.st_size),
                    int(stat.st_mtime_ns),
                    content_digest,
                )
            )
        )
    return {
        "file_count": count,
        "total_bytes": total_bytes,
        "missing_count": missing,
        "symlink_count": symlinks,
        "inventory_digest": _aggregate_digest(records),
    }


def _media_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    return _inventory_from_paths(
        Path(str(row[0]))
        for row in conn.execute("SELECT path FROM tracks ORDER BY id").fetchall()
    )


def _portrait_inventory(cache_root: Path) -> dict[str, Any]:
    files_root = Path(cache_root).expanduser().resolve() / "files"
    files = (
        sorted(
            (path for path in files_root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(files_root).as_posix(),
        )
        if files_root.is_dir()
        else ()
    )
    return _inventory_from_paths(files)


def capture_preservation_state(
    *, project_root: Path, database: Path, cache_root: Path
) -> dict[str, Any]:
    """Capture an aggregate-only preservation fingerprint.

    Credential files are inspected with ``stat`` only.  Media and portrait
    contents are hashed to prove preservation but their paths and hashes are
    folded into one inventory digest before reporting.
    """

    root = Path(project_root).expanduser().resolve()
    data = root / "data"
    db_path = Path(database).expanduser().resolve()
    image_root = Path(cache_root).expanduser().resolve()
    if not acceptance.is_within(db_path, data) or not acceptance.is_within(
        image_root, data
    ):
        raise Batch105Failure("runtime_scope_invalid")
    if not db_path.is_file():
        raise Batch105Failure("database_unavailable")
    sidecars = [Path(str(db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(path.exists() for path in sidecars):
        raise Batch105Failure("sqlite_sidecar_present")
    with contextlib.closing(acceptance.readonly(db_path, immutable=False)) as conn:
        database_state = _database_guards(conn)
        media = _media_inventory(conn)
    return {
        "database": database_state,
        "database_file": {
            **_file_stat(db_path),
            "sha256": _sha256_file(db_path),
        },
        "media": media,
        "portraits": _portrait_inventory(image_root),
        "cache_index": {
            **_file_stat(image_root / "index.json"),
            "sha256": (
                _sha256_file(image_root / "index.json")
                if (image_root / "index.json").is_file()
                else None
            ),
        },
        "credential_metadata": {
            name: _file_stat(data / name) for name in CREDENTIAL_FILES
        },
        "runtime_metadata": {
            name: _file_stat(data / name) for name in RUNTIME_GUARD_FILES
        },
        "credential_contents_read": False,
        "raw_library_values_emitted": False,
    }


def _assert_health(health: Mapping[str, Any]) -> None:
    if health != {
        "schema_version": SCHEMA_VERSION,
        "integrity_ok": True,
        "foreign_key_issue_count": 0,
        "required_index_count": health.get("required_index_count"),
        "missing_required_index_count": 0,
    }:
        raise Batch105Failure("database_health_failed")


def _assert_preserved(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    required_equal = (
        "protected_tables",
        "track_ids",
        "track_identity",
        "manual_locked_fields",
        "manual_locked_credits",
    )
    before_db = before["database"]
    after_db = after["database"]
    for key in required_equal:
        if before_db[key] != after_db[key]:
            raise Batch105Failure(f"preservation_guard_failed_{key}")
    for key in ("tracks", "playlists", "playlist_tracks"):
        if before_db["counts"].get(key) != after_db["counts"].get(key):
            raise Batch105Failure(f"preservation_count_failed_{key}")
    for key in (
        "playlist_origins",
        "sync_sources",
        "sync_source_items",
        "track_source_memberships",
        "source_track_identities",
        "source_identity_conflicts",
        "metadata_remediation_jobs",
        "metadata_remediation_items",
    ):
        if before_db["counts"].get(key) != after_db["counts"].get(key):
            raise Batch105Failure(f"preservation_count_failed_{key}")
    for table, prior in before_db["evidence_subsets"].items():
        current = after_db["evidence_subsets"].get(table)
        if current is None or not set(prior["row_hashes"]).issubset(
            current["row_hashes"]
        ):
            raise Batch105Failure(f"evidence_subset_failed_{table}")
    _assert_health(after_db["health"])


def _plan_counts(plan: ArtistConsolidationPlan) -> dict[str, int]:
    reason_counts = Counter(str(conflict.reason) for conflict in plan.conflicts)
    same_provider = sum(
        count
        for reason, count in reason_counts.items()
        if reason
        in {
            "discogs_id_conflict",
            "musicbrainz_id_conflict",
            "accepted_discogs_artist_id_conflict",
            "accepted_musicbrainz_artist_id_conflict",
        }
    )
    return {
        "artist_merge_groups": len(plan.merges),
        "artist_entities_to_merge": plan.duplicate_artist_count,
        "aliases_to_add": plan.duplicate_artist_count,
        "full_credit_cards_to_remove": len(plan.full_credit_repairs),
        "version_artist_repairs": len(plan.version_repairs),
        "same_provider_identity_conflicts": same_provider,
        "diagnostic_identity_conflicts": sum(
            count
            for reason, count in reason_counts.items()
            if reason in SAFE_DIAGNOSTIC_ARTIST_CONFLICT_REASONS
        ),
        "unexpected_identity_conflicts": unexpected_artist_identity_conflict_count(
            plan
        ),
    }


def analyze_database(conn: sqlite3.Connection) -> dict[str, Any]:
    """Analyze stored evidence without writing or constructing providers."""

    conn.row_factory = sqlite3.Row
    _assert_health(_health(conn))
    if _marker_present(conn):
        return {
            "marker_present": True,
            "no_op": True,
            "artist_merge_groups": 0,
            "artist_entities_to_merge": 0,
            "review_items_to_reclassify": 0,
        }
    adapter = _DatabaseAdapter(conn)
    consolidation = ArtistConsolidationService(adapter)
    plan = consolidation.plan()
    if unexpected_artist_identity_conflict_count(plan):
        raise Batch105Failure("unexpected_artist_identity_conflict")
    review = best_available_reclassify(adapter, apply=False)
    albums = analyze_canonical_album_backfill(conn)
    planned_artist_ids = {
        duplicate
        for merge in plan.merges
        for duplicate in merge.duplicate_artist_ids
    }
    credits_to_reassign = 0
    if planned_artist_ids:
        placeholders = ",".join("?" for _ in planned_artist_ids)
        credits_to_reassign = int(
            conn.execute(
                f"SELECT COUNT(*) FROM track_artist_credits "
                f"WHERE artist_id IN ({placeholders})",
                tuple(sorted(planned_artist_ids)),
            ).fetchone()[0]
        )
    unknown_memberships = int(
        conn.execute(
            "SELECT COUNT(*) FROM track_album_memberships m "
            "JOIN canonical_albums a ON a.id=m.canonical_album_id "
            "WHERE LOWER(TRIM(a.title)) IN ('unknown album','unknown')"
        ).fetchone()[0]
    )
    return {
        "marker_present": False,
        "no_op": False,
        **_plan_counts(plan),
        "credits_to_reassign": credits_to_reassign,
        "review_items_to_reclassify": int(review.scanned),
        "reversed_orientation_repairs": int(review.reversed_orientation_repairs),
        "albums_to_fill": int(review.album_fields_applied),
        "canonical_album_memberships_to_rebuild": int(
            albums.get("eligible_track_count", albums.get("candidate_track_count", 0))
        ),
        "unknown_album_memberships_to_remove": unknown_memberships,
        "unresolved_operational_failures": int(review.operational_failures),
        "required_zero_guards": {
            "track_deletions": 0,
            "track_merges": 0,
            "media_changes": 0,
            "cover_path_changes": 0,
            "source_membership_changes": 0,
            "playlist_changes": 0,
        },
    }


def _connection_state(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"database": _database_guards(conn)}


def apply_database_transaction(conn: sqlite3.Connection) -> dict[str, Any]:
    """Apply the full stored-evidence repair in exactly one outer transaction."""

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _assert_health(_health(conn))
    if _marker_present(conn):
        return {"marker_present": True, "no_op": True, "changes": 0}

    before = _connection_state(conn)
    adapter = _DatabaseAdapter(conn)
    preflight_plan = ArtistConsolidationService(adapter).plan()
    if unexpected_artist_identity_conflict_count(preflight_plan):
        raise Batch105Failure("unexpected_artist_identity_conflict")
    try:
        conn.execute("BEGIN IMMEDIATE")
        changes_before = conn.total_changes
        repair = apply_metadata_acceptance_repair(adapter)
        transaction_changes = conn.total_changes - changes_before
        after = _connection_state(conn)
        _assert_preserved(before, after)
        if int(after["database"]["review_count"]) != 0:
            raise Batch105Failure("review_items_remain")
        if int(
            after["database"]["browser_outcomes"][
                "per_artist_unknown_album_card_count"
            ]
        ) != 0:
            raise Batch105Failure("unknown_album_cards_remain")
        if int(
            after["database"]["browser_outcomes"][
                "duplicate_normalized_undisambiguated_artist_card_count"
            ]
        ) != 0:
            raise Batch105Failure("duplicate_artist_cards_remain")
        if not bool(after["database"]["marker_present"]):
            raise Batch105Failure("repair_marker_missing")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "marker_present": True,
        "no_op": False,
        "repair": asdict(repair),
        "transaction_write_count": int(transaction_changes),
        "track_count": int(after["database"]["counts"].get("tracks", 0)),
        "review_count": int(after["database"]["review_count"]),
    }


def _artist_image_groups(conn: sqlite3.Connection) -> dict[ArtistIdentity, tuple[ArtistIdentity, ...]]:
    aliases: dict[int, list[str]] = {}
    if _table_exists(conn, "artist_aliases"):
        for row in conn.execute(
            "SELECT artist_id,alias_name FROM artist_aliases ORDER BY artist_id,id"
        ).fetchall():
            aliases.setdefault(int(row[0]), []).append(str(row[1]))
    rows = conn.execute(
        "SELECT id,display_name,discogs_artist_id,musicbrainz_artist_id "
        "FROM artists ORDER BY id"
    ).fetchall()
    display_counts = Counter(
        ArtistIdentity.from_display_name(row[1]).normalized_key for row in rows
    )
    alias_counts = Counter(
        ArtistIdentity.from_display_name(alias).normalized_key
        for values in aliases.values()
        for alias in values
    )
    groups: dict[ArtistIdentity, tuple[ArtistIdentity, ...]] = {}
    for row in rows:
        historical = tuple(
            alias
            for alias in aliases.get(int(row[0]), ())
            if alias_counts[ArtistIdentity.from_display_name(alias).normalized_key] == 1
        )
        normalized = ArtistIdentity.from_display_name(row[1]).normalized_key
        identity = ArtistIdentity.from_display_name(
            row[1],
            canonical_artist_id=row[0],
            discogs_artist_id=row[2],
            musicbrainz_artist_id=row[3],
            historical_aliases=historical,
            allow_normalized_name_cache=display_counts[normalized] == 1,
            allow_historical_alias_cache=True,
        )
        related = tuple(
            ArtistIdentity.from_display_name(alias) for alias in historical
        )
        groups[identity] = related
    return groups


def analyze_cache_index(conn: sqlite3.Connection, cache_root: Path) -> dict[str, Any]:
    report = ArtistImageCache(cache_root).repair_index(
        _artist_image_groups(conn), dry_run=True
    )
    return {
        "portrait_index_keys_to_consolidate": int(report["keys_consolidated_count"]),
        "preferred_cached_portrait_changes": int(report["preferred_change_count"]),
        "changed_alias_count": int(report["changed_alias_count"]),
        "cache_identity_conflict_count": int(report["conflict_count"]),
        "image_files_deleted": 0,
    }


def _copy_verified_index_backup(source: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise Batch105Failure("index_backup_destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        return {"exists": False, "size": 0, "sha256": None}
    source_hash = _sha256_file(source)
    shutil.copyfile(source, destination)
    if _sha256_file(destination) != source_hash:
        destination.unlink(missing_ok=True)
        raise Batch105Failure("index_backup_verification_failed")
    return {
        "exists": True,
        "size": int(destination.stat().st_size),
        "sha256": source_hash,
    }


def _restore_index_backup(backup: Path, index_path: Path) -> None:
    if not backup.is_file():
        return
    temporary = index_path.with_name(index_path.name + ".batch10_5_restore.tmp")
    shutil.copyfile(backup, temporary)
    os.replace(temporary, index_path)


def _verified_database_backup(
    *, database: Path, backup: Path, baseline: Mapping[str, Any]
) -> dict[str, Any]:
    if backup.exists() or database.resolve() == backup.resolve():
        raise Batch105Failure("database_backup_destination_invalid")
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = acceptance.readonly(database, immutable=False)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
        destination.commit()
    except Exception:
        destination.close()
        source.close()
        backup.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    with contextlib.closing(acceptance.readonly(backup, immutable=False)) as copied:
        copied_guard = _database_guards(copied)
    if copied_guard != baseline["database"]:
        backup.unlink(missing_ok=True)
        raise Batch105Failure("database_backup_logical_mismatch")
    return {
        "verified": True,
        "schema_version": SCHEMA_VERSION,
        "size": int(backup.stat().st_size),
        "sha256": _sha256_file(backup),
    }


def _run_disposable_dry_run_unguarded(
    *, project_root: Path, database: Path, cache_root: Path
) -> dict[str, Any]:
    """Apply to a temporary clone and prove the source runtime stayed unchanged."""

    ensure_execution_policy()
    root = Path(project_root).expanduser().resolve()
    db_path = Path(database).expanduser().resolve()
    image_root = Path(cache_root).expanduser().resolve()
    source_before = capture_preservation_state(
        project_root=root, database=db_path, cache_root=image_root
    )
    with tempfile.TemporaryDirectory(prefix="MusicVault_Batch10_5_DryRun_") as temp:
        clone = Path(temp) / "music_vault.sqlite3"
        source = acceptance.readonly(db_path, immutable=False)
        destination = sqlite3.connect(clone)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        connection = sqlite3.connect(clone)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            proposal = analyze_database(connection)
            database_result = apply_database_transaction(connection)
            cache_result = analyze_cache_index(connection, image_root)
            clone_state = _database_guards(connection)
        finally:
            connection.close()
    source_after = capture_preservation_state(
        project_root=root, database=db_path, cache_root=image_root
    )
    if source_before != source_after:
        raise Batch105Failure("dry_run_changed_source_runtime")
    return {
        "report_format_version": REPORT_FORMAT_VERSION,
        "dry_run": True,
        "source_runtime_unchanged": True,
        "proposal": proposal,
        "database_result": database_result,
        "cache_result": cache_result,
        "clone_health": clone_state["health"],
        "database_counts_before": source_before["database"]["counts"],
        "database_counts_after": clone_state["counts"],
        "browser_outcomes_before": source_before["database"]["browser_outcomes"],
        "browser_outcomes_after": clone_state["browser_outcomes"],
        "terminal_state_counts_before": source_before["database"][
            "terminal_state_counts"
        ],
        "terminal_state_counts_after": clone_state["terminal_state_counts"],
        "temporary_clone_deleted": True,
        "credential_contents_read": False,
        "media_writes": 0,
        "tag_writes": 0,
    }


def _apply_live_repair_unguarded(
    *,
    project_root: Path,
    database: Path,
    cache_root: Path,
    acknowledgement: str,
) -> dict[str, Any]:
    """Perform the one explicitly acknowledged live repair and verification."""

    if acknowledgement != LIVE_ACKNOWLEDGEMENT:
        raise Batch105Failure("live_acknowledgement_missing")
    ensure_execution_policy()
    root = Path(project_root).expanduser().resolve()
    db_path = Path(database).expanduser().resolve()
    image_root = Path(cache_root).expanduser().resolve()
    baseline = capture_preservation_state(
        project_root=root, database=db_path, cache_root=image_root
    )
    _assert_health(baseline["database"]["health"])
    if baseline["database"]["marker_present"]:
        return {
            "report_format_version": REPORT_FORMAT_VERSION,
            "dry_run": False,
            "no_op": True,
            "marker_present": True,
            "backups_created": 0,
        }

    dry_run = _run_disposable_dry_run_unguarded(
        project_root=root, database=db_path, cache_root=image_root
    )
    stamp = _utc_stamp()
    backups = root / "data" / "backups"
    database_backup = backups / f"{DATABASE_BACKUP_PREFIX}{stamp}.sqlite3"
    index_backup = backups / f"{INDEX_BACKUP_PREFIX}{stamp}.json"
    database_backup_report = _verified_database_backup(
        database=db_path, backup=database_backup, baseline=baseline
    )
    index_backup_report = _copy_verified_index_backup(
        image_root / "index.json", index_backup
    )

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        database_result = apply_database_transaction(connection)
        groups = _artist_image_groups(connection)
    finally:
        connection.close()

    try:
        cache_result = ArtistImageCache(image_root).repair_index(
            groups,
            backup_path=index_backup if index_backup_report["exists"] else None,
            dry_run=False,
        )
    except Exception as exc:
        _restore_index_backup(index_backup, image_root / "index.json")
        raise Batch105Failure("cache_index_update_failed") from exc

    current = capture_preservation_state(
        project_root=root, database=db_path, cache_root=image_root
    )
    _assert_preserved(baseline, current)
    for key in ("media", "portraits", "credential_metadata", "runtime_metadata"):
        if baseline[key] != current[key]:
            raise Batch105Failure(f"live_preservation_failed_{key}")
    if not current["database"]["marker_present"]:
        raise Batch105Failure("repair_marker_missing_after_commit")
    if int(current["database"]["review_count"]) != 0:
        raise Batch105Failure("review_items_remain_after_commit")

    second = sqlite3.connect(db_path)
    second.row_factory = sqlite3.Row
    try:
        second_result = apply_database_transaction(second)
    finally:
        second.close()
    if second_result != {"marker_present": True, "no_op": True, "changes": 0}:
        raise Batch105Failure("second_repair_not_noop")

    return {
        "report_format_version": REPORT_FORMAT_VERSION,
        "dry_run": False,
        "no_op": False,
        "dry_run_passed": bool(dry_run["source_runtime_unchanged"]),
        "database_backup": {
            **database_backup_report,
            "name": database_backup.name,
        },
        "cache_index_backup": {
            **index_backup_report,
            "name": index_backup.name,
        },
        "database_result": database_result,
        "cache_result": {
            key: value
            for key, value in cache_result.items()
            if key not in {"backup_path"}
        },
        "schema_version": int(current["database"]["health"]["schema_version"]),
        "integrity_ok": bool(current["database"]["health"]["integrity_ok"]),
        "foreign_key_issue_count": int(
            current["database"]["health"]["foreign_key_issue_count"]
        ),
        "required_indexes_present": (
            int(current["database"]["health"]["missing_required_index_count"]) == 0
        ),
        "review_count": int(current["database"]["review_count"]),
        "database_counts_before": baseline["database"]["counts"],
        "database_counts_after": current["database"]["counts"],
        "browser_outcomes_before": baseline["database"]["browser_outcomes"],
        "browser_outcomes_after": current["database"]["browser_outcomes"],
        "terminal_state_counts_before": baseline["database"][
            "terminal_state_counts"
        ],
        "terminal_state_counts_after": current["database"]["terminal_state_counts"],
        "marker_present": bool(current["database"]["marker_present"]),
        "second_run_no_op": True,
        "media_unchanged": baseline["media"] == current["media"],
        "portrait_files_unchanged": baseline["portraits"] == current["portraits"],
        "credentials_unchanged": (
            baseline["credential_metadata"] == current["credential_metadata"]
        ),
        "credential_contents_read": False,
        "media_writes": 0,
        "tag_writes": 0,
    }


def ensure_execution_policy() -> None:
    acceptance.ensure_no_secret_mode()
    if os.environ.get(NO_NETWORK_ENVIRONMENT) != "1":
        raise Batch105Failure("no_network_environment_missing")


@contextmanager
def _offline_guard():
    """Deny outbound work and return measured aggregate-only evidence."""

    ensure_execution_policy()
    report_root = Path(
        tempfile.mkdtemp(prefix="MusicVault_Batch10_5_Network_")
    ).resolve()
    report_path = report_root / "network.json"
    previous = os.environ.get(acceptance_network.NETWORK_REPORT_ENVIRONMENT)
    os.environ[acceptance_network.NETWORK_REPORT_ENVIRONMENT] = str(report_path)
    guard = None
    evidence: dict[str, Any] = {}
    operation_error: BaseException | None = None
    proof_error: BaseException | None = None
    try:
        guard = acceptance_network.install_acceptance_network_guard()
        if guard is None:
            raise Batch105Failure("network_guard_not_installed")
        try:
            yield evidence
        except BaseException as exc:
            operation_error = exc
    finally:
        if guard is not None:
            try:
                guard.finalize()
                evidence.update(acceptance.verify_acceptance_network_report(report_path))
            except BaseException as exc:
                proof_error = exc
            guard.restore()
        if previous is None:
            os.environ.pop(acceptance_network.NETWORK_REPORT_ENVIRONMENT, None)
        else:
            os.environ[acceptance_network.NETWORK_REPORT_ENVIRONMENT] = previous
        shutil.rmtree(report_root, ignore_errors=True)
    if proof_error is not None:
        raise Batch105Failure("provider_or_network_access_observed") from proof_error
    if operation_error is not None:
        raise operation_error


def run_disposable_dry_run(
    *, project_root: Path, database: Path, cache_root: Path
) -> dict[str, Any]:
    with _offline_guard() as network:
        result = _run_disposable_dry_run_unguarded(
            project_root=project_root,
            database=database,
            cache_root=cache_root,
        )
    result["network_evidence"] = network
    result["provider_requests"] = int(network["attempt_count"])
    return result


def apply_live_repair(
    *,
    project_root: Path,
    database: Path,
    cache_root: Path,
    acknowledgement: str,
) -> dict[str, Any]:
    # Check the acknowledgement before installing any runtime mechanism or
    # inspecting the live project.
    if acknowledgement != LIVE_ACKNOWLEDGEMENT:
        raise Batch105Failure("live_acknowledgement_missing")
    with _offline_guard() as network:
        result = _apply_live_repair_unguarded(
            project_root=project_root,
            database=database,
            cache_root=cache_root,
            acknowledgement=acknowledgement,
        )
    result["network_evidence"] = network
    result["provider_requests"] = int(network["attempt_count"])
    return result


def ensure_music_vault_closed() -> None:
    """Fail closed when a Windows MusicVault process is running."""

    if os.name != "nt":
        return
    completed = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq MusicVault.exe", "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise Batch105Failure("process_check_failed")
    if "MusicVault.exe" in completed.stdout:
        raise Batch105Failure("music_vault_process_running")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run-clone", "apply-live"):
        item = subparsers.add_parser(command)
        item.add_argument("--project-root", type=Path, required=True)
        item.add_argument("--database", type=Path, required=True)
        item.add_argument("--cache-root", type=Path, required=True)
        if command == "apply-live":
            item.add_argument("--acknowledge-live-repair", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        ensure_music_vault_closed()
        if args.command == "dry-run-clone":
            result = run_disposable_dry_run(
                project_root=args.project_root,
                database=args.database,
                cache_root=args.cache_root,
            )
        else:
            result = apply_live_repair(
                project_root=args.project_root,
                database=args.database,
                cache_root=args.cache_root,
                acknowledgement=args.acknowledge_live_repair,
            )
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error_code": "batch10_5_acceptance_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Batch105Failure",
    "LIVE_ACKNOWLEDGEMENT",
    "REPAIR_MARKER",
    "SCHEMA_VERSION",
    "analyze_cache_index",
    "analyze_database",
    "apply_database_transaction",
    "apply_live_repair",
    "capture_preservation_state",
    "ensure_execution_policy",
    "ensure_music_vault_closed",
    "run_disposable_dry_run",
]
