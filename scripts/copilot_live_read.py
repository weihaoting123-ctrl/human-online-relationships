"""Private-pipe, selected-contact text reader. Never an exporter or cloud caller.

Only the fixed CLI project root is used in production. Run in a dedicated,
single-flight subprocess with a parent hard timeout of at most 90 seconds.
The cooperative deadline cannot interrupt an OS file-copy operation. Cadence
is best effort, not a claim of real-time or complete historical reconciliation.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import codecs
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

MAX_ROWS = 200
MAX_TEXT_CHARS = 4000
MAX_TOTAL_CHARS = 40000
MAX_ENCODED_BYTES = 65536
MAX_SHARDS = 64
MAX_IDENTITY_HEADER_BYTES = 65536
MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
READ_TIMEOUT_SECONDS = 80
HEX64 = re.compile(r'^[0-9a-f]{64}$')
SHARD_NAME = re.compile(r'^message/message_\d+\.db$', re.I)
SAFE_CODES = frozenset({
    'LIVE_INVALID_REQUEST', 'LIVE_IDENTITY_UNAVAILABLE', 'LIVE_IDENTITY_CHANGED',
    'LIVE_ACCOUNT_UNAVAILABLE', 'LIVE_KEY_UNAVAILABLE', 'LIVE_BUSY',
    'LIVE_SOURCE_UNAVAILABLE', 'LIVE_SOURCE_BUSY', 'LIVE_DECODE_FAILED',
    'LIVE_SENDER_UNKNOWN', 'LIVE_OVERFLOW', 'LIVE_READ_FAILED',
})


class LiveReadError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in SAFE_CODES else 'LIVE_READ_FAILED'
        super().__init__(self.code)


def _load_sync(project_root: Path):
    path = project_root / 'scripts' / 'sync_all_wechat.py'
    spec = importlib.util.spec_from_file_location('_copilot_live_sync', path)
    if spec is None or spec.loader is None:
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Do not inherit a different data-root override in this fixed-project worker.
    if module.DATA_ROOT != project_root / 'data':
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    return module


def _check_request(bundle_id, previous):
    if (not isinstance(bundle_id, str) or not bundle_id or len(bundle_id) > 240
            or bundle_id in {'.', '..'} or any(c in bundle_id for c in '/\\:\x00')
            or any(ord(c) < 32 for c in bundle_id)):
        raise LiveReadError('LIVE_INVALID_REQUEST')
    if previous is not None and (
        not isinstance(previous, dict) or set(previous) != {'version', 'identity', 'digest'}
        or type(previous.get('version')) is not int or previous['version'] != 1
        or not isinstance(previous.get('identity'), str) or not HEX64.fullmatch(previous['identity'])
        or not isinstance(previous.get('digest'), str) or not HEX64.fullmatch(previous['digest'])
    ):
        raise LiveReadError('LIVE_INVALID_REQUEST')


def _checked_path(root: Path, *parts: str) -> Path:
    current = root
    for part in parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
    if not current.resolve().is_relative_to(root):
        raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
    return current


def _object(path: Path, limit: int):
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise LiveReadError('LIVE_OVERFLOW')
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
    return value


def _identity_header(path: Path) -> dict:
    """Read native scalar metadata preceding messages, never parse its array.

    Native converters place identity before messages. Other layouts fail closed;
    this is not a general JSON importer and does not search historical bodies.
    """
    decoder = json.JSONDecoder()
    with path.open('rb') as handle:
        # A bounded prefix can end midway through a UTF-8 character in the
        # unread messages body. Do not turn that benign boundary into an error.
        text = codecs.getincrementaldecoder('utf-8-sig')().decode(handle.read(MAX_IDENTITY_HEADER_BYTES), final=False)
    position = 0
    result = {}
    def whitespace(index):
        while index < len(text) and text[index] in ' \t\r\n':
            index += 1
        return index
    position = whitespace(position)
    if position >= len(text) or text[position] != '{':
        raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
    position += 1
    while True:
        position = whitespace(position)
        key, position = decoder.raw_decode(text, position)
        if not isinstance(key, str) or key in result:
            raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
        position = whitespace(position)
        if position >= len(text) or text[position] != ':':
            raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
        position = whitespace(position + 1)
        if key == 'messages':
            if position >= len(text) or text[position] != '[':
                raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
            return result
        value, position = decoder.raw_decode(text, position)
        if isinstance(value, (dict, list)):
            raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
        result[key] = value
        position = whitespace(position)
        if position >= len(text) or text[position] != ',':
            raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
        position += 1


def _identity(root, bundle_id, sync, account):
    bundle = _checked_path(root, 'data', 'contacts', bundle_id)
    manifest = _object(_checked_path(bundle, 'dashboard_manifest.json'), 65536)
    header = _identity_header(_checked_path(bundle, 'messages.json'))
    peer, owner = header.get('contact_username'), header.get('own_wxid')
    if (manifest.get('source') != 'wechat-local-readonly' or manifest.get('conversation_kind') != 'direct'
            or header.get('source') != 'weflow-cli' or not isinstance(peer, str)
            or not isinstance(owner, str) or owner in {'', 'unknown'} or owner != account
            or peer in {'', 'unknown'} or not sync._safe_session_id(peer) or '@chatroom' in peer
            or sync._is_self_sender(peer, owner)):
        raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
    checkpoint = _object(_checked_path(root, 'data', 'private', 'wechat-sync', 'incremental-checkpoint.json'), MAX_CHECKPOINT_BYTES)
    bundles = checkpoint.get('bundles')
    matching = bundles.get(peer) if isinstance(bundles, dict) else None
    if (checkpoint.get('version') != 1 or checkpoint.get('account_hash') != sync._account_hash(account)
            or not isinstance(matching, dict) or matching.get('bundle_name') != bundle_id
            or sum(isinstance(v, dict) and v.get('bundle_name') == bundle_id for v in bundles.values()) != 1):
        raise LiveReadError('LIVE_IDENTITY_UNAVAILABLE')
    identity = hashlib.sha256(json.dumps([owner, peer], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return peer, identity


def _deadline(deadline):
    if time.monotonic() >= deadline:
        raise LiveReadError('LIVE_SOURCE_BUSY')


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 946684800 <= value <= 4102444800:
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _message_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 63:
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    return str(value)


def _recent_rows(sync, reader, records, peer, account, deadline):
    table = 'Msg_' + hashlib.md5(peer.encode()).hexdigest()
    shards = [record for record in records if SHARD_NAME.fullmatch(str(record.get('name') or ''))]
    if not shards:
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    if len(shards) > MAX_SHARDS:
        raise LiveReadError('LIVE_OVERFLOW')
    candidates = []
    record_by_partition = {}
    found_table = False
    for record in shards:
        _deadline(deadline)
        partition = sync._source_partition_token(record)
        if partition in record_by_partition:
            raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
        record_by_partition[partition] = record
        with closing(sync._open_readonly_database(reader, record)) as connection:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            cursor = connection.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE", (table,))
            if cursor.fetchone() is None:
                continue
            found_table = True
            cursor.execute('PRAGMA table_info(Name2Id)')
            columns = {str(row[1]) for row in cursor.fetchall()}
            name_column = next((name for name in ('user_name', 'username') if name in columns), None)
            if name_column is None:
                raise LiveReadError('LIVE_SENDER_UNKNOWN')
            # Only selected-table metadata, never other conversation bodies.
            cursor.execute(f'''SELECT m.local_id,m.server_id,m.create_time,n."{name_column}"
                FROM "{table}" AS m LEFT JOIN Name2Id AS n ON n.rowid=m.real_sender_id
                WHERE m.local_type=1 ORDER BY m.create_time DESC,m.local_id DESC LIMIT ?''', (MAX_ROWS,))
            for local_id, server_id, timestamp, sender in cursor.fetchall():
                row_id = partition + ':' + _message_id(local_id)
                iso = _timestamp(timestamp)
                candidates.append({'id': row_id, 'server_id': str(server_id or '0'), 'timestamp': iso,
                                   '_time': timestamp, '_local_id': local_id, '_partition': partition, '_sender': sender})
        candidates = sorted(candidates, key=lambda row: (row['_time'], row['_local_id'], row['_partition']))[-MAX_ROWS:]
    if not found_table:
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    selected = {row['id']: row for row in candidates}
    if len(selected) != len(candidates):
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    for row in candidates:
        sender = row.pop('_sender')
        if sender == peer:
            row['sender'] = 'peer'
        elif isinstance(sender, str) and sync._is_self_sender(sender, account):
            # Existing audited self-name normalization is only for direction;
            # it does not authenticate the current UI window or conversation.
            row['sender'] = 'me'
        else:
            raise LiveReadError('LIVE_SENDER_UNKNOWN')
        if len(row['server_id']) > 128:
            raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    # Local IDs are ordered within a shard only. With second-resolution time,
    # opposite directions in different shards have no trustworthy total order.
    # Refuse instead of guessing which message cancels a pending suggestion.
    groups = {}
    for row in candidates:
        group = groups.setdefault(row['_time'], {'partitions': set(), 'senders': set()})
        group['partitions'].add(row['_partition'])
        group['senders'].add(row['sender'])
    if any(len(group['partitions']) > 1 and len(group['senders']) > 1 for group in groups.values()):
        raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    total_chars = 0
    for partition, record in record_by_partition.items():
        target_rows = [row for row in candidates if row['_partition'] == partition]
        if not target_rows:
            continue
        _deadline(deadline)
        ids = [row['_local_id'] for row in target_rows]
        placeholders = ','.join('?' for _ in ids)
        with closing(sync._open_readonly_database(reader, record)) as connection:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            cursor = connection.cursor()
            cursor.execute(f'''SELECT local_id,length(CAST(message_content AS BLOB)),length(CAST(compress_content AS BLOB)),
                substr(message_content,1,?),substr(compress_content,1,?) FROM "{table}"
                WHERE local_type=1 AND local_id IN ({placeholders})''', (MAX_ENCODED_BYTES + 1, MAX_ENCODED_BYTES + 1, *ids))
            received = set()
            for local_id, plain_size, compressed_size, plain, compressed in cursor.fetchall():
                row_id = partition + ':' + _message_id(local_id)
                if row_id not in selected or row_id in received:
                    raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
                received.add(row_id)
                if (plain_size or 0) > MAX_ENCODED_BYTES or (compressed_size or 0) > MAX_ENCODED_BYTES:
                    raise LiveReadError('LIVE_OVERFLOW')
                text, failed = sync._decode_message_content(plain, compressed)
                if failed or not isinstance(text, str) or not text.strip():
                    raise LiveReadError('LIVE_DECODE_FAILED')
                if len(text) > MAX_TEXT_CHARS:
                    raise LiveReadError('LIVE_OVERFLOW')
                total_chars += len(text)
                if total_chars > MAX_TOTAL_CHARS:
                    raise LiveReadError('LIVE_OVERFLOW')
                selected[row_id]['text'] = text
            if len(received) != len(target_rows):
                raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
    return [{key: row[key] for key in ('id', 'server_id', 'timestamp', 'sender', 'text')} for row in candidates]


def read_contact(project_root: Path, bundle_id: str, previous: dict | None = None) -> dict:
    """Read a full recent tail; previous is an identity guard, never authority.

    Consumers must detect lost overlap/gaps and fence results on authorization
    changes. This method makes no claim to monitor arbitrary historical edits.
    """
    _check_request(bundle_id, previous)
    try:
        root = Path(project_root).resolve(strict=True)
        sync = _load_sync(root)
        deadline = time.monotonic() + min(90, max(1, READ_TIMEOUT_SECONDS))
        with sync._exclusive_sync_lock(), sync._database_snapshot_scope():
            readiness = sync.inspect_runtime()
            if not readiness.get('ready') or readiness.get('version') != '1.5.0':
                raise LiveReadError('LIVE_SOURCE_UNAVAILABLE')
            account, _ = sync.discover_unique_account()
            peer, identity = _identity(root, bundle_id, sync, account)
            if previous is not None and previous['identity'] != identity:
                raise LiveReadError('LIVE_IDENTITY_CHANGED')
            reader, records = sync._database_records(account, cache_only=True, rescan=False, allow_process_hook=False)
            rows = _recent_rows(sync, reader, records, peer, account, deadline)
            _deadline(deadline)
            current_account, _ = sync.discover_unique_account()
            if current_account != account or _identity(root, bundle_id, sync, current_account) != (peer, identity):
                raise LiveReadError('LIVE_IDENTITY_CHANGED')
            _deadline(deadline)
            digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return {'identity': identity, 'rows': rows,
                    'cursor': {'version': 1, 'identity': identity, 'digest': digest},
                    'observed_at': datetime.now(timezone.utc).isoformat()}
    except LiveReadError:
        raise
    except Exception as exc:
        mapping = {'needs_account_selection': 'LIVE_ACCOUNT_UNAVAILABLE', 'wechat_data_not_found': 'LIVE_ACCOUNT_UNAVAILABLE',
                   'account_probe_failed': 'LIVE_ACCOUNT_UNAVAILABLE', 'wechat_not_running': 'LIVE_ACCOUNT_UNAVAILABLE',
                   'database_key_unavailable': 'LIVE_KEY_UNAVAILABLE', 'secret_cache_invalid': 'LIVE_KEY_UNAVAILABLE',
                   'sync_busy': 'LIVE_BUSY', 'source_database_busy': 'LIVE_SOURCE_BUSY',
                   'database_unavailable': 'LIVE_SOURCE_UNAVAILABLE', 'wechat_database_not_found': 'LIVE_SOURCE_UNAVAILABLE',
                   'database_inventory_changed': 'LIVE_SOURCE_BUSY', 'database_inventory_incomplete': 'LIVE_SOURCE_UNAVAILABLE'}
        code = mapping.get(getattr(exc, 'code', None), 'LIVE_READ_FAILED')
        if isinstance(exc, (FileNotFoundError, json.JSONDecodeError, UnicodeError)):
            code = 'LIVE_IDENTITY_UNAVAILABLE'
        raise LiveReadError(code) from None


@contextmanager
def _private_stdio():
    """Suppress even native diagnostics while private work runs in this worker."""
    saved = {}
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stdout.flush(); sys.stderr.flush()
        for descriptor in (1, 2):
            saved[descriptor] = os.dup(descriptor)
            os.dup2(null, descriptor)
        yield
    finally:
        # Flush buffered Python diagnostics while descriptors still point at
        # the null sink, including when private work raised an exception.
        sys.stdout.flush(); sys.stderr.flush()
        for descriptor, old in saved.items():
            os.dup2(old, descriptor)
            os.close(old)
        os.close(null)


def main() -> int:
    try:
        raw = sys.stdin.read(16385)
        if len(raw) > 16384:
            raise LiveReadError('LIVE_INVALID_REQUEST')
        try:
            request = json.loads(raw)
        except (ValueError, TypeError):
            raise LiveReadError('LIVE_INVALID_REQUEST') from None
        if not isinstance(request, dict) or not {'bundle_id'} <= set(request) or set(request) - {'bundle_id', 'previous'}:
            raise LiveReadError('LIVE_INVALID_REQUEST')
        _check_request(request['bundle_id'], request.get('previous'))
        with _private_stdio():
            result = read_contact(Path(__file__).resolve().parents[1], request['bundle_id'], request.get('previous'))
        response = {'ok': True, **result}
        code = 0
    except Exception as exc:
        response = {'ok': False, 'code': exc.code if isinstance(exc, LiveReadError) else 'LIVE_READ_FAILED'}
        code = 1
    print(json.dumps(response, ensure_ascii=False, separators=(',', ':')))
    return code


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
