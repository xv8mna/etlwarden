"""The engine's icon next to the clock, in the notification area (Windows only).

The engine has no window, so the icon gets a hidden one in its own thread: Windows sends
clicks on the icon to it as messages. Everything uses Windows' own functions, so the
engine needs no Qt and no extra package.

Microsoft's guidance for these icons: a click shows what people most want (the window),
a right-click shows a menu with that command in bold, and a problem shows as one small
mark at the bottom right of the icon.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("etlwarden.tray")

ICONS = {False: Path(__file__).with_name("tray.ico"), True: Path(__file__).with_name("tray-error.ico")}
OPEN, STOP = 1, 2  # the menu's commands
OPEN_AGAIN_SECONDS = 2.0  # a double-click is two clicks; it opens one window

WM_NULL, WM_DESTROY, WM_CLOSE, WM_COMMAND = 0x0000, 0x0002, 0x0010, 0x0111
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
CLICKED, UPDATE = 0x8001, 0x8002  # WM_APP + 1 and + 2: our own messages
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_STATE = 0x01, 0x02, 0x04, 0x08
NIS_HIDDEN = 0x01
MF_STRING, TPM_RIGHTBUTTON = 0x0000, 0x0002
MB_YESNO, MB_ICONQUESTION, MB_SETFOREGROUND, IDYES = 0x04, 0x20, 0x10000, 6
IMAGE_ICON, LR_LOADFROMFILE, SM_CXSMICON = 1, 0x10, 49
SW_RESTORE = 9
CLASS_NAME = "etlwarden-tray"

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uTimeout", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


_api: dict[str, ctypes.WinDLL] = {}
_trays: dict[int, Tray] = {}  # by window handle, for the one window procedure
_class_lock = threading.Lock()
_class_registered = False


def _dll(name: str) -> ctypes.WinDLL:
    """Windows' libraries with their argument types set, so 64-bit handles stay whole. Loaded on first use."""
    if not _api:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        signatures = {
            (user32, "RegisterClassExW"): (wintypes.ATOM, [ctypes.POINTER(WNDCLASSEXW)]),
            (user32, "CreateWindowExW"): (wintypes.HWND, [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]),
            (user32, "DefWindowProcW"): (LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
            (user32, "DestroyWindow"): (wintypes.BOOL, [wintypes.HWND]),
            (user32, "GetMessageW"): (wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                                      wintypes.UINT]),
            (user32, "TranslateMessage"): (wintypes.BOOL, [ctypes.POINTER(wintypes.MSG)]),
            (user32, "DispatchMessageW"): (LRESULT, [ctypes.POINTER(wintypes.MSG)]),
            (user32, "PostMessageW"): (wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
            (user32, "SendMessageW"): (LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
            (user32, "PostQuitMessage"): (None, [ctypes.c_int]),
            (user32, "RegisterWindowMessageW"): (wintypes.UINT, [wintypes.LPCWSTR]),
            (user32, "CreatePopupMenu"): (wintypes.HMENU, []),
            (user32, "AppendMenuW"): (wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                                                      wintypes.LPCWSTR]),
            (user32, "SetMenuDefaultItem"): (wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, wintypes.UINT]),
            (user32, "TrackPopupMenuEx"): (wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                                           wintypes.HWND, wintypes.LPVOID]),
            (user32, "DestroyMenu"): (wintypes.BOOL, [wintypes.HMENU]),
            (user32, "GetCursorPos"): (wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)]),
            (user32, "SetForegroundWindow"): (wintypes.BOOL, [wintypes.HWND]),
            (user32, "LoadImageW"): (wintypes.HANDLE, [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]),
            (user32, "DestroyIcon"): (wintypes.BOOL, [wintypes.HICON]),
            (user32, "GetSystemMetrics"): (ctypes.c_int, [ctypes.c_int]),
            (user32, "MessageBoxW"): (ctypes.c_int, [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]),
            (user32, "EnumWindows"): (wintypes.BOOL, [WNDENUMPROC, wintypes.LPARAM]),
            (user32, "GetWindowTextW"): (ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
            (user32, "GetClassNameW"): (ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
            (user32, "IsWindowVisible"): (wintypes.BOOL, [wintypes.HWND]),
            (user32, "IsIconic"): (wintypes.BOOL, [wintypes.HWND]),
            (user32, "ShowWindow"): (wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
            (kernel32, "GetModuleHandleW"): (wintypes.HMODULE, [wintypes.LPCWSTR]),
            (shell32, "Shell_NotifyIconW"): (wintypes.BOOL, [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]),
        }
        for (library, function), (result, arguments) in signatures.items():
            getattr(library, function).restype = result
            getattr(library, function).argtypes = arguments
        _api.update(user32=user32, kernel32=kernel32, shell32=shell32)
    return _api[name]


@WNDPROC
def _window_procedure(hwnd, message, wparam, lparam):
    tray = _trays.get(hwnd)
    if tray is not None:
        try:
            handled = tray._handle(message, wparam, lparam)
        except Exception:  # an error here must never reach Windows
            log.exception("The icon next to the clock couldn't handle message %#x", message)
            handled = 0
        if handled is not None:
            return handled
    return _dll("user32").DefWindowProcW(hwnd, message, wparam, lparam)


def _register_class() -> None:
    global _class_registered
    with _class_lock:
        if _class_registered:
            return
        window_class = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=_window_procedure,
                                   hInstance=_dll("kernel32").GetModuleHandleW(None), lpszClassName=CLASS_NAME)
        if not _dll("user32").RegisterClassExW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        _class_registered = True


class Tray:
    """The icon, with a menu to open the window or stop the engine.

    `hidden` keeps the icon out of sight, so the tests can use the real thing.
    """

    def __init__(self, on_open: Callable[[], None], on_stop: Callable[[], None], *, hidden: bool = False) -> None:
        self.on_open = on_open
        self.on_stop = on_stop
        self.hidden = hidden
        self.hwnd: int | None = None
        self._lock = threading.Lock()
        self._wanted = ("etlwarden", False)  # the tooltip, and whether a job failed
        self._shown: tuple[str, bool] | None = None  # what Windows shows now; None means no icon
        self._icons: dict[bool, int] = {}
        self._taskbar_created: int | None = None
        self._last_open = -OPEN_AGAIN_SECONDS
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)

    @property
    def visible(self) -> bool:
        """Whether Windows has the icon."""
        return self._shown is not None

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout) or not self.hwnd:
            raise OSError("Couldn't create the window that receives the icon's clicks")

    def show(self, tooltip: str, failed: bool) -> None:
        """Set the tooltip and the error mark. Callable from any thread."""
        with self._lock:
            self._wanted = (tooltip[:127], failed)
        if self.hwnd:
            _dll("user32").PostMessageW(self.hwnd, UPDATE, 0, 0)

    def close(self, timeout: float = 10.0) -> None:
        """Remove the icon and end its thread."""
        if self.hwnd:
            _dll("user32").PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        user32 = _dll("user32")
        try:
            _register_class()
            hwnd = user32.CreateWindowExW(0, CLASS_NAME, "etlwarden", 0, 0, 0, 0, 0, None, None,
                                          _dll("kernel32").GetModuleHandleW(None), None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            size = user32.GetSystemMetrics(SM_CXSMICON)
            for failed, path in ICONS.items():
                self._icons[failed] = user32.LoadImageW(None, str(path), IMAGE_ICON, size, size, LR_LOADFROMFILE)
                if not self._icons[failed]:
                    raise ctypes.WinError(ctypes.get_last_error())
            # Explorer sends this when the taskbar comes back after a restart; the icon must then be added again.
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            _trays[hwnd] = self
            self.hwnd = hwnd
        except Exception:
            log.exception("Couldn't set up the icon next to the clock")
            return
        finally:
            self._ready.set()
        self._apply()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        _trays.pop(hwnd, None)
        for icon in self._icons.values():
            if icon:
                user32.DestroyIcon(icon)

    def _handle(self, message: int, wparam: int, lparam: int) -> int | None:
        """One message for the hidden window. None leaves it to Windows."""
        if message == UPDATE:
            self._apply()
        elif message == self._taskbar_created:
            self._shown = None
            self._apply()
        elif message == CLICKED:
            if lparam == WM_LBUTTONUP:
                self._open()
            elif lparam == WM_RBUTTONUP:
                self._menu()
        elif message == WM_COMMAND:
            command = wparam & 0xFFFF
            if command == OPEN:
                self._open()
            elif command == STOP:
                self.on_stop()
        elif message == WM_DESTROY:
            self._remove()
            _dll("user32").PostQuitMessage(0)
        else:
            return None
        return 0

    def _data(self) -> NOTIFYICONDATAW:
        return NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1)

    def _apply(self) -> None:
        """Bring the icon up to date. While there's no taskbar yet (just after logging on), adding it
        fails; "TaskbarCreated" and the next update try again."""
        with self._lock:
            wanted = self._wanted
        if wanted == self._shown:
            return
        tooltip, failed = wanted
        data = self._data()
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = CLICKED
        data.hIcon = self._icons[failed]
        data.szTip = tooltip
        if self.hidden:
            data.uFlags |= NIF_STATE
            data.dwState = data.dwStateMask = NIS_HIDDEN
        shell = _dll("shell32").Shell_NotifyIconW
        if self._shown is not None and shell(NIM_MODIFY, ctypes.byref(data)):
            self._shown = wanted
        elif shell(NIM_ADD, ctypes.byref(data)) or shell(NIM_MODIFY, ctypes.byref(data)):
            self._shown = wanted  # adding fails if the taskbar still has the icon; then it's changed

    def _remove(self) -> None:
        if self._shown is not None:
            _dll("shell32").Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data()))
            self._shown = None

    def _open(self) -> None:
        now = time.monotonic()
        if now - self._last_open < OPEN_AGAIN_SECONDS:
            return
        self._last_open = now
        if not bring_window_to_front():
            self.on_open()

    def _menu(self) -> None:
        user32 = _dll("user32")
        menu = user32.CreatePopupMenu()
        try:
            user32.AppendMenuW(menu, MF_STRING, OPEN, "Open etlwarden")
            user32.AppendMenuW(menu, MF_STRING, STOP, "Stop engine")
            user32.SetMenuDefaultItem(menu, OPEN, False)  # shown in bold
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)  # or the menu stays open after a click elsewhere
            user32.TrackPopupMenuEx(menu, TPM_RIGHTBUTTON, point.x, point.y, self.hwnd, None)
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)

    def ask(self, question: str) -> bool:
        """A Yes/No question, for "Stop engine" while jobs run."""
        answer = _dll("user32").MessageBoxW(self.hwnd, question, "etlwarden",
                                            MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND)
        return answer == IDYES


def is_etlwarden_window(title: str, class_name: str) -> bool:
    """etlwarden's own window: Qt names its window classes "Qt...", and Explorer's folder
    window for a folder called etlwarden has the same title but another class."""
    return title == "etlwarden" and class_name.startswith("Qt")


def bring_window_to_front() -> bool:
    """Show an open etlwarden window instead of starting a second one. Returns whether there was one."""
    user32 = _dll("user32")
    found: list[int] = []

    @WNDENUMPROC
    def look(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title, class_name = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, title, 64)
            user32.GetClassNameW(hwnd, class_name, 64)
            if is_etlwarden_window(title.value, class_name.value):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(look, 0)
    if not found:
        return False
    if user32.IsIconic(found[0]):
        user32.ShowWindow(found[0], SW_RESTORE)
    user32.SetForegroundWindow(found[0])
    return True
