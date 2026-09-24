"""Folder discovery is local, read-only, and bound to the right-clicked track."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint

from music_vault.app import MusicVaultWindow
from test_ui_system import isolated_ui_window  # noqa: F401


def harness(path):
    messages = []
    host = SimpleNamespace(
        db=SimpleNamespace(get_track=lambda _id: {"path": path}),
        statusBar=lambda: SimpleNamespace(showMessage=lambda text, _ms: messages.append(text)),
    )
    return host, messages


def test_unicode_spaces_and_shell_characters_are_local_url_only(tmp_path, monkeypatch):
    folder = tmp_path / "Home Trip & café #1"
    folder.mkdir()
    song = folder / "A; $(not a command).synthetic"
    song.write_bytes(b"not audio")
    before = (song.read_bytes(), song.stat().st_mtime_ns)
    host, messages = harness(str(song))
    opened = []
    monkeypatch.setattr("music_vault.app.QDesktopServices.openUrl", lambda url: opened.append(url) or True)
    MusicVaultWindow.show_track_in_folder(host, 7)
    assert len(opened) == 1 and opened[0].isLocalFile()
    assert Path(opened[0].toLocalFile()) == folder
    assert (song.read_bytes(), song.stat().st_mtime_ns) == before
    assert messages == ["Opened song folder."]


@pytest.mark.parametrize("case", ["no-track", "empty", "relative", "directory", "missing-folder", "no-selection"])
def test_invalid_locations_do_not_open_or_create_folders(tmp_path, monkeypatch, case):
    values = {"empty": "", "relative": "relative/song.mp3", "directory": str(tmp_path),
              "missing-folder": str(tmp_path / "absent" / "song.mp3")}
    host, messages = harness(values.get(case, ""))
    if case == "no-track":
        host.db.get_track = lambda _id: None
    monkeypatch.setattr("music_vault.app.QDesktopServices.openUrl", lambda _url: pytest.fail("must not open"))
    MusicVaultWindow.show_track_in_folder(host, None if case == "no-selection" else 7)
    assert not (tmp_path / "absent").exists()
    assert bool(messages) == (case != "no-selection")


def test_missing_file_opens_existing_parent_and_explains(tmp_path, monkeypatch):
    host, messages = harness(str(tmp_path / "missing.mp3"))
    opened = []
    monkeypatch.setattr("music_vault.app.QDesktopServices.openUrl", lambda url: opened.append(url) or True)
    MusicVaultWindow.show_track_in_folder(host, 7)
    assert Path(opened[0].toLocalFile()) == tmp_path
    assert "file is missing" in messages[-1]


@pytest.mark.parametrize("failure", [False, OSError("unavailable"), RuntimeError("unavailable")])
def test_desktop_failure_is_reported_without_crashing(tmp_path, monkeypatch, failure):
    song = tmp_path / "song.synthetic"
    song.touch()
    host, messages = harness(str(song))
    def open_url(_url):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr("music_vault.app.QDesktopServices.openUrl", open_url)
    MusicVaultWindow.show_track_in_folder(host, 7)
    assert messages == ["Could not open this song's folder."]


@pytest.mark.parametrize("view", ["library", "playlist", "album_tracks", "artist_tracks"])
def test_menu_uses_clicked_id_without_touching_playback_or_database(isolated_ui_window, qapp, monkeypatch, view):
    fixture = isolated_ui_window
    window = fixture.window
    window.current_view_kind = view
    window.library_table.selectRow(0)
    index = window.library_table.model().index(1, 0)
    position = window.library_table.visualRect(index).center()
    expected_id = window.library_table.visible_track_ids()[1]
    expected_folder = Path(window.db.get_track(expected_id)["path"]).parent
    before = list(window.db.conn.iterdump())
    queue = list(window.manual_queue)
    current = window.current_track_id
    state = window.player.playbackState()
    opened = []
    monkeypatch.setattr(fixture.app_module.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)

    class Menu:
        def __init__(self, _parent):
            self.actions = {}
        def addAction(self, text):
            action = SimpleNamespace(setIcon=lambda _icon: None, text=text)
            self.actions[text] = action
            return action
        def addSeparator(self):
            pass
        def exec(self, _position):
            # Simulate a selection change while the menu's nested loop is open.
            window.library_table.selectRow(0)
            return self.actions["Show in Folder"]

    captured = []
    original = window.show_track_in_folder
    def reveal(track_id):
        captured.append(track_id)
        original(track_id)
    monkeypatch.setattr(window, "show_track_in_folder", reveal)
    monkeypatch.setattr(fixture.app_module, "QMenu", Menu)
    window.open_song_context_menu(position)
    assert captured == [expected_id]
    assert Path(opened[0].toLocalFile()) == expected_folder
    assert list(window.db.conn.iterdump()) == before
    assert window.manual_queue == queue and window.current_track_id == current
    assert window.player.playbackState() == state


def test_right_click_empty_space_does_not_create_menu(isolated_ui_window, monkeypatch):
    fixture = isolated_ui_window
    monkeypatch.setattr(fixture.app_module, "QMenu", lambda *_: pytest.fail("no track menu"))
    fixture.window.open_song_context_menu(QPoint(0, -10))


@pytest.mark.parametrize("error", [OSError("unavailable"), ValueError("invalid")])
def test_source_folder_display_tolerates_unavailable_root(isolated_ui_window, monkeypatch, error):
    fixture = isolated_ui_window
    def unavailable(*_args):
        raise error
    monkeypatch.setattr(fixture.app_module, "SourceDownloadFolders", unavailable)
    assert fixture.window.saved_source_download_folder(None) == (
        "Unavailable — check the download folder in Settings"
    )
