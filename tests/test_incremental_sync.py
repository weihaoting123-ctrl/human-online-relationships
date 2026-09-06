"""Synthetic incremental sync checks; never discover a real account or DB."""

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_all_wechat as sync


class IncrementalSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, path in {
            "PRIVATE_ROOT": self.root / "private",
            "STATUS_PATH": self.root / "private" / "status.json",
            "RAW_ARCHIVE_ROOT": self.root / "private" / "raw-archive",
            "CONTACTS_DIR": self.root / "contacts",
        }.items():
            self.stack.enter_context(mock.patch.object(sync, name, path))
        self.stats = self.stack.enter_context(mock.patch.object(sync, "_run_stats", side_effect=self.fake_stats))
        self.reader = SimpleNamespace(sqlcipher=sqlite3)
        self.records = []
        self.runs = 0

    def fake_stats(self, _messages_path, bundle_dir):
        sync._atomic_write_json(bundle_dir / "stats.json", {"synthetic": True})
        return True

    def shard(self, number, talkers=("wxid_example",)):
        path = self.root / f"message_{number}.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
            for talker in talkers:
                connection.execute("INSERT INTO Name2Id VALUES (?)", (talker,))
                table = "Msg_" + hashlib.md5(talker.encode()).hexdigest()
                connection.execute(f'''CREATE TABLE "{table}" (
                    local_id, server_id, local_type, sort_seq, real_sender_id,
                    create_time, status, upload_status, download_status,
                    server_seq, origin_source, source, message_content, compress_content
                )''')
        record = {"path": str(path), "name": f"message/message_{number}.db", "salt": str(number + 1) * 32, "key": "a" * 64}
        self.records.append(record)
        return path

    def insert(self, path, local_id, text="synthetic example", timestamp=1700000000, talker="wxid_example", writer=None):
        table = "Msg_" + hashlib.md5(talker.encode()).hexdigest()
        connection = writer or sqlite3.connect(path)
        try:
            connection.execute(
                f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (local_id, str(1000 + local_id), 1, local_id, 1, timestamp, 0, 0, 0, 0, "", "", text, None),
            )
            connection.commit()
        finally:
            if writer is None:
                connection.close()

    def run_once(self, full=False, account="synthetic-account"):
        self.runs += 1
        run_root = self.root / "raw" / str(self.runs)
        status = sync._base_status("scheduled")
        status["last_run_at"] = sync._iso()
        if full:
            status["sync_strategy"] = "full-reconcile"
        with sync._database_snapshot_scope():
            sync._sync_sessions(self.reader, self.records, account, run_root, status)
        return status, run_root

    def test_unchanged_run_never_snapshots_queries_merges_or_restats(self):
        source = self.shard(0)
        self.insert(source, 1)
        first, _ = self.run_once()
        self.assertEqual(first["imported_messages"], 1)
        self.stats.reset_mock()
        with (
            mock.patch.object(sync._DatabaseSnapshotSet, "path_for", side_effect=AssertionError("unchanged snapshot")),
            mock.patch.object(sync, "_query_messages", side_effect=AssertionError("unchanged query")),
            mock.patch.object(sync, "merge_into_bundle", side_effect=AssertionError("unchanged merge")),
        ):
            second, run_root = self.run_once()
        self.assertEqual(second["read_messages"], 0)
        self.assertEqual(second["skipped_databases"], 1)
        self.assertEqual(second["processed_databases"], 0)
        self.assertEqual(second["skipped_conversations"], 1)
        self.assertFalse((run_root / "sessions").exists())
        self.stats.assert_not_called()

    def test_changed_shard_retains_late_historical_message(self):
        source = self.shard(0)
        untouched = self.shard(1, ("wxid_other",))
        self.insert(source, 1)
        self.insert(untouched, 8, talker="wxid_other")
        self.run_once()
        self.insert(source, 2, timestamp=1500000000)
        second, _ = self.run_once()
        self.assertEqual(second["skipped_databases"], 1)
        self.assertEqual(second["processed_databases"], 1)
        self.assertEqual(second["imported_messages"], 1)
        self.assertEqual(second["read_messages"], 2)
        bundle = sync._existing_bundle_path("wxid_example", "unused")
        data = json.loads((bundle / "messages.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][0]["timestamp"], 1500000000)

    def test_in_place_message_change_is_archived_without_source_deletion(self):
        source = self.shard(0)
        self.insert(source, 1)
        self.run_once()
        table = "Msg_" + hashlib.md5(b"wxid_example").hexdigest()
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute(f'UPDATE "{table}" SET message_content=? WHERE local_id=1', ("edited synthetic",))
        second, run_root = self.run_once()
        self.assertEqual(second["processed_databases"], 1)
        self.assertEqual(second["imported_conversations"], 1)
        self.assertEqual(len(list((run_root / "sessions").glob("*/messages.json"))), 1)
        with closing(sqlite3.connect(source)) as connection, connection:
            self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 1)

    def test_changed_db_without_message_change_skips_raw_merge_and_stats(self):
        source = self.shard(0)
        self.insert(source, 1)
        self.run_once()
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute("CREATE TABLE auxiliary(value INTEGER)")
        self.stats.reset_mock()
        with mock.patch.object(sync, "merge_into_bundle", side_effect=AssertionError("unchanged merge")):
            second, run_root = self.run_once()
        self.assertEqual(second["read_messages"], 1)
        self.assertEqual(second["skipped_conversations"], 1)
        self.assertFalse((run_root / "sessions").exists())
        self.stats.assert_not_called()

    def test_new_shard_imports_without_requerying_older_shard(self):
        source = self.shard(0)
        self.insert(source, 1)
        self.run_once()
        new = self.shard(1)
        self.insert(new, 2, timestamp=1600000000)
        second, _ = self.run_once()
        self.assertEqual(second["read_messages"], 1)
        self.assertEqual(second["skipped_databases"], 1)
        self.assertEqual(second["imported_messages"], 1)

    def test_wal_only_write_detected_and_source_members_untouched(self):
        source = self.shard(0)
        writer = sqlite3.connect(source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        self.insert(source, 1, writer=writer)
        self.run_once()
        self.insert(source, 2, timestamp=1400000000, writer=writer)
        before = sync._member_digests(source)
        second, _ = self.run_once()
        after = sync._member_digests(source)
        self.assertEqual(second["imported_messages"], 1)
        self.assertEqual(second["processed_databases"], 1)
        self.assertEqual(before, after)

    def test_mmap_like_same_metadata_change_is_detected_by_digest(self):
        source = self.root / "synthetic.db"
        source.write_bytes(b"a" * 4096)
        wal = Path(str(source) + "-wal")
        wal.write_bytes(b"b" * 4096)
        with mock.patch.object(sync, "_source_member_signatures", return_value=((1, 2, 4096, 0, 0), (1, 3, 4096, 0, 0), None)):
            before = sync._stable_database_fingerprint(source)
            wal.write_bytes(b"c" * 4096)
            after = sync._stable_database_fingerprint(source)
        self.assertEqual(before["metadata"], after["metadata"])
        self.assertNotEqual(before["sha256"], after["sha256"])

    def test_failure_does_not_advance_checkpoint_and_next_run_retries(self):
        source = self.shard(0)
        self.insert(source, 1)
        self.run_once()
        checkpoint_before = sync._checkpoint_path().read_bytes()
        self.insert(source, 2)
        with mock.patch.object(sync, "_run_stats", return_value=False):
            failed, _ = self.run_once()
        self.assertEqual(failed["failed_conversations"], 1)
        self.assertEqual(sync._checkpoint_path().read_bytes(), checkpoint_before)
        retried, _ = self.run_once()
        self.assertEqual(retried["failed_conversations"], 0)
        self.assertEqual(retried["processed_databases"], 1)
        self.assertEqual(retried["read_messages"], 2)
        self.assertNotEqual(sync._checkpoint_path().read_bytes(), checkpoint_before)

    def test_missing_derived_stats_triggers_safe_repair(self):
        source = self.shard(0)
        self.insert(source, 1)
        self.run_once()
        bundle = sync._existing_bundle_path("wxid_example", "unused")
        (bundle / "stats.json").unlink()
        self.stats.reset_mock()
        repaired, _ = self.run_once()
        self.assertEqual(repaired["read_messages"], 1)
        self.stats.assert_called_once()
        self.assertTrue((bundle / "stats.json").is_file())
        self.assertEqual(repaired["skipped_databases"], 0)
        self.assertEqual(repaired["processed_databases"], 1)

    def test_initial_stats_failure_retries_without_a_checkpoint(self):
        source = self.shard(0)
        self.insert(source, 1)
        with mock.patch.object(sync, "_run_stats", return_value=False):
            failed, _ = self.run_once()
        self.assertEqual(failed["failed_conversations"], 1)
        self.assertFalse(sync._checkpoint_path().exists())
        retried, _ = self.run_once()
        self.assertEqual(retried["failed_conversations"], 0)
        self.stats.assert_called_once()
        self.assertTrue(sync._checkpoint_path().exists())

    def test_account_bound_checkpoint_and_explicit_full_reconcile(self):
        source = self.shard(0)
        self.insert(source, 1)
        self.run_once()
        self.assertEqual(sync._load_incremental_checkpoint("another-account"), {})
        full, _ = self.run_once(full=True)
        self.assertEqual(full["skipped_databases"], 0)
        self.assertEqual(full["processed_databases"], 1)
        self.assertEqual(full["read_messages"], 1)
        self.assertEqual(full["imported_messages"], 0)

    def test_changed_shard_uses_name_mapping_from_cached_unchanged_shard(self):
        mapping = self.shard(0)
        messages = self.shard(1)
        self.insert(messages, 1)
        with closing(sqlite3.connect(messages)) as connection, connection:
            connection.execute("DELETE FROM Name2Id")
        self.run_once()
        self.insert(messages, 2)
        second, _ = self.run_once()
        self.assertEqual(second["skipped_databases"], 1)
        self.assertEqual(second["imported_messages"], 1)

    def test_public_incremental_status_has_no_checkpoint_metadata(self):
        status = sync._base_status("scheduled")
        status.update({"sources": {"private-source": {}}, "contact_names": {"private-id": "private-name"}})
        public = sync.public_status(status)
        self.assertEqual(public["sync_strategy"], "changed-shards")
        self.assertEqual(public["read_messages"], 0)
        self.assertNotIn("private-", json.dumps(public))

    def test_cached_key_inventory_does_not_eagerly_snapshot_all_history(self):
        self.shard(0)
        with (
            sync._database_snapshot_scope() as snapshots,
            mock.patch.object(sync, "_load_nt_reader", return_value=self.reader),
            mock.patch.object(sync, "_database_inventory", return_value=self.records),
            mock.patch.object(sync, "_cache_records", return_value={"databases": self.records}),
            mock.patch.object(snapshots, "prepare", side_effect=AssertionError("eager snapshot")),
        ):
            _reader, records = sync._database_records("synthetic-account", cache_only=True)
            self.assertEqual(records, self.records)
            self.assertFalse(snapshots._paths)

    def test_next_run_matches_daily_20_china_time(self):
        self.assertEqual(sync._next_scheduled_run(datetime(2026, 9, 6, 11, 59, tzinfo=timezone.utc)),
                         "2026-09-06T20:00:00+08:00")
        self.assertEqual(sync._next_scheduled_run(datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)),
                         "2026-09-07T20:00:00+08:00")
        self.assertEqual(sync._next_scheduled_run(datetime(2026, 9, 6, 22, 0, tzinfo=timezone.utc)),
                         "2026-09-07T20:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
