"""Small, GUI-thread-owned Windows thumbnail transport backend.

No player, shell keyboard hook, Qt dependency, filesystem, or application data
access. ITaskbarList3 is used only after the host receives TaskbarButtonCreated.
The host filters HWNDs and dispatches returned commands through its authority.

ABI: Microsoft shobjidl_core.h ITaskbarList3/THUMBBUTTON. Icon ownership follows
CreateIconIndirect: delete input HBITMAPs and eventually DestroyIcon our HICONs.
https://learn.microsoft.com/windows/win32/api/shobjidl_core/ns-shobjidl_core-thumbbutton
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-createiconindirect
"""
from __future__ import annotations

import ctypes as C
import os
import sys
import threading
import uuid

from music_vault.core.transport_actions import TransportSnapshot


UINT = C.c_uint32
DWORD = C.c_uint32
LONG = C.c_int32
HRESULT = C.c_int32
HANDLE = C.c_void_p
WCHAR = C.c_uint16  # Windows UTF-16, including on non-Windows ABI tests.
WINFUNCTYPE = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)
WM_COMMAND = 0x0111
THBN_CLICKED = 0x1800
PREVIOUS_ID, TOGGLE_ID, NEXT_ID = 0xA711, 0xA712, 0xA713
THB_ICON, THB_TOOLTIP, THB_FLAGS = 0x2, 0x4, 0x8
THBF_DISABLED, THBF_HIDDEN = 0x1, 0x8
RPC_E_CHANGED_MODE = 0x80010106
_POINTER_MAX = (1 << (8 * C.sizeof(HANDLE))) - 1


class GUID(C.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", C.c_uint16), ("Data3", C.c_uint16),
                ("Data4", C.c_uint8 * 8)]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


CLSID_TASKBAR_LIST = GUID.parse("56fdf344-fd6d-11d0-958a-006097c9a090")
IID_TASKBAR_LIST_3 = GUID.parse("ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf")


def _thumbbutton_type(handle_type):
    class Button(C.Structure):
        _fields_ = [("dwMask", DWORD), ("iId", UINT), ("iBitmap", UINT),
                    ("hIcon", handle_type), ("szTip", WCHAR * 260), ("dwFlags", DWORD)]
    return Button


THUMBBUTTON = _thumbbutton_type(HANDLE)


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG),
                ("biPlanes", C.c_uint16), ("biBitCount", C.c_uint16),
                ("biCompression", DWORD), ("biSizeImage", DWORD),
                ("biXPelsPerMeter", LONG), ("biYPelsPerMeter", LONG),
                ("biClrUsed", DWORD), ("biClrImportant", DWORD)]


class BITMAPINFO(C.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 1)]


class ICONINFO(C.Structure):
    _fields_ = [("fIcon", LONG), ("xHotspot", DWORD), ("yHotspot", DWORD),
                ("hbmMask", HANDLE), ("hbmColor", HANDLE)]


class _NativeFailure(RuntimeError):
    """Fixed codes only: never publish raw Windows exception text."""


def _check_hresult(value: int, code: str) -> None:
    if HRESULT(value).value < 0:
        raise _NativeFailure(code)


def _com_call(pointer, slot, result_type, argument_types, *args):
    # IUnknown is a pointer to a pointer-width vtable. The first argument is
    # always the interface pointer, never a 32-bit integer HWND approximation.
    vtable = C.cast(pointer, C.POINTER(C.POINTER(HANDLE))).contents
    function = WINFUNCTYPE(result_type, HANDLE, *argument_types)(vtable[slot])
    return function(pointer, *args)


def _glyph_bitmap(kind: str, width: int, height: int) -> tuple[bytes, bytes]:
    """Opaque light glyph, dark one-pixel outline, transparent background.

    Coordinates are normalized to 32px; generate at the window's icon metrics
    instead of stretching a tiny low-DPI resource. The 1bpp mask is WORD aligned.
    """
    if kind not in {"previous", "play", "pause", "next"}:
        raise ValueError("Unsupported transport glyph.")
    if not (16 <= width <= 256 and 16 <= height <= 256):
        raise _NativeFailure("icon_dimensions")

    def inside(x, y):
        if kind == "pause":
            return 7 <= y <= 25 and (9 <= x <= 13 or 19 <= x <= 23)
        if kind == "play":
            return 9 <= x <= 25 and abs(y - 16) <= (25 - x) * 10 / 16
        if kind == "previous":
            x = 32 - x
        return (25 <= x <= 28 and 7 <= y <= 25) or (
            5 <= x <= 23 and abs(y - 16) <= (23 - x) / 2
        )

    filled = {(x, y) for y in range(height) for x in range(width)
              if inside((x + .5) * 32 / width, (y + .5) * 32 / height)}
    pixels = bytearray(width * height * 4)
    stride = ((width + 15) // 16) * 2
    mask = bytearray(b"\xff" * stride * height)
    for y in range(height):
        for x in range(width):
            core = (x, y) in filled
            outline = not core and any((x + dx, y + dy) in filled
                                      for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)))
            if core or outline:
                pixels[(y * width + x) * 4:(y * width + x + 1) * 4] = (
                    b"\xf0\xeb\xeb\xff" if core else b"\x37\x2d\x2d\xff"
                )
                mask[y * stride + x // 8] &= ~(0x80 >> (x % 8))
    return bytes(pixels), bytes(mask)


class _Win32:
    """DLLs load only when an actual Windows backend is constructed."""

    def __init__(self):
        user = C.WinDLL("user32", use_last_error=True)
        gdi = C.WinDLL("gdi32", use_last_error=True)
        ole = C.WinDLL("ole32", use_last_error=True)

        def bind(dll, name, restype, *argtypes):
            function = getattr(dll, name)
            function.restype, function.argtypes = restype, list(argtypes)
            return function

        self._register = bind(user, "RegisterWindowMessageW", UINT, C.c_wchar_p)
        self._is_window = bind(user, "IsWindow", LONG, HANDLE)
        self._window_process = bind(user, "GetWindowThreadProcessId", DWORD, HANDLE, C.POINTER(DWORD))
        self._metrics = bind(user, "GetSystemMetrics", C.c_int, C.c_int)
        self._dpi = bind(user, "GetDpiForWindow", UINT, HANDLE) if hasattr(user, "GetDpiForWindow") else None
        self._dpi_metrics = bind(user, "GetSystemMetricsForDpi", C.c_int, C.c_int, UINT) if hasattr(user, "GetSystemMetricsForDpi") else None
        self._create_icon = bind(user, "CreateIconIndirect", HANDLE, C.POINTER(ICONINFO))
        self._destroy_icon = bind(user, "DestroyIcon", LONG, HANDLE)
        self._create_dib = bind(gdi, "CreateDIBSection", HANDLE, HANDLE, C.POINTER(BITMAPINFO), UINT, C.POINTER(HANDLE), HANDLE, DWORD)
        self._create_bitmap = bind(gdi, "CreateBitmap", HANDLE, C.c_int, C.c_int, UINT, UINT, HANDLE)
        self._delete_object = bind(gdi, "DeleteObject", LONG, HANDLE)
        self._co_initialize = bind(ole, "CoInitializeEx", HRESULT, HANDLE, DWORD)
        self._co_uninitialize = bind(ole, "CoUninitialize", None)
        self._co_create = bind(ole, "CoCreateInstance", HRESULT, C.POINTER(GUID), HANDLE, DWORD, C.POINTER(GUID), C.POINTER(HANDLE))

    def register_message(self):
        return int(self._register("TaskbarButtonCreated"))

    def owns_window(self, hwnd):
        pid = DWORD()
        return bool(self._is_window(hwnd) and self._window_process(hwnd, C.byref(pid)) and pid.value == os.getpid())

    def begin_com(self):
        result = int(self._co_initialize(None, 2))  # COINIT_APARTMENTTHREADED
        if result & 0xFFFFFFFF == RPC_E_CHANGED_MODE:
            return False  # Existing apartment; do not uninitialize someone else's.
        _check_hresult(result, "com_initialize")
        return True  # Includes S_FALSE: this successful call must be balanced.

    def end_com(self):
        self._co_uninitialize()

    def create_taskbar(self):
        pointer = HANDLE()
        result = self._co_create(C.byref(CLSID_TASKBAR_LIST), None, 1,
                                 C.byref(IID_TASKBAR_LIST_3), C.byref(pointer))
        _check_hresult(result, "com_create")
        if not pointer.value:
            raise _NativeFailure("com_create")
        return pointer

    def initialize_taskbar(self, pointer):
        _check_hresult(_com_call(pointer, 3, HRESULT, ()), "taskbar_initialize")

    def add_buttons(self, pointer, hwnd, buttons):
        _check_hresult(_com_call(pointer, 15, HRESULT, (HANDLE, UINT, C.POINTER(THUMBBUTTON)),
                                 hwnd, len(buttons), buttons), "taskbar_add")

    def update_buttons(self, pointer, hwnd, buttons):
        _check_hresult(_com_call(pointer, 16, HRESULT, (HANDLE, UINT, C.POINTER(THUMBBUTTON)),
                                 hwnd, len(buttons), buttons), "taskbar_update")

    def release(self, pointer):
        _com_call(pointer, 2, DWORD, ())

    def create_icon(self, kind, hwnd):
        dpi = int(self._dpi(hwnd)) if self._dpi else 0
        metrics = lambda index: int(self._dpi_metrics(index, dpi)) if dpi and self._dpi_metrics else int(self._metrics(index))
        return self._icon(kind, metrics(11), metrics(12))

    def _icon(self, kind, width, height):
        pixels, mask = _glyph_bitmap(kind, width, height)
        info = BITMAPINFO()
        info.bmiHeader = BITMAPINFOHEADER(C.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, len(pixels), 0, 0, 0, 0)
        bits = HANDLE()
        color = self._create_dib(None, C.byref(info), 0, C.byref(bits), None, 0)
        monochrome = None
        try:
            if not color or not bits.value:
                raise _NativeFailure("icon_bitmap")
            C.memmove(bits, pixels, len(pixels))
            buffer = C.create_string_buffer(mask)
            monochrome = self._create_bitmap(width, height, 1, 1, C.cast(buffer, HANDLE))
            if not monochrome:
                raise _NativeFailure("icon_mask")
            handle = self._create_icon(C.byref(ICONINFO(1, 0, 0, monochrome, color)))
            if not handle:
                raise _NativeFailure("icon_create")
            return handle
        finally:
            if monochrome:
                self._delete_object(monochrome)
            if color:
                self._delete_object(color)

    def destroy_icon(self, handle):
        self._destroy_icon(handle)


class WindowsTaskbar:
    """One immutable HWND session. Replace the object when its HWND changes.

    Call all methods on the constructing GUI thread. Optional failures never
    escape to playback; error_code exposes fixed diagnostics. Readiness starts
    a new shell epoch, adds exactly three buttons, and replays the latest state.
    """

    def __init__(self, hwnd: int, *, _api=None):
        self.hwnd = hwnd
        self.taskbar_created_message = 0
        self.error_code = None
        self.available = False
        self._thread = threading.get_ident()
        self._closed = False
        self._api = None
        self._pointer = None
        self._com_owned = False
        self._icons = {}
        self._snapshot = TransportSnapshot()
        self._published = None
        if type(hwnd) is not int or not 0 < hwnd <= _POINTER_MAX:
            self.error_code = "invalid_window"
            return
        if _api is None and sys.platform != "win32":
            self.error_code = "unsupported_platform"
            return
        try:
            self._api = _api if _api is not None else _Win32()
            if not self._api.owns_window(hwnd):
                self.error_code = "invalid_window"
                self._api = None
                return
            self.taskbar_created_message = self._api.register_message()
            if not self.taskbar_created_message:
                self.error_code = "message_registration"
                self._api = None
        except Exception:
            self.error_code = "native_unavailable"
            self._api = None

    def _on_owner_thread(self):
        if threading.get_ident() != self._thread:
            self.error_code = "wrong_thread"
            return False
        return True

    @staticmethod
    def _signature(snapshot):
        return bool(snapshot.loaded), snapshot.state == "playing", bool(snapshot.can_previous), bool(snapshot.can_next)

    def _buttons(self, *, hidden=False):
        snapshot = self._snapshot
        playing = snapshot.loaded and snapshot.state == "playing"
        rows = (
            (PREVIOUS_ID, "previous", "Previous", snapshot.can_previous),
            (TOGGLE_ID, "pause" if playing else "play", "Pause" if playing else "Play", snapshot.loaded),
            (NEXT_ID, "next", "Next", snapshot.can_next),
        )
        buttons = (THUMBBUTTON * 3)()
        for button, (identifier, icon, tip, enabled) in zip(buttons, rows):
            button.dwMask = THB_ICON | THB_TOOLTIP | THB_FLAGS
            button.iId = identifier
            button.hIcon = self._icons[icon]
            for index, char in enumerate(tip):
                button.szTip[index] = ord(char)
            button.dwFlags = THBF_HIDDEN | THBF_DISABLED if hidden else 0 if enabled else THBF_DISABLED
        return buttons

    def _release_epoch(self):
        self.available = False
        self._published = None
        pointer, self._pointer = self._pointer, None
        if pointer is not None:
            try:
                self._api.release(pointer)
            except Exception:
                self.error_code = "taskbar_release"
        icons, self._icons = self._icons, {}
        for handle in icons.values():
            try:
                self._api.destroy_icon(handle)
            except Exception:
                self.error_code = "icon_release"
        owned, self._com_owned = self._com_owned, False
        if owned:
            try:
                self._api.end_com()
            except Exception:
                self.error_code = "com_release"

    def taskbar_ready(self) -> bool:
        if self._closed or not self._on_owner_thread() or self._api is None:
            return False
        self._release_epoch()
        try:
            if not self._api.owns_window(self.hwnd):
                raise _NativeFailure("invalid_window")
            self._com_owned = self._api.begin_com()
            self._pointer = self._api.create_taskbar()
            self._api.initialize_taskbar(self._pointer)
            for kind in ("previous", "play", "pause", "next"):
                self._icons[kind] = self._api.create_icon(kind, self.hwnd)
            self._api.add_buttons(self._pointer, self.hwnd, self._buttons())
            self._published = self._signature(self._snapshot)
            self.available = True
            self.error_code = None
            return True
        except Exception as exc:
            self.error_code = str(exc) if isinstance(exc, _NativeFailure) else "taskbar_unavailable"
            self._release_epoch()
            return False

    def publish(self, snapshot: TransportSnapshot) -> bool:
        if self._closed or not self._on_owner_thread():
            return False
        self._snapshot = snapshot
        if not self.available:
            return False
        signature = self._signature(snapshot)
        if signature == self._published:
            return True
        try:
            self._api.update_buttons(self._pointer, self.hwnd, self._buttons())
            self._published = signature
            return True
        except Exception as exc:
            self.error_code = str(exc) if isinstance(exc, _NativeFailure) else "taskbar_update"
            self._release_epoch()
            return False

    def handle_message(self, message: int, wparam: int, lparam: int) -> str | None:
        if self._closed or not self.available or not self._on_owner_thread():
            return None
        if message != WM_COMMAND or lparam != 0 or type(wparam) is not int or not 0 <= wparam <= 0xFFFFFFFF:
            return None
        if (wparam >> 16) != THBN_CLICKED:
            return None
        identifier = wparam & 0xFFFF
        snapshot = self._snapshot
        if identifier == PREVIOUS_ID and snapshot.can_previous:
            return "previous"
        if identifier == TOGGLE_ID and snapshot.loaded:
            return "toggle"
        if identifier == NEXT_ID and snapshot.can_next:
            return "next"
        return None

    def close(self) -> None:
        if self._closed or not self._on_owner_thread():
            return
        self._closed = True
        if self.available:
            try:
                self._api.update_buttons(self._pointer, self.hwnd, self._buttons(hidden=True))
            except Exception:
                self.error_code = "taskbar_close"
        self._release_epoch()


__all__ = ["WindowsTaskbar"]
