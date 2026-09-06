"""Wait for a future WeChat login, cache its key locally, then sync.

The watcher never closes or restarts WeChat.  It leaves an already-running
session alone and only invokes the pinned, temporary process-injection key
observer after every previous WeChat process has exited and a fresh process
appears.  Captured key material is immediately validated, protected with
Windows DPAPI, and never printed or placed in a command line.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes
from datetime import timedelta
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_all_wechat as sync  # noqa: E402


WATCH_LOCK_PATH = sync.PRIVATE_ROOT / "watcher.lock"


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


@contextmanager
def _watch_lock():
    WATCH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = WATCH_LOCK_PATH.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        yield True
    finally:
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _process_age_seconds(pid: int) -> float | None:
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    created = _FileTime()
    exited = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (int(created.high) << 32) | int(created.low)
        created_unix = ticks / 10_000_000 - 11_644_473_600
        return max(0.0, time.time() - created_unix)
    finally:
        kernel32.CloseHandle(handle)


def _safe_status(state: str, error_code: str | None = None) -> dict:
    previous = sync._read_json(sync.STATUS_PATH, {})
    status = sync._base_status("scheduled")
    if isinstance(previous, dict):
        for key in sync.PUBLIC_STATUS_FIELDS:
            if key in previous:
                status[key] = previous[key]
    status.update({
        "enabled": True,
        "state": state,
        "mode": "scheduled",
        "next_run_at": sync._iso(sync._utc_now() + timedelta(hours=6)),
        "error_code": error_code,
        "attention": (
            "等待微信下次正常启动时完成一次性本地密钥初始化。"
            if state == "awaiting_wechat_restart"
            else "本地微信初始化需要处理。"
        ),
        "original_wechat_untouched": True,
        "raw_snapshots_retained": True,
    })
    status.pop("internal_error_code", None)
    sync._write_status(status)
    return status


def _handle_captured_key(passphrase: str, expected_account_id: str) -> int:
    try:
        sync.cache_captured_passphrase(
            passphrase,
            expected_account_id=expected_account_id,
        )
        status = sync.run_sync(scheduled=True, rescan_keys=False)
    except sync.SyncFailure as exc:
        state = (
            "needs_account_selection"
            if exc.code == "needs_account_selection"
            else "error"
        )
        status = _safe_status(state, sync._dashboard_error_code(exc.code))
    except Exception:
        status = _safe_status("error", "sync_failed")
    print(json.dumps(sync.public_status(status), ensure_ascii=False))
    return 0 if status.get("state") == "completed" else 1


def watch(wait_hours: int) -> int:
    with _watch_lock() as acquired:
        if not acquired:
            return 0

        # Defense in depth for manual launches: once a complete DPAPI cache is
        # available, never arm login-time observation again.
        if sync.has_usable_key_cache():
            return 0

        _safe_status("awaiting_wechat_restart")
        deadline = time.monotonic() + wait_hours * 3600
        ordered_processes = sync._weixin_pids()
        baseline = set(ordered_processes)

        # A watcher launched at logon may race with a newly started WeChat.
        # Only a genuinely fresh process is eligible; an established session
        # is never touched.
        fresh = []
        for pid in ordered_processes:
            age = _process_age_seconds(pid)
            if age is not None and age <= 20:
                fresh.append(pid)
        armed = not baseline
        pending = fresh
        poll_delay = 1.0

        while time.monotonic() < deadline:
            if pending:
                # Another local sync may have completed while this watcher was
                # waiting.  Recheck immediately before every real Hook so a
                # complete cache permanently disarms observation.
                if sync.has_usable_key_cache():
                    return 0
                try:
                    expected_account_id, _db_root = sync.discover_unique_account()
                except sync.SyncFailure as exc:
                    state = (
                        "needs_account_selection"
                        if exc.code == "needs_account_selection"
                        else "error"
                    )
                    _safe_status(state, sync._dashboard_error_code(exc.code))
                    return 1
                except Exception:
                    _safe_status("error", "sync_failed")
                    return 1
                passphrase = sync._capture_hook_key(
                    pending,
                    timeout_per_pid_seconds=120,
                )
                if passphrase:
                    return _handle_captured_key(passphrase, expected_account_id)
                baseline = set(sync._weixin_pids())
                armed = False
                pending = []

            ordered_processes = sync._weixin_pids()
            current = set(ordered_processes)
            previous = baseline
            if not current:
                armed = True
                baseline = set()
            elif armed:
                pending = ordered_processes
                armed = False
            elif baseline and not (baseline & current):
                pending = ordered_processes
            if current != previous:
                poll_delay = 1.0
            else:
                poll_delay = min(5.0, poll_delay * 1.5)
            time.sleep(poll_delay)

        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="等待下次微信登录并完成本地只读同步"
    )
    parser.add_argument(
        "--wait-hours",
        type=int,
        default=168,
        choices=range(1, 721),
    )
    args = parser.parse_args(argv)
    return watch(args.wait_hours)


if __name__ == "__main__":
    raise SystemExit(main())
