"""Synthetic window observations only; never move or inspect a real chat window."""

import ctypes
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
if importlib.util.find_spec("copilot_window_watch"):
    import copilot_window_watch as watch
else:
    watch = None


class FakeAPI:
    def __init__(self, windows=(), foreground=(0, 0)):
        self.observations = list(windows)
        self.active = foreground

    def windows(self):
        return list(self.observations)

    def foreground(self):
        return self.active


def window(**overrides):
    values = dict(hwnd=0x100000042, pid=410, process_started=12345,
                  executable="Weixin.exe", class_name="mmui::MainWindow",
                  style=0x00040000, exstyle=0, owner=0, root=0x100000042,
                  rect=watch.Rect(-900, 80, 800, 700), visible=True,
                  minimized=False, cloaked=False)
    values.update(overrides)
    return watch.WindowInfo(**values)


class WindowWatcherTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(watch, "read-only window watcher is not implemented")

    def test_zero_and_multiple_candidates_fail_closed(self):
        api = FakeAPI()
        monitor = watch.WindowWatcher(api)
        self.assertEqual(monitor.sample()["state"], "waiting")
        api.observations = [window(), window(hwnd=2, root=2, pid=420)]
        frame = monitor.sample()
        self.assertEqual(frame["state"], "ambiguous")
        self.assertEqual(frame["candidate_count"], 2)
        self.assertNotIn("rect", frame)
        self.assertNotIn("target", frame)

    def test_unique_candidate_has_physical_geometry_and_private_protocol(self):
        api = FakeAPI([window()], (0x100000042, 410))
        frame = watch.WindowWatcher(api).sample()
        self.assertEqual(set(frame), {"version", "state", "target", "rect",
                                     "visible", "minimized", "foreground",
                                     "foreground_pid", "candidate_count"})
        self.assertEqual(frame["rect"], {"x": -900, "y": 80, "width": 800, "height": 700})
        self.assertTrue(frame["foreground"])
        self.assertEqual(frame["foreground_pid"], 410)
        self.assertRegex(frame["target"], r"^[0-9a-f]{32}$")
        self.assertNotIn("Weixin", json.dumps(frame))
        self.assertNotIn("MainWindow", json.dumps(frame))

    def test_filter_excludes_hidden_tool_owned_child_small_and_dialog_windows(self):
        base = window()
        rejected = [dict(executable="NotWeixin.exe"), dict(executable="WeChat.exe.bad"),
                    dict(owner=99), dict(root=99), dict(style=0x40040000),
                    dict(exstyle=0x80), dict(exstyle=0x08000000), dict(exstyle=1),
                    dict(class_name="#32770"), dict(class_name="WeChatLoginWndForPC"),
                    dict(class_name="QtDialog"), dict(style=0, class_name="Unknown"),
                    dict(rect=watch.Rect(0, 0, 200, 300)), dict(visible=False),
                    dict(cloaked=True), dict(minimized=True), dict(rect=None)]
        for changes in rejected:
            with self.subTest(changes=changes):
                frame = watch.WindowWatcher(FakeAPI([replace(base, **changes)])).sample()
                self.assertEqual(frame["state"], "waiting")
        for executable in ["WeChat.exe", "WEIXIN.EXE"]:
            frame = watch.WindowWatcher(FakeAPI([replace(base, executable=executable)])).sample()
            self.assertEqual(frame["state"], "bound")

    def test_minimized_invisible_and_cloaked_binding_is_sticky(self):
        api = FakeAPI([window()])
        monitor = watch.WindowWatcher(api)
        first = monitor.sample()
        for changes in [dict(minimized=True, rect=None), dict(visible=False, rect=None),
                        dict(cloaked=True), dict(rect=watch.Rect(0, 0, 200, 200))]:
            with self.subTest(changes=changes):
                api.observations = [window(**changes)]
                frame = monitor.sample()
                self.assertEqual(frame["state"], "bound")
                self.assertEqual(frame["target"], first["target"])
                if changes.get("minimized"):
                    self.assertTrue(frame["minimized"])
                if changes.get("cloaked") or changes.get("visible") is False:
                    self.assertFalse(frame["visible"])
        api.observations = [window()]
        self.assertEqual(monitor.sample()["target"], first["target"])

    def test_visible_window_with_unavailable_geometry_fails_closed(self):
        api = FakeAPI([window()])
        monitor = watch.WindowWatcher(api)
        monitor.sample()
        api.observations = [window(rect=None)]
        frame = monitor.sample()
        self.assertEqual(frame["state"], "error")
        self.assertNotIn("rect", frame)

    def test_bound_window_does_not_switch_to_another_candidate(self):
        api = FakeAPI([window()])
        monitor = watch.WindowWatcher(api)
        first = monitor.sample()
        api.observations = [window(visible=False), window(hwnd=2, root=2, pid=420)]
        self.assertEqual(monitor.sample()["state"], "ambiguous")
        api.observations = [window()]
        self.assertEqual(monitor.sample()["target"], first["target"])

    def test_pid_process_generation_and_handle_invalidation_change_identity(self):
        api = FakeAPI([window()])
        monitor = watch.WindowWatcher(api)
        targets = [monitor.sample()["target"]]
        for changes in [dict(pid=420), dict(process_started=67890)]:
            api.observations = [window(**changes)]
            targets.append(monitor.sample()["target"])
        api.observations = []
        self.assertEqual(monitor.sample()["state"], "waiting")
        api.observations = [window()]
        targets.append(monitor.sample()["target"])
        self.assertEqual(len(set(targets)), len(targets))

    def test_same_process_dialog_is_not_bound_foreground(self):
        frame = watch.WindowWatcher(FakeAPI([window()], (99, 410))).sample()
        self.assertFalse(frame["foreground"])
        self.assertEqual(frame["foreground_pid"], 410)

    def test_changed_frames_are_rate_limited_and_quiet_frames_have_heartbeat(self):
        emitter = watch.FrameEmitter(io.StringIO())
        frame = watch.WindowWatcher(FakeAPI([window()])).sample()
        self.assertTrue(emitter.emit(frame, 0.0))
        self.assertFalse(emitter.emit(frame, 0.25))
        changed = dict(frame, foreground=True)
        self.assertFalse(emitter.emit(changed, 0.01))
        self.assertTrue(emitter.emit(changed, 0.25))
        self.assertFalse(emitter.emit(changed, 0.75))
        self.assertTrue(emitter.emit(changed, 1.0))

    def test_broken_output_pipe_stops_without_exception_details(self):
        class BrokenOutput:
            def write(self, value):
                raise BrokenPipeError("synthetic private path")

        self.assertFalse(watch.FrameEmitter(BrokenOutput()).emit({"version": 1}, 0))

    def test_broken_standard_output_does_not_fail_interpreter_final_flush(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        stream = os.fdopen(write_fd, "w", encoding="utf-8")
        try:
            with patch.object(sys, "stdout", stream):
                emitter = watch.FrameEmitter(stream)
                self.assertFalse(emitter.emit({"version": 1}, 0))
                self.assertTrue(emitter.broken)
                flushed = True
                try:
                    stream.flush()
                except OSError:
                    flushed = False
                self.assertTrue(flushed, "broken standard output must not fail again during interpreter exit")
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def test_stdin_eof_sets_shutdown_event(self):
        stopped = threading.Event()
        watch.wait_for_stdin_eof(io.StringIO("ignored input\n"), stopped)
        self.assertTrue(stopped.is_set())

    def test_once_uses_normal_frame_and_fixed_errors(self):
        output = io.StringIO()
        self.assertEqual(watch.main(["--once"], api=FakeAPI([window()]), stdout=output), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "bound")
        with patch.object(sys, "platform", "linux"):
            output = io.StringIO()
            self.assertEqual(watch.main(["--once"], stdout=output), 2)
            self.assertEqual(json.loads(output.getvalue())["error_code"], "UNSUPPORTED_PLATFORM")
        with patch.object(FakeAPI, "windows", side_effect=RuntimeError("synthetic secret")):
            output = io.StringIO()
            self.assertEqual(watch.main(["--once"], api=FakeAPI(), stdout=output), 1)
            self.assertNotIn("synthetic secret", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["error_code"], "WINDOW_UNAVAILABLE")

    def test_parent_pid_requires_canonical_positive_dword(self):
        for pid in ["0", "-1", "1.5", "4294967296", "arbitrary", "+3", "03"]:
            with self.subTest(pid=pid):
                output = io.StringIO()
                result = watch.main(["--parent-pid", pid], api=FakeAPI(), stdout=output)
                self.assertEqual(result, 2)
                self.assertEqual(json.loads(output.getvalue())["error_code"], "INVALID_ARGUMENTS")

    def test_native_signatures_use_pointer_width_handles(self):
        class Function:
            pass

        class DLL:
            def __getattr__(self, name):
                value = Function()
                setattr(self, name, value)
                return value

        user32, kernel32, dwmapi, shcore = DLL(), DLL(), DLL(), DLL()
        watch.declare_native_api(user32, kernel32, dwmapi, shcore)
        for function in [user32.GetForegroundWindow, user32.GetAncestor, user32.GetWindow,
                         kernel32.OpenProcess]:
            self.assertEqual(ctypes.sizeof(function.restype), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(user32.GetWindowThreadProcessId.argtypes[0], ctypes.c_void_p)
        self.assertEqual(user32.GetWindowLongPtrW.restype, ctypes.c_ssize_t)
        self.assertEqual(dwmapi.DwmGetWindowAttribute.argtypes[0], ctypes.c_void_p)
        self.assertEqual(kernel32.QueryFullProcessImageNameW.argtypes[0], ctypes.c_void_p)
        self.assertEqual(kernel32.WaitForSingleObject.argtypes[0], ctypes.c_void_p)
        self.assertEqual(user32.SetWinEventHook.restype, ctypes.c_void_p)
        self.assertEqual(user32.UnhookWinEvent.argtypes, [ctypes.c_void_p])
        self.assertEqual(user32.DispatchMessageW.restype, ctypes.c_ssize_t)

    def test_read_geometry_prefers_dwm_and_falls_back_to_physical_window_rect(self):
        class User32:
            def GetWindowRect(self, hwnd, rectangle):
                rectangle._obj.left, rectangle._obj.top = 12, 34
                rectangle._obj.right, rectangle._obj.bottom = 512, 434
                return True

            def IsWindowVisible(self, hwnd):
                return True

            def IsIconic(self, hwnd):
                return False

        class DwmAPI:
            fail_bounds = False
            fail_cloak = False
            cloaked = 0

            def DwmGetWindowAttribute(self, hwnd, attribute, value, size):
                if attribute == 9:
                    if self.fail_bounds:
                        return -1
                    value._obj.left, value._obj.top = -1200, 10
                    value._obj.right, value._obj.bottom = -400, 710
                    return 0
                value._obj.value = self.cloaked
                return -1 if self.fail_cloak else 0

        api = watch.WindowsAPI.__new__(watch.WindowsAPI)
        api.user32, api.dwmapi = User32(), DwmAPI()
        self.assertTrue(callable(getattr(api, "read_geometry", None)), "public read-only geometry API is missing")
        geometry = api.read_geometry(0x100000001)
        self.assertEqual(geometry.rect, watch.Rect(-1200, 10, 800, 700))
        self.assertTrue(geometry.visible)
        api.dwmapi.fail_bounds = True
        self.assertEqual(api.read_geometry(1).rect, watch.Rect(12, 34, 500, 400))
        api.dwmapi.fail_cloak = True
        self.assertTrue(api.read_geometry(1).cloaked)

    def test_parent_handle_is_closed_and_no_window_read_after_parent_exit(self):
        class ParentAPI(FakeAPI):
            def __init__(self):
                super().__init__()
                self.closed = []

            def open_parent(self, pid):
                return 0x100000010

            def parent_alive(self, handle):
                return False

            def close_parent(self, handle):
                self.closed.append(handle)

            def windows(self):
                raise AssertionError("must not enumerate after parent death")

        api = ParentAPI()
        output = io.StringIO()
        self.assertEqual(watch.main(["--parent-pid", "410"], api=api, stdout=output), 0)
        self.assertEqual(api.closed, [0x100000010])
        self.assertEqual(output.getvalue(), "")

    def test_eof_watching_process_stops(self):
        self.assertEqual(watch.main([], api=FakeAPI(), stdout=io.StringIO(), stdin=io.StringIO()), 0)

    def test_secure_or_unavailable_input_desktop_suppresses_foreground(self):
        class Kernel32:
            def GetCurrentThreadId(self):
                return 12

        class User32:
            active = False
            success = True

            def GetThreadDesktop(self, thread_id):
                return 0x100000003

            def GetUserObjectInformationW(self, handle, index, value, size, needed):
                self.assert_index = index
                value._obj.value = self.active
                return self.success

            def GetForegroundWindow(self):
                return 0x100000042

            def GetAncestor(self, hwnd, flag):
                return hwnd

            def GetWindowThreadProcessId(self, hwnd, pid):
                pid._obj.value = 410
                return 12

        api = watch.WindowsAPI.__new__(watch.WindowsAPI)
        api.user32, api.kernel32 = User32(), Kernel32()
        self.assertEqual(api.foreground(), (0, 0))
        api.user32.active = True
        self.assertEqual(api.foreground(), (0x100000042, 410))
        api.user32.success = False
        self.assertEqual(api.foreground(), (0, 0))

    def test_external_event_hook_lifetime_and_content_free_callback(self):
        self.assertTrue(hasattr(watch, 'WinEventMonitor'), 'external geometry event monitor is missing')
        native = FakeEventUser32()
        events = watch.WinEventMonitor(native)
        self.assertGreaterEqual(len(native.registrations), 5)
        for args in native.registrations:
            self.assertIsNone(args[2])
            self.assertEqual(args[4:7], (0, 0, 2), 'OUTOFCONTEXT and SKIPOWNPROCESS only')
        self.assertTrue(events.changed())
        self.assertFalse(events.changed())
        callback = native.registrations[0][3]
        callback(None, 0x800B, 42, -4, 0, 1, 0)  # Client text/control changes are ignored.
        self.assertFalse(events.changed())
        callback(None, 0x800B, 42, 0, 0, 1, 0)
        callback(None, 0x800B, 42, 0, 0, 1, 0)
        self.assertTrue(events.changed())
        self.assertFalse(events.changed(), 'bursts are coalesced into one boolean signal')
        events.close()
        self.assertEqual(set(native.removed), set(range(1, len(native.registrations) + 1)))
        events.close()
        self.assertEqual(len(native.removed), len(native.registrations))

    def test_failed_hook_registration_releases_partial_hooks(self):
        self.assertTrue(hasattr(watch, 'WinEventMonitor'), 'external geometry event monitor is missing')
        native = FakeEventUser32(fail_at=3)
        with self.assertRaises(OSError):
            watch.WinEventMonitor(native)
        self.assertEqual(set(native.removed), {1, 2})

    def test_event_wait_is_bounded_and_native_failure_stops_monitoring(self):
        self.assertTrue(hasattr(watch, 'WinEventMonitor'), 'external geometry event monitor is missing')
        native = FakeEventUser32()
        events = watch.WinEventMonitor(native)
        try:
            events.wait(0.033)
            self.assertEqual(native.wait_args[:2], (0, None))
            self.assertLessEqual(native.wait_args[2], 50)
            self.assertEqual(native.wait_args[-1], 4)
            native.wait_result = 0xFFFFFFFF
            with self.assertRaises(OSError):
                events.wait(0.033)
        finally:
            events.close()

    def test_event_loop_updates_geometry_and_second_candidate_within_a_frame_budget(self):
        self.assertTrue(hasattr(watch, 'watch_loop'), 'event-coalescing watch loop is missing')
        now = [0.0]
        api = FakeAPI([window()])
        changes = [(0.04, [window(rect=watch.Rect(-850, 80, 800, 700))]),
                   (0.05, [window(rect=watch.Rect(-800, 80, 800, 700))]),
                   (0.09, [window(), window(hwnd=2, root=2, pid=420)])]
        stopped = threading.Event()
        events = FakeEventClock(now, api, stopped, changes, end=0.20)
        output = io.StringIO()
        emitter = watch.FrameEmitter(output)
        sample_times = []
        original = api.windows
        def windows():
            sample_times.append(now[0])
            return original()
        with patch.object(api, 'windows', side_effect=windows):
            watch.watch_loop(api, watch.WindowWatcher(api), emitter, stopped, events, clock=lambda: now[0])
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(frames[0]['rect']['x'], -900)
        self.assertTrue(any(frame.get('rect', {}).get('x') == -850 for frame in frames))
        self.assertEqual(frames[-1]['state'], 'ambiguous')
        self.assertEqual(frames[-1]['candidate_count'], 2)
        self.assertTrue(any(0.09 <= stamp <= 0.124 for stamp in sample_times), sample_times)
        self.assertTrue(all(b - a >= watch.MIN_FRAME_SECONDS - 1e-8 for a, b in zip(sample_times, sample_times[1:])))

    def test_lost_events_still_recheck_all_candidates_within_half_a_second(self):
        self.assertTrue(hasattr(watch, 'watch_loop'), 'event-coalescing watch loop is missing')
        now = [0.0]
        api = FakeAPI([window()])
        changes = [(0.1, [window(), window(hwnd=2, root=2, pid=420)])]
        stopped = threading.Event()
        events = FakeEventClock(now, api, stopped, changes, end=0.60, notify=False)
        output = io.StringIO()
        sample_times = []
        def windows():
            sample_times.append(now[0])
            return list(api.observations)
        with patch.object(api, 'windows', side_effect=windows):
            watch.watch_loop(api, watch.WindowWatcher(api), watch.FrameEmitter(output), stopped, events, clock=lambda: now[0])
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(frames[-1]['state'], 'ambiguous')
        self.assertLessEqual(sample_times[-1], 0.51)

    def test_event_monitor_is_closed_when_watch_loop_fails_without_leaking_details(self):
        self.assertTrue(hasattr(watch, 'watch_loop'), 'event-coalescing watch loop is missing')
        class EventAPI(FakeAPI):
            def watch_events(self):
                return events
        class Events:
            closed = False
            def close(self):
                self.closed = True
        events = Events()
        output = io.StringIO()
        with patch.object(watch.threading, 'Thread'), patch.object(watch, 'watch_loop', side_effect=RuntimeError('PRIVATE_EVENT_ERROR')):
            self.assertEqual(watch.main([], api=EventAPI(), stdout=output, stdin=io.StringIO()), 1)
        self.assertTrue(events.closed)
        self.assertNotIn('PRIVATE_EVENT_ERROR', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['error_code'], 'WINDOW_UNAVAILABLE')

    def test_pending_heartbeat_does_not_spin_before_frame_rate_limit(self):
        self.assertTrue(hasattr(watch, 'watch_loop'), 'event-coalescing watch loop is missing')
        now = [0.0]
        api = FakeAPI([window()])
        stopped = threading.Event()
        events = FakeEventClock(now, api, stopped, [(0.74, [window()])], end=0.81)
        output = io.StringIO()
        watch.watch_loop(api, watch.WindowWatcher(api), watch.FrameEmitter(output), stopped, events, clock=lambda: now[0])
        self.assertLess(len(events.waits), 30, 'a due heartbeat must still wait for the next allowed frame')
        self.assertTrue(all(delay > 0 for delay in events.waits))

    def test_stopped_or_dead_parent_does_not_pump_events_or_sample_windows(self):
        self.assertTrue(hasattr(watch, 'watch_loop'), 'event-coalescing watch loop is missing')
        stopped = threading.Event()
        stopped.set()
        api = FakeAPI()
        class NoEvents:
            def changed(self):
                raise AssertionError('must not pump after termination')
        with patch.object(api, 'windows', side_effect=AssertionError('must not read after termination')):
            watch.watch_loop(api, watch.WindowWatcher(api), watch.FrameEmitter(io.StringIO()), stopped, NoEvents())
            stopped.clear()
            api.parent_alive = lambda handle: False
            watch.watch_loop(api, watch.WindowWatcher(api), watch.FrameEmitter(io.StringIO()), stopped, NoEvents(), parent=42)

    def test_hook_close_continues_after_one_unhook_failure(self):
        self.assertTrue(hasattr(watch, 'WinEventMonitor'), 'external geometry event monitor is missing')
        native = FakeEventUser32()
        events = watch.WinEventMonitor(native)
        original = native.UnhookWinEvent
        def unhook(handle):
            original(handle)
            return handle != 1
        native.UnhookWinEvent = unhook
        with self.assertRaises(OSError):
            events.close()
        self.assertEqual(len(native.removed), len(native.registrations))


class FakeEventUser32:
    def __init__(self, fail_at=None):
        self.registrations = []
        self.removed = []
        self.fail_at = fail_at
        self.wait_args = None
        self.wait_result = 258

    def SetWinEventHook(self, *args):
        self.registrations.append(args)
        return 0 if len(self.registrations) == self.fail_at else len(self.registrations)

    def UnhookWinEvent(self, handle):
        self.removed.append(handle)
        return True

    def PeekMessageW(self, *_args):
        return False

    def MsgWaitForMultipleObjectsEx(self, *args):
        self.wait_args = args
        return self.wait_result


class FakeEventClock:
    def __init__(self, now, api, stopped, changes, *, end, notify=True):
        self.now, self.api, self.stopped = now, api, stopped
        self.changes, self.end, self.notify = list(changes), end, notify
        self.dirty = True
        self.waits = []

    def changed(self):
        dirty, self.dirty = self.dirty, False
        return dirty

    def wait(self, seconds):
        self.waits.append(seconds)
        delay = min(seconds, self.changes[0][0] - self.now[0]) if self.changes else seconds
        self.now[0] += max(delay, 0.000001)
        while self.changes and self.changes[0][0] <= self.now[0] + 1e-9:
            self.api.observations = self.changes.pop(0)[1]
            self.dirty = self.notify
        if self.now[0] >= self.end:
            self.stopped.set()


if __name__ == "__main__":
    unittest.main()
