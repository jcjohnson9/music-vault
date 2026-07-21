from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from music_vault.core.db import MusicVaultDB
from music_vault.core.importer import ImportSourceContext, import_file, import_folder
from music_vault.core.sync_result import SyncImportItem


def _patch_import_metadata(monkeypatch) -> None:
    from music_vault.core import importer

    monkeypatch.setattr(
        importer,
        "read_audio_metadata",
        lambda _path: {
            "title": "Synthetic WebM",
            "artist": None,
            "album": None,
            "album_artist": None,
            "release_date": None,
            "year": None,
            "duration_seconds": 1.0,
            "title_provenance": "embedded",
        },
    )
    monkeypatch.setattr(importer, "extract_embedded_cover", lambda _path: None)


def test_webm_import_uses_explicit_ffprobe_without_fallback_discovery(
    tmp_path,
    monkeypatch,
) -> None:
    from music_vault.core import importer

    media = tmp_path / "configured.webm"
    media.write_bytes(b"synthetic audio-only webm")
    configured_probe = tmp_path / "configured-tools" / "ffprobe.exe"
    captured: list[Path] = []
    monkeypatch.setattr(
        importer,
        "discover_ffmpeg",
        lambda: (_ for _ in ()).throw(AssertionError("fallback discovery used")),
    )
    monkeypatch.setattr(
        importer,
        "is_verified_audio_only_webm",
        lambda _path, *, ffprobe_path: captured.append(Path(ffprobe_path)) or True,
    )
    _patch_import_metadata(monkeypatch)
    db = MusicVaultDB(tmp_path / "library.sqlite3")

    assert import_file(db, media, ffprobe_path=configured_probe) is True
    assert captured == [configured_probe]
    db.close()


def test_webm_import_accepts_ffprobe_from_source_context(
    tmp_path,
    monkeypatch,
) -> None:
    from music_vault.core import importer

    media = tmp_path / "source-context.webm"
    media.write_bytes(b"synthetic audio-only webm")
    configured_probe = tmp_path / "source-tools" / "ffprobe.exe"
    captured: list[Path] = []
    monkeypatch.setattr(
        importer,
        "discover_ffmpeg",
        lambda: (_ for _ in ()).throw(AssertionError("fallback discovery used")),
    )
    monkeypatch.setattr(
        importer,
        "is_verified_audio_only_webm",
        lambda _path, *, ffprobe_path: captured.append(Path(ffprobe_path)) or True,
    )
    _patch_import_metadata(monkeypatch)
    db = MusicVaultDB(tmp_path / "library.sqlite3")

    assert import_file(
        db,
        media,
        ImportSourceContext("youtube", "abcdefghijk", ffprobe_path=configured_probe),
    )
    assert captured == [configured_probe]
    db.close()


def test_folder_import_threads_one_configured_ffprobe_to_each_file(
    tmp_path,
    monkeypatch,
) -> None:
    from music_vault.core import importer

    folder = tmp_path / "library"
    folder.mkdir()
    (folder / "one.webm").write_bytes(b"one")
    (folder / "two.mp3").write_bytes(b"two")
    configured_probe = tmp_path / "tools" / "ffprobe.exe"
    received: list[tuple[Path, Path | None]] = []

    def fake_import_file(_db, path, _source=None, *, ffprobe_path=None):
        received.append((Path(path), Path(ffprobe_path) if ffprobe_path else None))
        return True

    monkeypatch.setattr(importer, "import_file", fake_import_file)

    assert import_folder(object(), folder, ffprobe_path=configured_probe) == 2
    assert {path.name for path, _probe in received} == {"one.webm", "two.mp3"}
    assert {probe for _path, probe in received} == {configured_probe}


def test_multi_source_default_importer_resolves_configured_ffprobe_once(
    tmp_path,
    monkeypatch,
) -> None:
    from music_vault.core import multi_source_sync

    db = MusicVaultDB(tmp_path / "library.sqlite3")
    configured_location = tmp_path / "configured-tools"
    configured_probe = configured_location / "ffprobe.exe"
    discovery_calls: list[Path] = []
    received_contexts: list[ImportSourceContext] = []

    def fake_discovery(*, configured_location=None):
        discovery_calls.append(Path(configured_location))
        return SimpleNamespace(ready=True, ffprobe_path=configured_probe)

    def fake_import(_db, _path, source):
        received_contexts.append(source)
        return False

    monkeypatch.setattr(multi_source_sync, "discover_ffmpeg", fake_discovery)
    monkeypatch.setattr(multi_source_sync, "import_file", fake_import)
    orchestrator = multi_source_sync.MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        ffmpeg_location=configured_location,
    )
    item = SyncImportItem(str(tmp_path / "track.webm"), "abcdefghijk")

    assert orchestrator._default_importer(db, item) is None
    assert orchestrator._default_importer(db, item) is None
    assert discovery_calls == [configured_location]
    assert [Path(context.ffprobe_path) for context in received_contexts] == [
        configured_probe.resolve(),
        configured_probe.resolve(),
    ]
    db.close()


def test_manual_folder_import_uses_window_configured_ffprobe(
    tmp_path,
    monkeypatch,
) -> None:
    from music_vault import app as app_module

    folder = tmp_path / "manual-library"
    folder.mkdir()
    configured_probe = tmp_path / "manual-tools" / "ffprobe.exe"
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        app_module,
        "QFileDialog",
        SimpleNamespace(getExistingDirectory=lambda *_args: str(folder)),
    )
    monkeypatch.setattr(
        app_module,
        "QMessageBox",
        SimpleNamespace(information=lambda *_args: None),
    )

    def fake_import_folder(db, selected_folder, *, ffprobe_path=None):
        captured.update(
            db=db,
            folder=selected_folder,
            ffprobe_path=ffprobe_path,
        )
        return 0

    monkeypatch.setattr(app_module, "import_folder", fake_import_folder)
    refreshed: list[bool] = []
    harness = SimpleNamespace(
        db=object(),
        import_ffprobe_path=lambda: configured_probe,
        refresh_current_view=lambda: refreshed.append(True),
    )

    app_module.MusicVaultWindow.import_music_folder(harness)

    assert captured["db"] is harness.db
    assert captured["folder"] == str(folder)
    assert captured["ffprobe_path"] == configured_probe
    assert refreshed == [True]


def test_window_import_probe_uses_central_readiness_and_fails_closed(tmp_path) -> None:
    from music_vault.app import MusicVaultWindow

    configured_probe = tmp_path / "configured-tools" / "ffprobe.exe"
    ready_harness = SimpleNamespace(
        discover_ffmpeg_readiness=lambda: SimpleNamespace(
            ready=True,
            ffprobe_path=configured_probe,
        )
    )
    assert MusicVaultWindow.import_ffprobe_path(ready_harness) == configured_probe

    failed_harness = SimpleNamespace(
        discover_ffmpeg_readiness=lambda: (_ for _ in ()).throw(
            OSError("synthetic discovery failure")
        )
    )
    assert MusicVaultWindow.import_ffprobe_path(failed_harness) is None
