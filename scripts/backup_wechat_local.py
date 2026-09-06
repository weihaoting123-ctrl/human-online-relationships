"""Local, incremental backup of the project's already archived WeChat material.

No WeChat installation, credential directory, network client or cloud API is used.
Objects are independent byte copies; immutable manifests retain every completed
snapshot. A stat baseline avoids reading unchanged content. Explicit verification
and every restore hash the saved bytes; stat baselines are not a bit-rot scan.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
# Manifest versions retain their exact allowlists. Never extend a historical
# version in place: old manifests are immutable, hashed recovery evidence.
LEGACY_SOURCE_ROOTS = {
    "contacts": "data/contacts",
    "raw": "data/raw",
    "archive": "data/private/wechat-archive",
    "voice": "data/private/wechat-voice",
    "media_exports": "data/exports/wechat-media",
    "voice_exports": "data/exports/wechat-voice",
}
MANIFEST_SCHEMA_VERSION = 2
SOURCE_ROOTS = {**LEGACY_SOURCE_ROOTS, "library": "data/private/library"}
MANIFEST_SOURCE_ROOTS = {1: LEGACY_SOURCE_ROOTS, 2: SOURCE_ROOTS}
DEFAULT_RESERVE = 512 * 1024 * 1024
CHUNK_SIZE = 2 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODES = {
    "INVALID_LOCATION", "UNSAFE_PATH", "BUSY", "NO_SOURCE_DATA", "NO_SNAPSHOT",
    "SOURCE_CHANGED", "SOURCE_UNREADABLE", "INSUFFICIENT_SPACE", "SQLITE_SNAPSHOT_FAILED",
    "OBJECT_CORRUPT", "MANIFEST_INVALID", "RESTORE_EXISTS", "INTERRUPTED", "IO_ERROR",
    "INVALID_ARGUMENT", "INTERNAL_ERROR",
}
COUNT_KEYS = ("files", "bytes", "objects_copied", "bytes_copied", "unchanged",
              "sqlite_snapshots", "skipped", "verified", "processed", "errors")
CATEGORIES = ("text", "voice", "images", "other")
SQLITE_MAGIC = b"SQLite format 3\x00"
LIVE_SQLITE = {
    "data/private/wechat-archive/catalog.sqlite3",
    "data/private/wechat-voice/catalog.sqlite3",
    "data/private/wechat-voice/transcripts.sqlite3",
    "data/private/library/library.sqlite3",
}
WRITER_LOCKS = (
    "data/private/wechat-sync/sync.lock", "data/private/dashboard-import.lock",
    "data/private/wechat-archive/archive.lock", "data/private/wechat-voice/archive.lock",
    "data/private/wechat-voice/transcription/archive.lock",
    "data/private/wechat-classification/archive.lock",
    "data/private/library/writer.lock",
)


class BackupError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "INTERNAL_ERROR"
        super().__init__(self.code)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_chain(path, *, destination=False):
    """Reject symlinks/junctions in every existing ancestor, not just the leaf."""
    path = Path(os.path.abspath(path))
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info) or (destination and stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise BackupError("UNSAFE_PATH")
    return path


def _layout(project_root):
    root = _safe_chain(project_root, destination=True)
    if root.drive.casefold() == "c:" or root.drive.startswith("\\\\") or not root.is_dir() or root == Path(root.anchor):
        raise BackupError("INVALID_LOCATION")
    backup = _safe_chain(root / "backups/wechat", destination=True)
    status_path = _safe_chain(root / "data/private/wechat-backup/status.json", destination=True)
    for relative in SOURCE_ROOTS.values():
        source = root / relative
        if source == backup or source in backup.parents or backup in source.parents:
            raise BackupError("INVALID_LOCATION")
    return root, backup, status_path


def _manifest_roots(version):
    if type(version) is not int or version not in MANIFEST_SOURCE_ROOTS:
        raise BackupError("MANIFEST_INVALID")
    return MANIFEST_SOURCE_ROOTS[version]


def _safe_relative(value, *, schema_version=MANIFEST_SCHEMA_VERSION):
    roots = _manifest_roots(schema_version)
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise BackupError("MANIFEST_INVALID")
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in parts):
        raise BackupError("MANIFEST_INVALID")
    if any(re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p, re.I) for p in parts):
        raise BackupError("MANIFEST_INVALID")
    result = PurePosixPath(value)
    if result.is_absolute() or not any(value.startswith(base + "/") for base in roots.values()):
        raise BackupError("MANIFEST_INVALID")
    return result


def _fingerprint(path):
    _safe_chain(path)
    s = Path(path).stat()
    if not stat.S_ISREG(s.st_mode):
        raise BackupError("UNSAFE_PATH")
    return _stat_signature(s)


def _stat_signature(info):
    # Windows Path.stat and os.fstat disagree about legacy st_ctime semantics
    # on recent Python. Their explicit birthtime fields describe the same value.
    created = getattr(info, "st_birthtime_ns", info.st_ctime_ns)
    return [info.st_size, info.st_mtime_ns, created, info.st_dev, info.st_ino]


def _sqlite_fingerprint(path):
    result = [_fingerprint(path)]
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        result.append(_fingerprint(sidecar) if sidecar.exists() else None)
    return result


def _excluded(name, *, directory=False):
    lower = name.casefold()
    if lower in {"tmp", "temp", ".tmp", ".staging"}:
        return True
    if directory:
        return False
    return (lower.endswith((".lock", ".tmp", ".temp", ".part", ".partial", ".dpapi", "~"))
            or lower.startswith(("~$", ".~", "key-cache", "image-key-cache"))
            or lower in {"config.json", "credentials.json", "secrets.json", ".env"})


def _inventory(root):
    files, skipped, presence = {}, 0, {}
    for label, relative in SOURCE_ROOTS.items():
        folder = root / relative
        presence[label] = folder.exists()
        if not presence[label]:
            continue
        try:
            _safe_chain(folder)
        except BackupError:
            # A scope root that is itself redirected is a configuration error.
            raise BackupError("UNSAFE_PATH") from None
        if not folder.is_dir():
            raise BackupError("UNSAFE_PATH")
        def onerror(_error):
            raise BackupError("SOURCE_UNREADABLE")
        for current, dirs, names in os.walk(folder, followlinks=False, onerror=onerror):
            accepted = []
            for name in sorted(dirs):
                item = Path(current) / name
                if _excluded(name, directory=True) or _is_link(item.lstat()):
                    skipped += 1
                else:
                    accepted.append(name)
            dirs[:] = accepted
            for name in sorted(names):
                item = Path(current) / name
                info = item.lstat()
                key = item.relative_to(root).as_posix()
                live_sidecar = any(key.endswith(suffix) and key[:-len(suffix)] in LIVE_SQLITE
                                   for suffix in ("-wal", "-shm", "-journal"))
                if _excluded(name) or live_sidecar or _is_link(info) or not stat.S_ISREG(info.st_mode):
                    skipped += 1
                    continue
                _safe_relative(key)
                files[key] = _fingerprint(item)
    return files, skipped, presence


def _category(key):
    lower = key.casefold()
    if "/wechat-voice/" in lower or lower.endswith((".silk", ".wav", ".mp3", ".ogg", ".m4a", ".flac")):
        return "voice"
    if "/wechat-media/" in lower or lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".dat", ".wxgf")):
        return "images"
    if lower.startswith(("data/contacts/", "data/raw/")) or lower.endswith((".txt", ".json", ".jsonl", ".md")):
        return "text"
    return "other"


def _space(path, required, reserve):
    if shutil.disk_usage(path).free < max(0, required) + reserve:
        raise BackupError("INSUFFICIENT_SPACE")


def _sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_json(path, value):
    _safe_chain(path, destination=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        with temp.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _safe_chain(path, destination=True)
        os.replace(temp, path)
        _sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _lock(backup):
    _safe_chain(backup, destination=True)
    backup.mkdir(parents=True, exist_ok=True)
    with _file_lock(backup / ".backup.lock"):
        yield


@contextmanager
def _file_lock(path):
    path = _safe_chain(path, destination=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BackupError("BUSY") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _source_locks(root):
    """Coordinate project writers without opening any account/credential data."""
    with ExitStack() as stack:
        for relative in WRITER_LOCKS:
            path = root / relative
            _safe_chain(path, destination=True)
            # Do not invent missing source roots merely to create lock files.
            # Newly appearing roots are caught by the final inventory comparison.
            if path.parent.is_dir():
                stack.enter_context(_file_lock(path))
        yield


def _empty_status():
    return {"schema_version": 1, "state": "idle", "mode": "backup", "last_attempt_at": None,
            "last_success_at": None, "snapshot_id": None, "snapshot_count": 0,
            "source_presence": {key: False for key in SOURCE_ROOTS}, "error_code": None,
            "verification_complete": False, "verified_at": None,
            "counts": {**{key: 0 for key in COUNT_KEYS}, "categories": {key: 0 for key in CATEGORIES}}}


def _public_status(value):
    """Allowlist every returned field, including errors, to avoid private-data leaks."""
    result = _empty_status()
    if not isinstance(value, dict):
        return result
    if value.get("state") in ("idle", "running", "completed", "failed", "busy"):
        result["state"] = value["state"]
    if value.get("mode") in ("backup", "verify"):
        result["mode"] = value["mode"]
    for name in ("last_attempt_at", "last_success_at", "verified_at"):
        candidate = value.get(name)
        if isinstance(candidate, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)", candidate):
            result[name] = candidate
    candidate = value.get("snapshot_id")
    if isinstance(candidate, str) and re.fullmatch(r"\d{8}T\d{6}Z_[0-9a-f]{32}", candidate):
        result["snapshot_id"] = candidate
    result["snapshot_count"] = _count(value.get("snapshot_count"))
    result["verification_complete"] = value.get("verification_complete") is True
    if isinstance(value.get("error_code"), str) and value["error_code"] in ERROR_CODES:
        result["error_code"] = value["error_code"]
    counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
    for key in COUNT_KEYS:
        result["counts"][key] = _count(counts.get(key))
    categories = counts.get("categories") if isinstance(counts.get("categories"), dict) else {}
    result["counts"]["categories"] = {key: _count(categories.get(key)) for key in CATEGORIES}
    presence = value.get("source_presence") if isinstance(value.get("source_presence"), dict) else {}
    result["source_presence"] = {key: presence.get(key) is True for key in SOURCE_ROOTS}
    return result


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _read_status(path):
    if not path.exists():
        return _empty_status()
    _safe_chain(path, destination=True)
    try:
        return _public_status(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return _empty_status()


def get_status(project_root=REPO):
    try:
        _, backup, status_path = _layout(project_root)
        result = _read_status(status_path)
        if result["state"] == "running":
            try:
                with _lock(backup):
                    result.update(state="failed", error_code="INTERRUPTED")
            except BackupError as error:
                if error.code != "BUSY":
                    raise
        return result
    except (BackupError, OSError) as error:
        result = _empty_status()
        result.update(state="failed", error_code=_error_code(error))
        return result


def _object_path(backup, digest):
    if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
        raise BackupError("MANIFEST_INVALID")
    return _safe_chain(backup / "objects" / digest[:2] / digest, destination=True)


def _load_manifest(backup, reference):
    try:
        snapshot_id, expected = reference["snapshot_id"], reference["manifest_sha256"]
        if not isinstance(snapshot_id, str) or not ID_RE.fullmatch(snapshot_id) or not HASH_RE.fullmatch(expected):
            raise BackupError("MANIFEST_INVALID")
        path = _safe_chain(backup / "snapshots" / (snapshot_id + ".json"), destination=True)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise BackupError("MANIFEST_INVALID")
        value = json.loads(payload)
        roots = _manifest_roots(value["schema_version"])
        if value["snapshot_id"] != snapshot_id or value["source_roots"] != roots:
            raise BackupError("MANIFEST_INVALID")
        if not isinstance(value["files"], dict):
            raise BackupError("MANIFEST_INVALID")
        aliases, object_sizes = set(), {}
        for key, record in value["files"].items():
            _safe_relative(key, schema_version=value["schema_version"])
            alias = key.casefold() if os.name == "nt" else key
            if alias in aliases:
                raise BackupError("MANIFEST_INVALID")
            aliases.add(alias)
            if not isinstance(record["object"], str) or not HASH_RE.fullmatch(record["object"]):
                raise BackupError("MANIFEST_INVALID")
            if type(record["size"]) is not int or record["size"] < 0 or record["kind"] not in {"file", "sqlite"}:
                raise BackupError("MANIFEST_INVALID")
            if record["object"] in object_sizes and object_sizes[record["object"]] != record["size"]:
                raise BackupError("MANIFEST_INVALID")
            object_sizes[record["object"]] = record["size"]
        return value
    except (ValueError, KeyError, TypeError, OSError):
        raise BackupError("MANIFEST_INVALID") from None


def _current(backup):
    path = _safe_chain(backup / "current.json", destination=True)
    if not path.exists():
        return None, None
    try:
        reference = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise BackupError("MANIFEST_INVALID") from None
    return reference, _load_manifest(backup, reference)


def _hash_file(path):
    digest = hashlib.sha256()
    before = _fingerprint(path)
    with Path(path).open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    if _fingerprint(path) != before:
        raise BackupError("OBJECT_CORRUPT")
    return digest.hexdigest()


def _stable_copy(source, target, expected):
    """Copy ordinary bytes and compare both the open handle and path identity."""
    if _fingerprint(source) != expected:
        raise BackupError("SOURCE_CHANGED")
    digest, size = hashlib.sha256(), 0
    with Path(source).open("rb") as reader, Path(target).open("xb") as writer:
        before = os.fstat(reader.fileno())
        opened = _stat_signature(before)
        if opened != expected:
            raise BackupError("SOURCE_CHANGED")
        while chunk := reader.read(CHUNK_SIZE):
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(reader.fileno())
        closed = _stat_signature(after)
        writer.flush()
        os.fsync(writer.fileno())
    if opened != closed or _fingerprint(source) != expected or size != expected[0]:
        raise BackupError("SOURCE_CHANGED")
    return digest.hexdigest(), size


def _is_sqlite(path):
    with path.open("rb") as source:
        return source.read(16) == SQLITE_MAGIC


def _snapshot_sqlite(source, staging, expected, backup, reserve):
    """SQLite only opens a stable private copy; original DB/WAL/SHM stay untouched."""
    if _sqlite_fingerprint(source) != expected:
        raise BackupError("SOURCE_CHANGED")
    total = sum(item[0] for item in expected if item)
    _space(backup, total * 2 + CHUNK_SIZE, reserve)
    work = staging / ("sqlite_" + uuid.uuid4().hex)
    work.mkdir()
    clone = work / "source.sqlite3"
    source_hashes = [_stable_copy(source, clone, expected[0])[0]]
    for index, suffix in enumerate(("-wal", "-journal"), 1):
        if expected[index] is not None:
            source_hashes.append(_stable_copy(Path(str(source) + suffix), Path(str(clone) + suffix), expected[index])[0])
        else:
            source_hashes.append(None)
    if _sqlite_fingerprint(source) != expected:
        raise BackupError("SOURCE_CHANGED")
    target = work / "snapshot.sqlite3"
    started = time.monotonic()
    def progress(status, remaining, pages):
        if time.monotonic() - started > 300:
            raise BackupError("SQLITE_SNAPSHOT_FAILED")
    try:
        # SHM (a coordination file) is rebuilt only alongside the private copy.
        with sqlite3.connect(clone.as_uri() + "?mode=ro", uri=True, timeout=5) as reader:
            reader.execute("PRAGMA query_only=ON")
            with sqlite3.connect(target) as writer:
                reader.backup(writer, pages=512, progress=progress, sleep=0.02)
        # Explicit close is required on Windows: context managers only commit.
        reader.close()
        writer.close()
    except sqlite3.Error:
        raise BackupError("SQLITE_SNAPSHOT_FAILED") from None
    finally:
        if "reader" in locals():
            reader.close()
        if "writer" in locals():
            writer.close()
    if _sqlite_fingerprint(source) != expected:
        raise BackupError("SOURCE_CHANGED")
    with target.open("r+b") as handle:
        os.fsync(handle.fileno())
    if _sqlite_hashes(source, expected) != source_hashes:
        raise BackupError("SOURCE_CHANGED")
    return target, _hash_file(target), target.stat().st_size, source_hashes


def _sqlite_hashes(source, expected):
    # Small mutable catalogs need a stronger check than media-file timestamps:
    # memory-mapped WAL writes on Windows may not immediately update metadata.
    if _sqlite_fingerprint(source) != expected:
        raise BackupError("SOURCE_CHANGED")
    result = [_hash_file(source)]
    for index, suffix in enumerate(("-wal", "-journal"), 1):
        result.append(_hash_file(Path(str(source) + suffix)) if expected[index] else None)
    return result


def _publish_object(temp, digest, size, backup, known_objects):
    destination = _object_path(backup, digest)
    if destination.exists():
        signature = _fingerprint(destination)
        if signature[0] != size:
            raise BackupError("OBJECT_CORRUPT")
        if known_objects.get(digest) != signature and _hash_file(destination) != digest:
            raise BackupError("OBJECT_CORRUPT")
        known_objects[digest] = signature
        # This is only an unpublished staging copy; keeping it until the end
        # would waste disk space for a large set of repeated exports.
        _safe_chain(temp, destination=True)
        temp.unlink()
        return signature, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe_chain(destination, destination=True)
    # The process-wide backup lock prevents simultaneous publishers. Existing
    # objects are never overwritten, and temporary bytes are on the same volume.
    if destination.exists():
        raise BackupError("BUSY")
    os.rename(temp, destination)
    _sync_dir(destination.parent)
    signature = _fingerprint(destination)
    known_objects[digest] = signature
    return signature, True


def _error_code(error):
    if isinstance(error, BackupError):
        return error.code
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "INTERRUPTED"
    if isinstance(error, OSError):
        if error.errno == errno.ENOSPC or getattr(error, "winerror", None) == 112:
            return "INSUFFICIENT_SPACE"
        return "IO_ERROR"
    return "INTERNAL_ERROR"


def _operation(project_root, mode, work):
    status, status_path = _empty_status(), None
    try:
        root, backup, status_path = _layout(project_root)
        status = _read_status(status_path)
        with _lock(backup):
            status.update(state="running", mode=mode, last_attempt_at=_now(), error_code=None,
                          verification_complete=False)
            status["counts"] = {**{key: 0 for key in COUNT_KEYS}, "categories": {key: 0 for key in CATEGORIES}}
            _atomic_json(status_path, _public_status(status))
            try:
                if mode == "backup":
                    with _source_locks(root):
                        work(root, backup, status_path, status)
                else:
                    work(root, backup, status_path, status)
                status.update(state="completed", error_code=None)
            except (Exception, KeyboardInterrupt) as error:
                status.update(state="busy" if _error_code(error) == "BUSY" else "failed", error_code=_error_code(error))
                status["counts"]["errors"] = 1
            _atomic_json(status_path, _public_status(status))
    except (Exception, KeyboardInterrupt) as error:
        status.update(state="busy" if _error_code(error) == "BUSY" else "failed", error_code=_error_code(error))
        # In particular, a contender must never overwrite the lock owner's status.
    return _public_status(status)


def run_backup(project_root=REPO, *, reserve_bytes=DEFAULT_RESERVE):
    """Publish a snapshot only after all included source versions remain stable."""
    def work(root, backup, status_path, status):
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise BackupError("INVALID_ARGUMENT")
        _space(backup, CHUNK_SIZE, reserve_bytes)
        previous_ref, previous = _current(backup)
        old_files = previous["files"] if previous else {}
        inventory, skipped, presence = _inventory(root)
        status["source_presence"] = presence
        status["counts"].update(files=len(inventory), skipped=skipped)
        if not inventory:
            raise BackupError("NO_SOURCE_DATA")
        known_objects, records, stable_sources = {}, {}, {}
        staging_root = _safe_chain(backup / ".staging", destination=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        last_progress = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="backup_", dir=staging_root) as temporary:
            staging = Path(temporary)
            for key, initial in inventory.items():
                source, old = root / key, old_files.get(key)
                # Only mutable project catalogs are normalized. Archived SQLite
                # blobs and imported files must keep their original exact bytes.
                kind = "sqlite" if key in LIVE_SQLITE else "file"
                if kind == "sqlite" and not _is_sqlite(source):
                    raise BackupError("SQLITE_SNAPSHOT_FAILED")
                current_fp = _sqlite_fingerprint(source) if kind == "sqlite" else [_fingerprint(source)]
                if current_fp[0] != initial:
                    raise BackupError("SOURCE_CHANGED")
                stable_sources[key] = (kind, current_fp)
                intact = False
                if kind != "sqlite" and old and old.get("source_fingerprint") == current_fp:
                    obj = _object_path(backup, old["object"])
                    intact = obj.exists() and _fingerprint(obj) == old.get("object_fingerprint")
                if intact:
                    record = old
                    known_objects[old["object"]] = old["object_fingerprint"]
                    status["counts"]["unchanged"] += 1
                else:
                    if kind == "sqlite":
                        temp, digest, size, sqlite_hashes = _snapshot_sqlite(source, staging, current_fp, backup, reserve_bytes)
                        status["counts"]["sqlite_snapshots"] += 1
                    else:
                        _space(backup, initial[0] + CHUNK_SIZE, reserve_bytes)
                        temp = staging / (uuid.uuid4().hex + ".tmp")
                        digest, size = _stable_copy(source, temp, initial)
                    object_fp, copied = _publish_object(temp, digest, size, backup, known_objects)
                    if copied:
                        status["counts"]["objects_copied"] += 1
                        status["counts"]["bytes_copied"] += size
                    record = {"object": digest, "size": size, "kind": kind,
                              "source_fingerprint": current_fp, "object_fingerprint": object_fp}
                    if kind == "sqlite":
                        record["sqlite_source_hashes"] = sqlite_hashes
                records[key] = record
                status["counts"]["bytes"] += record["size"]
                status["counts"]["processed"] += 1
                status["counts"]["categories"][_category(key)] += 1
                if time.monotonic() - last_progress > 2:
                    _atomic_json(status_path, _public_status(status))
                    last_progress = time.monotonic()
            final_inventory, _, final_presence = _inventory(root)
            if final_inventory != inventory or final_presence != presence:
                raise BackupError("SOURCE_CHANGED")
            for key, (kind, expected) in stable_sources.items():
                actual = _sqlite_fingerprint(root / key) if kind == "sqlite" else [_fingerprint(root / key)]
                if actual != expected:
                    raise BackupError("SOURCE_CHANGED")
                if kind == "sqlite" and _sqlite_hashes(root / key, expected) != records[key]["sqlite_source_hashes"]:
                    raise BackupError("SOURCE_CHANGED")
            if (previous and previous["schema_version"] == MANIFEST_SCHEMA_VERSION
                    and records == old_files and presence == previous.get("source_presence")):
                status.update(snapshot_id=previous["snapshot_id"], snapshot_count=previous["snapshot_count"])
            else:
                snapshot_id = _new_id()
                manifest = {"schema_version": MANIFEST_SCHEMA_VERSION, "snapshot_id": snapshot_id, "created_at": _now(),
                            "snapshot_count": (previous["snapshot_count"] if previous else 0) + 1,
                            "source_roots": SOURCE_ROOTS, "source_presence": presence,
                            "parent": previous_ref, "files": records,
                            "counts": {key: status["counts"][key] for key in ("files", "bytes", "categories")}}
                # Metadata needs its own reserve; source removal never deletes old objects.
                _space(backup, len(records) * 1024 + CHUNK_SIZE, reserve_bytes)
                manifest_hash = _atomic_json(backup / "snapshots" / (snapshot_id + ".json"), manifest)
                _atomic_json(backup / "current.json", {"snapshot_id": snapshot_id, "manifest_sha256": manifest_hash})
                status.update(snapshot_id=snapshot_id, snapshot_count=manifest["snapshot_count"])
            status["last_success_at"] = _now()
    return _operation(project_root, "backup", work)


def get_history(project_root=REPO, limit=20):
    """Return only fixed aggregate fields from the published snapshot lineage."""
    try:
        _, backup, _ = _layout(project_root)
        reference, current = _current(backup)
        history, visited = [], set()
        for _ in range(min(100, max(0, int(limit)))):
            if not current:
                break
            if current["snapshot_id"] in visited:
                raise BackupError("MANIFEST_INVALID")
            visited.add(current["snapshot_id"])
            safe = _public_status({"snapshot_id": current["snapshot_id"], "last_success_at": current["created_at"],
                                   "counts": current["counts"]})
            history.append({"snapshot_id": safe["snapshot_id"], "created_at": safe["last_success_at"],
                            "files": safe["counts"]["files"], "bytes": safe["counts"]["bytes"],
                            "categories": safe["counts"]["categories"]})
            reference = current.get("parent")
            current = _load_manifest(backup, reference) if reference else None
        return history
    except (BackupError, OSError, ValueError, TypeError, KeyError):
        return []


def verify_backup(project_root=REPO, *, max_objects=None):
    """Hash the latest saved snapshot without reading any source files or backing up."""
    def work(root, backup, status_path, status):
        if max_objects is not None and (type(max_objects) is not int or max_objects <= 0):
            raise BackupError("INVALID_ARGUMENT")
        _, manifest = _current(backup)
        if not manifest:
            raise BackupError("NO_SNAPSHOT")
        status.update(snapshot_id=manifest["snapshot_id"], snapshot_count=manifest["snapshot_count"],
                      source_presence=manifest["source_presence"])
        status["counts"].update(manifest["counts"])
        unique = {value["object"]: value["size"] for value in manifest["files"].values()}
        last_progress = time.monotonic()
        for digest, size in unique.items():
            if max_objects is not None and status["counts"]["verified"] >= max_objects:
                break
            path = _object_path(backup, digest)
            if not path.exists() or _fingerprint(path)[0] != size or _hash_file(path) != digest:
                raise BackupError("OBJECT_CORRUPT")
            status["counts"]["verified"] += 1
            if time.monotonic() - last_progress > 2:
                _atomic_json(status_path, _public_status(status))
                last_progress = time.monotonic()
        status["verification_complete"] = status["counts"]["verified"] == len(unique)
        status["verified_at"] = _now()
    return _operation(project_root, "verify", work)


def restore_snapshot(project_root, restore_id, *, snapshot_id=None, reserve_bytes=DEFAULT_RESERVE):
    """Materialize verified independent copies in a NEW backups/restored/<id>.

    The requested directory appears only after every file verifies successfully.
    No restore destination can be a source or the live WeChat installation.
    """
    result = {"schema_version": 1, "state": "failed", "error_code": None,
              "restore_id": None, "snapshot_id": None, "files": 0, "bytes": 0}
    try:
        root, backup, _ = _layout(project_root)
        if not isinstance(restore_id, str) or not ID_RE.fullmatch(restore_id):
            raise BackupError("INVALID_ARGUMENT")
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or not ID_RE.fullmatch(snapshot_id)):
            raise BackupError("INVALID_ARGUMENT")
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise BackupError("INVALID_ARGUMENT")
        # Validate Windows reserved names as well, even for a restore ID without dots.
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", restore_id, re.I):
            raise BackupError("INVALID_ARGUMENT")
        parent = _safe_chain(root / "backups/restored", destination=True)
        target = _safe_chain(parent / restore_id, destination=True)
        if target.exists():
            raise BackupError("RESTORE_EXISTS")
        with _lock(backup):
            reference, manifest = _current(backup)
            visited = set()
            while manifest and snapshot_id and manifest["snapshot_id"] != snapshot_id:
                if manifest["snapshot_id"] in visited:
                    raise BackupError("MANIFEST_INVALID")
                visited.add(manifest["snapshot_id"])
                reference = manifest.get("parent")
                manifest = _load_manifest(backup, reference) if reference else None
            if not manifest:
                raise BackupError("NO_SNAPSHOT")
            _space(backup, sum(item["size"] for item in manifest["files"].values()) + CHUNK_SIZE, reserve_bytes)
            parent.mkdir(parents=True, exist_ok=True)
            staging_root = _safe_chain(backup / ".staging", destination=True)
            staging_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="restore_", dir=staging_root) as temporary:
                materialized = Path(temporary) / "complete"
                materialized.mkdir()
                for key, entry in manifest["files"].items():
                    relative = _safe_relative(key, schema_version=manifest["schema_version"])
                    destination = _safe_chain(materialized / relative, destination=True)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    obj = _object_path(backup, entry["object"])
                    if not obj.exists():
                        raise BackupError("OBJECT_CORRUPT")
                    digest, size = _stable_copy(obj, destination, _fingerprint(obj))
                    if digest != entry["object"] or size != entry["size"]:
                        raise BackupError("OBJECT_CORRUPT")
                    result["files"] += 1
                    result["bytes"] += size
                _safe_chain(target, destination=True)
                if target.exists():
                    raise BackupError("RESTORE_EXISTS")
                os.rename(materialized, target)
                _sync_dir(parent)
            result.update(state="completed", restore_id=restore_id, snapshot_id=manifest["snapshot_id"])
    except (Exception, KeyboardInterrupt) as error:
        result["error_code"] = _error_code(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--backup", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--restore-to", metavar="NEW_ID")
    parser.add_argument("--project-root", type=Path, default=REPO)
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE)
    parser.add_argument("--max-objects", type=int, default=None)
    parser.add_argument("--snapshot-id", default=None)
    args = parser.parse_args()
    if args.status:
        result = get_status(args.project_root)
    elif args.verify:
        result = verify_backup(args.project_root, max_objects=args.max_objects)
    elif args.restore_to:
        result = restore_snapshot(args.project_root, args.restore_to, snapshot_id=args.snapshot_id,
                                  reserve_bytes=args.reserve_bytes)
    else:
        result = run_backup(args.project_root, reserve_bytes=args.reserve_bytes)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["state"] in {"idle", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
