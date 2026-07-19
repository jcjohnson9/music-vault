from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.metadata.intelligence_settings import DiscogsTokenStore


def _synthetic_runtime(
    tmp_path: Path,
    *,
    metadata_enabled: bool,
) -> tuple[Path, Path, Path]:
    root = tmp_path / "synthetic runtime"
    data = root / "data"
    downloads = data / "youtube_downloads"
    downloads.mkdir(parents=True)
    (root / "music_vault").mkdir()
    (root / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    source_icons = Path(__file__).resolve().parents[1] / "assets" / "icons"
    shutil.copytree(source_icons, root / "assets" / "icons")
    (data / "music_vault_config.json").write_text(
        json.dumps(
            {
                "download_folder": str(downloads),
                "onboarding_completed": True,
                "artist_image_fetch_enabled": False,
                "metadata_intelligence_enabled": metadata_enabled,
                "metadata_intelligence_consent_version": 1,
                "metadata_discogs_enabled": metadata_enabled,
                "metadata_discogs_consent_version": 1,
            }
        ),
        encoding="utf-8",
    )
    token = data / "discogs_token.txt"
    token.write_text("synthetic-secret-marker\n", encoding="utf-8")
    return root, data, token


def _prepare_schema_six_database(data: Path) -> None:
    database = data / "music_vault.sqlite3"
    db = MusicVaultDB(database, backup_dir=data / "backups")
    db.close()
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION - 1}")


def _construct_window_without_token_read(
    *,
    root: Path,
    token: Path,
    monkeypatch,
    qapp,
):
    from music_vault import app as app_module
    from music_vault.core import paths
    from music_vault.ui.icons import clear_icon_cache

    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(root))
    monkeypatch.delenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", raising=False)
    monkeypatch.delenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", raising=False)
    monkeypatch.setattr(paths, "_configured_data_directory", None)
    paths._resolved_project_root.cache_clear()
    clear_icon_cache()

    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if Path(path) == token:
            raise AssertionError("startup read Discogs token contents")
        return original_read_text(path, *args, **kwargs)

    def forbidden_token_read(*_args, **_kwargs):
        raise AssertionError("startup called DiscogsTokenStore.read")

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(DiscogsTokenStore, "read", forbidden_token_read)
    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "use_system_default_audio_output",
        lambda self: None,
    )
    monkeypatch.setattr(app_module, "apply_dark_title_bar", lambda _window: False)

    window = app_module.MusicVaultWindow()
    qapp.processEvents()
    return window, paths, clear_icon_cache


def _close_window(window, paths, clear_icon_cache, qapp) -> None:
    window.audio_device_timer.stop()
    window.volume_save_timer.stop()
    window.close()
    window.db.close()
    window.deleteLater()
    qapp.processEvents()
    paths._resolved_project_root.cache_clear()
    clear_icon_cache()


def test_ordinary_window_construction_does_not_read_discogs_token(
    tmp_path: Path,
    monkeypatch,
    qapp,
) -> None:
    root, data, token = _synthetic_runtime(tmp_path, metadata_enabled=False)
    window, paths, clear_icon_cache = _construct_window_without_token_read(
        root=root,
        token=token,
        monkeypatch=monkeypatch,
        qapp=qapp,
    )
    try:
        assert window.runtime_policy.startup_provider_work_deferred is False
        assert window.discogs_provider_status.text() == "Discogs: Token configured"
        payload = json.loads(
            (data / "music_vault_status.json").read_text(encoding="utf-8")
        )
        assert payload["discogs_ready"] is True
    finally:
        _close_window(window, paths, clear_icon_cache, qapp)


def test_migration_only_window_construction_does_not_read_discogs_token(
    tmp_path: Path,
    monkeypatch,
    qapp,
) -> None:
    root, data, token = _synthetic_runtime(tmp_path, metadata_enabled=True)
    _prepare_schema_six_database(data)
    window, paths, clear_icon_cache = _construct_window_without_token_read(
        root=root,
        token=token,
        monkeypatch=monkeypatch,
        qapp=qapp,
    )
    try:
        assert window.db.migration_performed is True
        assert window.runtime_policy.startup_provider_work_deferred is True
        assert window.discogs_provider_status.text() == (
            "Providers: Deferred until an allowed launch"
        )
        payload = json.loads(
            (data / "music_vault_status.json").read_text(encoding="utf-8")
        )
        assert payload["discogs_ready"] is False
        assert payload["provider_work_defer_reason"] == "migration_startup"
    finally:
        _close_window(window, paths, clear_icon_cache, qapp)


def test_stored_readiness_is_content_blind_but_provider_read_is_preserved(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = tmp_path / "discogs_token.txt"
    token.write_text("synthetic-provider-token\n", encoding="utf-8")
    store = DiscogsTokenStore(token)
    original_read = store.read

    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("content-blind readiness opened the token")
        ),
    )
    assert store.stored() is True

    monkeypatch.undo()
    assert original_read() == "synthetic-provider-token"


@pytest.mark.parametrize("migration_performed", (False, True))
def test_app_status_discogs_readiness_never_reads_token_contents(
    tmp_path: Path,
    monkeypatch,
    migration_performed: bool,
) -> None:
    from music_vault.core import app_status

    token = tmp_path / "discogs_token.txt"
    token.write_text("synthetic-provider-token\n", encoding="utf-8")
    database = type("Database", (), {"migration_performed": migration_performed})()
    monkeypatch.setattr(app_status, "discogs_token_path", lambda: token)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("App Status opened the Discogs token")
        ),
    )

    assert app_status._discogs_ready(database) is (not migration_performed)
