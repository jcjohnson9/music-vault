from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.core.runtime_policy import (
    ACCEPTANCE_NO_NETWORK_REASON,
    ACCEPTANCE_NO_SECRETS_REASON,
    MIGRATION_STARTUP_REASON,
    NO_NETWORK_ENVIRONMENT,
    NO_SECRETS_ENVIRONMENT,
    RuntimePolicy,
    runtime_policy_for,
)


def _database_with_track(path: Path) -> None:
    db = MusicVaultDB(path)
    db.upsert_track(
        path.parent / "synthetic.mp3",
        title="Synthetic",
        artist="Synthetic Artist",
        album="Synthetic Album",
    )
    db.close()


def _set_schema_version(path: Path, version: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={int(version)}")


def test_new_database_initialization_is_not_reported_as_migration(tmp_path: Path) -> None:
    db = MusicVaultDB(tmp_path / "new.sqlite3")
    try:
        assert db.initialized_new_database is True
        assert db.migration_performed is False
        assert db.migrated_from_version is None
        assert db.migrated_to_version is None
        assert db.migration_backup_path is None
        assert db.last_migration_backup is None
    finally:
        db.close()


def test_schema_upgrade_reports_exact_process_local_migration_state(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    backups = tmp_path / "backups"
    _database_with_track(database)
    _set_schema_version(database, CURRENT_SCHEMA_VERSION - 1)

    db = MusicVaultDB(database, backup_dir=backups)
    try:
        assert db.initialized_new_database is False
        assert db.migration_performed is True
        assert db.migrated_from_version == CURRENT_SCHEMA_VERSION - 1
        assert db.migrated_to_version == CURRENT_SCHEMA_VERSION
        assert db.migration_backup_path is not None
        assert db.migration_backup_path.is_file()
        assert db.last_migration_backup == db.migration_backup_path
        with sqlite3.connect(db.migration_backup_path) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == (
                CURRENT_SCHEMA_VERSION - 1
            )
    finally:
        db.close()


def test_current_schema_and_old_backup_do_not_report_migration(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    backups = tmp_path / "backups"
    _database_with_track(database)
    backups.mkdir()
    old_backup = backups / "music_vault_pre_schema_v7_old.sqlite3"
    old_backup.write_bytes(database.read_bytes())

    db = MusicVaultDB(database, backup_dir=backups)
    try:
        assert old_backup.is_file()
        assert db.initialized_new_database is False
        assert db.migration_performed is False
        assert db.migrated_from_version is None
        assert db.migrated_to_version is None
        assert db.migration_backup_path is None
        assert db.last_migration_backup is None
    finally:
        db.close()


def test_normal_runtime_policy_allows_configured_optional_work() -> None:
    policy = RuntimePolicy.from_environment(environ={})

    assert policy.secrets_allowed is True
    assert policy.network_allowed is True
    assert policy.provider_construction_allowed is True
    assert policy.allows_provider_construction(token_backed=False) is True
    assert policy.background_provider_work_allowed is True
    assert policy.startup_provider_work_deferred is False
    assert policy.defer_reason is None
    assert policy.status_fields() == {
        "provider_work_deferred": False,
        "provider_work_defer_reason": None,
    }


@pytest.mark.parametrize(
    ("environment", "migration_performed", "reason"),
    [
        ({}, True, MIGRATION_STARTUP_REASON),
        ({NO_NETWORK_ENVIRONMENT: "1"}, False, ACCEPTANCE_NO_NETWORK_REASON),
        ({NO_SECRETS_ENVIRONMENT: "1"}, False, ACCEPTANCE_NO_SECRETS_REASON),
        (
            {
                NO_NETWORK_ENVIRONMENT: "1",
                NO_SECRETS_ENVIRONMENT: "1",
            },
            False,
            ACCEPTANCE_NO_NETWORK_REASON,
        ),
    ],
)
def test_deferred_runtime_policy_has_one_safe_stable_reason(
    environment: dict[str, str],
    migration_performed: bool,
    reason: str,
) -> None:
    policy = RuntimePolicy.from_environment(
        migration_performed=migration_performed,
        environ=environment,
    )

    assert policy.background_provider_work_allowed is False
    assert policy.startup_provider_work_deferred is True
    assert policy.provider_construction_allowed is False
    assert policy.defer_reason == reason
    assert policy.status_fields() == {
        "provider_work_deferred": True,
        "provider_work_defer_reason": reason,
    }


def test_no_secrets_blocks_token_provider_but_not_tokenless_construction() -> None:
    policy = RuntimePolicy.from_environment(
        environ={NO_SECRETS_ENVIRONMENT: "1"}
    )

    assert policy.secrets_allowed is False
    assert policy.provider_construction_allowed is False
    assert policy.allows_provider_construction(token_backed=True) is False
    assert policy.allows_provider_construction(token_backed=False) is True


def test_policy_is_config_inert_and_does_not_leak_to_next_launch() -> None:
    config = {
        "metadata_intelligence_enabled": True,
        "metadata_discogs_enabled": True,
    }
    before = dict(config)

    deferred = RuntimePolicy.from_environment(
        migration_performed=True,
        environ={NO_NETWORK_ENVIRONMENT: "1", NO_SECRETS_ENVIRONMENT: "1"},
    )
    next_launch = RuntimePolicy.from_environment(
        migration_performed=False,
        environ={},
    )

    assert deferred.startup_provider_work_deferred is True
    assert next_launch.background_provider_work_allowed is True
    assert config == before


def test_runtime_policy_for_uses_only_the_supplied_database_instance_state() -> None:
    class StartupState:
        migration_performed = True

    migrated = runtime_policy_for(StartupState(), environ={})
    ordinary = runtime_policy_for(None, environ={})

    assert migrated.defer_reason == MIGRATION_STARTUP_REASON
    assert ordinary.background_provider_work_allowed is True


def test_environment_switches_require_literal_one() -> None:
    policy = RuntimePolicy.from_environment(
        environ={
            NO_NETWORK_ENVIRONMENT: "true",
            NO_SECRETS_ENVIRONMENT: "yes",
        }
    )

    assert policy.network_allowed is True
    assert policy.secrets_allowed is True
    assert policy.background_provider_work_allowed is True
