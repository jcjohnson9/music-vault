from __future__ import annotations

import threading

from PySide6.QtTest import QSignalSpy, QTest

from music_vault.ui.browser_loader import BrowserSummaryLoader


def _wait_for(spy: QSignalSpy, timeout: int = 2_000) -> None:
    if spy.count() == 0:
        assert spy.wait(timeout)


def test_summary_loader_runs_outside_gui_thread_and_delivers_current_result(qapp):
    loader = BrowserSummaryLoader()
    spy = QSignalSpy(loader.loaded)
    gui_thread = threading.get_ident()

    token = loader.request(
        "albums",
        (1, 2, 3),
        lambda: (threading.get_ident(), ("summary",)),
    )
    _wait_for(spy)

    assert token == 1
    assert spy.count() == 1
    arguments = spy.at(0)
    assert arguments[0] == "albums"
    assert arguments[1] == token
    assert arguments[3][0] != gui_thread
    loader.close()


def test_summary_loader_ignores_superseded_result(qapp):
    loader = BrowserSummaryLoader()
    spy = QSignalSpy(loader.loaded)
    release = threading.Event()

    first = loader.request("artists", "old", lambda: (release.wait(1), "old")[1])
    second = loader.request("artists", "new", lambda: "new")
    assert second > first
    _wait_for(spy)
    release.set()
    QTest.qWait(100)

    assert spy.count() == 1
    arguments = spy.at(0)
    assert arguments[1] == second
    assert arguments[3] == "new"
    loader.close()


def test_summary_loader_close_abandons_pending_result(qapp):
    loader = BrowserSummaryLoader()
    spy = QSignalSpy(loader.loaded)
    release = threading.Event()

    loader.request("albums", "revision", lambda: (release.wait(1), "late")[1])
    loader.close()
    release.set()
    QTest.qWait(100)

    assert spy.count() == 0


def test_summary_loader_sanitizes_worker_errors(qapp, tmp_path):
    loader = BrowserSummaryLoader()
    spy = QSignalSpy(loader.failed)
    private_path = tmp_path / "private.sqlite3"

    def fail():
        raise RuntimeError(f"could not open {private_path}")

    loader.request("albums", "revision", fail)
    _wait_for(spy)

    assert spy.count() == 1
    arguments = spy.at(0)
    assert str(private_path) not in arguments[3]
    assert arguments[3] == "The albums browser could not be loaded."
    loader.close()
