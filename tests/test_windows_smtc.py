from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from enum import IntEnum
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread

from music_vault.core.transport_actions import TransportSnapshot
from music_vault.platform import windows_smtc as smtc


def wait_until(qapp, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert predicate()


class FakeSession:
    def __init__(self, hwnd, callback):
        self.hwnd = hwnd
        self.callback = callback
        self.created_thread = threading.get_ident()
        self.publish_threads = []
        self.published = []
        self.closed_thread = None

    def publish(self, snapshot, is_current):
        self.publish_threads.append(threading.get_ident())
        self.published.append(snapshot)

    def close(self):
        self.closed_thread = threading.get_ident()


def make_backend(factory=None):
    sessions = []

    def create(hwnd, callback):
        session = (factory or FakeSession)(hwnd, callback)
        sessions.append(session)
        return session

    backend = smtc.SmtcBackend(session_factory=create)
    events = []
    backend.availability.connect(lambda *args: events.append(args))
    return backend, sessions, events


def test_lazy_construction_and_invalid_hwnd_do_not_create_native_session(qapp):
    backend, sessions, events = make_backend()
    assert sessions == []
    backend.start(0, 4)
    assert sessions == []
    assert events == [(False, 4, "unavailable")]
    assert backend.close()


def test_worker_owns_session_and_dispatches_only_supported_commands_on_gui(qapp):
    backend, sessions, events = make_backend()
    commands = []
    backend.command.connect(lambda action, generation: commands.append(
        (action, generation, QThread.currentThread() == qapp.thread())
    ))
    try:
        backend.start(42, 7)
        backend.start(99, 8)  # One window/generation per backend.
        wait_until(qapp, lambda: bool(events))
        assert events == [(True, 7, "ready")]
        snapshot = TransportSnapshot(revision=1, loaded=True, title="Synthetic")
        backend.publish(snapshot)
        wait_until(qapp, lambda: bool(sessions[0].published))
        callback_thread = threading.Thread(target=lambda: [
            sessions[0].callback(action)
            for action in ("play", "pause", "next", "previous", "invalid")
        ])
        callback_thread.start()
        callback_thread.join(1)
        wait_until(qapp, lambda: len(commands) == 4)
        assert commands == [(action, 7, True) for action in ("play", "pause", "next", "previous")]
        assert len(sessions) == 1
        assert sessions[0].hwnd == 42
        assert sessions[0].published == [snapshot]
        assert sessions[0].created_thread != threading.get_ident()
        assert sessions[0].publish_threads == [sessions[0].created_thread]
    finally:
        assert backend.close()
    assert sessions[0].closed_thread == sessions[0].created_thread
    sessions[0].callback("next")
    qapp.processEvents()
    assert len(commands) == 4


def test_coalesces_latest_snapshot_but_accepts_same_revision_state(qapp):
    gate = threading.Event()

    class DelayedSession(FakeSession):
        def __init__(self, hwnd, callback):
            super().__init__(hwnd, callback)
            assert gate.wait(2)

    backend, sessions, events = make_backend(DelayedSession)
    try:
        backend.start(42, 3)
        for revision in range(1, 101):
            backend.publish(TransportSnapshot(revision=revision, title=f"Synthetic {revision}"))
        backend.publish(TransportSnapshot(revision=99, title="Stale"))
        gate.set()
        wait_until(qapp, lambda: bool(sessions) and bool(sessions[0].published))
        assert [state.revision for state in sessions[0].published] == [100]
        same_revision = TransportSnapshot(revision=100, state="paused", position_ms=1200)
        backend.publish(same_revision)
        wait_until(qapp, lambda: len(sessions[0].published) == 2)
        assert sessions[0].published[-1] == same_revision
    finally:
        gate.set()
        assert backend.close()


def test_close_drops_pending_snapshot_and_queued_command(qapp):
    backend, sessions, events = make_backend()
    commands = []
    backend.command.connect(lambda *args: commands.append(args))
    backend.start(42, 5)
    wait_until(qapp, lambda: events == [(True, 5, "ready")])
    # The worker signal is queued; close must suppress it before GUI delivery.
    sender = threading.Thread(target=lambda: sessions[0].callback("next"))
    sender.start()
    sender.join(1)
    assert backend.close()
    backend.publish(TransportSnapshot(revision=4))
    backend.start(99, 6)
    qapp.processEvents()
    assert commands == []
    assert sessions[0].published == []
    assert len(sessions) == 1


def test_constructor_failure_is_bounded_and_sanitized(qapp):
    def fail(_hwnd, _callback):
        raise OSError("sensitive exception text must never be emitted")

    backend = smtc.SmtcBackend(session_factory=fail)
    events = []
    backend.availability.connect(lambda *args: events.append(args))
    backend.start(42, 2)
    wait_until(qapp, lambda: bool(events))
    assert events == [(False, 2, "unavailable")]
    assert backend.close()


def test_publish_failure_disposes_session_and_blocks_later_commands(qapp):
    class BrokenSession(FakeSession):
        def publish(self, snapshot, is_current):
            raise OSError("private details")

    backend, sessions, events = make_backend(BrokenSession)
    commands = []
    backend.command.connect(lambda *args: commands.append(args))
    try:
        backend.start(42, 2)
        wait_until(qapp, lambda: bool(events))
        backend.publish(TransportSnapshot(revision=1))
        wait_until(qapp, lambda: (False, 2, "publish_failed") in events)
        wait_until(qapp, lambda: sessions[0].closed_thread is not None)
        sessions[0].callback("play")
        qapp.processEvents()
        assert commands == []
    finally:
        assert backend.close()


def test_close_timeout_retains_worker_without_termination_then_can_join(qapp, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class BusySession(FakeSession):
        def publish(self, snapshot, is_current):
            entered.set()
            assert release.wait(3)
            super().publish(snapshot, is_current)

    backend, sessions, events = make_backend(BusySession)
    monkeypatch.setattr(smtc, "CLOSE_WAIT_MS", 10)
    try:
        backend.start(42, 1)
        backend.publish(TransportSnapshot(revision=1))
        assert entered.wait(2)
        assert backend.close() is False
        assert backend._worker in smtc._RETIRED_WORKERS
        assert backend._worker.isRunning()
        assert events[-1] == (False, 1, "shutdown_pending")
    finally:
        release.set()
        monkeypatch.setattr(smtc, "CLOSE_WAIT_MS", 5000)
        assert backend.close()
    assert backend._worker not in smtc._RETIRED_WORKERS
    assert sessions[0].closed_thread == sessions[0].created_thread


class Status(IntEnum):
    CLOSED = 0
    STOPPED = 1
    PLAYING = 2
    PAUSED = 3


class Updater:
    def __init__(self):
        self.music_properties = SimpleNamespace()
        self.thumbnail = None
        self.updates = 0
        self.clears = 0

    def update(self):
        self.updates += 1

    def clear_all(self):
        self.clears += 1
        self.type = 0
        self.music_properties = SimpleNamespace()
        self.thumbnail = None


def native_without_winrt():
    native = smtc._NativeSession.__new__(smtc._NativeSession)
    updater = Updater()
    timeline_updates = []
    removed = []
    session = SimpleNamespace(
        update_timeline_properties=timeline_updates.append,
        remove_button_pressed=removed.append,
    )
    native._session = session
    native._updater = updater
    native._status = Status
    native._type = SimpleNamespace(MUSIC=1)
    native._timeline = SimpleNamespace
    native._metadata = native._stream = native._reference = None
    native._initialized = True
    uninitialized = []
    native._runtime = SimpleNamespace(uninit_apartment=lambda: uninitialized.append(True))
    native._token = 12
    native._callback = lambda: None
    return native, session, updater, timeline_updates, removed, uninitialized


def test_native_state_changes_same_revision_and_clear_on_unloaded():
    native, session, updater, timelines, removed, uninitialized = native_without_winrt()
    try:
        playing = TransportSnapshot(
            revision=1, track_id=12, loaded=True, title="Synthetic title",
            artist="Synthetic artist", album="Synthetic album", album_artist="Synthetic",
            state="playing", duration_ms=9000, position_ms=1000, can_next=True,
        )
        native.publish(playing)
        assert updater.music_properties.title == "Synthetic title"
        assert session.is_enabled and session.is_next_enabled
        assert not session.is_previous_enabled
        assert session.playback_status == Status.PLAYING
        assert updater.updates == 1
        native.publish(replace(playing, state="paused", position_ms=19000, can_next=False))
        assert updater.updates == 1  # No repeated metadata/artwork publication.
        assert session.playback_status == Status.PAUSED
        assert timelines[-1].position == timedelta(seconds=9)
        assert not session.is_next_enabled
        native.publish(replace(playing, state="stopped", position_ms=-10))
        assert session.playback_status == Status.STOPPED
        assert timelines[-1].position == timedelta(0)
        native.publish(TransportSnapshot(revision=2))
        assert session.playback_status == Status.CLOSED
        assert not session.is_enabled
        assert not session.is_play_enabled
        assert updater.clears == 1
        assert not hasattr(updater.music_properties, "title")
    finally:
        native.close()
    assert removed == [12]
    assert uninitialized == [True]
    assert native._session is None
    assert native._callback is None
    native.close()  # Idempotent.
    assert uninitialized == [True]


def test_native_cleanup_attempts_every_resource_after_failure():
    native, session, updater, timelines, removed, uninitialized = native_without_winrt()

    def fail(_token):
        raise OSError("sensitive cleanup details")

    session.remove_button_pressed = fail
    stream_closed = []
    native._stream = SimpleNamespace(close=lambda: stream_closed.append(True))
    with pytest.raises(RuntimeError, match="^cleanup_failed$"):
        native.close()
    assert not session.is_enabled
    assert session.playback_status == Status.CLOSED
    assert updater.clears == 1 and updater.updates == 1
    assert stream_closed == [True]
    assert uninitialized == [True]
    assert native._session is None


def install_artwork_fakes(native, *, complete=True, during_wait=None):
    events = []

    class Resource:
        def close(self):
            events.append(type(self).__name__ + ".close")

    class Stream(Resource):
        def seek(self, position):
            events.append(("seek", position))

    class Operation(Resource):
        def wait(self, timeout):
            events.append(("wait", timeout))
            if during_wait is not None:
                during_wait()
            return "completed" if complete else "started"

        def get_results(self):
            return len(payload)

        def cancel(self):
            events.append("cancel")

    class Writer(Resource):
        def __init__(self, stream):
            events.append("writer_created")

        def write_bytes(self, value):
            nonlocal payload
            payload = value

        def store_async(self):
            return Operation()

        def detach_stream(self):
            events.append("detach")

    payload = b""
    native._stream_type = Stream
    native._writer_type = Writer
    native._reference_type = SimpleNamespace(create_from_stream=lambda stream: ("memory", stream))
    native._async_status = SimpleNamespace(COMPLETED="completed")
    return events


def test_native_artwork_is_in_memory_bounded_and_cleared_on_next_track():
    native, session, updater, *_ = native_without_winrt()
    events = install_artwork_fakes(native)
    png = smtc._PNG_SIGNATURE + b"synthetic"
    try:
        native.publish(TransportSnapshot(revision=1, loaded=True, artwork=png))
        assert updater.thumbnail[0] == "memory"
        assert ("wait", 2.0) in events
        assert "Operation.close" in events and "Writer.close" in events
        assert "Stream.close" not in events
        native.publish(TransportSnapshot(revision=2, loaded=True, artwork=b""))
        assert updater.thumbnail is None
        assert events.count("Stream.close") == 1
        native.publish(TransportSnapshot(revision=3, loaded=True, artwork=png * 100000))
        assert updater.thumbnail is None
        native.publish(TransportSnapshot(revision=4, loaded=True, artwork=b"not png"))
        assert updater.thumbnail is None
        assert events.count("writer_created") == 1
    finally:
        native.close()


def test_artwork_timeout_cancels_and_closes_every_created_resource():
    native, *_ = native_without_winrt()
    events = install_artwork_fakes(native, complete=False)
    try:
        with pytest.raises(TimeoutError, match="artwork_timeout"):
            native.publish(TransportSnapshot(revision=1, loaded=True, artwork=smtc._PNG_SIGNATURE))
        assert "cancel" in events
        assert "Operation.close" in events
        assert "Writer.close" in events
        assert "Stream.close" in events
    finally:
        native.close()


def test_slow_artwork_does_not_commit_superseded_track(qapp):
    entered, release = threading.Event(), threading.Event()
    commits = []
    sessions = []

    def factory(hwnd, callback):
        native, session, updater, *_ = native_without_winrt()
        original_update = updater.update

        def update():
            commits.append(getattr(updater.music_properties, "title", None))
            original_update()

        def delay():
            entered.set()
            assert release.wait(2)

        updater.update = update
        events = install_artwork_fakes(native, during_wait=delay)
        sessions.append((native, events))
        return native

    backend = smtc.SmtcBackend(session_factory=factory)
    try:
        backend.start(42, 3)
        backend.publish(TransportSnapshot(
            revision=1, loaded=True, title="Obsolete synthetic A", artwork=smtc._PNG_SIGNATURE,
        ))
        assert entered.wait(2)
        backend.publish(TransportSnapshot(revision=2, loaded=True, title="Latest synthetic B"))
        release.set()
        wait_until(qapp, lambda: bool(commits))
        assert commits == ["Latest synthetic B"]
        assert sessions[0][1].count("Stream.close") == 1
    finally:
        release.set()
        assert backend.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failure_availability_waits_for_disposal(qapp, cleanup_fails):
    cleanup_started, allow_cleanup = threading.Event(), threading.Event()

    class FailingSession(FakeSession):
        def publish(self, snapshot, is_current):
            raise OSError("synthetic publication failure")

        def close(self):
            cleanup_started.set()
            assert allow_cleanup.wait(2)
            super().close()
            if cleanup_fails:
                raise RuntimeError("synthetic disposal failure")

    backend, sessions, events = make_backend(FailingSession)
    try:
        backend.start(42, 1)
        wait_until(qapp, lambda: events == [(True, 1, "ready")])
        backend.publish(TransportSnapshot(revision=1))
        assert cleanup_started.wait(2)
        qapp.processEvents()
        assert events == [(True, 1, "ready")]
        allow_cleanup.set()
        expected = "cleanup_failed" if cleanup_fails else "publish_failed"
        wait_until(qapp, lambda: (False, 1, expected) in events)
        assert sessions[0].closed_thread is not None
        assert not any(code == "publish_failed" for _, _, code in events) if cleanup_fails else True
    finally:
        allow_cleanup.set()
        assert backend.close()
    assert events[-1][2] == ("cleanup_failed" if cleanup_fails else "closed")
