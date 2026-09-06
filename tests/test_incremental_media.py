"""Synthetic incremental tests; never read the user's chats or media."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_wechat_local as archive
import classify_wechat_local as classifier


class IncrementalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        (self.root / 'msg').mkdir(parents=True)
        self.source = self.root / 'msg' / 'synthetic.txt'
        self.source.write_bytes(b'synthetic original')
        self.private = self.base / 'private'
        self.export = self.base / 'export'
        self.status = self.base / 'status.json'

    def run_archive(self, **kwargs):
        return archive.archive(self.root, self.private, self.export, self.status, **kwargs)

    def test_unchanged_source_does_not_read_hash_copy_decode_or_rebuild(self):
        first = self.run_archive()
        source_stat = self.source.stat()
        with mock.patch.object(archive, 'preserve_file', side_effect=AssertionError('copy forbidden')), \
                mock.patch.object(archive, 'digest_file', side_effect=AssertionError('hash forbidden')), \
                mock.patch.object(archive, 'decode_image', side_effect=AssertionError('decode forbidden')), \
                mock.patch.object(archive, 'write_report', side_effect=AssertionError('report forbidden')):
            second = self.run_archive()
        self.assertEqual(first['new_files'], 1)
        self.assertEqual(second['processed_files'], 0)
        self.assertEqual(second['skipped_existing_files'], 1)
        self.assertEqual(second['state'], 'completed')
        self.assertEqual(source_stat.st_mtime_ns, self.source.stat().st_mtime_ns)
        self.assertNotIn('synthetic', json.dumps(second))

    def test_changed_and_new_files_processed_and_all_versions_retained(self):
        self.run_archive()
        self.source.write_bytes(b'changed longer original')
        (self.root / 'msg' / 'second.txt').write_bytes(b'new')
        status = self.run_archive()
        self.assertEqual(status['processed_files'], 2)
        self.assertEqual(status['changed_files'], 1)
        self.assertEqual(status['new_files'], 1)
        db = archive.open_catalog(self.private)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 3)
        self.assertEqual(self.source.read_bytes(), b'changed longer original')

    def test_running_status_preserves_previously_archived_counts(self):
        self.run_archive()
        snapshots = []
        with mock.patch.object(archive, 'atomic_json', side_effect=lambda path, value: snapshots.append(dict(value))):
            self.run_archive()
        running = [value for value in snapshots if value['state'] == 'running']
        self.assertTrue(running)
        self.assertTrue(all(value['archived_files'] == 1 for value in running))

    def test_missing_blob_restored_even_when_source_signature_unchanged(self):
        self.run_archive()
        sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        blob = self.private / 'blobs' / sha[:2] / sha
        blob.unlink()
        status = self.run_archive()
        self.assertEqual(status['restored_files'], 1)
        self.assertEqual(status['skipped_existing_files'], 0)
        self.assertEqual(blob.read_bytes(), self.source.read_bytes())

    def test_missing_report_rebuilt_without_source_reread(self):
        self.run_archive()
        (self.export / 'index.html').unlink()
        with mock.patch.object(archive, 'preserve_file', side_effect=AssertionError('copy forbidden')):
            status = self.run_archive()
        self.assertTrue(status['report_rebuilt'])
        self.assertEqual(status['processed_files'], 0)

    def test_verify_existing_strongly_checks_and_reports_corrupt_blob(self):
        self.run_archive()
        sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        blob = self.private / 'blobs' / sha[:2] / sha
        status = self.run_archive(verify_existing=True)
        self.assertEqual(status['verified_existing_files'], 1)
        blob.write_bytes(b'x' * blob.stat().st_size)
        status = self.run_archive(verify_existing=True)
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['failed_files'], 1)
        self.assertEqual(status['skipped_existing_files'], 0)
        self.assertEqual(self.source.read_bytes(), b'synthetic original')

    def test_legacy_catalog_requires_verified_baseline_once(self):
        self.run_archive()
        db = archive.open_catalog(self.private)
        db.execute('UPDATE files SET source_signature=NULL')
        db.commit()
        db.close()
        with mock.patch.object(archive, 'preserve_file', wraps=archive.preserve_file) as preserve:
            status = self.run_archive()
        preserve.assert_called_once()
        self.assertEqual(status['baselined_files'], 1)
        self.assertEqual(self.run_archive()['skipped_existing_files'], 1)


class IncrementalClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.bundle = self.repo / 'data' / 'contacts' / 'synthetic_bundle'
        self.bundle.mkdir(parents=True)
        self.messages = self.bundle / 'messages.json'
        self.manifest = self.bundle / 'dashboard_manifest.json'
        self.write_messages(8)
        self.manifest.write_text(json.dumps({'contact': 'Synthetic Person', 'conversation_kind': 'direct'}), encoding='utf-8')

    def write_messages(self, count):
        self.messages.write_text(json.dumps({'contact': 'Synthetic Person', 'messages': [
            {'content': '商户测试文本', 'type': 'text'} for _ in range(count)
        ]}), encoding='utf-8')

    def run_classification(self):
        with contextlib.redirect_stdout(io.StringIO()):
            classifier.run_locked(self.repo)
        return json.loads((self.repo / 'data' / 'classification-status.json').read_text(encoding='utf-8'))

    def test_unchanged_does_not_read_messages_classify_or_rebuild(self):
        self.run_classification()
        original_read = Path.read_text
        def guarded_read(path, *args, **kwargs):
            if path.name in {'messages.json', 'dashboard_manifest.json'}:
                raise AssertionError('unchanged chat read forbidden')
            return original_read(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', guarded_read), \
                mock.patch.object(classifier, 'classify_messages', side_effect=AssertionError('classification forbidden')), \
                mock.patch.object(classifier, 'write_report', side_effect=AssertionError('report forbidden')):
            result = self.run_classification()
        self.assertEqual(result['processed_conversations'], 0)
        self.assertEqual(result['processed_messages'], 0)
        self.assertEqual(result['skipped_conversations'], 1)
        self.assertEqual(result['classified_messages'], 8)
        self.assertNotIn('Synthetic', json.dumps(result))

    def test_new_messages_and_manifest_change_reclassify(self):
        self.run_classification()
        self.write_messages(10)
        result = self.run_classification()
        self.assertEqual(result['processed_messages'], 10)
        self.manifest.write_text(json.dumps({'contact': 'Synthetic Group', 'conversation_kind': 'group'}), encoding='utf-8')
        result = self.run_classification()
        self.assertEqual(result['processed_conversations'], 1)
        self.assertEqual(result['conversation_kinds'], {'group': 1})

    def test_rule_changes_and_missing_result_invalidate_cache(self):
        self.run_classification()
        with mock.patch.object(classifier, 'MIN_TOPIC_HITS', 100):
            result = self.run_classification()
        self.assertEqual(result['processed_conversations'], 1)
        self.assertEqual(result['topic_counts'], {'待确认／低信号': 1})
        (self.bundle / 'classification.json').unlink()
        result = self.run_classification()
        self.assertEqual(result['processed_conversations'], 1)

    def test_missing_report_rebuilt_from_cached_results_without_chat_read(self):
        self.run_classification()
        (self.repo / 'data' / 'exports' / 'classification' / 'index.html').unlink()
        with mock.patch.object(classifier, 'classify_messages', side_effect=AssertionError('classification forbidden')):
            result = self.run_classification()
        self.assertTrue(result['report_rebuilt'])
        self.assertEqual(result['processed_messages'], 0)

    def test_failure_does_not_overwrite_valid_classification(self):
        self.run_classification()
        classification = self.bundle / 'classification.json'
        old = classification.read_bytes()
        self.messages.write_text('invalid json', encoding='utf-8')
        result = self.run_classification()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['failed_conversations'], 1)
        self.assertEqual(classification.read_bytes(), old)

    def test_report_recovers_after_transient_failure_even_when_chat_unchanged(self):
        self.run_classification()
        with mock.patch.object(classifier, 'input_signature', side_effect=OSError('synthetic transient')):
            failed = self.run_classification()
        self.assertEqual(failed['state'], 'partial')
        result = self.run_classification()
        self.assertEqual(result['skipped_conversations'], 1)
        self.assertTrue(result['report_rebuilt'])
        self.assertEqual(result['classified_conversations'], 1)


if __name__ == '__main__':
    unittest.main()
