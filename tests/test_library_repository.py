"""Synthetic tests for local management metadata; never open production data."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


if importlib.util.find_spec("dashboard.library"):
    from dashboard.library import (
        LibraryConflictError,
        LibraryNotFoundError,
        LibraryRepository,
        LibraryValidationError,
    )
    from dashboard.library import repository
else:
    LibraryRepository = None


class LibraryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(LibraryRepository, "Persistent library repository is not implemented")
        scratch = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="library-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        self.repo = LibraryRepository(self.data)
        self.db = self.data / "private/library/library.sqlite3"
        self.source = {"id": "synthetic__001", "contact": "合成联系人", "source": "wechat",
                       "message_count": 3, "has_stats": True, "date_range": ["2026-01-01", "2026-01-03"]}

    def seed(self):
        self.repo.reconcile([self.source])
        return self.repo.snapshot()["conversations"][0]

    def test_reconcile_is_idempotent_and_stable_identity_keeps_manual_metadata(self):
        row = self.seed()
        self.assertEqual(row["bundle_id"], self.source["id"])
        self.assertEqual(row["version"], 0)
        tag = self.repo.create_tag("朋友", "#112233")
        changed = self.repo.update_conversation(row["bundle_id"], 0, {
            "alias": "我的备注", "note": "下次聚会", "pinned": True, "hidden": True, "tag_ids": [tag["id"]],
        })
        changed_source = {**self.source, "contact": "导入后的名字", "message_count": 9}
        self.repo.reconcile([changed_source])
        first = self.repo.snapshot()
        self.repo.reconcile([changed_source])
        self.assertEqual(first, self.repo.snapshot())
        actual = first["conversations"][0]
        self.assertEqual(actual["source"], changed_source)
        for field in ("alias", "note", "pinned", "hidden_at", "tags", "version"):
            self.assertEqual(actual[field], changed[field])

    def test_missing_source_and_reimport_preserve_tombstone(self):
        self.seed()
        before = self.repo.update_conversation(self.source["id"], 0, {"hidden": True})
        self.repo.reconcile([])
        missing = self.repo.snapshot()["conversations"][0]
        self.assertTrue(missing["source_missing"])
        self.assertEqual(missing["hidden_at"], before["hidden_at"])
        self.repo.reconcile([self.source])
        restored_source = self.repo.snapshot()["conversations"][0]
        self.assertFalse(restored_source["source_missing"])
        self.assertEqual(restored_source["hidden_at"], before["hidden_at"])
        restored = self.repo.update_conversation(self.source["id"], 1, {"hidden": False})
        self.assertIsNone(restored["hidden_at"])

    def test_preferences_persist_across_repository_instances(self):
        self.seed()
        self.repo.update_conversation(self.source["id"], 0, {"alias": "持久备注"})
        self.assertEqual(LibraryRepository(self.data).snapshot()["conversations"][0]["alias"], "持久备注")

    def test_stale_version_is_rejected_without_partial_changes(self):
        self.seed()
        self.repo.update_conversation(self.source["id"], 0, {"alias": "first"})
        with self.assertRaises(LibraryConflictError):
            self.repo.update_conversation(self.source["id"], 0, {"alias": "stale", "hidden": True})
        actual = self.repo.snapshot()["conversations"][0]
        self.assertEqual(actual["alias"], "first")
        self.assertIsNone(actual["hidden_at"])

    def test_conversation_mutation_is_strictly_bounded(self):
        self.seed()
        for patch in ({"id": "other"}, {"source": {}}, {"alias": None}, {"note": 2},
                      {"alias": "a" * 121}, {"note": "a" * 4001}, {"alias": "bad\x00"},
                      {"note": "bad\x01"}, {"pinned": 1}, {"hidden": "false"},
                      {"tag_ids": "oops"}, {"tag_ids": ["missing"]}, {}, []):
            with self.subTest(patch=patch), self.assertRaises(LibraryValidationError):
                self.repo.update_conversation(self.source["id"], 0, patch)
        for version in (True, -1, "0", None):
            with self.subTest(version=version), self.assertRaises(LibraryValidationError):
                self.repo.update_conversation(self.source["id"], version, {"alias": "new"})
        self.assertEqual(self.repo.snapshot()["conversations"][0]["version"], 0)

    def test_unknown_conversation_never_creates_source(self):
        with self.assertRaises(LibraryNotFoundError):
            self.repo.update_conversation("missing__001", 0, {"alias": "x"})
        self.assertEqual(self.repo.snapshot()["conversations"], [])

    def test_tag_crud_soft_delete_restore_and_version_conflicts(self):
        self.seed()
        tag = self.repo.create_tag("朋友", "#112233")
        self.assertEqual(tag["version"], 0)
        self.repo.update_conversation(self.source["id"], 0, {"tag_ids": [tag["id"]]})
        changed = self.repo.update_tag(tag["id"], 0, {"name": "同学", "color": "#aabbcc"})
        self.assertEqual(changed["version"], 1)
        with self.assertRaises(LibraryConflictError):
            self.repo.update_tag(tag["id"], 0, {"name": "过时"})
        deleted = self.repo.update_tag(tag["id"], 1, {"deleted": True})
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.repo.snapshot()["conversations"][0]["tags"], [tag["id"]])
        with self.assertRaises(LibraryValidationError):
            self.repo.update_conversation(self.source["id"], 1, {"tag_ids": [tag["id"]]})
        restored = self.repo.update_tag(tag["id"], 2, {"deleted": False})
        self.assertFalse(restored["deleted"])
        self.repo.update_conversation(self.source["id"], 1, {"tag_ids": []})
        self.assertEqual(self.repo.snapshot()["conversations"][0]["tags"], [])

    def test_tag_validation_and_normalized_duplicate_name(self):
        tag = self.repo.create_tag("Work", "#123abc")
        with self.assertRaises(LibraryConflictError):
            self.repo.create_tag(" work ", "#123abc")
        for name, color in (("", "#123abc"), ("x" * 65, "#123abc"),
                            ("bad\x7f", "#123abc"), ("x", "red"), ("x", 3)):
            with self.subTest(name=name, color=color), self.assertRaises(LibraryValidationError):
                self.repo.create_tag(name, color)
        for patch in ({"id": "other"}, {"deleted": 1}, {"color": "#000"}):
            with self.subTest(patch=patch), self.assertRaises(LibraryValidationError):
                self.repo.update_tag(tag["id"], 0, patch)
        with self.assertRaises(LibraryNotFoundError):
            self.repo.update_tag("00000000-0000-0000-0000-000000000000", 0, {"name": "missing"})

    def test_editing_active_tags_preserves_invisible_soft_deleted_memberships(self):
        self.seed()
        deleted = self.repo.create_tag("稍后恢复", "#112233")
        active = self.repo.create_tag("当前标签", "#445566")
        self.repo.update_conversation(self.source["id"], 0, {"tag_ids": [deleted["id"], active["id"]]})
        self.repo.update_tag(deleted["id"], 0, {"deleted": True})
        self.repo.update_conversation(self.source["id"], 1, {"note": "仅修改备注"})
        changed = self.repo.update_conversation(self.source["id"], 2, {"tag_ids": []})
        self.assertEqual(changed["tags"], [deleted["id"]])
        self.repo.update_tag(deleted["id"], 1, {"deleted": False})
        restored = self.repo.snapshot()["conversations"][0]
        self.assertEqual(restored["tags"], [deleted["id"]])
        self.assertEqual(restored["note"], "仅修改备注")

    def test_module_settings_start_at_zero_and_persist_optimistically(self):
        self.assertEqual(self.repo.module_settings(), {})
        setting = self.repo.update_module("search", False, 0)
        self.assertFalse(setting["enabled"])
        self.assertEqual(setting["version"], 1)
        with self.assertRaises(LibraryConflictError):
            self.repo.update_module("search", True, 0)
        self.repo.update_module("search", True, 1)
        self.assertEqual(LibraryRepository(self.data).module_settings()["search"], {"enabled": True, "version": 2})
        for module_id, enabled, version in (("../bad", True, 0), ("search", 1, 2), ("search", False, True)):
            with self.assertRaises(LibraryValidationError):
                self.repo.update_module(module_id, enabled, version)

    def test_module_dependency_snapshot_is_compared_inside_mutation(self):
        original = self.repo.module_settings()
        self.repo.update_module("media", True, 0, expected_settings=original)
        with self.assertRaises(LibraryConflictError):
            self.repo.update_module("voice", True, 0, expected_settings=original)
        self.assertNotIn("voice", self.repo.module_settings())
        current = self.repo.module_settings()
        self.repo.update_module("voice", True, 0, expected_settings=current)
        self.assertTrue(self.repo.module_settings()["voice"]["enabled"])

    def test_source_validation_prevents_raw_messages_and_duplicate_identity(self):
        self.seed()
        for summaries in ([{**self.source, "messages": [{"text": "synthetic raw secret"}]}],
                          [self.source, self.source], [{**self.source, "id": "../escape"}],
                          [{**self.source, "message_count": True}], "oops"):
            with self.subTest(summaries=summaries), self.assertRaises(LibraryValidationError):
                self.repo.reconcile(summaries)
        self.assertEqual(self.repo.snapshot()["conversations"][0]["source"], self.source)

    def test_source_names_preserve_valid_emoji_format_characters(self):
        source = {**self.source, "id": "synthetic\u200d__001", "contact": "合成开发者 👩\u200d💻"}
        self.repo.reconcile([source])
        self.assertEqual(self.repo.snapshot()["conversations"][0]["source"], source)
        changed = self.repo.update_conversation(source["id"], 0, {"alias": "备注"})
        self.assertEqual(changed["bundle_id"], source["id"])

    def test_parallel_same_process_reads_and_reconcile_do_not_report_busy(self):
        self.seed()

        def request(index):
            other = LibraryRepository(self.data)
            if index % 3 == 0:
                other.reconcile([self.source])
            elif index % 3 == 1:
                other.module_settings()
            return other.snapshot()["conversations"][0]["bundle_id"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(request, range(32)))
        self.assertEqual(results, [self.source["id"]] * 32)

    def test_health_and_audit_contain_only_aggregate_or_change_metadata(self):
        self.seed()
        self.repo.update_conversation(self.source["id"], 0, {"note": "private synthetic note"})
        health = self.repo.health()
        self.assertEqual(health["schema_version"], 1)
        self.assertEqual(health["conversation_count"], 1)
        self.assertNotIn(str(self.data), json.dumps(health))
        self.assertNotIn(self.source["contact"], json.dumps(health, ensure_ascii=False))
        connection = sqlite3.connect(self.db)
        try:
            audit = connection.execute("SELECT action, fields_json FROM audit_log").fetchall()
            self.assertTrue(any(row[0] == "conversation.update" for row in audit))
            self.assertNotIn("private synthetic note", json.dumps(audit))
            self.assertNotIn("messages", {row[1] for row in connection.execute("PRAGMA table_info(conversations)")})
        finally:
            connection.close()

    def test_writer_lock_conflict_is_safe_and_recoverable(self):
        self.seed()
        lock_path = self.db.parent / "writer.lock"
        with lock_path.open("r+b") as handle:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(LibraryConflictError) as raised:
                    self.repo.update_conversation(self.source["id"], 0, {"alias": "busy"})
                self.assertNotIn(str(self.data), str(raised.exception))
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.repo.update_conversation(self.source["id"], 0, {"alias": "retry"})

    def test_hardlinked_database_lock_and_sidecars_are_rejected(self):
        self.seed()
        original = Path(self.temp.name) / "external-fixture"
        original.write_bytes(b"synthetic unchanged")
        for name in ("writer.lock", "library.sqlite3-wal", "library.sqlite3-shm", "library.sqlite3-journal"):
            target = self.db.parent / name
            if target.exists():
                target.unlink()
            os.link(original, target)
            try:
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    self.repo.health()
                self.assertEqual(original.read_bytes(), b"synthetic unchanged")
            finally:
                target.unlink()
        database_link = Path(self.temp.name) / "linked-db"
        os.link(self.db, database_link)
        with self.assertRaises(RuntimeError):
            self.repo.health()
        database_link.unlink()
        self.assertEqual(self.repo.health()["conversation_count"], 1)

    def test_symlinked_private_directory_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            (self.data / "private").symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.data / "private"), str(outside)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            self.assertEqual(result.returncode, 0, "Synthetic junction setup failed")
        with self.assertRaises(RuntimeError):
            self.repo.health()
        self.assertEqual(list(outside.iterdir()), [])

    def test_changed_migration_checksum_is_rejected_without_data_loss(self):
        self.seed()
        self.repo.update_conversation(self.source["id"], 0, {"alias": "saved"})
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("UPDATE schema_migrations SET checksum = 'changed'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RuntimeError):
            self.repo.snapshot()
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(connection.execute("SELECT alias FROM conversations").fetchone()[0], "saved")
        finally:
            connection.close()

    def test_unknown_schema_version_is_rejected(self):
        self.seed()
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("PRAGMA user_version=99")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RuntimeError):
            self.repo.snapshot()

    def test_unknown_schema_table_is_rejected_without_removal(self):
        self.seed()
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("CREATE TABLE unsupported (value TEXT)")
            connection.execute("INSERT INTO unsupported VALUES ('synthetic preserved')")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RuntimeError):
            self.repo.snapshot()
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(connection.execute("SELECT value FROM unsupported").fetchone()[0], "synthetic preserved")
        finally:
            connection.close()

    def test_failed_migration_rolls_back_and_preserves_prior_data(self):
        self.seed()
        with mock.patch.object(repository, "_MIGRATIONS", (*repository._MIGRATIONS,
                               "CREATE TABLE temporary_migration_marker (id INTEGER); INVALID SQL;")):
            with self.assertRaises(RuntimeError):
                LibraryRepository(self.data).snapshot()
        self.assertEqual(self.repo.snapshot()["conversations"][0]["source"], self.source)
        connection = sqlite3.connect(self.db)
        try:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='temporary_migration_marker'").fetchone())
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            connection.close()

    def test_capacity_failure_keeps_previous_preferences_and_allows_retry(self):
        self.seed()
        original_size = self.db.stat().st_size
        with mock.patch.object(repository, "MAX_DATABASE_BYTES", original_size):
            with self.assertRaises(RuntimeError):
                self.repo.update_conversation(self.source["id"], 0, {"note": "合" * 4000})
        actual = self.repo.snapshot()["conversations"][0]
        self.assertEqual(actual["note"], "")
        self.assertEqual(actual["version"], 0)
        self.repo.update_conversation(self.source["id"], 0, {"note": "retry"})


if __name__ == "__main__":
    unittest.main()
