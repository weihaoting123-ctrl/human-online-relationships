import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import sync_all_wechat as sync
import watch_wechat_key as watcher


class WeChatSyncSafetyTests(unittest.TestCase):
    def test_public_status_drops_private_fields(self):
        status = sync.public_status({
            **sync._base_status("scheduled"),
            "state": "completed",
            "account_id": "wxid_private",
            "session_id": "room@chatroom",
            "database_key": "secret",
            "message": "chat body",
        })
        serialized = json.dumps(status)
        self.assertNotIn("wxid_private", serialized)
        self.assertNotIn("room@chatroom", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("chat body", serialized)

    def test_multiple_accounts_stop_before_database_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary) / "private"
            payload = {
                "ok": True,
                "db_path": temporary,
                "accounts": [
                    {"id": "wxid_first", "nickname": "账号甲", "modified_time": 2},
                    {"id": "wxid_second", "nickname": "账号乙", "modified_time": 1},
                ],
            }
            with (
                mock.patch.object(sync, "PRIVATE_ROOT", private_root),
                mock.patch.object(sync, "_run_account_bridge", return_value=payload),
            ):
                with self.assertRaises(sync.SyncFailure) as raised:
                    sync.discover_unique_account()
            self.assertEqual(raised.exception.code, "needs_account_selection")
            choices = (private_root / "account-choices.json").read_text(encoding="utf-8")
            self.assertNotIn("wxid_first", choices)
            self.assertNotIn("wxid_second", choices)

    def test_unique_account_can_proceed(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = {
                "ok": True,
                "db_path": temporary,
                "accounts": [{"id": "wxid_only", "nickname": "", "modified_time": 1}],
            }
            with mock.patch.object(sync, "_run_account_bridge", return_value=payload):
                account, root = sync.discover_unique_account()
            self.assertEqual(account, "wxid_only")
            self.assertEqual(root, Path(temporary).resolve())

    def test_session_ids_reject_path_traversal(self):
        self.assertTrue(sync._safe_session_id("wxid_friend"))
        self.assertTrue(sync._safe_session_id("room@chatroom"))
        self.assertFalse(sync._safe_session_id("../messages"))
        self.assertFalse(sync._safe_session_id("name/child"))
        self.assertFalse(sync._safe_session_id("name\\child"))

    def test_self_sender_matching_does_not_use_unsafe_prefix(self):
        self.assertTrue(sync._is_self_sender("wxid_synthetic_me", "wxid_synthetic_me"))
        self.assertTrue(sync._is_self_sender("wxid_synthetic_me", "wxid_synthetic_me_A1b2"))
        self.assertFalse(sync._is_self_sender("wxid_synthetic_me_other", "wxid_synthetic_me"))

    def test_existing_bundle_survives_contact_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            contacts = Path(temporary) / "contacts"
            contact_id = "wxid_friend"
            suffix = sync.hashlib.md5(contact_id.encode("utf-8")).hexdigest()[:8]
            existing = contacts / f"旧备注__{suffix}"
            existing.mkdir(parents=True)
            with mock.patch.object(sync, "CONTACTS_DIR", contacts):
                result = sync._existing_bundle_path(contact_id, "新备注")
            self.assertEqual(result, existing)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
    def test_dpapi_key_cache_round_trip(self):
        secret = "a" * 64
        protected = sync._protect_secret(secret)
        self.assertNotIn(secret, protected)
        self.assertEqual(sync._unprotect_secret(protected), secret)

    def test_dashboard_error_codes_are_coarse(self):
        self.assertEqual(
            sync._dashboard_error_code("database_key_unavailable"),
            "permission_required",
        )
        self.assertEqual(
            sync._dashboard_error_code("unexpected_private_detail"),
            "sync_failed",
        )

    def test_launcher_uses_project_virtual_environment(self):
        source = (REPO_ROOT / "sync_wechat.ps1").read_text(encoding="utf-8-sig")
        self.assertIn(".venv\\Scripts\\python.exe", source)
        self.assertIn("sync_all_wechat.py", source)
        self.assertNotIn("Start-Process", source)

    def test_sync_dependencies_are_pinned(self):
        requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("sqlcipher3==0.6.2", requirements)
        self.assertIn("pymem==1.14.0", requirements)

    def test_native_probe_output_is_silenced_and_restored(self):
        read_fd, write_fd = os.pipe()
        original_stderr = os.dup(2)
        try:
            os.dup2(write_fd, 2)
            with sync._silence_native_output():
                os.write(2, b"private-native-diagnostic")
            os.write(2, b"restored")
        finally:
            os.dup2(original_stderr, 2)
            os.close(original_stderr)
            os.close(write_fd)
        captured = os.read(read_fd, 4096)
        os.close(read_fd)
        self.assertEqual(captured, b"restored")

    def test_sync_assigns_an_opaque_partition_token(self):
        source = (REPO_ROOT / "scripts" / "sync_all_wechat.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"sourcePartition": partition_token', source)
        self.assertNotIn('"sourcePartition": str(context["record"].get("path")', source)

    def test_recent_wechat_passphrase_derivation_is_pinned(self):
        passphrase = "00" * 32
        salt = "11" * 16
        expected = sync.hashlib.pbkdf2_hmac(
            "sha512",
            bytes.fromhex(passphrase),
            bytes.fromhex(salt),
            256_000,
            dklen=32,
        ).hex()
        self.assertEqual(sync._derive_database_key(passphrase, salt), expected)

    def test_automatic_sync_never_enables_process_hook(self):
        source = (REPO_ROOT / "scripts" / "sync_all_wechat.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("allow_process_hook=allow_process_hook and not scheduled", source)
        launcher = (REPO_ROOT / "sync_wechat.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$AllowProcessHook -and -not $Scheduled", launcher)

    def test_scheduled_sync_only_starts_watcher_after_missing_key_status(self):
        launcher = (REPO_ROOT / "sync_wechat.ps1").read_text(encoding="utf-8-sig")
        sync_index = launcher.index("$syncOutput = @(& $pythonPath @arguments)")
        watcher_index = launcher.index("& $watcherLauncher | Out-Null")
        self.assertLess(sync_index, watcher_index)
        self.assertIn(
            '$publicStatus.state -eq "awaiting_wechat_restart"',
            launcher,
        )

    def test_watcher_launcher_treats_reused_pid_as_stale_metadata(self):
        launcher = (REPO_ROOT / "start_wechat_watcher.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertNotIn(
            "The recorded process is not this repository's WeChat watcher",
            launcher,
        )
        self.assertIn("Remove-Item -LiteralPath $pidFile -Force", launcher)

    def test_watcher_does_not_rearm_when_dpapi_cache_is_usable(self):
        with (
            mock.patch.object(
                watcher,
                "_watch_lock",
                return_value=mock.MagicMock(
                    __enter__=mock.Mock(return_value=True),
                    __exit__=mock.Mock(return_value=False),
                ),
            ),
            mock.patch.object(
                watcher.sync,
                "has_usable_key_cache",
                return_value=True,
            ),
            mock.patch.object(watcher, "_safe_status") as safe_status,
            mock.patch.object(watcher.sync, "_capture_hook_key") as capture,
        ):
            self.assertEqual(watcher.watch(1), 0)
        safe_status.assert_not_called()
        capture.assert_not_called()

    def test_bridge_environment_drops_unrelated_secrets(self):
        observed_environment = {}

        def fake_run(_arguments, **kwargs):
            observed_environment.update(kwargs["env"])
            return mock.Mock(stdout="", returncode=1)

        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.dict(os.environ, {
                    "PATH": os.environ.get("PATH", ""),
                    "DEEPSEEK_API_KEY": "private-ai-key",
                    "QCE_TOKEN": "private-chat-token",
                }, clear=True),
                mock.patch.object(sync, "PRIVATE_ROOT", Path(temporary)),
                mock.patch.object(
                    sync.subprocess,
                    "run",
                    side_effect=fake_run,
                ) as run,
            ):
                sync._capture_hook_key([123], timeout_per_pid_seconds=7)
        environment = run.call_args.kwargs["env"]
        self.assertIn("PATH", observed_environment)
        self.assertEqual(observed_environment["SHE_LOVE_ME_HOOK_TIMEOUT_MS"], "7000")
        self.assertRegex(
            observed_environment["SHE_LOVE_ME_KEY_WRAP_KEY"],
            r"^[0-9a-f]{64}$",
        )
        self.assertNotIn("DEEPSEEK_API_KEY", observed_environment)
        self.assertNotIn("QCE_TOKEN", observed_environment)
        self.assertNotIn("SHE_LOVE_ME_KEY_WRAP_KEY", environment)
        self.assertIs(run.call_args.kwargs["stdout"], sync.subprocess.PIPE)
        self.assertIs(run.call_args.kwargs["stderr"], sync.subprocess.DEVNULL)

    def test_hook_key_moves_only_through_authenticated_memory_envelope(self):
        secret = "a" * 64

        def fake_run(arguments, **kwargs):
            from Crypto.Cipher import AES

            wrapping_key = bytes.fromhex(kwargs["env"]["SHE_LOVE_ME_KEY_WRAP_KEY"])
            nonce = b"n" * 12
            cipher = AES.new(wrapping_key, AES.MODE_GCM, nonce=nonce)
            cipher.update(b"she-love-me-wechat-key-v1")
            ciphertext, tag = cipher.encrypt_and_digest(secret.encode("ascii"))
            return mock.Mock(
                stdout=sync.BRIDGE_MARKER + json.dumps({
                    "ok": True,
                    "key_envelope": {
                        "version": 1,
                        "algorithm": "A256GCM",
                        "nonce": sync.base64.b64encode(nonce).decode("ascii"),
                        "ciphertext": sync.base64.b64encode(ciphertext).decode("ascii"),
                        "tag": sync.base64.b64encode(tag).decode("ascii"),
                    },
                }),
                returncode=0,
            )

        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(sync, "PRIVATE_ROOT", Path(temporary)),
                mock.patch.object(sync.subprocess, "run", side_effect=fake_run) as run,
            ):
                captured = sync._capture_hook_key([123], timeout_per_pid_seconds=7)

        self.assertEqual(captured, secret)
        arguments = run.call_args.args[0]
        self.assertNotIn(secret, arguments)
        self.assertNotIn(secret, run.call_args.kwargs["env"].values())

        bridge = (REPO_ROOT / "scripts" / "weflow_local_bridge.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("SHE_LOVE_ME_KEY_WRAP_KEY", bridge)
        self.assertIn("createCipheriv('aes-256-gcm'", bridge)
        self.assertIn("key_envelope", bridge)
        self.assertNotIn("emit({ ok: true, key:", bridge)

    def test_hook_key_rejects_tampered_envelope(self):
        def fake_run(_arguments, **kwargs):
            return mock.Mock(
                stdout=sync.BRIDGE_MARKER + json.dumps({
                    "ok": True,
                    "key_envelope": {
                        "version": 1,
                        "algorithm": "A256GCM",
                        "nonce": sync.base64.b64encode(b"n" * 12).decode("ascii"),
                        "ciphertext": sync.base64.b64encode(b"x" * 64).decode("ascii"),
                        "tag": sync.base64.b64encode(b"t" * 16).decode("ascii"),
                    },
                }),
                returncode=0,
            )

        with (
            mock.patch.object(sync.subprocess, "run", side_effect=fake_run),
            mock.patch.object(sync, "PRIVATE_ROOT", Path("unused-private-root")),
        ):
            self.assertIsNone(sync._capture_hook_key([123]))

    def test_hook_key_rejects_legacy_stdout_secret(self):
        secret = "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(sync, "PRIVATE_ROOT", Path(temporary)),
                mock.patch.object(sync.subprocess, "run", return_value=mock.Mock(
                    stdout=sync.BRIDGE_MARKER + json.dumps({
                        "ok": True,
                        "key": secret,
                    }),
                    returncode=0,
                )),
            ):
                captured = sync._capture_hook_key([123], timeout_per_pid_seconds=7)
        self.assertIsNone(captured)

    def test_source_database_and_sidecars_are_never_opened_by_sqlcipher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "message_0.db"
            source.parent.mkdir()
            writer = sqlite3.connect(source)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE sample(value TEXT)")
                writer.execute("INSERT INTO sample VALUES ('fixture')")
                writer.commit()
                before = sync._source_member_signatures(source)
                self.assertIsNotNone(before[1])
                self.assertIsNotNone(before[2])

                opened = []

                class SqlcipherProxy:
                    @staticmethod
                    def connect(database, **kwargs):
                        opened.append(database)
                        return sqlite3.connect(database, **kwargs)

                nt_reader = SimpleNamespace(sqlcipher=SqlcipherProxy)
                record = {
                    "path": str(source),
                    "name": "message/message_0.db",
                    "salt": "b" * 32,
                    "key": "a" * 64,
                }
                with mock.patch.object(sync, "PRIVATE_ROOT", root / "private"):
                    connection = sync._open_readonly_database(nt_reader, record)
                    try:
                        self.assertEqual(
                            connection.execute("SELECT value FROM sample").fetchone()[0],
                            "fixture",
                        )
                    finally:
                        connection.close()

                after = sync._source_member_signatures(source)
                self.assertEqual(after, before)
                self.assertEqual(len(opened), 1)
                self.assertNotEqual(opened[0].split("?", 1)[0], source.as_uri())
                self.assertIn("db-snapshots", opened[0])
            finally:
                writer.close()

    def test_cached_records_never_hide_a_new_message_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path = root / "message_0.db"
            new_path = root / "message_1.db"
            old_path.touch()
            new_path.touch()
            old_record = {
                "path": str(old_path),
                "name": "message/message_0.db",
                "salt": "1" * 32,
                "key": "a" * 64,
            }
            inventory = [
                {key: old_record[key] for key in ("path", "name", "salt")},
                {
                    "path": str(new_path),
                    "name": "message/message_1.db",
                    "salt": "2" * 32,
                },
            ]
            with (
                mock.patch.object(sync, "_load_nt_reader", return_value=object()),
                mock.patch.object(sync, "_database_inventory", return_value=inventory),
                mock.patch.object(sync, "_cache_records", return_value={
                    "databases": [old_record],
                    "passphrase": "f" * 64,
                }),
                mock.patch.object(sync, "_apply_passphrase", return_value=False),
            ):
                with self.assertRaises(sync.SyncFailure) as raised:
                    sync._database_records("account")
            self.assertEqual(raised.exception.code, "database_key_unavailable")

    def test_scheduled_cache_only_path_never_starts_memory_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "message_0.db"
            path.touch()
            inventory = [{
                "path": str(path),
                "name": "message/message_0.db",
                "salt": "1" * 32,
            }]
            with (
                mock.patch.object(sync, "_load_nt_reader", return_value=object()),
                mock.patch.object(sync, "_database_inventory", return_value=inventory),
                mock.patch.object(sync, "_cache_records", return_value=None),
                mock.patch.object(sync, "_scan_database_keys") as scan,
            ):
                with self.assertRaises(sync.SyncFailure) as raised:
                    sync._database_records("account", cache_only=True)
        self.assertEqual(raised.exception.code, "database_key_unavailable")
        scan.assert_not_called()

    def test_memory_key_fallback_has_hard_candidate_and_time_budgets(self):
        self.assertLessEqual(sync.MAX_BINARY_CANDIDATES_PER_SALT, 16)
        self.assertLessEqual(sync.MAX_BINARY_CANDIDATES_TOTAL, 64)
        self.assertLessEqual(sync.MAX_BINARY_SCAN_SECONDS, 30)
        self.assertLessEqual(sync.MAX_PASSPHRASE_CANDIDATES, 16)
        self.assertLessEqual(sync.MAX_PASSPHRASE_VALIDATION_SECONDS, 30)

    def test_inventory_cross_checks_every_message_shard_on_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            message_dir = Path(temporary) / "db_storage" / "message"
            message_dir.mkdir(parents=True)
            known = message_dir / "message_0.db"
            omitted = message_dir / "message_1.db"
            known.touch()
            omitted.touch()
            nt_reader = SimpleNamespace(find_nt_databases=lambda: [{
                "wxid": "account",
                "path": str(known),
                "name": "message/message_0.db",
                "salt": "1" * 32,
            }])
            with self.assertRaises(sync.SyncFailure) as raised:
                sync._database_inventory(nt_reader, "account")
        self.assertEqual(raised.exception.code, "database_inventory_incomplete")

    def test_partition_created_during_snapshot_fails_closed(self):
        first = [{
            "path": "message_0.db",
            "name": "message/message_0.db",
            "salt": "1" * 32,
        }]
        second = [*first, {
            "path": "message_1.db",
            "name": "message/message_1.db",
            "salt": "2" * 32,
        }]
        snapshots = mock.Mock()
        token = sync._ACTIVE_DATABASE_SNAPSHOTS.set(snapshots)
        try:
            with (
                mock.patch.object(sync, "_load_nt_reader", return_value=object()),
                mock.patch.object(
                    sync,
                    "_database_inventory",
                    side_effect=[first, second],
                ),
            ):
                with self.assertRaises(sync.SyncFailure) as raised:
                    sync._database_records("account")
        finally:
            sync._ACTIVE_DATABASE_SNAPSHOTS.reset(token)
        self.assertEqual(raised.exception.code, "database_inventory_changed")

    def test_cached_records_refresh_when_same_path_has_a_new_salt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "message_0.db"
            path.touch()
            old_record = {
                "path": str(path),
                "name": "message/message_0.db",
                "salt": "1" * 32,
                "key": "a" * 64,
            }
            inventory = [{
                "path": str(path),
                "name": "message/message_0.db",
                "salt": "2" * 32,
            }]
            with (
                mock.patch.object(sync, "_load_nt_reader", return_value=object()),
                mock.patch.object(sync, "_database_inventory", return_value=inventory),
                mock.patch.object(sync, "_cache_records", return_value={
                    "databases": [old_record],
                    "passphrase": "f" * 64,
                }),
                mock.patch.object(sync, "_apply_passphrase", return_value=False),
            ):
                with self.assertRaises(sync.SyncFailure) as raised:
                    sync._database_records("account")
            self.assertEqual(raised.exception.code, "database_key_unavailable")

    def test_usable_cache_rejects_path_or_salt_coverage_gaps(self):
        cached = {
            "databases": [{
                "path": "message_0.db",
                "name": "message/message_0.db",
                "salt": "1" * 32,
                "key": "a" * 64,
            }],
            "passphrase": "f" * 64,
        }
        inventory = [{
            "path": "message_0.db",
            "name": "message/message_0.db",
            "salt": "2" * 32,
        }]
        with (
            mock.patch.object(sync, "discover_unique_account", return_value=(
                "account", Path("unused")
            )),
            mock.patch.object(sync, "_load_nt_reader", return_value=object()),
            mock.patch.object(sync, "_database_inventory", return_value=inventory),
            mock.patch.object(sync, "_cache_records", return_value=cached),
        ):
            self.assertFalse(sync.has_usable_key_cache())

    def test_unstable_source_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "message_0.db"
            source.write_bytes(b"fixture")
            signature_a = ((1, 1, 7, 1, 1), None, None)
            signature_b = ((1, 1, 7, 2, 2), None, None)
            signatures = [signature_a, signature_b] * sync.SOURCE_SNAPSHOT_RETRIES
            with (
                mock.patch.object(sync, "PRIVATE_ROOT", root / "private"),
                mock.patch.object(
                    sync,
                    "_source_member_signatures",
                    side_effect=signatures,
                ),
            ):
                snapshots = sync._DatabaseSnapshotSet()
                try:
                    with self.assertRaises(sync.SyncFailure) as raised:
                        snapshots.path_for(source)
                finally:
                    snapshots.close()
            self.assertEqual(raised.exception.code, "source_database_busy")

    def test_snapshot_rejects_content_change_when_metadata_is_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "message_0.db"
            source.write_bytes(b"fixture")
            digest_round = [
                ("source-before", None, None),
                ("copied-torn", None, None),
                ("source-before", None, None),
            ] * sync.SOURCE_SNAPSHOT_RETRIES
            with (
                mock.patch.object(sync, "PRIVATE_ROOT", root / "private"),
                mock.patch.object(sync, "_member_digests", side_effect=digest_round),
            ):
                snapshots = sync._DatabaseSnapshotSet()
                try:
                    with self.assertRaises(sync.SyncFailure) as raised:
                        snapshots.path_for(source)
                    self.assertEqual(list(snapshots.root.iterdir()), [])
                finally:
                    snapshots.close()
        self.assertEqual(raised.exception.code, "source_database_busy")

    def test_cache_filters_out_stale_database_superset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_path = root / "current" / "message_0.db"
            stale_path = root / "stale" / "message_0.db"
            current_path.parent.mkdir()
            stale_path.parent.mkdir()
            current_path.touch()
            stale_path.touch()
            current = {
                "path": str(current_path),
                "name": "message/message_0.db",
                "salt": "1" * 32,
            }
            stale = {
                "path": str(stale_path),
                "name": "message/message_0.db",
                "salt": "2" * 32,
            }
            cache = {
                "account_hash": sync._account_hash("account"),
                "databases": [
                    {**current, "protected_key": "a" * 64},
                    {**stale, "protected_key": "b" * 64},
                ],
            }
            with (
                mock.patch.object(sync, "_read_json", return_value=cache),
                mock.patch.object(sync, "_unprotect_secret", side_effect=lambda value: value),
            ):
                result = sync._cache_records("account", [current])
        self.assertIsNotNone(result)
        self.assertEqual(result["databases"], [{**current, "key": "a" * 64}])

    def test_partition_token_uses_source_path_and_salt(self):
        first = sync._source_partition_token({
            "path": "root-a/message_0.db",
            "salt": "1" * 32,
        })
        second = sync._source_partition_token({
            "path": "root-b/message_0.db",
            "salt": "1" * 32,
        })
        rotated = sync._source_partition_token({
            "path": "root-a/message_0.db",
            "salt": "2" * 32,
        })
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, rotated)

    def test_session_discovery_uses_real_tables_not_only_is_session_flag(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE Name2Id(user_name TEXT, is_session INTEGER)")
        active = "wxid_active"
        historical = "wxid_historical"
        structural = "wxid_synthetic_structure"
        connection.executemany(
            "INSERT INTO Name2Id(user_name, is_session) VALUES (?, ?)",
            [(active, 1), (historical, 0), (structural, 0)],
        )
        for talker in (active, historical):
            table = "Msg_" + sync.hashlib.md5(talker.encode()).hexdigest()
            connection.execute(f'CREATE TABLE "{table}"(value INTEGER)')

        with mock.patch.object(sync, "_open_readonly_database", return_value=connection):
            with sync._message_databases(object(), [{
                "name": "message/message_0.db",
            }]) as contexts:
                sessions = contexts[0]["sessions"]
                self.assertEqual(sessions, {active, historical})
                self.assertNotIn(structural, sessions)

    def test_auxiliary_stores_without_name_mapping_do_not_break_all_export(self):
        auxiliary = sqlite3.connect(":memory:")
        auxiliary.execute("CREATE TABLE Resource(value INTEGER)")
        with mock.patch.object(sync, "_open_readonly_database", return_value=auxiliary):
            with sync._message_databases(object(), [{"name": "message/message_resource.db"}]) as contexts:
                self.assertEqual(contexts[0]["sessions"], set())

    def _query_synthetic_message(self, message_content, compress_content):
        talker = "wxid_synthetic"
        table = "Msg_" + sync.hashlib.md5(talker.encode()).hexdigest()
        connection = sqlite3.connect(":memory:")
        connection.execute(f'''CREATE TABLE "{table}"(
            local_id, server_id, local_type, sort_seq, real_sender_id,
            create_time, status, upload_status, download_status,
            server_seq, origin_source, source, message_content, compress_content
        )''')
        connection.execute(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (1, "100", 1, 1, 1, 1700000000, 0, 0, 0, 0, "", "",
             message_content, compress_content),
        )
        context = {
            "record": {"path": "synthetic.db", "salt": "a" * 32},
            "sender_map": {1: talker},
            "message_tables": {table},
        }
        with mock.patch.object(sync, "_open_readonly_database", return_value=connection):
            return sync._query_messages(object(), context, talker, "wxid_self")[0]

    def test_compressed_message_body_reaches_normalized_bundle_and_keeps_bytes(self):
        import zstandard

        body = "压缩正文也应完整保留"
        compressed = zstandard.ZstdCompressor().compress(body.encode("utf-8"))
        raw = self._query_synthetic_message("旧版摘要", compressed)
        converted = sync.convert_weflow_cli_payload(
            {"messages": [raw]}, "合成会话", "wxid_synthetic", "wxid_self"
        )
        self.assertEqual(converted["messages"][0]["content"], body)
        self.assertEqual(sync.base64.b64decode(raw["compressContentBase64"]), compressed)
        self.assertEqual(raw["messageContentRaw"], "旧版摘要")
        self.assertNotIn("contentDecodeFailed", raw)

    def test_message_content_column_also_decodes_zstd_and_retains_raw_bytes(self):
        import zstandard

        body = "正文列中的压缩内容"
        compressed = zstandard.ZstdCompressor().compress(body.encode("utf-8"))
        raw = self._query_synthetic_message(compressed, None)
        self.assertEqual(raw["content"], body)
        self.assertEqual(sync.base64.b64decode(raw["messageContentBase64"]), compressed)

    def test_corrupt_compressed_body_is_explicit_and_keeps_fallback(self):
        corrupt = sync.ZSTD_FRAME_MAGIC + b"invalid-frame"
        raw = self._query_synthetic_message("可读备用正文", corrupt)
        self.assertEqual(raw["content"], "可读备用正文")
        self.assertTrue(raw["contentDecodeFailed"])
        self.assertEqual(sync.base64.b64decode(raw["compressContentBase64"]), corrupt)
        raw = self._query_synthetic_message(b"\xff\xfe", corrupt)
        self.assertIn("正文解码失败", raw["content"])
        self.assertTrue(raw["contentDecodeFailed"])

    def test_zstd_expansion_is_bounded_for_known_and_unknown_frame_sizes(self):
        import zstandard

        for known_size in (True, False):
            compressed = zstandard.ZstdCompressor(
                write_content_size=known_size
            ).compress(b"x" * 65)
            with mock.patch.object(sync, "MAX_MESSAGE_CONTENT_BYTES", 64):
                self.assertEqual(sync._decode_message_field(compressed), ("", True))

    def test_truncated_zstd_frame_never_yields_partial_success(self):
        import zstandard

        compressed = zstandard.ZstdCompressor(
            write_content_size=False
        ).compress(b"synthetic message body")
        self.assertEqual(sync._decode_message_field(compressed[:-1]), ("", True))

    def test_unmapped_message_table_fails_instead_of_silent_omission(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE Name2Id(user_name TEXT, is_session INTEGER)")
        connection.execute(
            'CREATE TABLE "Msg_00000000000000000000000000000000"(value INTEGER)'
        )
        with mock.patch.object(sync, "_open_readonly_database", return_value=connection):
            with self.assertRaises(sync.SyncFailure) as raised:
                with sync._message_databases(object(), [{
                    "name": "message/message_0.db",
                }]):
                    pass
        self.assertEqual(raised.exception.code, "message_index_incomplete")

    def test_session_table_can_use_name_mapping_from_another_partition(self):
        talker = "wxid_cross_partition"
        table = "Msg_" + sync.hashlib.md5(talker.encode()).hexdigest()
        mapping = sqlite3.connect(":memory:")
        messages = sqlite3.connect(":memory:")
        mapping.execute("CREATE TABLE Name2Id(user_name TEXT)")
        mapping.execute("INSERT INTO Name2Id(user_name) VALUES (?)", (talker,))
        messages.execute("CREATE TABLE Name2Id(user_name TEXT)")
        messages.execute(f'CREATE TABLE "{table}"(value INTEGER)')
        with mock.patch.object(
            sync,
            "_open_readonly_database",
            side_effect=[mapping, messages],
        ):
            with sync._message_databases(object(), [
                {"name": "message/message_0.db"},
                {"name": "message/message_1.db"},
            ]) as contexts:
                self.assertEqual(contexts[0]["sessions"], set())
                self.assertEqual(contexts[1]["sessions"], {talker})

    def test_watcher_rechecks_unique_account_before_hook(self):
        @contextmanager
        def acquired():
            yield True

        capture = mock.Mock()
        with (
            mock.patch.object(watcher, "_watch_lock", acquired),
            mock.patch.object(watcher, "_safe_status"),
            mock.patch.object(sync, "_weixin_pids", return_value=[123]),
            mock.patch.object(watcher, "_process_age_seconds", return_value=0),
            mock.patch.object(
                sync,
                "discover_unique_account",
                side_effect=sync.SyncFailure("needs_account_selection"),
            ),
            mock.patch.object(sync, "_capture_hook_key", capture),
        ):
            result = watcher.watch(1)
        self.assertEqual(result, 1)
        capture.assert_not_called()

    def test_watcher_rechecks_cache_immediately_before_hook(self):
        @contextmanager
        def acquired():
            yield True

        capture = mock.Mock()
        with (
            mock.patch.object(watcher, "_watch_lock", acquired),
            mock.patch.object(watcher, "_safe_status"),
            mock.patch.object(
                sync,
                "has_usable_key_cache",
                side_effect=[False, True],
            ),
            mock.patch.object(sync, "_weixin_pids", return_value=[22, 11]),
            mock.patch.object(watcher, "_process_age_seconds", return_value=0),
            mock.patch.object(sync, "_capture_hook_key", capture),
        ):
            result = watcher.watch(1)
        self.assertEqual(result, 0)
        capture.assert_not_called()

    def test_watcher_passes_all_fresh_pids_in_reader_priority_order(self):
        @contextmanager
        def acquired():
            yield True

        capture = mock.Mock(return_value="a" * 64)
        with (
            mock.patch.object(watcher, "_watch_lock", acquired),
            mock.patch.object(watcher, "_safe_status"),
            mock.patch.object(sync, "has_usable_key_cache", return_value=False),
            mock.patch.object(sync, "_weixin_pids", return_value=[22, 11]),
            mock.patch.object(watcher, "_process_age_seconds", return_value=0),
            mock.patch.object(
                sync,
                "discover_unique_account",
                return_value=("account", Path("unused")),
            ),
            mock.patch.object(sync, "_capture_hook_key", capture),
            mock.patch.object(watcher, "_handle_captured_key", return_value=0),
        ):
            result = watcher.watch(1)
        self.assertEqual(result, 0)
        capture.assert_called_once_with([22, 11], timeout_per_pid_seconds=120)

        bridge = (REPO_ROOT / "scripts" / "weflow_local_bridge.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("permissionDenied = true", bridge)
        self.assertIn("hookFailed = true\n      continue", bridge)

    def test_watcher_uses_bounded_second_scale_backoff(self):
        @contextmanager
        def acquired():
            yield True

        with (
            mock.patch.object(watcher, "_watch_lock", acquired),
            mock.patch.object(watcher, "_safe_status"),
            mock.patch.object(sync, "has_usable_key_cache", return_value=False),
            mock.patch.object(sync, "_weixin_pids", return_value=[]),
            mock.patch.object(watcher.time, "monotonic", side_effect=[0, 0, 4000]),
            mock.patch.object(watcher.time, "sleep") as sleep,
        ):
            result = watcher.watch(1)
        self.assertEqual(result, 0)
        delay = sleep.call_args.args[0]
        self.assertGreaterEqual(delay, 1.0)
        self.assertLessEqual(delay, 5.0)

    def test_captured_key_is_not_cached_after_account_changes(self):
        @contextmanager
        def acquired():
            yield None

        with (
            mock.patch.object(sync, "_exclusive_sync_lock", acquired),
            mock.patch.object(sync, "_database_snapshot_scope", acquired),
            mock.patch.object(
                sync,
                "discover_unique_account",
                return_value=("second-account", Path("unused")),
            ),
            mock.patch.object(sync, "_load_nt_reader") as load_reader,
        ):
            with self.assertRaises(sync.SyncFailure) as raised:
                sync.cache_captured_passphrase(
                    "a" * 64,
                    expected_account_id="first-account",
                )
        self.assertEqual(raised.exception.code, "needs_account_selection")
        load_reader.assert_not_called()

    def test_watcher_never_closes_or_restarts_wechat(self):
        watcher = (REPO_ROOT / "scripts" / "watch_wechat_key.py").read_text(
            encoding="utf-8"
        ).lower()
        self.assertNotIn("stop-process", watcher)
        self.assertNotIn("terminateprocess", watcher)
        self.assertNotIn("taskkill", watcher)
        self.assertNotIn("sendmessage", watcher)
        launcher = (REPO_ROOT / "start_wechat_watcher.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("-WindowStyle Hidden", launcher)

    def test_scheduled_missing_key_reports_safe_wait_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            status_path = Path(temporary) / "status.json"
            with (
                mock.patch.object(sync, "STATUS_PATH", status_path),
                mock.patch.object(sync, "PRIVATE_ROOT", Path(temporary)),
                mock.patch.object(sync, "LOCK_PATH", Path(temporary) / "sync.lock"),
                mock.patch.object(sync, "inspect_runtime", return_value={
                    "ready": True,
                    "version": "1.5.0",
                }),
                mock.patch.object(
                    sync,
                    "discover_unique_account",
                    return_value=("wxid_only", Path(temporary)),
                ),
                mock.patch.object(
                    sync,
                    "_database_records",
                    side_effect=sync.SyncFailure("database_key_unavailable"),
                ),
            ):
                status = sync.run_sync(scheduled=True)
        self.assertEqual(status["state"], "awaiting_wechat_restart")
        self.assertIsNone(status["error_code"])

    def test_safe_wait_status_is_not_a_failed_automation_run(self):
        with (
            mock.patch.object(sync, "run_sync", return_value={
                **sync._base_status("scheduled"),
                "state": "awaiting_wechat_restart",
            }),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(sync.main(["--sync", "--scheduled"]), 0)

    def test_partial_import_is_an_error_and_not_a_completed_automation_run(self):
        def incomplete_sync(_reader, _records, _account, _run_root, status):
            status.update({
                "scanned_conversations": 2,
                "imported_conversations": 1,
                "failed_conversations": 1,
                "imported_messages": 7,
            })
            return {"sessions": []}

        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary) / "private"
            with (
                mock.patch.object(sync, "STATUS_PATH", private_root / "status.json"),
                mock.patch.object(sync, "PRIVATE_ROOT", private_root),
                mock.patch.object(sync, "LOCK_PATH", private_root / "sync.lock"),
                mock.patch.object(sync, "RAW_ROOT", Path(temporary) / "raw"),
                mock.patch.object(sync, "inspect_runtime", return_value={
                    "ready": True, "version": "1.5.0",
                }),
                mock.patch.object(sync, "discover_unique_account", return_value=(
                    "synthetic-account", Path(temporary),
                )),
                mock.patch.object(sync, "_database_records", return_value=(object(), [])),
                mock.patch.object(sync, "_sync_sessions", side_effect=incomplete_sync),
            ):
                status = sync.run_sync(scheduled=True)
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["error_code"], "sync_failed")
        self.assertEqual(status["failed_conversations"], 1)
        self.assertEqual(status["imported_conversations"], 1)
        self.assertEqual(status["imported_messages"], 7)
        self.assertIn("部分会话未完整导入", status["attention"])
        self.assertIn("原始微信记录未受影响", status["attention"])
        with (
            mock.patch.object(sync, "run_sync", return_value=status),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(sync.main(["--sync", "--scheduled"]), 1)


if __name__ == "__main__":
    unittest.main()
