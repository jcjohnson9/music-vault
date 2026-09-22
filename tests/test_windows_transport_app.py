from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent

from music_vault import app
from test_ui_system import isolated_ui_window


class Worker(QObject):
    finished = Signal()

    def isRunning(self):
        return True


def test_ignored_sync_close_keeps_transport_alive(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(app.QMessageBox, "information", lambda *_args: None)
        patch.setattr(window, "sync_center_controller", SimpleNamespace(
            worker=Worker(), stop_after_current=lambda: calls.append("stop_source"),
        ))
        patch.setattr(window.windows_transport, "close", lambda: calls.append("close_native") or True)
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert calls == ["stop_source"]
        assert not window.windows_transport.closing


def test_pending_native_shutdown_defers_other_resources(isolated_ui_window, monkeypatch):
    window = isolated_ui_window.window
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(window.windows_transport, "close", lambda: calls.append("close_native") or False)
        patch.setattr(window.metadata_intelligence_tasks, "close", lambda: calls.append("metadata_close"))
        patch.setattr(app.QTimer, "singleShot", lambda delay, callback: calls.append(("retry", delay)))
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert calls == ["close_native", ("retry", 100)]


def test_offscreen_app_owns_no_native_session_and_accepted_close_stops_commands(isolated_ui_window):
    window = isolated_ui_window.window
    controller = window.windows_transport
    assert not controller.enabled
    assert controller.smtc is None and controller.taskbar is None
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert controller.closing and controller.actions.closed
