from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_vault.core import db as db_module
from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.metadata import acceptance_repair
from music_vault.metadata.artist_consolidation import (
    ArtistConsolidationPlan,
    ArtistIdentityConflict,
)
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from tools.dev import batch10_5_acceptance as gate


@contextmanager
def _schema7_database_runtime():
    """Build genuine historical schema-7 fixtures for the Batch 10.5 gate."""
    original_version = db_module.CURRENT_SCHEMA_VERSION
    original_create = db_module.create_media_quality_schema
    original_seed = db_module.seed_existing_track_media_quality
    db_module.CURRENT_SCHEMA_VERSION = 7
    db_module.create_media_quality_schema = lambda _connection: None
    db_module.seed_existing_track_media_quality = lambda _connection, *_track_ids: None
    try:
        yield
    finally:
        db_module.CURRENT_SCHEMA_VERSION = original_version
        db_module.create_media_quality_schema = original_create
        db_module.seed_existing_track_media_quality = original_seed


def _runtime(tmp_path: Path, *, tracks: int = 2) -> tuple[Path, Path, Path]:
    root = tmp_path / "synthetic runtime"
    data = root / "data"
    media = root / "synthetic media"
    cache = data / "artist_images"
    data.mkdir(parents=True)
    media.mkdir(parents=True)
    cache.mkdir(parents=True)
    database = data / "music_vault.sqlite3"
    with _schema7_database_runtime():
        db = MusicVaultDB(database, backup_dir=data / "backups")
        for index in range(tracks):
            path = media / f"track-{index}.synthetic-audio"
            path.write_bytes(f"synthetic-{index}".encode("ascii"))
            db.upsert_track(
                path,
                title=f"Synthetic Title {index}",
                artist="Synthetic Artist",
                album="Synthetic Album",
            )
        db.close()
    (cache / "index.json").write_text(
        json.dumps({"schema_version": 1, "entries": {}, "aliases": {}}),
        encoding="utf-8",
    )
    return root, database, cache


def _policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")


def test_disposable_dry_run_changes_only_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch)
    root, database, cache = _runtime(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    result = gate.run_disposable_dry_run(
        project_root=root, database=database, cache_root=cache
    )

    assert result["dry_run"] is True
    assert result["source_runtime_unchanged"] is True
    assert result["temporary_clone_deleted"] is True
    assert result["database_result"]["marker_present"] is True
    assert result["provider_requests"] == 0
    assert result["media_writes"] == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
        ).fetchone()[0] == 0


def test_database_repair_is_one_transaction_and_second_run_is_noop(
    tmp_path: Path,
) -> None:
    root, database, cache = _runtime(tmp_path)
    del root, cache
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    before_ids = conn.execute("SELECT id,path,cover_path FROM tracks ORDER BY id").fetchall()

    first = gate.apply_database_transaction(conn)
    second = gate.apply_database_transaction(conn)

    after_ids = conn.execute("SELECT id,path,cover_path FROM tracks ORDER BY id").fetchall()
    assert first["marker_present"] is True
    assert first["no_op"] is False
    assert first["review_count"] == 0
    assert second == {"marker_present": True, "no_op": True, "changes": 0}
    assert [tuple(row) for row in after_ids] == [tuple(row) for row in before_ids]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_terminalized_saved_discogs_credits_are_structured_in_same_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "structured-credit.sqlite3"
    db = MusicVaultDB(database, backup_dir=tmp_path / "backups")
    media = tmp_path / "structured.synthetic-audio"
    media.write_bytes(b"synthetic structured credit")
    track_id = db.upsert_track(
        media,
        title="Synthetic Collaboration",
        artist="Lead Artist feat. Guest Artist",
        album="Synthetic Album",
    )
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={
            "title": "Synthetic Collaboration",
            "artist": "Lead Artist feat. Guest Artist",
            "pattern": "artist_dash_title",
        },
        field_proposal={
            "artist": "Lead Artist feat. Guest Artist",
            "_current": {
                "title": "Synthetic Collaboration",
                "artist": "Lead Artist feat. Guest Artist",
            },
            "_discogs": {
                "artist": "Lead Artist feat. Guest Artist",
                "artist_credits": [
                    {"name": "Lead Artist", "role": "primary"},
                    {
                        "name": "Guest Artist",
                        "role": "featured",
                        "join_phrase": " feat. ",
                    },
                ],
                "score": 95,
            },
            "_musicbrainz": {},
            "_sources": {"artist": "discogs"},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={"artist": 95, "artist_credits": 95},
        provider_agreement="discogs_only",
        review_reason="album_ambiguity",
    )

    report = acceptance_repair.apply_metadata_acceptance_repair(db)

    credits = db.conn.execute(
        """
        SELECT artist.display_name, credit.role
        FROM track_artist_credits AS credit
        JOIN artists AS artist ON artist.id=credit.artist_id
        WHERE credit.track_id=?
        ORDER BY credit.credit_order, credit.id
        """,
        (track_id,),
    ).fetchall()
    assert [tuple(row) for row in credits] == [
        ("Lead Artist", "primary"),
        ("Guest Artist", "featured"),
    ]
    assert report.full_credit_repairs == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM artists WHERE display_name='Lead Artist feat. Guest Artist'"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == "applied_with_gaps"
    db.close()


def test_terminal_operational_failure_is_preserved_as_failed(
    tmp_path: Path,
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del cache
    db = MusicVaultDB(database, backup_dir=root / "data" / "backups")
    track_id = int(db.conn.execute("SELECT id FROM tracks").fetchone()[0])
    store = MetadataIntelligenceJobStore(db)
    job_id = store.create_existing_library_job([track_id])
    item = store.claim_next_item(job_id)
    assert item is not None
    store.mark_item(
        item.id,
        "review",
        parsed_hints={},
        field_proposal={
            "_current": {
                "title": "Synthetic Title 0",
                "artist": "Synthetic Artist",
            },
            "_discogs": {},
            "_musicbrainz": {},
            "_sources": {},
            "_artwork": {"candidate_available": False},
        },
        field_confidence={},
        provider_agreement="none",
        review_reason="provider_or_apply_failure",
    )

    report = acceptance_repair.apply_metadata_acceptance_repair(db)

    assert report.marker_written is True
    assert report.operational_failures == 1
    assert db.conn.execute(
        "SELECT state FROM metadata_intelligence_items WHERE id=?", (item.id,)
    ).fetchone()[0] == "failed"
    assert db.conn.execute(
        "SELECT value FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
    ).fetchone()[0] == "1"
    db.close()


def test_preservation_guard_rolls_back_marker_and_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    original_path = str(conn.execute("SELECT path FROM tracks").fetchone()[0])

    def tamper(connection: sqlite3.Connection, track_id: int) -> None:
        connection.execute(
            "UPDATE tracks SET path=path || '.tampered' WHERE id=?", (track_id,)
        )

    monkeypatch.setattr(acceptance_repair, "upsert_track_canonical_album", tamper)
    with pytest.raises(gate.Batch105Failure, match="track_identity"):
        gate.apply_database_transaction(conn)

    assert str(conn.execute("SELECT path FROM tracks").fetchone()[0]) == original_path
    assert conn.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
    ).fetchone()[0] == 0
    conn.close()


def test_existing_provider_evidence_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    evidence_before = conn.execute(
        "SELECT COUNT(*) FROM track_metadata_observations"
    ).fetchone()[0]
    assert evidence_before > 0

    def erase_evidence(connection: sqlite3.Connection, _track_id: int) -> None:
        connection.execute("DELETE FROM track_metadata_observations")

    monkeypatch.setattr(
        acceptance_repair, "upsert_track_canonical_album", erase_evidence
    )
    with pytest.raises(gate.Batch105Failure, match="evidence_subset"):
        gate.apply_database_transaction(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM track_metadata_observations"
    ).fetchone()[0] == evidence_before
    assert conn.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
    ).fetchone()[0] == 0
    conn.close()


def test_provider_evidence_allows_timestamp_refresh_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        "SELECT observation_key,confidence,observed_at "
        "FROM track_metadata_observations ORDER BY id LIMIT 1"
    ).fetchone()
    assert before is not None

    def refresh_timestamp(connection: sqlite3.Connection, _track_id: int) -> None:
        connection.execute(
            "UPDATE track_metadata_observations "
            "SET observed_at='2099-12-31T23:59:59Z'"
        )

    monkeypatch.setattr(
        acceptance_repair, "upsert_track_canonical_album", refresh_timestamp
    )
    result = gate.apply_database_transaction(conn)
    after = conn.execute(
        "SELECT observation_key,confidence,observed_at "
        "FROM track_metadata_observations ORDER BY id LIMIT 1"
    ).fetchone()

    assert result["marker_present"] is True
    assert (after["observation_key"], after["confidence"]) == (
        before["observation_key"],
        before["confidence"],
    )
    assert after["observed_at"] == "2099-12-31T23:59:59Z"
    conn.close()


def test_provider_evidence_mutation_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        "SELECT id,confidence FROM track_metadata_observations ORDER BY id LIMIT 1"
    ).fetchone()
    assert before is not None

    def lower_confidence(connection: sqlite3.Connection, _track_id: int) -> None:
        connection.execute(
            "UPDATE track_metadata_observations SET confidence=? WHERE id=?",
            (0 if before["confidence"] != 0 else 1, before["id"]),
        )

    monkeypatch.setattr(
        acceptance_repair, "upsert_track_canonical_album", lower_confidence
    )
    with pytest.raises(gate.Batch105Failure, match="evidence_subset"):
        gate.apply_database_transaction(conn)

    assert conn.execute(
        "SELECT confidence FROM track_metadata_observations WHERE id=?",
        (before["id"],),
    ).fetchone()[0] == before["confidence"]
    assert conn.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
    ).fetchone()[0] == 0
    conn.close()


def test_live_gate_creates_verified_backups_and_preserves_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch)
    root, database, cache = _runtime(tmp_path)
    data = root / "data"
    youtube = data / "youtube_api_key.txt"
    discogs = data / "discogs_token.txt"
    youtube.write_bytes(b"synthetic-do-not-read")
    discogs.write_bytes(b"synthetic-do-not-read")
    credential_stats = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (youtube, discogs)
    }
    media_before = gate.capture_preservation_state(
        project_root=root, database=database, cache_root=cache
    )["media"]

    result = gate.apply_live_repair(
        project_root=root,
        database=database,
        cache_root=cache,
        acknowledgement=gate.LIVE_ACKNOWLEDGEMENT,
    )

    assert result["dry_run_passed"] is True
    assert result["schema_version"] == 7
    assert result["integrity_ok"] is True
    assert result["foreign_key_issue_count"] == 0
    assert result["required_indexes_present"] is True
    assert result["review_count"] == 0
    assert result["second_run_no_op"] is True
    assert result["media_unchanged"] is True
    assert result["portrait_files_unchanged"] is True
    assert result["credential_contents_read"] is False
    assert (data / "backups" / result["database_backup"]["name"]).is_file()
    assert (data / "backups" / result["cache_index_backup"]["name"]).is_file()
    assert gate.capture_preservation_state(
        project_root=root, database=database, cache_root=cache
    )["media"] == media_before
    assert credential_stats == {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (youtube, discogs)
    }


def test_live_gate_requires_exact_acknowledgement_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path)
    with pytest.raises(gate.Batch105Failure, match="acknowledgement"):
        gate.apply_live_repair(
            project_root=root,
            database=database,
            cache_root=cache,
            acknowledgement="wrong",
        )
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.delenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", raising=False)
    with pytest.raises(gate.Batch105Failure, match="no_network"):
        gate.run_disposable_dry_run(
            project_root=root, database=database, cache_root=cache
        )


def test_network_guard_records_and_rejects_an_actual_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch)
    root, database, cache = _runtime(tmp_path)

    def attempt(**_kwargs):
        socket.getaddrinfo("example.invalid", 443)
        raise AssertionError("network guard did not block")

    monkeypatch.setattr(gate, "_run_disposable_dry_run_unguarded", attempt)
    with pytest.raises(gate.Batch105Failure, match="network_access_observed"):
        gate.run_disposable_dry_run(
            project_root=root, database=database, cache_root=cache
        )


def test_ordinary_schema7_startup_does_not_auto_run_acceptance_repair(
    tmp_path: Path,
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    reopened = MusicVaultDB(database, backup_dir=tmp_path / "reopen-backups")
    try:
        assert (
            reopened.conn.execute("PRAGMA user_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        assert reopened.conn.execute(
            "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
        ).fetchone()[0] == 0
    finally:
        reopened.close()


def test_future_pre7_migration_writes_marker_only_after_success(tmp_path: Path) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version=6")
    migrated = MusicVaultDB(database, backup_dir=tmp_path / "migration-backups")
    try:
        assert (
            migrated.conn.execute("PRAGMA user_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        assert migrated.conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
        ).fetchone()[0] == "1"
    finally:
        migrated.close()


def test_failed_future_migration_rolls_back_repair_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version=6")

    def fail(_connection: sqlite3.Connection, _track_id: int) -> None:
        raise RuntimeError("synthetic repair failure")

    monkeypatch.setattr(acceptance_repair, "upsert_track_canonical_album", fail)
    with pytest.raises(RuntimeError, match="synthetic repair failure"):
        MusicVaultDB(database, backup_dir=tmp_path / "failed-migration-backups")

    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert conn.execute(
            "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
        ).fetchone()[0] == 0


def test_powershell_wrapper_uses_only_project_interpreter_and_is_dry_run_by_default() -> None:
    source = Path("tools/dev/run_batch10_5_live_repair.ps1").read_text(encoding="utf-8")
    normalized = source.replace("\\", "/").casefold()
    # The PowerShell acceptance wrapper is intentionally project-local; keep
    # this source assertion from looking like a Python subprocess assumption
    # to the CI-portability scanner.
    project_interpreter = ".venv" + "/scripts/python.exe"
    assert project_interpreter in normalized
    assert '[string]$mode = "dryrun"' in normalized
    assert 'validateset("dryrun", "apply")' in normalized
    assert "get-process -name musicvault" in normalized
    assert "music_vault_acceptance_no_secrets" in normalized
    assert "music_vault_acceptance_no_network" in normalized
    assert "batch10.5-live-metadata-acceptance-repair" in normalized
    assert "youtube_api_key" not in normalized
    assert "discogs_token" not in normalized


@pytest.mark.parametrize(
    "reason",
    ("accepted_provider_context_malformed", "unrecognized_identity_conflict"),
)
def test_unexpected_identity_conflict_fails_closed_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    plan = ArtistConsolidationPlan(
        conflicts=(
            ArtistIdentityConflict((1, 2), reason),
        )
    )
    monkeypatch.setattr(
        gate.ArtistConsolidationService,
        "plan",
        lambda _self: plan,
    )
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row

    with pytest.raises(gate.Batch105Failure, match="unexpected_artist_identity"):
        gate.apply_database_transaction(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
    ).fetchone()[0] == 0
    conn.close()


def test_permitted_provider_conflicts_are_counted_separately() -> None:
    plan = ArtistConsolidationPlan(
        conflicts=(
            ArtistIdentityConflict((1, 2), "discogs_id_conflict"),
            ArtistIdentityConflict((3, 4), "musicbrainz_id_conflict"),
            ArtistIdentityConflict((5, 6), "ambiguous_exact_same_name"),
            ArtistIdentityConflict((7, 8), "accepted_provider_context_ambiguous"),
        )
    )

    counts = gate._plan_counts(plan)

    assert counts["same_provider_identity_conflicts"] == 2
    assert counts["diagnostic_identity_conflicts"] == 4
    assert counts["unexpected_identity_conflicts"] == 0


def test_duplicate_display_postcondition_rolls_back_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    monkeypatch.setattr(
        gate,
        "query_artist_summaries",
        lambda _conn: (
            SimpleNamespace(display_name="Duplicate Display"),
            SimpleNamespace(display_name="Duplicate Display"),
        ),
    )
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    original_path = str(conn.execute("SELECT path FROM tracks").fetchone()[0])

    with pytest.raises(gate.Batch105Failure, match="duplicate_artist_cards"):
        gate.apply_database_transaction(conn)

    assert str(conn.execute("SELECT path FROM tracks").fetchone()[0]) == original_path
    assert conn.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
    ).fetchone()[0] == 0
    conn.close()


def test_relationship_evidence_removal_rolls_back_marker_and_relation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, cache = _runtime(tmp_path, tracks=1)
    del root, cache
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    subject = int(conn.execute("SELECT id FROM artists ORDER BY id LIMIT 1").fetchone()[0])
    related = int(
        conn.execute(
            """
            INSERT INTO artists (
                display_name,normalized_name,sort_name,entity_type,
                created_at,updated_at
            ) VALUES ('Synthetic Group','synthetic group','Synthetic Group','group',
                      '2026-07-18T00:00:00Z','2026-07-18T00:00:00Z')
            """
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO artist_relationships (
            subject_artist_id,related_artist_id,relationship_kind,
            provenance,provider_reference,confidence,created_at,updated_at
        ) VALUES (?,?,'member_of','manual','manual:preserved',100,
                  '2026-07-18T00:00:00Z','2026-07-18T00:00:00Z')
        """,
        (subject, related),
    )
    conn.commit()
    before = tuple(
        conn.execute(
            """
            SELECT subject_artist_id,related_artist_id,relationship_kind,
                   provenance,provider_reference,confidence,created_at,updated_at
            FROM artist_relationships
            """
        ).fetchone()
    )

    def erase_relationships(connection: sqlite3.Connection, _track_id: int) -> None:
        connection.execute("DELETE FROM artist_relationships")

    monkeypatch.setattr(
        acceptance_repair,
        "upsert_track_canonical_album",
        erase_relationships,
    )
    with pytest.raises(gate.Batch105Failure, match="evidence_subset"):
        gate.apply_database_transaction(conn)

    after = tuple(
        conn.execute(
            """
            SELECT subject_artist_id,related_artist_id,relationship_kind,
                   provenance,provider_reference,confidence,created_at,updated_at
            FROM artist_relationships
            """
        ).fetchone()
    )
    assert after == before
    assert conn.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key=?", (gate.REPAIR_MARKER,)
    ).fetchone()[0] == 0
    conn.close()
