"""Synthetic provenance checks; no real account discovery or media reads."""
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_wechat_local as archive


class MediaAccountBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source-a'
        (self.root / 'msg').mkdir(parents=True)
        self.source = self.root / 'msg' / 'synthetic.txt'
        self.source.write_bytes(b'synthetic provenance')
        self.private, self.export, self.status = (self.base / value for value in ('private', 'export', 'status.json'))
        self.account = hashlib.sha256(b'synthetic-account-a').hexdigest()
        self.other_account = hashlib.sha256(b'synthetic-account-b').hexdigest()

    def run_archive(self, root=None, account=None):
        return archive.archive(root or self.root, self.private, self.export, self.status,
                               account_hash=account or self.account)

    def legacy(self):
        self.run_archive()
        db = archive.open_catalog(self.private)
        with db:
            db.execute("DELETE FROM metadata WHERE key IN ('account_hash','source_root_hash_v1')")
        db.close()

    def metadata(self):
        db = archive.open_catalog(self.private)
        result = dict(db.execute('SELECT key,value FROM metadata'))
        db.close()
        return result

    def test_new_catalog_binds_account_and_second_account_cannot_change_catalog(self):
        self.run_archive()
        metadata = self.metadata()
        catalog = self.private / 'catalog.sqlite3'
        before = catalog.read_bytes()
        with self.assertRaises(archive.ArchiveIdentityError) as caught:
            self.run_archive(account=self.other_account)
        self.assertEqual(caught.exception.code, 'needs_account_selection')
        self.assertEqual(catalog.read_bytes(), before)
        self.assertEqual(self.metadata(), metadata)
        self.assertNotIn(self.account, self.status.read_text('utf-8'))
        self.assertNotIn(str(self.root), self.status.read_text('utf-8'))
        self.assertIn('needs_account_selection', self.status.read_text('utf-8'))

    def test_legacy_unchanged_catalog_migrates_and_skips_existing_files(self):
        self.legacy()
        result = self.run_archive()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['processed_files'], 0)
        self.assertEqual(result['skipped_existing_files'], 1)
        self.assertEqual(self.metadata()['account_hash'], self.account)
        self.assertEqual(self.source.read_bytes(), b'synthetic provenance')

    def test_legacy_changed_file_with_same_identity_uses_intact_anchor_then_archives_change(self):
        other = self.root / 'msg' / 'changed.txt'
        other.write_bytes(b'synthetic original')
        self.legacy()
        other.write_bytes(b'synthetic correction')
        result = self.run_archive()
        self.assertEqual(result['changed_files'], 1)
        self.assertEqual(self.metadata()['account_hash'], self.account)

    def test_legacy_copied_or_unrelated_root_is_not_silently_adopted(self):
        self.legacy()
        other = self.base / 'source-b'
        shutil.copytree(self.root, other)
        with self.assertRaises(archive.ArchiveIdentityError) as caught:
            self.run_archive(root=other, account=self.other_account)
        self.assertEqual(caught.exception.code, 'media_account_binding_required')
        self.assertNotIn('account_hash', self.metadata())

    def test_legacy_without_a_provenance_signature_stops_and_preserves_originals(self):
        self.legacy()
        db = archive.open_catalog(self.private)
        with db:
            db.execute('UPDATE files SET source_signature=NULL')
        db.close()
        before = self.source.read_bytes()
        with self.assertRaises(archive.ArchiveIdentityError) as caught:
            self.run_archive()
        self.assertEqual(caught.exception.code, 'media_account_binding_required')
        self.assertEqual(self.source.read_bytes(), before)
        self.assertNotIn('account_hash', self.metadata())

    def test_corrupt_binding_metadata_is_not_interpreted_as_an_unbound_catalog(self):
        self.run_archive()
        db = archive.open_catalog(self.private)
        with db:
            db.execute("UPDATE metadata SET value='' WHERE key='account_hash'")
        db.close()
        with self.assertRaises(archive.ArchiveIdentityError) as caught:
            self.run_archive()
        self.assertEqual(caught.exception.code, 'media_account_binding_required')
        self.assertEqual(self.metadata()['account_hash'], '')

    def test_legacy_all_changed_without_intact_anchor_requires_migration(self):
        self.legacy()
        self.source.write_bytes(b'changed synthetic source without an anchor')
        with self.assertRaises(archive.ArchiveIdentityError) as caught:
            self.run_archive()
        self.assertEqual(caught.exception.code, 'media_account_binding_required')
        self.assertNotIn('account_hash', self.metadata())

    def test_root_only_catalog_rejects_other_source_root(self):
        archive.archive(self.root, self.private, self.export, self.status)
        other = self.base / 'source-b'
        (other / 'msg').mkdir(parents=True)
        (other / 'msg' / 'other.txt').write_bytes(b'synthetic other account')
        with self.assertRaises(archive.ArchiveIdentityError):
            archive.archive(other, self.private, self.export, self.status)
        db = archive.open_catalog(self.private)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)
        db.close()

    def test_explicit_same_account_can_relocate_source_without_mixing_owner(self):
        self.run_archive()
        other = self.base / 'relocated-source'
        self.root.rename(other)
        result = self.run_archive(root=other)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['archived_files'], 1)
        self.assertEqual(self.metadata()['account_hash'], self.account)


if __name__ == '__main__':
    unittest.main()
