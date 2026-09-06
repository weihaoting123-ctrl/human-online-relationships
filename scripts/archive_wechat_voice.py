"""Archive voice BLOBs from stable, read-only copies of one local account.

Only the existing DPAPI cache is used. No memory scanning, hooks, client
control, network requests, source writes, or transcript generation occur here.
Private rows retain every observed audio version; stdout is aggregate-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from archive_wechat_local import archive_lock, atomic_json, digest_file, fingerprint, is_link
import sync_all_wechat as sync

REPO = Path(__file__).resolve().parents[1]
PRIVATE = REPO / 'data/private/wechat-voice'
CONTACTS = REPO / 'data/contacts'
STATUS = REPO / 'data/voice-archive-status.json'
MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
DERIVED_FIELDS = {'transcript', 'voice_transcript', 'voice', 'audio', 'voice_archive',
                  'voice_id', 'voice_status', 'transcription', 'transcript_source',
                  'transcript_model', 'transcript_updated_at', 'transcript_status',
                  'voice_archive_sha256'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def token(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def message_fingerprint(message):
    return token({key: value for key, value in message.items() if key not in DERIVED_FIELDS})


def server_id(message):
    if message.get('source_message_id_kind') in {'server_id', 'msg_svr_id', 'message_id'}:
        value = message.get('source_message_id')
        if value not in (None, '', 0, '0') and not isinstance(value, bool):
            return str(value)
    for key in ('server_id', 'serverId', 'msgSvrId', 'msg_svr_id'):
        value = message.get(key)
        if value not in (None, '', 0, '0') and not isinstance(value, bool):
            return str(value)
    return ''


def message_key(bundle_id, message, occurrence=0):
    stable = server_id(message)
    identity = ['server', stable] if stable else ['fingerprint', message_fingerprint(message)]
    return token([bundle_id, identity, occurrence])


def audio_format(data):
    if data.startswith((b'\x02#!SILK_V3', b'#!SILK_V3')):
        return 'silk'
    if data.startswith(b'#!AMR'):
        return 'amr'
    if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
        return 'wav'
    if data.startswith(b'OggS'):
        return 'ogg'
    if data.startswith(b'fLaC'):
        return 'flac'
    if data.startswith(b'ID3'):
        return 'mp3'
    return 'bin'


def open_catalog(private):
    private.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(private / 'catalog.sqlite3', timeout=30)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS assets(sha TEXT PRIMARY KEY,format TEXT,size INTEGER,relative_path TEXT);
      CREATE TABLE IF NOT EXISTS media_records(id TEXT PRIMARY KEY,database_id TEXT,source_rowid INTEGER,
        session_hash TEXT,local_id TEXT,server_id TEXT,timestamp INTEGER,sha TEXT,state TEXT);
      CREATE INDEX IF NOT EXISTS media_server ON media_records(session_hash,server_id);
      CREATE INDEX IF NOT EXISTS media_local ON media_records(session_hash,local_id,timestamp);
      CREATE TABLE IF NOT EXISTS media_versions(media_id TEXT,sha TEXT,first_seen TEXT,PRIMARY KEY(media_id,sha));
      CREATE TABLE IF NOT EXISTS voice_messages(id TEXT PRIMARY KEY,bundle_id TEXT,message_index INTEGER,
        message_fingerprint TEXT,local_id TEXT,server_id TEXT,timestamp INTEGER,sha TEXT,state TEXT,media_id TEXT);
      CREATE INDEX IF NOT EXISTS voice_bundle ON voice_messages(bundle_id);
      CREATE TABLE IF NOT EXISTS bundle_meta(bundle_id TEXT PRIMARY KEY,session_hash TEXT,signature TEXT);
      CREATE TABLE IF NOT EXISTS database_meta(database_id TEXT PRIMARY KEY,fingerprint TEXT);
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
    ''')
    return db


def asset_present(private, row):
    if not row:
        return False
    sha, _format, size, relative = row
    if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
        return False
    path = private / relative
    try:
        return (path.resolve().is_relative_to(private.resolve()) and not is_link(path)
                and path.is_file() and path.stat().st_size == size and size > 0)
    except OSError:
        return False


def save_asset(db, private, data):
    sha, kind = hashlib.sha256(data).hexdigest(), audio_format(data)
    relative = f'blobs/{sha[:2]}/{sha}.{kind}'
    path = private / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if is_link(path) or path.stat().st_size != len(data) or digest_file(path) != sha:
            raise ValueError('voice_archive_asset_corrupt')
    else:
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?)', (sha, kind, len(data), relative))
    return sha


def extract_database(db, connection, database_id, private, now):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'VoiceInfo', 'Name2Id'}.issubset(tables):
        raise ValueError('voice_schema_unsupported')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(VoiceInfo)')}
    if not {'chat_name_id', 'create_time', 'local_id', 'svr_id', 'voice_data'}.issubset(columns):
        raise ValueError('voice_schema_unsupported')
    name_columns = {row[1] for row in connection.execute('PRAGMA table_info(Name2Id)')}
    name_column = 'user_name' if 'user_name' in name_columns else 'username' if 'username' in name_columns else None
    if name_column is None:
        raise ValueError('voice_schema_unsupported')
    # Size gating happens before materializing the BLOB. Unknown or excessive
    # payloads remain represented and their encrypted DB original is untouched.
    rows = connection.execute(f'''SELECT v.rowid,n."{name_column}",v.local_id,v.svr_id,v.create_time,
        length(v.voice_data), CASE WHEN length(v.voice_data)<=? THEN v.voice_data ELSE NULL END
        FROM VoiceInfo v LEFT JOIN Name2Id n ON n.rowid=v.chat_name_id ORDER BY v.rowid''', (MAX_AUDIO_BYTES,))
    count = 0
    for rowid, session, local, server, timestamp, size, data in rows:
        media_id = token([database_id, rowid])
        sha, state = '', 'missing_blob'
        if size and size > MAX_AUDIO_BYTES:
            state = 'oversized_blob'
        elif isinstance(data, (bytes, bytearray, memoryview)) and data:
            sha = save_asset(db, private, bytes(data))
            state = 'archived'
            db.execute('INSERT OR IGNORE INTO media_versions VALUES(?,?,?)', (media_id, sha, now))
        db.execute('INSERT OR REPLACE INTO media_records VALUES(?,?,?,?,?,?,?,?,?)',
                   (media_id, database_id, rowid, token(str(session)) if session else '',
                    str(local or ''), str(server or ''), int(timestamp or 0), sha, state))
        count += 1
    return count


def database_assets_present(db, database_id, private):
    rows = db.execute('''SELECT DISTINCT a.sha,a.format,a.size,a.relative_path
        FROM media_records m LEFT JOIN assets a ON a.sha=m.sha
        WHERE m.database_id=? AND m.sha != '' ''', (database_id,)).fetchall()
    return all(asset_present(private, row) for row in rows)


def import_bundle(db, path):
    if is_link(path) or is_link(path.parent) or path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError('voice_bundle_unsafe')
    before = fingerprint(path)
    payload = json.loads(path.read_text(encoding='utf-8'))
    if fingerprint(path) != before:
        raise ValueError('voice_bundle_changed')
    session = payload.get('contact_username')
    if not isinstance(session, str) or not session:
        raise ValueError('voice_bundle_identity_missing')
    messages = payload.get('messages')
    if not isinstance(messages, list):
        raise ValueError('voice_bundle_invalid')
    seen = Counter()
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get('type') != 'voice':
            continue
        base = message_key(path.parent.name, message)
        occurrence = seen[base]
        seen[base] += 1
        identity = message_key(path.parent.name, message, occurrence)
        db.execute('''INSERT INTO voice_messages VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET message_index=excluded.message_index,
            message_fingerprint=excluded.message_fingerprint,local_id=excluded.local_id,
            server_id=excluded.server_id,timestamp=excluded.timestamp''',
            (identity, path.parent.name, index, message_fingerprint(message),
             str(message.get('local_id') or ''), server_id(message),
             int(message.get('timestamp') or 0), '', 'missing', ''))
    db.execute('INSERT OR REPLACE INTO bundle_meta VALUES(?,?,?)',
               (path.parent.name, token(session), canonical(before)))
    return sum(seen.values())


def match_messages(db, private):
    # Never use a server ID across sessions, and never guess between conflicting
    # audio hashes. Exact local+time fallback is allowed only without a known
    # contradictory server ID in the candidate record.
    rows = db.execute('''SELECT v.id,v.server_id,v.local_id,v.timestamp,b.session_hash
        FROM voice_messages v JOIN bundle_meta b ON b.bundle_id=v.bundle_id''').fetchall()
    for identity, server, local, timestamp, session in rows:
        candidates = []
        if server:
            candidates = db.execute('''SELECT id,sha FROM media_records WHERE session_hash=?
                AND server_id=? AND state='archived' ''', (session, server)).fetchall()
        if not candidates and local and timestamp:
            candidates = db.execute('''SELECT id,sha FROM media_records WHERE session_hash=?
                AND local_id=? AND timestamp=? AND state='archived'
                AND (server_id='' OR server_id='0' OR ?='')''',
                (session, local, timestamp, server)).fetchall()
        hashes = {sha for _, sha in candidates}
        sha, media_id, state = '', '', 'ambiguous' if len(hashes) > 1 else 'missing'
        if len(hashes) == 1:
            candidate_sha = next(iter(hashes))
            asset = db.execute('SELECT sha,format,size,relative_path FROM assets WHERE sha=?', (candidate_sha,)).fetchone()
            if asset_present(private, asset):
                sha, media_id, state = candidate_sha, candidates[0][0], 'archived'
        db.execute('UPDATE voice_messages SET sha=?,state=?,media_id=? WHERE id=?',
                   (sha, state, media_id, identity))


def summarize(db, status):
    status['total_voice_messages'] = db.execute('SELECT COUNT(*) FROM voice_messages').fetchone()[0]
    status['archived_voice_messages'] = db.execute("SELECT COUNT(*) FROM voice_messages WHERE state='archived'").fetchone()[0]
    status['ambiguous_voice_messages'] = db.execute("SELECT COUNT(*) FROM voice_messages WHERE state='ambiguous'").fetchone()[0]
    status['missing_voice_messages'] = status['total_voice_messages'] - status['archived_voice_messages']
    status['unique_audio_files'], status['archived_bytes'] = db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM assets').fetchone()
    status['media_records'] = db.execute('SELECT COUNT(*) FROM media_records').fetchone()[0]
    status['audio_formats'] = dict(db.execute('SELECT format,COUNT(*) FROM assets GROUP BY format'))
    status['unmatched_media_records'] = db.execute("SELECT COUNT(*) FROM media_records m WHERE sha != '' AND NOT EXISTS (SELECT 1 FROM voice_messages v WHERE v.media_id=m.id)").fetchone()[0]
    return status


def archive_records(account_hash, records, open_connection, private=PRIVATE, contacts=CONTACTS,
                    status_path=STATUS, fingerprint_database=sync._stable_database_fingerprint):
    status = {'state': 'running', 'total_voice_messages': 0, 'archived_voice_messages': 0,
              'missing_voice_messages': 0, 'unique_audio_files': 0, 'archived_bytes': 0,
              'processed_databases': 0, 'skipped_databases': 0, 'processed_bundles': 0,
              'skipped_bundles': 0, 'failed_databases': 0, 'failed_bundles': 0,
              'source_untouched': True, 'network_used': False,
              'updated_at': datetime.now(timezone.utc).isoformat()}
    with archive_lock(private):
        db = open_catalog(private)
        try:
            previous = db.execute("SELECT value FROM metadata WHERE key='account_hash'").fetchone()
            if previous and previous[0] != account_hash:
                raise sync.SyncFailure('needs_account_selection')
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', ('account_hash', account_hash))
            summarize(db, status)
            atomic_json(status_path, status)
            for record in records:
                source = Path(record['path'])
                database_id = token([account_hash, record['name']])
                try:
                    stamp = canonical(fingerprint_database(source))
                    old = db.execute('SELECT fingerprint FROM database_meta WHERE database_id=?', (database_id,)).fetchone()
                    if old and old[0] == stamp and database_assets_present(db, database_id, private):
                        status['skipped_databases'] += 1
                        continue
                    db.execute('SAVEPOINT voice_partition')
                    with open_connection(record) as connection:
                        extract_database(db, connection, database_id, private, status['updated_at'])
                    # A changing database is retried next run without advancing
                    # its checkpoint. Already archived raw assets are retained.
                    if canonical(fingerprint_database(source)) != stamp:
                        raise ValueError('voice_source_changed')
                    db.execute('INSERT OR REPLACE INTO database_meta VALUES(?,?)', (database_id, stamp))
                    db.execute('RELEASE voice_partition')
                    status['processed_databases'] += 1
                    db.commit()
                except Exception:
                    try:
                        db.execute('ROLLBACK TO voice_partition')
                        db.execute('RELEASE voice_partition')
                    except sqlite3.Error:
                        pass
                    status['failed_databases'] += 1
            for path in sorted(contacts.glob('*/messages.json')):
                try:
                    old = db.execute('SELECT signature FROM bundle_meta WHERE bundle_id=?', (path.parent.name,)).fetchone()
                    if old and old[0] == canonical(fingerprint(path)):
                        status['skipped_bundles'] += 1
                        continue
                    db.execute('SAVEPOINT voice_bundle')
                    import_bundle(db, path)
                    db.execute('RELEASE voice_bundle')
                    status['processed_bundles'] += 1
                except Exception:
                    try:
                        db.execute('ROLLBACK TO voice_bundle')
                        db.execute('RELEASE voice_bundle')
                    except sqlite3.Error:
                        pass
                    status['failed_bundles'] += 1
            match_messages(db, private)
            db.commit()
            summarize(db, status)
            status['state'] = 'partial' if status['failed_databases'] or status['failed_bundles'] else 'completed'
            status['updated_at'] = datetime.now(timezone.utc).isoformat()
            atomic_json(status_path, status)
            return status
        finally:
            db.close()


def run_archive():
    if REPO.drive.upper() == 'C:' or sync.DATA_ROOT.drive.upper() == 'C:':
        raise ValueError('non_c_project_required')
    account, _ = sync.discover_unique_account()
    with sync._exclusive_sync_lock(), sync._database_snapshot_scope():
        reader, records = sync._database_records(account, cache_only=True)
        media = [record for record in records if re.fullmatch(r'message/(?:media|voice)_\d+\.db', record['name'], re.I)]
        if not media:
            raise sync.SyncFailure('voice_database_missing')
        return archive_records(sync._account_hash(account), media,
                               lambda record: sync._open_readonly_database(reader, record))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local read-only voice archive; no transcription or network.')
    parser.add_argument('--archive', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.status:
            result = json.loads(STATUS.read_text(encoding='utf-8')) if STATUS.exists() else {'state': 'not_started'}
        else:
            result = run_archive()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('state') == 'completed' else 1
    except Exception as exc:
        allowed = {'needs_account_selection', 'database_key_unavailable', 'voice_database_missing',
                   'source_database_busy', 'sync_already_running'}
        code = exc.code if isinstance(exc, sync.SyncFailure) and exc.code in allowed else 'local_voice_archive_failed'
        result = {'state': 'error', 'error_code': code, 'source_untouched': True, 'network_used': False}
        atomic_json(STATUS, result)
        print(json.dumps(result))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
