import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import message_merge
from convert_weflow_cli import convert_payload as convert_weflow_cli_payload
from message_merge import BundleIdentityError, archive_raw_export, merge_into_bundle, merge_payloads
from message_normalizer import normalize_payload


def chat_payload(messages, *, contact="wxid_friend", owner="wxid_me"):
    return {
        "source": "weflow-cli",
        "contact_username": contact,
        "contact_display": "contact",
        "own_wxid": owner,
        "messages": messages,
    }


def stable_message(message_id, content, timestamp=1_700_000_000, **extra):
    message = {
        "local_id": message_id,
        "source_message_id": message_id,
        "source_message_id_kind": "server_id",
        "source_message_namespace": "weflow-cli",
        "sender": "them",
        "timestamp": timestamp,
        "type": "text",
        "content": content,
    }
    message.update(extra)
    return message


def fallback_message(local_id, content="same", timestamp=1_700_000_000, **extra):
    message = {
        "local_id": local_id,
        "sender": "them",
        "timestamp": timestamp,
        "type": "text",
        "content": content,
    }
    message.update(extra)
    return message


class PayloadMergeTests(unittest.TestCase):
    def test_stable_ids_are_idempotent(self):
        incoming = chat_payload([
            stable_message("server-1", "first"),
            stable_message("server-2", "second", 1_700_000_001),
        ])
        first, first_counts = merge_payloads(None, incoming)
        second, second_counts = merge_payloads(first, incoming)

        self.assertEqual(first_counts["added"], 2)
        self.assertEqual(second_counts["duplicates"], 2)
        self.assertEqual(second_counts["stable_id_duplicates"], 2)
        self.assertEqual(second_counts["total_after"], 2)
        self.assertEqual(second["messages"], first["messages"])

    def test_fallback_fingerprint_preserves_legitimate_repeated_messages(self):
        incoming = chat_payload([
            fallback_message(1),
            fallback_message(2),
        ])
        first, first_counts = merge_payloads(None, incoming)
        second, second_counts = merge_payloads(first, incoming)

        self.assertEqual(first_counts["added"], 2)
        self.assertEqual(len(first["messages"]), 2)
        self.assertEqual(second_counts["fingerprint_duplicates"], 2)
        self.assertEqual(len(second["messages"]), 2)

    def test_fallback_uses_durable_metadata_to_avoid_false_match(self):
        existing = chat_payload([fallback_message(1, attachment_sha256="aaa")])
        incoming = chat_payload([fallback_message(9, attachment_sha256="bbb")])
        merged, counts = merge_payloads(existing, incoming)

        self.assertEqual(counts["duplicates"], 0)
        self.assertEqual(counts["added"], 1)
        self.assertEqual(len(merged["messages"]), 2)

    def test_same_stable_id_enriches_missing_fields_without_overwrite(self):
        existing = chat_payload([
            stable_message("voice-1", "[voice]", type="voice"),
        ])
        incoming = chat_payload([
            stable_message("voice-1", "[voice]", type="voice", transcript="transcript"),
        ])
        merged, counts = merge_payloads(existing, incoming)

        self.assertEqual(counts["duplicates"], 1)
        self.assertEqual(counts["enriched"], 1)
        self.assertEqual(counts["stable_id_conflicts"], 1)
        self.assertEqual(merged["messages"][0]["transcript"], "transcript")

    def test_conflicting_stable_id_keeps_current_record(self):
        existing = chat_payload([stable_message("server-1", "old-version")])
        incoming = chat_payload([stable_message("server-1", "new-version")])
        merged, counts = merge_payloads(existing, incoming)

        self.assertEqual(counts["stable_id_conflicts"], 1)
        self.assertEqual(counts["duplicates"], 1)
        self.assertEqual(merged["messages"][0]["content"], "old-version")

    def test_existing_stable_duplicates_are_compacted(self):
        existing = chat_payload([
            stable_message("server-1", "same"),
            stable_message("server-1", "same"),
        ])
        merged, counts = merge_payloads(existing, chat_payload([]))

        self.assertEqual(counts["existing_stable_duplicates_removed"], 1)
        self.assertEqual(len(merged["messages"]), 1)

    def test_contact_or_owner_mismatch_is_rejected(self):
        existing = chat_payload([stable_message("server-1", "same")])
        with self.assertRaises(BundleIdentityError):
            merge_payloads(existing, chat_payload([], contact="wxid_other"))
        with self.assertRaises(BundleIdentityError):
            merge_payloads(existing, chat_payload([], owner="wxid_other_owner"))

    def test_server_id_is_stable_across_source_partitions(self):
        existing_message = stable_message(
            "server-1", "same", source_partition="database-shard-a"
        )
        incoming_message = stable_message(
            "server-1", "same", source_partition="database-shard-b"
        )
        merged, counts = merge_payloads(
            chat_payload([existing_message]), chat_payload([incoming_message])
        )

        self.assertEqual(counts["stable_id_duplicates"], 1)
        self.assertEqual(len(merged["messages"]), 1)

    def test_local_id_is_scoped_to_source_partition(self):
        existing_message = stable_message(
            "local-1",
            "same",
            source_message_id_kind="local_id",
            source_partition="database-shard-a",
        )
        incoming_message = stable_message(
            "local-1",
            "same",
            source_message_id_kind="local_id",
            source_partition="database-shard-b",
        )
        merged, counts = merge_payloads(
            chat_payload([existing_message]), chat_payload([incoming_message])
        )

        self.assertEqual(counts["duplicates"], 0)
        self.assertEqual(counts["added"], 1)
        self.assertEqual(len(merged["messages"]), 2)

    def test_local_id_without_partition_falls_back_instead_of_colliding(self):
        existing_message = stable_message(
            "local-1", "first", source_message_id_kind="local_id"
        )
        incoming_message = stable_message(
            "local-1", "second", source_message_id_kind="local_id"
        )
        merged, counts = merge_payloads(
            chat_payload([existing_message]), chat_payload([incoming_message])
        )

        self.assertEqual(counts["stable_id_duplicates"], 0)
        self.assertEqual(counts["added"], 1)
        self.assertEqual(len(merged["messages"]), 2)

    def test_unscoped_local_id_does_not_hide_an_available_server_id(self):
        message = stable_message(
            "local-1", "same", source_message_id_kind="local_id", serverId="server-1"
        )
        identity = message_merge.stable_message_identity(message, "weflow-cli")

        self.assertIsNotNone(identity)
        self.assertEqual(identity[1], "server_id")
        self.assertEqual(identity[2], "global")

    def test_fallback_fingerprint_is_scoped_to_source_partition(self):
        existing_message = fallback_message(1, sourcePartition="database-shard-a")
        incoming_message = fallback_message(1, sourcePartition="database-shard-b")
        merged, counts = merge_payloads(
            chat_payload([existing_message]), chat_payload([incoming_message])
        )

        self.assertEqual(counts["fingerprint_duplicates"], 0)
        self.assertEqual(len(merged["messages"]), 2)

    def test_raw_partition_value_is_replaced_by_opaque_token(self):
        private_partition = r"C:\private\account\MSG0.db"
        merged, _counts = merge_payloads(
            None,
            chat_payload([fallback_message(1, sourcePartition=private_partition)]),
        )
        serialized = json.dumps(merged, ensure_ascii=False)
        partition = merged["messages"][0]["source_partition"]

        self.assertNotIn(private_partition, serialized)
        self.assertRegex(partition, r"^p_[0-9a-f]{24}$")


class BundleStorageTests(unittest.TestCase):
    def test_raw_export_is_content_addressed_and_source_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "export.json"
            original = b'{"private":"raw-body"}'
            raw.write_bytes(original)

            first = archive_raw_export(raw, root / "private" / "raw")
            second = archive_raw_export(raw, root / "private" / "raw")

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["path"], second["path"])
            self.assertEqual(Path(first["path"]).read_bytes(), original)
            self.assertEqual(raw.read_bytes(), original)

    def test_bundle_update_is_atomic_versioned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "contact" / "messages.json"
            raw = root / "export.json"
            raw.write_text('{"raw":"private"}', encoding="utf-8")
            incoming = chat_payload([stable_message("server-1", "private-body")])

            first = merge_into_bundle(
                incoming,
                target,
                raw_export_path=raw,
                raw_archive_dir=root / "private" / "raw",
            )
            first_bytes = target.read_bytes()
            second = merge_into_bundle(
                incoming,
                target,
                raw_export_path=raw,
                raw_archive_dir=root / "private" / "raw",
            )

            self.assertEqual(first["counts"]["written"], 1)
            self.assertEqual(first["counts"]["raw_archives_created"], 1)
            self.assertEqual(second["counts"]["written"], 0)
            self.assertEqual(second["counts"]["raw_archives_reused"], 1)
            self.assertEqual(target.read_bytes(), first_bytes)
            self.assertEqual(raw.read_text(encoding="utf-8"), '{"raw":"private"}')

            expanded = chat_payload([
                stable_message("server-1", "private-body"),
                stable_message("server-2", "another-body", 1_700_000_001),
            ])
            third = merge_into_bundle(expanded, target)
            snapshots = list((target.parent / ".history").glob("messages-*.json"))
            self.assertEqual(third["counts"]["history_snapshots_created"], 1)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].read_bytes(), first_bytes)

    def test_failed_atomic_replace_leaves_current_bundle_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "contact" / "messages.json"
            merge_into_bundle(chat_payload([stable_message("server-1", "old")]), target)
            before = target.read_bytes()
            expanded = chat_payload([
                stable_message("server-1", "old"),
                stable_message("server-2", "new", 1_700_000_001),
            ])

            with mock.patch.object(message_merge.os, "replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    merge_into_bundle(expanded, target)

            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(target.parent.glob(".messages.json.*.tmp")), [])
            # The inert file may remain, but OS lock ownership was released.
            with message_merge._exclusive_bundle_lock(target):
                pass

    def test_process_crash_releases_os_bundle_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "bundle" / "messages.json"
            child_code = (
                "import os, sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from message_merge import _exclusive_bundle_lock\n"
                "with _exclusive_bundle_lock(Path(sys.argv[2])):\n"
                "    os._exit(0)\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", child_code, str(SCRIPTS_DIR), str(target)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )

            self.assertEqual(result.returncode, 0)
            lock_path = target.with_name(".messages.json.merge.lock")
            self.assertTrue(lock_path.exists())
            self.assertEqual(lock_path.read_bytes(), b"\0")
            with message_merge._exclusive_bundle_lock(target):
                pass

    def test_os_bundle_lock_rejects_a_concurrent_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "bundle" / "messages.json"
            child_code = (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from message_merge import _exclusive_bundle_lock\n"
                "with _exclusive_bundle_lock(Path(sys.argv[2])):\n"
                "    print('ready', flush=True)\n"
                "    sys.stdin.readline()\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(SCRIPTS_DIR), str(target)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(message_merge.ConcurrentMergeError):
                    with message_merge._exclusive_bundle_lock(target):
                        pass
            finally:
                child.communicate("\n", timeout=10)

            self.assertEqual(child.returncode, 0)
            with message_merge._exclusive_bundle_lock(target):
                pass

    def test_identity_mismatch_does_not_write_or_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "contact" / "messages.json"
            merge_into_bundle(chat_payload([stable_message("server-1", "old")]), target)
            before = target.read_bytes()
            raw = root / "new-export.json"
            raw.write_text('{"raw":"not-associated"}', encoding="utf-8")
            archive_dir = root / "private" / "raw"

            with self.assertRaises(BundleIdentityError):
                merge_into_bundle(
                    chat_payload([], contact="wxid_other"),
                    target,
                    raw_export_path=raw,
                    raw_archive_dir=archive_dir,
                )

            self.assertEqual(target.read_bytes(), before)
            self.assertFalse(archive_dir.exists())

    def test_cli_stdout_contains_counts_but_no_message_or_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "normalized.json"
            source.write_text(
                json.dumps(
                    chat_payload([stable_message("secret-id", "secret-message-body")], contact="secret-contact"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "message_merge.py"),
                    "--input",
                    str(source),
                    "--output",
                    str(root / "bundle" / "messages.json"),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            output = json.loads(result.stdout)
            self.assertEqual(output["status"], "ok")
            self.assertEqual(output["counts"]["added"], 1)
            self.assertNotIn("secret-message-body", result.stdout)
            self.assertNotIn("secret-contact", result.stdout)
            self.assertNotIn("secret-id", result.stdout)


class WeflowIdentityTests(unittest.TestCase):
    def test_converter_prefers_server_id_and_marks_namespace(self):
        converted = convert_weflow_cli_payload([{
            "serverId": "server-1",
            "msgSvrId": "server-2",
            "messageId": "server-3",
            "localId": 7,
            "localType": 1,
            "createTime": 1_700_000_000,
            "isSend": 0,
            "parsedContent": "body",
        }], "contact", "wxid_friend")
        message = normalize_payload(converted)["messages"][0]

        self.assertEqual(message["source_message_id"], "server-1")
        self.assertEqual(message["source_message_id_kind"], "server_id")
        self.assertEqual(message["source_message_namespace"], "weflow-cli")

    def test_converter_marks_explicit_local_id_but_not_synthetic_index(self):
        with_local = convert_weflow_cli_payload([{
            "localId": 8,
            "localType": 1,
            "createTime": 1_700_000_000,
            "isSend": 0,
            "parsedContent": "body",
        }], "contact", "wxid_friend")["messages"][0]
        without_id = convert_weflow_cli_payload([{
            "localType": 1,
            "createTime": 1_700_000_001,
            "isSend": 0,
            "parsedContent": "body",
        }], "contact", "wxid_friend")["messages"][0]

        self.assertEqual(with_local["source_message_id"], 8)
        self.assertEqual(with_local["source_message_id_kind"], "local_id")
        self.assertNotIn("source_message_id", without_id)

    def test_converter_hashes_partition_metadata_before_storing_it(self):
        private_partition = r"C:\private\account\MSG0.db"
        converted = convert_weflow_cli_payload({
            "sourcePartition": private_partition,
            "messages": [{
                "localId": 8,
                "localType": 1,
                "createTime": 1_700_000_000,
                "isSend": 0,
                "parsedContent": "body",
            }],
        }, "contact", "wxid_friend")
        message = converted["messages"][0]

        self.assertNotEqual(message["source_partition"], private_partition)
        self.assertRegex(message["source_partition"], r"^p_[0-9a-f]{24}$")


if __name__ == "__main__":
    unittest.main()
