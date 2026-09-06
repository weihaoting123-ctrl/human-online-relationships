"""Release guard checks with synthetic files, never the active chat project."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_release.py"
guard = None
if SCRIPT.is_file():
    spec = importlib.util.spec_from_file_location("public_release_guard", SCRIPT)
    guard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = guard
    spec.loader.exec_module(guard)


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(guard, "Public release guard is missing")
        self.temp = tempfile.TemporaryDirectory(prefix="release-guard-", dir=SCRIPT.parents[2])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative, value=b"ordinary source\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode() if isinstance(value, str) else value)
        return path

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def rules(self, findings):
        return {item["rule"] for item in findings}

    def test_clean_text_tree_passes_without_git(self):
        self.write("scripts/example.py", "print('public example')\n")
        self.write("README.md", "# Local application\n")
        self.assertEqual(guard.scan_tree(self.root), [])

    def test_default_requires_git_instead_of_silently_scanning_untracked_tree(self):
        self.write("README.md")
        self.assertIn("NOT_GIT_REPOSITORY", self.rules(guard.scan_git(self.root)))

    def test_empty_git_index_is_not_a_successful_release_check(self):
        self.git("init", "-q")
        self.assertEqual(guard.scan_git(self.root), [{"path": ".", "line": 0, "rule": "EMPTY_INDEX"}])

    def test_sensitive_directories_are_rejected_without_reading_inside(self):
        for relative in ("data/private/account.json", "backups/snapshot.json", "reports/output.html",
                         "vendor/source.py", ".runtime/state.json", ".venv/module.py",
                         "scripts/tmp/private.txt", "x/__pycache__/module.pyc"):
            self.write(relative)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Must not read blocked content")):
            findings = guard.scan_tree(self.root)
        self.assertEqual(len(findings), 8)
        self.assertEqual(self.rules(findings), {"SENSITIVE_DIRECTORY"})
        self.assertFalse(any("account" in item["path"] for item in findings))

    def test_databases_media_private_keys_and_runtime_names_are_rejected(self):
        names = ("archive.db", "archive.sqlite3", "archive.sqlite3-wal", "voice.wav", "voice.silk",
                 "photo.png", "photo.svg", "video.mp4", "private.pem", "key.pfx", "dump.zip",
                 ".env", "all_keys.json", "messages.json", "config.local.json")
        for name in names:
            self.write(name)
        findings = guard.scan_tree(self.root)
        self.assertEqual(len(findings), len(names))
        self.assertIn("PRIVATE_FILE_TYPE", self.rules(findings))
        self.assertIn("PRIVATE_FILE_NAME", self.rules(findings))

    def test_known_icon_is_only_allowed_by_exact_path_and_content_hash(self):
        value = b"<svg>synthetic reviewed icon</svg>"
        import hashlib
        with mock.patch.object(guard, "PUBLIC_ASSET_HASHES", {"dashboard/static/favicon.svg": hashlib.sha256(value).hexdigest()}):
            self.assertEqual(guard.scan_bytes("dashboard/static/favicon.svg", value), [])
            self.assertIn("PRIVATE_FILE_TYPE", self.rules(guard.scan_bytes("other.svg", value)))
            self.assertIn("PRIVATE_FILE_TYPE", self.rules(guard.scan_bytes("dashboard/static/favicon.svg", value + b"changed")))

    def test_api_tokens_private_key_blocks_and_credentials_are_detected(self):
        samples = [("API_TOKEN", "sk" + "-" + "A1b2" * 8),
                   ("API_TOKEN", "ghp" + "_" + "a" * 36),
                   ("PRIVATE_KEY", "-----BEGIN " + "RSA PRIVATE KEY-----"),
                   ("CREDENTIAL_LITERAL", 'api_key = "' + "not-a-real-secret-123" + '"'),
                   ("CREDENTIAL_LITERAL", 'password: "' + "private-value-123" + '"'),
                   ("CREDENTIAL_URL", "postgresql://user:" + "secret-value" + "@" + "db.invalid/app")]
        for rule, sample in samples:
            with self.subTest(rule=rule):
                findings = guard.scan_bytes("example.py", sample.encode())
                self.assertIn(rule, self.rules(findings))
                self.assertNotIn(sample, str(findings))

    def test_wechat_email_and_chinese_mobile_are_rejected_in_text_and_filenames(self):
        samples = [("WECHAT_ACCOUNT", "wx" + "id_" + "syntheticaccount123456"),
                   ("EMAIL_ADDRESS", "synthetic.person" + "@" + "example.invalid"),
                   ("CHINA_MOBILE", "13" + "000000001")]
        for rule, value in samples:
            with self.subTest(rule=rule):
                self.assertIn(rule, self.rules(guard.scan_bytes("example.py", value.encode())))
                findings = guard.scan_bytes(value + ".txt", b"public")
                self.assertIn("SENSITIVE_FILENAME", self.rules(findings))
                self.assertNotIn(value, str(findings))

    def test_mobile_rule_does_not_match_sha_fragments_or_longer_numbers(self):
        number = "13" + "000000001"
        for value in ("a" + number + "b", "9" + number + "9", "01" + number, number + "00"):
            self.assertNotIn("CHINA_MOBILE", self.rules(guard.scan_bytes("hashes.py", value.encode())))
        self.assertIn("CHINA_MOBILE", self.rules(guard.scan_bytes("example.py", ("+86 " + number).encode())))

    def test_package_version_is_not_an_email_address(self):
        for value in ("weflow-cli" + "@" + "1.5.0", "package" + "@" + "10.2.3"):
            self.assertNotIn("EMAIL_ADDRESS", self.rules(guard.scan_bytes("example.py", value.encode())))

    def test_personal_homes_for_windows_and_unix_are_detected(self):
        values = ["C:" + "/Users/" + "private-person/project", "C:" + "\\\\Users\\\\" + "private-person\\\\project",
                  "/" + "home/" + "private-person/project", "/" + "Users/" + "private-person/project"]
        for value in values:
            with self.subTest(style=value[:2]):
                self.assertIn("PERSONAL_HOME", self.rules(guard.scan_bytes("README.md", value.encode())))

    def test_non_ascii_personal_home_is_not_mistaken_for_a_placeholder(self):
        for value in ("D:" + "/Users/" + "合成用户/project", "/" + "home/" + "合成用户/project"):
            self.assertIn("PERSONAL_HOME", self.rules(guard.scan_bytes("README.md", value.encode())))

    def test_path_redaction_regex_is_not_a_real_personal_home(self):
        value = 'pattern = r"(/Users/|/home/)[^/]+"'
        self.assertEqual(guard.scan_bytes("analysis.py", value.encode()), [])

    def test_fixture_exception_is_exact_path_rule_and_literal_not_entire_tests(self):
        value = "sk" + "-" + "abcdef1234567890"
        self.assertEqual(guard.scan_bytes("tests/test_scoped_analysis.py", value.encode()), [])
        self.assertIn("API_TOKEN", self.rules(guard.scan_bytes("tests/other.py", value.encode())))
        self.assertIn("API_TOKEN", self.rules(guard.scan_bytes("tests/test_scoped_analysis.py", (value + "x").encode())))
        self.assertIn("PERSONAL_HOME", self.rules(guard.scan_bytes("tests/test_scoped_analysis.py", ("C:" + "/Us" + "ers/real-user").encode())))

    def test_personal_identifier_fixtures_are_exact_not_prefix_exemptions(self):
        path = "tests/test_scoped_analysis.py"
        samples = [
            ("WECHAT_ACCOUNT", "wx" + "id_" + "abc123", "wx" + "id_" + "abc123x"),
            ("EMAIL_ADDRESS", "test" + "@" + "example.com", "different" + "@" + "example.com"),
            ("CHINA_MOBILE", "138" + "12345678", "138" + "12345679"),
        ]
        for rule, allowed, blocked in samples:
            with self.subTest(rule=rule):
                self.assertEqual(guard.scan_bytes(path, allowed.encode()), [])
                self.assertIn(rule, self.rules(guard.scan_bytes("tests/other.py", allowed.encode())))
                self.assertIn(rule, self.rules(guard.scan_bytes(path, blocked.encode())))

    def test_guard_and_its_tests_are_publishable_without_a_whole_file_exception(self):
        self.assertEqual(guard.scan_bytes("scripts/check_public_release.py", SCRIPT.read_bytes()), [])
        self.assertEqual(guard.scan_bytes("tests/test_public_release.py", Path(__file__).read_bytes()), [])

    def test_tree_rejects_private_file_types_without_loading_them(self):
        self.write("archive.sqlite3")
        self.write("voice.wav")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Never read private binary content")):
            self.assertEqual(self.rules(guard.scan_tree(self.root)), {"PRIVATE_FILE_TYPE"})

    def test_symlinks_are_rejected_without_following_targets(self):
        outside = self.write("source.txt")
        link = self.root / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this Windows host")
        self.assertIn("LINK_OR_REPARSE_POINT", self.rules(guard.scan_tree(self.root)))

    def test_binary_oversized_and_invalid_utf8_files_fail_closed(self):
        self.assertIn("BINARY_OR_UNDECODABLE", self.rules(guard.scan_bytes("example.txt", b"hello\x00world")))
        self.assertIn("BINARY_OR_UNDECODABLE", self.rules(guard.scan_bytes("example.txt", b"\xff\xfe\xfa")))
        self.assertIn("FILE_TOO_LARGE", self.rules(guard.scan_bytes("example.txt", b"x" * (guard.MAX_FILE_BYTES + 1))))

    def test_git_scans_both_staged_blob_and_worktree_without_commits(self):
        self.git("init", "-q")
        token = "sk" + "-" + "1234abcd" * 4
        self.write("example.py", token)
        self.git("add", "example.py")
        self.write("example.py", "clean working tree\n")
        self.assertIn("API_TOKEN", self.rules(guard.scan_git(self.root)))
        self.git("add", "example.py")
        self.write("example.py", token)
        self.assertIn("API_TOKEN", self.rules(guard.scan_git(self.root)))
        self.write("example.py", "clean working tree\n")
        self.assertEqual(guard.scan_git(self.root), [])

    def test_git_untracked_files_are_not_published_but_tree_mode_detects_them(self):
        self.git("init", "-q")
        self.write("README.md")
        self.git("add", "README.md")
        self.write("private.sqlite3")
        self.assertEqual(guard.scan_git(self.root), [])
        self.assertIn("PRIVATE_FILE_TYPE", self.rules(guard.scan_tree(self.root)))

    def test_staged_private_paths_are_rejected_without_reading_blob(self):
        self.git("init", "-q")
        self.write("data/private/record.txt")
        self.git("add", "data/private/record.txt")
        with mock.patch.object(guard, "_git_blob", side_effect=AssertionError("Do not load private data")):
            findings = guard.scan_git(self.root)
        self.assertEqual(findings, [{"path": "data", "line": 0, "rule": "SENSITIVE_DIRECTORY"}])

    def test_cli_prints_only_positions_and_rules_never_sensitive_values(self):
        token = "sk" + "-" + "a1b2c3d4" * 4
        self.write("example.py", "safe\n" + token + "\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = guard.main(["--root", str(self.root), "--tree"])
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue().strip(), "example.py:2:API_TOKEN")
        self.assertNotIn(token, output.getvalue())

    def test_secret_in_filename_is_rejected_and_redacted_in_findings(self):
        token = "sk" + "-" + "a1b2c3d4" * 4
        self.write(token + ".txt")
        findings = guard.scan_tree(self.root)
        self.assertIn("SENSITIVE_FILENAME", self.rules(findings))
        self.assertNotIn(token, str(findings))

    def test_windows_reparse_attribute_is_rejected_without_symlink_privilege(self):
        from types import SimpleNamespace
        path = self.write("junction-target.txt")
        info = SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
        with mock.patch.object(Path, "lstat", return_value=info):
            self.assertTrue(guard._is_link(path))

    def test_git_symlink_index_mode_rejected_without_loading_blob(self):
        self.git("init", "-q")
        identity = "a" * 40
        def git_output(_root, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return os.fsencode(self.root)
            if args == ("ls-files", "--stage", "-z"):
                return ("120000 " + identity + " 0\tlink.txt\0").encode()
            self.fail("Index symlink content must not be read")
        with mock.patch.object(guard, "_git", side_effect=git_output):
            self.assertEqual(self.rules(guard.scan_git(self.root)), {"LINK_OR_NONREGULAR_INDEX_ENTRY"})


if __name__ == "__main__":
    unittest.main()
