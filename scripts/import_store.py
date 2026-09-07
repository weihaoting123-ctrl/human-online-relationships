"""Immutable offline import bundles shared by CLI and the local dashboard.

The OS lock coordinates updated import writers. It does not protect against an
uncooperative process changing paths during a save. A successful directory rename
is the commit point; failures after it leave the complete bundle for retry/reuse.
POSIX directory fsync is used where supported. Windows flushes files before
rename, without claiming power-loss durability for directory metadata.
"""

from contextlib import contextmanager
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile

from contact_bundle import resolve_bundle_paths


class ImportStoreError(ValueError):
    """A fixed, non-sensitive import failure code."""


class ImportBusyError(RuntimeError):
    def __init__(self):
        super().__init__("IMPORT_BUSY")


_ERROR_MESSAGES = {
    "IMPORT_INVALID_INPUT": "Import input is invalid.",
    "IMPORT_INPUT_MISSING": "Import input is missing.",
    "IMPORT_BUSY": "Another import is in progress.",
    "IMPORT_IO_ERROR": "Import could not be saved.",
    "IMPORT_UNSAFE_PATH": "Import path is not supported.",
}
_MATERIAL_NAMES = frozenset(("messages.json", "emojis.json"))


def import_error_payload(exc):
    """Map failures to fixed messages; never return exception/input/path text."""
    if isinstance(exc, ImportBusyError):
        code = "IMPORT_BUSY"
    elif isinstance(exc, ImportStoreError) and str(exc) in _ERROR_MESSAGES:
        code = str(exc)
    elif isinstance(exc, FileNotFoundError):
        code = "IMPORT_INPUT_MISSING"
    elif isinstance(exc, OSError):
        code = "IMPORT_IO_ERROR"
    else:
        code = "IMPORT_INVALID_INPUT"
    return {"status": "error", "code": code, "error": _ERROR_MESSAGES[code]}


def _validate_json(value, active=None):
    active = set() if active is None else active
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) not in (dict, list) or id(value) in active:
        raise ImportStoreError("IMPORT_INVALID_INPUT")
    active.add(id(value))
    try:
        if isinstance(value, dict):
            if any(type(key) is not str for key in value):
                raise ImportStoreError("IMPORT_INVALID_INPUT")
            values = value.values()
        else:
            values = value
        for child in values:
            _validate_json(child, active)
    finally:
        active.remove(id(value))


def _canonical_bytes(value):
    try:
        _validate_json(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ImportStoreError("IMPORT_INVALID_INPUT") from exc


def _material(payload, identity_context, sidecars):
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise ImportStoreError("IMPORT_INVALID_INPUT")
    if not isinstance(identity_context, dict):
        raise ImportStoreError("IMPORT_INVALID_INPUT")
    sidecars = {} if sidecars is None else sidecars
    if not isinstance(sidecars, dict) or set(sidecars) - {"emojis.json"}:
        raise ImportStoreError("IMPORT_INVALID_INPUT")
    if any(not isinstance(value, dict) for value in sidecars.values()):
        raise ImportStoreError("IMPORT_INVALID_INPUT")
    # Validate even omitted bundle_dir fields: the API accepts JSON values only.
    _canonical_bytes({"payload": payload, "identity_context": identity_context, "sidecars": sidecars})
    return {
        "payload": {key: value for key, value in payload.items() if key != "bundle_dir"},
        "identity_context": identity_context,
        "sidecars": {name: {key: value for key, value in value.items() if key != "bundle_dir"}
                     for name, value in sidecars.items()},
    }


def fingerprint_import(payload, *, identity_context, sidecars=None):
    """Hash prepared JSON and explicit identity, omitting only material bundle_dir.

    Callers capture source/account identity and explicit conversion options before
    parsing drops them. Input source filenames and filesystem paths belong outside
    identity_context. Messages are never normalized, reordered or deduplicated.
    """
    return hashlib.sha256(_canonical_bytes(_material(payload, identity_context, sidecars))).hexdigest()


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def validate_import_path(path):
    """Return an absolute lexical path after rejecting links/reparse ancestors.

    Missing components are accepted. Parent traversal is rejected so lexical
    normalization cannot hide a link that would otherwise be traversed.
    """
    try:
        raw = Path(path)
        if ".." in raw.parts:
            raise ImportStoreError("IMPORT_UNSAFE_PATH")
        absolute = Path(os.path.abspath(raw))
        for current in (*reversed(absolute.parents), absolute):
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if _is_link(info) or (current != absolute and not stat.S_ISDIR(info.st_mode)):
                raise ImportStoreError("IMPORT_UNSAFE_PATH")
        return absolute
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, ImportStoreError):
            raise
        raise ImportStoreError("IMPORT_UNSAFE_PATH") from exc


def validated_private_root(contacts_dir):
    """Validate/create the contacts root and its fixed private work sibling."""
    contacts = validate_import_path(contacts_dir)
    private = validate_import_path(contacts.parent / "private")
    if contacts == private or contacts.parent == contacts:
        raise ImportStoreError("IMPORT_UNSAFE_PATH")
    try:
        for directory in (contacts, private):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            validate_import_path(directory)
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ImportStoreError("IMPORT_UNSAFE_PATH")
        if contacts.stat().st_dev != private.stat().st_dev:
            raise ImportStoreError("IMPORT_UNSAFE_PATH")
        return private
    except OSError as exc:
        raise ImportStoreError("IMPORT_IO_ERROR") from exc


@contextmanager
def exclusive_import_lock(contacts_dir):
    """Nonblocking process-owned lock shared with the dashboard; never retries."""
    private = validated_private_root(contacts_dir)
    lock_path = validate_import_path(private / "dashboard-import.lock")
    fd = None
    acquired = False
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
        validate_import_path(lock_path)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ImportStoreError("IMPORT_UNSAFE_PATH")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise ImportBusyError() from exc
            raise
        acquired = True
    except OSError as exc:
        raise ImportStoreError("IMPORT_IO_ERROR") from exc
    finally:
        if not acquired and fd is not None:
            os.close(fd)
    try:
        yield
    finally:
        os.close(fd)  # Closing releases the kernel lock, including on failure.


def _fsync_directory(path):
    if os.name == "nt":
        return
    unsupported = (errno.EINVAL, errno.ENOTSUP, errno.EBADF)
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in unsupported:
            raise
    finally:
        if fd is not None:
            os.close(fd)


def sync_import_directory(path):
    """Flush a validated directory where supported (Windows is a no-op)."""
    _fsync_directory(validate_import_path(path))


def _write_json(path, data):
    content = _canonical_bytes(data)
    with open(path, "xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(content).hexdigest()


def _read_json(path):
    path = validate_import_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ImportStoreError("IMPORT_UNSAFE_PATH")
    with open(path, "rb") as handle:
        content = handle.read()
    data = json.loads(content)
    _canonical_bytes(data)
    return data, hashlib.sha256(content).hexdigest()


def _reusable(directory, expected_files, fingerprint, identity_context):
    try:
        validate_import_path(directory)
        info = directory.lstat()
        if directory.name.startswith(".") or not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 2:
            return False
        manifest, _ = _read_json(directory / "import_manifest.json")
        if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
                or manifest["version"] != 1 or manifest.get("fingerprint") != fingerprint
                or _canonical_bytes(manifest.get("identity_context")) != _canonical_bytes(identity_context)
                or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != expected_files):
            return False
        # Derived reports/stats may coexist; all import material must be accounted for.
        material_names = {os.path.normcase(name) for name in _MATERIAL_NAMES}
        actual_files = {entry.name for entry in directory.iterdir()
                        if os.path.normcase(entry.name) in material_names}
        if actual_files != expected_files:
            return False
        materials = {}
        for name in expected_files:
            data, digest = _read_json(directory / name)
            if manifest["files"][name] != digest:
                return False
            materials[name] = data
        payload = materials.pop("messages.json")
        return fingerprint_import(payload, identity_context=manifest["identity_context"], sidecars=materials) == fingerprint
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def _bundle_at(directory):
    # Keep the native resolver unchanged; rebuild every path from the final location.
    return resolve_bundle_paths("", output=str(directory / "messages.json"))


def _prepared_payload(payload, directory):
    prepared = json.loads(_canonical_bytes(payload))
    prepared["bundle_dir"] = str(directory)
    return prepared


def save_import_bundle(payload, *, contact, contact_id, output_dir, identity_context, sidecars=None):
    """Publish a prepared, immutable bundle or reuse verified identical material.

    Existing files/directories, legacy bundles and tampered bundles are preserved.
    Only this call's private staging directory is removed on an uncommitted error.
    """
    fingerprint = fingerprint_import(payload, identity_context=identity_context, sidecars=sidecars)
    if not isinstance(contact, str) or (contact_id is not None and not isinstance(contact_id, str)):
        raise ImportStoreError("IMPORT_INVALID_INPUT")
    # Clone before writing to prevent mutation of caller payloads/sidecars/identity.
    material = json.loads(_canonical_bytes(_material(payload, identity_context, sidecars)))
    contacts = validate_import_path(output_dir)
    private = validated_private_root(contacts)
    expected_files = {"messages.json", *material["sidecars"]}
    staging = None
    try:
        with exclusive_import_lock(contacts):
            for candidate in sorted(contacts.iterdir()):
                if _reusable(candidate, expected_files, fingerprint, material["identity_context"]):
                    return {"payload": _prepared_payload(material["payload"], candidate),
                            "bundle": _bundle_at(candidate), "created": False, "reused": True}
            base = Path(resolve_bundle_paths(contact, contact_id or contact, output_dir=str(contacts))["bundle_dir"])
            target = base
            suffix = 0
            while os.path.lexists(target):
                suffix += 1
                tail = "" if suffix == 1 else f"_{suffix}"
                target = base.with_name(f"{base.name}__import_{fingerprint[:12]}{tail}")
            validate_import_path(target)
            prepared = _prepared_payload(material["payload"], target)
            staging = Path(tempfile.mkdtemp(prefix=".import-", dir=private))
            validate_import_path(staging)
            files = {"messages.json": _write_json(staging / "messages.json", prepared)}
            for name, sidecar in material["sidecars"].items():
                files[name] = _write_json(staging / name, {**sidecar, "bundle_dir": str(target)})
            _write_json(staging / "import_manifest.json", {
                "version": 1, "fingerprint": fingerprint,
                "identity_context": material["identity_context"], "files": files,
            })
            _fsync_directory(staging)
            validate_import_path(contacts)
            validate_import_path(private)
            if os.path.lexists(target):
                raise ImportStoreError("IMPORT_IO_ERROR")
            os.rename(staging, target)
            staging = None  # Commit: subsequent failure must never remove target.
            _fsync_directory(contacts)
            _fsync_directory(private)
            return {"payload": prepared, "bundle": _bundle_at(target), "created": True, "reused": False}
    except OSError as exc:
        raise ImportStoreError("IMPORT_IO_ERROR") from exc
    finally:
        if staging is not None:
            # Only our unique direct child, after validating the entire work path.
            validate_import_path(private)
            validate_import_path(staging)
            if staging.parent == private and staging.name.startswith(".import-"):
                shutil.rmtree(staging)
