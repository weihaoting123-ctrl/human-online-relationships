"""Read-only Windows geometry bridge for the optional desktop copilot.

stdout is versioned JSON Lines, never window titles, executable paths or chat
content. Out-of-context geometry events are coalesced at 30 Hz, with a 500 ms
full-enumeration fallback. Only changed frames and a heartbeat are sent. --once
emits one normal frame; non-Windows exits 2 with a fixed error. Keep stdin open.

No window is moved, activated, reparented, injected into, or read with process
memory APIs. DPI calls configure this helper only. Executable image paths exist
only long enough to compare the basename against the fixed allowlist.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import ntpath
import os
import secrets
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import TextIO


FALLBACK_SECONDS = 0.5
MAX_WAIT_SECONDS = 0.05
HEARTBEAT_SECONDS = 0.75
MIN_FRAME_SECONDS = 1.0 / 30.0
EXECUTABLES = frozenset({"weixin.exe", "wechat.exe"})
MAIN_CLASSES = frozenset({"wechatmainwndforpc", "mmui::mainwindow"})
WS_CHILD = 0x40000000
WS_THICKFRAME = 0x00040000
EXCLUDED_EXSTYLES = 0x00000080 | 0x08000000 | 0x00000001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
HWND = ctypes.c_void_p
HANDLE = ctypes.c_void_p
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
LONG = ctypes.c_int32
UINT = ctypes.c_uint32
LPARAM = ctypes.c_ssize_t
CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
ENUM_WINDOWS_PROC = CALLBACK(BOOL, HWND, LPARAM)
WIN_EVENT_PROC = CALLBACK(None, HANDLE, DWORD, HWND, LONG, LONG, DWORD, DWORD)
# Receive no text/name/value/accessibility-content events. OBJID_WINDOW and
# CHILDID_SELF are checked before even setting the dirty flag.
EVENT_RANGES = ((0x0003, 0x0003), (0x000A, 0x000B), (0x0016, 0x0017),
                (0x8000, 0x8003), (0x800B, 0x800B), (0x8017, 0x8018))


class NativePoint(ctypes.Structure):
    _fields_ = [('x', LONG), ('y', LONG)]


class NativeMessage(ctypes.Structure):
    _fields_ = [('hwnd', HWND), ('message', UINT), ('wParam', ctypes.c_size_t),
                ('lParam', LPARAM), ('time', DWORD), ('pt', NativePoint), ('lPrivate', DWORD)]


class NativeRect(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class FileTime(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass(frozen=True)
class WindowGeometry:
    rect: Rect | None
    visible: bool
    minimized: bool
    cloaked: bool


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    process_started: int
    executable: str
    class_name: str
    style: int
    exstyle: int
    owner: int
    root: int
    rect: Rect | None
    visible: bool
    minimized: bool
    cloaked: bool

    @property
    def identity(self) -> tuple[int, int, int]:
        return self.hwnd, self.pid, self.process_started


def ordinary_window(window: WindowInfo) -> bool:
    """Reject owned/tool/child/dialog windows before considering a main target."""
    class_name = window.class_name.casefold()
    return (
        window.executable.casefold() in EXECUTABLES
        and window.hwnd > 0 and window.pid > 0
        and window.owner == 0 and window.root == window.hwnd
        and not (window.style & WS_CHILD)
        and not (window.exstyle & EXCLUDED_EXSTYLES)
        and class_name not in {"#32770", "#32768"}
        and not any(part in class_name for part in ("dialog", "popup", "tooltip", "login", "update", "tray"))
        and bool(window.style & WS_THICKFRAME or class_name in MAIN_CLASSES)
    )


def candidate(window: WindowInfo) -> bool:
    return bool(ordinary_window(window) and window.visible and not window.minimized
                and not window.cloaked and window.rect and window.rect.valid
                and window.rect.width >= 480 and window.rect.height >= 320)


def unbound_frame(state: str, code: str, count: int = 0, foreground_pid: int = 0) -> dict:
    return {"version": 1, "state": state, "visible": False, "minimized": False,
            "foreground": False, "foreground_pid": foreground_pid,
            "candidate_count": count, "error_code": code}


class WindowWatcher:
    """State machine over injected, read-only native observations."""

    def __init__(self, api):
        self.api = api
        self._identity = None
        self._target = None
        self._last_rect = None
        self._generation = 0
        self._salt = secrets.token_bytes(16)

    def sample(self) -> dict:
        windows = {item.hwnd: item for item in self.api.windows()}
        foreground_root, foreground_pid = self.api.foreground()
        current = windows.get(self._identity[0]) if self._identity else None
        if current and (current.identity != self._identity or not ordinary_window(current)):
            current = None
        if current is None:
            self._identity = self._target = self._last_rect = None
        candidates = [item for item in windows.values() if candidate(item)]
        # An existing invisible/minimized target still counts as a competing
        # identity: never jump to a second visible account while it is hidden.
        competing = [item for item in candidates if current is None or item.identity != current.identity]
        count = len(competing) + (1 if current else 0)
        if count > 1:
            return unbound_frame("ambiguous", "WINDOW_AMBIGUOUS", count, foreground_pid)
        if current is None:
            if not candidates:
                return unbound_frame("waiting", "WINDOW_UNAVAILABLE", 0, foreground_pid)
            current = candidates[0]
            self._identity = current.identity
            self._generation += 1
            identity = f"{current.hwnd}:{current.pid}:{current.process_started}:{self._generation}"
            self._target = hashlib.blake2s(identity.encode("ascii"), key=self._salt, digest_size=16).hexdigest()
        rect = current.rect
        hidden = current.minimized or not current.visible or current.cloaked
        if not rect or not rect.valid:
            if not hidden or self._last_rect is None:
                return unbound_frame("error", "WINDOW_UNAVAILABLE", 1, foreground_pid)
            rect = self._last_rect
        else:
            self._last_rect = rect
        return {"version": 1, "state": "bound", "target": self._target,
                "rect": asdict(rect), "visible": current.visible and not current.cloaked,
                "minimized": current.minimized,
                "foreground": bool(foreground_root == current.hwnd and foreground_pid == current.pid),
                "foreground_pid": foreground_pid, "candidate_count": 1}


def _signature(dll, name, argtypes, restype):
    function = getattr(dll, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def declare_native_api(user32, kernel32, dwmapi, shcore=None):
    """Explicit pointer-sized HWND/HANDLE/LONG_PTR declarations, including x64."""
    _signature(user32, "EnumWindows", [ENUM_WINDOWS_PROC, LPARAM], BOOL)
    _signature(user32, 'SetWinEventHook', [DWORD, DWORD, HANDLE, WIN_EVENT_PROC, DWORD, DWORD, DWORD], HANDLE)
    _signature(user32, 'UnhookWinEvent', [HANDLE], BOOL)
    _signature(user32, 'PeekMessageW', [ctypes.POINTER(NativeMessage), HWND, UINT, UINT, UINT], BOOL)
    _signature(user32, 'TranslateMessage', [ctypes.POINTER(NativeMessage)], BOOL)
    _signature(user32, 'DispatchMessageW', [ctypes.POINTER(NativeMessage)], LPARAM)
    _signature(user32, 'MsgWaitForMultipleObjectsEx', [DWORD, ctypes.POINTER(HANDLE), DWORD, DWORD, DWORD], DWORD)
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        _signature(user32, name, [HWND], BOOL)
    _signature(user32, "GetForegroundWindow", [], HWND)
    _signature(user32, "GetThreadDesktop", [DWORD], HANDLE)
    _signature(user32, "GetUserObjectInformationW",
               [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)], BOOL)
    _signature(user32, "GetAncestor", [HWND, UINT], HWND)
    _signature(user32, "GetWindow", [HWND, UINT], HWND)
    _signature(user32, "GetWindowThreadProcessId", [HWND, ctypes.POINTER(DWORD)], DWORD)
    _signature(user32, "GetWindowRect", [HWND, ctypes.POINTER(NativeRect)], BOOL)
    _signature(user32, "GetClassNameW", [HWND, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int)
    long_name = "GetWindowLongPtrW" if ctypes.sizeof(HANDLE) == 8 else "GetWindowLongW"
    _signature(user32, long_name, [HWND, ctypes.c_int], ctypes.c_ssize_t)
    for name in ("SetProcessDpiAwarenessContext", "SetThreadDpiAwarenessContext"):
        if hasattr(user32, name):
            _signature(user32, name, [HANDLE], BOOL if name.startswith("SetProcess") else HANDLE)
    _signature(user32, "SetProcessDPIAware", [], BOOL)
    _signature(kernel32, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
    _signature(kernel32, "GetCurrentThreadId", [], DWORD)
    _signature(kernel32, "CloseHandle", [HANDLE], BOOL)
    _signature(kernel32, "QueryFullProcessImageNameW",
               [HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)], BOOL)
    _signature(kernel32, "GetProcessTimes", [HANDLE] + [ctypes.POINTER(FileTime)] * 4, BOOL)
    _signature(kernel32, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
    _signature(dwmapi, "DwmGetWindowAttribute", [HWND, DWORD, ctypes.c_void_p, DWORD], LONG)
    if shcore is not None:
        _signature(shcore, "SetProcessDpiAwareness", [ctypes.c_int], LONG)


class WindowsAPI:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        try:
            self.shcore = ctypes.WinDLL("shcore", use_last_error=True)
        except OSError:
            self.shcore = None
        declare_native_api(self.user32, self.kernel32, self.dwmapi, self.shcore)
        self._get_long = getattr(self.user32, "GetWindowLongPtrW" if ctypes.sizeof(HANDLE) == 8 else "GetWindowLongW")
        self._configure_dpi()

    def _configure_dpi(self):
        # Before any geometry call; these APIs affect only the helper process.
        set_process = getattr(self.user32, "SetProcessDpiAwarenessContext", None)
        if set_process and set_process(HANDLE(-4)):
            return
        set_thread = getattr(self.user32, "SetThreadDpiAwarenessContext", None)
        if set_thread and set_thread(HANDLE(-4)):
            return
        if self.shcore and self.shcore.SetProcessDpiAwareness(2) == 0:
            return
        if not self.user32.SetProcessDPIAware():
            raise OSError("DPI_UNAVAILABLE")

    def _pid(self, hwnd: int) -> int:
        pid = DWORD()
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return 0
        return pid.value

    def _process_identity(self, pid: int) -> tuple[str, int] | None:
        process = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = DWORD(len(buffer))
            if not self.kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
                return None
            basename = ntpath.basename(buffer.value).casefold()
            if basename not in EXECUTABLES:
                return None
            creation, exit_time, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
            if not self.kernel32.GetProcessTimes(process, ctypes.byref(creation), ctypes.byref(exit_time),
                                                  ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return basename, (creation.high << 32) | creation.low
        finally:
            self.kernel32.CloseHandle(process)

    def _rect(self, hwnd: int) -> Rect | None:
        native = NativeRect()
        if self.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(native), ctypes.sizeof(native)) != 0:
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(native)):
                return None
        result = Rect(native.left, native.top, native.right - native.left, native.bottom - native.top)
        return result if result.valid else None

    def read_geometry(self, hwnd: int) -> WindowGeometry:
        """Read geometry only; also usable by synthetic, self-owned UI fixtures."""
        cloaked = DWORD()
        result = self.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return WindowGeometry(
            rect=self._rect(hwnd), visible=bool(self.user32.IsWindowVisible(hwnd)),
            minimized=bool(self.user32.IsIconic(hwnd)),
            # Unknown virtual-desktop visibility fails closed.
            cloaked=bool(result != 0 or cloaked.value),
        )

    def input_desktop_active(self) -> bool:
        desktop = self.user32.GetThreadDesktop(self.kernel32.GetCurrentThreadId())
        if not desktop:
            return False
        active, needed = BOOL(), DWORD()
        # UOI_IO asks only whether this desktop receives input. It reads neither
        # a desktop/account name nor any window title and requires no new handle.
        success = self.user32.GetUserObjectInformationW(
            desktop, 6, ctypes.byref(active), ctypes.sizeof(active), ctypes.byref(needed))
        return bool(success and active.value)

    def windows(self) -> list[WindowInfo]:
        handles = []

        @ENUM_WINDOWS_PROC
        def collect(hwnd, _lparam):
            handles.append(int(hwnd))
            return True

        # EnumWindows invokes this held callback synchronously; no global hook.
        if not self.user32.EnumWindows(collect, 0):
            raise OSError("WINDOW_UNAVAILABLE")
        identities = {}
        observations = []
        desktop_active = self.input_desktop_active()
        for hwnd in handles:
            pid = self._pid(hwnd)
            if not pid:
                continue
            if pid not in identities:
                identities[pid] = self._process_identity(pid)
            identity = identities[pid]
            if identity is None:
                continue
            class_name = ctypes.create_unicode_buffer(256)
            if not self.user32.GetClassNameW(hwnd, class_name, len(class_name)):
                continue
            geometry = self.read_geometry(hwnd)
            observation = WindowInfo(
                hwnd=hwnd, pid=pid, process_started=identity[1], executable=identity[0],
                class_name=class_name.value, style=int(self._get_long(hwnd, -16)),
                exstyle=int(self._get_long(hwnd, -20)), owner=int(self.user32.GetWindow(hwnd, 4) or 0),
                root=int(self.user32.GetAncestor(hwnd, 2) or 0), rect=geometry.rect,
                visible=geometry.visible and desktop_active,
                minimized=geometry.minimized, cloaked=geometry.cloaked,
            )
            if self.user32.IsWindow(hwnd) and self._pid(hwnd) == pid:
                observations.append(observation)
        return observations

    def foreground(self) -> tuple[int, int]:
        if not self.input_desktop_active():
            return 0, 0
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return 0, 0
        # GA_ROOT deliberately does not walk owner links. A same-process owned
        # dialog is not the bound window and must not keep the copilot visible.
        return int(self.user32.GetAncestor(hwnd, 2) or 0), self._pid(hwnd)

    def open_parent(self, pid: int):
        return self.kernel32.OpenProcess(SYNCHRONIZE, False, pid)

    def parent_alive(self, handle) -> bool:
        return self.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT

    def close_parent(self, handle):
        self.kernel32.CloseHandle(handle)

    def watch_events(self):
        return WinEventMonitor(self.user32)


class WinEventMonitor:
    """Same-thread, external event signals only; no target control or content.

    Microsoft requires a message loop on the registering thread and unhooking
    on that same thread. The held ctypes callback only flips a flag, so native
    callbacks cannot reenter enumeration or publish partially observed frames.
    """

    def __init__(self, user32):
        self.user32 = user32
        self._thread = threading.get_ident()
        self._hooks = []
        self._dirty = True
        self._closed = False

        @WIN_EVENT_PROC
        def changed(_hook, event, _hwnd, object_id, child_id, _thread_id, _time):
            if not self._closed and (event < 0x8000 or (object_id == 0 and child_id == 0)):
                self._dirty = True

        self._callback = changed  # Keep the native callback alive through unhook.
        try:
            for first, last in EVENT_RANGES:
                # OUTOFCONTEXT=0, SKIPOWNPROCESS=2; NULL DLL means no injection.
                hook = user32.SetWinEventHook(first, last, None, changed, 0, 0, 2)
                if not hook:
                    raise OSError('WINDOW_EVENTS_UNAVAILABLE')
                self._hooks.append(hook)
        except Exception:
            self.close()
            raise

    def _same_thread(self):
        if threading.get_ident() != self._thread:
            raise OSError('WINDOW_EVENT_THREAD_INVALID')

    def changed(self):
        self._same_thread()
        if self._closed:
            raise OSError('WINDOW_EVENTS_UNAVAILABLE')
        message = NativeMessage()
        # Bound each drain so an event burst cannot starve parent/EOF checks.
        for _ in range(256):
            if not self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                break
            if message.message == 0x0012:  # WM_QUIT
                raise OSError('WINDOW_EVENTS_STOPPED')
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))
        dirty, self._dirty = self._dirty, False
        return dirty

    def wait(self, seconds):
        self._same_thread()
        milliseconds = max(0, math.ceil(min(seconds, MAX_WAIT_SECONDS) * 1000))
        # QS_ALLINPUT / MWMO_INPUTAVAILABLE wake this helper's message queue.
        result = self.user32.MsgWaitForMultipleObjectsEx(0, None, milliseconds, 0x04FF, 4)
        if result == 0xFFFFFFFF:
            raise OSError('WINDOW_EVENTS_UNAVAILABLE')

    def close(self):
        self._same_thread()
        if self._closed:
            return
        self._closed = True
        failed = False
        for hook in self._hooks:
            try:
                failed = not self.user32.UnhookWinEvent(hook) or failed
            except Exception:
                failed = True
        self._hooks.clear()
        if failed:
            # A failed unhook still ends the helper thread/process; Windows then
            # removes remaining hooks. Keep the callback reference until exit.
            raise OSError('WINDOW_EVENTS_UNAVAILABLE')


class FrameEmitter:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.last_frame = None
        self.last_sent = None
        self.broken = False

    def emit(self, frame: dict, now: float) -> bool:
        if self.broken:
            return False
        if self.last_sent is not None:
            elapsed = now - self.last_sent
            if elapsed < MIN_FRAME_SECONDS:
                return False
            if frame == self.last_frame and elapsed < HEARTBEAT_SECONDS:
                return False
        try:
            self.stream.write(json.dumps(frame, ensure_ascii=True, separators=(",", ":")) + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            self.broken = True
            if self.stream is sys.stdout:
                # Python flushes stdout again at shutdown. Redirect only this
                # helper's broken descriptor so that flush cannot log a second
                # exception or turn clean pipe shutdown into exit code 120.
                try:
                    with open(os.devnull, "w", encoding="utf-8") as sink:
                        os.dup2(sink.fileno(), self.stream.fileno())
                except (OSError, ValueError):
                    sys.stdout = None
            return False
        self.last_frame = frame
        self.last_sent = now
        return True


def wait_for_stdin_eof(stream: TextIO, stopped: threading.Event):
    try:
        while stream.read(1):
            pass
    except (OSError, ValueError):
        pass
    finally:
        stopped.set()


def watch_loop(api, watcher, emitter, stopped, events, *, parent=None, clock=time.monotonic):
    pending, last_sample = True, None
    while not stopped.is_set():
        if parent and not api.parent_alive(parent):
            break
        pending = events.changed() or pending
        now = clock()
        since_sample = float('inf') if last_sample is None else now - last_sample
        since_sent = float('inf') if emitter.last_sent is None else now - emitter.last_sent
        if since_sample >= MIN_FRAME_SECONDS and (pending or since_sample >= FALLBACK_SECONDS
                                                  or since_sent >= HEARTBEAT_SECONDS):
            # Every sample enumerates all candidates: newly opened/shown second
            # windows are detected with the same event latency as target moves.
            emitter.emit(watcher.sample(), now)
            if emitter.broken:
                break
            last_sample, pending = now, False
        due = last_sample + (MIN_FRAME_SECONDS if pending else FALLBACK_SECONDS)
        if emitter.last_sent is not None:
            due = min(due, emitter.last_sent + HEARTBEAT_SECONDS)
        # A heartbeat becoming due between two allowed frames must not cause
        # zero-time message polling until the 30 Hz budget becomes available.
        due = max(due, last_sample + MIN_FRAME_SECONDS)
        events.wait(min(MAX_WAIT_SECONDS, max(0.001, due - clock())))


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError("INVALID_ARGUMENTS")


def _parent_pid(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or value.startswith("0") or len(value) > 10:
        raise ValueError("INVALID_ARGUMENTS")
    pid = int(value)
    if not 0 < pid <= 0xFFFFFFFF:
        raise ValueError("INVALID_ARGUMENTS")
    return pid


def main(argv=None, *, api=None, stdout=None, stdin=None) -> int:
    output = sys.stdout if stdout is None else stdout
    emitter = FrameEmitter(output)
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--parent-pid", type=_parent_pid)
    try:
        args = parser.parse_args(argv)
    except ValueError:
        emitter.emit(unbound_frame("error", "INVALID_ARGUMENTS"), time.monotonic())
        return 2
    if api is None and sys.platform != "win32":
        emitter.emit(unbound_frame("error", "UNSUPPORTED_PLATFORM"), time.monotonic())
        return 2
    parent = events = None
    try:
        api = WindowsAPI() if api is None else api
        if args.parent_pid:
            parent = api.open_parent(args.parent_pid)
            if not parent or not api.parent_alive(parent):
                return 0
        watcher = WindowWatcher(api)
        if args.once:
            emitter.emit(watcher.sample(), time.monotonic())
            return 0 if not emitter.broken else 1
        stopped = threading.Event()
        reader = threading.Thread(target=wait_for_stdin_eof,
                                  args=(sys.stdin if stdin is None else stdin, stopped), daemon=True)
        reader.start()
        if not stopped.is_set():
            events = api.watch_events()
            watch_loop(api, watcher, emitter, stopped, events, parent=parent)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Exception text can contain paths or caller data. Never put it on a
        # desktop pipe or stderr; the fixed protocol code is sufficient.
        emitter.emit(unbound_frame("error", "WINDOW_UNAVAILABLE"), time.monotonic())
        return 1
    finally:
        if events is not None:
            try:
                events.close()
            except Exception:
                pass  # Thread exit releases any native hooks not yet removed.
        if parent:
            api.close_parent(parent)


if __name__ == "__main__":
    raise SystemExit(main())
