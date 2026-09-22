from datetime import datetime, timedelta, timezone

import pytest

from music_vault.core.listening_history import ListeningHistoryObserver


ROW = {"id": 1, "title": "Synthetic track", "artist": "Fixture artist", "album": "Fixture album"}
SOURCE = "file:///synthetic.wav"


class Clock:
    def __init__(self):
        self.value = 0.0
        self.wall_offset = 0

    def monotonic(self):
        return self.value

    def utc(self):
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.value + self.wall_offset)

    def advance(self, milliseconds):
        self.value += milliseconds / 1000


def setup_observer(**options):
    clock, writes, changes, degraded = Clock(), [], [], []

    def sink(record):
        writes.append(record)
        return True

    observer = ListeningHistoryObserver(
        options.pop("sink", sink), monotonic=clock.monotonic, utc_now=clock.utc,
        changed=lambda: changes.append(True), degraded=lambda *state: degraded.append(state), **options,
    )
    observer.prepare_track(ROW, source=SOURCE)
    return observer, clock, writes, changes, degraded


def observe(observer, position, **kwargs):
    values = dict(generation=observer.generation, track_id=1, source=SOURCE,
                  position_ms=position, playing=True, usable=True)
    values.update(kwargs)
    observer.observe(**values)


def progress(observer, clock, *, start=0, count=1, step=1000, **kwargs):
    observe(observer, start, **kwargs)
    for index in range(count):
        clock.advance(step)
        observe(observer, start + (index + 1) * step, **kwargs)


def test_pending_playing_selection_and_failure_without_progress_do_not_record():
    observer, clock, writes, *_ = setup_observer()
    observe(observer, 0)
    clock.advance(1000)
    observe(observer, 0)
    observer.finish("error")
    assert writes == []
    assert observer.current is None


def test_actual_progress_records_private_cumulative_event_without_source_path():
    observer, clock, writes, changes, *_ = setup_observer()
    progress(observer, clock)
    assert len(writes) == len(changes) == 1
    event = writes[0]
    assert event["listened_ms"] == 1000
    assert event["started_at"] and event["last_observed_at"]
    assert event["track_id"] == event["recorded_track_id"] == 1
    assert event["qualified_at"] is None
    assert event["ended_at"] is None
    assert "source" not in event and "path" not in event
    assert SOURCE not in str(event)
    observer.finish("next")
    assert writes[-1]["end_reason"] == "next"
    assert writes[-1]["event_id"] == event["event_id"]
    assert writes[-1]["update_sequence"] == 2


@pytest.mark.parametrize("duration,threshold", [(None, 30000), (10000, 5000), (120000, 30000), (100, 50)])
def test_meaningful_listen_threshold_is_min_half_duration_or_thirty_seconds(duration, threshold):
    observer, clock, writes, *_ = setup_observer()
    step = min(1000, threshold)
    progress(observer, clock, count=threshold // step, step=step, duration_ms=duration)
    assert observer.current.listened_ms == threshold
    assert observer.current.qualified_at is not None
    assert writes[-1]["qualified_at"] == observer.current.qualified_at


def test_pause_resume_and_buffering_keep_one_event_without_credit_for_gap():
    observer, clock, writes, *_ = setup_observer()
    progress(observer, clock)
    event_id = observer.current.event_id
    observe(observer, 1000, playing=False)
    assert len(writes) == 2
    clock.advance(60000)
    observe(observer, 1000)
    clock.advance(1000)
    observe(observer, 2000)
    observe(observer, 2000, usable=False)
    clock.advance(40000)
    observe(observer, 9000, usable=False)
    observe(observer, 9000)
    clock.advance(1000)
    observe(observer, 10000)
    assert observer.current.event_id == event_id
    assert observer.current.listened_ms == 3000


def test_seek_to_end_rewind_jump_and_suspension_do_not_invent_listening():
    observer, clock, writes, *_ = setup_observer()
    progress(observer, clock)
    observer.before_seek()
    clock.advance(1000)
    observe(observer, 119000, duration_ms=120000)
    clock.advance(1000)
    observe(observer, 0)  # Unannounced rewind also just resets the anchor.
    clock.advance(1000)
    observe(observer, 100000)  # Unannounced forward seek.
    clock.advance(10000)
    observe(observer, 110000)  # Suspended/gapped observation.
    observer.finish("end")
    assert writes[-1]["listened_ms"] == 1000
    assert writes[-1]["end_reason"] == "ended"
    assert writes[-1]["qualified_at"] is None


def test_wall_clock_changes_do_not_change_elapsed_credit_and_rate_is_bounded():
    observer, clock, writes, *_ = setup_observer()
    observe(observer, 0)
    clock.wall_offset = 7200
    clock.advance(1000)
    observe(observer, 2000, playback_rate=2)
    assert observer.current.listened_ms == 2000
    clock.wall_offset = -7200
    clock.advance(1000)
    observe(observer, 3500, playback_rate=1)  # Tolerated jitter, wall capped.
    assert observer.current.listened_ms == 3000


def test_stale_generation_cannot_advance_or_reset_new_occurrence_anchor():
    observer, clock, writes, *_ = setup_observer()
    old_generation = observer.generation
    progress(observer, clock)
    observer.prepare_track(ROW, source=SOURCE, origin="manual_queue", reason="next")
    observe(observer, 0)
    clock.advance(1000)
    observe(observer, 100000, generation=old_generation)
    observe(observer, 1000)
    assert observer.current.listened_ms == 1000
    assert writes[-1]["event_id"] != writes[0]["event_id"]
    assert writes[-1]["playback_origin"] == "manual_queue"


@pytest.mark.parametrize("field,value", [("track_id", 2), ("source", "file:///other.wav")])
def test_wrong_identity_does_not_start_history(field, value):
    observer, clock, writes, *_ = setup_observer()
    observe(observer, 0)
    clock.advance(1000)
    observe(observer, 1000, **{field: value})
    assert writes == []
    clock.advance(1000)
    observe(observer, 2000)
    assert writes == []


def test_repeated_occurrences_and_terminal_calls_are_idempotent():
    observer, clock, writes, *_ = setup_observer()
    progress(observer, clock)
    observer.finish("ended")
    observer.finish("ended")
    observer.prepare_track(ROW, source=SOURCE, origin="repeat", reason="ended")
    progress(observer, clock)
    observer.finish("close")
    observer.finish("close")
    assert len(writes) == 4
    assert len({event["event_id"] for event in writes}) == 2
    assert writes[-1]["playback_origin"] == "repeat"
    assert writes[-1]["end_reason"] == "app_closed"


def test_context_only_copies_bounded_display_fields_not_queue_or_provider_data():
    observer, clock, writes, *_ = setup_observer()
    observer.prepare_track(ROW, source=SOURCE, context={
        "kind": "playlist", "playlist_id": 4, "playlist_name": "Synthetic mix",
        "track_ids": [1, 2, 3], "source_url": "https://provider.invalid/private",
    })
    progress(observer, clock)
    assert writes[0]["context_playlist_id"] == 4
    assert writes[0]["context_label"] == "Synthetic mix"
    assert "provider.invalid" not in str(writes)
    assert "track_ids" not in writes[0]


def test_checkpoints_and_qualification_are_coarse_crash_leaves_open_evidence():
    observer, clock, writes, *_ = setup_observer()
    progress(observer, clock, count=40)
    assert [event["listened_ms"] for event in writes] == [1000, 30000]
    assert observer.current.listened_ms == 40000
    assert writes[-1]["ended_at"] is None  # Simulated crash, not a fabricated end.
    assert writes[-1]["qualified_at"] is not None


def test_failed_writes_coalesce_and_do_not_retry_on_position_ticks():
    failures = True
    attempts, saved = [], []

    def sink(record):
        attempts.append(record)
        if failures:
            raise OSError("do not expose private exception details")
        saved.append(record)
        return True

    observer, clock, _, changes, degraded = setup_observer(sink=sink)
    progress(observer, clock, count=100, step=100)
    assert len(attempts) == 1
    assert observer.pending_count == 1
    observer.finish("next")
    assert len(attempts) == 2
    assert observer.pending_count == 1
    assert changes == []
    assert degraded == [(True, "history_write_failed")]
    failures = False
    assert observer.retry_pending()
    assert len(saved) == 1
    assert saved[0]["end_reason"] == "next"
    assert saved[0]["listened_ms"] == 10000
    assert saved[0]["update_sequence"] == 2
    assert degraded[-1] == (False, "")
    assert changes == [True]


def test_retry_capacity_is_bounded_and_loss_remains_honestly_degraded():
    failed = True
    saved = []

    def sink(record):
        if failed:
            raise RuntimeError("synthetic unavailable store")
        saved.append(record)
        return True

    observer, clock, _, _, degraded = setup_observer(sink=sink, retry_capacity=2)
    for index in range(3):
        if index:
            observer.prepare_track(ROW, source=SOURCE)
        progress(observer, clock)
        observer.finish("next")
    assert observer.pending_count == 2
    assert observer.dropped_events == 1
    failed = False
    assert not observer.retry_pending()
    assert len(saved) == 2 and observer.pending_count == 0
    assert observer.is_degraded
    assert degraded[-1] == (True, "history_incomplete")


def test_one_retry_flush_is_bounded_and_store_idempotence_does_not_invalidate():
    failed = True
    attempts = []

    def sink(record):
        attempts.append(record)
        if failed:
            raise RuntimeError("synthetic unavailable store")
        return False  # Already committed; retry is safely acknowledged.

    observer, clock, _, changes, _ = setup_observer(sink=sink)
    for index in range(6):
        if index:
            observer.prepare_track(ROW, source=SOURCE)
        progress(observer, clock)
        observer.finish("next")
    before = len(attempts)
    failed = False
    observer.retry_pending()
    assert len(attempts) - before == 4
    assert observer.pending_count == 2
    assert observer.retry_pending()
    assert changes == []


def test_observer_snapshots_roundtrip_real_store_without_canonical_mutations():
    import sqlite3
    from music_vault.core.listening_schema import create_listening_schema
    from music_vault.core.listening_store import ListeningStore

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE tracks(id INTEGER PRIMARY KEY, title TEXT, artist TEXT, album TEXT);
            CREATE TABLE playlists(id INTEGER PRIMARY KEY);
            INSERT INTO tracks VALUES(1,'Canonical fixture','Fixture artist','Fixture album');
        """)
        create_listening_schema(conn)
        store = ListeningStore(conn)
        observer, clock, *_ = setup_observer(sink=store.save_event)
        progress(observer, clock, count=30)
        observer.finish("stop")
        observer.finish("stop")
        rows = conn.execute("SELECT listened_ms,qualified_at,end_reason,update_sequence FROM listening_events").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 30000 and rows[0][1] is not None
        assert rows[0][2:] == ("stopped", 3)
        assert conn.execute("SELECT * FROM tracks").fetchall() == [
            (1, "Canonical fixture", "Fixture artist", "Fixture album"),
        ]
        assert not observer.is_degraded
    finally:
        conn.close()
