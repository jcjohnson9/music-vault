from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

from music_vault.core.db import MusicVaultDB
from music_vault.metadata.musicbrainz_enricher import MetadataProviderError
from music_vault.metadata.remediation import (
    ApplyEstimate,
    RemediationError,
    RemediationService,
    candidate_review_token,
)
from music_vault.metadata.remediation_schema import (
    PROVIDER_CACHE_TABLE,
    REMEDIATION_ITEMS_TABLE,
    REMEDIATION_JOBS_TABLE,
)
from music_vault.metadata.service import MetadataService
from music_vault.metadata.tag_writer import TagWriteError, inspect_mp3


_SYNTHETIC_MP3_BASE64 = (
    "//sQxAADwAABpAAAACAAADSAAAAETEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFX/+xLE"
    "KYPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFX/+xDEU4PAAAGk"
    "AAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVf/7EsR9A8AAAaQAAAAg"
    "AAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVf/7EMSnA8AAAaQAAAAgAAA0gAAABF"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBV//sSxNCDwAABpAAAACAAADSAAAEVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7"
    "EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
)


def _canonical_title(value: str) -> str:
    return value.replace(" (Official Video)", "")


def _candidate(title: str, duration: float, **changes) -> dict[str, object]:
    values: dict[str, object] = {
        "title": _canonical_title(title),
        "artist": "Synthetic Artist",
        "album": "Provider Album",
        "album_artist": "Synthetic Artist",
        "release_date": "2001-02-03",
        "recording_id": f"recording-{_canonical_title(title).casefold().replace(' ', '-')}",
        "release_id": f"release-{_canonical_title(title).casefold().replace(' ', '-')}",
        "score": 99,
        "duration_seconds": duration,
        "provider": "MusicBrainz",
        "provider_order": 0,
        "release_status": "Official",
    }
    values.update(changes)
    return values


class FakeProvider:
    def __init__(self, responses=None, failures=None):
        self.responses = dict(responses or {})
        self.failures = dict(failures or {})
        self.calls: list[tuple[str, str | None]] = []

    def search(self, title: str, artist: str | None = None):
        self.calls.append((title, artist))
        presentation_title = f"{title} (Official Video)"
        failure_key = title if title in self.failures else presentation_title
        remaining = int(self.failures.get(failure_key, 0))
        if remaining:
            self.failures[failure_key] = remaining - 1
            raise MetadataProviderError("musicbrainz_unavailable?token=synthetic-secret")
        response = self.responses.get(title, self.responses.get(presentation_title))
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        return [_candidate(title, 200.0)]


class NoCoverProvider:
    def fetch(self, _release_id: str):
        return None


@dataclass
class Harness:
    db: MusicVaultDB
    service: RemediationService
    provider: FakeProvider
    reports: Path
    job_backups: Path


@pytest.fixture
def harness_factory(tmp_path):
    opened: list[MusicVaultDB] = []

    def create(*, provider: FakeProvider | None = None) -> Harness:
        root = tmp_path / f"harness-{len(opened)}"
        root.mkdir()
        db = MusicVaultDB(root / "library.sqlite3", backup_dir=root / "db-backups")
        opened.append(db)
        fake = provider or FakeProvider()
        reports = root / "reports"
        job_backups = root / "job-backups"
        service = RemediationService(
            db,
            provider=fake,
            cover_provider=NoCoverProvider(),
            reports_root=reports,
            backups_root=job_backups,
            sleep=lambda _seconds: None,
        )
        return Harness(db, service, fake, reports, job_backups)

    yield create
    for db in opened:
        db.close()


def _add_track(
    harness: Harness,
    title: str,
    *,
    suffix: str = ".synthetic",
    payload: bytes = b"synthetic-media-payload",
    duration: float = 200.0,
    album: str | None = None,
    album_artist: str | None = None,
    release_date: str | None = None,
) -> tuple[int, Path]:
    media = harness.db.db_path.parent / f"track-{title.casefold().replace(' ', '-')}{suffix}"
    media.write_bytes(payload)
    track_id = harness.db.upsert_track(
        media,
        title=title,
        artist="Synthetic Artist",
        album=album,
        album_artist=album_artist,
        release_date=release_date,
        duration_seconds=duration,
    )
    return track_id, media


def _add_mp3(harness: Harness, title: str = "Synthetic Song (Official Video)"):
    media = harness.db.db_path.parent / "synthetic.mp3"
    media.write_bytes(base64.b64decode(_SYNTHETIC_MP3_BASE64))
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TPE1(encoding=3, text=["Synthetic Artist"]))
    tags.save(media, v2_version=3)
    duration = inspect_mp3(media).duration_seconds
    track_id = harness.db.upsert_track(
        media,
        title=title,
        artist="Synthetic Artist",
        album=None,
        album_artist=None,
        duration_seconds=duration,
    )
    return track_id, media, duration


def _item(harness: Harness, job_id: str, track_id: int | None = None) -> dict:
    sql = f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=?"
    values: tuple[object, ...] = (job_id,)
    if track_id is not None:
        sql += " AND track_id=?"
        values = (job_id, track_id)
    row = harness.db.conn.execute(sql, values).fetchone()
    assert row is not None
    return dict(row)


def _field_state(snapshot, name: str):
    state = snapshot.fields[name]
    return (
        state.value,
        state.provenance,
        state.provider_reference,
        state.confidence,
        state.is_manual,
        state.is_locked,
    )


def test_analysis_persists_strict_classes_counts_and_is_non_destructive(
    harness_factory,
):
    titles = {
        "Strict (Official Video)": "high",
        "Review (Official Video)": "review",
        "Ambiguous (Official Video)": "ambiguous",
        "Absent (Official Video)": "none",
        "Locked (Official Video)": "locked",
        "Failure (Official Video)": "failure",
    }
    responses = {
        "Strict (Official Video)": [_candidate("Strict (Official Video)", 200)],
        "Review (Official Video)": [
            _candidate("Review (Official Video)", 200, score=94)
        ],
        "Ambiguous (Official Video)": [
            _candidate("Ambiguous (Official Video)", 200, recording_id="recording-a"),
            _candidate(
                "Ambiguous (Official Video)",
                200,
                recording_id="recording-b",
                release_id="release-b",
                score=97,
                provider_order=1,
            ),
        ],
        "Absent (Official Video)": [],
    }
    provider = FakeProvider(
        responses,
        failures={"Failure (Official Video)": 3},
    )
    harness = harness_factory(provider=provider)
    media_before: dict[int, tuple[bytes, int]] = {}
    track_ids: dict[str, int] = {}
    for title in titles:
        track_id, media = _add_track(harness, title)
        track_ids[title] = track_id
        media_before[track_id] = (media.read_bytes(), media.stat().st_mtime_ns)
    MetadataService(harness.db).apply_manual_patch(
        track_ids["Locked (Official Video)"],
        {"title": "Locked (Official Video)"},
    )
    history_before = harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0]
    snapshots_before = {
        track_id: MetadataService(harness.db).snapshot(track_id)
        for track_id in track_ids.values()
    }

    job = harness.service.create_job()
    summary, metrics = harness.service.analyze(job.id)

    assert summary.status == "ready"
    assert (summary.total, summary.analyzed) == (6, 6)
    assert (
        summary.high_confidence,
        summary.needs_review,
        summary.ambiguous,
        summary.no_match,
        summary.skipped,
        summary.failed,
    ) == (1, 1, 1, 1, 1, 1)
    # Five analyzed queries (including the partially locked item), plus three
    # provider retries for the isolated failure.
    assert metrics.provider_requests == 8
    assert all("Official Video" not in title for title, _artist in harness.provider.calls)
    statuses = {
        str(row["status"])
        for row in harness.db.conn.execute(
            f"SELECT status FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=?", (job.id,)
        )
    }
    assert statuses == {
        "high_confidence",
        "needs_review",
        "ambiguous",
        "no_match",
        "skipped",
        "failed",
    }
    for title, track_id in track_ids.items():
        current = MetadataService(harness.db).snapshot(track_id)
        assert {
            name: _field_state(current, name) for name in current.fields
        } == {
            name: _field_state(snapshots_before[track_id], name)
            for name in snapshots_before[track_id].fields
        }
        path = Path(current.path)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == media_before[track_id]
    assert harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0] == history_before

    high_item = _item(harness, job.id, track_ids["Strict (Official Video)"])
    assert json.loads(high_item["proposed_patch"])
    assessment_evidence = json.loads(high_item["candidate_snapshot"])["assessments"]
    assert assessment_evidence
    assert {
        "match_score",
        "reasons",
        "field_decisions",
    } <= set(assessment_evidence[0])
    for title in (
        "Review (Official Video)",
        "Ambiguous (Official Video)",
        "Absent (Official Video)",
        "Locked (Official Video)",
        "Failure (Official Video)",
    ):
        assert json.loads(_item(harness, job.id, track_ids[title])["proposed_patch"]) == {}
    report_text = (harness.reports / job.id / "items.json").read_text(encoding="utf-8")
    assert "synthetic-secret" not in report_text
    assert "token=<redacted>" in report_text


def test_nonofficial_release_keeps_recording_match_but_blocks_release_metadata(
    harness_factory,
):
    title = "Nonofficial Release (Official Video)"
    candidate = _candidate(
        title,
        200.0,
        release_status="Bootleg",
        artwork_available=True,
    )
    harness = harness_factory(provider=FakeProvider({title: [candidate]}))
    track_id, _media = _add_track(harness, title)

    job = harness.service.create_job()
    analyzed, _metrics = harness.service.analyze(job.id)

    assert analyzed.high_confidence == 1
    item = _item(harness, job.id, track_id)
    proposed = json.loads(item["proposed_patch"])
    candidate_snapshot = json.loads(item["candidate_snapshot"])
    assert set(proposed) <= {"title", "artist"}
    assert item["provider_recording_id"] == candidate["recording_id"]
    assert item["provider_release_id"] is None
    assert item["artwork_candidate"] is None
    assert candidate_snapshot["release_confident"] is False

    summary, estimate = harness.service.apply_high_confidence(
        job.id,
        confirmed=True,
        write_files=False,
    )

    assert summary.applied == 1
    assert estimate.file_writes == 0
    snapshot = MetadataService(harness.db).snapshot(track_id)
    assert snapshot.value("title") == "Nonofficial Release"
    assert snapshot.value("artist") == "Synthetic Artist"
    assert snapshot.value("album") is None
    assert snapshot.value("album_artist") is None
    assert snapshot.value("release_date") is None
    assert snapshot.value("artwork") is None
    assert snapshot.musicbrainz_recording_id == candidate["recording_id"]
    assert snapshot.musicbrainz_release_id is None


def test_conflicting_release_fields_do_not_attach_a_mismatched_release_id(
    harness_factory,
):
    title = "Release Conflict (Official Video)"
    harness = harness_factory()
    track_id, _media = _add_track(
        harness,
        title,
        album="Existing Album",
        album_artist="Existing Album Artist",
        release_date="1999",
    )
    harness.provider.responses[title] = [_candidate(title, 200.0)]
    job = harness.service.create_job()
    analyzed, _metrics = harness.service.analyze(job.id)

    assert analyzed.high_confidence == 1
    item = _item(harness, job.id, track_id)
    assert set(json.loads(item["proposed_patch"])) <= {"title", "artist"}
    assert item["provider_release_id"] is None

    harness.service.apply_high_confidence(job.id, confirmed=True)
    snapshot = MetadataService(harness.db).snapshot(track_id)
    assert snapshot.musicbrainz_recording_id is not None
    assert snapshot.musicbrainz_release_id is None
    assert snapshot.value("album") == "Existing Album"


def test_job_pause_restart_resume_reuse_cancel_and_status_counts(harness_factory):
    harness = harness_factory()
    for index in range(3):
        _add_track(harness, f"Resume {index} (Official Video)")
    created = harness.service.create_job()
    assert harness.service.create_job().id == created.id
    assert harness.service.status(created.id).status == "created"

    paused_once = False

    def pause_after_first(summary):
        nonlocal paused_once
        if summary.analyzed == 1 and not paused_once:
            paused_once = True
            harness.service.pause(created.id)

    paused, _metrics = harness.service.analyze(created.id, progress=pause_after_first)
    assert paused.status == "paused" and paused.analyzed == 1
    assert harness.service.create_job().id == created.id

    restarted_provider = FakeProvider()
    restarted = RemediationService(
        harness.db,
        provider=restarted_provider,
        cover_provider=NoCoverProvider(),
        reports_root=harness.reports,
        backups_root=harness.job_backups,
        sleep=lambda _seconds: None,
    )
    ready, _metrics = restarted.resume(created.id)
    assert ready.status == "ready"
    assert ready.analyzed == ready.total == 3
    assert restarted.status().id == created.id

    cancelled = restarted.create_job(reuse=False)
    assert restarted.cancel(cancelled.id).status == "cancelled"
    with pytest.raises(RemediationError, match="remediation_job_not_resumable"):
        restarted.resume(cancelled.id)


def test_provider_metrics_checkpoint_survives_hard_analysis_interruption(
    harness_factory,
):
    harness = harness_factory()
    _add_track(harness, "Metric One (Official Video)")
    _add_track(harness, "Metric Two (Official Video)")
    job = harness.service.create_job()

    def interrupt_after_first(summary):
        if summary.analyzed == 1:
            raise KeyboardInterrupt("synthetic hard interruption")

    with pytest.raises(KeyboardInterrupt):
        harness.service.analyze(job.id, progress=interrupt_after_first)
    checkpoint = json.loads(
        (harness.reports / job.id / "metrics.json").read_text(encoding="utf-8")
    )
    assert checkpoint["provider_requests"] == 1

    completed, metrics = harness.service.resume(job.id)
    assert completed.status == "ready"
    assert completed.analyzed == 2
    assert metrics.provider_requests == 2


def test_retry_failed_job_reuses_persisted_items_and_sanitizes_errors(harness_factory):
    title = "Retry (Official Video)"
    provider = FakeProvider(failures={title: 3})
    harness = harness_factory(provider=provider)
    _add_track(harness, title)
    job = harness.service.create_job()
    failed, _metrics = harness.service.analyze(job.id)
    assert failed.status == "failed" and failed.failed == 1
    assert provider.calls == [("Retry", "Synthetic Artist")] * 3

    provider.responses[title] = [_candidate(title, 200)]
    retried, metrics = harness.service.retry_failed(job.id)
    assert retried.status == "ready"
    assert retried.high_confidence == 1 and retried.failed == 0
    assert metrics.provider_requests == 4
    assert harness.db.conn.execute(
        f"SELECT COUNT(*) FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=?", (job.id,)
    ).fetchone()[0] == 1


def test_unexpected_provider_exception_counts_only_the_request_attempted(harness_factory):
    title = "Unexpected Provider (Official Video)"
    provider = FakeProvider(responses={title: RuntimeError("synthetic failure")})
    harness = harness_factory(provider=provider)
    _add_track(harness, title)
    job = harness.service.create_job()

    failed, metrics = harness.service.analyze(job.id)

    assert failed.status == "failed"
    assert metrics.provider_requests == 1
    assert len(provider.calls) == 1


def test_provider_cache_hits_expiry_and_normalized_query_identity(harness_factory):
    provider = FakeProvider()
    harness = harness_factory(provider=provider)
    _add_track(harness, "Cache Song (Official Video)")

    first = harness.service.create_job(reuse=False)
    _summary, first_metrics = harness.service.analyze(first.id)
    assert first_metrics.provider_requests == 1 and first_metrics.cache_hits == 0

    second = harness.service.create_job(reuse=False)
    _summary, second_metrics = harness.service.analyze(second.id)
    assert len(provider.calls) == 1
    assert second_metrics.provider_requests == 0 and second_metrics.cache_hits == 1

    harness.db.conn.execute(
        f"UPDATE {PROVIDER_CACHE_TABLE} SET expires_at='2000-01-01T00:00:00Z'"
    )
    harness.db.conn.commit()
    third = harness.service.create_job(reuse=False)
    _summary, third_metrics = harness.service.analyze(third.id)
    assert len(provider.calls) == 2
    assert third_metrics.provider_requests == 1 and third_metrics.cache_hits == 0
    cache_rows = harness.db.conn.execute(
        f"SELECT normalized_query_key, candidate_data FROM {PROVIDER_CACHE_TABLE}"
    ).fetchall()
    assert len(cache_rows) == 1
    assert "Cache Song" in str(cache_rows[0]["candidate_data"])
    assert "Official Video" not in str(cache_rows[0]["normalized_query_key"])


def test_apply_requires_confirmation_space_and_fresh_job(harness_factory, monkeypatch):
    harness = harness_factory()
    _track_id, _media, duration = _add_mp3(harness, "Gate Song (Official Video)")
    harness.provider.responses["Gate Song (Official Video)"] = [
        _candidate("Gate Song (Official Video)", duration)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    with pytest.raises(RemediationError, match="explicit_apply_confirmation_required"):
        harness.service.apply_high_confidence(job.id)

    import music_vault.metadata.remediation as remediation_module

    monkeypatch.setattr(
        remediation_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=0),
    )
    with pytest.raises(RemediationError, match="insufficient_disk_space"):
        harness.service.apply_high_confidence(job.id, confirmed=True, write_files=True)
    assert harness.service.status(job.id).status == "ready"

    MetadataService(harness.db).apply_manual_patch(_track_id, {"title": "User Override"})
    with pytest.raises(RemediationError, match="remediation_job_stale"):
        harness.service.apply_high_confidence(job.id, confirmed=True)


def test_file_change_after_analysis_blocks_apply_as_stale(harness_factory):
    harness = harness_factory()
    _track_id, media = _add_track(harness, "Stale File (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    media.write_bytes(media.read_bytes() + b"changed-after-analysis")
    with pytest.raises(RemediationError, match="remediation_job_stale"):
        harness.service.apply_high_confidence(job.id, confirmed=True)


def test_per_item_stale_lock_is_reviewed_without_overwrite(harness_factory):
    harness = harness_factory()
    track_id, _media = _add_track(harness, "Lock Race (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    MetadataService(harness.db).apply_manual_patch(track_id, {"title": "User Locked"})
    # Simulate a resumed coordinator that has accepted the new aggregate revision;
    # the private per-item snapshot must still prevent this item from applying.
    harness.db.conn.execute(
        f"UPDATE {REMEDIATION_JOBS_TABLE} SET library_revision=? WHERE id=?",
        (harness.service.library_revision(), job.id),
    )
    harness.db.conn.commit()
    summary, _estimate = harness.service.apply_high_confidence(job.id, confirmed=True)
    assert summary.needs_review == 1 and summary.applied == 0
    assert _item(harness, job.id)["apply_error"] == "item_stale_or_locked"
    assert MetadataService(harness.db).snapshot(track_id).value("title") == "User Locked"


def test_per_item_source_identity_change_is_never_overwritten(harness_factory):
    harness = harness_factory()
    track_id, _media = _add_track(harness, "Source Race (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    harness.db.conn.execute(
        "UPDATE tracks SET source_kind='youtube', source_video_id=? WHERE id=?",
        ("abcdefghijk", track_id),
    )
    harness.db.conn.execute(
        f"UPDATE {REMEDIATION_JOBS_TABLE} SET library_revision=? WHERE id=?",
        (harness.service.library_revision(), job.id),
    )
    harness.db.conn.commit()

    summary, _estimate = harness.service.apply_high_confidence(job.id, confirmed=True)
    assert summary.needs_review == 1 and summary.applied == 0
    item = _item(harness, job.id, track_id)
    assert item["apply_error"] == "item_stale_or_locked"
    row = harness.db.get_track(track_id)
    assert row["source_kind"] == "youtube"
    assert row["source_video_id"] == "abcdefghijk"


def test_database_only_apply_is_audited_and_never_writes_media(harness_factory):
    harness = harness_factory()
    track_id, media = _add_track(harness, "Database Only (Official Video)")
    original = media.read_bytes()
    job = harness.service.create_job()
    analyzed, _metrics = harness.service.analyze(job.id)
    assert analyzed.high_confidence == 1
    summary, estimate = harness.service.apply_high_confidence(
        job.id, confirmed=True, write_files=False
    )
    assert summary.status == "complete" and summary.applied == 1
    assert estimate.database_updates == 1 and estimate.file_writes == 0
    assert media.read_bytes() == original
    item = _item(harness, job.id)
    assert item["file_write_status"] == "not_requested"
    assert item["applied_change_group_id"]
    snapshot = MetadataService(harness.db).snapshot(track_id)
    assert snapshot.value("title") == "Database Only"
    assert snapshot.fields["title"].provenance == "musicbrainz_high_confidence"
    history = harness.db.conn.execute(
        "SELECT actor, reason FROM track_metadata_history WHERE track_id=?", (track_id,)
    ).fetchall()
    assert any(tuple(row) == ("remediation", "musicbrainz_high_confidence") for row in history)
    assert harness.service.verify_job(job.id)["ok"] is True


def test_artwork_preparation_issue_keeps_applied_item_truthful(harness_factory):
    title = "Artwork Unavailable (Official Video)"
    candidate = _candidate(title, 200.0, artwork_available=True)
    harness = harness_factory(provider=FakeProvider({title: [candidate]}))
    track_id, _media = _add_track(harness, title)
    job = harness.service.create_job()
    harness.service.analyze(job.id)

    summary, _estimate = harness.service.apply_high_confidence(
        job.id, confirmed=True, write_files=False
    )

    assert summary.status == "complete_with_issues"
    assert summary.applied == 1 and summary.failed == 0
    item = _item(harness, job.id, track_id)
    assert item["status"] == "applied"
    assert item["file_write_status"] == "not_requested"
    assert item["apply_error"] == "artwork_not_available"
    assert item["applied_change_group_id"]
    assert "artwork" not in json.loads(item["proposed_patch"])
    snapshot = MetadataService(harness.db).snapshot(track_id)
    assert snapshot.value("title") == "Artwork Unavailable"
    assert snapshot.value("artwork") is None


def test_mp3_apply_keeps_database_tags_and_audio_payload_consistent(harness_factory):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(harness)
    original_audio = inspect_mp3(media).audio_payload_sha256
    harness.provider.responses["Synthetic Song (Official Video)"] = [
        _candidate("Synthetic Song (Official Video)", duration)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    summary, estimate = harness.service.apply_high_confidence(
        job.id, confirmed=True, write_files=True
    )
    assert summary.status == "complete" and summary.file_written == 1
    assert estimate.file_writes == 1
    item = _item(harness, job.id)
    assert item["file_write_status"] == "verified"
    assert Path(item["backup_file"]).is_file()
    assert inspect_mp3(media).audio_payload_sha256 == original_audio

    tags = ID3(media)
    snapshot = MetadataService(harness.db).snapshot(track_id)
    assert str(tags.getall("TIT2")[0].text[0]) == snapshot.value("title") == "Synthetic Song"
    assert str(tags.getall("TALB")[0].text[0]) == snapshot.value("album") == "Provider Album"
    assert str(tags.getall("TDRC")[0].text[0]) == snapshot.value("release_date")
    descriptions = {str(frame.desc): str(frame.text[0]) for frame in tags.getall("TXXX")}
    assert descriptions["MusicBrainz Track Id"] == snapshot.musicbrainz_recording_id
    assert descriptions["MusicBrainz Album Id"] == snapshot.musicbrainz_release_id
    verification = harness.service.verify_job(job.id)
    assert verification["ok"] is True
    assert verification["checks"]["audio_payloads_preserved"] is True


def test_one_item_apply_failure_isolated_and_error_is_sanitized(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    first_id, _ = _add_track(harness, "Failure One (Official Video)")
    second_id, _ = _add_track(harness, "Success Two (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    original = harness.service.metadata.apply_high_confidence_candidate

    def fail_one(track_id, *args, **kwargs):
        if track_id == first_id:
            raise RuntimeError("synthetic apply failure?token=private-value")
        return original(track_id, *args, **kwargs)

    monkeypatch.setattr(
        harness.service.metadata, "apply_high_confidence_candidate", fail_one
    )
    summary, _estimate = harness.service.apply_high_confidence(job.id, confirmed=True)
    assert summary.status == "complete_with_issues"
    assert summary.failed == 1 and summary.applied == 1
    failed = _item(harness, job.id, first_id)
    succeeded = _item(harness, job.id, second_id)
    assert failed["status"] == "apply_failed"
    assert "private-value" not in failed["apply_error"]
    assert "<redacted>" in failed["apply_error"]
    assert succeeded["status"] == "applied"
    assert MetadataService(harness.db).snapshot(second_id).value("title") == "Success Two"


def test_retry_apply_failures_requires_fresh_consent_and_skips_file_conflicts(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    retry_id, _ = _add_track(harness, "Retry Apply (Official Video)")
    conflict_id, _ = _add_track(harness, "Conflict Apply (Official Video)")
    success_id, _ = _add_track(harness, "Already Applied (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    original_apply = harness.service.metadata.apply_high_confidence_candidate

    def fail_selected(track_id, *args, **kwargs):
        if track_id in {retry_id, conflict_id}:
            raise RuntimeError("synthetic apply failure")
        return original_apply(track_id, *args, **kwargs)

    monkeypatch.setattr(
        harness.service.metadata, "apply_high_confidence_candidate", fail_selected
    )
    first, _estimate = harness.service.apply_high_confidence(job.id, confirmed=True)
    assert first.status == "complete_with_issues"
    manifest_path = harness.reports / job.id / "backup_manifest.json"
    first_database_backup = json.loads(manifest_path.read_text(encoding="utf-8"))[
        "database_backup"
    ]
    with harness.db.conn:
        harness.db.conn.execute(
            f"UPDATE {REMEDIATION_ITEMS_TABLE} SET file_write_status='conflict' WHERE job_id=? AND track_id=?",
            (job.id, conflict_id),
        )
    monkeypatch.setattr(
        harness.service.metadata, "apply_high_confidence_candidate", original_apply
    )

    retried, _metrics = harness.service.retry_failed(job.id)

    assert retried.status == "ready"
    assert _item(harness, job.id, retry_id)["status"] == "high_confidence"
    assert _item(harness, job.id, retry_id)["file_write_status"] == "not_requested"
    assert _item(harness, job.id, retry_id)["apply_error"] is None
    conflict = _item(harness, job.id, conflict_id)
    assert conflict["status"] == "apply_failed"
    assert conflict["file_write_status"] == "conflict"
    assert _item(harness, job.id, success_id)["status"] == "applied"
    assert MetadataService(harness.db).snapshot(retry_id).value("title") == (
        "Retry Apply (Official Video)"
    )
    with pytest.raises(RemediationError, match="explicit_apply_confirmation_required"):
        harness.service.apply_high_confidence(job.id)

    completed, _estimate = harness.service.apply_high_confidence(
        job.id, confirmed=True, write_files=False
    )

    assert completed.status == "complete_with_issues"
    assert _item(harness, job.id, retry_id)["status"] == "applied"
    assert _item(harness, job.id, conflict_id)["status"] == "apply_failed"
    assert MetadataService(harness.db).snapshot(retry_id).value("title") == "Retry Apply"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))[
        "database_backup"
    ] == first_database_backup


def test_interrupted_prepared_file_apply_resumes_without_rewriting_audio(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "Resume Prepared (Official Video)"
    )
    harness.provider.responses["Resume Prepared (Official Video)"] = [
        _candidate("Resume Prepared (Official Video)", duration)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    original_apply = harness.service.metadata.apply_high_confidence_candidate

    def interrupt_after_file(*_args, **_kwargs):
        raise KeyboardInterrupt("synthetic interruption")

    monkeypatch.setattr(
        harness.service.metadata, "apply_high_confidence_candidate", interrupt_after_file
    )
    with pytest.raises(KeyboardInterrupt):
        harness.service.apply_high_confidence(job.id, confirmed=True, write_files=True)
    journal = _item(harness, job.id, track_id)
    assert journal["status"] == "applying"
    assert journal["file_write_status"] == "prepared"
    assert inspect_mp3(media).full_sha256 == journal["updated_file_hash"]
    with pytest.raises(RemediationError, match="remediation_apply_mode_mismatch"):
        harness.service.apply_high_confidence(
            job.id, confirmed=True, write_files=False
        )

    monkeypatch.setattr(
        harness.service.metadata, "apply_high_confidence_candidate", original_apply
    )
    completed, _metrics = harness.service.resume(job.id)
    assert completed.status == "complete"
    resumed = _item(harness, job.id, track_id)
    assert resumed["status"] == "applied"
    assert resumed["file_write_status"] == "verified"
    assert resumed["original_audio_payload_hash"] == resumed["updated_audio_payload_hash"]
    assert harness.service.verify_job(job.id)["ok"] is True


def test_post_replace_restore_failure_is_a_conflict_and_retry_keeps_journal(
    harness_factory,
    monkeypatch,
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "Uncertain Commit (Official Video)"
    )
    harness.provider.responses["Uncertain Commit (Official Video)"] = [
        _candidate("Uncertain Commit (Official Video)", duration)
    ]
    original_bytes = media.read_bytes()
    job = harness.service.create_job()
    harness.service.analyze(job.id)

    def replace_then_raise(prepared, *, backup):
        assert backup.backup_path.is_file()
        prepared.temporary_path.replace(prepared.original_path)
        raise TagWriteError("media_post_replace_restore_failed")

    def restore_still_fails(*_args, **_kwargs):
        raise TagWriteError("restore_failed")

    monkeypatch.setattr(harness.service.tag_writer, "commit", replace_then_raise)
    monkeypatch.setattr(harness.service.tag_writer, "restore", restore_still_fails)

    summary, _estimate = harness.service.apply_high_confidence(
        job.id, confirmed=True, write_files=True
    )
    assert summary.status == "complete_with_issues"
    assert summary.failed == 1
    item = _item(harness, job.id, track_id)
    assert item["status"] == "apply_failed"
    assert item["file_write_status"] == "conflict"
    assert item["backup_file"] and Path(item["backup_file"]).is_file()
    assert item["original_file_hash"]
    assert item["updated_file_hash"]
    assert media.read_bytes() != original_bytes

    verification = harness.service.verify_job(job.id)
    assert verification["ok"] is False
    assert verification["checks"]["no_unresolved_file_write_states"] is False

    journal = {
        name: item[name]
        for name in (
            "file_write_status",
            "backup_file",
            "original_file_hash",
            "original_audio_payload_hash",
            "updated_file_hash",
            "updated_audio_payload_hash",
        )
    }
    retried, _metrics = harness.service.retry_failed(job.id)
    assert retried.status == "failed"
    after_retry = _item(harness, job.id, track_id)
    assert after_retry["status"] == "apply_failed"
    assert {name: after_retry[name] for name in journal} == journal


def test_manual_review_post_replace_failure_is_recovered_to_exact_original(
    harness_factory,
    monkeypatch,
):
    title = "Manual Recovery (Official Video)"
    provider = FakeProvider(
        responses={title: [_candidate(title, 200.0, score=94)]}
    )
    harness = harness_factory(provider=provider)
    track_id, media, _duration = _add_mp3(harness, title)
    original_bytes = media.read_bytes()
    original_title = MetadataService(harness.db).snapshot(track_id).value("title")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    item = _item(harness, job.id, track_id)
    assert item["status"] == "needs_review"

    def replace_then_raise(prepared, *, backup):
        assert backup.backup_path.is_file()
        prepared.temporary_path.replace(prepared.original_path)
        raise TagWriteError("media_post_replace_restore_failed")

    monkeypatch.setattr(harness.service.tag_writer, "commit", replace_then_raise)

    with pytest.raises(RemediationError, match="review_item_apply_failed"):
        harness.service.approve_review_item(
            job.id,
            int(item["id"]),
            {"title"},
            confirmed=True,
            write_files=True,
        )

    failed = _item(harness, job.id, track_id)
    assert failed["status"] == "apply_failed"
    assert failed["file_write_status"] == "restored"
    assert failed["backup_file"] and Path(failed["backup_file"]).is_file()
    assert failed["original_file_hash"]
    assert failed["updated_file_hash"]
    assert media.read_bytes() == original_bytes
    assert MetadataService(harness.db).snapshot(track_id).value("title") == original_title
    verification = harness.service.verify_job(job.id)
    assert verification["ok"] is True
    assert verification["checks"]["no_unresolved_file_write_states"] is True


def test_rollback_restores_exact_mp3_metadata_provenance_ids_and_records_history(
    harness_factory,
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(harness, "Rollback Song (Official Video)")
    harness.provider.responses["Rollback Song (Official Video)"] = [
        _candidate("Rollback Song (Official Video)", duration)
    ]
    metadata = MetadataService(harness.db)
    before = metadata.snapshot(track_id)
    before_fields = {name: _field_state(before, name) for name in before.fields}
    before_bytes = media.read_bytes()
    history_before = harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history WHERE track_id=?", (track_id,)
    ).fetchone()[0]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    harness.service.apply_high_confidence(job.id, confirmed=True, write_files=True)
    assert media.read_bytes() != before_bytes
    with pytest.raises(RemediationError, match="explicit_rollback_confirmation_required"):
        harness.service.rollback(job.id)

    rolled = harness.service.rollback(job.id, confirmed=True)
    assert rolled.status == "rolled_back" and rolled.rolled_back == 1
    assert media.read_bytes() == before_bytes
    after = metadata.snapshot(track_id)
    assert {name: _field_state(after, name) for name in after.fields} == before_fields
    assert after.musicbrainz_recording_id == before.musicbrainz_recording_id
    assert after.musicbrainz_release_id == before.musicbrainz_release_id
    actors = [
        str(row[0])
        for row in harness.db.conn.execute(
            "SELECT actor FROM track_metadata_history WHERE track_id=? ORDER BY id",
            (track_id,),
        )
    ]
    assert "remediation" in actors and "remediation_rollback" in actors
    assert len(actors) > history_before
    item = _item(harness, job.id)
    assert item["status"] == "rolled_back" and item["file_write_status"] == "restored"


def test_rollback_conflicts_preserve_later_metadata_and_media_changes(harness_factory):
    metadata_harness = harness_factory()
    track_id, _media = _add_track(
        metadata_harness, "Conflict Metadata (Official Video)"
    )
    job = metadata_harness.service.create_job()
    metadata_harness.service.analyze(job.id)
    metadata_harness.service.apply_high_confidence(job.id, confirmed=True)
    MetadataService(metadata_harness.db).apply_manual_patch(
        track_id, {"title": "Later User Choice"}
    )
    conflict = metadata_harness.service.rollback(job.id, confirmed=True)
    assert conflict.status == "complete_with_issues"
    assert _item(metadata_harness, job.id)["status"] == "conflict"
    assert MetadataService(metadata_harness.db).snapshot(track_id).value("title") == "Later User Choice"

    independent_harness = harness_factory()
    independent_id, _independent_media = _add_track(
        independent_harness, "Independent Artwork (Official Video)"
    )
    independent_job = independent_harness.service.create_job()
    independent_harness.service.analyze(independent_job.id)
    independent_harness.service.apply_high_confidence(independent_job.id, confirmed=True)
    MetadataService(independent_harness.db).apply_manual_patch(
        independent_id, {"artwork": "synthetic-later-artwork.png"}
    )
    independent_conflict = independent_harness.service.rollback(
        independent_job.id, confirmed=True
    )
    assert independent_conflict.status == "complete_with_issues"
    assert _item(independent_harness, independent_job.id)["status"] == "conflict"

    media_harness = harness_factory()
    _track_id, media, duration = _add_mp3(
        media_harness, "Conflict Media (Official Video)"
    )
    media_harness.provider.responses["Conflict Media (Official Video)"] = [
        _candidate("Conflict Media (Official Video)", duration)
    ]
    media_job = media_harness.service.create_job()
    media_harness.service.analyze(media_job.id)
    media_harness.service.apply_high_confidence(
        media_job.id, confirmed=True, write_files=True
    )
    media.write_bytes(media.read_bytes() + b"later-external-change")
    changed = media.read_bytes()
    media_conflict = media_harness.service.rollback(media_job.id, confirmed=True)
    assert media_conflict.status == "complete_with_issues"
    assert _item(media_harness, media_job.id)["file_write_status"] == "conflict"
    assert media.read_bytes() == changed


def test_rollback_rechecks_exact_metadata_inside_restore_transaction(
    harness_factory,
    monkeypatch,
):
    harness = harness_factory()
    track_id, _media = _add_track(harness, "Rollback Race (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    harness.service.apply_high_confidence(job.id, confirmed=True)

    original_precheck = harness.service._metadata_matches_applied_item
    race_injected = False

    def inject_manual_change_after_precheck(item):
        nonlocal race_injected
        matched = original_precheck(item)
        if matched and not race_injected:
            race_injected = True
            MetadataService(harness.db).apply_manual_patch(
                track_id,
                {"title": "Later User Choice"},
            )
        return matched

    monkeypatch.setattr(
        harness.service,
        "_metadata_matches_applied_item",
        inject_manual_change_after_precheck,
    )

    rolled = harness.service.rollback(job.id, confirmed=True)

    assert race_injected is True
    assert rolled.status == "complete_with_issues"
    item = _item(harness, job.id, track_id)
    assert item["status"] == "conflict"
    assert item["rollback_change_group_id"] is None
    assert "metadata_changed_after_remediation" in str(item["apply_error"])
    current = MetadataService(harness.db).snapshot(track_id)
    assert current.value("title") == "Later User Choice"
    assert current.fields["title"].is_manual is True
    assert current.fields["title"].is_locked is True
    assert all(
        group.actor != "remediation_rollback"
        for group in MetadataService(harness.db).history_groups(track_id)
    )


def test_verify_job_returns_only_aggregate_private_safe_data(harness_factory):
    harness = harness_factory()
    _add_track(harness, "Private Synthetic Title (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    result = harness.service.verify_job(job.id)
    assert result["ok"] is True
    assert set(result) == {"job_id", "status", "ok", "checks", "counts"}
    serialized = json.dumps(result, sort_keys=True)
    assert "Private Synthetic Title" not in serialized
    assert str(harness.db.db_path.parent) not in serialized
    assert "recording-private" not in serialized
    assert result["counts"]["total"] == 1


def test_apply_estimate_and_job_dates_are_structured_and_bounded(harness_factory):
    harness = harness_factory()
    _add_track(harness, "Estimate (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    estimate = harness.service.estimate_apply(job.id)
    assert isinstance(estimate, ApplyEstimate)
    assert estimate.database_updates == 1
    assert estimate.file_writes == 0
    assert all(value >= 0 for value in estimate.aggregate_dict().values())
    row = harness.db.conn.execute(
        f"SELECT created_at, updated_at FROM {REMEDIATION_JOBS_TABLE} WHERE id=?",
        (job.id,),
    ).fetchone()
    for value in row:
        assert datetime.fromisoformat(str(value).replace("Z", "+00:00")).tzinfo == timezone.utc


def test_confirmed_review_file_apply_is_resumable_verifiable_and_rollback_safe(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "Review Resume (Official Video)"
    )
    before_bytes = media.read_bytes()
    harness.provider.responses["Review Resume (Official Video)"] = [
        _candidate("Review Resume (Official Video)", duration, score=94)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    item = _item(harness, job.id, track_id)
    assert item["status"] == "needs_review"
    original_apply = harness.service.metadata.apply_confirmed_candidate

    def interrupt_after_file(*_args, **_kwargs):
        raise KeyboardInterrupt("synthetic review interruption")

    monkeypatch.setattr(
        harness.service.metadata, "apply_confirmed_candidate", interrupt_after_file
    )
    with pytest.raises(KeyboardInterrupt):
        harness.service.approve_review_item(
            job.id,
            int(item["id"]),
            {"title", "album", "release_date"},
            confirmed=True,
            write_files=True,
        )
    journal = _item(harness, job.id, track_id)
    persisted_job = harness.service.status(job.id)
    assert journal["status"] == "applying"
    assert journal["file_write_status"] == "prepared"
    assert json.loads(journal["approved_fields"]) == ["album", "release_date", "title"]
    assert persisted_job.status == "applying"
    assert persisted_job.mode == "review_apply_files"
    assert inspect_mp3(media).full_sha256 == journal["updated_file_hash"]
    harness.db.conn.execute(
        f"UPDATE {REMEDIATION_JOBS_TABLE} SET created_at='2000-01-01T00:00:00Z' WHERE id=?",
        (job.id,),
    )
    harness.db.conn.commit()

    monkeypatch.setattr(
        harness.service.metadata, "apply_confirmed_candidate", original_apply
    )
    completed, _metrics = harness.service.resume(job.id)
    assert completed.status == "complete"
    applied = _item(harness, job.id, track_id)
    assert applied["status"] == "applied"
    assert applied["review_reason"] == "user_confirmed"
    assert applied["file_write_status"] == "verified"
    assert harness.service.verify_job(job.id)["ok"] is True

    rolled = harness.service.rollback(job.id, confirmed=True)
    assert rolled.status == "rolled_back"
    assert media.read_bytes() == before_bytes
    assert harness.service.verify_job(job.id)["ok"] is True


def test_high_confidence_file_write_restores_when_metadata_changes_before_commit(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "High Race (Official Video)"
    )
    before_bytes = media.read_bytes()
    harness.provider.responses["High Race (Official Video)"] = [
        _candidate("High Race (Official Video)", duration)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    original_commit = harness.service.tag_writer.commit

    def commit_then_lock(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        racer = MusicVaultDB(
            harness.db.db_path,
            backup_dir=harness.db.db_path.parent / "race-backups",
        )
        try:
            MetadataService(racer).lock_fields(track_id, {"title"})
        finally:
            racer.close()
        return result

    monkeypatch.setattr(harness.service.tag_writer, "commit", commit_then_lock)
    summary, _estimate = harness.service.apply_high_confidence(
        job.id, confirmed=True, write_files=True
    )
    failed = _item(harness, job.id, track_id)
    assert summary.status == "complete_with_issues"
    assert failed["status"] == "apply_failed"
    assert failed["file_write_status"] == "restored"
    assert media.read_bytes() == before_bytes
    assert MetadataService(harness.db).snapshot(track_id).fields["title"].is_locked is True


def test_confirmed_review_file_write_restores_when_metadata_changes_before_commit(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "Review Race (Official Video)"
    )
    before_bytes = media.read_bytes()
    harness.provider.responses["Review Race (Official Video)"] = [
        _candidate("Review Race (Official Video)", duration, score=94)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    item = _item(harness, job.id, track_id)
    original_commit = harness.service.tag_writer.commit

    def commit_then_lock(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        racer = MusicVaultDB(
            harness.db.db_path,
            backup_dir=harness.db.db_path.parent / "review-race-backups",
        )
        try:
            MetadataService(racer).lock_fields(track_id, {"title"})
        finally:
            racer.close()
        return result

    monkeypatch.setattr(harness.service.tag_writer, "commit", commit_then_lock)
    with pytest.raises(RemediationError, match="review_item_apply_failed"):
        harness.service.approve_review_item(
            job.id,
            int(item["id"]),
            {"title"},
            confirmed=True,
            write_files=True,
        )
    failed = _item(harness, job.id, track_id)
    assert failed["status"] == "apply_failed"
    assert failed["file_write_status"] == "restored"
    assert media.read_bytes() == before_bytes
    assert MetadataService(harness.db).snapshot(track_id).fields["title"].is_locked is True


def test_confirmed_review_rejects_media_changed_after_analysis(harness_factory):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "Review Stale Media (Official Video)"
    )
    harness.provider.responses["Review Stale Media (Official Video)"] = [
        _candidate("Review Stale Media (Official Video)", duration, score=94)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    item = _item(harness, job.id, track_id)
    media.write_bytes(media.read_bytes() + b"synthetic-external-change")
    changed = media.read_bytes()

    with pytest.raises(RemediationError, match="media_changed_after_analysis"):
        harness.service.approve_review_item(
            job.id,
            int(item["id"]),
            {"title"},
            confirmed=True,
            write_files=True,
        )
    assert media.read_bytes() == changed
    assert _item(harness, job.id, track_id)["status"] == "needs_review"


def test_rollback_metadata_race_leaves_applied_media_untouched(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "Rollback Race Media (Official Video)"
    )
    harness.provider.responses["Rollback Race Media (Official Video)"] = [
        _candidate("Rollback Race Media (Official Video)", duration)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    harness.service.apply_high_confidence(job.id, confirmed=True, write_files=True)
    applied_bytes = media.read_bytes()
    original_match = harness.service._metadata_matches_applied_item
    injected = False

    def match_then_commit_manual_change(item):
        nonlocal injected
        matched = original_match(item)
        if matched and not injected:
            injected = True
            racer = MusicVaultDB(
                harness.db.db_path,
                backup_dir=harness.db.db_path.parent / "rollback-race-backups",
            )
            try:
                MetadataService(racer).apply_manual_patch(
                    track_id, {"title": "Later Manual Choice"}
                )
            finally:
                racer.close()
        return matched

    monkeypatch.setattr(
        harness.service, "_metadata_matches_applied_item", match_then_commit_manual_change
    )
    result = harness.service.rollback(job.id, confirmed=True)

    assert result.status == "complete_with_issues"
    item = _item(harness, job.id, track_id)
    assert item["status"] == "conflict"
    assert item["file_write_status"] == "verified"
    assert media.read_bytes() == applied_bytes
    assert MetadataService(harness.db).snapshot(track_id).value("title") == "Later Manual Choice"


def test_rollback_database_failure_compensates_media_to_applied_file(
    harness_factory, monkeypatch
):
    harness = harness_factory()
    track_id, media, duration = _add_mp3(
        harness, "Rollback Compensation (Official Video)"
    )
    harness.provider.responses["Rollback Compensation (Official Video)"] = [
        _candidate("Rollback Compensation (Official Video)", duration)
    ]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    harness.service.apply_high_confidence(job.id, confirmed=True, write_files=True)
    applied_bytes = media.read_bytes()

    def fail_database_restore(*_args, **_kwargs):
        raise RuntimeError("synthetic rollback database failure")

    monkeypatch.setattr(
        harness.service.metadata,
        "restore_remediation_snapshot",
        fail_database_restore,
    )
    result = harness.service.rollback(job.id, confirmed=True)

    assert result.status == "complete_with_issues"
    item = _item(harness, job.id, track_id)
    assert item["status"] == "conflict"
    assert item["file_write_status"] == "verified"
    assert media.read_bytes() == applied_bytes
    assert MetadataService(harness.db).snapshot(track_id).value("title") == "Rollback Compensation"


def test_verify_job_rejects_stale_backup_manifest_and_report_counts(harness_factory):
    harness = harness_factory()
    _track_id, _media = _add_track(harness, "Manifest Truth (Official Video)")
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    harness.service.apply_high_confidence(job.id, confirmed=True)
    assert harness.service.verify_job(job.id)["ok"] is True

    manifest_path = harness.reports / job.id / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = harness.service.verify_job(job.id)
    assert result["ok"] is False
    assert result["checks"]["backup_manifest_reconciles"] is False

    harness.service._write_apply_manifests(
        job.id, harness.service._existing_database_backup(job.id)
    )
    summary_path = harness.reports / job.id / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["applied"] = 0
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    result = harness.service.verify_job(job.id)
    assert result["ok"] is False
    assert result["checks"]["reports_reconcile"] is False


def test_edited_query_retry_is_non_destructive_and_requires_manual_review(
    harness_factory,
):
    original_title = "Original Query (Official Video)"
    edited_title = "Edited Query"
    provider = FakeProvider(
        {
            original_title: [_candidate(original_title, 200.0, score=94)],
            edited_title: [
                _candidate(
                    edited_title,
                    200.0,
                    artist="Synthetic Artist",
                )
            ],
        }
    )
    harness = harness_factory(provider=provider)
    track_id, media = _add_track(harness, original_title)
    original_bytes = media.read_bytes()
    history_before = harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    item = _item(harness, job.id, track_id)

    summary, _metrics = harness.service.retry_item_with_query(
        job.id, int(item["id"]), edited_title, "Synthetic Artist"
    )

    assert summary.status == "ready"
    retried = _item(harness, job.id, track_id)
    assert retried["status"] == "needs_review"
    assert json.loads(retried["proposed_patch"]) == {}
    assert "edited_query_requires_confirmation" in json.loads(
        retried["match_reasons"]
    )
    assert MetadataService(harness.db).snapshot(track_id).value("title") == original_title
    assert media.read_bytes() == original_bytes
    assert harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0] == history_before


def test_review_decisions_persist_distinct_audit_reasons_without_metadata_changes(
    harness_factory,
):
    harness = harness_factory()
    track_ids = [
        _add_track(harness, f"Review Decision {index} (Official Video)")[0]
        for index in range(3)
    ]
    history_before = harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0]
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    item_ids = [int(_item(harness, job.id, track_id)["id"]) for track_id in track_ids]

    harness.service.reject_candidates(job.id, [item_ids[0]])
    harness.service.skip_items(job.id, [item_ids[1]])
    harness.service.keep_current_items(job.id, [item_ids[2]])

    decisions = harness.db.conn.execute(
        f"""
        SELECT status, confidence_class, review_reason
        FROM {REMEDIATION_ITEMS_TABLE}
        WHERE job_id=? ORDER BY track_id
        """,
        (job.id,),
    ).fetchall()
    assert [tuple(row) for row in decisions] == [
        ("skipped", "skipped", "user_rejected_candidate"),
        ("skipped", "skipped", "user_skipped"),
        ("skipped", "skipped", "user_kept_current"),
    ]
    assert harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history"
    ).fetchone()[0] == history_before


def test_manual_approval_is_bound_to_exact_reviewed_candidate(harness_factory):
    title = "Candidate Token Review (Official Video)"
    provider = FakeProvider({title: [_candidate(title, 200.0, score=94)]})
    harness = harness_factory(provider=provider)
    track_id, _media = _add_track(harness, title)
    job = harness.service.create_job()
    harness.service.analyze(job.id)
    item = _item(harness, job.id, track_id)
    candidate = json.loads(item["candidate_snapshot"])

    with pytest.raises(RemediationError, match="review_candidate_changed"):
        harness.service.approve_review_item(
            job.id,
            int(item["id"]),
            ["title"],
            confirmed=True,
            write_files=False,
            expected_candidate_token="stale-candidate-token",
        )

    unchanged = _item(harness, job.id, track_id)
    assert unchanged["status"] == "needs_review"
    applied = harness.service.approve_review_item(
        job.id,
        int(item["id"]),
        ["title"],
        confirmed=True,
        write_files=False,
        expected_candidate_token=candidate_review_token(candidate),
    )
    assert applied.applied == 1


def test_candidate_artwork_preview_cannot_cross_candidate_identity(
    harness_factory,
    tmp_path,
    monkeypatch,
):
    harness = harness_factory()
    artwork_root = tmp_path / "cover_art_archive"
    artwork_root.mkdir()
    stale_preview = artwork_root / "stale-preview.png"
    stale_preview.write_bytes(b"synthetic-stale-preview")
    monkeypatch.setattr(
        "music_vault.metadata.remediation.cover_art_archive_dir",
        lambda: artwork_root,
    )
    prior_candidate = {
        "title": "Prior Candidate",
        "recording_id": "prior-recording",
        "release_id": "prior-release",
    }
    current_candidate = {
        "title": "Current Candidate",
        "recording_id": "current-recording",
        "release_id": "current-release",
    }
    item = {
        "candidate_snapshot": current_candidate,
        "artwork_candidate": json.dumps(
            {
                "release_id": "current-release",
                "preview_path": str(stale_preview),
                "candidate_token": candidate_review_token(prior_candidate),
            }
        ),
    }

    path, issue = harness.service._prepare_candidate_artwork(
        item,
        {"fields": {"artwork": {"is_locked": False}}},
    )

    assert path is None
    assert issue == "artwork_not_available"
