"""Synthetic voice extraction tests; no real account or audio access."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_wechat_voice as voice


class VoiceArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / 'scripts/tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='voice-tests-', dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / 'private'
        self.contacts = self.root / 'contacts'
        self.bundle = self.contacts / 'synthetic_bundle'
        self.bundle.mkdir(parents=True)
        self.status = self.root / 'status.json'
        self.source = self.root / 'source.db'
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executescript('''CREATE TABLE Name2Id(user_name TEXT);
              INSERT INTO Name2Id VALUES('synthetic-session');
              INSERT INTO Name2Id VALUES('different-session');
              CREATE TABLE VoiceInfo(chat_name_id INTEGER,create_time INTEGER,local_id INTEGER,
                svr_id INTEGER,voice_data BLOB,data_index TEXT);''')
            db.execute('INSERT INTO VoiceInfo VALUES(?,?,?,?,?,?)',
                       (1, 1750000000, 10, 9001, b'\x02#!SILK_V3synthetic-audio', ''))
        self.record = {'name': 'message/media_0.db', 'path': str(self.source)}
        self.messages = [{'type': 'voice', 'timestamp': 1750000000, 'local_id': 10,
                          'source_message_id': '9001', 'source_message_id_kind': 'server_id',
                          'content': '[synthetic voice]', 'sender': 'them'}]
        self.write_bundle()

    def write_bundle(self):
        (self.bundle / 'messages.json').write_text(json.dumps({
            'contact_username': 'synthetic-session', 'messages': self.messages}), encoding='utf-8')

    @contextmanager
    def connection(self, record):
        db = sqlite3.connect(Path(record['path']).as_uri() + '?mode=ro', uri=True)
        db.execute('PRAGMA query_only=ON')
        try:
            yield db
        finally:
            db.close()

    def fingerprint(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_archive(self, **kwargs):
        return voice.archive_records('synthetic-account-hash', [self.record],
            kwargs.get('opener', self.connection), self.private, self.contacts, self.status,
            self.fingerprint)

    def test_extracts_silk_and_preserves_original_source_bytes(self):
        before = self.source.read_bytes()
        result = self.run_archive()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['total_voice_messages'], 1)
        self.assertEqual(result['archived_voice_messages'], 1)
        self.assertEqual(result['audio_formats'], {'silk': 1})
        self.assertEqual(self.source.read_bytes(), before)
        self.assertNotIn('synthetic-session', self.status.read_text())
        self.assertNotIn(str(self.root), self.status.read_text())
        db = voice.open_catalog(self.private)
        self.addCleanup(db.close)
        relative = db.execute('SELECT relative_path FROM assets').fetchone()[0]
        self.assertEqual((self.private / relative).read_bytes(), b'\x02#!SILK_V3synthetic-audio')

    def test_unchanged_skips_database_and_bundle_reads(self):
        self.run_archive()
        with mock.patch.object(voice, 'import_bundle', side_effect=AssertionError('no bundle read')):
            status = self.run_archive(opener=mock.Mock(side_effect=AssertionError('no database read')))
        self.assertEqual(status['skipped_databases'], 1)
        self.assertEqual(status['skipped_bundles'], 1)
        self.assertEqual(status['failed_databases'], 0)
        self.assertEqual(status['archived_voice_messages'], 1)

    def test_missing_audio_keeps_message_and_empty_media_is_explicit(self):
        self.messages.append({**self.messages[0], 'source_message_id': '9002', 'local_id': 11})
        self.write_bundle()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('INSERT INTO VoiceInfo VALUES(?,?,?,?,?,?)', (1, 1750000000, 11, 9002, b'', ''))
        result = self.run_archive()
        self.assertEqual(result['total_voice_messages'], 2)
        self.assertEqual(result['archived_voice_messages'], 1)
        self.assertEqual(result['missing_voice_messages'], 1)

    def test_duplicate_payload_shares_asset_but_keeps_each_message(self):
        self.messages.append({**self.messages[0], 'source_message_id': '9002', 'local_id': 11})
        self.write_bundle()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('INSERT INTO VoiceInfo VALUES(?,?,?,?,?,?)',
                       (1, 1750000000, 11, 9002, b'\x02#!SILK_V3synthetic-audio', ''))
        result = self.run_archive()
        self.assertEqual(result['archived_voice_messages'], 2)
        self.assertEqual(result['unique_audio_files'], 1)

    def test_same_server_id_in_another_session_never_matches(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE VoiceInfo SET chat_name_id=2')
        result = self.run_archive()
        self.assertEqual(result['archived_voice_messages'], 0)
        self.assertEqual(result['missing_voice_messages'], 1)
        self.assertEqual(result['unique_audio_files'], 1)

    def test_conflicting_payloads_for_same_message_are_ambiguous(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('INSERT INTO VoiceInfo VALUES(?,?,?,?,?,?)',
                       (1, 1750000000, 10, 9001, b'\x02#!SILK_V3different-audio', ''))
        result = self.run_archive()
        self.assertEqual(result['archived_voice_messages'], 0)
        self.assertEqual(result['ambiguous_voice_messages'], 1)

    def test_missing_blob_is_restored_even_when_database_unchanged(self):
        self.run_archive()
        blob = next((self.private / 'blobs').glob('*/*.silk'))
        blob.unlink()
        result = self.run_archive()
        self.assertTrue(blob.is_file())
        self.assertEqual(result['processed_databases'], 1)
        self.assertEqual(result['archived_voice_messages'], 1)

    def test_old_voice_version_retained_after_source_blob_changes(self):
        self.run_archive()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE VoiceInfo SET voice_data=?', (b'\x02#!SILK_V3new-audio',))
        self.run_archive()
        db = voice.open_catalog(self.private)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM media_versions').fetchone()[0], 2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 2)

    def test_identity_survives_transcription_but_not_real_content_changes(self):
        message = self.messages[0]
        with_text = {**message, 'transcript': 'synthetic local transcription',
                     'transcript_source': 'local:synthetic-model',
                     'transcript_status': 'machine_generated',
                     'voice_archive_sha256': 'a' * 64}
        self.assertEqual(voice.message_fingerprint(message), voice.message_fingerprint(with_text))
        self.assertEqual(voice.message_key('b', message), voice.message_key('b', with_text))
        self.assertNotEqual(voice.message_fingerprint(message), voice.message_fingerprint({**message, 'content': 'changed'}))

    def test_failure_does_not_advance_database_checkpoint(self):
        result = self.run_archive(opener=mock.Mock(side_effect=OSError('synthetic failure')))
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['failed_databases'], 1)
        result = self.run_archive()
        self.assertEqual(result['processed_databases'], 1)
        self.assertEqual(result['archived_voice_messages'], 1)

    def test_known_conflicting_server_id_blocks_local_time_fallback(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE VoiceInfo SET svr_id=9002')
        result = self.run_archive()
        self.assertEqual(result['archived_voice_messages'], 0)
        self.assertEqual(result['missing_voice_messages'], 1)

    def test_catalog_cannot_silently_mix_different_accounts(self):
        self.run_archive()
        with self.assertRaises(voice.sync.SyncFailure) as caught:
            voice.archive_records('different-account', [self.record], self.connection,
                self.private, self.contacts, self.status, self.fingerprint)
        self.assertEqual(caught.exception.code, 'needs_account_selection')


if __name__ == '__main__':
    unittest.main()
