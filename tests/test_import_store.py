"""Immutable import storage tests. Every record here is synthetic."""

from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
TEMP = SCRIPTS / "tmp"


class ImportStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("import_store"), "Shared immutable import store is missing")
        self.store = importlib.import_module("import_store")
        TEMP.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP, prefix="import-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.contacts = self.root / "contacts"
        self.payload = {"source": "synthetic", "contact_username": "sample-contact",
                        "normalization": {"dropped_messages": 3}, "messages": [
            {"local_id": 1, "sender": "me", "timestamp": 1692946166, "content": "Synthetic message"},
            {"local_id": 1, "sender": "me", "timestamp": 1692946166, "content": "Synthetic message"},
        ]}
        self.identity = {"source": "synthetic", "request": {"contact_id": "sample-contact"},
                         "source_identity": {"owner_id": "sample-owner"}}

    def save(self, payload=None, identity=None, sidecars=None):
        return self.store.save_import_bundle(
            self.payload if payload is None else payload, contact="Sample Contact",
            contact_id="sample-contact", output_dir=self.contacts,
            identity_context=self.identity if identity is None else identity, sidecars=sidecars)

    def test_repeat_keeps_bytes_mtime_duplicates_and_dropped_stats(self):
        original = deepcopy(self.payload)
        first = self.save()
        path = Path(first["bundle"]["messages_path"])
        before = path.read_bytes(), path.stat().st_mtime_ns
        second = self.save()
        self.assertEqual(first["bundle"], second["bundle"])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(self.payload, original)
        self.assertEqual(first["payload"]["messages"], original["messages"])
        self.assertEqual(first["payload"]["normalization"], original["normalization"])
        self.assertTrue(first["created"])
        self.assertTrue(second["reused"])

    def test_changed_content_and_identity_allocate_new_bundles(self):
        first = self.save()
        for identity in ({**self.identity, "source": "another-source"},
                         {**self.identity, "request": {"contact_id": "other-contact"}},
                         {**self.identity, "source_identity": {"owner_id": "other-owner"}}):
            with self.subTest(identity=identity):
                self.assertNotEqual(first["bundle"]["bundle_dir"], self.save(identity=identity)["bundle"]["bundle_dir"])
        changed = deepcopy(self.payload)
        changed["messages"][1]["local_id"] = 2
        last = self.save(changed)
        self.assertNotEqual(first["bundle"]["bundle_dir"], last["bundle"]["bundle_dir"])
        for field, value in last["bundle"].items():
            self.assertTrue(Path(value) == Path(last["bundle"]["bundle_dir"]) or Path(last["bundle"]["bundle_dir"]) in Path(value).parents, field)

    def test_fingerprint_omits_only_material_top_level_bundle_dir(self):
        sidecars = {"emojis.json": {"emoji_records": [], "bundle_dir": "old-place"}}
        before = deepcopy(sidecars)
        fp = self.store.fingerprint_import(self.payload, identity_context=self.identity, sidecars=sidecars)
        moved = {**self.payload, "bundle_dir": "new-place"}
        sidecars["emojis.json"]["bundle_dir"] = "new-place"
        self.assertEqual(fp, self.store.fingerprint_import(moved, identity_context=self.identity, sidecars=sidecars))
        nested = {**self.payload, "nested": {"bundle_dir": "identity-relevant"}}
        self.assertNotEqual(fp, self.store.fingerprint_import(nested, identity_context=self.identity, sidecars=sidecars))
        sidecars = before
        before = deepcopy(sidecars)
        first = self.save(sidecars=sidecars)
        self.assertEqual(sidecars, before)
        self.assertEqual(json.loads(Path(first["bundle"]["emojis_path"]).read_text())["bundle_dir"], first["bundle"]["bundle_dir"])

    def test_sidecar_omission_and_changes_cannot_reuse(self):
        first = self.save(sidecars={"emojis.json": {"emoji_records": []}})
        no_sidecar = self.save()
        different = self.save(sidecars={"emojis.json": {"emoji_records": [{"emoji_id": "synthetic"}]}})
        self.assertEqual(len({result["bundle"]["bundle_dir"] for result in (first, no_sidecar, different)}), 3)

    def test_legacy_or_occupied_targets_are_preserved(self):
        from contact_bundle import resolve_bundle_paths
        base = Path(resolve_bundle_paths("Sample Contact", "sample-contact", output_dir=self.contacts)["bundle_dir"])
        self.contacts.mkdir()
        base.write_bytes(b"synthetic occupied file")
        created = self.save()
        self.assertEqual(base.read_bytes(), b"synthetic occupied file")
        self.assertNotEqual(str(base), created["bundle"]["bundle_dir"])
        manifest = Path(created["bundle"]["bundle_dir"]) / "import_manifest.json"
        manifest.unlink()
        legacy_bytes = Path(created["bundle"]["messages_path"]).read_bytes()
        newer = self.save()
        self.assertNotEqual(newer["bundle"]["bundle_dir"], created["bundle"]["bundle_dir"])
        self.assertEqual(Path(created["bundle"]["messages_path"]).read_bytes(), legacy_bytes)

    def test_tampered_material_digest_and_forged_fingerprint_do_not_reuse(self):
        first = self.save()
        directory = Path(first["bundle"]["bundle_dir"])
        messages = directory / "messages.json"
        tampered = json.loads(messages.read_text())
        tampered["messages"][0]["content"] = "Synthetic tamper"
        messages.write_text(json.dumps(tampered), encoding="utf-8")
        manifest_path = directory / "import_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["messages.json"] = hashlib.sha256(messages.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        second = self.save()
        self.assertNotEqual(first["bundle"]["bundle_dir"], second["bundle"]["bundle_dir"])
        self.assertEqual(json.loads(messages.read_text())["messages"][0]["content"], "Synthetic tamper")

    def test_missing_or_tampered_sidecar_cannot_reuse(self):
        sidecars = {"emojis.json": {"emoji_records": []}}
        first = self.save(sidecars=sidecars)
        Path(first["bundle"]["emojis_path"]).unlink()
        second = self.save(sidecars=sidecars)
        self.assertNotEqual(first["bundle"]["bundle_dir"], second["bundle"]["bundle_dir"])
        Path(second["bundle"]["emojis_path"]).write_text('{"emoji_records":["tampered"]}', encoding="utf-8")
        third = self.save(sidecars=sidecars)
        self.assertNotEqual(second["bundle"]["bundle_dir"], third["bundle"]["bundle_dir"])

    def test_invalid_json_and_empty_input_are_safe_errors_without_commit(self):
        cyclic = {}
        cyclic["cycle"] = cyclic
        for extra in (float("nan"), float("inf"), object(), {1: "not-json-key"}, (1, 2), cyclic):
            with self.subTest(kind=type(extra).__name__):
                with self.assertRaises(self.store.ImportStoreError) as caught:
                    self.save({**self.payload, "extra": extra})
                self.assertEqual(str(caught.exception), "IMPORT_INVALID_INPUT")
        with self.assertRaises(self.store.ImportStoreError):
            self.save({"messages": []})
        with self.assertRaises(self.store.ImportStoreError):
            self.save(sidecars={"../outside.json": {}})
        self.assertFalse(self.contacts.exists())

    def test_injected_write_failures_leave_no_discoverable_partial_bundle(self):
        real_write = self.store._write_json
        for filename in ("messages.json", "emojis.json", "import_manifest.json"):
            with self.subTest(filename=filename):
                def fail_selected(path, data):
                    if Path(path).name == filename:
                        raise OSError("synthetic private failure details")
                    return real_write(path, data)
                with patch.object(self.store, "_write_json", side_effect=fail_selected):
                    with self.assertRaises(self.store.ImportStoreError) as caught:
                        self.save(sidecars={"emojis.json": {"emoji_records": []}})
                self.assertEqual(str(caught.exception), "IMPORT_IO_ERROR")
                self.assertEqual(list(self.contacts.iterdir()), [])
                self.assertFalse(list((self.root / "private").glob(".import-*")))

    def test_rename_failure_is_clean_and_postcommit_failure_is_reusable(self):
        with patch.object(self.store.os, "rename", side_effect=OSError("synthetic rename error")):
            with self.assertRaises(self.store.ImportStoreError):
                self.save()
        self.assertEqual(list(self.contacts.iterdir()), [])
        real_sync = self.store._fsync_directory
        def fail_postcommit(path):
            if Path(path) == self.contacts:
                raise OSError("synthetic postcommit failure")
            return real_sync(path)
        with patch.object(self.store, "_fsync_directory", side_effect=fail_postcommit):
            with self.assertRaises(self.store.ImportStoreError):
                self.save()
        self.assertEqual(len(list(self.contacts.iterdir())), 1)
        self.assertTrue(self.save()["reused"])

    def test_lock_contention_and_process_exit_release(self):
        self.store.validated_private_root(self.contacts)
        code = "\n".join([
            "import sys", "sys.path.insert(0, sys.argv[1])",
            "from import_store import exclusive_import_lock",
            "with exclusive_import_lock(sys.argv[2]):",
            "    print('LOCKED', flush=True)", "    sys.stdin.read()",
        ])
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(SCRIPTS), str(self.contacts)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        ready = threading.Event()
        ready_output = []

        def read_ready():
            try:
                ready_output.append(process.stdout.readline().strip())
            except (OSError, ValueError):
                pass
            finally:
                ready.set()

        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(timeout=10), "Synthetic lock holder did not become ready within 10 seconds")
            self.assertEqual(ready_output, ["LOCKED"])
            with self.assertRaises(self.store.ImportBusyError):
                self.save()
            self.assertEqual(list(self.contacts.iterdir()), [])
            process.kill()
            process.wait(timeout=10)
            self.assertTrue(self.save()["created"])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(timeout=10)
            self.assertFalse(reader.is_alive(), "Synthetic lock readiness reader did not exit")
            process.communicate(timeout=10)

    def test_lock_preserves_exceptions_from_caller_body(self):
        with self.assertRaises(OSError):
            with self.store.exclusive_import_lock(self.contacts):
                raise OSError("Synthetic caller failure")
        with self.store.exclusive_import_lock(self.contacts):
            pass

    def make_link(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable on this platform: {exc.winerror if os.name == 'nt' else exc.errno}")

    def test_root_ancestor_private_and_lock_links_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        for name in ("contacts", "ancestor", "private", "lock"):
            with self.subTest(name=name):
                case = self.root / name
                case.mkdir(exist_ok=True)
                output = case / "contacts"
                if name == "contacts":
                    self.make_link(output, outside, True)
                elif name == "ancestor":
                    self.make_link(case / "linked", outside, True)
                    output = case / "linked" / "contacts"
                elif name == "private":
                    self.make_link(case / "private", outside, True)
                else:
                    (case / "private").mkdir()
                    self.make_link(case / "private" / "dashboard-import.lock", outside / "missing")
                with self.assertRaises(self.store.ImportStoreError):
                    self.store.validated_private_root(output) if name != "lock" else self.store.save_import_bundle(
                        self.payload, contact="Sample Contact", contact_id="sample-contact",
                        output_dir=output, identity_context=self.identity)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction/reparse test")
    def test_windows_junction_roots_and_candidates_are_not_followed(self):
        outside = self.root / "synthetic-target"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("Synthetic untouched material", encoding="utf-8")

        def junction(link, target):
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, "Synthetic test junction could not be created")

        for name in ("root", "ancestor", "private"):
            with self.subTest(name=name):
                case = self.root / name
                case.mkdir()
                if name == "private":
                    junction(case / "private", outside)
                    output = case / "contacts"
                else:
                    junction(case / "linked", outside)
                    output = case / "linked" if name == "root" else case / "linked" / "contacts"
                with self.assertRaises(self.store.ImportStoreError):
                    self.store.validated_private_root(output)
        first = self.save()
        first_dir = Path(first["bundle"]["bundle_dir"])
        moved = self.root / "synthetic-moved-bundle"
        first_dir.rename(moved)
        junction(first_dir, moved)
        second = self.save()
        self.assertNotEqual(str(first_dir), second["bundle"]["bundle_dir"])
        self.assertTrue(first_dir.is_junction())
        self.assertEqual(sentinel.read_text(), "Synthetic untouched material")
        self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_hidden_candidate_is_preserved_and_not_reused(self):
        first = self.save()
        hidden = self.contacts / ".hidden-import"
        Path(first["bundle"]["bundle_dir"]).rename(hidden)
        second = self.save()
        self.assertNotEqual(str(hidden), second["bundle"]["bundle_dir"])
        self.assertTrue((hidden / "messages.json").exists())

    def test_manifest_requires_exact_complete_material_file_set(self):
        sidecars = {"emojis.json": {"emoji_records": []}}
        first = self.save(sidecars=sidecars)
        first_dir = Path(first["bundle"]["bundle_dir"])
        manifest_path = first_dir / "import_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        del manifest["files"]["emojis.json"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        second = self.save(sidecars=sidecars)
        self.assertNotEqual(first["bundle"]["bundle_dir"], second["bundle"]["bundle_dir"])
        no_sidecar = self.save()
        Path(no_sidecar["bundle"]["emojis_path"]).write_text("{}", encoding="utf-8")
        next_no_sidecar = self.save()
        self.assertNotEqual(no_sidecar["bundle"]["bundle_dir"], next_no_sidecar["bundle"]["bundle_dir"])

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive file aliases")
    def test_windows_unmanifested_case_variant_sidecar_prevents_reuse(self):
        first = self.save()
        directory = Path(first["bundle"]["bundle_dir"])
        (directory / "EMOJIS.JSON").write_text("{}", encoding="utf-8")
        self.assertTrue(Path(first["bundle"]["emojis_path"]).exists())
        second = self.save()
        self.assertNotEqual(first["bundle"]["bundle_dir"], second["bundle"]["bundle_dir"])
        self.assertEqual((directory / "EMOJIS.JSON").read_text(), "{}")

    def test_staging_is_private_and_complete_before_rename(self):
        real_rename = self.store.os.rename
        observations = []
        def inspect_commit(source, target):
            source, target = Path(source), Path(target)
            observations.append(source)
            self.assertEqual(source.parent, self.root / "private")
            self.assertEqual(list(self.contacts.iterdir()), [])
            self.assertEqual({path.name for path in source.iterdir()},
                             {"messages.json", "emojis.json", "import_manifest.json"})
            self.assertFalse(target.exists())
            return real_rename(source, target)
        with patch.object(self.store.os, "rename", side_effect=inspect_commit):
            self.save(sidecars={"emojis.json": {"emoji_records": []}})
        self.assertEqual(len(observations), 1)

    def test_occupancy_recheck_preserves_newly_occupied_target(self):
        real_write = self.store._write_json
        occupied = []
        def occupy_after_manifest(path, data):
            result = real_write(path, data)
            if Path(path).name == "import_manifest.json":
                target = Path(json.loads((Path(path).parent / "messages.json").read_text())["bundle_dir"])
                target.mkdir()
                (target / "synthetic.txt").write_text("Synthetic concurrent occupant", encoding="utf-8")
                occupied.append(target)
            return result
        with patch.object(self.store, "_write_json", side_effect=occupy_after_manifest):
            with self.assertRaises(self.store.ImportStoreError):
                self.save()
        self.assertEqual(len(occupied), 1)
        self.assertEqual((occupied[0] / "synthetic.txt").read_text(), "Synthetic concurrent occupant")
        self.assertFalse(list((self.root / "private").glob(".import-*")))

    def test_linked_or_hidden_candidate_materials_are_not_followed(self):
        first = self.save()
        directory = Path(first["bundle"]["bundle_dir"])
        messages = directory / "messages.json"
        outside = self.root / "synthetic-outside.json"
        outside.write_bytes(messages.read_bytes())
        messages.unlink()
        self.make_link(messages, outside)
        second = self.save()
        self.assertNotEqual(first["bundle"]["bundle_dir"], second["bundle"]["bundle_dir"])
        hidden = self.contacts / ".hidden-import"
        Path(second["bundle"]["bundle_dir"]).rename(hidden)
        third = self.save()
        self.assertNotEqual(str(hidden), third["bundle"]["bundle_dir"])


if __name__ == "__main__":
    unittest.main()
