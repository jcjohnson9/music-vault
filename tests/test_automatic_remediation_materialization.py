"""Synthetic saved automatic decisions use the complete journal boundary."""
import json

import pytest

from music_vault.metadata.materializer import capture_metadata_state
from music_vault.metadata.remediation import RemediationError
from music_vault.metadata.remediation_schema import REMEDIATION_ITEMS_TABLE, REMEDIATION_JOBS_TABLE
from test_metadata_remediation import _add_mp3, _add_track, _candidate, _item, harness_factory  # noqa: F401


def ready(harness_factory, *, media=False):
    harness = harness_factory()
    title = "Automatic Synthetic (Official Video)"
    if media:
        track, path, duration = _add_mp3(harness, title)
        harness.provider.responses[title] = [_candidate(title, duration)]
    else:
        track, path = _add_track(harness, title)
    before = capture_metadata_state(harness.db.conn, track)
    job = harness.service.create_job()
    summary, _ = harness.service.analyze(job.id)
    assert summary.high_confidence == 1
    harness.provider.calls.clear()
    return harness, track, path, job.id, before


def test_automatic_job_has_complete_journal_and_exact_job_rollback(harness_factory):
    h, track, path, job, before = ready(harness_factory)
    media = path.read_bytes()
    summary, _ = h.service.apply_high_confidence(job, confirmed=True)
    assert summary.applied == 1
    item = _item(h, job)
    applied = json.loads(item["applied_snapshot"])
    assert applied["metadata_materialization_id"] == item["applied_change_group_id"]
    assert applied["metadata_graph"]["state"] == capture_metadata_state(h.db.conn, track)
    assert h.service.verify_job(job)["ok"]
    assert h.service.rollback(job, confirmed=True).status == "rolled_back"
    assert capture_metadata_state(h.db.conn, track) == before
    assert h.service.verify_job(job)["ok"]
    assert path.read_bytes() == media and not h.provider.calls


@pytest.mark.parametrize("change", ["credit", "legacy"])
def test_old_or_stale_graph_requires_review_before_art_or_media(harness_factory, monkeypatch, change):
    h, track, path, job, _ = ready(harness_factory)
    with h.db.conn:
        if change == "credit":
            h.db.conn.execute("UPDATE track_artist_credits SET credited_as='New synthetic alias' WHERE track_id=?", (track,))
        else:
            snapshot = json.loads(_item(h, job)["current_snapshot"])
            snapshot.pop("metadata_graph")
            h.db.conn.execute(f"UPDATE {REMEDIATION_ITEMS_TABLE} SET current_snapshot=? WHERE job_id=?", (json.dumps(snapshot), job))
    before = capture_metadata_state(h.db.conn, track)
    monkeypatch.setattr(h.service, "_prepare_candidate_artwork", lambda *_: pytest.fail("artwork attempted"))
    monkeypatch.setattr(h.service.tag_writer, "create_backup", lambda *_a, **_k: pytest.fail("media write attempted"))
    summary, _ = h.service.apply_high_confidence(job, confirmed=True)
    assert summary.needs_review == 1 and summary.applied == 0
    assert capture_metadata_state(h.db.conn, track) == before


@pytest.mark.parametrize("target", ["item", "job"])
def test_late_saved_decision_change_is_not_overwritten(harness_factory, monkeypatch, target):
    h, track, path, job, before = ready(harness_factory)
    def change(*args):
        with h.db.conn:
            if target == "item":
                h.db.conn.execute(f"UPDATE {REMEDIATION_ITEMS_TABLE} SET candidate_snapshot='{{}}' WHERE job_id=?", (job,))
            else:
                h.db.conn.execute(f"UPDATE {REMEDIATION_JOBS_TABLE} SET status='cancelled' WHERE id=?", (job,))
        return None, None
    monkeypatch.setattr(h.service, "_prepare_candidate_artwork", change)
    with pytest.raises(RemediationError, match="remediation_decision_changed"):
        h.service.apply_high_confidence(job, confirmed=True)
    assert capture_metadata_state(h.db.conn, track) == before
    assert not h.db.conn.execute("SELECT 1 FROM metadata_materializations").fetchone()
    if target == "job":
        assert h.service.status(job).status == "cancelled"
    else:
        assert _item(h, job)["candidate_snapshot"] == "{}"
        assert _item(h, job)["status"] == "high_confidence"


def test_job_counter_failure_rolls_back_metadata_and_item(harness_factory, monkeypatch):
    h, track, _path, job, before = ready(harness_factory)
    original = h.service._refresh_counts
    failed = False
    def fail_after_item(identifier):
        nonlocal failed
        if not failed and _item(h, job)["status"] == "applied":
            failed = True
            raise RuntimeError("synthetic counter failure")
        return original(identifier)
    monkeypatch.setattr(h.service, "_refresh_counts", fail_after_item)
    summary, _ = h.service.apply_high_confidence(job, confirmed=True)
    assert failed and summary.applied == 0 and summary.failed == 1
    assert _item(h, job)["status"] == "apply_failed"
    assert capture_metadata_state(h.db.conn, track) == before
    assert not h.db.conn.execute("SELECT 1 FROM metadata_materializations").fetchone()


def test_job_rejects_unrelated_open_transaction_without_committing(harness_factory):
    h, track, _path, job, _before = ready(harness_factory)
    h.db.conn.execute("BEGIN")
    h.db.conn.execute("UPDATE tracks SET title='Caller pending edit' WHERE id=?", (track,))
    before = tuple(h.db.conn.iterdump())
    with pytest.raises(RemediationError, match="remediation_requires_idle_connection"):
        h.service.apply_high_confidence(job, confirmed=True)
    assert h.db.conn.in_transaction and tuple(h.db.conn.iterdump()) == before
    h.db.conn.rollback()


def test_late_graph_conflict_compensates_verified_synthetic_file(harness_factory, monkeypatch):
    h, track, path, job, before = ready(harness_factory, media=True)
    original_bytes = path.read_bytes()
    original_commit = h.service.tag_writer.commit
    def change_graph_after_file(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        with h.db.conn:
            h.db.conn.execute("UPDATE track_artist_credits SET credited_as='Later alias' WHERE track_id=?", (track,))
        return result
    monkeypatch.setattr(h.service.tag_writer, "commit", change_graph_after_file)
    summary, _ = h.service.apply_high_confidence(job, confirmed=True, write_files=True)
    assert summary.applied == 0 and summary.failed == 1
    assert path.read_bytes() == original_bytes
    assert not h.db.conn.execute("SELECT 1 FROM metadata_materializations").fetchone()
    after = capture_metadata_state(h.db.conn, track)
    assert after["track"] == before["track"]
    assert after["track_artist_credits"][0]["credited_as"] == "Later alias"
    assert _item(h, job)["file_write_status"] == "restored"


@pytest.mark.parametrize("change", ["credit", "legacy", "identity", "media_conflict"])
def test_resumed_prepared_rejection_compensates_prior_replacement(harness_factory, monkeypatch, change):
    h, track, path, job, _ = ready(harness_factory, media=True)
    original = path.read_bytes()
    real_commit = h.service.tag_writer.commit
    def crash_after_replace(*args, **kwargs):
        real_commit(*args, **kwargs)
        raise KeyboardInterrupt("synthetic crash")
    monkeypatch.setattr(h.service.tag_writer, "commit", crash_after_replace)
    with pytest.raises(KeyboardInterrupt):
        h.service.apply_high_confidence(job, confirmed=True, write_files=True)
    assert path.read_bytes() != original and _item(h, job)["file_write_status"] == "prepared"
    with h.db.conn:
        if change in {"credit", "media_conflict"}:
            h.db.conn.execute("UPDATE track_artist_credits SET credited_as='Later alias' WHERE track_id=?", (track,))
            if change == "media_conflict":
                from mutagen.id3 import ID3, TIT2
                tags = ID3(path)
                tags.add(TIT2(encoding=3, text=["New external title"]))
                tags.save(path)
        elif change == "legacy":
            snapshot = json.loads(_item(h, job)["current_snapshot"])
            snapshot.pop("metadata_graph")
            h.db.conn.execute(f"UPDATE {REMEDIATION_ITEMS_TABLE} SET current_snapshot=? WHERE job_id=?", (json.dumps(snapshot), job))
        else:
            # A saved incompatible candidate gets the same fail-closed recovery,
            # not permission to accept bytes made for another candidate.
            h.db.conn.execute(f"UPDATE {REMEDIATION_ITEMS_TABLE} SET provider_recording_id='different-id' WHERE job_id=?", (job,))
    monkeypatch.setattr(h.service.tag_writer, "commit", lambda *_a, **_k: pytest.fail("second replacement"))
    external = path.read_bytes()
    summary, _ = h.service.apply_high_confidence(job, confirmed=True, write_files=True)
    if change == "media_conflict":
        assert path.read_bytes() == external
        assert _item(h, job)["file_write_status"] == "conflict"
        assert summary.status == "complete_with_issues"
        with pytest.raises(RemediationError, match="not_clearable"):
            h.service.clear_completed_job(job)
        h.service.cancel(job)
        with pytest.raises(RemediationError, match="unresolved_media_requires_recovery"):
            h.service.clear_completed_job(job)
        assert not h.db.conn.execute("SELECT 1 FROM metadata_materializations").fetchone()
        return
    assert path.read_bytes() == original
    assert _item(h, job)["file_write_status"] == "restored"
    assert not h.db.conn.execute("SELECT 1 FROM metadata_materializations").fetchone()


def test_change_during_tag_preparation_does_not_replace_media(harness_factory, monkeypatch):
    h, track, path, job, _ = ready(harness_factory, media=True)
    original = path.read_bytes()
    real_prepare = h.service.tag_writer.prepare
    def prepare_then_change(*args, **kwargs):
        prepared = real_prepare(*args, **kwargs)
        with h.db.conn:
            h.db.conn.execute("UPDATE track_artist_credits SET credited_as='Later alias' WHERE track_id=?", (track,))
        return prepared
    monkeypatch.setattr(h.service.tag_writer, "prepare", prepare_then_change)
    monkeypatch.setattr(h.service.tag_writer, "commit", lambda *_a, **_k: pytest.fail("stale replacement"))
    summary, _ = h.service.apply_high_confidence(job, confirmed=True, write_files=True)
    assert summary.failed == 1 and summary.applied == 0
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".*.music-vault-*.tmp.mp3"))


def test_final_progress_cancellation_survives_finalization(harness_factory):
    h, _track, _path, job, _ = ready(harness_factory)
    def cancel(_summary):
        h.service.cancel(job)
    with pytest.raises(RemediationError, match="remediation_decision_changed"):
        h.service.apply_high_confidence(job, confirmed=True, progress=cancel)
    assert h.service.status(job).status == "cancelled"
    assert _item(h, job)["status"] == "applied"
