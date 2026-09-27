"""Bounded local-only reader process protocol; never reflect child output/errors."""
from __future__ import annotations

import json
import atexit
import os
from pathlib import Path
import re
import queue
import subprocess
import sys
import threading
from datetime import datetime

from dashboard import analysis as ai


MAX_OUTPUT_BYTES = 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]
HEX = re.compile(r'[a-f0-9]{64}')
SAFE_CODES = frozenset({'LIVE_INVALID_REQUEST', 'LIVE_IDENTITY_UNAVAILABLE', 'LIVE_IDENTITY_CHANGED',
    'LIVE_ACCOUNT_UNAVAILABLE', 'LIVE_KEY_UNAVAILABLE', 'LIVE_BUSY', 'LIVE_SOURCE_UNAVAILABLE',
    'LIVE_SOURCE_BUSY', 'LIVE_DECODE_FAILED', 'LIVE_SENDER_UNKNOWN', 'LIVE_OVERFLOW', 'LIVE_READ_FAILED'})


class LiveReadError(ai.AnalysisError):
    def __init__(self, code='LIVE_READ_FAILED'):
        self.code = code if code in SAFE_CODES else 'LIVE_READ_FAILED'
        super().__init__('实时读取不可用，已停止；不会自动重试')


def _stamp(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 40 or 'T' not in value:
        raise ValueError
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not 2000 <= stamp.year <= 2100:
        raise ValueError
    return stamp.timestamp()


def validate_tail(value):
    """Strict validation is also used after injected readers in service tests."""
    try:
        if (not isinstance(value, dict) or set(value) not in (
                {'ok', 'identity', 'rows', 'cursor', 'observed_at'},
                {'ok', 'identity', 'rows', 'cursor', 'observed_at', 'unchanged'})
                or value['ok'] is not True or not isinstance(value['identity'], str)
                or not HEX.fullmatch(value['identity'])):
            raise ValueError
        cursor = value['cursor']
        if (not isinstance(cursor, dict) or type(cursor.get('version')) is not int
                or cursor['version'] not in (1, 2)
                or set(cursor) != ({'version', 'identity', 'digest', 'source_digest'} if cursor['version'] == 2
                                   else {'version', 'identity', 'digest'})
                or cursor['identity'] != value['identity']
                or not isinstance(cursor['digest'], str) or not HEX.fullmatch(cursor['digest'])
                or (cursor['version'] == 2 and (not isinstance(cursor['source_digest'], str)
                                                or not HEX.fullmatch(cursor['source_digest'])))):
            raise ValueError
        _stamp(value['observed_at'])
        rows = value['rows']
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError
        if 'unchanged' in value and (value['unchanged'] is not True or rows or cursor['version'] != 2):
            raise ValueError
        ids, chars, last = set(), 0, float('-inf')
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'id', 'server_id', 'timestamp', 'sender', 'text'}:
                raise ValueError
            for key in ('id', 'server_id'):
                if (not isinstance(row[key], str) or not 1 <= len(row[key]) <= 256
                        or any(ord(char) < 32 for char in row[key])):
                    raise ValueError
            if row['id'] in ids:
                raise ValueError
            ids.add(row['id'])
            if (row['sender'] not in ('me', 'peer') or not isinstance(row['text'], str)
                    or not row['text'].strip() or len(row['text']) > 4000):
                raise ValueError
            chars += len(row['text'])
            stamp = _stamp(row['timestamp'])
            if stamp < last or chars > 40000:
                raise ValueError
            last = stamp
        return value
    except Exception:
        raise LiveReadError() from None


class _ReaderWorker:
    def __init__(self, bundle_id):
        self.bundle_id = bundle_id
        self.closed = False
        self.responses = queue.Queue(maxsize=1)
        self.child = subprocess.Popen(
            [sys.executable, '-X', 'utf8', str(ROOT / 'scripts' / 'copilot_live_read.py'), '--serve'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=ROOT, shell=False, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        threading.Thread(target=self._receive, name='copilot-reader-output', daemon=True).start()

    def _receive(self):
        try:
            while not self.closed:
                raw = self.child.stdout.readline(MAX_OUTPUT_BYTES + 1)
                self.responses.put(raw, timeout=1)
                if not raw or len(raw) > MAX_OUTPUT_BYTES:
                    return
        except Exception:
            try:
                self.responses.put_nowait(b'')
            except queue.Full:
                pass

    def request(self, previous):
        if self.closed or self.child.poll() is not None:
            raise LiveReadError()
        request = {'bundle_id': self.bundle_id}
        if previous is not None:
            request['previous'] = previous
        raw = (json.dumps(request, ensure_ascii=True) + '\n').encode('utf-8')
        if len(raw) > 16384:
            raise LiveReadError('LIVE_INVALID_REQUEST')
        if self.child.stdin.write(raw) != len(raw):
            raise LiveReadError()
        self.child.stdin.flush()
        raw = self.responses.get(timeout=90)
        if self.closed or not raw or len(raw) > MAX_OUTPUT_BYTES or not raw.endswith(b'\n'):
            raise LiveReadError()
        response = json.loads(raw.decode('utf-8'))
        if isinstance(response, dict) and set(response) == {'ok', 'code'} and response['ok'] is False:
            raise LiveReadError(response['code'])
        return validate_tail(response)

    def close(self):
        if self.closed:
            return
        self.closed = True
        # EOF is cooperative: the worker finishes its current bounded read,
        # deletes its own temporary copies, then exits. Stop never waits on it.
        try:
            self.child.stdin.close()
        except OSError:
            pass
        def reap():
            try:
                self.child.wait(timeout=95)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
            finally:
                self.child.stdout.close()
        threading.Thread(target=reap, name='copilot-reader-cleanup', daemon=True).start()


_STATE_LOCK = threading.Lock()
_READ_LOCK = threading.Lock()
_WORKER = None


def release_reader(bundle_id=None):
    global _WORKER
    with _STATE_LOCK:
        if _WORKER is not None and (bundle_id is None or _WORKER.bundle_id == bundle_id):
            worker, _WORKER = _WORKER, None
            worker.close()


atexit.register(release_reader)


def read_tail(bundle_id, previous):
    """One fixed private worker per grant. Death/error never silently restarts it."""
    global _WORKER
    if not _READ_LOCK.acquire(blocking=False):
        raise LiveReadError('LIVE_BUSY')
    worker = None
    try:
        with _STATE_LOCK:
            if previous is None:
                if _WORKER is not None:
                    _WORKER.close()
                _WORKER = _ReaderWorker(bundle_id)
            worker = _WORKER
            if worker is None or worker.bundle_id != bundle_id:
                raise LiveReadError('LIVE_IDENTITY_CHANGED')
        return worker.request(previous)
    except Exception as error:
        with _STATE_LOCK:
            if worker is not None:
                worker.close()
                if _WORKER is worker:
                    _WORKER = None
        if isinstance(error, LiveReadError):
            raise
        raise LiveReadError() from None
    finally:
        _READ_LOCK.release()
