"""Synthetic import identity and committed-material regressions."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dashboard import app


class WebImportIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.contacts = self.root / "contacts"
        self.raw = self.root / "raw"
        for name, value in (("CONTACTS_DIR", self.contacts), ("RAW_IMPORT_DIR", self.raw)):
            patch = mock.patch.object(app, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.request = {
            "kind": "markdown", "contact": "合成对象", "contact_id": "synthetic-peer-a",
            "my_name": "我", "filename": "synthetic.md",
            "content": "[2026-08-10 20:10] 我: 合成材料\n[2026-08-10 20:11] 合成对象: 收到\n",
        }

    def bundle(self, detail):
        return self.contacts / detail["id"]

    def snapshot(self, path):
        return {str(item.relative_to(path)): (item.read_bytes(), item.stat().st_mtime_ns)
                for item in path.rglob("*") if item.is_file()}

    def test_explicit_contact_identity_separates_same_display_and_content(self):
        first = app.import_payload(self.request)
        second = app.import_payload({**self.request, "contact_id": "synthetic-peer-b"})
        self.assertNotEqual(first["id"], second["id"])

    def test_payload_owner_source_and_original_contact_are_part_of_identity(self):
        base = {"source": "synthetic-source-a", "contact_display": "载荷对象",
                "contact_username": "synthetic-source-peer-a", "own_wxid": "synthetic-owner-a",
                "messages": [{"local_id": 1, "timestamp": 1786363800,
                              "sender": "me", "type": "text", "content": "合成材料"}]}
        request = {**self.request, "kind": "normalized-json", "content": json.dumps(base)}
        first = app.import_payload(request)
        for key, value in (("own_wxid", "synthetic-owner-b"),
                           ("source", "synthetic-source-b"),
                           ("contact_username", "synthetic-source-peer-b"),
                           ("contact_display", "另一载荷对象")):
            with self.subTest(field=key):
                changed = {**base, key: value}
                other = app.import_payload({**request, "content": json.dumps(changed)})
                self.assertNotEqual(first["id"], other["id"])

    def test_ciphertalk_raw_owner_is_retained_before_conversion(self):
        data = {"meta": {"ownerId": "synthetic-owner-a"}, "messages": [
            {"localId": 1, "direction": "out", "timestamp": 1786363800,
             "type": "text", "content": "合成材料"}]}
        request = {**self.request, "kind": "ciphertalk", "content": json.dumps(data)}
        first = app.import_payload(request)
        data["meta"]["ownerId"] = "synthetic-owner-b"
        other = app.import_payload({**request, "content": json.dumps(data)})
        self.assertNotEqual(first["id"], other["id"])

    def test_weflow_cli_outgoing_source_identity_survives_conversion(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                messages = [{"localId": 1, "isSend": 1, "senderUsername": "synthetic-owner-a",
                             "createTime": 1786363800, "localType": 1, "content": "合成材料"}]
                data = {"messages": messages} if wrapped else messages
                request = {**self.request, "kind": "weflow-cli", "content": json.dumps(data)}
                first = app.import_payload(request)
                messages[0]["senderUsername"] = "synthetic-owner-b"
                second = app.import_payload({**request, "content": json.dumps(data)})
                self.assertNotEqual(first["id"], second["id"])

    def test_reuse_does_not_mutate_artifacts_or_expose_identity(self):
        request_before = copy.deepcopy(self.request)
        first = app.import_payload(self.request)
        before = self.snapshot(self.root)
        second = app.import_payload({**self.request, "filename": "renamed.md"})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(before, self.snapshot(self.root))
        self.assertEqual(request_before, self.request)
        for value in ("synthetic-peer-a", "import_identity", "messages_sha256", "合成材料"):
            self.assertNotIn(value, json.dumps(second, ensure_ascii=False))

    def test_mutated_messages_are_preserved_and_not_reused(self):
        first = app.import_payload(self.request)
        messages = self.bundle(first) / "messages.json"
        payload = json.loads(messages.read_text(encoding="utf-8"))
        payload["messages"][0]["content"] = "changed synthetic material"
        messages.write_text(json.dumps(payload), encoding="utf-8")
        before = self.snapshot(self.bundle(first))
        second = app.import_payload(self.request)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(before, self.snapshot(self.bundle(first)))

    def test_forged_digest_does_not_replace_canonical_identity_check(self):
        first = app.import_payload(self.request)
        bundle = self.bundle(first)
        messages = bundle / "messages.json"
        data = json.loads(messages.read_text(encoding="utf-8"))
        data["messages"][0]["content"] = "changed synthetic material"
        messages.write_text(json.dumps(data), encoding="utf-8")
        manifest_path = bundle / "dashboard_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["messages_sha256"] = hashlib.sha256(messages.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        second = app.import_payload(self.request)
        self.assertNotEqual(first["id"], second["id"])

    def test_legacy_manifest_is_not_upgraded_or_reused(self):
        first = app.import_payload(self.request)
        path = self.bundle(first) / "dashboard_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for key in ("import_fingerprint_version", "import_identity", "messages_sha256"):
            manifest.pop(key, None)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        before = self.snapshot(self.bundle(first))
        second = app.import_payload(self.request)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(before, self.snapshot(self.bundle(first)))

    def test_excessively_nested_damaged_manifest_is_not_reused(self):
        first = app.import_payload(self.request)
        path = self.bundle(first) / "dashboard_manifest.json"
        path.write_text("[" * 10000 + "0" + "]" * 10000, encoding="utf-8")
        before = path.read_bytes()
        second = app.import_payload(self.request)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(before, path.read_bytes())

    def test_old_hidden_staging_is_never_a_reuse_candidate(self):
        first = app.import_payload(self.request)
        hidden = self.contacts / ".dashboard-import-interrupted"
        self.bundle(first).rename(hidden)
        before = self.snapshot(hidden)
        second = app.import_payload(self.request)
        self.assertFalse(second["id"].startswith("."))
        self.assertEqual(before, self.snapshot(hidden))

    @unittest.skipUnless(os.name == "nt", "Windows hidden attribute")
    def test_windows_hidden_bundle_is_preserved_not_reused(self):
        import ctypes
        first = app.import_payload(self.request)
        bundle = self.bundle(first)
        original_attributes = bundle.stat().st_file_attributes
        set_attributes = ctypes.windll.kernel32.SetFileAttributesW
        set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        self.assertTrue(set_attributes(str(bundle), original_attributes | 2))
        try:
            before = self.snapshot(bundle)
            second = app.import_payload(self.request)
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(before, self.snapshot(bundle))
        finally:
            set_attributes(str(bundle), original_attributes)

    @unittest.skipUnless(os.name == "nt", "Windows configured data root junction")
    def test_configured_root_junction_is_rejected_before_writing_target(self):
        target = self.root / "synthetic-external-data"
        target.mkdir()
        junction = self.root / "configured-data"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                                capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode)
        code = (
            "import json, sys\n"
            "from dashboard import app\n"
            "from import_store import ImportStoreError\n"
            "try:\n"
            "    app.import_payload(json.loads(sys.stdin.read()))\n"
            "except ImportStoreError:\n"
            "    print('REJECTED')\n"
            "else:\n"
            "    print('ACCEPTED')\n"
        )
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                                cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, "SHE_LOVE_ME_DATA_DIR": str(junction)},
                                input=json.dumps(self.request), text=True, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, "synthetic configuration check failed")
        self.assertEqual("REJECTED", result.stdout.strip())
        self.assertEqual([], list(target.iterdir()))

    def test_current_staging_is_outside_contact_discovery(self):
        run = app.run_script

        def observed_run(name, arguments, **kwargs):
            if name == "stats_analyzer.py":
                staged = Path(arguments[arguments.index("--input") + 1]).parent
                self.assertFalse(staged.is_relative_to(self.contacts))
                self.assertTrue(staged.is_relative_to(self.root / "private"))
            return run(name, arguments, **kwargs)

        with mock.patch.object(app, "run_script", side_effect=observed_run):
            app.import_payload(self.request)

    def test_return_failure_keeps_committed_bundle_and_raw_for_retry(self):
        with mock.patch.object(app, "bundle_detail", side_effect=RuntimeError("synthetic response failure")):
            with self.assertRaises(RuntimeError):
                app.import_payload(self.request)
        bundles = list(self.contacts.iterdir())
        self.assertEqual(1, len(bundles))
        self.assertEqual(1, len(list(self.raw.iterdir())))
        before = self.snapshot(self.root)
        result = app.import_payload(self.request)
        self.assertEqual(bundles[0].name, result["id"])
        self.assertEqual(before, self.snapshot(self.root))

    def test_linked_material_is_not_reused(self):
        first = app.import_payload(self.request)
        messages = self.bundle(first) / "messages.json"
        outside = self.root / "external-synthetic.json"
        shutil.copyfile(messages, outside)
        messages.unlink()
        try:
            messages.symlink_to(outside)
        except OSError:
            self.skipTest("file symlink creation is unavailable")
        original = outside.read_bytes()
        second = app.import_payload(self.request)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(messages.is_symlink())
        self.assertEqual(original, outside.read_bytes())

    def test_weflow_dropped_count_survives_without_second_normalization(self):
        data = {"session": {"wxid": "synthetic-peer-a"}, "messages": [
            {"localId": 1, "createTime": 1786363800, "isSend": 1,
             "type": "文本消息", "content": "合成材料"},
            {"localId": 2, "createTime": "invalid", "isSend": 0,
             "type": "文本消息", "content": "合成无效材料"}]}
        result = app.import_payload({**self.request, "kind": "weflow-json", "content": json.dumps(data)})
        payload = json.loads((self.bundle(result) / "messages.json").read_text(encoding="utf-8"))
        self.assertEqual(1, payload["normalization"]["dropped_messages"])
        self.assertEqual(1, len(payload["messages"]))

    def test_write_and_publish_failures_do_not_leave_half_bundles(self):
        write = app._write_web_import_file
        rename = Path.rename
        for stage in ("messages.json", "dashboard_manifest.json", "publish"):
            with self.subTest(stage=stage):
                def failing_write(path, content):
                    if path.name == stage:
                        raise OSError("synthetic write failure")
                    return write(path, content)

                def failing_rename(path, target):
                    if stage == "publish" and path.name.startswith(".dashboard-import-"):
                        raise OSError("synthetic publication failure")
                    return rename(path, target)

                with mock.patch.object(app, "_write_web_import_file", side_effect=failing_write), \
                        mock.patch.object(Path, "rename", new=failing_rename):
                    with self.assertRaises(OSError):
                        app.import_payload(self.request)
                self.assertEqual([], list(self.contacts.iterdir()))
                self.assertEqual([], list(self.raw.iterdir()))
                self.assertEqual([], list((self.root / "private").glob(".dashboard-import-*")))


if __name__ == "__main__":
    unittest.main()
