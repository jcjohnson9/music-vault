from __future__ import annotations

"""Prove schema 6 to 7 on deterministic synthetic data outside the repo."""

import argparse
import contextlib
import gc
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.core import paths as runtime_paths  # noqa: E402
from music_vault.core.acceptance_network import AcceptanceNetworkGuard  # noqa: E402
from music_vault.core.app_status import write_app_status  # noqa: E402
from music_vault.core.db import MusicVaultDB  # noqa: E402
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore  # noqa: E402
from music_vault.metadata.service import MetadataService  # noqa: E402
from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402
from tools.dev import verify_batch10_3_live_migration as live_gate  # noqa: E402


TEMP_PREFIX = "MusicVault_Batch10_3_SourceMigrationProof_"


class ProofFailure(acceptance.AcceptanceFailure):
    """A stable, non-identifying proof failure."""


@contextlib.contextmanager
def _isolated_runtime(root: Path):
    previous_root = os.environ.get("MUSIC_VAULT_PROJECT_ROOT")
    previous_no_secrets = os.environ.get(acceptance.NO_SECRETS_ENVIRONMENT)
    previous_data = runtime_paths._configured_data_directory
    os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(root)
    os.environ[acceptance.NO_SECRETS_ENVIRONMENT] = "1"
    runtime_paths._configured_data_directory = None
    runtime_paths._resolved_project_root.cache_clear()
    try:
        if runtime_paths.project_root().resolve() != root.resolve():
            raise ProofFailure("temporary_runtime_root_not_resolved")
        yield
    finally:
        runtime_paths._configured_data_directory = previous_data
        if previous_root is None:
            os.environ.pop("MUSIC_VAULT_PROJECT_ROOT", None)
        else:
            os.environ["MUSIC_VAULT_PROJECT_ROOT"] = previous_root
        if previous_no_secrets is None:
            os.environ.pop(acceptance.NO_SECRETS_ENVIRONMENT, None)
        else:
            os.environ[acceptance.NO_SECRETS_ENVIRONMENT] = previous_no_secrets
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


def _mark_review(
    store: MetadataIntelligenceJobStore,
    job_id: str,
    evidence: Mapping[str, object],
) -> None:
    item = store.claim_next_item(job_id)
    if item is None:
        raise ProofFailure("synthetic_review_item_unavailable")
    store.mark_item(item.id, "review", **dict(evidence))


def _review_evidence(
    *,
    fallback: bool = False,
    conflict: bool = False,
    member_relationship: bool = False,
) -> dict[str, object]:
    hints = (
        {
            "title": "Fixture Source Title",
            "artist": "Fixture Source Artist",
            "pattern": "artist_dash_title",
        }
        if fallback
        else {}
    )
    reasons = {"version_type": ["version_identity_conflict"]} if conflict else {}
    discogs: dict[str, object] = (
        {} if fallback else {"title": "Fixture Title", "score": 96}
    )
    field_confidence: dict[str, object] = {}
    if member_relationship:
        discogs.update(
            {
                "provider_reference": "synthetic-relationship-evidence",
                "artist_relationships": [
                    {
                        "relationship_kind": "member_of",
                        "member": {"discogs_artist_id": "91001"},
                        "group": {"discogs_artist_id": "91002"},
                        "provider_reference": "synthetic-relationship-evidence",
                        "confidence": 99,
                    }
                ],
            }
        )
        field_confidence["artist_relationships"] = 99
    return {
        "parsed_hints": hints,
        "field_proposal": {
            "_current": {"title": "Fixture Title", "artist": "Fixture Ensemble"},
            "_discogs": discogs,
            "_musicbrainz": {},
            "_artwork": {"candidate_available": False},
            "_reasons": reasons,
            **(
                {
                    "_orientation": {
                        "schema_version": 1,
                        "selected": "left_is_artist",
                        "evaluated_count": 2,
                        "confidence": 35.0,
                        "reasons": [
                            "provisional_conventional_orientation",
                            "provider_adjudication_required",
                        ],
                        "provider_confirmed": False,
                        "requires_provider_adjudication": True,
                        "discogs_queries": 0,
                        "musicbrainz_queries": 0,
                        "evaluations": [],
                    }
                }
                if fallback
                else {}
            ),
        },
        "field_confidence": field_confidence,
        "provider_agreement": "none" if fallback else "discogs_only",
        "review_reason": (
            "version_conflict" if conflict else "youtube_exclusive" if fallback else "album_ambiguity"
        ),
    }


def _create_synthetic_schema6(database: Path, backup_dir: Path, runtime: Path) -> None:
    # The packaged application always supplies the legacy failure-history path
    # when it opens the database.  A previously initialized schema-6 library
    # therefore already has this one-time marker even when the legacy file does
    # not exist.  Seed it through the production API so the synthetic baseline
    # models that stable startup state and app_meta can remain fully protected
    # by the migration verifier.
    db = MusicVaultDB(
        database,
        backup_dir=backup_dir,
        legacy_failure_file=runtime / "data" / "youtube_failed_ids.txt",
    )
    fixtures = (
        ("Fixture Record", "Fixture Ensemble", "Fixture Ensemble", "2001-01-01"),
        ("Fixture Record (Deluxe Edition)", "fixture ensemble", "Fixture Ensemble", "2024-01-01"),
        ("Fixture Record Live", "Fixture Collective", "Fixture Ensemble", "2020-01-01"),
        ("Fixture Film Original Motion Picture Soundtrack", "Fixture Composer", "Various Artists", "2022-01-01"),
        ("Fixture Film Original Motion Picture Score", "Fixture Perspective", "Fixture Composer", "2022-01-01"),
        (None, "Fixture Soloist", None, None),
    )
    track_ids: list[int] = []
    for index, (album, artist, album_artist, release_date) in enumerate(fixtures):
        track_ids.append(
            db.upsert_track(
                runtime / "missing-media" / f"track-{index}.fixture",
                title=f"Fixture Track {index}",
                artist=artist,
                album=album,
                album_artist=album_artist,
                release_date=release_date,
                cover_path=(
                    str(runtime / "missing-covers" / f"cover-{index}.fixture")
                    if index < 3
                    else None
                ),
                source_kind="local",
            )
        )

    # Create one safe case-only duplicate on a different track.  The display
    # values are synthetic and are never included in a report.
    duplicate_artist = int(
        db.conn.execute(
            """
            INSERT INTO artists(
                display_name,normalized_name,sort_name,entity_type,
                created_at,updated_at
            ) VALUES('fixture ensemble','fixture ensemble','fixture ensemble',
                     'group',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """
        ).lastrowid
    )
    db.conn.execute(
        "UPDATE track_artist_credits SET artist_id=? WHERE track_id=? AND role='primary'",
        (duplicate_artist, track_ids[1]),
    )

    # Give two synthetic canonical identities stable provider IDs, then add
    # structured featured/collaborator roles.  A saved accepted provider
    # relationship below supplies the only member-of evidence; punctuation or
    # co-crediting alone never fabricates group membership.
    target_artist_id = int(
        db.conn.execute(
            "SELECT id FROM artists WHERE normalized_name='fixture perspective' ORDER BY id LIMIT 1"
        ).fetchone()[0]
    )
    group_artist_id = int(
        db.conn.execute(
            "SELECT id FROM artists WHERE normalized_name='fixture collective' ORDER BY id LIMIT 1"
        ).fetchone()[0]
    )
    db.conn.execute(
        "UPDATE artists SET entity_type='person',discogs_artist_id='91001' WHERE id=?",
        (target_artist_id,),
    )
    db.conn.execute(
        "UPDATE artists SET entity_type='group',discogs_artist_id='91002' WHERE id=?",
        (group_artist_id,),
    )
    for track_id, role, join_phrase in (
        (track_ids[0], "featured", " feat. "),
        (track_ids[1], "collaborator", " x "),
    ):
        db.conn.execute(
            """
            INSERT INTO track_artist_credits(
                track_id,artist_id,role,credit_order,join_phrase,provenance,
                provider_reference,confidence,is_manual,is_locked,created_at,updated_at
            ) VALUES(?,?,?,1,?,'discogs','synthetic-credit-evidence',99,0,0,
                     CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """,
            (track_id, target_artist_id, role, join_phrase),
        )

    # Store a conservative, fully local malformed version identity.  Schema 7
    # must repoint only its one primary credit to the canonical soloist and
    # retain the location text as version metadata plus an audit alias.
    soloist_artist_id = int(
        db.conn.execute(
            "SELECT id FROM artists WHERE normalized_name='fixture soloist' ORDER BY id LIMIT 1"
        ).fetchone()[0]
    )
    db.conn.execute(
        "UPDATE artists SET entity_type='person' WHERE id=?",
        (soloist_artist_id,),
    )
    malformed_artist_id = int(
        db.conn.execute(
            """
            INSERT INTO artists(
                display_name,normalized_name,sort_name,entity_type,created_at,updated_at
            ) VALUES('Fixture Soloist Live at North Hall',
                     'fixture soloist live at north hall',
                     'fixture soloist live at north hall','person',
                     CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """
        ).lastrowid
    )
    MetadataService(db).record_source_observations(
        track_ids[5],
        provider="confirmed_provider",
        values={
            "artist": "Fixture Soloist",
            "version_type": "live",
            "version_label": "Live at North Hall",
        },
        confidence=100,
    )
    db.conn.execute(
        "UPDATE track_artist_credits SET artist_id=? WHERE track_id=? AND role='primary'",
        (malformed_artist_id, track_ids[5]),
    )
    db.conn.execute(
        """
        INSERT INTO track_release_context(
            track_id,discogs_release_id,discogs_master_id,release_title,
            provider_reference,confidence,updated_at
        ) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)
        """,
        (track_ids[0], "fixture-release", "fixture-master", "Fixture Record", "fixture", 99),
    )

    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job(track_ids[:3])
    _mark_review(store, job_id, _review_evidence(member_relationship=True))
    _mark_review(store, job_id, _review_evidence(fallback=True))
    _mark_review(store, job_id, _review_evidence(conflict=True))
    db.conn.commit()

    # Rebuild the item table with the exact schema-6 state constraint and drop
    # every schema-7-only table.  This mirrors the migration boundary without
    # depending on a personal database.
    current_sql = str(
        db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='metadata_intelligence_items'"
        ).fetchone()[0]
    )
    legacy_sql = current_sql.replace(
        "CREATE TABLE metadata_intelligence_items",
        "CREATE TABLE metadata_intelligence_items_schema6",
        1,
    ).replace("'applied_with_gaps', 'source_fallback', ", "")
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute(legacy_sql)
    item_columns = [
        str(row[1])
        for row in db.conn.execute("PRAGMA table_info(metadata_intelligence_items)")
    ]
    column_sql = ",".join(acceptance.quote_identifier(name) for name in item_columns)
    db.conn.execute(
        f"INSERT INTO metadata_intelligence_items_schema6 ({column_sql}) "
        f"SELECT {column_sql} FROM metadata_intelligence_items"
    )
    db.conn.execute("DROP TABLE metadata_intelligence_items")
    db.conn.execute(
        "ALTER TABLE metadata_intelligence_items_schema6 RENAME TO metadata_intelligence_items"
    )

    # The current schema creator includes the two Batch 10.3 release-family
    # columns even when this synthetic fixture is subsequently marked as
    # schema 6.  Rebuild the table with its genuine schema-6 definition so the
    # proof exercises ALTER TABLE additions from the real migration boundary.
    legacy_release_columns = (
        "track_id",
        "discogs_release_id",
        "discogs_master_id",
        "release_title",
        "release_country",
        "release_format",
        "catalog_number",
        "label_name",
        "release_date",
        "original_release_date",
        "provider_reference",
        "confidence",
        "updated_at",
    )
    db.conn.execute(
        """
        CREATE TABLE track_release_context_schema6 (
            track_id INTEGER PRIMARY KEY,
            discogs_release_id TEXT,
            discogs_master_id TEXT,
            release_title TEXT,
            release_country TEXT,
            release_format TEXT,
            catalog_number TEXT,
            label_name TEXT,
            release_date TEXT,
            original_release_date TEXT,
            provider_reference TEXT,
            confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
            updated_at TEXT NOT NULL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
        """
    )
    legacy_release_sql = ",".join(
        acceptance.quote_identifier(name) for name in legacy_release_columns
    )
    db.conn.execute(
        f"INSERT INTO track_release_context_schema6 ({legacy_release_sql}) "
        f"SELECT {legacy_release_sql} FROM track_release_context"
    )
    db.conn.execute("DROP TABLE track_release_context")
    db.conn.execute(
        "ALTER TABLE track_release_context_schema6 RENAME TO track_release_context"
    )
    db.conn.execute(
        "CREATE INDEX idx_release_context_discogs_release "
        "ON track_release_context(discogs_release_id, track_id)"
    )
    db.conn.execute(
        "CREATE INDEX idx_release_context_discogs_master "
        "ON track_release_context(discogs_master_id, track_id)"
    )
    for column in ("applied_with_gaps_items", "source_fallback_items"):
        if column in {
            str(row[1])
            for row in db.conn.execute("PRAGMA table_info(metadata_intelligence_jobs)")
        }:
            db.conn.execute(f"ALTER TABLE metadata_intelligence_jobs DROP COLUMN {column}")
    for table in (
        "artist_relationships",
        "artist_aliases",
        "track_album_memberships",
        "canonical_albums",
    ):
        db.conn.execute(f"DROP TABLE IF EXISTS {acceptance.quote_identifier(table)}")
    db.conn.execute(f"PRAGMA user_version={acceptance.PRE_SCHEMA_VERSION}")
    db.conn.commit()
    db.close()


def run_source_migration_proof(*, temporary_parent: Path | None = None) -> dict[str, Any]:
    parent = Path(temporary_parent).resolve() if temporary_parent is not None else None
    temporary_root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent)).resolve()
    if acceptance.is_within(temporary_root, PROJECT_ROOT):
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise ProofFailure("temporary_root_inside_repository")
    data = temporary_root / "data"
    backups = data / "backups"
    database = data / "music_vault.sqlite3"
    network_report = temporary_root / "batch10_3-network-report.json"
    (temporary_root / "music_vault").mkdir()
    (temporary_root / "run.py").write_text(
        "# disposable Batch 10.3 migration proof marker\n", encoding="utf-8"
    )
    data.mkdir(parents=True)
    try:
        with _isolated_runtime(temporary_root):
            _create_synthetic_schema6(database, backups, temporary_root)
            baseline = acceptance.capture_database_baseline(
                project_root=temporary_root,
                data_dir=data,
                database=database,
                expected_schema=acceptance.PRE_SCHEMA_VERSION,
            )
            dry_run = live_gate.clone_dry_run(
                project_root=temporary_root,
                data_dir=data,
                database=database,
                baseline=baseline,
                temporary_parent=parent,
            )
            explicit_backup_path = backups / "batch10_3_source_schema6_rollback.sqlite3"
            acceptance.create_verified_sqlite_backup(
                database=database,
                backup=explicit_backup_path,
                baseline=baseline,
            )
            with _offline_guard() as attempts:
                guard = AcceptanceNetworkGuard(network_report).install()
                try:
                    migrated = MusicVaultDB(database, backup_dir=backups)
                    write_app_status(migrated, {"onboarding_completed": True})
                    migrated.close()
                    first_state = acceptance.capture_database_baseline(
                        project_root=temporary_root,
                        data_dir=data,
                        database=database,
                        expected_schema=acceptance.POST_SCHEMA_VERSION,
                    )
                    reopened = MusicVaultDB(database, backup_dir=backups)
                    reopened.close()
                    second_state = acceptance.capture_database_baseline(
                        project_root=temporary_root,
                        data_dir=data,
                        database=database,
                        expected_schema=acceptance.POST_SCHEMA_VERSION,
                    )
                finally:
                    guard.finalize()
                    guard.restore()
            verification = live_gate.verify_migration(
                baseline=baseline,
                dry_run=dry_run,
                project_root=temporary_root,
                data_dir=data,
                database=database,
                backup_path=explicit_backup_path,
                network_report=network_report,
            )
        idempotent = first_state["database"]["tables"] == second_state["database"]["tables"]
        backup_count = len(list(backups.glob("music_vault_pre_schema_v7_*.sqlite3")))
        checks = {
            "isolated_temp_root": True,
            "schema_migrated_6_to_7": verification["checks"]["schema_migrated_6_to_7"],
            "preservation_gate_passed": verification["ok"] is True,
            "migration_idempotent": idempotent,
            "exactly_one_automatic_backup": backup_count == 1,
            "network_attempt_count_zero": int(attempts["count"]) == 0,
            "credentials_absent": not any(
                (data / name).exists() for name in acceptance.CREDENTIAL_FILE_NAMES.values()
            ),
            "media_files_absent": first_state["media"]["existing_media_count"] == 0,
            "dist_data_absent": not (temporary_root / "dist" / "MusicVault" / "data").exists(),
        }
        result = {
            "ok": all(checks.values()),
            "checks": checks,
            "counts": {
                **verification["counts"],
                "automatic_schema7_backup_count": backup_count,
                "network_attempt_count": int(attempts["count"]),
            },
            "raw_library_values_emitted": False,
            "credential_contents_read": False,
            "media_contents_read": False,
            "temporary_root_deleted": True,
        }
    finally:
        # SQLite constructors can fail during migration before their wrapper is
        # assigned.  Collect any such unreachable connection before deleting
        # the disposable root so the original fail-closed error is preserved.
        gc.collect()
        shutil.rmtree(temporary_root, ignore_errors=False)
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporary-parent", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ[acceptance.NO_SECRETS_ENVIRONMENT] = "1"
    try:
        result = run_source_migration_proof(temporary_parent=args.temporary_parent)
        if args.output is not None:
            acceptance.atomic_write_json(args.output, result)
    except (acceptance.AcceptanceFailure, OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        print(json.dumps({"ok": False, "error_code": "batch10_3_source_migration_proof_failed"}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
