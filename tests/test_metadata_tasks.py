from __future__ import annotations

import threading

import pytest
from PySide6.QtTest import QTest

from music_vault.ui.metadata_tasks import MetadataTaskRunner, sanitized_provider_error


def test_task_runs_outside_gui_thread_and_delivers(qapp):
    main_thread = threading.get_ident()
    runner = MetadataTaskRunner()
    results = []
    runner.completed.connect(results.append)
    runner.submit("search", lambda _cancel: threading.get_ident())
    for _ in range(100):
        qapp.processEvents()
        if results:
            break
        QTest.qWait(5)
    assert results and results[0].value != main_thread
    assert runner.pending_count == 0
    runner.close()


def test_cancelled_task_is_abandoned_safely(qapp):
    started = threading.Event()
    release = threading.Event()
    runner = MetadataTaskRunner()
    results = []
    runner.completed.connect(results.append)

    def work(cancel):
        started.set()
        release.wait(1)
        return "ignored" if not cancel.is_set() else "cancelled"

    request_id = runner.submit("search", work)
    assert started.wait(1)
    runner.cancel(request_id)
    release.set()
    QTest.qWait(30)
    qapp.processEvents()
    assert results == []
    assert runner.pending_count == 0
    runner.close()


def test_close_prevents_new_tasks_and_sanitizes_errors():
    runner = MetadataTaskRunner()
    runner.close()
    with pytest.raises(RuntimeError):
        runner.submit("search", lambda _cancel: None)
    assert sanitized_provider_error(RuntimeError("C:\\private\\path secret")) == "provider_unavailable"
    assert sanitized_provider_error(RuntimeError("musicbrainz_unavailable")) == "musicbrainz_unavailable"
