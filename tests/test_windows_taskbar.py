"""Synthetic COM/Win32 ABI and ownership tests; no real shell or player."""
from dataclasses import replace
import ctypes as C
import threading

import pytest

from music_vault.core.transport_actions import TransportSnapshot
from music_vault.platform import windows_taskbar as taskbar


class FakeAPI:
    def __init__(self, failure=None, *, com_owned=True):
        self.calls = []
        self.failure = failure
        self.com_owned = com_owned
        self.valid_window = True
        self.counter = 0
        self.last_buttons = ()

    def call(self, name, *args):
        self.calls.append((name, *args))
        if name == self.failure:
            raise taskbar._NativeFailure(name)

    def owns_window(self, hwnd):
        self.call("owns_window", hwnd)
        return self.valid_window

    def register_message(self):
        self.call("register_message")
        return 0xC111

    def begin_com(self):
        self.call("begin_com")
        return self.com_owned

    def end_com(self):
        self.call("end_com")

    def create_taskbar(self):
        self.call("create_taskbar")
        self.counter += 1
        return self.counter

    def initialize_taskbar(self, pointer):
        self.call("initialize_taskbar", pointer)

    def create_icon(self, kind, hwnd):
        self.call("create_icon_" + kind, hwnd)
        return self.counter * 10 + ("previous", "play", "pause", "next").index(kind) + 1

    def capture_buttons(self, buttons):
        self.last_buttons = tuple((b.iId, b.hIcon, b.dwFlags,
                                   "".join(chr(value) for value in b.szTip if value), b.dwMask)
                                  for b in buttons)

    def add_buttons(self, pointer, hwnd, buttons):
        self.call("add_buttons", pointer, hwnd)
        self.capture_buttons(buttons)

    def update_buttons(self, pointer, hwnd, buttons):
        self.call("update_buttons", pointer, hwnd)
        self.capture_buttons(buttons)

    def release(self, pointer):
        self.call("release", pointer)

    def destroy_icon(self, handle):
        self.call("destroy_icon", handle)


def active_snapshot(**kwargs):
    return replace(TransportSnapshot(loaded=True, state="playing", can_previous=True, can_next=True), **kwargs)


def command(identifier):
    return taskbar.THBN_CLICKED << 16 | identifier


@pytest.mark.parametrize("pointer_type,size,icon_offset,tip_offset,flags_offset", [
    (C.c_uint32, 540, 12, 16, 536), (C.c_uint64, 552, 16, 24, 544),
])
def test_thumbbutton_windows_abi_for_both_pointer_widths(pointer_type, size, icon_offset, tip_offset, flags_offset):
    button = taskbar._thumbbutton_type(pointer_type)
    assert C.sizeof(button) == size
    assert button.hIcon.offset == icon_offset
    assert button.szTip.offset == tip_offset
    assert button.dwFlags.offset == flags_offset
    assert C.sizeof(taskbar.WCHAR) == 2
    assert C.sizeof(taskbar.HRESULT) == 4
    assert C.sizeof(taskbar.GUID) == 16
    assert C.sizeof(taskbar.BITMAPINFOHEADER) == 40


def test_real_pointer_width_vtable_slots_and_signatures_do_not_truncate_hwnd():
    observed = []
    hwnd = (1 << 40) + 123 if C.sizeof(C.c_void_p) == 8 else 0xF0001234
    callbacks = [
        taskbar.WINFUNCTYPE(taskbar.DWORD, taskbar.HANDLE)(lambda ptr: observed.append(("release", ptr)) or 0),
        taskbar.WINFUNCTYPE(taskbar.HRESULT, taskbar.HANDLE)(lambda ptr: observed.append(("init", ptr)) or 0),
    ]
    for name in ("add", "update"):
        callbacks.append(taskbar.WINFUNCTYPE(taskbar.HRESULT, taskbar.HANDLE, taskbar.HANDLE,
                                             taskbar.UINT, C.POINTER(taskbar.THUMBBUTTON))(
            lambda ptr, window, count, buttons, name=name:
                observed.append((name, window, count, buttons[0].iId, buttons[0].hIcon)) or 0
        ))
    vtable = (taskbar.HANDLE * 21)()
    for slot, callback in zip((2, 3, 15, 16), callbacks):
        vtable[slot] = C.cast(callback, taskbar.HANDLE)
    interface = (C.POINTER(taskbar.HANDLE) * 1)(C.cast(vtable, C.POINTER(taskbar.HANDLE)))
    pointer = C.cast(interface, taskbar.HANDLE)
    native = taskbar._Win32.__new__(taskbar._Win32)
    buttons = (taskbar.THUMBBUTTON * 3)()
    buttons[0].iId = taskbar.PREVIOUS_ID
    buttons[0].hIcon = hwnd
    native.initialize_taskbar(pointer)
    native.add_buttons(pointer, hwnd, buttons)
    native.update_buttons(pointer, hwnd, buttons)
    native.release(pointer)
    assert [row[0] for row in observed] == ["init", "add", "update", "release"]
    assert observed[1] == ("add", hwnd, 3, taskbar.PREVIOUS_ID, hwnd)
    assert observed[2] == ("update", hwnd, 3, taskbar.PREVIOUS_ID, hwnd)


@pytest.mark.parametrize("result,owned", [(0, True), (1, True), (0x80010106, False), (-2147417850, False)])
def test_com_apartment_ownership_balances_only_successful_calls(result, owned):
    native = taskbar._Win32.__new__(taskbar._Win32)
    native._co_initialize = lambda reserved, flags: result
    assert native.begin_com() is owned


@pytest.mark.parametrize("result", [0x80004005, -2147467259])
def test_hresult_failures_are_signed_and_bounded(result):
    with pytest.raises(taskbar._NativeFailure, match="^fixed_code$"):
        taskbar._check_hresult(result, "fixed_code")
    native = taskbar._Win32.__new__(taskbar._Win32)
    native._co_initialize = lambda *_args: result
    with pytest.raises(taskbar._NativeFailure, match="^com_initialize$"):
        native.begin_com()


@pytest.mark.parametrize("hwnd", [0, -1, True, "123", 1 << (8 * C.sizeof(C.c_void_p))])
def test_invalid_windows_never_load_or_call_native_api(hwnd):
    api = FakeAPI()
    backend = taskbar.WindowsTaskbar(hwnd, _api=api)
    assert backend.error_code == "invalid_window"
    assert not backend.taskbar_ready()
    assert not backend.publish(active_snapshot())
    backend.close()
    assert api.calls == []


def test_nonwindows_construction_is_noop_without_dll_load(monkeypatch):
    monkeypatch.setattr(taskbar.sys, "platform", "linux")
    monkeypatch.setattr(taskbar, "_Win32", lambda: pytest.fail("DLL loader must remain unused"))
    backend = taskbar.WindowsTaskbar(123)
    backend.publish(active_snapshot())
    assert backend.taskbar_created_message == 0
    assert backend.error_code == "unsupported_platform"
    assert not backend.taskbar_ready()
    backend.close()


def test_constructor_and_publish_wait_for_shell_readiness_then_replay_latest():
    api = FakeAPI()
    backend = taskbar.WindowsTaskbar(123, _api=api)
    assert backend.taskbar_created_message == 0xC111
    assert [call[0] for call in api.calls] == ["owns_window", "register_message"]
    assert not backend.publish(active_snapshot())
    assert len(api.calls) == 2
    assert backend.handle_message(taskbar.WM_COMMAND, command(taskbar.NEXT_ID), 0) is None
    assert backend.taskbar_ready()
    assert backend.available
    assert api.last_buttons == (
        (taskbar.PREVIOUS_ID, 11, 0, "Previous", 14),
        (taskbar.TOGGLE_ID, 13, 0, "Pause", 14),
        (taskbar.NEXT_ID, 14, 0, "Next", 14),
    )
    backend.close()


def test_capability_updates_dedupe_timeline_metadata_and_never_export_titles():
    api = FakeAPI()
    backend = taskbar.WindowsTaskbar(123, _api=api)
    backend.taskbar_ready()
    assert all(row[2] == taskbar.THBF_DISABLED for row in api.last_buttons)
    backend.publish(active_snapshot())
    count = len(api.calls)
    backend.publish(active_snapshot(revision=9, title="Private synthetic title", position_ms=1000))
    assert len(api.calls) == count
    backend.publish(active_snapshot(state="paused", can_previous=False))
    assert api.last_buttons[0][2] == taskbar.THBF_DISABLED
    assert api.last_buttons[1][1:4] == (12, 0, "Play")
    backend.publish(TransportSnapshot())
    assert all(row[2] == taskbar.THBF_DISABLED for row in api.last_buttons)
    assert "Private" not in str(api.last_buttons)
    backend.close()


def test_second_shell_epoch_recreates_and_replays_current_state_only_once():
    api = FakeAPI()
    backend = taskbar.WindowsTaskbar(123, _api=api)
    backend.publish(active_snapshot(state="paused"))
    assert backend.taskbar_ready()
    assert backend.taskbar_ready()
    assert [call for call in api.calls if call[0] == "release"] == [("release", 1)]
    assert [call for call in api.calls if call[0] == "destroy_icon"] == [
        ("destroy_icon", value) for value in (11, 12, 13, 14)
    ]
    assert len([call for call in api.calls if call[0] == "add_buttons"]) == 2
    assert api.last_buttons[1][1:4] == (22, 0, "Play")
    backend.close()


@pytest.mark.parametrize("identifier,expected", [(taskbar.PREVIOUS_ID, "previous"), (taskbar.TOGGLE_ID, "toggle"), (taskbar.NEXT_ID, "next")])
def test_only_enabled_thumbnail_notifications_map_to_actions_without_debounce(identifier, expected):
    api = FakeAPI()
    backend = taskbar.WindowsTaskbar(123, _api=api)
    backend.publish(active_snapshot())
    backend.taskbar_ready()
    for _ in range(3):
        assert backend.handle_message(taskbar.WM_COMMAND, command(identifier), 0) == expected
    for message, wparam, lparam in ((1, command(identifier), 0), (taskbar.WM_COMMAND, identifier, 0),
                                  (taskbar.WM_COMMAND, command(identifier), 1),
                                  (taskbar.WM_COMMAND, command(5), 0),
                                  (taskbar.WM_COMMAND, command(identifier) | (1 << 40), 0)):
        assert backend.handle_message(message, wparam, lparam) is None
    backend.publish(TransportSnapshot())
    assert backend.handle_message(taskbar.WM_COMMAND, command(identifier), 0) is None
    backend.close()
    assert backend.handle_message(taskbar.WM_COMMAND, command(identifier), 0) is None


@pytest.mark.parametrize("failure,icon_count,releases,ends", [
    ("begin_com", 0, 0, 0), ("create_taskbar", 0, 0, 1), ("initialize_taskbar", 0, 1, 1),
    ("create_icon_previous", 0, 1, 1), ("create_icon_pause", 2, 1, 1), ("add_buttons", 4, 1, 1),
])
def test_partial_native_failures_release_all_owned_resources_and_can_recover(failure, icon_count, releases, ends):
    api = FakeAPI(failure)
    backend = taskbar.WindowsTaskbar(123, _api=api)
    assert not backend.taskbar_ready()
    assert backend.error_code == failure
    assert not backend.available
    assert len([call for call in api.calls if call[0] == "destroy_icon"]) == icon_count
    assert len([call for call in api.calls if call[0] == "release"]) == releases
    assert len([call for call in api.calls if call[0] == "end_com"]) == ends
    api.failure = None
    assert backend.taskbar_ready()
    backend.close()


def test_update_failure_is_contained_then_waits_for_new_shell_epoch():
    api = FakeAPI()
    backend = taskbar.WindowsTaskbar(123, _api=api)
    backend.taskbar_ready()
    api.failure = "update_buttons"
    assert not backend.publish(active_snapshot())
    assert not backend.available
    assert backend.error_code == "update_buttons"
    count = len(api.calls)
    backend.publish(active_snapshot(state="paused"))
    assert len(api.calls) == count
    api.failure = None
    assert backend.taskbar_ready()
    assert api.last_buttons[1][3] == "Play"
    backend.close()


def test_close_hides_buttons_and_releases_once_without_uninitializing_foreign_com():
    api = FakeAPI(com_owned=False)
    backend = taskbar.WindowsTaskbar(123, _api=api)
    backend.taskbar_ready()
    backend.close()
    assert all(row[2] == taskbar.THBF_HIDDEN | taskbar.THBF_DISABLED for row in api.last_buttons)
    assert len([call for call in api.calls if call[0] == "release"]) == 1
    assert len([call for call in api.calls if call[0] == "destroy_icon"]) == 4
    assert not any(call[0] == "end_com" for call in api.calls)
    count = len(api.calls)
    backend.close()
    assert not backend.taskbar_ready()
    assert not backend.publish(active_snapshot())
    assert len(api.calls) == count


def test_wrong_thread_never_invokes_com_and_correct_owner_can_still_close():
    api = FakeAPI()
    backend = taskbar.WindowsTaskbar(123, _api=api)
    backend.taskbar_ready()
    before = len(api.calls)
    def elsewhere():
        backend.publish(active_snapshot())
        backend.taskbar_ready()
        backend.close()
    worker = threading.Thread(target=elsewhere)
    worker.start()
    worker.join()
    assert len(api.calls) == before
    assert backend.error_code == "wrong_thread"
    backend.close()
    assert not backend.available


def test_generated_glyphs_are_distinct_transparent_and_scale_bounded():
    shapes = []
    for kind in ("previous", "play", "pause", "next"):
        pixels, mask = taskbar._glyph_bitmap(kind, 32, 32)
        assert len(pixels) == 32 * 32 * 4
        assert len(mask) == 4 * 32
        assert set(pixels[3::4]) == {0, 255}
        assert pixels[:4] == b"\0\0\0\0"
        assert 0 < sum(value == 255 for value in pixels[3::4]) < 32 * 32
        shapes.append(pixels)
    assert len(set(shapes)) == 4
    assert len(taskbar._glyph_bitmap("play", 48, 48)[0]) == 48 * 48 * 4
    with pytest.raises(taskbar._NativeFailure):
        taskbar._glyph_bitmap("play", 10000, 32)


@pytest.mark.parametrize("failure,expected_deleted", [(None, [102, 101]), ("mask", [101]), ("icon", [102, 101]), ("color", [])])
def test_native_icon_creation_deletes_intermediate_bitmaps_on_every_path(failure, expected_deleted):
    native = taskbar._Win32.__new__(taskbar._Win32)
    pixel_memory = (C.c_uint8 * (32 * 32 * 4))()
    deleted = []
    def create_dib(_dc, info, _usage, bits, _section, _offset):
        header = C.cast(info, C.POINTER(taskbar.BITMAPINFO)).contents.bmiHeader
        assert (header.biWidth, header.biHeight, header.biPlanes, header.biBitCount) == (32, -32, 1, 32)
        if failure == "color":
            return None
        C.cast(bits, C.POINTER(taskbar.HANDLE)).contents.value = C.addressof(pixel_memory)
        return 101
    native._create_dib = create_dib
    native._create_bitmap = lambda *_args: None if failure == "mask" else 102
    native._create_icon = lambda _info: None if failure == "icon" else 501
    native._delete_object = lambda value: deleted.append(value)
    if failure:
        with pytest.raises(taskbar._NativeFailure):
            native._icon("play", 32, 32)
    else:
        assert native._icon("play", 32, 32) == 501
    assert deleted == expected_deleted
