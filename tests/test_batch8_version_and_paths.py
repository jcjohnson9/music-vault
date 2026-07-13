from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_vault import version
from music_vault.core import app_status, paths
from music_vault.metadata.artist_images import ARTIST_IMAGE_USER_AGENT
from music_vault.metadata.artwork import MUSIC_VAULT_USER_AGENT
from music_vault.metadata.musicbrainz_enricher import MUSICBRAINZ_USER_AGENT


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _project_root(root: Path) -> Path:
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# marker\n", encoding="utf-8")
    return root


def _portable_root(root: Path, *, data_directory: str = "data") -> tuple[Path, Path]:
    root.mkdir(parents=True)
    executable = root / "MusicVault.exe"
    executable.write_bytes(b"synthetic executable")
    (root / paths.PORTABLE_MARKER_NAME).write_text(
        json.dumps(
            {
                "schema_version": paths.PORTABLE_MARKER_VERSION,
                "product": "Music Vault",
                "portable": True,
                "data_directory": data_directory,
            }
        ),
        encoding="utf-8",
    )
    return root, executable


@pytest.fixture(autouse=True)
def _reset_path_state():
    paths.clear_configured_data_dir()
    paths._resolved_project_root.cache_clear()
    yield
    paths.clear_configured_data_dir()
    paths._resolved_project_root.cache_clear()


def test_release_version_is_authoritative_across_active_consumers():
    assert version.APP_VERSION == "1.0.0"
    assert version.RELEASE_CHANNEL == "stable"
    assert version.WINDOWS_VERSION == (1, 0, 0, 0)
    assert app_status.APP_VERSION == version.APP_VERSION
    assert MUSICBRAINZ_USER_AGENT == version.user_agent()
    assert MUSIC_VAULT_USER_AGENT == version.user_agent()
    assert ARTIST_IMAGE_USER_AGENT == version.user_agent()


def test_spec_builds_windows_metadata_from_central_version():
    spec = (PROJECT_ROOT / "MusicVault.spec").read_text(encoding="utf-8")
    assert "from music_vault.version import" in spec
    assert "filevers=WINDOWS_VERSION" in spec
    assert "prodvers=WINDOWS_VERSION" in spec
    assert "version=windows_version_info" in spec
    assert 'StringStruct("OriginalFilename", ORIGINAL_FILENAME)' in spec
    assert '"pyside6\\\\qt6pdf"' in spec
    assert '"pyside6\\\\qt6qml"' in spec
    assert '"pyside6\\\\qt6quick"' in spec
    assert '"pyside6\\\\qt6virtualkeyboard"' in spec
    assert 'destination == "pyside6\\\\opengl32sw.dll"' in spec.casefold()


@pytest.mark.parametrize("folder_name", ["Portable Copy", "Música portátil Ω"])
def test_portable_marker_is_independent_of_folder_name_and_cwd(tmp_path, folder_name):
    root, executable = _portable_root(tmp_path / folder_name)
    unrelated = tmp_path / "unrelated working directory"
    unrelated.mkdir()
    resolution = paths.resolve_runtime_root(
        environ={},
        frozen=True,
        executable=executable,
        source_file=tmp_path / "not" / "a" / "package" / "paths.py",
        cwd=unrelated,
    )
    assert resolution.root == root.resolve()
    assert resolution.source == "portable_marker"
    assert resolution.marker_path == (root / paths.PORTABLE_MARKER_NAME).resolve()


def test_environment_override_retains_priority_over_portable_marker(tmp_path):
    override = _project_root(tmp_path / "acceptance")
    _root, executable = _portable_root(tmp_path / "portable")
    resolution = paths.resolve_runtime_root(
        environ={"MUSIC_VAULT_PROJECT_ROOT": str(override)},
        frozen=True,
        executable=executable,
        source_file=tmp_path / "not" / "source" / "core" / "paths.py",
    )
    assert resolution.root == override.resolve()
    assert resolution.source == "environment_override"


def test_source_and_development_dist_compatibility_remain_supported(tmp_path):
    source_root = _project_root(tmp_path / "source")
    source_file = source_root / "music_vault" / "core" / "paths.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("", encoding="utf-8")
    source = paths.resolve_runtime_root(
        environ={},
        frozen=False,
        source_file=source_file,
        cwd=tmp_path / "elsewhere",
    )
    assert (source.root, source.source) == (source_root.resolve(), "source_package")

    dev_root = _project_root(tmp_path / "dev root")
    executable = dev_root / "dist" / "MusicVault" / "MusicVault.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    development = paths.resolve_runtime_root(
        environ={},
        frozen=True,
        executable=executable,
        source_file=tmp_path / "not" / "source" / "core" / "paths.py",
        cwd=tmp_path,
    )
    assert (development.root, development.source) == (
        dev_root.resolve(),
        "executable_parent",
    )


def test_frozen_fallback_never_uses_arbitrary_working_directory(tmp_path):
    executable = tmp_path / "application" / "MusicVault.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"exe")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    resolution = paths.resolve_runtime_root(
        environ={},
        frozen=True,
        executable=executable,
        source_file=tmp_path / "not" / "source" / "core" / "paths.py",
        cwd=unrelated,
    )
    assert resolution.root == executable.parent.resolve()
    assert resolution.source == "executable_fallback"
    assert resolution.warning


def test_portable_runtime_paths_stay_under_selected_data_directory(
    tmp_path, monkeypatch
):
    root, executable = _portable_root(tmp_path / "portable root")
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(executable))
    monkeypatch.delenv("MUSIC_VAULT_PROJECT_ROOT", raising=False)
    paths._resolved_project_root.cache_clear()

    expected = root / "data"
    assert paths.portable_root() == root.resolve()
    assert paths.data_dir() == expected.resolve()
    for runtime_path in (
        paths.database_path(),
        paths.config_path(),
        paths.youtube_api_key_path(),
        paths.youtube_download_archive_path(),
        paths.youtube_failed_ids_path(),
        paths.covers_dir(),
        paths.artist_images_dir(),
        paths.metadata_reports_dir(),
        paths.app_status_path(),
    ):
        assert runtime_path.resolve().is_relative_to(expected.resolve())


def test_external_data_selection_persists_via_non_secret_locator(
    tmp_path, monkeypatch
):
    root, executable = _portable_root(tmp_path / "read only portable")
    local_app_data = tmp_path / "Local App Data"
    selected = tmp_path / "Writable Library" / "data"
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(executable))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("MUSIC_VAULT_PROJECT_ROOT", raising=False)
    paths._resolved_project_root.cache_clear()

    result = paths.configure_data_dir(selected, persist=True)
    assert result.configured and result.persisted
    assert paths.config_path() == selected.resolve() / "music_vault_config.json"
    assert paths.database_path() == selected.resolve() / "music_vault.sqlite3"
    assert result.locator_path is not None and result.locator_path.is_file()
    locator_text = result.locator_path.read_text(encoding="utf-8")
    assert "api" not in locator_text.casefold()
    assert "secret" not in locator_text.casefold()

    paths.clear_configured_data_dir()
    assert paths.data_dir() == selected.resolve()
    assert paths.data_directory_source() == "portable_locator"
    assert root.resolve() == paths.portable_root()


def test_unwritable_location_returns_clear_result(tmp_path):
    not_a_directory = tmp_path / "occupied"
    not_a_directory.write_text("file", encoding="utf-8")
    result = paths.runtime_root_check(not_a_directory)
    assert not result.writable
    assert result.error == "The selected location is not a folder."


def test_marker_rejects_relative_path_traversal(tmp_path, monkeypatch):
    root, executable = _portable_root(tmp_path / "portable", data_directory="../escape")
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(executable))
    monkeypatch.delenv("MUSIC_VAULT_PROJECT_ROOT", raising=False)
    paths._resolved_project_root.cache_clear()
    assert paths.data_dir() == root.resolve() / "data"


def test_marker_rejects_absolute_data_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside" / "data"
    root, executable = _portable_root(
        tmp_path / "portable", data_directory=str(outside.resolve())
    )
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(executable))
    monkeypatch.delenv("MUSIC_VAULT_PROJECT_ROOT", raising=False)
    paths._resolved_project_root.cache_clear()

    assert paths.data_dir() == root.resolve() / "data"
    assert paths.data_dir() != outside.resolve()
