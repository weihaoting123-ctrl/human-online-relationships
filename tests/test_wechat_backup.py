"""Synthetic recovery/safety tests; temporary data stays in this D-drive project."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import backup_wechat_local as backup

LEGACY_SOURCE_ROOTS = {
    "contacts": "data/contacts", "raw": "data/raw",
    "archive": "data/private/wechat-archive", "voice": "data/private/wechat-voice",
    "media_exports": "data/exports/wechat-media", "voice_exports": "data/exports/wechat-voice",
}
LIBRARY_SOURCE_ROOTS = {**LEGACY_SOURCE_ROOTS, "library": "data/private/library"}


class WeChatBackupTests(unittest.TestCase):
    def setUp(self):
        temporary_root = REPO / "scripts/tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="backup_test_", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "data/contacts/synthetic/messages.json"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'{"messages":[{"text":"synthetic only"}]}')
        self.destination = self.root / "backups/wechat"

    def run_backup(self):
        return backup.run_backup(self.root, reserve_bytes=0)

    def manifest(self):
        return backup._current(self.destination)[1]

    def object_for(self, key="data/contacts/synthetic/messages.json"):
        return backup._object_path(self.destination, self.manifest()["files"][key]["object"])

    def legacy_snapshot(self):
        """Literal v1 on-disk fixture, independent of the current writer version."""
        content = self.source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        obj = backup._object_path(self.destination, digest)
        obj.parent.mkdir(parents=True)
        obj.write_bytes(content)
        snapshot_id = "20260905T120000Z_" + "a" * 32
        manifest = {
            "schema_version": 1, "snapshot_id": snapshot_id,
            "created_at": "2026-09-05T12:00:00+00:00", "snapshot_count": 1,
            "source_roots": dict(LEGACY_SOURCE_ROOTS),
            "source_presence": {key: key == "contacts" for key in LEGACY_SOURCE_ROOTS},
            "parent": None,
            "files": {self.source.relative_to(self.root).as_posix(): {
                "object": digest, "size": len(content), "kind": "file",
                "source_fingerprint": [backup._fingerprint(self.source)],
                "object_fingerprint": backup._fingerprint(obj),
            }},
            "counts": {"files": 1, "bytes": len(content),
                       "categories": {"text": 1, "voice": 0, "images": 0, "other": 0}},
        }
        path = self.destination / "snapshots" / (snapshot_id + ".json")
        checksum = backup._atomic_json(path, manifest)
        reference = {"snapshot_id": snapshot_id, "manifest_sha256": checksum}
        backup._atomic_json(self.destination / "current.json", reference)
        return path, reference, manifest

    def test_new_snapshots_use_version_two_fixed_source_roots(self):
        result = self.run_backup()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(self.manifest()["schema_version"], 2)
        self.assertEqual(self.manifest()["source_roots"], LIBRARY_SOURCE_ROOTS)

    def test_legacy_snapshot_verify_restore_and_mixed_history_preserve_old_bytes(self):
        path, legacy_ref, _ = self.legacy_snapshot()
        original = path.read_bytes()
        pointer = (self.destination / "current.json").read_bytes()
        verified = backup.verify_backup(self.root)
        self.assertTrue(verified["verification_complete"], verified)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((self.destination / "current.json").read_bytes(), pointer)
        self.assertEqual(backup.restore_snapshot(self.root, "legacy_before", reserve_bytes=0)["state"], "completed")
        self.source.write_bytes(b"synthetic new revision")
        updated = self.run_backup()
        self.assertEqual(updated["state"], "completed", updated)
        current = self.manifest()
        self.assertEqual(current["schema_version"], 2)
        self.assertEqual(current["parent"], legacy_ref)
        self.assertEqual(path.read_bytes(), original)
        history = backup.get_history(self.root)
        self.assertEqual([row["snapshot_id"] for row in history], [current["snapshot_id"], legacy_ref["snapshot_id"]])
        restored = backup.restore_snapshot(self.root, "legacy_after", snapshot_id=legacy_ref["snapshot_id"], reserve_bytes=0)
        self.assertEqual(restored["state"], "completed", restored)
        previous = self.root / "backups/restored/legacy_before" / self.source.relative_to(self.root)
        after = self.root / "backups/restored/legacy_after" / self.source.relative_to(self.root)
        self.assertEqual(previous.read_bytes(), after.read_bytes())
        self.assertTrue(backup.verify_backup(self.root)["verification_complete"])

    def test_upgrade_without_new_sources_copies_zero_objects_then_stays_unchanged(self):
        path, legacy_ref, _ = self.legacy_snapshot()
        original = path.read_bytes()
        upgraded = self.run_backup()
        self.assertEqual(upgraded["state"], "completed", upgraded)
        self.assertEqual(upgraded["counts"]["objects_copied"], 0)
        self.assertEqual(upgraded["snapshot_count"], 2)
        self.assertEqual(self.manifest()["schema_version"], 2)
        self.assertEqual(self.manifest()["parent"], legacy_ref)
        self.assertEqual(path.read_bytes(), original)
        repeated = self.run_backup()
        self.assertEqual(repeated["counts"]["objects_copied"], 0)
        self.assertEqual(repeated["snapshot_id"], upgraded["snapshot_id"])
        self.assertEqual(len(backup.get_history(self.root)), 2)

    def test_library_sqlite_wal_and_preferences_restore_without_changing_source(self):
        db_path = self.root / "data/private/library/library.sqlite3"
        db_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(db_path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE preferences (id TEXT PRIMARY KEY, note TEXT, hidden_at TEXT)")
        connection.execute("INSERT INTO preferences VALUES ('synthetic-id', 'synthetic note', '2026-09-06')")
        connection.commit()
        before = {suffix: Path(str(db_path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        result = self.run_backup()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["counts"]["sqlite_snapshots"], 1)
        key = db_path.relative_to(self.root).as_posix()
        manifest = self.manifest()
        self.assertEqual(manifest["files"][key]["kind"], "sqlite")
        self.assertTrue(manifest["source_presence"]["library"])
        self.assertFalse(any(name.endswith(("-wal", "-shm", ".lock")) for name in manifest["files"]))
        for suffix, content in before.items():
            self.assertEqual(Path(str(db_path) + suffix).read_bytes(), content)
        restored = backup.restore_snapshot(self.root, "library_restore", reserve_bytes=0)
        self.assertEqual(restored["state"], "completed", restored)
        recovered = self.root / "backups/restored/library_restore" / key
        reader = sqlite3.connect(recovered.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(reader.execute("SELECT * FROM preferences").fetchall(),
                             [("synthetic-id", "synthetic note", "2026-09-06")])
            self.assertEqual(reader.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        finally:
            reader.close()
        repeated = self.run_backup()
        self.assertEqual(repeated["counts"]["objects_copied"], 0)
        self.assertEqual(repeated["snapshot_id"], result["snapshot_id"])

    def test_library_writer_lock_rejects_backup_before_publication(self):
        private = self.root / "data/private/library"
        private.mkdir(parents=True)
        with backup._file_lock(private / "writer.lock"):
            result = self.run_backup()
        self.assertEqual(result["state"], "busy", result)
        self.assertEqual(result["error_code"], "BUSY")
        self.assertFalse((self.destination / "current.json").exists())

    def test_each_manifest_version_rejects_unknown_or_mismatched_source_roots(self):
        path, reference, original = self.legacy_snapshot()
        invalid = [
            (1, LIBRARY_SOURCE_ROOTS), (2, LEGACY_SOURCE_ROOTS),
            (2, {**LIBRARY_SOURCE_ROOTS, "library": "data/private/scoped-ai"}),
            (99, LIBRARY_SOURCE_ROOTS), (True, LEGACY_SOURCE_ROOTS),
        ]
        for version, roots in invalid:
            with self.subTest(version=version, roots=roots):
                value = {**original, "schema_version": version, "source_roots": roots}
                digest = backup._atomic_json(path, value)
                backup._atomic_json(self.destination / "current.json", {**reference, "manifest_sha256": digest})
                self.assertEqual(backup.verify_backup(self.root)["error_code"], "MANIFEST_INVALID")

    def test_legacy_manifest_cannot_claim_new_library_path(self):
        path, reference, original = self.legacy_snapshot()
        original["files"] = {"data/private/library/library.sqlite3": next(iter(original["files"].values()))}
        digest = backup._atomic_json(path, original)
        backup._atomic_json(self.destination / "current.json", {**reference, "manifest_sha256": digest})
        result = backup.restore_snapshot(self.root, "outside_legacy_scope", reserve_bytes=0)
        self.assertEqual(result["error_code"], "MANIFEST_INVALID", result)
        self.assertFalse((self.root / "backups/restored/outside_legacy_scope").exists())

    def test_independent_copies_dedup_and_byte_exact_restore(self):
        duplicate = self.source.with_name("duplicate.json")
        duplicate.write_bytes(self.source.read_bytes())
        image = self.root / "data/exports/wechat-media/image.png"
        voice = self.root / "data/exports/wechat-voice/voice.wav"
        for path, content in ((image, b"\x89PNG\r\n\x1a\nsynthetic pixels"), (voice, b"RIFFsynthetic audio")):
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        source_before = self.source.stat()
        result = self.run_backup()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["counts"]["files"], 4)
        self.assertEqual(result["counts"]["objects_copied"], 3)
        self.assertNotEqual(self.object_for().stat().st_ino, self.source.stat().st_ino)
        self.assertEqual(source_before.st_mtime_ns, self.source.stat().st_mtime_ns)
        restored = backup.restore_snapshot(self.root, "synthetic_restore", reserve_bytes=0)
        self.assertEqual(restored["state"], "completed", restored)
        for path in (self.source, duplicate, image, voice):
            output = self.root / "backups/restored/synthetic_restore" / path.relative_to(self.root)
            self.assertEqual(path.read_bytes(), output.read_bytes())
            self.assertNotEqual(path.stat().st_ino, output.stat().st_ino)

    def test_unchanged_regular_files_skip_all_content_reads_and_snapshot_creation(self):
        first = self.run_backup()
        with mock.patch.object(backup, "_stable_copy", side_effect=AssertionError("unexpected copy")), \
                mock.patch.object(backup, "_hash_file", side_effect=AssertionError("unexpected hash")), \
                mock.patch.object(backup, "_is_sqlite", side_effect=AssertionError("unexpected header read")):
            second = self.run_backup()
        self.assertEqual(second["state"], "completed", second)
        self.assertEqual(second["counts"]["unchanged"], 1)
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(second["snapshot_count"], 1)

    def test_versions_and_deleted_sources_remain_recoverable(self):
        first = self.run_backup()
        original = self.source.read_bytes()
        first_object = self.object_for()
        self.source.write_bytes(b"synthetic second version")
        second = self.run_backup()
        other = self.source.with_name("remaining.json")
        other.write_bytes(b"synthetic remaining")
        self.source.unlink()
        third = self.run_backup()
        self.assertEqual(third["snapshot_count"], 3)
        self.assertTrue(first_object.exists())
        self.assertEqual(len(backup.get_history(self.root)), 3)
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        restored = backup.restore_snapshot(self.root, "old_version", snapshot_id=first["snapshot_id"], reserve_bytes=0)
        self.assertEqual(restored["state"], "completed", restored)
        self.assertEqual((self.root / "backups/restored/old_version" / self.source.relative_to(self.root)).read_bytes(), original)

    def test_missing_object_is_rebuilt_without_discarding_history(self):
        first = self.run_backup()
        obj = self.object_for()
        obj.unlink()
        second = self.run_backup()
        self.assertEqual(second["state"], "completed", second)
        self.assertEqual(obj.read_bytes(), self.source.read_bytes())
        self.assertEqual(second["counts"]["objects_copied"], 1)
        self.assertGreaterEqual(second["snapshot_count"], first["snapshot_count"])

    def test_corruption_is_detected_by_verify_and_restore_without_source_reads(self):
        initial = self.run_backup()
        obj = self.object_for()
        saved = obj.stat()
        obj.write_bytes(b"x" * saved.st_size)
        os.utime(obj, ns=(saved.st_atime_ns, saved.st_mtime_ns))
        with mock.patch.object(backup, "_inventory", side_effect=AssertionError("source scan")), \
                mock.patch.object(backup, "_is_sqlite", side_effect=AssertionError("source read")):
            verified = backup.verify_backup(self.root)
        self.assertEqual(verified["error_code"], "OBJECT_CORRUPT", verified)
        self.assertEqual(verified["snapshot_id"], initial["snapshot_id"])
        restored = backup.restore_snapshot(self.root, "corrupt_attempt", reserve_bytes=0)
        self.assertEqual(restored["error_code"], "OBJECT_CORRUPT", restored)
        self.assertFalse((self.root / "backups/restored/corrupt_attempt").exists())

    def test_verify_is_bounded_and_does_not_create_snapshot(self):
        self.source.with_name("different.json").write_bytes(b"different synthetic bytes")
        first = self.run_backup()
        limited = backup.verify_backup(self.root, max_objects=1)
        self.assertEqual(limited["state"], "completed", limited)
        self.assertEqual(limited["counts"]["verified"], 1)
        self.assertFalse(limited["verification_complete"])
        full = backup.verify_backup(self.root)
        self.assertTrue(full["verification_complete"])
        self.assertEqual(full["snapshot_id"], first["snapshot_id"])
        self.assertEqual(len(backup.get_history(self.root)), 1)

    def test_sqlite_wal_is_consistent_and_source_db_remains_unchanged(self):
        db_path = self.root / "data/private/wechat-voice/catalog.sqlite3"
        db_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(db_path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE synthetic (value TEXT)")
        connection.execute("INSERT INTO synthetic VALUES ('synthetic committed row')")
        connection.commit()
        before = {suffix: Path(str(db_path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        result = self.run_backup()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["counts"]["sqlite_snapshots"], 1)
        for suffix, content in before.items():
            self.assertEqual(Path(str(db_path) + suffix).read_bytes(), content)
        manifest = self.manifest()
        self.assertFalse(any(key.endswith(("-wal", "-shm")) for key in manifest["files"]))
        restored = backup.restore_snapshot(self.root, "sqlite_restore", reserve_bytes=0)
        self.assertEqual(restored["state"], "completed", restored)
        restored_db = self.root / "backups/restored/sqlite_restore" / db_path.relative_to(self.root)
        with sqlite3.connect(restored_db.as_uri() + "?mode=ro", uri=True) as check:
            self.assertEqual(check.execute("SELECT value FROM synthetic").fetchall(), [("synthetic committed row",)])
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        check.close()

    def test_credentials_and_outside_allowlist_and_temporary_files_are_excluded(self):
        excluded = ("data/private/scoped-ai/config.json", "data/private/wechat-sync/key-cache.json",
                    "data/raw/key-cache.json", "data/raw/config.json", "data/raw/.env",
                    "data/contacts/synthetic/merge.lock", "data/raw/unfinished.tmp",
                    "data/raw/temp/partial.json", "reports/private.txt")
        for name in excluded:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"excluded synthetic secret")
        result = self.run_backup()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(set(self.manifest()["files"]), {self.source.relative_to(self.root).as_posix()})
        for blob in (self.destination / "objects").glob("*/*"):
            self.assertNotIn(b"excluded synthetic secret", blob.read_bytes())

    def test_raw_wechat_sync_history_is_included_but_private_sync_credentials_are_not(self):
        raw = self.root / "data/raw/wechat-sync/synthetic_run/messages.json"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"synthetic immutable raw export")
        credential = self.root / "data/private/wechat-sync/key-cache.json"
        credential.parent.mkdir(parents=True)
        credential.write_bytes(b"synthetic excluded credential")
        result = self.run_backup()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["counts"]["files"], 2)
        self.assertIn(raw.relative_to(self.root).as_posix(), self.manifest()["files"])
        self.assertNotIn(credential.relative_to(self.root).as_posix(), self.manifest()["files"])

    def test_immutable_sqlite_blob_and_raw_sidecars_keep_exact_bytes(self):
        original_db = self.root / "data/raw/imported.sqlite3"
        original_db.parent.mkdir(parents=True)
        connection = sqlite3.connect(original_db)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE fixture (number INTEGER)")
        connection.execute("INSERT INTO fixture VALUES (42)")
        connection.commit()
        digest = hashlib.sha256(original_db.read_bytes()).hexdigest()
        blob = self.root / "data/private/wechat-archive/blobs" / digest[:2] / digest
        blob.parent.mkdir(parents=True)
        blob.write_bytes(original_db.read_bytes())
        expected = {path.relative_to(self.root).as_posix(): path.read_bytes()
                    for path in (original_db, Path(str(original_db) + "-wal"), Path(str(original_db) + "-shm"), blob)}
        result = self.run_backup()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["counts"]["sqlite_snapshots"], 0)
        manifest = self.manifest()
        for key, content in expected.items():
            self.assertEqual(manifest["files"][key]["kind"], "file")
            self.assertEqual(backup._object_path(self.destination, manifest["files"][key]["object"]).read_bytes(), content)
        self.assertEqual(manifest["files"][blob.relative_to(self.root).as_posix()]["object"], digest)

    def test_live_sqlite_wal_changes_are_captured_even_when_stat_baseline_is_reused(self):
        db_path = self.root / "data/private/wechat-voice/transcripts.sqlite3"
        db_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(db_path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE fixture (value INTEGER)")
        connection.execute("INSERT INTO fixture VALUES (1)")
        connection.commit()
        self.assertEqual(self.run_backup()["state"], "completed")
        first_obj = self.object_for(db_path.relative_to(self.root).as_posix())
        connection.execute("UPDATE fixture SET value=2")
        connection.commit()
        # Force only the incremental comparison's prior metadata; the copy still
        # validates actual handles. A stat-only fast path would skip this DB.
        current_manifest = self.manifest()
        key = db_path.relative_to(self.root).as_posix()
        current_manifest["files"][key]["source_fingerprint"] = backup._sqlite_fingerprint(db_path)
        reference = json.loads((self.destination / "current.json").read_text())
        digest = backup._atomic_json(self.destination / "snapshots" / (reference["snapshot_id"] + ".json"), current_manifest)
        backup._atomic_json(self.destination / "current.json", {**reference, "manifest_sha256": digest})
        second = self.run_backup()
        self.assertEqual(second["state"], "completed", second)
        self.assertEqual(second["counts"]["sqlite_snapshots"], 1)
        self.assertNotEqual(first_obj, self.object_for(key))
        restored = backup.restore_snapshot(self.root, "updated_wal", reserve_bytes=0)
        self.assertEqual(restored["state"], "completed", restored)
        recovered = self.root / "backups/restored/updated_wal" / key
        with sqlite3.connect(recovered) as reader:
            self.assertEqual(reader.execute("SELECT value FROM fixture").fetchone(), (2,))
        reader.close()

    def test_writer_lock_prevents_catalog_inventory_races(self):
        private = self.root / "data/private/wechat-voice"
        private.mkdir(parents=True)
        with backup._file_lock(private / "archive.lock"):
            result = self.run_backup()
        self.assertEqual(result["state"], "busy", result)
        self.assertFalse((self.destination / "current.json").exists())

    def test_reparse_directory_is_rejected_without_symlink_privilege(self):
        original = backup._is_link
        root_info = self.root.stat()
        def reparse(info):
            return info.st_ino == root_info.st_ino or original(info)
        with mock.patch.object(backup, "_is_link", side_effect=reparse):
            result = self.run_backup()
        self.assertEqual(result["error_code"], "UNSAFE_PATH", result)
        self.assertFalse(self.destination.exists())

    def test_source_change_during_copy_never_publishes_completed_snapshot(self):
        first = self.run_backup()
        self.source.write_bytes(b"changed synthetic data")
        original_copy = backup._stable_copy
        def racing_copy(source, target, expected):
            result = original_copy(source, target, expected)
            if source == self.source:
                self.source.write_bytes(b"newer synthetic data during capture")
            return result
        with mock.patch.object(backup, "_stable_copy", side_effect=racing_copy):
            failed = self.run_backup()
        self.assertEqual(failed["error_code"], "SOURCE_CHANGED", failed)
        self.assertEqual(self.manifest()["snapshot_id"], first["snapshot_id"])
        self.assertEqual(len(backup.get_history(self.root)), 1)

    def test_file_added_after_inventory_prevents_publication(self):
        initial = self.run_backup()
        inventory = backup._inventory
        calls = 0
        def change_after_scan(root):
            nonlocal calls
            found = inventory(root)
            calls += 1
            if calls == 1:
                self.source.with_name("late.json").write_bytes(b"synthetic late arrival")
            return found
        with mock.patch.object(backup, "_inventory", side_effect=change_after_scan):
            failed = self.run_backup()
        self.assertEqual(failed["error_code"], "SOURCE_CHANGED", failed)
        self.assertEqual(self.manifest()["snapshot_id"], initial["snapshot_id"])

    def test_interrupted_pointer_publication_preserves_previous_snapshot(self):
        initial = self.run_backup()
        self.source.write_bytes(b"synthetic updated content")
        original = backup._atomic_json
        def interrupted(path, value):
            if path == self.destination / "current.json":
                raise KeyboardInterrupt()
            return original(path, value)
        with mock.patch.object(backup, "_atomic_json", side_effect=interrupted):
            failed = self.run_backup()
        self.assertEqual(failed["error_code"], "INTERRUPTED", failed)
        self.assertEqual(self.manifest()["snapshot_id"], initial["snapshot_id"])
        self.assertEqual(len(backup.get_history(self.root)), 1)
        self.assertEqual(backup.restore_snapshot(self.root, "before_interruption", reserve_bytes=0)["state"], "completed")

    def test_low_disk_preserves_previous_snapshot(self):
        initial = self.run_backup()
        self.source.write_bytes(b"synthetic changed data")
        with mock.patch.object(backup.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            failed = self.run_backup()
        self.assertEqual(failed["error_code"], "INSUFFICIENT_SPACE", failed)
        self.assertEqual(self.manifest()["snapshot_id"], initial["snapshot_id"])

    def test_concurrent_backup_refuses_without_overwriting_owner_status(self):
        self.run_backup()
        path = self.root / "data/private/wechat-backup/status.json"
        initial = path.read_bytes()
        with backup._lock(self.destination):
            result = self.run_backup()
        self.assertEqual(result["state"], "busy", result)
        self.assertEqual(path.read_bytes(), initial)

    def test_restore_rejects_existing_directory_and_path_traversal(self):
        self.run_backup()
        for value in ("../escape", "C:\\unsafe", "test:stream", "..", "/outside", "CON"):
            self.assertEqual(backup.restore_snapshot(self.root, value, reserve_bytes=0)["error_code"], "INVALID_ARGUMENT")
        target = self.root / "backups/restored/already_exists"
        target.mkdir(parents=True)
        (target / "keep.txt").write_bytes(b"synthetic existing file")
        result = backup.restore_snapshot(self.root, "already_exists", reserve_bytes=0)
        self.assertEqual(result["error_code"], "RESTORE_EXISTS")
        self.assertEqual((target / "keep.txt").read_bytes(), b"synthetic existing file")

    def test_manifest_hash_and_traversal_are_validated(self):
        self.run_backup()
        current = json.loads((self.destination / "current.json").read_text())
        manifest_path = self.destination / "snapshots" / (current["snapshot_id"] + ".json")
        original = manifest_path.read_bytes()
        manifest_path.write_bytes(original + b" ")
        self.assertEqual(backup.verify_backup(self.root)["error_code"], "MANIFEST_INVALID")
        original_value = json.loads(original)
        key = next(iter(original_value["files"]))
        for invalid in ("../escaped.json", "data/contacts/../../outside", "data/contacts/x:stream", "C:/outside", "data/contacts/CON", "data/contacts/test."):
            value = dict(original_value)
            value["files"] = {invalid: original_value["files"][key]}
            digest = backup._atomic_json(manifest_path, value)
            backup._atomic_json(self.destination / "current.json", {**current, "manifest_sha256": digest})
            result = backup.restore_snapshot(self.root, "invalid_manifest", reserve_bytes=0)
            self.assertEqual(result["error_code"], "MANIFEST_INVALID", result)

    def test_source_symlink_is_skipped_and_destination_link_is_refused(self):
        outside = self.root / "excluded_source.txt"
        outside.write_bytes(b"outside synthetic material")
        link = self.source.with_name("link.json")
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("OS does not permit synthetic symlinks")
        result = self.run_backup()
        self.assertEqual(result["counts"]["files"], 1)
        self.assertEqual(result["counts"]["skipped"], 1)
        obj = self.object_for()
        obj.unlink()
        obj.symlink_to(outside)
        self.assertEqual(self.run_backup()["error_code"], "UNSAFE_PATH")

    def test_destination_hardlink_is_refused_but_source_hardlink_is_copied(self):
        other = self.source.with_name("source_hardlink.json")
        os.link(self.source, other)
        initial = self.run_backup()
        self.assertEqual(initial["state"], "completed", initial)
        obj = self.object_for()
        alias = self.root / "backup_hardlink_alias"
        os.link(obj, alias)
        self.assertEqual(backup.verify_backup(self.root)["error_code"], "UNSAFE_PATH")

    def test_public_status_and_history_never_expose_paths_or_private_fields(self):
        self.run_backup()
        status_path = self.root / "data/private/wechat-backup/status.json"
        injected = json.loads(status_path.read_text())
        injected.update(account="synthetic secret", error_code="D:\\private path", snapshot_id="synthetic_account",
                        last_success_at="synthetic secret", exception="synthetic plaintext")
        injected["counts"]["unexpected"] = "synthetic secret"
        status_path.write_text(json.dumps(injected))
        public = json.dumps(backup.get_status(self.root))
        self.assertNotIn("synthetic", public)
        self.assertNotIn("private path", public)
        history = json.dumps(backup.get_history(self.root))
        self.assertNotIn("contacts", history)
        self.assertNotIn("messages.json", history)
        status_path.write_text(json.dumps({"state": [], "mode": {}, "error_code": [], "counts": []}))
        self.assertEqual(backup.get_status(self.root)["state"], "idle")

    def test_no_source_data_and_no_snapshot_are_clear_failures(self):
        self.source.unlink()
        self.assertEqual(self.run_backup()["error_code"], "NO_SOURCE_DATA")
        self.assertEqual(backup.verify_backup(self.root)["error_code"], "NO_SNAPSHOT")

    @unittest.skipUnless(os.name == "nt", "Windows drive restriction")
    def test_c_drive_project_is_rejected_before_writes(self):
        result = backup.run_backup(Path("C:/Users/synthetic-user/project"))
        self.assertEqual(result["error_code"], "INVALID_LOCATION")


if __name__ == "__main__":
    unittest.main()
