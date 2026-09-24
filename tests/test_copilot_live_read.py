"""Selected-contact reader tests: synthetic files, mocked discovery/key cache only."""
from contextlib import ExitStack, closing
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
SCRIPT = SCRIPTS / 'copilot_live_read.py'
sys.path.insert(0, str(SCRIPTS))
import sync_all_wechat as sync

if SCRIPT.is_file():
    spec = importlib.util.spec_from_file_location('copilot_live_read', SCRIPT)
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
else:
    live = None


class LiveReadTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(live, 'selected-only live reader is not implemented')
        temporary_root = ROOT / 'scripts' / 'tmp'
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='live-reader-test-', dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle_id = 'synthetic-native-bundle'
        self.bundle = self.root / 'data' / 'contacts' / self.bundle_id
        self.bundle.mkdir(parents=True)
        self.private = self.root / 'data' / 'private' / 'wechat-sync'
        self.private.mkdir(parents=True)
        self.owner = 'wxid_synthetic_self_ab12'
        self.peer = 'wxid_synthetic_peer'
        self.table = 'Msg_' + hashlib.md5(self.peer.encode()).hexdigest()
        self.metadata = {'source': 'weflow-cli', 'own_wxid': self.owner,
                         'contact_username': self.peer, 'messages': []}
        self.manifest = {'source': 'wechat-local-readonly', 'conversation_kind': 'direct'}
        self.checkpoint = {'version': 1, 'account_hash': sync._account_hash(self.owner),
                           'sources': {}, 'contact_names': {},
                           'bundles': {self.peer: {'bundle_name': self.bundle_id}}}
        self.save_metadata()
        self.records = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {'PRIVATE_ROOT': self.private, 'CONTACTS_DIR': self.bundle.parent,
                            'LOCK_PATH': self.private / 'sync.lock'}.items():
            self.stack.enter_context(mock.patch.object(sync, name, value))
        self.stack.enter_context(mock.patch.object(live, '_load_sync', return_value=sync))
        self.stack.enter_context(mock.patch.object(sync, 'inspect_runtime', return_value={'ready': True, 'version': '1.5.0'}))
        self.discover = self.stack.enter_context(mock.patch.object(sync, 'discover_unique_account', return_value=(self.owner, self.root)))
        self.keys = self.stack.enter_context(mock.patch.object(sync, '_database_records', side_effect=lambda *a, **kw: (SimpleNamespace(sqlcipher=sqlite3), self.records)))
        self.stack.enter_context(mock.patch.object(sync, '_scan_database_keys', side_effect=AssertionError('MUST_NOT_SCAN')))
        self.stack.enter_context(mock.patch.object(sync, '_query_messages', side_effect=AssertionError('MUST_NOT_QUERY_FULL_HISTORY')))

    def save_metadata(self):
        (self.bundle / 'messages.json').write_text(json.dumps(self.metadata), encoding='utf-8')
        (self.bundle / 'dashboard_manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        (self.private / 'incremental-checkpoint.json').write_text(json.dumps(self.checkpoint), encoding='utf-8')

    def shard(self, number=0, peer_mapping=True):
        path = self.root / f'message_{number}.db'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE Name2Id(user_name TEXT)')
            conn.execute('INSERT INTO Name2Id(rowid,user_name) VALUES(1,?)', (self.owner[:-5],))
            if peer_mapping:
                conn.execute('INSERT INTO Name2Id(rowid,user_name) VALUES(2,?)', (self.peer,))
            conn.execute(f'CREATE TABLE "{self.table}" (local_id INTEGER, server_id TEXT, local_type INTEGER, '
                         'real_sender_id INTEGER, create_time INTEGER, message_content, compress_content)')
        self.records.append({'path': str(path), 'name': f'message/message_{number}.db',
                             'salt': f'{number + 1:032x}', 'key': 'a' * 64})
        return path

    def insert(self, path, local_id=1, server_id='501', text='synthetic hello', sender=2,
               timestamp=1700000000, message_type=1, compressed=None):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f'INSERT INTO "{self.table}" VALUES (?,?,?,?,?,?,?)',
                         (local_id, server_id, message_type, sender, timestamp, text, compressed))

    def read(self, previous=None):
        return live.read_contact(self.root, self.bundle_id, previous)

    def assert_code(self, code, operation=None):
        with self.assertRaises(live.LiveReadError) as caught:
            (operation or self.read)()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_selected_only_private_snapshots_no_source_or_archive_changes(self):
        path = self.shard()
        self.insert(path)
        self.insert(path, 2, '502', 'synthetic outgoing', sender=1, timestamp=1700000001)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE Msg_00000000000000000000000000000000(content TEXT)')
            conn.execute('INSERT INTO Msg_00000000000000000000000000000000 VALUES("OTHER SYNTHETIC CONTACT")')
        self.records.append({'path': str(self.root / 'media_0.db'), 'name': 'message/media_0.db'})
        before = path.read_bytes()
        archive_before = (self.bundle / 'messages.json').read_bytes()
        result = self.read()
        self.assertEqual(set(result), {'identity', 'rows', 'cursor', 'observed_at'})
        self.assertRegex(result['identity'], r'^[0-9a-f]{64}$')
        self.assertEqual([row['sender'] for row in result['rows']], ['peer', 'me'])
        self.assertEqual(result['rows'][0]['text'], 'synthetic hello')
        self.assertRegex(result['rows'][0]['id'], r'^p_[0-9a-f]{16}:1$')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.bundle / 'messages.json').read_bytes(), archive_before)
        self.assertFalse((self.root / 'data' / 'raw').exists())
        self.keys.assert_called_once_with(self.owner, cache_only=True, rescan=False, allow_process_hook=False)
        private_dto = json.dumps({'identity': result['identity'], 'cursor': result['cursor']})
        self.assertNotIn(self.owner, private_dto)
        self.assertNotIn(self.peer, private_dto)
        self.assertNotIn('synthetic hello', private_dto)

    def test_local_identity_survives_server_id_promotion_and_duplicate_text_is_preserved(self):
        path = self.shard()
        self.insert(path, server_id='0')
        first = self.read()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f'UPDATE "{self.table}" SET server_id="501" WHERE local_id=1')
        self.insert(path, 2, '502')
        second = self.read(first['cursor'])
        self.assertEqual(first['rows'][0]['id'], second['rows'][0]['id'])
        self.assertEqual(len(second['rows']), 2)
        self.assertEqual(second['rows'][0]['server_id'], '501')
        self.assertNotEqual(first['cursor']['digest'], second['cursor']['digest'])

    def test_global_recent_tail_200_and_nontext_never_decoded(self):
        first = self.shard()
        second = self.shard(1)
        for number in range(220):
            self.insert(first if number % 2 else second, number + 1, str(number + 1000),
                        timestamp=1700000000 + number)
        self.insert(first, 999, '1999', b'\xff\xfe', timestamp=1800000000, message_type=3)
        result = self.read()
        self.assertEqual(len(result['rows']), 200)
        self.assertEqual(result['rows'][0]['server_id'], '1020')
        self.assertEqual(result['rows'][-1]['server_id'], '1219')

    def test_unknown_sender_fails_and_peer_cannot_match_self(self):
        path = self.shard()
        self.insert(path, sender=99)
        self.assert_code('LIVE_SENDER_UNKNOWN')
        self.metadata['contact_username'] = self.owner[:-5]
        self.checkpoint['bundles'] = {self.owner[:-5]: {'bundle_name': self.bundle_id}}
        self.save_metadata()
        self.assert_code('LIVE_IDENTITY_UNAVAILABLE')

    def test_cross_shard_same_second_opposite_directions_are_ambiguous(self):
        first, second = self.shard(), self.shard(1)
        self.insert(first, 50, '601', sender=2)
        self.insert(second, 1, '602', sender=1)
        self.assert_code('LIVE_SOURCE_UNAVAILABLE')

    def test_same_shard_or_same_direction_equal_timestamps_remain_valid(self):
        first, second = self.shard(), self.shard(1)
        self.insert(first, 50, '601', sender=2)
        self.insert(second, 1, '602', sender=2)
        self.assertEqual(len(self.read()['rows']), 2)
        with closing(sqlite3.connect(second)) as conn, conn:
            conn.execute(f'DELETE FROM "{self.table}"')
        self.insert(first, 51, '603', sender=1)
        self.assertEqual([row['sender'] for row in self.read()['rows']], ['peer', 'me'])

    def test_identity_requires_native_direct_bundle_exact_account_and_checkpoint(self):
        for field, value in [('own_wxid', 'unknown'), ('own_wxid', 'other-synthetic'),
                             ('contact_username', 'unknown')]:
            with self.subTest(field=field, value=value):
                original = self.metadata[field]
                self.metadata[field] = value
                self.save_metadata()
                self.assert_code('LIVE_IDENTITY_UNAVAILABLE')
                self.metadata[field] = original
        self.save_metadata()
        self.manifest['conversation_kind'] = 'group'
        self.save_metadata()
        self.assert_code('LIVE_IDENTITY_UNAVAILABLE')
        self.manifest['conversation_kind'] = 'direct'
        self.manifest['source'] = 'manual-import'
        self.save_metadata()
        self.assert_code('LIVE_IDENTITY_UNAVAILABLE')
        self.manifest['source'] = 'wechat-local-readonly'
        self.checkpoint['bundles'][self.peer]['bundle_name'] = 'other-bundle'
        self.save_metadata()
        self.assert_code('LIVE_IDENTITY_UNAVAILABLE')

    def test_previous_is_not_identity_authority_and_never_skips_current_read(self):
        path = self.shard()
        self.insert(path)
        first = self.read()
        self.assert_code('LIVE_IDENTITY_CHANGED', lambda: self.read({**first['cursor'], 'identity': '0' * 64}))
        self.insert(path, 2, '502', 'changed synthetic')
        second = self.read(first['cursor'])
        self.assertEqual(len(second['rows']), 2)
        self.assertEqual(first['identity'], second['identity'])

    def test_decode_failure_and_text_limits_fail_without_truncation(self):
        path = self.shard()
        self.insert(path, text=b'\xff\xfe')
        self.assert_code('LIVE_DECODE_FAILED')
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f'UPDATE "{self.table}" SET message_content=?', ('x' * 4001,))
        self.assert_code('LIVE_OVERFLOW')
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f'DELETE FROM "{self.table}"')
        for number in range(11):
            self.insert(path, number + 1, str(number + 1), 'x' * 4000)
        self.assert_code('LIVE_OVERFLOW')

    def test_missing_or_unknown_account_keys_and_busy_have_fixed_codes(self):
        self.discover.side_effect = sync.SyncFailure('needs_account_selection')
        self.assert_code('LIVE_ACCOUNT_UNAVAILABLE')
        self.discover.side_effect = None
        self.keys.side_effect = sync.SyncFailure('database_key_unavailable')
        self.assert_code('LIVE_KEY_UNAVAILABLE')
        self.keys.side_effect = sync.SyncFailure('source_database_busy')
        self.assert_code('LIVE_SOURCE_BUSY')
        with sync._exclusive_sync_lock():
            self.assert_code('LIVE_BUSY')

    def test_invalid_request_paths_and_unbounded_cursor_rejected_before_discovery(self):
        for bundle in ('../escape', 'x/y', 'x\\y', '', '.', '..'):
            self.assert_code('LIVE_INVALID_REQUEST', lambda: live.read_contact(self.root, bundle))
        self.assert_code('LIVE_INVALID_REQUEST', lambda: self.read({'identity': 'x' * 10000}))
        self.discover.assert_not_called()

    def test_cli_emits_safe_error_only_for_invalid_input(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], input='{"bundle_id":"../PRIVATE SYNTHETIC"}',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), {'ok': False, 'code': 'LIVE_INVALID_REQUEST'})
        self.assertEqual(result.stderr, '')
        self.assertNotIn('PRIVATE', result.stdout)

    def test_cli_success_forwards_only_internal_dto_and_accepts_cursor_dictionary(self):
        expected = {'identity': 'a' * 64, 'rows': [], 'cursor': {'version': 1, 'identity': 'a' * 64, 'digest': 'b' * 64}, 'observed_at': '2026-01-01T00:00:00+00:00'}
        stdin = io.StringIO(json.dumps({'bundle_id': self.bundle_id, 'previous': expected['cursor']}))
        stdout = io.StringIO()
        with mock.patch.object(live, 'read_contact', return_value=expected) as read, mock.patch.object(sys, 'stdin', stdin), mock.patch.object(sys, 'stdout', stdout):
            self.assertEqual(live.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {'ok': True, **expected})
        self.assertEqual(read.call_args.args[2], expected['cursor'])

    def test_cli_suppresses_buffered_diagnostics_even_when_reader_raises(self):
        code = """
import importlib.util
import sys
spec=importlib.util.spec_from_file_location('live',sys.argv[1])
live=importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
def fail(*args):
    print('SYNTHETIC_PRIVATE_DIAGNOSTIC')
    print('SYNTHETIC_PRIVATE_ERROR',file=sys.stderr)
    raise ValueError('SYNTHETIC_PRIVATE_EXCEPTION')
live.read_contact=fail
raise SystemExit(live.main())
"""
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT)],
                                input=json.dumps({'bundle_id': self.bundle_id}),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), '{"ok":false,"code":"LIVE_READ_FAILED"}')
        self.assertEqual(result.stderr, '')

    def test_identity_header_does_not_decode_history_and_handles_partial_utf8_tail(self):
        path = self.shard()
        self.insert(path)
        prefix = json.dumps({key: value for key, value in self.metadata.items() if key != 'messages'})[:-1] + ',"messages":['
        raw = prefix.encode() + ('合成'.encode() * 20000)
        (self.bundle / 'messages.json').write_bytes(raw)
        # The historical body deliberately is not valid JSON; only the native
        # preceding identity fields are used and live text comes from SQLite.
        self.assertEqual(self.read()['rows'][0]['text'], 'synthetic hello')

    def test_identity_changed_during_read_fails_before_returning_text(self):
        path = self.shard()
        self.insert(path)
        self.discover.side_effect = [(self.owner, self.root), ('different-synthetic', self.root)]
        self.assert_code('LIVE_IDENTITY_CHANGED')

    def test_query_missing_local_mapping_does_not_guess_from_another_shard(self):
        first = self.shard(peer_mapping=False)
        self.shard(1)
        self.insert(first)
        self.assert_code('LIVE_SENDER_UNKNOWN')

    def test_corrupt_old_target_body_outside_tail_is_not_decoded(self):
        path = self.shard()
        self.insert(path, text=b'\xff\xfe', sender=99)
        for number in range(200):
            self.insert(path, number + 2, str(number + 600), timestamp=1700000001 + number)
        self.assertEqual(len(self.read()['rows']), 200)

    def test_cooperative_deadline_and_shard_limit_fail_bounded(self):
        path = self.shard()
        self.insert(path)
        with mock.patch.object(live.time, 'monotonic', side_effect=[0, 100]):
            self.assert_code('LIVE_SOURCE_BUSY')
        self.records[:] = [dict(self.records[0], name=f'message/message_{number}.db') for number in range(65)]
        self.assert_code('LIVE_OVERFLOW')

    def test_duplicate_local_ids_are_not_silently_collapsed(self):
        path = self.shard()
        self.insert(path)
        self.insert(path, server_id='999')
        self.assert_code('LIVE_SOURCE_UNAVAILABLE')

    def test_ascii_same_time_rows_are_distinct_without_contiguous_id_assumptions(self):
        path = self.shard()
        self.insert(path, 7, '500')
        self.insert(path, 700, '501')
        result = self.read()
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual(result['rows'][0]['timestamp'], result['rows'][1]['timestamp'])
        self.assertNotEqual(result['rows'][0]['id'], result['rows'][1]['id'])

    def test_every_plain_dbapi_connection_is_closed_even_without_close_on_exit(self):
        path = self.shard()
        self.insert(path)
        opened = []
        def connect(_reader, record):
            connection = sqlite3.connect(Path(record['path']).as_uri() + '?mode=ro', uri=True)
            opened.append(connection)
            return connection
        try:
            with mock.patch.object(sync, '_open_readonly_database', side_effect=connect):
                self.assertEqual(len(self.read()['rows']), 1)
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')
        finally:
            for connection in opened:
                connection.close()

    def test_server_alias_across_partitions_is_preserved_for_consumer_dedup(self):
        first = self.shard()
        second = self.shard(1)
        self.insert(first, local_id=7, server_id='1234')
        self.insert(second, local_id=70, server_id='1234')
        rows = self.read()['rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['server_id'], rows[1]['server_id'])
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])


if __name__ == '__main__':
    unittest.main()
