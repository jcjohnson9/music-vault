"""Synthetic-only remediation review/journal integration regressions."""

from __future__ import annotations

import json

import pytest

from music_vault.metadata.materializer import capture_metadata_state, structural_change_fields
from music_vault.metadata.remediation import RemediationError, _snapshot_dict
from music_vault.metadata.remediation_schema import REMEDIATION_ITEMS_TABLE
from music_vault.metadata.service import MetadataService
from test_metadata_remediation import _add_track, _item, harness_factory  # noqa: F401


def _review(harness_factory, *, before_review=None):
    harness = harness_factory()
    track_id, media = _add_track(
        harness, "Initial Synthetic", album="Initial Album", album_artist="Synthetic Artist"
    )
    if before_review is not None:
        before_review(harness, track_id)
    metadata = MetadataService(harness.db)
    baseline = capture_metadata_state(harness.db.conn, track_id)
    job = harness.service.create_job()
    snapshot = _snapshot_dict(
        metadata.snapshot(track_id), dict(harness.db.get_track(track_id)), conn=harness.db.conn
    )
    harness.service._upsert_analysis_item(
        job.id, track_id, status="needs_review", snapshot=snapshot,
        candidate_snapshot={
            "title": "Approved Synthetic", "artist": "Approved Artist",
            "album": "Approved Album", "album_artist": "Approved Artist",
            "recording_id": "synthetic-recording", "release_id": "synthetic-release",
            "release_date": "2001-02-03", "provider": "MusicBrainz",
        },
        recording_id="synthetic-recording", release_id="synthetic-release",
        confidence_score=80,
    )
    harness.service._set_job_status(job.id, "ready")
    harness.service._refresh_counts(job.id)
    return harness, track_id, media, job.id, _item(harness, job.id), baseline


def _approve(case, fields):
    harness, _track_id, _media, job_id, item, _baseline = case
    harness.service.approve_review_item(
        job_id, int(item["id"]), fields, confirmed=True, write_files=False
    )
    return _item(harness, job_id)


@pytest.mark.parametrize("field", ["title", "artist", "album"])
def test_review_job_rollback_restores_complete_graph(harness_factory, field):
    case = _review(harness_factory)
    harness, track_id, media, job_id, _item_before, baseline = case
    media_before = media.read_bytes()
    applied = _approve(case, {field})
    graph_after = capture_metadata_state(harness.db.conn, track_id)
    assert graph_after != baseline
    assert applied["applied_change_group_id"]
    saved = json.loads(applied["applied_snapshot"])
    assert saved["metadata_graph"]["state"] == graph_after
    assert saved["metadata_materialization_id"] == applied["applied_change_group_id"]
    before_values = {row["field_name"]: row["value"] for row in baseline["track_metadata_fields"]}
    after_values = {row["field_name"]: row["value"] for row in graph_after["track_metadata_fields"]}
    assert {name for name in before_values if before_values[name] != after_values[name]} == {field}
    if field == "artist":
        assert "album" in structural_change_fields(baseline, graph_after)
    rolled = harness.service.rollback(job_id, confirmed=True)
    assert rolled.status == "rolled_back"
    assert capture_metadata_state(harness.db.conn, track_id) == baseline
    item = _item(harness, job_id)
    assert item["rollback_change_group_id"] == applied["applied_change_group_id"]
    journal = harness.db.conn.execute(
        "SELECT undone_at FROM metadata_materializations WHERE id=?",
        (applied["applied_change_group_id"],),
    ).fetchone()
    assert journal[0] is not None
    assert harness.service.verify_job(job_id)["ok"] is True
    assert media.read_bytes() == media_before
    assert harness.provider.calls == []


def test_review_rejects_changed_credit_without_changed_scalar_display(harness_factory):
    case = _review(harness_factory)
    harness, track_id, _media, _job_id, _item_before, baseline = case
    assert baseline["track_artist_credits"]
    with harness.db.conn:
        harness.db.conn.execute(
            "UPDATE track_artist_credits SET credited_as=? WHERE track_id=?",
            ("Later Credited Alias", track_id),
        )
    with pytest.raises(RemediationError, match="remediation_item_stale"):
        _approve(case, {"title"})
    assert harness.db.conn.execute("SELECT COUNT(*) FROM metadata_materializations").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["credit", "context"])
def test_rollback_rejects_later_graph_before_media_restore(harness_factory, monkeypatch, change):
    case = _review(harness_factory)
    harness, track_id, _media, job_id, _item_before, _baseline = case
    _approve(case, {"title"})
    with harness.db.conn:
        if change == "credit":
            harness.db.conn.execute(
                "UPDATE track_artist_credits SET credited_as='Later Alias' WHERE track_id=?", (track_id,)
            )
        else:
            harness.db.conn.execute(
                "INSERT INTO track_release_context(track_id,musicbrainz_release_group_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(track_id) DO UPDATE SET musicbrainz_release_group_id=excluded.musicbrainz_release_group_id",
                (track_id, "later-synthetic-family"),
            )
        harness.db.conn.execute(
            f"UPDATE {REMEDIATION_ITEMS_TABLE} SET file_write_status='verified' WHERE job_id=?",
            (job_id,),
        )
    expected = capture_metadata_state(harness.db.conn, track_id)
    monkeypatch.setattr(harness.service.tag_writer, "restore", lambda *_args, **_kwargs: pytest.fail("media restore attempted"))
    import music_vault.metadata.remediation as module
    monkeypatch.setattr(module, "full_file_sha256", lambda *_args: pytest.fail("media opened before graph guard"))
    assert harness.service.rollback(job_id, confirmed=True).status == "complete_with_issues"
    assert _item(harness, job_id)["status"] == "conflict"
    assert capture_metadata_state(harness.db.conn, track_id) == expected


def test_detached_old_album_conflict_is_checked_before_media(harness_factory, monkeypatch):
    case = _review(harness_factory)
    harness, track_id, _media, job_id, _item_before, baseline = case
    _approve(case, {"album"})
    old_album = baseline["canonical_albums"][0]["id"]
    after = capture_metadata_state(harness.db.conn, track_id)
    assert old_album not in {row["id"] for row in after["canonical_albums"]}
    with harness.db.conn:
        harness.db.conn.execute("UPDATE canonical_albums SET title='Later Shared Fact' WHERE id=?", (old_album,))
        harness.db.conn.execute(
            f"UPDATE {REMEDIATION_ITEMS_TABLE} SET file_write_status='verified' WHERE job_id=?", (job_id,)
        )
    import music_vault.metadata.remediation as module
    monkeypatch.setattr(module, "full_file_sha256", lambda *_args: pytest.fail("media opened before detached identity guard"))
    assert harness.service.rollback(job_id, confirmed=True).status == "complete_with_issues"
    assert capture_metadata_state(harness.db.conn, track_id) == after


def test_rollback_file_failure_rolls_back_graph_and_undo_marker(harness_factory, monkeypatch):
    case = _review(harness_factory)
    harness, track_id, _media, job_id, _item_before, baseline = case
    applied = _approve(case, {"album"})
    after = capture_metadata_state(harness.db.conn, track_id)
    with harness.db.conn:
        harness.db.conn.execute(
            f"UPDATE {REMEDIATION_ITEMS_TABLE} SET file_write_status='verified' WHERE job_id=?", (job_id,)
        )
    def failing_file_check(_path):
        assert capture_metadata_state(harness.db.conn, track_id) == baseline
        raise OSError("synthetic file check failure")
    import music_vault.metadata.remediation as module
    monkeypatch.setattr(module, "full_file_sha256", failing_file_check)
    harness.service.rollback(job_id, confirmed=True)
    assert capture_metadata_state(harness.db.conn, track_id) == after
    assert harness.db.conn.execute(
        "SELECT undone_at FROM metadata_materializations WHERE id=?", (applied["applied_change_group_id"],)
    ).fetchone()[0] is None
    assert harness.db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_history WHERE actor='remediation_rollback'"
    ).fetchone()[0] == 0


def test_item_update_failure_rolls_back_complete_restoration(harness_factory):
    case = _review(harness_factory)
    harness, track_id, _media, job_id, _item_before, _baseline = case
    applied = _approve(case, {"artist"})
    after = capture_metadata_state(harness.db.conn, track_id)
    with harness.db.conn:
        harness.db.conn.execute(
            f"CREATE TRIGGER synthetic_rollback_failure BEFORE UPDATE ON {REMEDIATION_ITEMS_TABLE} "
            "WHEN NEW.status='rolled_back' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    assert harness.service.rollback(job_id, confirmed=True).status == "complete_with_issues"
    assert capture_metadata_state(harness.db.conn, track_id) == after
    assert harness.db.conn.execute(
        "SELECT undone_at FROM metadata_materializations WHERE id=?", (applied["applied_change_group_id"],)
    ).fetchone()[0] is None


def test_old_scalar_snapshot_with_actual_journal_uses_complete_undo(harness_factory):
    case = _review(harness_factory)
    harness, track_id, _media, job_id, _item_before, baseline = case
    _approve(case, {"album"})
    item = _item(harness, job_id)
    with harness.db.conn:
        for column in ("current_snapshot", "applied_snapshot"):
            snapshot = json.loads(item[column])
            snapshot.pop("metadata_graph", None)
            snapshot.pop("metadata_materialization_id", None)
            harness.db.conn.execute(
                f"UPDATE {REMEDIATION_ITEMS_TABLE} SET {column}=? WHERE id=?",
                (json.dumps(snapshot), item["id"]),
            )
    assert harness.service.rollback(job_id, confirmed=True).status == "rolled_back"
    assert capture_metadata_state(harness.db.conn, track_id) == baseline
    assert harness.service.verify_job(job_id)["ok"] is True


@pytest.mark.parametrize("damage", ["missing", "undone", "mismatched"])
def test_journal_failure_never_falls_back_to_scalar_restore(harness_factory, monkeypatch, damage):
    case = _review(harness_factory)
    harness, track_id, _media, job_id, _item_before, _baseline = case
    applied = _approve(case, {"artist"})
    with harness.db.conn:
        if damage == "missing":
            harness.db.conn.execute("DELETE FROM metadata_materializations WHERE id=?", (applied["applied_change_group_id"],))
        elif damage == "undone":
            harness.db.conn.execute("BEGIN IMMEDIATE")
            MetadataService(harness.db).undo_last_change(track_id, commit=False)
        else:
            snapshot = json.loads(applied["applied_snapshot"])
            snapshot["metadata_materialization_id"] = "not-this-items-journal"
            harness.db.conn.execute(
                f"UPDATE {REMEDIATION_ITEMS_TABLE} SET applied_snapshot=? WHERE id=?",
                (json.dumps(snapshot), applied["id"]),
            )
    expected = capture_metadata_state(harness.db.conn, track_id)
    monkeypatch.setattr(harness.service.metadata, "restore_remediation_snapshot", lambda *_args, **_kwargs: pytest.fail("scalar fallback attempted"))
    assert harness.service.rollback(job_id, confirmed=True).status == "complete_with_issues"
    assert _item(harness, job_id)["status"] == "conflict"
    assert capture_metadata_state(harness.db.conn, track_id) == expected


def test_rollback_audit_does_not_reoffer_journal_as_scalar_undo(harness_factory):
    prior_groups = []
    def prior_manual(harness, track_id):
        result = MetadataService(harness.db).apply_manual_patch(track_id, {"version_label": "Prior manual version"})
        prior_groups.append(result.change_group_id)
    case = _review(harness_factory, before_review=prior_manual)
    harness, track_id, _media, job_id, _item_before, _baseline = case
    applied = _approve(case, {"album"})
    harness.service.rollback(job_id, confirmed=True)
    item = _item(harness, job_id)
    assert item["rollback_change_group_id"] == applied["applied_change_group_id"]
    assert MetadataService(harness.db).preview_undo(track_id).change_group_id == prior_groups[0]
    assert harness.service.verify_job(job_id)["ok"] is True


def test_verifier_requires_journal_undo_and_truthful_reversal_audit(harness_factory):
    case = _review(harness_factory)
    harness, _track_id, _media, job_id, _item_before, _baseline = case
    applied = _approve(case, {"title"})
    harness.service.rollback(job_id, confirmed=True)
    assert harness.service.verify_job(job_id)["ok"] is True
    with harness.db.conn:
        harness.db.conn.execute(
            "DELETE FROM track_metadata_history WHERE change_group_id=? AND actor='remediation_rollback'",
            (applied["applied_change_group_id"],),
        )
    assert harness.service.verify_job(job_id)["checks"]["rollback_history_present"] is False


def test_final_review_guard_rejects_graph_race_and_leaves_no_journal(harness_factory, monkeypatch):
    case = _review(harness_factory)
    harness, track_id, _media, job_id, _item_before, _baseline = case
    ensure_backup = harness.service._ensure_database_backup
    def change_after_precheck(selected_job):
        backup = ensure_backup(selected_job)
        with harness.db.conn:
            harness.db.conn.execute(
                "UPDATE track_artist_credits SET credited_as='Concurrent Alias' WHERE track_id=?", (track_id,)
            )
        return backup
    monkeypatch.setattr(harness.service, "_ensure_database_backup", change_after_precheck)
    with pytest.raises(RemediationError, match="review_item_apply_failed"):
        _approve(case, {"title"})
    assert _item(harness, job_id)["status"] == "apply_failed"
    assert harness.db.get_track(track_id)["title"] == "Initial Synthetic"
    assert harness.db.conn.execute("SELECT COUNT(*) FROM metadata_materializations").fetchone()[0] == 0
