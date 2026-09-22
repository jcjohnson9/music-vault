from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer

from music_vault.ui.listening_history import ListeningHistoryBridge


class FakePlayer(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    sourceChanged = Signal(QUrl)

    def __init__(self):
        super().__init__()
        self.url = QUrl()
        self.position_value = 0
        self.duration_value = 120000
        self.state = QMediaPlayer.PlaybackState.StoppedState
        self.status = QMediaPlayer.MediaStatus.NoMedia

    def source(self): return self.url
    def position(self): return self.position_value
    def duration(self): return self.duration_value
    def playbackState(self): return self.state
    def mediaStatus(self): return self.status
    def playbackRate(self): return 1.0


@pytest.fixture
def setup(qapp, tmp_path):
    host = QObject()
    host.player = FakePlayer()
    host.current_track_id = None
    writes = []
    now = [0.0]

    class Store:
        def save_event(self, record):
            writes.append(record)
            return True

    bridge = ListeningHistoryBridge(
        host, Store(), monotonic=lambda: now[0],
        utc_now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=now[0]),
    )
    row = {"id": 1, "path": str(tmp_path / "synthetic.wav"), "title": "Synthetic track"}

    def start(track=row):
        bridge.prepare_track(track)
        host.current_track_id = track["id"]
        host.player.url = QUrl.fromLocalFile(track["path"])
        host.player.sourceChanged.emit(host.player.url)
        host.player.position_value = 0
        host.player.state = QMediaPlayer.PlaybackState.PlayingState
        host.player.status = QMediaPlayer.MediaStatus.BufferedMedia
        host.player.playbackStateChanged.emit(host.player.state)

    def tick(position, seconds=1):
        now[0] += seconds
        host.player.position_value = position
        host.player.positionChanged.emit(position)

    yield host, bridge, row, writes, start, tick
    bridge.accepted_close()


def test_nested_source_transition_arms_before_signals_but_requires_real_progress(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    assert writes == []
    tick(1000)
    assert len(writes) == 1
    assert writes[0]["track_id"] == 1
    assert writes[0]["listened_ms"] == 1000
    assert bridge.run_id == writes[0]["run_id"]


def test_same_source_nested_no_media_does_not_cancel_fresh_replay(setup, qapp):
    host, bridge, row, writes, start, tick = setup
    start()
    tick(1000)
    old_callback = bridge._connections[0][1]
    old_event = bridge.observer.current.event_id
    bridge.prepare_track(row)
    # A backend may clear its old URL inside setSource before accepting this
    # very same URL. The intermediate NoMedia cannot cancel the armed replay.
    host.player.url = QUrl()
    host.player.status = QMediaPlayer.MediaStatus.NoMedia
    host.player.sourceChanged.emit(host.player.url)
    host.player.mediaStatusChanged.emit(host.player.status)
    host.player.url = QUrl.fromLocalFile(row["path"])
    host.player.position_value = 0
    host.player.status = QMediaPlayer.MediaStatus.BufferedMedia
    host.player.sourceChanged.emit(host.player.url)
    old_callback(0)  # A queued callback from the disconnected old generation.
    qapp.processEvents()
    assert bridge.observer.current is not None
    assert bridge.observer.current.event_id != old_event
    tick(1000)
    assert writes[-1]["listened_ms"] == 1000
    assert writes[-1]["event_id"] != old_event


def test_pending_no_source_is_canceled_after_transition_settles(setup, qapp):
    host, bridge, row, writes, start, tick = setup
    bridge.prepare_track(row)
    host.player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.NoMedia)
    assert bridge.observer.current is not None  # Synchronous setSource may follow.
    qapp.processEvents()
    assert bridge.observer.current is None
    assert writes == []


def test_same_source_repeat_rejects_old_end_emission_after_host_arms_new_generation(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    tick(1000)
    old_generation = bridge.observer.generation
    bridge.finish("end")
    bridge.repeat_current()
    host.player.status = QMediaPlayer.MediaStatus.EndOfMedia
    bridge._media_status(old_generation, host.player.status)
    assert bridge.observer.current is not None
    assert bridge.observer.current.playback_origin == "repeat"
    host.player.status = QMediaPlayer.MediaStatus.BufferedMedia
    tick(0, seconds=0)
    tick(1000)
    assert writes[-1]["playback_origin"] == "repeat"


def test_scoped_intents_restore_after_nested_queue_and_missing_path_exception(setup):
    host, bridge, row, writes, start, tick = setup
    with bridge.intent(reason="next"):
        with bridge.intent(origin="manual_queue"):
            start()
            tick(1000)
    assert writes[0]["playback_origin"] == "manual_queue"
    with pytest.raises(ValueError):
        with bridge.intent(reason="previous", origin="base"):
            raise ValueError("synthetic missing candidate, prepare was not called")
    start()
    tick(1000)
    assert writes[-1]["playback_origin"] == "manual"
    assert writes[-2]["end_reason"] == "replaced"
    assert bridge._intents == []


def test_stopped_signal_checkpoints_without_premature_natural_end_reason(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    tick(1000)
    host.player.state = QMediaPlayer.PlaybackState.StoppedState
    host.player.playbackStateChanged.emit(host.player.state)
    assert writes[-1]["end_reason"] is None
    bridge.finish("end")
    assert writes[-1]["end_reason"] == "ended"


def test_repeat_same_url_creates_new_uuid_before_rewind(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    tick(1000)
    original = writes[0]["event_id"]
    bridge.finish("end")  # Root's handler runs before existing repeat choice.
    bridge.repeat_current({"kind": "playlist", "playlist_id": 4})
    tick(0, seconds=0)
    tick(1000)
    assert writes[-1]["event_id"] != original
    assert writes[-1]["playback_origin"] == "repeat"
    assert writes[-1]["context_playlist_id"] == 4
    assert writes[-2]["end_reason"] == "ended"


def test_seek_paused_buffered_and_old_signal_payloads_never_inflate_progress(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    tick(1000)
    old_generation = bridge.observer.generation
    bridge.before_seek()
    tick(90000)
    assert bridge.observer.current.listened_ms == 1000
    host.player.status = QMediaPlayer.MediaStatus.StalledMedia
    host.player.mediaStatusChanged.emit(host.player.status)
    tick(95000, seconds=5)
    host.player.status = QMediaPlayer.MediaStatus.BufferedMedia
    host.player.mediaStatusChanged.emit(host.player.status)
    tick(96000)
    assert bridge.observer.current.listened_ms == 2000
    start()
    bridge._position(old_generation, 0)
    host.player.positionChanged.emit(118000)  # Payload differs from current getter.
    tick(1000)
    assert bridge.observer.current.listened_ms == 1000


def test_invalid_pending_source_has_no_history_and_started_error_is_final(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    host.player.status = QMediaPlayer.MediaStatus.InvalidMedia
    host.player.mediaStatusChanged.emit(host.player.status)
    assert bridge.observer.current is None
    assert writes == []
    start()
    tick(1000)
    bridge.finish("error")
    assert writes[-1]["end_reason"] == "error"


def test_stale_old_status_generation_does_not_finalize_new_track(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    old = bridge.observer.generation
    tick(1000)
    start()
    host.player.status = QMediaPlayer.MediaStatus.InvalidMedia
    bridge._media_status(old, host.player.status)
    assert bridge.observer.current is not None
    assert bridge.observer.current.started_at is None


def test_browsing_and_ignored_close_do_not_create_or_finish_event_accepted_close_once(setup):
    host, bridge, row, writes, start, tick = setup
    start()
    tick(1000)
    # An ignored host close deliberately does not call accepted_close.
    host.current_view_kind = "other_page"
    tick(2000)
    assert len({event["event_id"] for event in writes}) == 1
    assert all(event["ended_at"] is None for event in writes)
    assert bridge.accepted_close()
    count = len(writes)
    assert writes[-1]["end_reason"] == "app_closed"
    assert writes[-1]["listened_ms"] == 2000
    bridge.accepted_close()
    tick(3000)
    assert len(writes) == count


def test_coarse_changed_signal_fires_only_after_commit_and_degraded_is_local(qapp, tmp_path):
    host = QObject()
    host.player = FakePlayer()
    host.current_track_id = 1
    clock = [0.0]

    class Store:
        def save_event(self, record):
            raise OSError("private details must not leak")

    bridge = ListeningHistoryBridge(host, Store(), monotonic=lambda: clock[0])
    changes, degraded = [], []
    bridge.changed.connect(lambda: changes.append(True))
    bridge.degraded.connect(lambda *state: degraded.append(state))
    try:
        row = {"id": 1, "path": str(tmp_path / "synthetic.wav")}
        bridge.prepare_track(row)
        host.player.url = QUrl.fromLocalFile(row["path"])
        host.player.state = QMediaPlayer.PlaybackState.PlayingState
        host.player.status = QMediaPlayer.MediaStatus.BufferedMedia
        host.player.playbackStateChanged.emit(host.player.state)
        clock[0] = 1
        host.player.position_value = 1000
        host.player.positionChanged.emit(1000)
        assert changes == []
        assert degraded == [(True, "history_write_failed")]
        assert bridge.accepted_close() is False
    finally:
        bridge.accepted_close()
