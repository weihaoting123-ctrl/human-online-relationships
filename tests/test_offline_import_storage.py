"""Synthetic subprocess regressions for the four offline import entrypoints."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TEMP = SCRIPTS / "tmp"


class OfflineImportStorageTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP, prefix="offline-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, kind, content="synthetic alpha", *, include_emoji=False):
        if kind == "markdown":
            return f"[2026-08-12 20:10] Sample Self: {content}\n" * 2
        message = {
            "localId": 1, "createTime": 1692946166, "isSend": 1,
            "senderUsername": "synthetic_owner", "direction": "out",
            "type": "文本消息", "localType": 1, "content": content,
        }
        if kind == "weflow":
            messages = [message, message]
            if include_emoji:
                messages.append({
                    "localId": 2, "createTime": 1692946167, "isSend": 0,
                    "senderUsername": "synthetic_contact", "type": "动画表情",
                    "localType": 47, "content": "[表情]", "emojiMd5": "synthetic-emoji-alpha",
                    "emojiCdnUrl": "https://assets.example.invalid/synthetic-emoji.gif", "emojiLen": 128,
                })
            return json.dumps({"session": {"wxid": "synthetic_contact", "nickname": "Sample Contact"}, "messages": messages})
        return json.dumps({"meta": {"ownerId": "synthetic_owner"}, "messages": [message, message]})

    def run_import(self, kind, source, extra=(), check=True):
        args = [sys.executable, "-X", "utf8", str(SCRIPTS / f"convert_{kind}.py"),
                "--input", str(source), "--output-dir", str(self.root / kind / "contacts")]
        if kind != "weflow":
            args += ["--contact", "Sample Contact", "--contact-id", "synthetic_contact"]
        if kind == "markdown":
            args += ["--my-name", "Sample Self"]
        result = subprocess.run(args + list(extra), capture_output=True, text=True,
                                encoding="utf-8", timeout=20,
                                env={**os.environ, "TEMP": str(TEMP), "TMP": str(TEMP)})
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        return result

    def test_four_clis_preserve_changed_content_and_reuse_exact_relocations(self):
        for kind in ("markdown", "weflow", "weflow_cli", "ciphertalk"):
            with self.subTest(kind=kind):
                first_source = self.root / f"{kind}-first.txt"
                second_source = self.root / f"{kind}-second.txt"
                first_source.write_text(self.source(kind, include_emoji=True), encoding="utf-8")
                first = self.run_import(kind, first_source)
                bundle = Path(first["bundle_dir"])
                original = Path(first["messages_path"]).read_bytes()
                original_mtime = Path(first["messages_path"]).stat().st_mtime_ns
                (bundle / "analysis.json").write_text('{"synthetic":true}', encoding="utf-8")
                (bundle / "reports").mkdir()
                report = bundle / "reports" / "synthetic.html"
                report.write_text("<p>Synthetic report</p>", encoding="utf-8")
                preserved = [Path(first["messages_path"]), bundle / "analysis.json", report]
                if kind == "weflow":
                    emojis = Path(first["emojis_path"])
                    catalog = json.loads(emojis.read_text(encoding="utf-8"))
                    self.assertEqual(catalog["unique_emojis"], 1)
                    self.assertEqual(catalog["total_messages"], 1)
                    self.assertEqual(catalog["emoji_records"][0]["md5"], "synthetic-emoji-alpha")
                    self.assertEqual(catalog["emoji_records"][0]["cdnurl"],
                                     "https://assets.example.invalid/synthetic-emoji.gif")
                    preserved.append(emojis)
                snapshots = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in preserved}

                def assert_preserved():
                    for path, expected in snapshots.items():
                        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected, path.name)

                second_source.write_text(self.source(kind, "synthetic beta", include_emoji=True), encoding="utf-8")
                changed = self.run_import(kind, second_source)
                # This first assertion demonstrates the old destructive CLI behavior.
                self.assertNotEqual(first["bundle_dir"], changed["bundle_dir"])
                self.assertEqual(Path(first["messages_path"]).read_bytes(), original)
                self.assertEqual((bundle / "analysis.json").read_text(), '{"synthetic":true}')
                self.assertEqual(report.read_text(), "<p>Synthetic report</p>")
                assert_preserved()

                repeat = self.run_import(kind, first_source)
                assert_preserved()
                second_source.write_text(self.source(kind, include_emoji=True), encoding="utf-8")
                relocated = self.run_import(kind, second_source)
                assert_preserved()
                self.assertEqual(first["bundle_dir"], repeat["bundle_dir"])
                self.assertEqual(first["messages_path"], relocated["messages_path"])
                self.assertEqual(Path(first["messages_path"]).read_bytes(), original)
                self.assertEqual(Path(first["messages_path"]).stat().st_mtime_ns, original_mtime)
                self.assertTrue(first["created"])
                self.assertFalse(first["reused"])
                self.assertTrue(repeat["reused"])
                self.assertFalse(repeat["created"])
                payload = json.loads(original)
                self.assertEqual(len(payload["messages"]), 3 if kind == "weflow" else 2)

    def test_raw_owner_and_session_identity_survive_converter_projection(self):
        for kind in ("ciphertalk", "weflow"):
            with self.subTest(kind=kind):
                source = self.root / f"identity-{kind}.json"
                data = json.loads(self.source(kind))
                source.write_text(json.dumps(data), encoding="utf-8")
                extra = ("--wxid", "fixed-contact", "--display-name", "Fixed Display") if kind == "weflow" else ()
                first = self.run_import(kind, source, extra)
                if kind == "ciphertalk":
                    data["meta"]["ownerId"] = "another-synthetic-owner"
                else:
                    data["session"]["wxid"] = "another-synthetic-contact"
                source.write_text(json.dumps(data), encoding="utf-8")
                second = self.run_import(kind, source, extra)
                self.assertNotEqual(first["bundle_dir"], second["bundle_dir"])

    def test_explicit_options_are_distinct_even_when_converted_messages_match(self):
        for kind in ("markdown", "weflow_cli", "weflow"):
            with self.subTest(kind=kind):
                source = self.root / f"options-{kind}.txt"
                source.write_text(self.source(kind), encoding="utf-8")
                if kind == "markdown":
                    source.write_text("[2026-08-12 20:10] Sample Contact: Synthetic greeting\n", encoding="utf-8")
                    option_a, option_b = ("--my-name", "Sample Self"), ("--my-name", "Another Self")
                else:
                    option_a, option_b = (), ("--own-wxid", "synthetic_owner")
                first = self.run_import(kind, source, option_a)
                second = self.run_import(kind, source, option_b)
                self.assertNotEqual(first["bundle_dir"], second["bundle_dir"])

    def test_weflow_cli_keeps_available_raw_owner_when_option_is_omitted(self):
        source = self.root / "observed-owner.json"
        data = json.loads(self.source("weflow_cli"))["messages"]
        source.write_text(json.dumps(data), encoding="utf-8")
        first = self.run_import("weflow_cli", source)
        for message in data:
            message["senderUsername"] = "another-synthetic-owner"
        source.write_text(json.dumps(data), encoding="utf-8")
        second = self.run_import("weflow_cli", source)
        self.assertNotEqual(first["bundle_dir"], second["bundle_dir"])

    def test_weflow_identity_does_not_stop_at_missing_first_sender(self):
        source = self.root / "later-observed-owner.json"
        data = json.loads(self.source("weflow"))
        data["messages"][0].pop("senderUsername")
        source.write_text(json.dumps(data), encoding="utf-8")
        first = self.run_import("weflow", source)
        data["messages"][1]["senderUsername"] = "another-synthetic-owner"
        source.write_text(json.dumps(data), encoding="utf-8")
        second = self.run_import("weflow", source)
        self.assertNotEqual(first["bundle_dir"], second["bundle_dir"])

    def test_cli_errors_are_fixed_and_never_echo_source_or_tracebacks(self):
        for kind in ("markdown", "weflow", "weflow_cli", "ciphertalk"):
            with self.subTest(kind=kind):
                missing = self.root / "synthetic-confidential-missing.txt"
                result = self.run_import(kind, missing, check=False)
                self.assertNotEqual(result.returncode, 0)
                output = json.loads(result.stderr)
                self.assertEqual(output.get("code"), "IMPORT_INPUT_MISSING")
                self.assertNotIn("synthetic-confidential", result.stderr)
                source = self.root / f"invalid-{kind}.txt"
                source.write_text("synthetic-confidential-invalid-input", encoding="utf-8")
                result = self.run_import(kind, source, check=False)
                output = json.loads(result.stderr)
                self.assertEqual(output.get("code"), "IMPORT_INVALID_INPUT")
                self.assertNotIn("synthetic-confidential", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_malformed_nested_schemas_have_safe_error_outlets(self):
        for kind in ("weflow", "weflow_cli", "ciphertalk"):
            with self.subTest(kind=kind):
                source = self.root / f"malformed-{kind}.json"
                if kind == "ciphertalk":
                    data = {"meta": "synthetic-confidential", "messages": [{"timestamp": 1692946166, "sender": "sample"}]}
                else:
                    data = {"messages": ["synthetic-confidential"]}
                source.write_text(json.dumps(data), encoding="utf-8")
                result = self.run_import(kind, source, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr).get("code"), "IMPORT_INVALID_INPUT")
                self.assertNotIn("synthetic-confidential", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_all_cli_processes_fail_busy_without_waiting_or_partial_output(self):
        sys.path.insert(0, str(SCRIPTS))
        from import_store import exclusive_import_lock
        for kind in ("markdown", "weflow", "weflow_cli", "ciphertalk"):
            with self.subTest(kind=kind):
                source = self.root / f"busy-{kind}.txt"
                source.write_text(self.source(kind), encoding="utf-8")
                contacts = self.root / kind / "contacts"
                with exclusive_import_lock(contacts):
                    result = self.run_import(kind, source, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(json.loads(result.stderr).get("code"), "IMPORT_BUSY")
                    self.assertEqual(list(contacts.iterdir()), [])

    def test_weflow_extreme_numeric_input_has_fixed_error_without_traceback(self):
        source = self.root / "extreme-numeric.json"
        data = json.loads(self.source("weflow"))
        data["messages"][0]["createTime"] = 10 ** 400
        source.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_import("weflow", source, check=False)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(json.loads(result.stderr).get("code"), "IMPORT_INVALID_INPUT")

    def test_dropped_message_statistics_are_not_reset_by_storage(self):
        for kind in ("weflow", "weflow_cli", "ciphertalk"):
            with self.subTest(kind=kind):
                data = json.loads(self.source(kind))
                invalid = dict(data["messages"][0])
                invalid["createTime"] = 0
                data["messages"].append(invalid)
                source = self.root / f"invalid-row-{kind}.json"
                source.write_text(json.dumps(data), encoding="utf-8")
                output = self.run_import(kind, source)
                payload = json.loads(Path(output["messages_path"]).read_text(encoding="utf-8"))
                self.assertEqual(output["dropped"], 1)
                self.assertEqual(payload["normalization"]["dropped_messages"], 1)
                self.assertEqual(len(payload["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
