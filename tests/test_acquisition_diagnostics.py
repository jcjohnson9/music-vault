from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_vault.core.acquisition_diagnostics import (
    AcquisitionCircuitBreaker,
    AcquisitionDiagnostic,
    AcquisitionError,
    AcquisitionReason as Reason,
    AcquisitionStage as Stage,
    classify_acquisition_error,
)
from music_vault.core.db import MusicVaultDB
from music_vault.core.ffmpeg import FFmpegDiscoveryResult
from music_vault.core.multi_source_sync import MultiSourceSyncOrchestrator
from music_vault.core.sync_result import SyncFailure, SyncImportItem, SyncResult
from music_vault.core.sync_sources import SyncSourceService
from music_vault.core.youtube_sync import (
    AuthorizedYouTubePlaylistSyncer,
    YouTubeSyncConfig,
    _SanitizedYDLLogger,
    write_imported_archive,
)


FORBIDDEN = AcquisitionDiagnostic(Stage.TRANSFER, Reason.HTTP_FORBIDDEN, 403)


@pytest.mark.parametrize(
    ("message", "stage", "expected_stage", "reason", "status"),
    [
        ("runtime missing", Stage.READINESS, Stage.READINESS, Reason.NOT_READY, None),
        ("HTTP 403 quotaExceeded", Stage.ENUMERATION, Stage.ENUMERATION, Reason.QUOTA, 403),
        ("HTTP 401", Stage.ENUMERATION, Stage.ENUMERATION, Reason.API_ACCESS, 401),
        ("HTTP 403", Stage.ENUMERATION, Stage.ENUMERATION, Reason.API_ACCESS, 403),
        ("Private video HTTP 403", Stage.METADATA, Stage.METADATA, Reason.UNAVAILABLE, 403),
        ("Video unavailable", Stage.METADATA, Stage.METADATA, Reason.UNAVAILABLE, None),
        ("Video \x1b[0;31munavailable\x1b[0m", Stage.METADATA, Stage.METADATA, Reason.UNAVAILABLE, None),
        ("Video unavailable. Sign in to confirm you're not a bot", Stage.METADATA, Stage.METADATA, Reason.CHALLENGE, None),
        ("Video unavailable. This content isn't available, try again later. The current session has been rate-limited", Stage.METADATA, Stage.METADATA, Reason.RATE_LIMITED, None),
        ("Sign in to confirm you're not a bot", Stage.METADATA, Stage.METADATA, Reason.CHALLENGE, None),
        ("No supported JavaScript runtime", Stage.METADATA, Stage.METADATA, Reason.CHALLENGE, None),
        ("PO Token required", Stage.METADATA, Stage.METADATA, Reason.CHALLENGE, None),
        ("Unable to download video data: HTTP Error 403", Stage.METADATA, Stage.TRANSFER, Reason.HTTP_FORBIDDEN, 403),
        ("HTTP Error 403", Stage.METADATA, Stage.METADATA, Reason.HTTP_FORBIDDEN, 403),
        ("HTTP Error 429", Stage.TRANSFER, Stage.TRANSFER, Reason.RATE_LIMITED, 429),
        ("status code: 503", Stage.TRANSFER, Stage.TRANSFER, Reason.SERVER, 503),
        ("Connection reset", Stage.TRANSFER, Stage.TRANSFER, Reason.NETWORK, None),
        ("Postprocessing: FFmpeg exited", Stage.METADATA, Stage.TRANSFORM, Reason.TRANSFORMATION, None),
        ("invalid final codec", Stage.VERIFICATION, Stage.VERIFICATION, Reason.VERIFICATION, None),
        ("database unavailable", Stage.IMPORT, Stage.IMPORT, Reason.IMPORT, None),
        ("unrecognized error", Stage.METADATA, Stage.METADATA, Reason.UNKNOWN, None),
    ],
)
def test_diagnostics_keep_only_allowlisted_observations(
    message, stage, expected_stage, reason, status,
):
    # Deliberately synthetic credential/path markers. The entire raw provider
    # message, not only recognized URL parameters, must be absent downstream.
    sensitive = " https://invalid.test/audio?sig=SYNTHETIC_SECRET C:\\private\\synthetic.mp3"
    diagnostic = classify_acquisition_error(RuntimeError(message + sensitive), stage)
    assert diagnostic.stage == expected_stage
    assert diagnostic.reason == reason
    assert diagnostic.http_status == status
    exported = json.dumps(diagnostic.to_dict()) + diagnostic.message
    assert "SYNTHETIC_SECRET" not in exported
    assert "invalid.test" not in exported
    assert "synthetic.mp3" not in exported
    assert set(diagnostic.to_dict()) == {
        "stage", "reason", "http_status", "retry_recommendation",
    }


def test_typed_stage_survives_wrapping_and_sync_status_serialization():
    wrapped = AcquisitionError(FORBIDDEN)
    assert classify_acquisition_error(wrapped, Stage.METADATA) is FORBIDDEN
    result = SyncResult("complete", None, None)
    result.add_failure(SyncFailure("aaaaaaaaaaa", None, wrapped, "download", acquisition=FORBIDDEN))
    result.acquisition_circuit_open = True
    result.acquisition_deferred_count = 4
    result.finish_imports(0)
    status = result.to_status_dict()
    assert result.status == "complete_with_issues"
    assert status["last_sync_acquisition_diagnostic"] == FORBIDDEN.to_dict()
    assert status["last_sync_acquisition_deferred_count"] == 4
    assert status["last_sync_acquisition_circuit_open"] is True
    assert status["last_sync_failures"][0]["acquisition"] == FORBIDDEN.to_dict()


def test_backend_logger_never_forwards_raw_messages():
    messages = []
    logger = _SanitizedYDLLogger(messages.append)
    raw = "https://invalid.test/private?sig=SYNTHETIC_SECRET C:\\private\\synthetic.mp3"
    logger.debug(raw)
    logger.warning(raw)
    logger.error(raw)
    logger.error("Unable to download video data: HTTP Error 403 " + raw)
    assert len(messages) == 2
    assert messages[-1] == FORBIDDEN.message
    assert "SYNTHETIC_SECRET" not in " ".join(messages)
    assert "synthetic.mp3" not in " ".join(messages)


def test_circuit_requires_three_distinct_attempted_items_and_stays_open():
    circuit = AcquisitionCircuitBreaker()
    assert circuit.failure("a", FORBIDDEN) is False
    for _ in range(10):
        assert circuit.failure("a", FORBIDDEN) is False
    assert circuit.consecutive_failures == 1
    assert circuit.failure("b", FORBIDDEN) is False
    assert circuit.failure("c", FORBIDDEN) is True
    assert circuit.consecutive_failures == 3
    circuit.success()
    assert circuit.open is True  # only a new user-requested batch resets it
    assert circuit.last_diagnostic is FORBIDDEN


@pytest.mark.parametrize("reset", ["success", "unavailable", "verification"])
def test_success_or_item_specific_failure_resets_systemic_evidence(reset):
    circuit = AcquisitionCircuitBreaker()
    circuit.failure("a", FORBIDDEN)
    circuit.failure("b", FORBIDDEN)
    if reset == "success":
        circuit.success()
    else:
        reason = Reason.UNAVAILABLE if reset == "unavailable" else Reason.VERIFICATION
        circuit.failure("c", AcquisitionDiagnostic(Stage.VERIFICATION, reason))
    assert not circuit.open
    assert circuit.consecutive_failures == 0
    assert not circuit.failure("a", FORBIDDEN)
    assert not circuit.failure("b", FORBIDDEN)
    assert circuit.failure("c", FORBIDDEN)


def _entries(*video_ids):
    return [
        {"id": video_id, "title": "Synthetic", "source_item_id": f"item-{index}", "position": index}
        for index, video_id in enumerate(video_ids)
    ]


def _synthetic_syncer(config, entries, downloader, report=None):
    syncer = AuthorizedYouTubePlaylistSyncer(config, report)
    syncer._resolve_ffmpeg_once = lambda: FFmpegDiscoveryResult(False, "none")
    syncer._extract_playlist_entries_via_api = lambda: (
        syncer._playlist_id(), "Synthetic source", entries,
    )
    syncer._download_one = downloader
    return syncer


def _download_item(config, video_id):
    target = Path(config.source_destination_dir or config.output_dir) / f"Synthetic [{video_id}].opus"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"synthetic verified output")
    return SyncImportItem(str(target), video_id)


def test_provider_defers_only_unattempted_distinct_new_items_without_archive_bans(tmp_path):
    known, local = "kkkkkkkkkkk", "lllllllllll"
    local_file = tmp_path / f"Synthetic [{local}].opus"
    local_file.write_bytes(b"synthetic existing output")
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=PLsynthetic", tmp_path / "downloads", tmp_path / "archive.txt",
        existing_video_ids=frozenset({known}),
        known_downloads=((local, str(local_file)),),
    )
    config.archive_file.write_text("youtube zzzzzzzzzzz\n", encoding="utf-8")
    attempted = []

    def download(video_id, *_args):
        attempted.append(video_id)
        raise AcquisitionError(FORBIDDEN)

    entries = _entries(
        "aaaaaaaaaaa", "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc",
        "ddddddddddd", known, local, "eeeeeeeeeee", "ddddddddddd",
    )
    result = _synthetic_syncer(config, entries, download).sync()

    assert attempted == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    assert result.new_item_count == 5
    assert result.existing_count == 2
    assert result.acquisition_deferred_count == 2
    assert result.acquisition_circuit_open
    assert result.failed_count == 3
    assert result.downloaded_count == 0
    assert result.successful_video_ids == {known, local}
    assert [item.video_id for item in result.import_items] == [local]
    assert result.snapshot.complete
    assert all(item.available for item in result.snapshot.items)
    assert config.archive_file.read_text(encoding="utf-8") == f"youtube {known}\n"


def test_verified_download_is_not_archived_before_import_and_archive_failure_is_nonfatal(tmp_path):
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=PLsynthetic", tmp_path / "downloads", tmp_path / "archive.txt", known_downloads=(),
    )
    messages = []
    syncer = _synthetic_syncer(
        config, _entries("aaaaaaaaaaa"),
        lambda video_id, *_args: _download_item(config, video_id), messages.append,
    )
    result = syncer.sync()
    assert result.downloaded_count == 1
    assert result.imported_count == 0
    assert config.archive_file.read_text(encoding="utf-8") == ""

    def cannot_write(_ids):
        raise PermissionError("synthetic archive permission error")

    syncer._write_archive_ids_atomic = cannot_write
    syncer._existing_downloads = lambda: {"aaaaaaaaaaa": Path(result.import_items[0].path)}
    second = syncer.sync()
    assert second.status == "complete"
    assert len(second.import_items) == 1
    assert any("history could not be updated" in message for message in messages)


def test_atomic_archive_rejects_injected_ids_and_preserves_old_file_on_replace_failure(tmp_path, monkeypatch):
    archive = tmp_path / "archive.txt"
    write_imported_archive(archive, {"aaaaaaaaaaa", "bbbbbbbbbbb", "bad\nyoutube ccccccccccc"})
    before = archive.read_bytes()
    assert archive.read_text(encoding="utf-8") == "youtube aaaaaaaaaaa\nyoutube bbbbbbbbbbb\n"

    def deny_replace(*_args):
        raise PermissionError("synthetic")

    monkeypatch.setattr("music_vault.core.youtube_sync.os.replace", deny_replace)
    with pytest.raises(PermissionError):
        write_imported_archive(archive, {"ddddddddddd"})
    assert archive.read_bytes() == before


@pytest.fixture
def source_fixture(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3")
    service = SyncSourceService(db)
    playlist = db.create_playlist("Synthetic destination")
    manual_file = tmp_path / "manual.wav"
    manual_file.write_bytes(b"synthetic manual media")
    manual_track = db.upsert_track(manual_file)
    db.add_track_to_playlist(playlist, manual_track)
    sources = [
        service.create_source(
            f"PLsynthetic{letter}", label=f"Synthetic {letter}",
            destination_kind="playlist" if letter == "A" else "library",
            destination_playlist_id=playlist if letter == "A" else None,
        )
        for letter in "ABC"
    ]
    yield db, service, sources, playlist, manual_track
    db.close()


def _import(db, item):
    return db.upsert_track(item.path, source_kind="youtube", source_video_id=item.video_id)


def test_batch_circuit_crosses_sources_preserves_origins_and_resets_on_next_request(
    source_fixture, tmp_path,
):
    db, service, sources, playlist, manual_track = source_fixture
    source_a, source_b, source_c = sources
    old = "ooooooooooo"
    entries = {source.id: _entries(old) for source in sources}
    failures, enumerated, attempted, transitions = set(), [], [], []

    def factory(config, report):
        enumerated.append(config.saved_source_id)

        def download(video_id, *_args):
            attempted.append(video_id)
            if video_id in failures:
                raise AcquisitionError(FORBIDDEN)
            return _download_item(config, video_id)

        return _synthetic_syncer(config, entries[config.saved_source_id], download, report)

    archive = tmp_path / "archive.txt"
    orchestrator = MultiSourceSyncOrchestrator(
        db, tmp_path / "downloads", archive_file=archive, source_service=service,
        syncer_factory=factory, importer=_import, transition_callback=transitions.append,
    )
    assert orchestrator.sync_all_enabled().total_imported == 1
    canonical = db.canonical_track_id("youtube", old)
    origin_query = (
        "SELECT playlist_id, track_id, origin_kind, sync_source_id, origin_position "
        "FROM playlist_track_origins ORDER BY playlist_id, track_id, origin_kind"
    )
    before_origins = [tuple(row) for row in db.conn.execute(origin_query)]
    source_c_before = [tuple(row) for row in db.conn.execute("SELECT * FROM sync_source_items WHERE source_id=?", (source_c.id,))]
    before_files = {Path(row[0]): Path(row[0]).read_bytes() for row in db.conn.execute("SELECT path FROM tracks")}
    enumerated.clear()
    attempted.clear()
    entries[source_a.id] = _entries(old, "aaaaaaaaaaa")
    entries[source_b.id] = _entries(old, "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd")
    entries[source_c.id] = _entries(old, "eeeeeeeeeee")
    failures.update("aaaaaaaaaaa bbbbbbbbbbb ccccccccccc ddddddddddd eeeeeeeeeee".split())

    result = orchestrator.sync_all_enabled()

    assert result.status == "complete_with_issues"
    assert result.stopped_after_current
    assert result.selected_source_count == 3
    assert len(result.source_outcomes) == 2
    assert enumerated == [source_a.id, source_b.id]
    assert attempted == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    assert result.total_failed_items == 3
    assert transitions[-1]["last_sync_acquisition_circuit_open"] is True
    assert transitions[-1]["last_sync_acquisition_deferred_count"] == 1
    assert transitions[-1]["last_sync_acquisition_diagnostic"] == FORBIDDEN.to_dict()
    assert set(row["id"] for row in db.get_playlist_tracks(playlist)) == {canonical, manual_track}
    assert [tuple(row) for row in db.conn.execute(origin_query)] == before_origins
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM sync_source_items WHERE source_id=?", (source_c.id,))] == source_c_before
    assert {path: path.read_bytes() for path in before_files} == before_files
    deferred = db.conn.execute("SELECT availability_status, last_error FROM sync_source_items WHERE video_id='ddddddddddd'").fetchone()
    assert tuple(deferred) == ("available", None)
    assert db.conn.execute("SELECT COUNT(*) FROM sync_failures").fetchone()[0] == 3
    assert archive.read_text(encoding="utf-8") == f"youtube {old}\n"

    failures.clear()
    resumed = orchestrator.sync_all_enabled()
    assert resumed.status == "complete"
    assert not orchestrator._acquisition_circuit.open
    assert resumed.total_imported == 5
    assert db.conn.execute("SELECT COUNT(*) FROM sync_failures WHERE status='unresolved'").fetchone()[0] == 0
    assert manual_track in [row["id"] for row in db.get_playlist_tracks(playlist)]


@pytest.mark.parametrize("fault", ["import", "persistence", "archive"])
def test_archive_boundary_never_claims_failed_import_or_rolled_back_membership(
    source_fixture, tmp_path, monkeypatch, fault,
):
    db, service, sources, playlist, manual_track = source_fixture
    source = sources[0]
    archive = tmp_path / "archive.txt"
    events = []

    def factory(config, report):
        return _synthetic_syncer(
            config, _entries("aaaaaaaaaaa"),
            lambda video_id, *_args: _download_item(config, video_id), report,
        )

    def importer(db, item):
        if fault == "import":
            raise RuntimeError("synthetic private path and secret must not propagate")
        return _import(db, item)

    orchestrator = MultiSourceSyncOrchestrator(
        db, tmp_path / "downloads", archive_file=archive,
        source_service=service, syncer_factory=factory, importer=importer,
        progress=events.append,
    )
    if fault == "persistence":
        original = orchestrator._record_source_run

        def fail_good_snapshot(source_id, token, result):
            if result.snapshot.complete:
                raise RuntimeError("synthetic source transaction failure")
            original(source_id, token, result)

        monkeypatch.setattr(orchestrator, "_record_source_run", fail_good_snapshot)
    if fault == "archive":
        def denied_archive(*_args):
            raise PermissionError("synthetic private path")

        monkeypatch.setattr("music_vault.core.multi_source_sync.write_imported_archive", denied_archive)

    result = orchestrator.sync_selected([source.id])
    outcome = result.source_outcomes[0]
    assert archive.read_text(encoding="utf-8") == ""
    runs = db.conn.execute("SELECT status, imported_count FROM sync_source_runs").fetchall()
    assert len(runs) == 1
    if fault == "archive":
        assert outcome.status == "complete"
        assert outcome.snapshot.complete
        assert outcome.imported_count == 1
        assert len(db.get_playlist_tracks(playlist)) == 2
        assert any("changes were saved" in (event.message or "") for event in events)
    else:
        assert [row["id"] for row in db.get_playlist_tracks(playlist)] == [manual_track]
        assert outcome.status == ("failed" if fault == "persistence" else "complete_with_issues")
        if fault == "import":
            assert outcome.imported_count == 0
            assert outcome.successful_video_ids == set()
            assert outcome.acquisition_diagnostic.stage == Stage.IMPORT
            assert "private path" not in outcome.failures[0].reason
