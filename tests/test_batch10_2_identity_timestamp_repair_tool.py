from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from tools.dev import repair_batch10_2_identity_timestamps as repair_tool


PRIVATE_EXTERNAL_IDS = ("PRIVATE_VIDEO_ALPHA", "PRIVATE_VIDEO_BETA")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_database(
    path: Path,
    *,
    schema_version: int,
    updated_timestamps: tuple[str, str],
    second_track_id: int = 2,
    second_first_seen: str = "2025-01-02T00:00:00Z",
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            );
            CREATE TABLE source_track_identities (
                source_kind TEXT NOT NULL,
                external_track_id TEXT NOT NULL,
                track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_kind, external_track_id)
            );
            CREATE INDEX idx_source_track_identities_track
                ON source_track_identities(track_id);
            CREATE TABLE preservation_probe (
                id INTEGER PRIMARY KEY,
                payload BLOB,
                nullable_value TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO tracks(id, title) VALUES (?, ?)",
            ((1, "PRIVATE_TITLE_ONE"), (2, "PRIVATE_TITLE_TWO"), (3, "PRIVATE_TITLE_THREE")),
        )
        connection.executemany(
            """
            INSERT INTO source_track_identities(
                source_kind, external_track_id, track_id, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                (
                    "youtube",
                    PRIVATE_EXTERNAL_IDS[0],
                    1,
                    "2025-01-01T00:00:00Z",
                    updated_timestamps[0],
                ),
                (
                    "youtube",
                    PRIVATE_EXTERNAL_IDS[1],
                    second_track_id,
                    second_first_seen,
                    updated_timestamps[1],
                ),
            ),
        )
        connection.execute(
            "INSERT INTO preservation_probe(id, payload, nullable_value) VALUES (1, ?, NULL)",
            (b"private-binary-probe",),
        )
        connection.execute(f"PRAGMA user_version={schema_version}")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def timestamp_databases(tmp_path: Path) -> tuple[Path, Path, str]:
    reference = tmp_path / "schema5-reference.sqlite3"
    target = tmp_path / "schema6-target.sqlite3"
    _create_database(
        reference,
        schema_version=5,
        updated_timestamps=(
            "2025-01-01T00:00:00Z",
            "2025-01-02T00:00:00Z",
        ),
    )
    _create_database(
        target,
        schema_version=6,
        updated_timestamps=(
            "2026-07-16T00:00:00Z",
            "2026-07-16T00:00:00Z",
        ),
    )
    return target, reference, _sha256(reference)


def _identity_rows(path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return list(
            connection.execute(
                """
                SELECT source_kind, external_track_id, track_id,
                       first_seen_at, updated_at
                FROM source_track_identities
                ORDER BY source_kind, external_track_id
                """
            )
        )
    finally:
        connection.close()


def test_compare_is_aggregate_only_and_requires_only_timestamp_differences(
    timestamp_databases: tuple[Path, Path, str],
) -> None:
    target, reference, reference_hash = timestamp_databases
    result = repair_tool.compare_identity_timestamps(
        target_database=target,
        reference_backup=reference,
        expected_reference_sha256=reference_hash,
        expected_identity_count=2,
        expected_repair_count=2,
    )

    assert result["ok"] is True
    assert result["identity_comparison"] == {
        "target_identity_count": 2,
        "reference_identity_count": 2,
        "rows_compared": 2,
        "timestamp_repair_count": 2,
        "already_matching_count": 0,
        "mapping_conflict_count": 0,
        "first_seen_mismatch_count": 0,
        "other_column_mismatch_count": 0,
        "missing_count": 0,
        "extra_count": 0,
        "null_or_empty_key_count": 0,
        "duplicate_key_count": 0,
        "only_updated_at_differs": True,
    }
    encoded = json.dumps(result, sort_keys=True)
    assert result["raw_identity_values_emitted"] is False
    assert all(private not in encoded for private in PRIVATE_EXTERNAL_IDS)
    assert "PRIVATE_TITLE" not in encoded


@pytest.mark.parametrize(
    ("change", "error_code"),
    (
        ("mapping", "identity_relationship_mismatch"),
        ("first_seen", "identity_relationship_mismatch"),
        ("missing", "identity_relationship_mismatch"),
    ),
)
def test_compare_fails_closed_on_non_timestamp_identity_differences(
    tmp_path: Path,
    change: str,
    error_code: str,
) -> None:
    reference = tmp_path / "reference.sqlite3"
    target = tmp_path / "target.sqlite3"
    _create_database(
        reference,
        schema_version=5,
        updated_timestamps=("old-one", "old-two"),
    )
    _create_database(
        target,
        schema_version=6,
        updated_timestamps=("new-one", "new-two"),
        second_track_id=3 if change == "mapping" else 2,
        second_first_seen="different" if change == "first_seen" else "2025-01-02T00:00:00Z",
    )
    if change == "missing":
        with sqlite3.connect(target) as connection:
            connection.execute(
                "DELETE FROM source_track_identities WHERE external_track_id=?",
                (PRIVATE_EXTERNAL_IDS[1],),
            )

    with pytest.raises(repair_tool.RepairFailure, match=error_code):
        repair_tool.compare_identity_timestamps(
            target_database=target,
            reference_backup=reference,
            expected_reference_sha256=_sha256(reference),
            expected_identity_count=2,
            expected_repair_count=2,
        )


def test_repair_creates_verified_schema6_backup_and_changes_only_updated_at(
    timestamp_databases: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    target, reference, reference_hash = timestamp_databases
    target_rows_before = _identity_rows(target)
    with repair_tool._readonly(target) as before_connection:
        before_snapshot = repair_tool.capture_logical_snapshot(before_connection)

    result = repair_tool.repair_identity_timestamps(
        target_database=target,
        reference_backup=reference,
        backup_directory=tmp_path / "backups",
        expected_reference_sha256=reference_hash,
        expected_identity_count=2,
        expected_repair_count=2,
    )

    assert result["ok"] is True
    assert result["updated_row_count"] == 2
    assert result["digest_proof"]["no_other_values_changed"] is True
    assert result["digest_proof"]["changed_table"] == "source_track_identities"
    assert result["digest_proof"]["changed_column"] == "updated_at"
    assert result["provider_access_count"] == 0
    assert result["secret_file_read_count"] == 0
    assert result["media_file_access_count"] == 0

    target_rows_after = _identity_rows(target)
    reference_rows = _identity_rows(reference)
    assert target_rows_after == reference_rows
    assert [row[:-1] for row in target_rows_after] == [row[:-1] for row in target_rows_before]

    backup = Path(result["backup"]["path"])
    assert backup.is_file()
    assert _sha256(backup) == result["backup"]["sha256"]
    assert _identity_rows(backup) == target_rows_before
    with repair_tool._readonly(backup) as backup_connection:
        assert repair_tool._database_health(
            backup_connection, expected_schema=6
        ) == {
            "schema_version": 6,
            "integrity_ok": True,
            "foreign_keys_ok": True,
        }
        assert repair_tool.capture_logical_snapshot(backup_connection) == before_snapshot


def test_expected_count_mismatch_rolls_back_without_creating_backup(
    timestamp_databases: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    target, reference, reference_hash = timestamp_databases
    target_hash = _sha256(target)
    rows = _identity_rows(target)
    backup_dir = tmp_path / "backups"

    with pytest.raises(repair_tool.RepairFailure, match="repair_count_mismatch"):
        repair_tool.repair_identity_timestamps(
            target_database=target,
            reference_backup=reference,
            backup_directory=backup_dir,
            expected_reference_sha256=reference_hash,
            expected_identity_count=2,
            expected_repair_count=3,
        )

    assert _sha256(target) == target_hash
    assert _identity_rows(target) == rows
    assert not backup_dir.exists()


def test_reference_hash_mismatch_fails_before_backup_or_write(
    timestamp_databases: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    target, reference, _ = timestamp_databases
    target_hash = _sha256(target)
    with pytest.raises(repair_tool.RepairFailure, match="reference_hash_mismatch"):
        repair_tool.repair_identity_timestamps(
            target_database=target,
            reference_backup=reference,
            backup_directory=tmp_path / "backups",
            expected_reference_sha256="0" * 64,
            expected_identity_count=2,
            expected_repair_count=2,
        )
    assert _sha256(target) == target_hash
    assert not (tmp_path / "backups").exists()


def test_transaction_rolls_back_if_exact_affected_count_is_not_met(
    timestamp_databases: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    target, reference, reference_hash = timestamp_databases
    with sqlite3.connect(target) as connection:
        connection.execute(
            """
            CREATE TRIGGER ignore_one_timestamp_repair
            BEFORE UPDATE OF updated_at ON source_track_identities
            WHEN OLD.external_track_id = 'PRIVATE_VIDEO_BETA'
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )
    target_hash = _sha256(target)
    rows_before = _identity_rows(target)

    with pytest.raises(
        repair_tool.RepairFailure, match="affected_row_count_mismatch"
    ):
        repair_tool.repair_identity_timestamps(
            target_database=target,
            reference_backup=reference,
            backup_directory=tmp_path / "backups",
            expected_reference_sha256=reference_hash,
            expected_identity_count=2,
            expected_repair_count=2,
        )

    assert _identity_rows(target) == rows_before
    assert _sha256(target) == target_hash
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    assert _identity_rows(backups[0]) == rows_before


def test_disposable_clone_proof_leaves_both_input_databases_byte_identical(
    timestamp_databases: tuple[Path, Path, str],
) -> None:
    target, reference, reference_hash = timestamp_databases
    target_hash = _sha256(target)
    target_stat = target.stat().st_mtime_ns
    reference_hash_before = _sha256(reference)

    result = repair_tool.prove_repair_on_disposable_clone(
        target_database=target,
        reference_backup=reference,
        expected_reference_sha256=reference_hash,
        expected_identity_count=2,
        expected_repair_count=2,
    )

    assert result["ok"] is True
    assert result["temporary_root_outside_repository"] is True
    assert result["temporary_root_deleted"] is True
    assert result["source_target_unchanged"] is True
    assert result["reference_backup_unchanged"] is True
    assert result["repair"]["updated_row_count"] == 2
    assert result["repair"]["digest_proof"]["no_other_values_changed"] is True
    assert _sha256(target) == target_hash
    assert target.stat().st_mtime_ns == target_stat
    assert _sha256(reference) == reference_hash_before


def test_repair_cli_requires_acknowledgement_no_secret_mode_and_closed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(repair_tool.NO_SECRETS_ENVIRONMENT, raising=False)
    monkeypatch.setattr(repair_tool, "_music_vault_running", lambda: False)
    with pytest.raises(repair_tool.RepairFailure, match="live_acknowledgement_missing"):
        repair_tool._live_execution_guard(
            "wrong", expected_identity_count=304, expected_repair_count=304
        )
    with pytest.raises(repair_tool.RepairFailure, match="live_expected_count_mismatch"):
        repair_tool._live_execution_guard(
            repair_tool.LIVE_ACKNOWLEDGEMENT,
            expected_identity_count=303,
            expected_repair_count=304,
        )
    with pytest.raises(repair_tool.RepairFailure, match="no_secrets_environment_missing"):
        repair_tool._live_execution_guard(
            repair_tool.LIVE_ACKNOWLEDGEMENT,
            expected_identity_count=304,
            expected_repair_count=304,
        )
    monkeypatch.setenv(repair_tool.NO_SECRETS_ENVIRONMENT, "1")
    monkeypatch.setattr(repair_tool, "_music_vault_running", lambda: True)
    with pytest.raises(repair_tool.RepairFailure, match="music_vault_process_running"):
        repair_tool._live_execution_guard(
            repair_tool.LIVE_ACKNOWLEDGEMENT,
            expected_identity_count=304,
            expected_repair_count=304,
        )


def test_compare_cli_output_never_prints_identity_values(
    timestamp_databases: tuple[Path, Path, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    target, reference, reference_hash = timestamp_databases
    code = repair_tool.main(
        [
            "compare",
            "--target-database",
            str(target),
            "--reference-backup",
            str(reference),
            "--reference-sha256",
            reference_hash,
            "--expected-identity-count",
            "2",
            "--expected-repair-count",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    report = json.loads(captured.out)
    assert report["ok"] is True
    assert all(value not in captured.out for value in PRIVATE_EXTERNAL_IDS)
    assert "PRIVATE_TITLE" not in captured.out
