from __future__ import annotations

from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from music_vault.core.multi_source_sync import MultiSourceSyncOrchestrator
from music_vault.core.sync_result import (
    PlaylistSnapshot,
    PlaylistSnapshotItem,
    SyncFailure,
    SyncImportItem,
    SyncResult,
)
from music_vault.core.sync_sources import SyncSourceService


VIDEOS = {
    "A": "aaaaaaaaaaa",
    "B": "bbbbbbbbbbb",
    "C": "ccccccccccc",
    "D": "ddddddddddd",
    "E": "eeeeeeeeeee",
    "F": "fffffffffff",
}


def _item(source_item_id: str, video: str | None, position: int, *, unavailable=False):
    return PlaylistSnapshotItem(
        source_item_id,
        VIDEOS.get(video) if video else None,
        position,
        f"Synthetic {video or 'unavailable'}",
        "Synthetic unavailable item." if unavailable else None,
    )


class SyntheticSyncer:
    def __init__(self, config, report, snapshots, calls):
        self.config = config
        self.report = report
        self.snapshots = snapshots
        self.calls = calls

    def sync(self):
        snapshot = self.snapshots[self.config.saved_source_id]
        if not snapshot.complete:
            return SyncResult.failed_result(
                snapshot.error or "synthetic enumeration failure",
                playlist_id=snapshot.playlist_id,
                saved_source_id=self.config.saved_source_id,
                snapshot=snapshot,
            )
        result = SyncResult(
            "complete",
            snapshot.playlist_id,
            snapshot.playlist_title,
            visible_item_count=len(snapshot.items),
            saved_source_id=self.config.saved_source_id,
            snapshot=snapshot,
            duplicate_occurrence_count=snapshot.duplicate_occurrence_count,
        )
        database_ids = set(self.config.existing_video_ids)
        known_downloads = self.config.shared_download_index
        if known_downloads is None:
            known_downloads = dict(self.config.known_downloads or ())
        processed = set()
        occurrence_ids = {}
        for item in snapshot.items:
            if item.video_id:
                occurrence_ids.setdefault(item.video_id, []).append(item.source_item_id)
        for item in snapshot.items:
            if not item.available:
                result.add_failure(
                    SyncFailure(
                        item.video_id,
                        item.title,
                        item.availability_reason or "unavailable",
                        "unavailable",
                        item.source_item_id,
                    )
                )
                continue
            if item.video_id in processed:
                continue
            processed.add(item.video_id)
            if item.video_id in database_ids or item.video_id in known_downloads:
                result.existing_count += 1
                result.successful_video_ids.add(item.video_id)
                if item.video_id in known_downloads and item.video_id not in database_ids:
                    result.import_items.append(
                        SyncImportItem(
                            known_downloads[item.video_id],
                            item.video_id,
                            source_item_ids=tuple(occurrence_ids[item.video_id]),
                        )
                    )
                continue
            self.calls.append((self.config.saved_source_id, item.video_id))
            target = self.config.source_destination_dir / f"Track [{item.video_id}].mp3"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic media")
            result.downloaded_count += 1
            result.new_item_count += 1
            result.downloaded_paths.append(str(target))
            result.import_items.append(
                SyncImportItem(
                    str(target),
                    item.video_id,
                    source_item_ids=tuple(occurrence_ids[item.video_id]),
                )
            )
            result.successful_video_ids.add(item.video_id)
        result.refresh_status()
        return result


def _import(db, item):
    return db.upsert_track(
        item.path,
        source_kind="youtube",
        source_video_id=item.video_id,
    )


def _fixture(tmp_path):
    db = MusicVaultDB(tmp_path / "library.sqlite3")
    playlist_a = db.create_playlist("Playlist A")
    playlist_b = db.create_playlist("Playlist B")
    manual_path = tmp_path / "manual-x.mp3"
    manual_path.write_bytes(b"manual")
    manual_track = db.upsert_track(manual_path, title="Manual X")
    db.add_track_to_playlist(playlist_a, manual_track)
    sources = SyncSourceService(db)
    source_a = sources.create_source(
        "PLsourceAAAA",
        label="Source A",
        destination_kind="playlist",
        destination_playlist_id=playlist_a,
    )
    source_b = sources.create_source(
        "PLsourceBBBB",
        label="Source B",
        destination_kind="playlist",
        destination_playlist_id=playlist_b,
    )
    source_c = sources.create_source("PLsourceCCCC", label="Source C")
    return db, sources, (source_a, source_b, source_c), (playlist_a, playlist_b), manual_track


def test_sequential_sync_reuses_cross_source_identity_and_keeps_occurrences(tmp_path):
    db, service, sources, playlists, manual_track = _fixture(tmp_path)
    source_a, source_b, source_c = sources
    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id,
            "Remote A",
            [_item("a1", "A", 0), _item("a2", "B", 1), _item("a3", "B", 2), _item("a4", "C", 3)],
        ),
        source_b.id: PlaylistSnapshot.completed(
            source_b.external_id,
            "Remote B",
            [_item("b1", "B", 0), _item("b2", "D", 1), _item("b3", None, 2, unavailable=True)],
        ),
        source_c.id: PlaylistSnapshot.completed(
            source_c.external_id, "Remote C", [_item("c1", "E", 0)]
        ),
    }
    calls = []
    shared_indexes = []

    def factory(config, report):
        shared_indexes.append(config.shared_download_index)
        return SyntheticSyncer(config, report, snapshots, calls)

    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=factory,
        importer=_import,
    )
    valid_id_scan_count = 0
    original_valid_ids = orchestrator._valid_database_video_ids

    def count_valid_id_scan():
        nonlocal valid_id_scan_count
        valid_id_scan_count += 1
        return original_valid_ids()

    orchestrator._valid_database_video_ids = count_valid_id_scan

    aggregate = orchestrator.sync_all_enabled()
    assert aggregate.status == "complete_with_issues"
    assert aggregate.completed_source_count == 2
    assert aggregate.issue_source_count == 1
    assert valid_id_scan_count == 1
    assert len(shared_indexes) == 3
    assert shared_indexes[0] is not None
    assert all(index is shared_indexes[0] for index in shared_indexes)
    with pytest.raises(TypeError):
        shared_indexes[0]["not-a-video-id"] = tmp_path / "mutation.mp3"
    assert aggregate.total_imported == 5
    assert [video_id for _source, video_id in calls].count(VIDEOS["B"]) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM source_track_identities").fetchone()[0] == 5
    assert db.conn.execute(
        "SELECT COUNT(*) FROM sync_source_items WHERE source_id=?", (source_a.id,)
    ).fetchone()[0] == 4
    assert aggregate.total_duplicate_occurrences == 1

    playlist_a_ids = [row["id"] for row in db.get_playlist_tracks(playlists[0])]
    expected_a = [
        db.canonical_track_id("youtube", VIDEOS[key]) for key in ("A", "B", "C")
    ] + [manual_track]
    assert playlist_a_ids == expected_a
    playlist_b_ids = [row["id"] for row in db.get_playlist_tracks(playlists[1])]
    assert playlist_b_ids == [
        db.canonical_track_id("youtube", VIDEOS[key]) for key in ("B", "D")
    ]
    db.close()


def test_failed_enumeration_preserves_last_good_snapshot_and_stop_is_cooperative(tmp_path):
    db, service, sources, playlists, _manual_track = _fixture(tmp_path)
    source_a, source_b, source_c = sources
    first = PlaylistSnapshot.completed(
        source_a.external_id, "Remote A", [_item("a1", "A", 0), _item("a2", "B", 1)]
    )
    snapshots = {
        source_a.id: first,
        source_b.id: PlaylistSnapshot.completed(
            source_b.external_id, "Remote B", [_item("b1", "D", 0)]
        ),
        source_c.id: PlaylistSnapshot.completed(
            source_c.external_id, "Remote C", [_item("c1", "E", 0)]
        ),
    }
    calls = []
    holder = {}

    def factory(config, report):
        syncer = SyntheticSyncer(config, report, snapshots, calls)
        if config.saved_source_id == source_a.id and holder.get("stop"):
            original = syncer.sync

            def sync_and_stop():
                result = original()
                holder["orchestrator"].request_stop_after_current()
                return result

            syncer.sync = sync_and_stop
        return syncer

    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=factory,
        importer=_import,
    )
    holder["orchestrator"] = orchestrator
    orchestrator.sync_selected([source_a.id])
    before = list(
        db.conn.execute(
            "SELECT source_item_id, removed_at FROM sync_source_items "
            "WHERE source_id=? ORDER BY source_item_id",
            (source_a.id,),
        )
    )
    snapshots[source_a.id] = PlaylistSnapshot.failed(
        "synthetic top-level failure", playlist_id=source_a.external_id
    )
    failed = orchestrator.sync_selected([source_a.id])
    after = list(
        db.conn.execute(
            "SELECT source_item_id, removed_at FROM sync_source_items "
            "WHERE source_id=? ORDER BY source_item_id",
            (source_a.id,),
        )
    )
    assert failed.status == "failed"
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert [row["id"] for row in db.get_playlist_tracks(playlists[0])][:-1] == [
        db.canonical_track_id("youtube", VIDEOS["A"]),
        db.canonical_track_id("youtube", VIDEOS["B"]),
    ]

    snapshots[source_a.id] = first
    holder["stop"] = True
    stopped = orchestrator.sync_all_enabled()
    assert stopped.stopped_after_current
    assert len(stopped.source_outcomes) == 1
    assert stopped.status == "complete_with_issues"
    db.close()


def test_import_failure_does_not_make_later_source_redownload(tmp_path):
    db, service, sources, _playlists, _manual_track = _fixture(tmp_path)
    source_a, source_b, _source_c = sources
    service.set_enabled(sources[2].id, False)
    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id, "Remote A", [_item("a1", "B", 0)]
        ),
        source_b.id: PlaylistSnapshot.completed(
            source_b.external_id, "Remote B", [_item("b1", "B", 0)]
        ),
    }
    calls = []
    attempts = 0

    def flaky_import(db_object, item):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic first import failure")
        return _import(db_object, item)

    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=lambda config, report: SyntheticSyncer(
            config, report, snapshots, calls
        ),
        importer=flaky_import,
    )
    aggregate = orchestrator.sync_all_enabled()
    assert [video_id for _source, video_id in calls] == [VIDEOS["B"]]
    assert attempts == 2
    assert db.canonical_track_id("youtube", VIDEOS["B"], require_existing_file=True)
    assert aggregate.status == "complete_with_issues"
    db.close()


def test_source_outcome_fault_rolls_back_snapshot_removals_and_membership(tmp_path):
    db, service, sources, playlists, manual_track = _fixture(tmp_path)
    source_a = sources[0]
    service.set_enabled(sources[1].id, False)
    service.set_enabled(sources[2].id, False)
    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id,
            "Remote A",
            [_item("a1", "A", 0), _item("a2", "B", 1)],
        )
    }
    calls = []
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=lambda config, report: SyntheticSyncer(
            config, report, snapshots, calls
        ),
        importer=_import,
    )
    orchestrator.sync_selected([source_a.id])
    before_items = [
        tuple(row)
        for row in db.conn.execute(
            "SELECT source_item_id, removed_at FROM sync_source_items "
            "WHERE source_id=? ORDER BY source_item_id",
            (source_a.id,),
        )
    ]
    before_playlist = [row["id"] for row in db.get_playlist_tracks(playlists[0])]
    before_runs = db.conn.execute(
        "SELECT COUNT(*) FROM sync_source_runs WHERE source_id=?", (source_a.id,)
    ).fetchone()[0]

    snapshots[source_a.id] = PlaylistSnapshot.completed(
        source_a.external_id,
        "Remote A",
        [_item("a1", "A", 0), _item("a3", "D", 1, unavailable=True)],
    )
    original_record_run = orchestrator._record_source_run
    injected = False

    def fail_after_run_insert(source_id, batch_token, result):
        nonlocal injected
        original_record_run(source_id, batch_token, result)
        if not injected:
            injected = True
            raise RuntimeError("synthetic source-outcome persistence fault")

    orchestrator._record_source_run = fail_after_run_insert
    failed = orchestrator.sync_selected([source_a.id])

    assert injected
    assert failed.status == "failed"
    assert failed.source_outcomes[0].removed_occurrence_count == 0
    after_items = [
        tuple(row)
        for row in db.conn.execute(
            "SELECT source_item_id, removed_at FROM sync_source_items "
            "WHERE source_id=? ORDER BY source_item_id",
            (source_a.id,),
        )
    ]
    assert after_items == before_items
    assert [row["id"] for row in db.get_playlist_tracks(playlists[0])] == before_playlist
    assert before_playlist[-1] == manual_track
    runs = list(
        db.conn.execute(
            "SELECT status, removed_count FROM sync_source_runs "
            "WHERE source_id=? ORDER BY id",
            (source_a.id,),
        )
    )
    assert len(runs) == before_runs + 1
    assert tuple(runs[-1]) == ("failed", 0)
    failure = db.conn.execute(
        "SELECT attempt_count, status FROM sync_failures "
        "WHERE sync_source_id=? AND video_id=?",
        (source_a.id, VIDEOS["D"]),
    ).fetchone()
    assert tuple(failure) == (1, "unresolved")
    assert service.get(source_a.id).last_sync_status == "failed"
    db.close()


def test_batch_promotes_existing_stale_identity_claim_without_redownload(tmp_path):
    db, service, sources, _playlists, _manual_track = _fixture(tmp_path)
    source_a = sources[0]
    service.set_enabled(sources[1].id, False)
    service.set_enabled(sources[2].id, False)
    missing_path = tmp_path / "missing-canonical.mp3"
    missing_track = db.upsert_track(
        missing_path,
        source_kind="youtube",
        source_video_id=VIDEOS["B"],
    )
    preserved_path = tmp_path / "preserved-claim.mp3"
    preserved_path.write_bytes(b"preserved media")
    preserved_track = db.upsert_track(
        preserved_path,
        source_kind="youtube",
        source_video_id=VIDEOS["B"],
    )
    with db.conn:
        db.conn.execute(
            "UPDATE source_track_identities SET track_id=? "
            "WHERE source_kind='youtube' AND external_track_id=?",
            (missing_track, VIDEOS["B"]),
        )

    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id,
            "Remote A",
            [_item("a1", "B", 0)],
        )
    }
    calls = []
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=lambda config, report: SyntheticSyncer(
            config, report, snapshots, calls
        ),
        importer=_import,
    )
    result = orchestrator.sync_selected([source_a.id])

    assert result.status == "complete"
    assert calls == []
    assert db.canonical_track_id("youtube", VIDEOS["B"]) == preserved_track
    assert db.source_identity_conflict_count() >= 1
    assert preserved_path.read_bytes() == b"preserved media"
    db.close()


def test_batch_restores_cascade_deleted_mapping_from_preserved_claim(tmp_path):
    db, service, sources, _playlists, _manual_track = _fixture(tmp_path)
    source_a = sources[0]
    service.set_enabled(sources[1].id, False)
    service.set_enabled(sources[2].id, False)
    removed_path = tmp_path / "removed-missing-canonical.mp3"
    removed_path.write_bytes(b"soon missing")
    removed_track = db.upsert_track(
        removed_path,
        source_kind="youtube",
        source_video_id=VIDEOS["B"],
    )
    preserved_path = tmp_path / "preserved-after-remove-missing.mp3"
    preserved_path.write_bytes(b"preserved media")
    preserved_track = db.upsert_track(
        preserved_path,
        source_kind="youtube",
        source_video_id=VIDEOS["B"],
    )
    assert db.canonical_track_id("youtube", VIDEOS["B"]) == removed_track
    removed_path.unlink()
    with db.conn:
        db.conn.execute("DELETE FROM tracks WHERE id=?", (removed_track,))
    assert db.canonical_track_id("youtube", VIDEOS["B"]) is None

    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id,
            "Remote A",
            [_item("a1", "B", 0)],
        )
    }
    calls = []
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=lambda config, report: SyntheticSyncer(
            config, report, snapshots, calls
        ),
        importer=_import,
    )
    result = orchestrator.sync_selected([source_a.id])

    assert result.status == "complete"
    assert calls == []
    assert db.canonical_track_id("youtube", VIDEOS["B"]) == preserved_track
    assert preserved_path.read_bytes() == b"preserved media"
    db.close()


def test_present_redacted_unavailable_occurrence_retains_track_and_membership(tmp_path):
    db, service, sources, playlists, manual_track = _fixture(tmp_path)
    source_a = sources[0]
    service.set_enabled(sources[1].id, False)
    service.set_enabled(sources[2].id, False)
    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id,
            "Remote A",
            [_item("a1", "B", 0)],
        )
    }
    calls = []
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=lambda config, report: SyntheticSyncer(
            config, report, snapshots, calls
        ),
        importer=_import,
    )
    orchestrator.sync_selected([source_a.id])
    track_id = db.canonical_track_id("youtube", VIDEOS["B"])
    track = db.get_track(track_id)
    media_path = Path(track["path"])
    before_media = media_path.read_bytes()
    assert [row["id"] for row in db.get_playlist_tracks(playlists[0])] == [
        track_id,
        manual_track,
    ]

    snapshots[source_a.id] = PlaylistSnapshot.completed(
        source_a.external_id,
        "Remote A",
        [_item("a1", None, 0, unavailable=True)],
    )
    result = orchestrator.sync_selected([source_a.id])
    outcome = result.source_outcomes[0]
    persisted = db.conn.execute(
        "SELECT video_id, track_id, availability_status, removed_at, last_error "
        "FROM sync_source_items WHERE source_id=? AND source_item_id='a1'",
        (source_a.id,),
    ).fetchone()

    assert outcome.status == "complete_with_issues"
    assert outcome.removed_occurrence_count == 0
    assert persisted["video_id"] == VIDEOS["B"]
    assert persisted["track_id"] == track_id
    assert persisted["availability_status"] == "unavailable"
    assert persisted["removed_at"] is None
    assert persisted["last_error"] == "Synthetic unavailable item."
    assert [row["id"] for row in db.get_playlist_tracks(playlists[0])] == [
        track_id,
        manual_track,
    ]
    assert media_path.read_bytes() == before_media
    db.close()


def test_destination_change_preserves_old_playlist_and_populates_new_destination(
    tmp_path,
):
    db, service, sources, playlists, manual_track = _fixture(tmp_path)
    source_a, source_b, _source_c = sources
    playlist_a, playlist_b = playlists
    service.archive(source_b.id)
    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id,
            "Remote A",
            [_item("a1", "A", 0), _item("a2", "B", 1)],
        )
    }
    calls = []
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=lambda config, report: SyntheticSyncer(
            config, report, snapshots, calls
        ),
        importer=_import,
    )
    orchestrator.sync_selected([source_a.id])
    source_track_ids = [
        db.canonical_track_id("youtube", VIDEOS[key]) for key in ("A", "B")
    ]
    assert [row["id"] for row in db.get_playlist_tracks(playlist_a)] == [
        *source_track_ids,
        manual_track,
    ]

    changed = service.update_source(
        source_a.id,
        destination_kind="playlist",
        destination_playlist_id=playlist_b,
    )
    assert changed.destination_playlist_id == playlist_b
    assert [row["id"] for row in db.get_playlist_tracks(playlist_a)] == [
        *source_track_ids,
        manual_track,
    ]
    assert db.get_playlist_tracks(playlist_b) == []

    orchestrator.sync_selected([source_a.id])
    assert [row["id"] for row in db.get_playlist_tracks(playlist_a)] == [
        *source_track_ids,
        manual_track,
    ]
    assert [row["id"] for row in db.get_playlist_tracks(playlist_b)] == source_track_ids
    db.close()


def test_persisted_unavailable_failures_refresh_clear_and_repopulate_by_source(
    tmp_path,
):
    db, service, sources, _playlists, _manual_track = _fixture(tmp_path)
    source_a, source_b, _source_c = sources
    snapshots = {
        source_a.id: PlaylistSnapshot.completed(
            source_a.external_id,
            "Remote A",
            [_item("a-redacted", None, 0, unavailable=True)],
        ),
        source_b.id: PlaylistSnapshot.completed(
            source_b.external_id,
            "Remote B",
            [_item("b-video", "B", 0, unavailable=True)],
        ),
    }
    orchestrator = MultiSourceSyncOrchestrator(
        db,
        tmp_path / "downloads",
        archive_file=tmp_path / "archive.txt",
        source_service=service,
        syncer_factory=lambda config, report: SyntheticSyncer(
            config, report, snapshots, []
        ),
        importer=_import,
    )

    first = orchestrator.sync_selected([source_a.id])
    assert first.status == "complete_with_issues"
    assert first.total_failed_items == 1
    assert db.list_sync_failures("unresolved", sync_source_id=source_a.id) == []

    refreshed = SyncSourceService(db)
    assert refreshed.unresolved_failure_count(source_a.id) == 1
    assert refreshed.unresolved_failure_count(source_b.id) == 0
    assert refreshed.unresolved_failure_count() == 1
    source_a_failures = refreshed.list_unresolved_failures(source_a.id)
    assert len(source_a_failures) == 1
    assert source_a_failures[0]["title"] == "Synthetic unavailable"
    assert source_a_failures[0]["reason"] == "Synthetic unavailable item."
    assert source_a_failures[0]["source_item_id"] == "a-redacted"
    assert source_a_failures[0]["video_id"] is None
    assert source_a_failures[0]["failure_origin"] == "source_item"

    second = orchestrator.sync_selected([source_b.id])
    assert second.status == "complete_with_issues"
    assert second.total_failed_items == 1
    # The usable-video issue exists in both persistence paths but is one failure.
    assert len(db.list_sync_failures("unresolved", sync_source_id=source_b.id)) == 1
    assert refreshed.unresolved_failure_count(source_b.id) == 1
    source_b_failures = refreshed.list_unresolved_failures(source_b.id)
    assert len(source_b_failures) == 1
    assert source_b_failures[0]["source_item_id"] == "b-video"
    assert source_b_failures[0]["failure_origin"] == "sync_failure"
    assert refreshed.unresolved_failure_count() == 2

    item_before_clear = tuple(
        db.conn.execute(
            "SELECT availability_status, removed_at, track_id, source_position "
            "FROM sync_source_items WHERE source_id=? AND source_item_id='a-redacted'",
            (source_a.id,),
        ).fetchone()
    )
    runs_before_clear = db.conn.execute(
        "SELECT COUNT(*) FROM sync_source_runs WHERE source_id=?", (source_a.id,)
    ).fetchone()[0]
    refreshed.clear_failure_history(source_a.id)
    assert refreshed.unresolved_failure_count(source_a.id) == 0
    assert refreshed.list_unresolved_failures(source_a.id) == []
    assert refreshed.unresolved_failure_count(source_b.id) == 1
    assert len(refreshed.list_unresolved_failures(source_b.id)) == 1
    assert tuple(
        db.conn.execute(
            "SELECT availability_status, removed_at, track_id, source_position "
            "FROM sync_source_items WHERE source_id=? AND source_item_id='a-redacted'",
            (source_a.id,),
        ).fetchone()
    ) == item_before_clear
    assert db.conn.execute(
        "SELECT COUNT(*) FROM sync_source_runs WHERE source_id=?", (source_a.id,)
    ).fetchone()[0] == runs_before_clear

    repopulated = orchestrator.sync_selected([source_a.id])
    assert repopulated.total_failed_items == 1
    assert refreshed.unresolved_failure_count(source_a.id) == 1
    assert len(refreshed.list_unresolved_failures(source_a.id)) == 1
    assert refreshed.unresolved_failure_count(source_b.id) == 1

    state_before_global_clear = [
        tuple(row)
        for row in db.conn.execute(
            "SELECT source_id, source_item_id, availability_status, removed_at, "
            "track_id, source_position FROM sync_source_items ORDER BY source_id, id"
        )
    ]
    run_count_before_global_clear = db.conn.execute(
        "SELECT COUNT(*) FROM sync_source_runs"
    ).fetchone()[0]
    db.clear_failure_history()
    assert db.unresolved_failure_count() == 0
    assert refreshed.unresolved_failure_count() == 0
    assert refreshed.list_unresolved_failures() == []
    assert db.conn.execute("SELECT COUNT(*) FROM sync_failures").fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM sync_source_items WHERE last_error IS NOT NULL"
    ).fetchone()[0] == 0
    assert [
        tuple(row)
        for row in db.conn.execute(
            "SELECT source_id, source_item_id, availability_status, removed_at, "
            "track_id, source_position FROM sync_source_items ORDER BY source_id, id"
        )
    ] == state_before_global_clear
    assert db.conn.execute("SELECT COUNT(*) FROM sync_source_runs").fetchone()[
        0
    ] == run_count_before_global_clear
    db.close()
