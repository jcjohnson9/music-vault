from types import SimpleNamespace

from music_vault.core import app_status
from music_vault.core.sync_result import SyncFailure, SyncResult
from music_vault import app


def test_acquisition_status_diagnostic_is_allowlisted():
    clean = app_status._sanitize_sync_values({
        "last_sync_acquisition_circuit_open": True,
        "last_sync_acquisition_deferred_count": 8,
        "last_sync_acquisition_diagnostic": {
            "stage": "media_transfer", "reason": "http_forbidden", "http_status": 403,
            "raw_message": "private", "path": "private", "retry_recommendation": "private",
        },
    })
    assert clean["last_sync_acquisition_circuit_open"] is True
    assert clean["last_sync_acquisition_deferred_count"] == 8
    assert clean["last_sync_acquisition_diagnostic"] == {
        "stage": "media_transfer", "reason": "http_forbidden", "http_status": 403,
        "retry_recommendation": "check_acquisition_health",
    }
    assert "private" not in str(clean)


def test_acquisition_status_rejects_unknown_values():
    clean = app_status._sanitize_sync_values({
        "last_sync_acquisition_circuit_open": "private",
        "last_sync_acquisition_deferred_count": "private",
        "last_sync_acquisition_diagnostic": {"stage": "private", "reason": "private"},
    })
    assert clean["last_sync_acquisition_circuit_open"] is False
    assert clean["last_sync_acquisition_deferred_count"] == 0
    assert clean["last_sync_acquisition_diagnostic"] is None


def test_legacy_archive_only_committed_nonfailed_identities(monkeypatch, tmp_path):
    writes = []
    window = SimpleNamespace(
        db=SimpleNamespace(conn=SimpleNamespace(in_transaction=False),
                           existing_youtube_video_ids=lambda: {"committed01", "failedid001"}),
        log_youtube=lambda message: None,
    )
    monkeypatch.setattr(app, "youtube_download_archive_path", lambda: tmp_path / "archive.txt")
    monkeypatch.setattr(app, "write_imported_archive", lambda path, ids: writes.append(ids))
    result = SyncResult("complete", None, None)
    result.add_failure(SyncFailure("failedid001", None, "failed", "import"))
    app.MusicVaultWindow._archive_legacy_youtube_imports(window, result)
    assert writes == [{"committed01"}]
    window.db.conn.in_transaction = True
    app.MusicVaultWindow._archive_legacy_youtube_imports(window, result)
    assert writes == [{"committed01"}]


def test_legacy_archive_io_failure_does_not_relabel_import(monkeypatch):
    warnings = []
    window = SimpleNamespace(
        db=SimpleNamespace(conn=SimpleNamespace(in_transaction=False),
                           existing_youtube_video_ids=lambda: {"committed01"}),
        log_youtube=warnings.append,
    )
    monkeypatch.setattr(app, "write_imported_archive", lambda *args: (_ for _ in ()).throw(OSError("private")))
    result = SyncResult("complete", None, None, imported_count=1)
    app.MusicVaultWindow._archive_legacy_youtube_imports(window, result)
    assert result.status == "complete" and result.imported_count == 1
    assert len(warnings) == 1 and "private" not in warnings[0]
