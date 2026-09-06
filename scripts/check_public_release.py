#!/usr/bin/env python3
"""Fail-closed, offline publication guard; findings never contain matched values.

Default: scan every index entry (tracked/staged), its staged blob, and its
working-tree file. No commit history, cloud service, or credential verifier is
used. --tree scans a non-Git release copy, refusing private directories without
descending into them. This is a publication guard, not a security certification
or a guarantee that arbitrary free text contains no personal information.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


MAX_FILE_BYTES = 2 * 1024 * 1024
SENSITIVE_DIRECTORIES = frozenset({
    "data", "backups", "reports", "vendor", ".runtime", ".venv", "venv",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".ssh", ".aws", ".azure", ".gcloud", ".codex", ".git",
})
PRIVATE_EXTENSIONS = frozenset({
    ".db", ".db3", ".sqlite", ".sqlite3", ".wal", ".shm", ".bak",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".svg",
    ".mp3", ".wav", ".ogg", ".opus", ".silk", ".amr", ".m4a", ".flac", ".aac",
    ".mp4", ".avi", ".mov", ".mkv", ".webm", ".zip", ".7z", ".rar", ".tar", ".gz",
    ".pem", ".key", ".p12", ".pfx", ".jks", ".pkl", ".pickle",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".pyc", ".pyo", ".log",
})
PRIVATE_NAMES = frozenset({
    "all_keys.json", "keys.json", "credentials.json", "secrets.json", "token.json",
    "config.local.json", "settings.local.json", "messages.json", "emojis.json",
    "chat_history.txt", "analysis.json", "stats.json", "account.json", "accounts.json",
    "id_rsa", "id_ed25519", "id_ecdsa", "local_deployment.md",
})
# One authored, public vector icon is reviewed by exact path AND content.
# Do not replace this with a blanket media/assets exemption.
PUBLIC_ASSET_HASHES = {
    "dashboard/static/favicon.svg": "a2929547da736535a47a9d5a8bab81e090b42f3d6b924aa831d8f61d2091a0de",
}

RULES = (
    ("WECHAT_ACCOUNT", re.compile(r"(?i)(?<![A-Za-z0-9_])wxid_[A-Za-z0-9_-]{3,}(?![A-Za-z0-9_-])")),
    ("EMAIL_ADDRESS", re.compile(r"(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}(?![A-Za-z0-9._%+-])")),
    ("CHINA_MOBILE", re.compile(r"(?<![A-Za-z0-9])(?:\+?86[ -]?)?1[3-9][0-9]{9}(?![A-Za-z0-9])")),
    ("API_TOKEN", re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{12,})(?![A-Za-z0-9])")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("PERSONAL_HOME", re.compile(r"(?i)(?:[a-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>\[\]{}^|()][^\\/\s\"'<>\[\]{}^]*|/(?:home|Users)/[^/\s\"'<>\[\]{}^|()][^/\s\"'<>\[\]{}^]*)")),
    ("CREDENTIAL_URL", re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|https?)://[^\s/:@]+:[^\s/@]{4,}@")),
    ("CREDENTIAL_LITERAL", re.compile(r'''(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|client[_-]?secret|secret|sealed[_-]?key)\b["']?\s*[:=]\s*["'](?P<value>[^"'\r\n]{8,})["']''')),
)

# Each exception is a specific synthetic literal in one known regression test.
# No directory, basename glob, comment directive, entropy rule, or secret-like
# prefix automatically exempts a test, documentation file, or code sample.
SYNTHETIC_ALLOWLIST = {
    ("tests/test_ciphertalk_diagnostics.py", "WECHAT_ACCOUNT"): frozenset({"wxid_" + "example"}),
    ("tests/test_dashboard.py", "WECHAT_ACCOUNT"): frozenset("wxid_" + suffix for suffix in ("secret", "private")),
    ("tests/test_incremental_sync.py", "WECHAT_ACCOUNT"): frozenset("wxid_" + suffix for suffix in ("example", "other")),
    ("tests/test_message_merge.py", "WECHAT_ACCOUNT"): frozenset("wxid_" + suffix for suffix in ("friend", "other", "other_owner")),
    ("tests/test_message_pipeline.py", "WECHAT_ACCOUNT"): frozenset({"wxid_" + "friend"}),
    ("tests/test_relationship_timeline_ai.py", "WECHAT_ACCOUNT"): frozenset({"wxid_" + "synthetic"}),
    ("tests/test_scoped_analysis.py", "WECHAT_ACCOUNT"): frozenset("wxid_" + suffix for suffix in ("abc123", "testid")),
    ("tests/test_wechat_cfg.py", "WECHAT_ACCOUNT"): frozenset({"wxid_" + "fixture"}),
    ("tests/test_wechat_sync.py", "WECHAT_ACCOUNT"): frozenset("wxid_" + suffix for suffix in (
        "private", "first", "second", "only", "friend", "active", "historical",
        "synthetic", "self", "cross_partition", "synthetic_me", "synthetic_me_A1b2",
        "synthetic_me_other", "synthetic_structure",
    )),
    ("tests/test_scoped_analysis.py", "EMAIL_ADDRESS"): frozenset({"test" + "@" + "example.com", "person" + "@" + "test.example"}),
    ("tests/test_scoped_analysis.py", "CHINA_MOBILE"): frozenset({"138" + "12345678", "+86 " + "138" + "12345678"}),
    ("tests/test_voice_analysis.py", "CHINA_MOBILE"): frozenset({"138" + "12345678"}),
    ("tests/test_scoped_analysis.py", "API_TOKEN"): frozenset({"sk" + "-abcdef1234567890"}),
    ("tests/test_scoped_analysis.py", "PERSONAL_HOME"): frozenset({"C:" + "/Users/" + "someone"}),
    ("tests/test_local_exporter_bootstrap.py", "PERSONAL_HOME"): frozenset({"C:" + "/Users/" + "private"}),
    ("tests/test_wechat_backup.py", "PERSONAL_HOME"): frozenset({"C:" + "/Users/" + "synthetic-user"}),
    ("tests/test_analysis_metrics.py", "CREDENTIAL_LITERAL"): frozenset({"UNIQUE-PRIVATE-TEXT-AND-IDENTITY"}),
    ("tests/test_analysis_segment_safety.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-safety-key-never-real"}),
    ("tests/test_analysis_ui.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-analysis-test-token"}),
    ("tests/test_backup_api.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-local-token"}),
    ("tests/test_backup_ui.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-backup-test-token"}),
    ("tests/test_dashboard.py", "CREDENTIAL_LITERAL"): frozenset({"test-secret"}),
    ("tests/test_dashboard_search.py", "CREDENTIAL_LITERAL"): frozenset({"not-returned", "synthetic-test-token"}),
    ("tests/test_dashboard_ui.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-test-token"}),
    ("tests/test_library_api.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-not-real-key"}),
    ("tests/test_relationship_timeline.py", "CREDENTIAL_LITERAL"): frozenset({"PRIVATE-NAME-ID-TEXT-AND-PATH"}),
    ("tests/test_scoped_analysis.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-api-key-not-real-12345", "synthetic-local-server-token", r"bad\nkey"}),
    ("tests/test_voice_analysis.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-voice-local-token"}),
    ("tests/test_voice_gallery_ui.py", "CREDENTIAL_LITERAL"): frozenset({"synthetic-gallery-local-token"}),
}
PLACEHOLDERS = frozenset({"YOUR_API_KEY", "YOUR_TOKEN", "REPLACE_ME", "your_api_key_here", "your_password_here"})


def _finding(path, rule, line=0):
    # Escape control characters in filenames so one finding always stays one
    # line; never include an absolute root, exception body, or matched value.
    safe = re.sub(r"[\x00-\x1f\x7f]", "?", str(path))
    for _, pattern in RULES:
        safe = pattern.sub("<redacted>", safe)
    return {"path": safe, "line": line, "rule": rule}


def _relative_parts(relative):
    relative = str(relative).replace("\\", "/")
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"..", "."} for part in path.parts) or ":" in relative:
        return None
    return path.parts


def _path_findings(relative):
    parts = _relative_parts(relative)
    if parts is None:
        return [_finding(".", "INVALID_RELATIVE_PATH")]
    if any(pattern.search(str(relative)) for _, pattern in RULES):
        return [_finding(relative, "SENSITIVE_FILENAME")]
    for index, part in enumerate(parts):
        if part.lower() in SENSITIVE_DIRECTORIES or (index and parts[index - 1].lower() == "scripts" and part.lower() == "tmp"):
            return [_finding("/".join(parts[:index + 1]), "SENSITIVE_DIRECTORY")]
    name = parts[-1].lower()
    if name in PRIVATE_NAMES or name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample"}):
        return [_finding("/".join(parts), "PRIVATE_FILE_NAME")]
    return []


def _allowed(relative, rule, value):
    if rule == "PERSONAL_HOME":
        value = re.sub(r"[\\/]+", "/", value)
    if rule == "CREDENTIAL_LITERAL" and value in PLACEHOLDERS:
        return True
    return value in SYNTHETIC_ALLOWLIST.get((relative, rule), ())


def scan_bytes(relative, content):
    """Scan an already-authorized public candidate without returning its text."""
    relative = str(relative).replace("\\", "/")
    blocked = _path_findings(relative)
    if blocked:
        return blocked
    name = PurePosixPath(relative).name.lower()
    if PurePosixPath(name).suffix in PRIVATE_EXTENSIONS or name.endswith(("-wal", "-shm", "-journal")):
        if PUBLIC_ASSET_HASHES.get(relative) != hashlib.sha256(content).hexdigest():
            return [_finding(relative, "PRIVATE_FILE_TYPE")]
    if len(content) > MAX_FILE_BYTES:
        return [_finding(relative, "FILE_TOO_LARGE")]
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        return [_finding(relative, "BINARY_OR_UNDECODABLE")]
    if "\x00" in text:
        return [_finding(relative, "BINARY_OR_UNDECODABLE")]
    findings = []
    for rule, pattern in RULES:
        for match in pattern.finditer(text):
            value = match.group("value") if rule == "CREDENTIAL_LITERAL" else match.group()
            if not _allowed(relative, rule, value):
                findings.append(_finding(relative, rule, text.count("\n", 0, match.start()) + 1))
    return _unique(findings)


def _unique(findings):
    values = {(item["path"], item["line"], item["rule"]) for item in findings}
    return [{"path": path, "line": line, "rule": rule} for path, line, rule in sorted(values)]


def _is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _working_file(root, relative):
    findings = _path_findings(relative)
    if findings:
        return findings
    name = PurePosixPath(relative).name.lower()
    if (PurePosixPath(name).suffix in PRIVATE_EXTENSIONS or name.endswith(("-wal", "-shm", "-journal"))) and relative not in PUBLIC_ASSET_HASHES:
        return [_finding(relative, "PRIVATE_FILE_TYPE")]
    path = root
    try:
        for part in _relative_parts(relative):
            path = path / part
            if _is_link(path):
                return [_finding(relative, "LINK_OR_REPARSE_POINT")]
        if not path.is_file():
            return [_finding(relative, "NOT_A_REGULAR_FILE")]
        if path.stat().st_size > MAX_FILE_BYTES:
            return [_finding(relative, "FILE_TOO_LARGE")]
        return scan_bytes(relative, path.read_bytes())
    except FileNotFoundError:
        return [_finding(relative, "WORKTREE_FILE_MISSING")]
    except OSError:
        return [_finding(relative, "FILE_UNREADABLE")]


def scan_tree(root):
    """Scan a release tree; never traverse private or link/reparse directories."""
    root = Path(root).resolve()
    findings = []
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            findings.append(_finding(prefix or ".", "DIRECTORY_UNREADABLE"))
            continue
        for entry in entries:
            if not prefix and entry.name == ".git":
                continue  # Git administration is never a publishable file.
            relative = prefix + entry.name
            blocked = _path_findings(relative)
            if blocked:
                findings.extend(blocked)
                continue
            try:
                if _is_link(Path(entry.path)):
                    findings.append(_finding(relative, "LINK_OR_REPARSE_POINT"))
                elif entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), relative + "/"))
                else:
                    findings.extend(_working_file(root, relative))
            except OSError:
                findings.append(_finding(relative, "FILE_UNREADABLE"))
    return _unique(findings)


def _git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _git_blob(root, identity):
    # identity comes only from ls-files, never an arbitrary revision/history.
    size = _git(root, "cat-file", "-s", identity)
    if size is None or not size.strip().isdigit():
        return None, "INDEX_BLOB_UNREADABLE"
    if int(size) > MAX_FILE_BYTES:
        return None, "FILE_TOO_LARGE"
    value = _git(root, "cat-file", "blob", identity)
    return (value, None) if value is not None else (None, "INDEX_BLOB_UNREADABLE")


def scan_git(root):
    """Inspect the index AND working tree; staged content cannot be hidden."""
    root = Path(root).resolve()
    top = _git(root, "rev-parse", "--show-toplevel")
    try:
        if top is None or Path(os.fsdecode(top.strip())).resolve() != root:
            return [_finding(".", "NOT_GIT_REPOSITORY")]
    except (OSError, ValueError):
        return [_finding(".", "NOT_GIT_REPOSITORY")]
    index = _git(root, "ls-files", "--stage", "-z")
    if index is None:
        return [_finding(".", "INDEX_UNREADABLE")]
    if not index:
        return [_finding(".", "EMPTY_INDEX")]
    findings = []
    for record in index.split(b"\0"):
        if not record:
            continue
        try:
            header, filename = record.split(b"\t", 1)
            mode, identity, stage = header.decode("ascii").split()
            relative = filename.decode("utf-8")
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", identity):
                raise ValueError
        except (ValueError, UnicodeError):
            findings.append(_finding(".", "INDEX_ENTRY_INVALID"))
            continue
        blocked = _path_findings(relative)
        if blocked:
            findings.extend(blocked)
            continue
        if stage != "0":
            findings.append(_finding(relative, "UNMERGED_INDEX_ENTRY"))
            continue
        if mode not in {"100644", "100755"}:
            findings.append(_finding(relative, "LINK_OR_NONREGULAR_INDEX_ENTRY"))
            continue
        # Refuse media/data blobs before loading them, except the reviewed icon.
        name = PurePosixPath(relative).name.lower()
        if (PurePosixPath(name).suffix in PRIVATE_EXTENSIONS or name.endswith(("-wal", "-shm", "-journal"))) and relative not in PUBLIC_ASSET_HASHES:
            findings.append(_finding(relative, "PRIVATE_FILE_TYPE"))
            continue
        content, error = _git_blob(root, identity)
        findings.extend([_finding(relative, error)] if error else scan_bytes(relative, content))
        findings.extend(_working_file(root, relative))
    return _unique(findings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tree", action="store_true", help="Scan a non-Git publication tree, including untracked files")
    args = parser.parse_args(argv)
    findings = scan_tree(args.root) if args.tree else scan_git(args.root)
    for item in findings:
        print(f"{item['path']}:{item['line']}:{item['rule']}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
