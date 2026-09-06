import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import archive_wechat_local as archive
from PIL import Image


class LocalArchiveTests(unittest.TestCase):
    def image(self):
        buffer=io.BytesIO()
        Image.new('RGB',(20,30),'red').save(buffer,format='PNG')
        return buffer.getvalue()

    def test_plain_and_xor_images_validate_and_corruption_rejected(self):
        raw=self.image()
        self.assertEqual(archive.decode_image(raw)[0],raw)
        encoded=bytes(x^0x73 for x in raw)
        self.assertEqual(archive.decode_image(encoded)[0],raw)
        self.assertIsNone(archive.decode_image(raw[:25])[0])
        self.assertEqual(archive.decode_image(archive.V2_MAGIC+b'x'*40)[2],'encrypted')

    def test_archive_is_idempotent_retains_each_mapping_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'source'
            folder=root/'msg'/'2026-09'
            folder.mkdir(parents=True)
            raw=self.image()
            first=folder/'private-name.dat'
            second=folder/'another.dat'
            first.write_bytes(raw)
            second.write_bytes(raw)
            private=Path(tmp)/'private'
            export=Path(tmp)/'export'
            status=Path(tmp)/'status.json'
            one=archive.archive(root,private,export,status)
            two=archive.archive(root,private,export,status)
            self.assertEqual(one['archived_files'],2)
            self.assertEqual(one['unique_payloads'],1)
            self.assertEqual(two['unique_payloads'],1)
            self.assertEqual(one['unique_viewable_images'],1)
            self.assertEqual(first.read_bytes(),raw)
            self.assertEqual(second.read_bytes(),raw)
            db=archive.open_catalog(private)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM files').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],2)
            db.close()
            self.assertNotIn('private-name',status.read_text())
            self.assertNotIn(str(root),status.read_text())
            self.assertTrue((export/'index.html').exists())

    def test_changed_source_keeps_old_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'source'
            (root/'msg').mkdir(parents=True)
            source=root/'msg'/'file.txt'
            private=Path(tmp)/'private'
            source.write_bytes(b'old')
            archive.archive(root,private,Path(tmp)/'export',Path(tmp)/'status')
            source.write_bytes(b'new')
            archive.archive(root,private,Path(tmp)/'export',Path(tmp)/'status')
            for value in (b'old',b'new'):
                sha=hashlib.sha256(value).hexdigest()
                self.assertEqual((private/'blobs'/sha[:2]/sha).read_bytes(),value)

    def test_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'source'
            (root/'msg').mkdir(parents=True)
            outside=Path(tmp)/'outside'
            outside.mkdir()
            (outside/'secret').write_bytes(b'outside')
            try:
                (root/'msg'/'escape').symlink_to(outside,target_is_directory=True)
            except OSError:
                self.skipTest('symlink privilege unavailable')
            files,skipped=archive.inventory(root)
            self.assertEqual(files,[])
            self.assertEqual(skipped,1)


if __name__=='__main__': unittest.main()
