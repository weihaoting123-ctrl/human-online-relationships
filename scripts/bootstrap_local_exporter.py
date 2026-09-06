"""Prepare a pinned, repository-local WeFlow CLI runtime.

This bootstrapper intentionally has a very small responsibility: install the
audited ``weflow-cli`` package into ``.runtime/weflow-cli`` without lifecycle
scripts.  It never invokes the exporter, reads chat databases, or prints child
process output.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / ".runtime" / "weflow-cli"
RUNTIME_LABEL = ".runtime/weflow-cli"

PACKAGE_NAME = "weflow-cli"
PACKAGE_VERSION = "1.5.0"
PACKAGE_SPEC = f"{PACKAGE_NAME}@{PACKAGE_VERSION}"
PACKAGE_INTEGRITY = (
    "sha512-SKjZtfh2DV9dRefKXKYdc5c7EnlOfUQpk7ym0cjhwg/"
    "viIv6i3U5rpHRbEYbsd/5xqC87tGNS3fSmt0giuP2eg=="
)
OFFICIAL_REGISTRY = "https://registry.npmjs.org/"
MINIMUM_NODE = (18, 0, 0)

RUNTIME_PACKAGE_NAME = "she-love-me-local-exporter-runtime"
RUNTIME_PACKAGE_VERSION = "1.0.0"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LOCK_BYTES = 16 * 1024 * 1024

PACKAGE_JSON = {
    "name": RUNTIME_PACKAGE_NAME,
    "version": RUNTIME_PACKAGE_VERSION,
    "private": True,
    "description": "Pinned local runtime for the she-love-me WeChat exporter",
    "dependencies": {PACKAGE_NAME: PACKAGE_VERSION},
}

NPMRC_TEXT = "\n".join((
    f"registry={OFFICIAL_REGISTRY}",
    "ignore-scripts=true",
    "audit=false",
    "fund=false",
    "update-notifier=false",
    "save-exact=true",
    "package-lock=true",
    "",
))

SAFE_ENV_KEYS = {
    "ALL_PROXY", "APPDATA", "COMSPEC", "HTTPS_PROXY", "HTTP_PROXY",
    "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "NO_PROXY", "PATH", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP",
    "USERPROFILE", "WINDIR",
}


class BootstrapError(RuntimeError):
    """An expected failure represented by a non-sensitive public code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SafeArgumentParser(argparse.ArgumentParser):
    """Convert parser failures to a stable code without echoing user input."""

    def error(self, message):
        del message
        raise BootstrapError("invalid_arguments")


def _is_reparse_point(path):
    """Return true for symlinks and Windows junction/reparse points."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _assert_runtime_boundary(create=False):
    """Keep all bootstrap writes inside the repository-owned runtime."""
    repo = REPO_ROOT.resolve(strict=True)
    lexical_runtime = Path(os.path.abspath(RUNTIME_DIR))
    try:
        lexical_runtime.relative_to(repo)
    except ValueError as exc:
        raise BootstrapError("runtime_outside_repository") from exc

    runtime_parent = repo / ".runtime"
    for candidate in (runtime_parent, lexical_runtime):
        if candidate.exists() and _is_reparse_point(candidate):
            raise BootstrapError("runtime_reparse_point")

    if create:
        runtime_parent.mkdir(exist_ok=True)
        lexical_runtime.mkdir(exist_ok=True)

    if lexical_runtime.exists():
        try:
            lexical_runtime.resolve(strict=True).relative_to(repo)
        except ValueError as exc:
            raise BootstrapError("runtime_outside_repository") from exc
    return lexical_runtime


def _assert_managed_paths_safe(runtime):
    """Reject indirection that could make npm alter files outside the runtime."""
    managed = (
        "package.json", "package-lock.json", ".npmrc", ".npmrc-global",
        "node_modules", ".npm-cache",
    )
    for name in managed:
        candidate = runtime / name
        if candidate.exists() and _is_reparse_point(candidate):
            raise BootstrapError("runtime_reparse_point")
    # npm gives shrinkwrap precedence over package-lock.  This runtime never
    # creates one, so its presence is an unsafe, unexpected control file.
    if (runtime / "npm-shrinkwrap.json").exists():
        raise BootstrapError("unexpected_npm_control_file")


def _safe_read_json(path, maximum_bytes):
    if _is_reparse_point(path):
        return None
    try:
        if not path.is_file() or path.stat().st_size > maximum_bytes:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _atomic_write_text(path, content):
    if path.exists() and _is_reparse_point(path):
        raise BootstrapError("runtime_reparse_point")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        if _is_reparse_point(temporary):
            raise BootstrapError("runtime_reparse_point")
        temporary.unlink()
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists() and not _is_reparse_point(temporary):
            temporary.unlink()


def _expected_package_json_text():
    return json.dumps(PACKAGE_JSON, ensure_ascii=False, indent=2) + "\n"


def verify_runtime_manifest(runtime_dir=None):
    runtime = Path(runtime_dir) if runtime_dir else RUNTIME_DIR
    data = _safe_read_json(runtime / "package.json", MAX_MANIFEST_BYTES)
    return data == PACKAGE_JSON


def _is_official_tarball_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return all((
            parsed.scheme == "https",
            parsed.hostname == "registry.npmjs.org",
            parsed.port in (None, 443),
            parsed.username is None,
            parsed.password is None,
            not parsed.query,
            not parsed.fragment,
        ))
    except ValueError:
        return False


def verify_lockfile(runtime_dir=None):
    """Validate lock provenance and the audited top-level package integrity."""
    runtime = Path(runtime_dir) if runtime_dir else RUNTIME_DIR
    lock = _safe_read_json(runtime / "package-lock.json", MAX_LOCK_BYTES)
    if not isinstance(lock, dict) or lock.get("lockfileVersion") not in (2, 3):
        return False
    if lock.get("name") != RUNTIME_PACKAGE_NAME:
        return False
    if lock.get("version") != RUNTIME_PACKAGE_VERSION:
        return False

    packages = lock.get("packages")
    if not isinstance(packages, dict):
        return False
    root = packages.get("")
    if not isinstance(root, dict):
        return False
    if root.get("dependencies") != {PACKAGE_NAME: PACKAGE_VERSION}:
        return False

    exporter = packages.get(f"node_modules/{PACKAGE_NAME}")
    if not isinstance(exporter, dict):
        return False
    if exporter.get("version") != PACKAGE_VERSION:
        return False
    if exporter.get("integrity") != PACKAGE_INTEGRITY:
        return False
    if not _is_official_tarball_url(exporter.get("resolved")):
        return False

    for record in packages.values():
        if not isinstance(record, dict):
            return False
        resolved = record.get("resolved")
        if resolved is not None:
            if not _is_official_tarball_url(resolved):
                return False
            integrity = record.get("integrity")
            if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                return False
    return True


def verify_installed_package(runtime_dir=None):
    runtime = Path(runtime_dir) if runtime_dir else RUNTIME_DIR
    package_root = runtime / "node_modules" / PACKAGE_NAME
    if _is_reparse_point(runtime / "node_modules") or _is_reparse_point(package_root):
        return False
    data = _safe_read_json(package_root / "package.json", MAX_MANIFEST_BYTES)
    if not isinstance(data, dict):
        return False
    if data.get("name") != PACKAGE_NAME or data.get("version") != PACKAGE_VERSION:
        return False

    binary = data.get("bin")
    if isinstance(binary, dict):
        entry = binary.get(PACKAGE_NAME)
    elif isinstance(binary, str):
        entry = binary
    else:
        entry = None
    if not isinstance(entry, str) or not entry:
        return False
    entry_path = Path(entry)
    if entry_path.is_absolute() or ".." in entry_path.parts:
        return False
    resolved_entry = package_root / entry_path
    try:
        resolved_package_root = package_root.resolve(strict=True)
        resolved_entry.resolve(strict=True).relative_to(resolved_package_root)
    except (OSError, ValueError):
        return False
    for candidate in (resolved_entry, *resolved_entry.parents):
        if candidate == package_root:
            break
        if _is_reparse_point(candidate):
            return False
    return resolved_entry.is_file() and not _is_reparse_point(resolved_entry)


def lock_sha256(runtime_dir=None):
    runtime = Path(runtime_dir) if runtime_dir else RUNTIME_DIR
    lock_path = runtime / "package-lock.json"
    if not verify_lockfile(runtime):
        return None
    digest = hashlib.sha256()
    with lock_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_node():
    return shutil.which("node.exe") or shutil.which("node")


def _find_npm():
    return shutil.which("npm.cmd") or shutil.which("npm")


def _sanitized_npm_environment(runtime):
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}
    env.update({
        "NPM_CONFIG_REGISTRY": OFFICIAL_REGISTRY,
        "NPM_CONFIG_USERCONFIG": str(runtime / ".npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(runtime / ".npmrc-global"),
        "NPM_CONFIG_CACHE": str(runtime / ".npm-cache"),
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NO_UPDATE_NOTIFIER": "1",
    })
    return env


def _run_captured(command, cwd, env, timeout, stage):
    """Run a prerequisite command without ever forwarding its raw output."""
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(f"{stage}_failed") from exc
    if result.returncode != 0:
        raise BootstrapError(f"{stage}_failed")
    return result.stdout.strip()


def _validate_node(node, runtime, env):
    output = _run_captured([node, "--version"], runtime, env, 15, "node_check")
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", output)
    if not match:
        raise BootstrapError("node_version_invalid")
    version = tuple(int(part) for part in match.groups())
    if version < MINIMUM_NODE:
        raise BootstrapError("node_version_unsupported")


def _npm_arguments(npm, action, runtime):
    common = [
        npm,
        action,
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        f"--registry={OFFICIAL_REGISTRY}",
        "--prefix",
        str(runtime),
    ]
    if action == "install":
        common.extend(("--package-lock-only", "--save-exact", PACKAGE_SPEC))
    return common


def install_runtime():
    runtime = _assert_runtime_boundary(create=True)
    _assert_managed_paths_safe(runtime)
    node = _find_node()
    npm = _find_npm()
    if not node:
        raise BootstrapError("node_missing")
    if not npm:
        raise BootstrapError("npm_missing")

    manifest_path = runtime / "package.json"
    if manifest_path.exists() and not verify_runtime_manifest(runtime):
        raise BootstrapError("manifest_invalid")
    if not manifest_path.exists():
        _atomic_write_text(manifest_path, _expected_package_json_text())
    _atomic_write_text(runtime / ".npmrc", NPMRC_TEXT)
    _atomic_write_text(runtime / ".npmrc-global", "")

    environment = _sanitized_npm_environment(runtime)
    _validate_node(node, runtime, environment)

    lock_path = runtime / "package-lock.json"
    if lock_path.exists() and not verify_lockfile(runtime):
        raise BootstrapError("lock_invalid")
    if not lock_path.exists():
        _run_captured(
            _npm_arguments(npm, "install", runtime), runtime, environment, 600,
            "lock_generation",
        )
    if not verify_lockfile(runtime):
        raise BootstrapError("lock_invalid")

    _run_captured(
        _npm_arguments(npm, "ci", runtime), runtime, environment, 900,
        "package_installation",
    )
    if not verify_installed_package(runtime):
        raise BootstrapError("package_invalid")
    return True


def inspect_runtime():
    try:
        runtime = _assert_runtime_boundary(create=False)
        boundary_valid = True
    except BootstrapError:
        runtime = RUNTIME_DIR
        boundary_valid = False

    manifest_valid = boundary_valid and verify_runtime_manifest(runtime)
    lock_valid = boundary_valid and verify_lockfile(runtime)
    package_valid = boundary_valid and verify_installed_package(runtime)
    node_available = bool(_find_node())
    npm_available = bool(_find_npm())
    ready = all((
        boundary_valid, manifest_valid, lock_valid, package_valid,
        node_available, npm_available,
    ))
    return {
        "status": "ok" if ready else "missing",
        "provider": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "registry": OFFICIAL_REGISTRY,
        "runtime": RUNTIME_LABEL,
        "node_available": node_available,
        "npm_available": npm_available,
        "manifest_valid": bool(manifest_valid),
        "lock_valid": bool(lock_valid),
        "package_valid": bool(package_valid),
        "lock_sha256": lock_sha256(runtime) if lock_valid else None,
        "ready": ready,
    }


def _error_report(code):
    return {
        "status": "error",
        "provider": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "registry": OFFICIAL_REGISTRY,
        "runtime": RUNTIME_LABEL,
        "ready": False,
        "changed": False,
        "error_code": code,
    }


def main(argv=None):
    parser = SafeArgumentParser(
        description="Check or install the pinned local WeFlow CLI runtime"
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="install the audited package into the repository-local runtime",
    )
    changed = False
    try:
        args = parser.parse_args(argv)
        if args.install:
            install_runtime()
            changed = True
        report = inspect_runtime()
        report["changed"] = changed
        if not args.install and not report["ready"]:
            report["next_action"] = "rerun_with_install"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1
    except BootstrapError as exc:
        print(json.dumps(_error_report(exc.code), ensure_ascii=False, indent=2))
        return 1
    except Exception:
        # Never serialize exception text: npm and OS exceptions can contain
        # command arguments, local paths, registry credentials, or raw logs.
        print(json.dumps(_error_report("bootstrap_failed"), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
