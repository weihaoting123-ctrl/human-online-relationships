"""Fixed local desktop entrypoint, independent of archives and AI configuration.

Only an authenticated route supplies the server-owned project root and port.
Opening the app accepts a request; it cannot establish that a window is visible.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time


_CREATE_NO_WINDOW = 0x08000000
_COOLDOWN_SECONDS = 10.0
_PROBE_TIMEOUT_SECONDS = 4.0
_LOCK = threading.Lock()
_ATTEMPTS = {}
_CHILDREN = []
_PROBE_SCRIPT = Path(__file__).absolute().with_name('desktop_probe.ps1')


class _UnsafePath(ValueError):
    pass


def _reply(state, code, *, supported=True, installed=True):
    return {'status': 'ok', 'desktop': {'supported': supported, 'installed': installed,
                                      'state': state, 'code': code}}


def _checked_path(path):
    """Reject links/reparse points without first resolving away their evidence."""
    path = Path(path).absolute()
    for item in reversed((path, *path.parents)):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise _UnsafePath()
    return path


def _installation(project_root, port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid local desktop port')
    if sys.platform != 'win32':
        return None, _reply('unavailable', 'DESKTOP_UNSUPPORTED', supported=False, installed=False)
    root = Path(project_root).absolute()
    main = root / 'desktop' / 'copilot'
    exe = main / 'node_modules' / 'electron' / 'dist' / 'electron.exe'
    try:
        for target in (exe, main / 'main.cjs', main / 'package.json'):
            if not _checked_path(target).is_file():
                raise FileNotFoundError()
    except _UnsafePath:
        return None, _reply('unavailable', 'DESKTOP_UNSAFE_PATH', installed=False)
    except (OSError, ValueError):
        return None, _reply('unavailable', 'DESKTOP_NOT_INSTALLED', installed=False)
    return (root, main, exe), None


def _environment():
    # Node/Electron flags inherited from a development shell must not alter the
    # fixed invocation. Keep only OS/runtime essentials, never request fields.
    allowed = {'systemroot', 'windir', 'temp', 'tmp', 'userprofile', 'appdata',
               'localappdata', 'path', 'pathext', 'systemdrive'}
    return {key: value for key, value in os.environ.items() if key.lower() in allowed}


def status(project_root, port):
    """Read only installation metadata and this exact executable/app's state."""
    paths, error = _installation(project_root, port)
    if error:
        return error
    root, main, exe = paths
    try:
        probe = _checked_path(_PROBE_SCRIPT)
        # Do not resolve powershell.exe from the current directory or PATH.
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        result = subprocess.run(
            [str(shell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(probe), '-Executable', str(exe), '-Main', str(main), '-Port', str(port)],
            cwd=str(root), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW, env=_environment(),
            text=True, encoding='ascii', errors='replace', timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or len(result.stdout) > 32 or value not in {'running', 'stopped', 'unknown'}:
            return _reply('unknown', 'DESKTOP_PROBE_FAILED')
        return _reply(value, 'DESKTOP_STATUS_UNKNOWN' if value == 'unknown' else 'DESKTOP_' + value.upper())
    except subprocess.TimeoutExpired:
        return _reply('unknown', 'DESKTOP_PROBE_TIMEOUT')
    except (OSError, ValueError, RuntimeError):
        return _reply('unknown', 'DESKTOP_PROBE_FAILED')


def launch(project_root, port):
    """Open the fixed app once per explicit request, single-flight with cooldown."""
    paths, error = _installation(project_root, port)
    if error:
        return error
    root, main, exe = paths
    if not _LOCK.acquire(blocking=False):
        return _reply('launch_requested', 'DESKTOP_LAUNCH_PENDING')
    try:
        now = time.monotonic()
        key = str(root).casefold()
        previous = _ATTEMPTS.get(key)
        if previous and now < previous[0]:
            return (_reply('launch_requested', 'DESKTOP_LAUNCH_PENDING')
                    if previous[1]['desktop']['state'] == 'launch_requested' else previous[1])
        # Retain/reap only processes that this service itself created. Never
        # inspect or terminate unrelated processes as part of a launch.
        _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]
        for candidate, (deadline, _) in list(_ATTEMPTS.items()):
            if deadline <= now:
                _ATTEMPTS.pop(candidate, None)
        result = _reply('unknown', 'DESKTOP_LAUNCH_FAILED')
        _ATTEMPTS[key] = (now + _COOLDOWN_SECONDS, result)
        try:
            # Recheck immediately before CreateProcess to reduce the window
            # for an installation directory being replaced with a reparse point.
            paths, error = _installation(root, port)
            if error:
                result = error
            else:
                child = subprocess.Popen(
                    [str(exe), str(main), f'--port={port}', '--show'],
                    cwd=str(root), shell=False, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=_CREATE_NO_WINDOW, env=_environment(),
                )
                try:
                    code = child.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    _CHILDREN.append(child)
                    code = None
                if code in (0, None):
                    result = _reply('launch_requested', 'DESKTOP_LAUNCH_REQUESTED')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass  # Neither child output nor exception/path details leave here.
        _ATTEMPTS[key] = (time.monotonic() + _COOLDOWN_SECONDS, result)
        return result
    finally:
        _LOCK.release()
