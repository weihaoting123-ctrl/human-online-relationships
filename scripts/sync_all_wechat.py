"""Safely import every discoverable WeChat session into the local dashboard.

The source WeChat DB/WAL/SHM files are only copied with ordinary read handles.
SQLCipher opens the resulting private snapshot in read-only/query-only mode.
Changed source partitions create immutable raw JSON snapshots, then update
the derived ``data/contacts`` bundles through ``message_merge``. Unchanged
DB/WAL/SHM signatures and per-partition session fingerprints skip redundant
exports and statistics. No command in this module
sends messages, uploads data, or executes SQL that mutates WeChat.

Only aggregate status is written to stdout.  Account IDs, paths, keys, session
IDs, display names, and message bodies remain in local ignored data folders.
"""

from __future__ import annotations

import argparse
import base64
import contextvars
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
RUNTIME_ROOT = REPO_ROOT / ".runtime" / "weflow-cli"
PACKAGE_ROOT = RUNTIME_ROOT / "node_modules" / "weflow-cli"
BRIDGE_PATH = SCRIPTS_DIR / "weflow_local_bridge.mjs"
NT_READER_PATH = PACKAGE_ROOT / "scripts" / "nt_decrypt.py"

DATA_ROOT = Path(
    os.environ.get("SHE_LOVE_ME_DATA_DIR", str(REPO_ROOT / "data"))
).resolve()
CONTACTS_DIR = DATA_ROOT / "contacts"
RAW_ROOT = DATA_ROOT / "raw" / "wechat-sync"
PRIVATE_ROOT = DATA_ROOT / "private" / "wechat-sync"
STATUS_PATH = PRIVATE_ROOT / "status.json"
KEY_CACHE_PATH = PRIVATE_ROOT / "key-cache.json"
LOCK_PATH = PRIVATE_ROOT / "sync.lock"
RAW_ARCHIVE_ROOT = PRIVATE_ROOT / "raw-archive"

BRIDGE_MARKER = "__SHE_LOVE_ME_PRIVATE_JSON__"
HEX_64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
HEX_32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_@.\-]{1,240}$")
MESSAGE_TABLE_RE = re.compile(r"^Msg_[0-9a-f]{32}$", re.IGNORECASE)
SOURCE_SNAPSHOT_RETRIES = 3
MAX_BINARY_CANDIDATES_PER_SALT = 16
MAX_BINARY_CANDIDATES_TOTAL = 64
MAX_BINARY_SCAN_BYTES = 512 * 1024 * 1024
MAX_BINARY_SCAN_SECONDS = 30
MAX_PASSPHRASE_CANDIDATES = 16
MAX_PASSPHRASE_VALIDATION_SECONDS = 30
MAX_MESSAGE_CONTENT_BYTES = 16 * 1024 * 1024
ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"
PUBLIC_STATUS_FIELDS = (
    "enabled",
    "state",
    "mode",
    "last_run_at",
    "next_run_at",
    "scanned_conversations",
    "imported_conversations",
    "failed_conversations",
    "imported_messages",
    "deduplicated_messages",
    "unchanged_messages",
    "skipped_databases",
    "processed_databases",
    "skipped_conversations",
    "read_messages",
    "sync_strategy",
    "error_code",
    "attention",
    "original_wechat_untouched",
    "raw_snapshots_retained",
)
SAFE_BRIDGE_ENV_KEYS = {
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bootstrap_local_exporter import inspect_runtime  # noqa: E402
from contact_bundle import resolve_bundle_paths  # noqa: E402
from convert_weflow_cli import convert_payload as convert_weflow_cli_payload  # noqa: E402
from message_merge import merge_into_bundle  # noqa: E402
from message_normalizer import normalize_payload  # noqa: E402


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class SyncFailure(RuntimeError):
    """A sanitized failure that is safe to expose through local status."""

    def __init__(self, code: str, attention: str | None = None):
        super().__init__(code)
        self.code = code
        self.attention = attention


class _SnapshotConnection:
    """Proxy a SQLCipher connection and release its private snapshot on close."""

    def __init__(self, connection, cleanup=None):
        self._connection = connection
        self._cleanup = cleanup
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        if self._closed:
            return None
        self._closed = True
        try:
            return self._connection.close()
        finally:
            if self._cleanup is not None:
                self._cleanup()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


def _source_file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return write-relevant metadata without opening a source file for writing."""

    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _source_database_members(path: Path) -> tuple[Path, Path, Path]:
    return (
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    )


def _source_member_signatures(path: Path) -> tuple[tuple[int, int, int, int, int] | None, ...]:
    return tuple(_source_file_signature(item) for item in _source_database_members(path))


def _file_digest(path: Path) -> str | None:
    """Hash a source member through a read-only handle.

    SQLite updates the WAL index through mmap on Windows, where timestamps are
    not a sufficient change detector.  Comparing source/copy/source digests
    prevents accepting a torn DB/WAL/SHM set even when metadata appears stable.
    """

    try:
        digest = hashlib.sha256()
        with path.open("rb", buffering=0) as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


def _member_digests(path: Path) -> tuple[str | None, ...]:
    return tuple(_file_digest(item) for item in _source_database_members(path))


def _database_fingerprint(signatures, digests) -> dict[str, Any]:
    return {
        "metadata": [list(item) if item is not None else None for item in signatures],
        "sha256": list(digests),
    }


def _stable_database_fingerprint(path: Path) -> dict[str, Any]:
    """Compare all source bytes, including mmap-written WAL/SHM, read-only.

    Timestamps alone are insufficient on Windows.  These ordinary file reads
    deliberately remain part of every run; an unchanged result avoids the far
    more expensive copy, SQLCipher, message decode, export, merge and stats.
    """
    for _attempt in range(SOURCE_SNAPSHOT_RETRIES):
        try:
            before = _source_member_signatures(path)
            if before[0] is None:
                raise SyncFailure("wechat_database_not_found", "微信数据库路径已变化。")
            first = _member_digests(path)
            middle = _source_member_signatures(path)
            second = _member_digests(path)
            after = _source_member_signatures(path)
            if before == middle == after and first == second:
                return _database_fingerprint(before, first)
        except OSError:
            continue
    raise SyncFailure("source_database_busy", "微信数据库正在更新；等待下一次只读同步。")


class _DatabaseSnapshotSet:
    """Own stable, private copies of source DB/WAL/SHM files for one operation."""

    def __init__(self):
        snapshot_root = PRIVATE_ROOT / "db-snapshots"
        snapshot_root.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(
            prefix="sync-", dir=str(snapshot_root)
        )
        self.root = Path(self._temporary.name)
        self._paths: dict[Path, Path] = {}
        self.fingerprints: dict[Path, dict[str, Any]] = {}

    def close(self) -> None:
        self._temporary.cleanup()

    def prepare(self, paths) -> None:
        for path in paths:
            self.path_for(Path(path))

    def path_for(self, source: Path) -> Path:
        source = source.resolve()
        cached = self._paths.get(source)
        if cached is not None:
            return cached
        if not source.is_file():
            raise SyncFailure("wechat_database_not_found", "微信数据库路径已变化。")

        source_members = _source_database_members(source)
        source_token = hashlib.sha256(str(source).casefold().encode("utf-8")).hexdigest()[:20]
        for attempt in range(SOURCE_SNAPSHOT_RETRIES):
            before = _source_member_signatures(source)
            if before[0] is None:
                raise SyncFailure("wechat_database_not_found", "微信数据库路径已变化。")

            attempt_root = self.root / f"{source_token}-{attempt}"
            attempt_root.mkdir(parents=False, exist_ok=False)
            target = attempt_root / source.name
            target_members = _source_database_members(target)
            try:
                source_digests_before = _member_digests(source)
                after_first_digest = _source_member_signatures(source)
                if before != after_first_digest:
                    shutil.rmtree(attempt_root, ignore_errors=True)
                    continue
                for source_member, target_member, signature in zip(
                    source_members, target_members, before
                ):
                    if signature is not None:
                        shutil.copyfile(source_member, target_member)
                middle = _source_member_signatures(source)
                copied_sizes = tuple(
                    _source_file_signature(item)[2] if item.is_file() else None
                    for item in target_members
                )
            except OSError:
                shutil.rmtree(attempt_root, ignore_errors=True)
                continue

            expected_sizes = tuple(
                signature[2] if signature is not None else None for signature in before
            )
            if before != middle or copied_sizes != expected_sizes:
                shutil.rmtree(attempt_root, ignore_errors=True)
                continue

            try:
                copied_digests = _member_digests(target)
                source_digests_after = _member_digests(source)
                after = _source_member_signatures(source)
            except OSError:
                shutil.rmtree(attempt_root, ignore_errors=True)
                continue

            if (
                before == after
                and source_digests_before == copied_digests == source_digests_after
            ):
                self._paths[source] = target
                self.fingerprints[source] = _database_fingerprint(before, copied_digests)
                return target
            shutil.rmtree(attempt_root, ignore_errors=True)

        raise SyncFailure(
            "source_database_busy",
            "微信数据库正在更新，未能取得稳定只读快照；本次未导入。",
        )


_ACTIVE_DATABASE_SNAPSHOTS: contextvars.ContextVar[_DatabaseSnapshotSet | None] = (
    contextvars.ContextVar("wechat_database_snapshots", default=None)
)


@contextmanager
def _database_snapshot_scope():
    """Reuse one private snapshot set across key validation and message reads."""

    existing = _ACTIVE_DATABASE_SNAPSHOTS.get()
    if existing is not None:
        yield existing
        return
    snapshots = _DatabaseSnapshotSet()
    token = _ACTIVE_DATABASE_SNAPSHOTS.set(snapshots)
    try:
        yield snapshots
    finally:
        _ACTIVE_DATABASE_SNAPSHOTS.reset(token)
        snapshots.close()


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds")


def _next_scheduled_run(now: datetime | None = None) -> str:
    """Match the daily 20:00 Asia/Shanghai local automation, without tzdata."""
    current = (now or _utc_now()).astimezone(timezone(timedelta(hours=8)))
    target = current.replace(hour=20, minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return _iso(target)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, payload: Any) -> None:
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, body)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _base_status(mode: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "state": "ready",
        "mode": mode,
        "last_run_at": None,
        "next_run_at": None,
        "scanned_conversations": 0,
        "imported_conversations": 0,
        "failed_conversations": 0,
        "imported_messages": 0,
        "deduplicated_messages": 0,
        "unchanged_messages": 0,
        "skipped_databases": 0,
        "processed_databases": 0,
        "skipped_conversations": 0,
        "read_messages": 0,
        "sync_strategy": "changed-shards",
        "error_code": None,
        "attention": None,
        "original_wechat_untouched": True,
        "raw_snapshots_retained": True,
    }


def _write_status(status: dict[str, Any]) -> None:
    _atomic_write_json(STATUS_PATH, status)


def public_status(status: dict[str, Any] | None) -> dict[str, Any]:
    source = status if isinstance(status, dict) else _base_status("manual")
    return {key: source.get(key) for key in PUBLIC_STATUS_FIELDS}


def _dashboard_error_code(code: str) -> str:
    if code == "needs_account_selection":
        return "account_selection_required"
    if code == "wechat_not_running":
        return "wechat_not_running"
    if code in {"permission_required", "database_key_unavailable"}:
        return "permission_required"
    if code in {"exporter_missing", "sync_dependencies_missing"}:
        return "exporter_not_ready"
    return "sync_failed"


@contextmanager
def _silence_native_output():
    """Temporarily discard C-extension stdout/stderr without changing logs.

    SQLCipher can write wrong-key diagnostics directly to the process file
    descriptors, bypassing Python's ``redirect_stderr``.  Candidate keys are
    intentionally expected to fail validation, so those native diagnostics
    must never escape the private local probe or flood an automation log.
    """

    saved: dict[int, int] = {}
    null_fd: int | None = None
    try:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (AttributeError, OSError):
            pass
        null_fd = os.open(os.devnull, os.O_RDWR)
        for descriptor in (1, 2):
            saved[descriptor] = os.dup(descriptor)
            os.dup2(null_fd, descriptor)
        yield
    finally:
        for descriptor, duplicate in saved.items():
            try:
                os.dup2(duplicate, descriptor)
            finally:
                os.close(duplicate)
        if null_fd is not None:
            os.close(null_fd)


@contextmanager
def _exclusive_sync_lock():
    """Use an OS-held byte lock so a crash cannot leave a permanent lock."""

    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SyncFailure("sync_busy", "已有一次本地同步正在运行。") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SyncFailure("sync_busy", "已有一次本地同步正在运行。") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _run_account_bridge(timeout: int = 30) -> dict[str, Any]:
    if not BRIDGE_PATH.is_file() or not PACKAGE_ROOT.is_dir():
        raise SyncFailure("exporter_missing", "本地读取器尚未就绪。")
    try:
        completed = subprocess.run(
            ["node", str(BRIDGE_PATH), "accounts", str(PACKAGE_ROOT)],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncFailure("account_probe_failed", "无法完成微信账号只读检查。") from exc

    payload = None
    for line in completed.stdout.splitlines():
        if line.startswith(BRIDGE_MARKER):
            try:
                payload = json.loads(line[len(BRIDGE_MARKER):])
            except json.JSONDecodeError:
                payload = None
    if not isinstance(payload, dict) or not payload.get("ok"):
        code = payload.get("code") if isinstance(payload, dict) else None
        if code == "wechat_data_not_found":
            raise SyncFailure("wechat_data_not_found", "没有发现可读取的微信数据目录。")
        raise SyncFailure("account_probe_failed", "无法完成微信账号只读检查。")
    return payload


def _consume_private_key_file(path: Path, *, accept: bool) -> str | None:
    """Read a one-time bridge secret and best-effort erase its private file."""

    key = None
    byte_count = 0
    try:
        if path.is_symlink() or not path.is_file():
            return None
        byte_count = path.stat().st_size
        if accept and 1 <= byte_count <= 129:
            raw = path.read_bytes()
            try:
                candidate = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                candidate = ""
            if HEX_64_RE.fullmatch(candidate):
                key = candidate
    except OSError:
        key = None
    finally:
        try:
            if path.is_file() and not path.is_symlink():
                with path.open("r+b", buffering=0) as handle:
                    size = max(byte_count, os.fstat(handle.fileno()).st_size)
                    handle.seek(0)
                    handle.write(b"\0" * min(size, 4096))
                    handle.truncate(0)
                    os.fsync(handle.fileno())
        except OSError:
            pass
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            # The enclosing private temporary directory gets a second cleanup
            # attempt.  Never put a filesystem path or secret in diagnostics.
            pass
    return key


def _purge_stale_bridge_secrets(secret_root: Path) -> None:
    """Best-effort erase one-time key files left by an interrupted observer."""

    try:
        candidates = list(secret_root.iterdir())
    except OSError:
        return
    try:
        resolved_root = secret_root.resolve()
    except OSError:
        return
    for directory in candidates:
        try:
            contained = directory.resolve().parent == resolved_root
        except OSError:
            contained = False
        if not contained or directory.is_symlink() or not directory.is_dir():
            continue
        _consume_private_key_file(directory / "passphrase.txt", accept=False)
        try:
            directory.rmdir()
        except OSError:
            pass


def _capture_hook_key(
    pids: list[int],
    *,
    timeout_per_pid_seconds: int = 15,
) -> str | None:
    """Use the pinned observer with an authenticated in-memory key envelope.

    This is never enabled by the recurring database sync itself.  It exists
    only for a consented initialization flow around a fresh WeChat login.  The
    passphrase never enters argv, plaintext stdout/stderr, or the filesystem.
    """

    if not pids:
        return None
    timeout_per_pid_seconds = max(5, min(int(timeout_per_pid_seconds), 180))
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in SAFE_BRIDGE_ENV_KEYS
    }
    environment["SHE_LOVE_ME_HOOK_TIMEOUT_MS"] = str(
        timeout_per_pid_seconds * 1000
    )
    # Clean residues produced by versions that predate the in-memory envelope.
    _purge_stale_bridge_secrets(PRIVATE_ROOT / "bridge-secrets")
    wrapping_key = bytearray(os.urandom(32))
    environment["SHE_LOVE_ME_KEY_WRAP_KEY"] = bytes(wrapping_key).hex()
    try:
        completed = subprocess.run(
            [
                "node",
                str(BRIDGE_PATH),
                "capture-key",
                str(PACKAGE_ROOT),
                *(str(pid) for pid in pids),
            ],
            cwd=str(REPO_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20 + (timeout_per_pid_seconds + 1) * len(pids),
            check=False,
        )
        if completed.returncode != 0:
            return None
        payload = None
        for line in completed.stdout.splitlines():
            if not line.startswith(BRIDGE_MARKER):
                continue
            try:
                candidate = json.loads(line[len(BRIDGE_MARKER):])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
        envelope = payload.get("key_envelope") if isinstance(payload, dict) else None
        if not isinstance(envelope, dict) or not payload.get("ok"):
            return None
        if envelope.get("version") != 1 or envelope.get("algorithm") != "A256GCM":
            return None
        try:
            nonce = base64.b64decode(str(envelope.get("nonce") or ""), validate=True)
            ciphertext = base64.b64decode(
                str(envelope.get("ciphertext") or ""), validate=True
            )
            tag = base64.b64decode(str(envelope.get("tag") or ""), validate=True)
        except (ValueError, TypeError):
            return None
        if len(nonce) != 12 or len(tag) != 16 or len(ciphertext) != 64:
            return None
        try:
            from Crypto.Cipher import AES

            cipher = AES.new(bytes(wrapping_key), AES.MODE_GCM, nonce=nonce)
            cipher.update(b"she-love-me-wechat-key-v1")
            plaintext = bytearray(cipher.decrypt_and_verify(ciphertext, tag))
        except (ImportError, ValueError, KeyError):
            return None
        try:
            candidate = plaintext.decode("ascii")
            return candidate if HEX_64_RE.fullmatch(candidate) else None
        except UnicodeDecodeError:
            return None
        finally:
            plaintext[:] = b"\0" * len(plaintext)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        environment.pop("SHE_LOVE_ME_KEY_WRAP_KEY", None)
        wrapping_key[:] = b"\0" * len(wrapping_key)


def _account_hash(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def discover_unique_account() -> tuple[str, Path]:
    result = _run_account_bridge()
    accounts = result.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise SyncFailure("wechat_data_not_found", "没有发现可读取的微信账号。")
    if len(accounts) > 1:
        # Candidate details deliberately stay inside the private local file.
        choices = {
            "version": 1,
            "created_at": _iso(),
            "accounts": [
                {
                    "choice": index,
                    "account_hash": _account_hash(str(item.get("id") or "")),
                    "nickname": str(item.get("nickname") or ""),
                    "modified_time": item.get("modified_time"),
                }
                for index, item in enumerate(accounts, start=1)
                if isinstance(item, dict) and item.get("id")
            ],
        }
        _atomic_write_json(PRIVATE_ROOT / "account-choices.json", choices)
        raise SyncFailure(
            "needs_account_selection",
            "检测到多个微信账号；已停止，需先确认目标账号。",
        )
    account_id = str(accounts[0].get("id") or "")
    db_root = Path(str(result.get("db_path") or "")).resolve()
    if not account_id or not db_root.is_dir():
        raise SyncFailure("account_probe_failed", "微信账号检查结果不完整。")
    return account_id, db_root


def _protect_secret(secret: str) -> str:
    if sys.platform != "win32":
        raise SyncFailure("platform_unsupported", "自动同步目前仅支持 Windows。")
    data = secret.encode("utf-8")
    buffer = ctypes.create_string_buffer(data)
    incoming = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    ok = crypt32.CryptProtectData(
        ctypes.byref(incoming),
        "she-love-me local sync",
        None,
        None,
        None,
        0x01,
        ctypes.byref(outgoing),
    )
    if not ok:
        raise SyncFailure("secret_cache_failed", "无法创建本机加密密钥缓存。")
    try:
        protected = ctypes.string_at(outgoing.pbData, outgoing.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(outgoing.pbData)


def _unprotect_secret(protected: str) -> str:
    if sys.platform != "win32":
        raise SyncFailure("platform_unsupported", "自动同步目前仅支持 Windows。")
    try:
        data = base64.b64decode(protected, validate=True)
    except (ValueError, TypeError) as exc:
        raise SyncFailure("secret_cache_invalid", "本机密钥缓存无效。") from exc
    buffer = ctypes.create_string_buffer(data)
    incoming = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(incoming),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(outgoing),
    )
    if not ok:
        raise SyncFailure("secret_cache_invalid", "本机密钥缓存无法解锁。")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncFailure("secret_cache_invalid", "本机密钥缓存无效。") from exc
    finally:
        kernel32.LocalFree(outgoing.pbData)


def _load_nt_reader():
    if not NT_READER_PATH.is_file():
        raise SyncFailure("exporter_missing", "固定读取器缺少 NT 只读组件。")
    try:
        spec = importlib.util.spec_from_file_location(
            "she_love_me_pinned_nt_reader", NT_READER_PATH
        )
        if spec is None or spec.loader is None:
            raise ImportError("missing loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SystemExit) as exc:
        raise SyncFailure(
            "sync_dependencies_missing",
            "微信只读同步依赖尚未完整安装。",
        ) from exc


def _decode_process_name(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")


def _weixin_pids() -> list[int]:
    try:
        import pymem.process
    except ImportError as exc:
        raise SyncFailure("sync_dependencies_missing", "缺少本地进程读取依赖。") from exc
    result = []
    for item in pymem.process.list_processes():
        try:
            name = _decode_process_name(item.szExeFile).casefold()
            if name in {"weixin.exe", "wechat.exe"}:
                result.append(int(item.th32ProcessID))
        except (AttributeError, TypeError, ValueError):
            continue
    unique = sorted(set(result))
    visible = _visible_window_pids()
    return sorted(unique, key=lambda pid: (pid not in visible, pid))


def _visible_window_pids() -> set[int]:
    if sys.platform != "win32":
        return set()
    user32 = ctypes.windll.user32
    found: set[int] = set()
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(window, _parameter):
        if user32.IsWindowVisible(window):
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
            if process_id.value:
                found.add(int(process_id.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def _database_inventory(nt_reader, account_id: str) -> list[dict[str, Any]]:
    rows = nt_reader.find_nt_databases()
    if not isinstance(rows, list):
        return []
    selected = [
        dict(item)
        for item in rows
        if isinstance(item, dict) and str(item.get("wxid") or "") == account_id
    ]
    inventory_shards = {
        Path(str(item.get("path") or "")).resolve()
        for item in selected
        if re.fullmatch(
            r"message_\d+\.db",
            Path(str(item.get("path") or "")).name,
            re.IGNORECASE,
        )
    }
    message_directories = {
        Path(str(item.get("path") or "")).resolve().parent
        for item in selected
        if str(item.get("name") or "").startswith("message/")
        and str(item.get("path") or "")
    }
    if not message_directories:
        for item in selected:
            if str(item.get("name") or "") == "contact/contact.db":
                contact_path = Path(str(item.get("path") or "")).resolve()
                message_directories.add(contact_path.parent.parent / "message")
    try:
        filesystem_shards = {
            path.resolve()
            for directory in message_directories
            if directory.is_dir()
            for path in directory.iterdir()
            if path.is_file() and re.fullmatch(
                r"message_\d+\.db", path.name, re.IGNORECASE
            )
        }
    except OSError as exc:
        raise SyncFailure(
            "database_inventory_incomplete",
            "无法完整核对微信消息数据库分片；本次未导入。",
        ) from exc
    if not filesystem_shards.issubset(inventory_shards):
        raise SyncFailure(
            "database_inventory_incomplete",
            "微信消息数据库分片清单不完整；本次未导入。",
        )
    return selected


def _valid_key_record(item: dict[str, Any]) -> bool:
    return (
        isinstance(item, dict)
        and Path(str(item.get("path") or "")).is_file()
        and bool(HEX_64_RE.fullmatch(str(item.get("key") or "")))
        and bool(HEX_32_RE.fullmatch(str(item.get("salt") or "")))
    )


def _cache_records(account_id: str, inventory: list[dict[str, Any]]) -> dict[str, Any] | None:
    cache = _read_json(KEY_CACHE_PATH)
    if not isinstance(cache, dict) or cache.get("account_hash") != _account_hash(account_id):
        return None
    current_by_source = {
        (str(item.get("path") or ""), str(item.get("salt") or "")): item
        for item in inventory
    }
    records_by_source: dict[tuple[str, str], dict[str, Any]] = {}
    passphrase = None
    try:
        protected_passphrase = cache.get("protected_passphrase")
        if protected_passphrase:
            passphrase = _unprotect_secret(str(protected_passphrase))
            if not HEX_64_RE.fullmatch(passphrase):
                return None
        for item in cache.get("databases") or []:
            identity = (
                str(item.get("path") or ""),
                str(item.get("salt") or ""),
            )
            current_source = current_by_source.get(identity)
            if current_source is None:
                continue
            record = {
                "path": identity[0],
                "name": str(current_source.get("name") or ""),
                "salt": identity[1],
                "key": _unprotect_secret(str(item.get("protected_key") or "")),
            }
            if not _valid_key_record(record):
                return None
            records_by_source[identity] = record
    except SyncFailure:
        return None

    records = list(records_by_source.values())

    current_messages = {
        (str(item.get("path") or ""), str(item.get("salt") or ""))
        for item in inventory
        if str(item.get("name") or "").startswith("message/")
    }
    filtered_messages = {
        (item["path"], item["salt"])
        for item in records
        if item["name"].startswith("message/")
    }
    if (
        not current_messages
        or (current_messages != filtered_messages and passphrase is None)
    ):
        return None
    return {"databases": records, "passphrase": passphrase}


def has_usable_key_cache() -> bool:
    """Return whether the current sole account has a complete local DPAPI cache.

    This check reads only local account/database metadata and the protected cache;
    it never opens message tables or invokes the process observer.
    """

    try:
        account_id, _db_root = discover_unique_account()
        nt_reader = _load_nt_reader()
        inventory = _database_inventory(nt_reader, account_id)
        cached = _cache_records(account_id, inventory)
        if cached is None:
            return False
        current_sources = {
            (str(item.get("path") or ""), str(item.get("salt") or ""))
            for item in inventory
            if str(item.get("name") or "").startswith("message/")
        }
        cached_sources = {
            (str(item.get("path") or ""), str(item.get("salt") or ""))
            for item in cached.get("databases") or []
            if str(item.get("name") or "").startswith("message/")
        }
        return bool(current_sources) and current_sources == cached_sources
    except (OSError, SyncFailure):
        return False


def _save_key_cache(
    account_id: str,
    records: list[dict[str, Any]],
    *,
    passphrase: str | None = None,
) -> None:
    payload = {
        "version": 1,
        "updated_at": _iso(),
        "account_hash": _account_hash(account_id),
        "protection": "windows-dpapi-current-user",
        "databases": [
            {
                "path": str(item["path"]),
                "name": str(item["name"]),
                "salt": str(item["salt"]),
                "protected_key": _protect_secret(str(item["key"])),
            }
            for item in records
        ],
    }
    if passphrase:
        if not HEX_64_RE.fullmatch(passphrase):
            raise SyncFailure("secret_cache_invalid", "本机密钥格式无效。")
        payload["protected_passphrase"] = _protect_secret(passphrase)
    _atomic_write_json(KEY_CACHE_PATH, payload)


def _derive_database_key(passphrase: str, salt: str) -> str:
    """Derive the per-database raw key used by recent WeChat 4.x builds."""

    if not HEX_64_RE.fullmatch(passphrase) or not HEX_32_RE.fullmatch(salt):
        raise SyncFailure("secret_cache_invalid", "本机密钥格式无效。")
    return hashlib.pbkdf2_hmac(
        "sha512",
        bytes.fromhex(passphrase),
        bytes.fromhex(salt),
        256_000,
        dklen=32,
    ).hex()


def _validate_raw_key(
    nt_reader,
    source: dict[str, Any],
    salt: str,
    raw_key: str,
) -> bool:
    candidate = {
        "path": str(source.get("path") or ""),
        "name": str(source.get("name") or ""),
        "salt": salt,
        "key": raw_key,
    }
    connection = None
    try:
        connection = _open_readonly_database(nt_reader, candidate)
        return True
    except SyncFailure:
        return False
    finally:
        if connection is not None:
            connection.close()


def _apply_passphrase(
    nt_reader,
    passphrase: str,
    inventory: list[dict[str, Any]],
    keys_by_salt: dict[str, str],
) -> bool:
    """Derive and validate every discoverable key without exposing secrets."""

    validated: dict[str, str] = {}
    for item in inventory:
        salt = str(item.get("salt") or "")
        if not HEX_32_RE.fullmatch(salt):
            continue
        raw_key = _derive_database_key(passphrase, salt)
        if _validate_raw_key(nt_reader, item, salt, raw_key):
            validated[salt] = raw_key
    if not validated:
        return False
    keys_by_salt.update(validated)
    return True


def _scan_database_keys(
    nt_reader,
    account_id: str,
    inventory: list[dict[str, Any]],
    *,
    allow_process_hook: bool = False,
) -> list[dict[str, Any]]:
    message_rows = [
        item for item in inventory
        if str(item.get("name") or "").startswith("message/")
    ]
    if not message_rows:
        raise SyncFailure("wechat_database_not_found", "没有发现微信消息数据库。")
    pids = _weixin_pids()
    if not pids:
        raise SyncFailure("wechat_not_running", "微信未运行或尚未登录。")

    required_salts = {str(item.get("salt") or "") for item in message_rows}
    keys_by_salt: dict[str, str] = {}
    discovered_passphrase: str | None = None
    for pid in pids:
        try:
            found = nt_reader.scan_memory_keys(pid)
        except (OSError, RuntimeError):
            continue
        for item in found or []:
            salt = str(item.get("salt") or "")
            key = str(item.get("key") or "")
            if HEX_32_RE.fullmatch(salt) and HEX_64_RE.fullmatch(key):
                keys_by_salt[salt] = key
        if required_salts.issubset(keys_by_salt):
            break

    if not required_salts.issubset(keys_by_salt):
        binary_candidates = _scan_binary_key_candidates(nt_reader, pids, required_salts)
        database_by_salt = {
            str(item.get("salt") or ""): item
            for item in inventory
            if str(item.get("name") or "").startswith("message/")
        }
        for salt in required_salts - keys_by_salt.keys():
            source = database_by_salt.get(salt)
            if source is None:
                continue
            for candidate_key in sorted(binary_candidates.get(salt, ()))[:
                MAX_BINARY_CANDIDATES_PER_SALT
            ]:
                candidate = {
                    "path": str(source.get("path") or ""),
                    "name": str(source.get("name") or ""),
                    "salt": salt,
                    "key": candidate_key,
                }
                connection = None
                try:
                    connection = _open_readonly_database(nt_reader, candidate)
                    keys_by_salt[salt] = candidate_key
                    break
                except SyncFailure:
                    pass
                finally:
                    if connection is not None:
                        connection.close()

        # WeChat commonly uses one raw key with a different 16-byte salt for
        # each database file.  Reuse only keys already validated against a
        # sibling database, and still validate every additional file in
        # read-only mode before accepting it.
        validated_keys = set(keys_by_salt.values())
        for salt in required_salts - keys_by_salt.keys():
            source = database_by_salt.get(salt)
            if source is None:
                continue
            for candidate_key in validated_keys:
                candidate = {
                    "path": str(source.get("path") or ""),
                    "name": str(source.get("name") or ""),
                    "salt": salt,
                    "key": candidate_key,
                }
                connection = None
                try:
                    connection = _open_readonly_database(nt_reader, candidate)
                    keys_by_salt[salt] = candidate_key
                    break
                except SyncFailure:
                    pass
                finally:
                    if connection is not None:
                        connection.close()

        # WeChat 4.1.12.55 and later no longer retain the old
        # x'<raw-key><salt>' SQL text.  If a read-only memory candidate is the
        # account-wide passphrase, derive a per-database raw key and validate
        # it against a real message database before applying it to the rest.
        if not required_salts.issubset(keys_by_salt):
            primary = next(
                (
                    item for item in sorted(
                        message_rows,
                        key=lambda row: str(row.get("name") or ""),
                    )
                    if HEX_32_RE.fullmatch(str(item.get("salt") or ""))
                ),
                None,
            )
            passphrase_candidates = sorted(
                set().union(*(values for values in binary_candidates.values()))
            )[:MAX_PASSPHRASE_CANDIDATES]
            if primary is not None:
                primary_salt = str(primary.get("salt") or "")
                validation_deadline = (
                    time.monotonic() + MAX_PASSPHRASE_VALIDATION_SECONDS
                )
                for passphrase in passphrase_candidates:
                    if time.monotonic() >= validation_deadline:
                        break
                    derived = _derive_database_key(passphrase, primary_salt)
                    if not _validate_raw_key(
                        nt_reader,
                        primary,
                        primary_salt,
                        derived,
                    ):
                        continue
                    _apply_passphrase(
                        nt_reader,
                        passphrase,
                        inventory,
                        keys_by_salt,
                    )
                    discovered_passphrase = passphrase
                    break

    # Some WeChat builds do not retain the SQL text pattern searched above.
    # The pinned package also ships an explicitly opt-in login-time observer
    # which briefly injects into the WeChat process.  It is disabled for every
    # recurring sync.  Test its candidate using read-only database connections
    # and never expose it in a command line.
    if allow_process_hook and not required_salts.issubset(keys_by_salt):
        hook_key = _capture_hook_key(pids)
        if hook_key:
            # Current WeChat exposes an account-wide passphrase at database
            # initialization time.  Recent builds derive a different raw key
            # for each database salt; older builds are still covered by the
            # direct raw-key validation below.
            if _apply_passphrase(nt_reader, hook_key, inventory, keys_by_salt):
                discovered_passphrase = hook_key
            for item in inventory:
                salt = str(item.get("salt") or "")
                if salt in keys_by_salt or not HEX_32_RE.fullmatch(salt):
                    continue
                candidate = {
                    "path": str(item.get("path") or ""),
                    "name": str(item.get("name") or ""),
                    "salt": salt,
                    "key": hook_key,
                }
                connection = None
                try:
                    connection = _open_readonly_database(nt_reader, candidate)
                    keys_by_salt[salt] = hook_key
                except SyncFailure:
                    pass
                finally:
                    if connection is not None:
                        connection.close()

    records = []
    for item in inventory:
        salt = str(item.get("salt") or "")
        key = keys_by_salt.get(salt)
        if not key:
            continue
        record = {
            "path": str(item.get("path") or ""),
            "name": str(item.get("name") or ""),
            "salt": salt,
            "key": key,
        }
        if _valid_key_record(record):
            records.append(record)
    matched_message_paths = {
        item["path"] for item in records if item["name"].startswith("message/")
    }
    expected_message_paths = {str(item.get("path") or "") for item in message_rows}
    if not expected_message_paths.issubset(matched_message_paths):
        raise SyncFailure(
            "database_key_unavailable",
            "未能只读解锁全部微信消息数据库；未导入任何会话。",
        )
    _save_key_cache(
        account_id,
        records,
        passphrase=discovered_passphrase,
    )
    return records


def _scan_binary_key_candidates(
    nt_reader, pids: list[int], salts: set[str]
) -> dict[str, set[str]]:
    """Find possible key bytes adjacent to known DB salts in readable memory.

    SQLCipher may discard its original PRAGMA text while retaining a 32-byte
    key next to the 16-byte database salt.  Candidates are never printed or
    persisted unless a read-only database open validates them.
    """

    results: dict[str, set[str]] = {salt: set() for salt in salts}
    if sys.platform != "win32" or not salts:
        return results
    patterns = {
        salt: {
            "raw": bytes.fromhex(salt),
            "ascii": salt.encode("ascii"),
            "utf16": salt.encode("utf-16le"),
        }
        for salt in salts
        if HEX_32_RE.fullmatch(salt)
    }
    kernel32 = ctypes.windll.kernel32
    overlap_size = 256
    deadline = time.monotonic() + MAX_BINARY_SCAN_SECONDS
    scanned_bytes = 0

    def budget_exhausted() -> bool:
        return (
            scanned_bytes >= MAX_BINARY_SCAN_BYTES
            or time.monotonic() >= deadline
            or sum(len(values) for values in results.values())
            >= MAX_BINARY_CANDIDATES_TOTAL
        )

    def add_candidate(salt: str, value: bytes, *, encoded_hex: bool = False):
        if len(results[salt]) >= MAX_BINARY_CANDIDATES_PER_SALT:
            return
        try:
            text = value.decode("ascii") if encoded_hex else value.hex()
        except UnicodeDecodeError:
            return
        if HEX_64_RE.fullmatch(text) and int(text, 16) != 0:
            results[salt].add(text.lower())

    for pid in pids:
        if budget_exhausted():
            break
        process = kernel32.OpenProcess(
            nt_reader.PROCESS_VM_READ | nt_reader.PROCESS_QUERY_INFORMATION,
            False,
            pid,
        )
        if not process:
            continue
        try:
            address = 0x10000
            while address < 0x7FFFFFFFFFFF and not budget_exhausted():
                info = nt_reader.MEMORY_BASIC_INFORMATION()
                queried = nt_reader.VirtualQueryEx(
                    process,
                    ctypes.c_void_p(address),
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
                if queried == 0:
                    break
                region_address = int(info.BaseAddress or 0)
                region_size = int(info.RegionSize or 0)
                if (
                    info.State == nt_reader.MEM_COMMIT
                    and 256 < region_size < 200 * 1024 * 1024
                    and info.Protect not in (
                        0,
                        nt_reader.PAGE_NOACCESS,
                        nt_reader.PAGE_GUARD,
                    )
                ):
                    position = region_address
                    end = region_address + region_size
                    tail = b""
                    while position < end and not budget_exhausted():
                        chunk_size = min(
                            64 * 1024,
                            end - position,
                            MAX_BINARY_SCAN_BYTES - scanned_bytes,
                        )
                        if chunk_size <= 0:
                            break
                        buffer = ctypes.create_string_buffer(chunk_size)
                        bytes_read = ctypes.c_size_t(0)
                        ok = nt_reader.ReadProcessMemory(
                            process,
                            ctypes.c_void_p(position),
                            buffer,
                            chunk_size,
                            ctypes.byref(bytes_read),
                        )
                        if ok and bytes_read.value:
                            scanned_bytes += int(bytes_read.value)
                            data = tail + buffer.raw[:bytes_read.value]
                            for salt, variants in patterns.items():
                                if (
                                    len(results[salt])
                                    >= MAX_BINARY_CANDIDATES_PER_SALT
                                ):
                                    continue
                                raw = variants["raw"]
                                start = 0
                                while True:
                                    index = data.find(raw, start)
                                    if index < 0:
                                        break
                                    if index >= 32:
                                        add_candidate(salt, data[index - 32:index])
                                    if index + len(raw) + 32 <= len(data):
                                        add_candidate(
                                            salt,
                                            data[index + len(raw):index + len(raw) + 32],
                                        )
                                    start = index + 1

                                ascii_salt = variants["ascii"]
                                start = 0
                                while True:
                                    index = data.find(ascii_salt, start)
                                    if index < 0:
                                        break
                                    if index >= 64:
                                        add_candidate(
                                            salt,
                                            data[index - 64:index],
                                            encoded_hex=True,
                                        )
                                    start = index + 1

                                utf16_salt = variants["utf16"]
                                start = 0
                                while True:
                                    index = data.find(utf16_salt, start)
                                    if index < 0:
                                        break
                                    if index >= 128:
                                        prefix = data[index - 128:index]
                                        try:
                                            decoded = prefix.decode("utf-16le").encode("ascii")
                                        except (UnicodeDecodeError, UnicodeEncodeError):
                                            decoded = b""
                                        add_candidate(salt, decoded, encoded_hex=True)
                                    start = index + 2
                            tail = data[-overlap_size:]
                            if budget_exhausted() or all(
                                len(results[salt])
                                >= MAX_BINARY_CANDIDATES_PER_SALT
                                for salt in patterns
                            ):
                                return results
                        position += chunk_size
                address = region_address + max(region_size, 0x1000)
        finally:
            kernel32.CloseHandle(process)
    return results


def _database_records(
    account_id: str,
    *,
    rescan: bool = False,
    allow_process_hook: bool = False,
    cache_only: bool = False,
):
    nt_reader = _load_nt_reader()
    inventory = _database_inventory(nt_reader, account_id)
    active_snapshots = _ACTIVE_DATABASE_SNAPSHOTS.get()
    if active_snapshots is not None:
        # Inventory remains double-checked, but do not eagerly copy every
        # historical shard.  Incremental sync snapshots only changed sources;
        # key validation also obtains a private snapshot lazily when needed.
        verified_inventory = _database_inventory(nt_reader, account_id)
        before_sources = {
            (str(item.get("path") or ""), str(item.get("salt") or ""))
            for item in inventory
            if str(item.get("name") or "").startswith("message/")
        }
        after_sources = {
            (str(item.get("path") or ""), str(item.get("salt") or ""))
            for item in verified_inventory
            if str(item.get("name") or "").startswith("message/")
        }
        if before_sources != after_sources:
            raise SyncFailure(
                "database_inventory_changed",
                "微信消息数据库分片在快照期间发生变化；本次未导入。",
            )
    cached = None if rescan else _cache_records(account_id, inventory)
    records = cached.get("databases") if cached else None
    passphrase = cached.get("passphrase") if cached else None
    if passphrase:
        message_inventory = [
            item
            for item in inventory
            if str(item.get("name") or "").startswith("message/")
        ]
        cached_sources = {
            (str(item.get("path") or ""), str(item.get("salt") or ""))
            for item in records
            if str(item.get("name") or "").startswith("message/")
        }
        current_sources = {
            (str(item.get("path") or ""), str(item.get("salt") or ""))
            for item in message_inventory
        }
        if current_sources != cached_sources:
            keys_by_salt = {
                str(item.get("salt") or ""): str(item.get("key") or "")
                for item in records
                if _valid_key_record(item)
            }
            _apply_passphrase(nt_reader, passphrase, inventory, keys_by_salt)
            refreshed = []
            for item in inventory:
                salt = str(item.get("salt") or "")
                key = keys_by_salt.get(salt)
                if not key:
                    continue
                record = {
                    "path": str(item.get("path") or ""),
                    "name": str(item.get("name") or ""),
                    "salt": salt,
                    "key": key,
                }
                source_identity = (record["path"], record["salt"])
                if (
                    record["name"].startswith("message/")
                    and source_identity not in cached_sources
                    and not _validate_raw_key(nt_reader, item, salt, key)
                ):
                    continue
                if _valid_key_record(record):
                    refreshed.append(record)
            refreshed_sources = {
                (str(item.get("path") or ""), str(item.get("salt") or ""))
                for item in refreshed
                if str(item.get("name") or "").startswith("message/")
            }
            if not current_sources.issubset(refreshed_sources):
                raise SyncFailure(
                    "database_key_unavailable",
                    "新增或已轮换的微信消息数据库未能只读验证；本次未导入。",
                )
            records = refreshed
            _save_key_cache(
                account_id,
                records,
                passphrase=passphrase,
            )
    if not records:
        if cache_only:
            raise SyncFailure(
                "database_key_unavailable",
                "本机密钥缓存尚未就绪；等待一次性初始化。",
            )
        records = _scan_database_keys(
            nt_reader,
            account_id,
            inventory,
            allow_process_hook=allow_process_hook,
        )
    return nt_reader, records


def cache_captured_passphrase(
    passphrase: str,
    *,
    expected_account_id: str | None = None,
) -> None:
    """Validate and DPAPI-cache a login-time passphrase without exposing it."""

    if not HEX_64_RE.fullmatch(passphrase):
        raise SyncFailure("secret_cache_invalid", "捕获到的本机密钥格式无效。")
    with _exclusive_sync_lock(), _database_snapshot_scope():
        account_id, _db_root = discover_unique_account()
        if expected_account_id is not None and account_id != expected_account_id:
            raise SyncFailure(
                "needs_account_selection",
                "微信账号在初始化期间发生变化；已停止，需先确认目标账号。",
            )
        nt_reader = _load_nt_reader()
        inventory = _database_inventory(nt_reader, account_id)
        message_rows = [
            item
            for item in inventory
            if str(item.get("name") or "").startswith("message/")
        ]
        if not message_rows:
            raise SyncFailure("wechat_database_not_found", "没有发现微信消息数据库。")
        keys_by_salt: dict[str, str] = {}
        if not _apply_passphrase(nt_reader, passphrase, inventory, keys_by_salt):
            raise SyncFailure("database_key_unavailable", "捕获到的密钥无法验证。")
        records = []
        for item in inventory:
            salt = str(item.get("salt") or "")
            key = keys_by_salt.get(salt)
            if not key:
                continue
            record = {
                "path": str(item.get("path") or ""),
                "name": str(item.get("name") or ""),
                "salt": salt,
                "key": key,
            }
            if _valid_key_record(record):
                records.append(record)
        expected_paths = {str(item.get("path") or "") for item in message_rows}
        validated_paths = {
            str(item.get("path") or "")
            for item in records
            if str(item.get("name") or "").startswith("message/")
        }
        if not expected_paths.issubset(validated_paths):
            raise SyncFailure("database_key_unavailable", "未能验证全部微信消息数据库。")
        _save_key_cache(
            account_id,
            records,
            passphrase=passphrase,
        )


def _open_readonly_database(nt_reader, record: dict[str, Any]):
    key = str(record.get("key") or "")
    salt = str(record.get("salt") or "")
    if not HEX_64_RE.fullmatch(key) or not HEX_32_RE.fullmatch(salt):
        raise SyncFailure("secret_cache_invalid", "本机密钥缓存无效。")
    source_path = Path(str(record.get("path") or "")).resolve()
    if not source_path.is_file():
        raise SyncFailure("wechat_database_not_found", "微信数据库路径已变化。")
    snapshots = _ACTIVE_DATABASE_SNAPSHOTS.get()
    owns_snapshots = snapshots is None
    if snapshots is None:
        snapshots = _DatabaseSnapshotSet()
    connection = None
    try:
        snapshot_path = snapshots.path_for(source_path)
        with _silence_native_output():
            connection = nt_reader.sqlcipher.connect(
                snapshot_path.as_uri() + "?mode=ro",
                uri=True,
            )
            cursor = connection.cursor()
            cursor.execute(f'PRAGMA key = "x\'{key}{salt}\'";')
            cursor.execute("PRAGMA query_only = ON;")
            cursor.execute("PRAGMA temp_store = MEMORY;")
            cursor.execute("SELECT count(*) FROM sqlite_master")
            cursor.fetchone()
        return _SnapshotConnection(
            connection,
            snapshots.close if owns_snapshots else None,
        )
    except SyncFailure:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if owns_snapshots:
            snapshots.close()
        raise
    except Exception as exc:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if owns_snapshots:
            snapshots.close()
        raise SyncFailure("database_unavailable", "微信数据库只读连接失败。") from exc


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_self_sender(sender: str, own_wxid: str) -> bool:
    if not sender or not own_wxid:
        return False
    if sender == own_wxid:
        return True
    parts = own_wxid.rsplit("_", 1)
    return bool(
        len(parts) == 2
        and len(parts[1]) == 4
        and parts[1].isalnum()
        and sender == parts[0]
    )


def _load_contact_names(nt_reader, records: list[dict[str, Any]]) -> dict[str, str]:
    contact = next(
        (item for item in records if item.get("name") == "contact/contact.db"),
        None,
    )
    if contact is None:
        return {}
    connection = None
    try:
        connection = _open_readonly_database(nt_reader, contact)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT username, COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), "
            "NULLIF(alias,''), username) FROM contact"
        )
        return {
            _text(username): _text(display)
            for username, display in cursor.fetchall()
            if _text(username)
        }
    except (SyncFailure, Exception):
        return {}
    finally:
        if connection is not None:
            connection.close()


@contextmanager
def _message_databases(nt_reader, records: list[dict[str, Any]], cached_contexts=None):
    contexts = []
    for record in records:
        if not str(record.get("name") or "").startswith("message/"):
            continue
        cached = (cached_contexts or {}).get(_source_partition_token(record))
        if cached is not None:
            contexts.append({
                "record": record,
                "sender_map": {int(key): value for key, value in cached["sender_map"].items()},
                "name_rows": cached["name_rows"],
                "message_tables": set(cached["message_tables"]),
                "source_unchanged": True,
            })
            continue
        connection = _open_readonly_database(nt_reader, record)
        try:
            context = {
                "record": record,
                "sender_map": {},
                "sessions": set(),
                "message_tables": set(),
            }
            cursor = connection.cursor()
            # This folder also contains FTS, resource, media and revoke stores.
            # They do not all have the same mapping schema as message shards.
            cursor.execute("PRAGMA table_info(Name2Id)")
            mapping_columns = {_text(row[1]) for row in cursor.fetchall()}
            mapping_column = next((name for name in ("user_name", "username") if name in mapping_columns), None)
            rows = []
            if mapping_column:
                cursor.execute(f'SELECT rowid, "{mapping_column}" FROM Name2Id')
                rows = cursor.fetchall()
            sender_map = {int(rowid): _text(username) for rowid, username in rows}
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            )
            message_tables = {
                "Msg_" + _text(row[0])[4:].lower()
                for row in cursor.fetchall()
                if row and MESSAGE_TABLE_RE.fullmatch(_text(row[0]))
            }
            context.update({
                "sender_map": sender_map,
                "name_rows": rows,
                "message_tables": message_tables,
            })
            contexts.append(context)
        finally:
            connection.close()

    # Name2Id may be authoritative across message partitions.  Resolve every
    # real Msg_* table against the union instead of requiring its talker row to
    # live in the same shard.  Rows without a table are sender mappings only.
    table_candidates: dict[str, set[str]] = {}
    for context in contexts:
        for _rowid, username in context.get("name_rows", []):
            talker = _text(username)
            if not talker:
                continue
            table = f"Msg_{hashlib.md5(talker.encode('utf-8')).hexdigest()}"
            table_candidates.setdefault(table, set()).add(talker)

    for context in contexts:
        sessions: set[str] = set()
        for table in context["message_tables"]:
            candidates = table_candidates.get(table, set())
            if len(candidates) != 1:
                raise SyncFailure(
                    "message_index_incomplete",
                    "发现无法安全映射到会话的微信消息表；本次未导入。",
                )
            sessions.add(next(iter(candidates)))
        context["sessions"] = sessions
        # Keep these local-only rows in the account-bound checkpoint.  A
        # changed shard may refer to a mapping that lives in an unchanged one.

    # Compatibility context manager: enumeration connections are already
    # closed before yielding.  Message reads below reopen one shard at a time.
    try:
        yield contexts
    finally:
        contexts.clear()


def _source_partition_token(record: dict[str, Any]) -> str:
    partition_identity = "\0".join((
        str(Path(str(record.get("path") or "")).resolve()).casefold(),
        str(record.get("salt") or "").casefold(),
    ))
    return "p_" + hashlib.sha256(
        partition_identity.encode("utf-8")
    ).hexdigest()[:16]


def _decode_message_field(value) -> tuple[str, bool]:
    """Decode one local message field with bounded Zstandard expansion.

    The caller retains original bytes in the immutable export.  A failed
    decode must be reported instead of silently turning binary text into
    replacement characters or an ordinary empty-message placeholder.
    """

    if value is None or value == "" or value == b"":
        return "", False
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return str(value), False
    raw = bytes(value)
    try:
        if raw.startswith(ZSTD_FRAME_MAGIC):
            import zstandard

            frame_size = zstandard.frame_content_size(raw)
            if (
                frame_size not in (
                    zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR
                )
                and frame_size > MAX_MESSAGE_CONTENT_BYTES
            ):
                return "", True
            decoder = zstandard.ZstdDecompressor(
                max_window_size=max(1024, MAX_MESSAGE_CONTENT_BYTES // 1024)
            )
            decoded = decoder.decompress(
                raw,
                max_output_size=MAX_MESSAGE_CONTENT_BYTES,
                allow_extra_data=False,
            )
            if len(decoded) > MAX_MESSAGE_CONTENT_BYTES:
                return "", True
        else:
            decoded = raw
        return decoded.decode("utf-8"), False
    except (ImportError, UnicodeDecodeError, ValueError, OSError):
        return "", True
    except Exception:
        # Native Zstd errors expose only a failure flag; raw bytes stay local.
        return "", True


def _decode_message_content(message_content, compress_content) -> tuple[str, bool]:
    compressed_text, compressed_failed = _decode_message_field(compress_content)
    if compressed_text:
        return compressed_text, compressed_failed
    message_text, message_failed = _decode_message_field(message_content)
    failed = compressed_failed or message_failed
    if message_text:
        return message_text, failed
    return ("[正文解码失败，原始数据已保留]" if failed else ""), failed


def _query_messages(
    nt_reader,
    context: dict[str, Any],
    talker: str,
    own_wxid: str,
):
    table_hash = hashlib.md5(talker.encode("utf-8")).hexdigest()
    table = f"Msg_{table_hash}"
    if table not in context.get("message_tables", set()):
        return []
    messages = []
    partition_token = _source_partition_token(context["record"])
    sender_map = context["sender_map"]
    connection = _open_readonly_database(nt_reader, context["record"])
    try:
        cursor = connection.cursor()
        cursor.execute(
            f'''SELECT local_id, server_id, local_type, sort_seq, real_sender_id,
                       create_time, status, upload_status, download_status,
                       server_seq, origin_source, source, message_content, compress_content
                FROM "{table}" ORDER BY create_time ASC, local_id ASC'''
        )
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                sender = sender_map.get(int(row[4] or 0), "")
                content, content_decode_failed = _decode_message_content(
                    row[12], row[13]
                )
                source = _text(row[11])
                origin = _text(row[10])
                full_content = content or source or origin
                item = {
                    "localId": row[0] or 0,
                    "serverId": _text(row[1]),
                    "localType": row[2] or 0,
                    "sortSeq": row[3] or 0,
                    "createTime": row[5] or 0,
                    "isSend": 1 if _is_self_sender(sender, own_wxid) else 0,
                    "senderUsername": sender,
                    "content": full_content,
                    "rawContent": full_content,
                    "parsedContent": full_content,
                    "status": row[6] or 0,
                    "uploadStatus": row[7] or 0,
                    "downloadStatus": row[8] or 0,
                    "serverSeq": row[9] or 0,
                    # Opaque and non-identifying.  It prevents localId
                    # collisions between partitions while server IDs remain
                    # globally stable across them.
                    "sourcePartition": partition_token,
                }
                if isinstance(row[12], (bytes, bytearray, memoryview)) and row[12]:
                    item["messageContentBase64"] = base64.b64encode(
                        bytes(row[12])
                    ).decode("ascii")
                elif row[12] is not None:
                    item["messageContentRaw"] = _text(row[12])
                compressed = row[13]
                if isinstance(compressed, (bytes, bytearray, memoryview)) and compressed:
                    item["compressContentBase64"] = base64.b64encode(
                        bytes(compressed)
                    ).decode("ascii")
                elif compressed is not None:
                    item["compressContentRaw"] = _text(compressed)
                if content_decode_failed:
                    item["contentDecodeFailed"] = True
                messages.append(item)
    finally:
        connection.close()
    return messages


def _existing_bundle_path(contact_id: str, display_name: str) -> Path:
    suffix = hashlib.md5(contact_id.encode("utf-8")).hexdigest()[:8]
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    matches = [
        path for path in CONTACTS_DIR.glob(f"*__{suffix}")
        if path.is_dir() and path.parent.resolve() == CONTACTS_DIR.resolve()
    ]
    if len(matches) == 1:
        return matches[0]
    bundle = resolve_bundle_paths(
        display_name,
        contact_id,
        output_dir=str(CONTACTS_DIR),
    )
    return Path(bundle["bundle_dir"])


def _run_stats(messages_path: Path, bundle_dir: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "stats_analyzer.py"),
                "--input",
                str(messages_path),
                "--output",
                str(bundle_dir / "stats.json"),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=180,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _safe_session_id(value: str) -> bool:
    return bool(SESSION_ID_RE.fullmatch(value)) and ".." not in value


def _session_snapshot_path(run_root: Path, talker: str) -> Path:
    token = hashlib.sha256(talker.encode("utf-8")).hexdigest()[:24]
    return run_root / "sessions" / token / "messages.json"


def _safe_fallback_name(talker: str) -> str:
    token = hashlib.sha256(talker.encode("utf-8")).hexdigest()[:8]
    return f"微信会话 {token}"


def _checkpoint_path() -> Path:
    return PRIVATE_ROOT / "incremental-checkpoint.json"


def _load_incremental_checkpoint(account_id: str) -> dict[str, Any]:
    value = _read_json(_checkpoint_path(), {})
    if not isinstance(value, dict) or value.get("version") != 1:
        return {}
    if value.get("account_hash") != _account_hash(account_id):
        return {}
    if not all(isinstance(value.get(key), dict) for key in ("sources", "bundles", "contact_names")):
        return {}
    return value


def _cached_context_valid(value) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("sender_map"), dict) or not isinstance(value.get("name_rows"), list):
        return False
    if not isinstance(value.get("message_tables"), list):
        return False
    return (
        all(str(key).isdigit() and isinstance(name, str) for key, name in value["sender_map"].items())
        and all(isinstance(row, (list, tuple)) and len(row) == 2 and isinstance(row[1], str)
                for row in value["name_rows"])
        and all(isinstance(table, str) and MESSAGE_TABLE_RE.fullmatch(table)
                for table in value["message_tables"])
    )


def _bundle_checkpoint(bundle_dir: Path, display_name: str) -> dict[str, Any]:
    return {
        "bundle_name": bundle_dir.name,
        "display_name": display_name,
        "files": {
            name: list(signature) if signature is not None else None
            for name in ("messages.json", "stats.json", "dashboard_manifest.json")
            for signature in [_source_file_signature(bundle_dir / name)]
        },
    }


def _bundle_checkpoint_matches(value) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("bundle_name"), str):
        return False
    bundle = CONTACTS_DIR / value["bundle_name"]
    try:
        if bundle.resolve().parent != CONTACTS_DIR.resolve() or bundle.is_symlink():
            return False
        files = value.get("files")
        if not isinstance(files, dict):
            return False
        return all(
            files.get(name) is not None
            and list(_source_file_signature(bundle / name) or ()) == files[name]
            for name in ("messages.json", "stats.json", "dashboard_manifest.json")
        )
    except OSError:
        return False


def _messages_fingerprint(messages: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in messages:
        # Include all source fields and raw bytes, not only max time / rowid.
        # This notices historical inserts, in-place edits and decode changes.
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _sync_sessions(
    nt_reader,
    records: list[dict[str, Any]],
    account_id: str,
    run_root: Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    full = status.get("sync_strategy") == "full-reconcile"
    checkpoint = {} if full else _load_incremental_checkpoint(account_id)
    prior_sources = checkpoint.get("sources", {})
    fingerprints = {}
    cached_contexts = {}
    for record in records:
        if not str(record.get("name") or "").startswith(("message/", "contact/")):
            continue
        token = _source_partition_token(record)
        fingerprints[token] = _stable_database_fingerprint(Path(record["path"]))
        prior = prior_sources.get(token, {})
        if (prior.get("fingerprint") == fingerprints[token]
                and _cached_context_valid(prior.get("context"))):
            cached_contexts[token] = prior["context"]

    contact_record = next((item for item in records if item.get("name") == "contact/contact.db"), None)
    contact_token = _source_partition_token(contact_record) if contact_record else None
    cached_names = checkpoint.get("contact_names", {})
    if (contact_token and cached_names and prior_sources.get(contact_token, {}).get("fingerprint")
            == fingerprints.get(contact_token)):
        contact_names = cached_names
    else:
        contact_names = _load_contact_names(nt_reader, records)
    next_checkpoint: dict[str, Any] = {
        "version": 1,
        "account_hash": _account_hash(account_id),
        "sources": {},
        "bundles": {},
        "contact_names": contact_names,
    }
    private_manifest: dict[str, Any] = {
        "version": 2,
        "run_id": run_root.name,
        "started_at": status["last_run_at"],
        "account_hash": _account_hash(account_id),
        "source_read_only": True,
        "sync_strategy": status.get("sync_strategy", "changed-shards"),
        "incremental_partition_snapshots": True,
        "sessions": [],
    }

    # _message_databases is a generator so every connection is closed even if
    # one conversation fails.
    with _message_databases(nt_reader, records, cached_contexts=cached_contexts) as contexts:
        status["skipped_databases"] = sum(bool(item.get("source_unchanged")) for item in contexts)
        status["processed_databases"] = len(contexts) - status["skipped_databases"]
        for context in contexts:
            token = _source_partition_token(context["record"])
            context["checkpoint_sessions"] = {}
            next_checkpoint["sources"][token] = {
                "fingerprint": fingerprints[token],
                "context": {
                    "sender_map": context["sender_map"],
                    "name_rows": context["name_rows"],
                    "message_tables": sorted(context["message_tables"]),
                },
                "sessions": context["checkpoint_sessions"],
            }
        sessions = sorted(set().union(*(item["sessions"] for item in contexts)))
        status["scanned_conversations"] = len(sessions)
        _write_status(status)

        for talker in sessions:
            row = {"session_id": talker, "state": "pending"}
            private_manifest["sessions"].append(row)
            try:
                if not _safe_session_id(talker):
                    raise ValueError("unsafe_session_id")
                prior_bundle = checkpoint.get("bundles", {}).get(talker)
                repair = not _bundle_checkpoint_matches(prior_bundle)
                raw_messages = []
                source_partitions = []
                skipped_messages = 0
                for context in contexts:
                    if talker in context["sessions"]:
                        token = _source_partition_token(context["record"])
                        previous = prior_sources.get(token, {}).get("sessions", {}).get(talker)
                        if context.get("source_unchanged") and previous and not repair:
                            context["checkpoint_sessions"][talker] = previous
                            skipped_messages += int(previous.get("count", 0))
                            continue
                        if context.get("source_unchanged") and not context.get("repair_queried"):
                            context["repair_queried"] = True
                            status["skipped_databases"] -= 1
                            status["processed_databases"] += 1
                        queried = _query_messages(nt_reader, context, talker, account_id)
                        status["read_messages"] += len(queried)
                        current = {"sha256": _messages_fingerprint(queried), "count": len(queried)}
                        context["checkpoint_sessions"][talker] = current
                        if current == previous and not repair:
                            skipped_messages += len(queried)
                            continue
                        raw_messages.extend(queried)
                        source_partitions.append(token)

                display_name = contact_names.get(talker) or _safe_fallback_name(talker)
                if not source_partitions and not repair:
                    status["skipped_conversations"] += 1
                    status["unchanged_messages"] += skipped_messages
                    row["state"] = "unchanged"
                    bundle_dir = CONTACTS_DIR / prior_bundle["bundle_name"]
                    if prior_bundle.get("display_name") != display_name:
                        manifest_path = bundle_dir / "dashboard_manifest.json"
                        dashboard_manifest = _read_json(manifest_path, {})
                        dashboard_manifest["contact"] = display_name
                        dashboard_manifest["last_sync_at"] = _iso()
                        _atomic_write_json(manifest_path, dashboard_manifest)
                    next_checkpoint["bundles"][talker] = _bundle_checkpoint(bundle_dir, display_name)
                    continue
                raw_payload = {
                    "source": "weflow-cli-nt-readonly",
                    "snapshot_scope": "changed-source-partitions",
                    "source_partitions": source_partitions,
                    "session": {
                        "username": talker,
                        "displayName": display_name,
                        "type": "group" if "@chatroom" in talker else "direct",
                    },
                    "messages": raw_messages,
                }
                raw_path = _session_snapshot_path(run_root, talker)
                _atomic_write_json(raw_path, raw_payload)
                content_decode_failures = sum(
                    bool(item.get("contentDecodeFailed")) for item in raw_messages
                )

                converted = convert_weflow_cli_payload(
                    raw_payload,
                    display_name,
                    talker,
                    account_id,
                )
                del raw_payload
                del raw_messages
                normalized = normalize_payload(converted, drop_invalid=True)
                del converted
                bundle_dir = _existing_bundle_path(talker, display_name)
                messages_path = bundle_dir / "messages.json"
                merge_result = merge_into_bundle(
                    normalized,
                    messages_path,
                    raw_export_path=raw_path,
                    raw_archive_dir=RAW_ARCHIVE_ROOT,
                )
                del normalized
                counts = merge_result["counts"]
                stats_ok = True
                stats_path = bundle_dir / "stats.json"
                stats_stale = (
                    not stats_path.is_file()
                    or stats_path.stat().st_mtime_ns < messages_path.stat().st_mtime_ns
                )
                if counts.get("written") or (prior_bundle and repair) or stats_stale:
                    stats_ok = _run_stats(messages_path, bundle_dir)
                dashboard_manifest = {
                    "version": 1,
                    "contact": display_name,
                    "source": "wechat-local-readonly",
                    "message_count": counts["total_after"],
                    "conversation_kind": "group" if "@chatroom" in talker else "direct",
                    "last_sync_at": _iso(),
                }
                _atomic_write_json(bundle_dir / "dashboard_manifest.json", dashboard_manifest)
                next_checkpoint["bundles"][talker] = _bundle_checkpoint(bundle_dir, display_name)

                status["imported_conversations"] += 1
                status["imported_messages"] += int(counts.get("added", 0))
                status["deduplicated_messages"] += int(counts.get("duplicates", 0))
                if not counts.get("written"):
                    status["unchanged_messages"] += int(counts.get("incoming", 0))
                if not stats_ok or content_decode_failures:
                    status["failed_conversations"] += 1
                if not stats_ok:
                    row["stats_state"] = "failed"
                if content_decode_failures:
                    row["content_decode_failures"] = content_decode_failures
                row.update({
                    "state": "imported",
                    "raw_path": str(raw_path.relative_to(run_root)),
                    "added": int(counts.get("added", 0)),
                    "duplicates": int(counts.get("duplicates", 0)),
                    "total": int(counts.get("total_after", 0)),
                })
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                status["failed_conversations"] += 1
                row["state"] = "failed"
                row["error_code"] = "conversation_import_failed"
            finally:
                _write_status(status)
                _atomic_write_json(run_root / "manifest.json", private_manifest)
        # Commit only a completely successful run.  A crash or a failed
        # conversation leaves the previous checkpoint, so all affected work
        # is retried with the same conservative, idempotent merge next time.
        if not status["failed_conversations"]:
            snapshots = _ACTIVE_DATABASE_SNAPSHOTS.get()
            for record in records:
                token = _source_partition_token(record)
                if token not in fingerprints:
                    continue
                target = next_checkpoint["sources"].setdefault(token, {})
                source = Path(record["path"]).resolve()
                target["fingerprint"] = (
                    snapshots.fingerprints.get(source, fingerprints[token])
                    if snapshots is not None else fingerprints[token]
                )
            next_checkpoint["completed_at"] = _iso()
            _atomic_write_json(_checkpoint_path(), next_checkpoint)
    return private_manifest


def run_sync(
    *,
    scheduled: bool = False,
    rescan_keys: bool = False,
    allow_process_hook: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    mode = "scheduled" if scheduled else "manual"
    status = _base_status(mode)
    status.update({
        "state": "running",
        "last_run_at": _iso(),
        "next_run_at": _next_scheduled_run() if scheduled else None,
        "sync_strategy": "full-reconcile" if full else "changed-shards",
    })
    _write_status(status)

    try:
        with _exclusive_sync_lock(), _database_snapshot_scope():
            readiness = inspect_runtime()
            if not readiness.get("ready") or readiness.get("version") != "1.5.0":
                raise SyncFailure("exporter_missing", "固定本地读取器尚未就绪。")
            account_id, _db_root = discover_unique_account()
            nt_reader, records = _database_records(
                account_id,
                rescan=rescan_keys,
                allow_process_hook=allow_process_hook and not scheduled,
                cache_only=scheduled and not rescan_keys,
            )

            run_id = _utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
            run_root = RAW_ROOT / run_id
            run_root.mkdir(parents=True, exist_ok=False)
            manifest = _sync_sessions(
                nt_reader,
                records,
                account_id,
                run_root,
                status,
            )
            manifest["completed_at"] = _iso()
            manifest["counts"] = {
                key: status[key]
                for key in (
                    "scanned_conversations",
                    "imported_conversations",
                    "failed_conversations",
                    "imported_messages",
                    "deduplicated_messages",
                    "unchanged_messages",
                )
            }
            _atomic_write_json(run_root / "manifest.json", manifest)

            status["state"] = "completed"
            status["next_run_at"] = _next_scheduled_run()
            if status["failed_conversations"]:
                status["state"] = "error"
                status["error_code"] = "sync_failed"
                status["attention"] = "部分会话未完整导入（含解码或统计失败），原始微信记录未受影响。"
            _write_status(status)
            return status
    except SyncFailure as exc:
        if scheduled and exc.code == "database_key_unavailable":
            status["state"] = "awaiting_wechat_restart"
            status["error_code"] = None
            status["attention"] = "等待微信下次正常启动时完成一次性本地密钥初始化。"
        else:
            status["state"] = (
                "needs_account_selection"
                if exc.code == "needs_account_selection"
                else "wechat_not_running"
                if exc.code == "wechat_not_running"
                else "error"
            )
            status["error_code"] = _dashboard_error_code(exc.code)
            status["attention"] = exc.attention
        status["internal_error_code"] = exc.code
        _write_status(status)
        return status
    except Exception:
        status["state"] = "error"
        status["error_code"] = "sync_failed"
        status["internal_error_code"] = "local_sync_error"
        status["attention"] = "本地同步遇到错误；微信原始记录未被修改。"
        _write_status(status)
        return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="只读同步全部微信会话到本地可视化（原库不修改）"
    )
    parser.add_argument("--sync", action="store_true", help="执行一次本地只读同步")
    parser.add_argument("--scheduled", action="store_true", help="标记为自动任务运行")
    parser.add_argument("--full", action="store_true", help="忽略增量检查点，完整核对本机历史（不删除原记录）")
    parser.add_argument("--rescan-keys", action="store_true", help="重新扫描本机数据库密钥")
    parser.add_argument(
        "--allow-process-hook",
        action="store_true",
        help="仅用于一次性初始化；允许固定读取器临时注入微信进程以观察登录密钥",
    )
    parser.add_argument("--status", action="store_true", help="仅显示脱敏同步状态")
    args = parser.parse_args(argv)

    if args.sync:
        status = run_sync(
            scheduled=args.scheduled,
            rescan_keys=args.rescan_keys,
            allow_process_hook=args.allow_process_hook,
            full=args.full,
        )
    else:
        status = _read_json(STATUS_PATH, _base_status("manual"))
    print(json.dumps(public_status(status), ensure_ascii=False))
    return 0 if status.get("state") in {
        "ready",
        "completed",
        "awaiting_wechat_restart",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
