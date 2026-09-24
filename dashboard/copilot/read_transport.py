"""Bounded local-only reader process protocol; never reflect child output/errors."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
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
        if (not isinstance(value, dict) or set(value) != {'ok', 'identity', 'rows', 'cursor', 'observed_at'}
                or value['ok'] is not True or not isinstance(value['identity'], str)
                or not HEX.fullmatch(value['identity'])):
            raise ValueError
        cursor = value['cursor']
        if (not isinstance(cursor, dict) or set(cursor) != {'version', 'identity', 'digest'}
                or type(cursor['version']) is not int or cursor['version'] != 1
                or cursor['identity'] != value['identity']
                or not isinstance(cursor['digest'], str) or not HEX.fullmatch(cursor['digest'])):
            raise ValueError
        _stamp(value['observed_at'])
        rows = value['rows']
        if not isinstance(rows, list) or len(rows) > 200:
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


def read_tail(bundle_id, previous):
    """Invoke only the fixed project helper, with no shell or diagnostic logs."""
    request = {'bundle_id': bundle_id}
    if previous is not None:
        request['previous'] = previous
    try:
        child = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts' / 'copilot_live_read.py')],
            input=json.dumps(request, ensure_ascii=True).encode('utf-8'), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=ROOT, timeout=90, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if not isinstance(child.stdout, bytes) or len(child.stdout) > MAX_OUTPUT_BYTES:
            raise ValueError
        response = json.loads(child.stdout.decode('utf-8'))
        if isinstance(response, dict) and set(response) == {'ok', 'code'} and response['ok'] is False:
            raise LiveReadError(response['code'])
        if child.returncode != 0:
            raise ValueError
        return validate_tail(response)
    except LiveReadError:
        raise
    except Exception:
        raise LiveReadError() from None
