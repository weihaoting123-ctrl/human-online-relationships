"""Idempotently merge normalized chat bundles without touching source data.

The module deliberately separates immutable raw-export archiving from the
mutable, derived ``messages.json`` view.  Raw exports are stored by SHA-256 and
are never moved, deleted, or overwritten.  Existing derived bundles are
snapshotted before an atomic replacement.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from message_normalizer import (
    LOCAL_ID_UNVERIFIED as _LOCAL_ID_UNVERIFIED,
    LOCAL_ID_VERIFIED as _LOCAL_ID_VERIFIED,
    normalize_payload,
)


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


MERGE_FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024

# Ordered from the most portable server-side IDs to exporter-local IDs.  A
# normalized ``local_id`` is intentionally absent: normalizers may synthesize
# it from the array index, which is not a durable identity.
STABLE_ID_FIELDS = (
    ("source_message_id", "source_message_id"),
    ("serverId", "server_id"),
    ("server_id", "server_id"),
    ("msgSvrId", "msg_svr_id"),
    ("msg_svr_id", "msg_svr_id"),
    ("messageId", "message_id"),
    ("message_id", "message_id"),
    ("localId", "local_id"),
)

_IDENTITY_FIELDS = frozenset(name for name, _kind in STABLE_ID_FIELDS)
_SOURCE_PROOF_FIELDS = _IDENTITY_FIELDS | {
    "local_id", "content", "sender", "timestamp", "type", "local_type",
    "source_message_id_kind", "source_message_namespace", "source_namespace",
    "source_partition", "sourcePartition",
    _LOCAL_ID_UNVERIFIED,
    _LOCAL_ID_VERIFIED,
}
_BUNDLE_IDENTITY_FIELDS = frozenset({"own_wxid", "contact_username"})
_FINGERPRINT_IGNORED_FIELDS = _IDENTITY_FIELDS | {
    "local_id",
    "source_message_id_kind",
    "source_message_namespace",
    "source_namespace",
    "sourcePartition",
    "source_partition",
    "dedupe",
    "merge",
    _LOCAL_ID_UNVERIFIED,
    _LOCAL_ID_VERIFIED,
}
_EMPTY_ID_VALUES = (None, "", 0, "0")
_SERVER_STABLE_ID_KINDS = frozenset({"server_id", "msg_svr_id", "message_id"})
_OPAQUE_PARTITION_PATTERN = re.compile(r"p_[0-9a-f]{16,64}", re.IGNORECASE)


class BundleIdentityError(ValueError):
    """Raised when two payloads belong to different contacts or accounts."""


class ConcurrentMergeError(RuntimeError):
    """Raised when another process is already updating the same bundle."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _present(value: Any) -> bool:
    return value not in (None, "", [], {}, "unknown")


def _valid_source_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return value not in _EMPTY_ID_VALUES


def _source_namespace(message: dict[str, Any], payload_source: Any) -> str:
    value = (
        message.get("source_message_namespace")
        or message.get("source_namespace")
        or payload_source
        or "unknown"
    )
    return str(value).strip().casefold() or "unknown"


def normalize_source_partition(value: Any) -> str | None:
    """Return a deterministic opaque partition token without retaining input text."""

    if value is None or value == "":
        return None
    if isinstance(value, str):
        material = value.strip()
        if not material:
            return None
        if _OPAQUE_PARTITION_PATTERN.fullmatch(material):
            return material.casefold()
    else:
        material = _canonical_json(value)
    return "p_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _source_partition(message: dict[str, Any], payload_partition: Any = None) -> str | None:
    for value in (
        message.get("source_partition"),
        message.get("sourcePartition"),
        payload_partition,
    ):
        partition = normalize_source_partition(value)
        if partition is not None:
            return partition
    return None


def stable_message_identity(
    message: dict[str, Any], payload_source: Any = None, payload_partition: Any = None
) -> tuple[str, str, str, str] | None:
    """Return a namespaced upstream identity, never a synthesized array ID."""

    for field, default_kind in STABLE_ID_FIELDS:
        value = message.get(field)
        if not _valid_source_id(value):
            continue
        kind = (
            message.get("source_message_id_kind")
            if field == "source_message_id"
            else default_kind
        )
        kind = str(kind or default_kind).strip().casefold()
        if kind in _SERVER_STABLE_ID_KINDS:
            identity_scope = "global"
        else:
            # Exporter-local IDs can repeat in MSG database shards.  With no
            # partition proof, treat the record as ID-less and fall back to a
            # conservative content fingerprint instead of risking data loss.
            partition = _source_partition(message, payload_partition)
            if partition is None:
                continue
            identity_scope = partition
        return (
            _source_namespace(message, payload_source),
            kind,
            identity_scope,
            str(value).strip(),
        )
    return None


def conservative_message_fingerprint(
    message: dict[str, Any], payload_source: Any = None, payload_partition: Any = None
) -> str:
    """Hash all durable message fields when no upstream ID is available.

    The source namespace is included to avoid collapsing records produced by
    unrelated exporters.  Occurrence counts are handled by ``merge_payloads``;
    two legitimate identical messages in one snapshot therefore stay as two
    messages instead of being reduced to one.
    """

    durable = {
        key: value
        for key, value in message.items()
        if key not in _FINGERPRINT_IGNORED_FIELDS
    }
    material = {
        "source_namespace": _source_namespace(message, payload_source),
        "source_partition": _source_partition(message, payload_partition) or "p_unknown",
        "message": durable,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _message_content_digest(message: dict[str, Any]) -> str:
    durable = {
        key: value
        for key, value in message.items()
        if key not in _FINGERPRINT_IGNORED_FIELDS
    }
    return hashlib.sha256(_canonical_json(durable).encode("utf-8")).hexdigest()


def _source_content_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Separate source disagreement from harmless transcript/metadata enrichment."""
    return left.get("content") != right.get("content") or any(
        _present(left.get(key)) and _present(right.get(key)) and left[key] != right[key]
        for key in ("sender", "timestamp", "type", "local_type"))


def _incoming_source_signature(message, payload_partition):
    """Strict equality for candidate copies; unknown/missing is not agreement."""
    return (tuple(message.get(key) for key in
                  ("content", "sender", "timestamp", "type", "local_type", "local_id")),
            _source_partition(message, payload_partition),
            message.get(_LOCAL_ID_VERIFIED) is True,
            message.get(_LOCAL_ID_UNVERIFIED) is True)


def _verified_local_text_source(existing, incoming, prior, message, partitions, blocked):
    """Match a fresh, explicitly proven row to the retained source identity."""
    identity = stable_message_identity(message, incoming.get("source"))
    partition = _source_partition(message)
    return bool(
        partitions and identity and identity not in blocked
        and identity[1] in _SERVER_STABLE_ID_KINDS
        and identity[0] == "weflow-cli"
        and partition in partitions and _source_partition(prior) == partition
        and prior.get(_LOCAL_ID_UNVERIFIED) is not True
        and message.get(_LOCAL_ID_UNVERIFIED) is not True
        and message.get(_LOCAL_ID_VERIFIED) is True
        and all(_known_identity(existing.get(key)) is not None
                and _known_identity(existing.get(key)) == _known_identity(incoming.get(key))
                for key in ("own_wxid", "contact_username"))
        and prior.get("type") == message.get("type") == "text"
        and all(prior.get(key) == message.get(key)
                for key in ("sender", "timestamp", "local_type", "local_id"))
    )


def _authoritative_text_update(existing, incoming, prior, message, partitions, blocked):
    """Allow corrections only when BOTH rows have positive local-row proof.

    A legacy normalized local_id can be a synthesized array index. Conflicting
    text never backfills that proof, even after multiple attempted merges.
    """
    return bool(prior.get(_LOCAL_ID_VERIFIED) is True
                and isinstance(message.get("content"), str) and message["content"]
                and _verified_local_text_source(existing, incoming, prior, message,
                                                partitions, blocked))


def _add_source_context(
    message: dict[str, Any], payload_source: Any, payload_partition: Any = None
) -> dict[str, Any]:
    item = copy.deepcopy(message)
    if not _present(item.get("source_namespace")) and _present(payload_source):
        item["source_namespace"] = str(payload_source)
    partition = _source_partition(item, payload_partition)
    item.pop("sourcePartition", None)
    if partition is None:
        item.pop("source_partition", None)
    else:
        item["source_partition"] = partition
    return item


def _enrich_without_overwrite(target: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Enrich derived fields, never manufacture missing proof from a duplicate.

    A stable server ID can occur in multiple shards. Missing provenance stays
    missing even for equal/blank content; otherwise a rejected first merge could
    grant authority to replace historical text during the next merge.
    """
    changed = False
    for key, value in incoming.items():
        if key in _SOURCE_PROOF_FIELDS or key in {"merge", "dedupe"} or not _present(value):
            continue
        if not _present(target.get(key)):
            target[key] = copy.deepcopy(value)
            changed = True
    return changed


def _known_identity(value: Any) -> str | None:
    if not _present(value):
        return None
    return str(value).strip()


def _validate_bundle_identity(
    existing: dict[str, Any] | None, incoming: dict[str, Any]
) -> None:
    if existing is None:
        return
    for field in ("contact_username", "own_wxid"):
        left = _known_identity(existing.get(field))
        right = _known_identity(incoming.get(field))
        if left is not None and right is not None and left != right:
            raise BundleIdentityError(f"bundle_{field}_mismatch")


def _normalized_strict(payload: dict[str, Any]) -> dict[str, Any]:
    # Strict normalization prevents a merge from silently removing malformed
    # messages.  Export adapters may make their own explicit drop decision
    # before calling this module, while the immutable raw export remains saved.
    return normalize_payload(copy.deepcopy(payload), drop_invalid=False)


def _merged_top_level(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    result = copy.deepcopy(existing or incoming)
    for key, value in incoming.items():
        # A populated legacy bundle with unknown identity cannot establish its
        # historical owner/contact merely by accepting metadata from this batch.
        if existing and existing["messages"] and key in _BUNDLE_IDENTITY_FIELDS:
            continue
        if key in {
            "messages", "total", "normalization", "merge", "sources",
            "sourcePartition", "source_partition", "source_partitions",
        }:
            continue
        if not _present(result.get(key)) and _present(value):
            result[key] = copy.deepcopy(value)

    sources = set()
    for payload in (existing or {}, incoming):
        source = payload.get("source")
        if _present(source):
            sources.add(str(source))
        for item in payload.get("sources") or []:
            if _present(item):
                sources.add(str(item))
    if sources:
        result["sources"] = sorted(sources)

    # A bundle may span multiple database shards.  Keep only opaque tokens and
    # never retain a raw database path, filename, account, or session value.
    result.pop("sourcePartition", None)
    result.pop("source_partition", None)
    partitions = sorted({
        item["source_partition"]
        for item in messages
        if _present(item.get("source_partition"))
    })
    if partitions:
        result["source_partitions"] = partitions
    else:
        result.pop("source_partitions", None)

    messages.sort(key=lambda item: (item["timestamp"], str(item.get("local_id", ""))))
    result["messages"] = messages
    result["total"] = len(messages)
    result["normalization"] = {
        "timestamp_unit": "seconds",
        "dropped_messages": 0,
        "warnings": [],
    }
    result["merge"] = {
        "format_version": MERGE_FORMAT_VERSION,
        "strategy": "upstream-id-then-conservative-multiset-fingerprint",
    }
    return result


def merge_payloads(
    existing_payload: dict[str, Any] | None,
    incoming_payload: dict[str, Any],
    *, authoritative_source_partitions=(),
) -> tuple[dict[str, Any], dict[str, int]]:
    """Merge normalized payloads and return the result plus body-free counts."""

    if not isinstance(incoming_payload, dict):
        raise ValueError("incoming_payload_not_object")
    if existing_payload is not None and not isinstance(existing_payload, dict):
        raise ValueError("existing_payload_not_object")

    _validate_bundle_identity(existing_payload, incoming_payload)
    incoming = _normalized_strict(incoming_payload)
    existing = _normalized_strict(existing_payload) if existing_payload is not None else None

    existing_messages = existing["messages"] if existing else []
    incoming_messages = incoming["messages"]
    counts = {
        "existing_before": len(existing_messages),
        "incoming": len(incoming_messages),
        "added": 0,
        "duplicates": 0,
        "stable_id_duplicates": 0,
        "fingerprint_duplicates": 0,
        "stable_id_conflicts": 0,
        "source_content_conflicts": 0,
        "updated": 0,
        "enriched": 0,
        "existing_stable_duplicates_removed": 0,
        "total_after": 0,
    }

    output: list[dict[str, Any]] = []
    stable_index: dict[tuple[str, str, str, str], int] = {}
    fallback_counts: Counter[str] = Counter()
    existing_source = existing.get("source") if existing else None
    existing_partition = (
        existing.get("source_partition", existing.get("sourcePartition"))
        if existing else None
    )
    authoritative_partitions = frozenset(authoritative_source_partitions)
    if any(not isinstance(value, str) or not _OPAQUE_PARTITION_PATTERN.fullmatch(value)
           for value in authoritative_partitions):
        raise ValueError("authoritative_source_partition_invalid")
    blocked_updates = set()
    incoming_source = incoming.get("source")
    incoming_partition = incoming.get("source_partition", incoming.get("sourcePartition"))
    incoming_source_signatures = {}
    for message in incoming_messages:
        identity = stable_message_identity(message, incoming_source, incoming_partition)
        if identity:
            signature = _incoming_source_signature(message, incoming_partition)
            if incoming_source_signatures.setdefault(identity, signature) != signature:
                blocked_updates.add(identity)

    # Preserve all existing fallback records.  Without an upstream identity we
    # cannot prove that two same-looking historical messages are duplicates.
    # Existing duplicate stable IDs, however, are safe to compact.
    for raw_message in existing_messages:
        message = _add_source_context(raw_message, existing_source, existing_partition)
        stable_id = stable_message_identity(message, existing_source, existing_partition)
        if stable_id is None:
            output.append(message)
            fallback_counts[
                conservative_message_fingerprint(message, existing_source, existing_partition)
            ] += 1
            continue
        prior_index = stable_index.get(stable_id)
        if prior_index is None:
            stable_index[stable_id] = len(output)
            output.append(message)
            continue
        prior = output[prior_index]
        if _message_content_digest(prior) != _message_content_digest(message):
            counts["stable_id_conflicts"] += 1
        if _source_content_conflict(prior, message):
            counts["source_content_conflicts"] += 1
            blocked_updates.add(stable_id)
        if _enrich_without_overwrite(prior, message):
            counts["enriched"] += 1
        counts["existing_stable_duplicates_removed"] += 1

    incoming_fallback_occurrences: Counter[str] = Counter()
    for raw_message in incoming_messages:
        message = _add_source_context(raw_message, incoming_source, incoming_partition)
        stable_id = stable_message_identity(message, incoming_source, incoming_partition)
        if stable_id is not None:
            prior_index = stable_index.get(stable_id)
            if prior_index is not None:
                prior = output[prior_index]
                source_conflict = _source_content_conflict(prior, message)
                proof_enriched = False
                if _message_content_digest(prior) != _message_content_digest(message):
                    counts["stable_id_conflicts"] += 1
                if source_conflict:
                    if _authoritative_text_update(existing or {}, incoming, prior, message,
                                                  authoritative_partitions, blocked_updates):
                        prior["content"] = message["content"]
                        counts["updated"] += 1
                        # Keep transcription and enrich only derived fields;
                        # existing source proof remains immutable.
                        if _enrich_without_overwrite(prior, message):
                            counts["enriched"] += 1
                        continue
                    counts["source_content_conflicts"] += 1
                elif (prior.get(_LOCAL_ID_VERIFIED) is not True
                      and message.get("content")
                      and _verified_local_text_source(existing or {}, incoming, prior, message,
                                                      authoritative_partitions, blocked_updates)):
                    # Exact nonempty fresh text plus all bound source fields
                    # may certify an unmarked legacy row. Missing/negative
                    # partition, account or local-row evidence cannot upgrade.
                    prior[_LOCAL_ID_VERIFIED] = True
                    proof_enriched = True
                if _enrich_without_overwrite(prior, message) or proof_enriched:
                    counts["enriched"] += 1
                counts["duplicates"] += 1
                counts["stable_id_duplicates"] += 1
                continue
            stable_index[stable_id] = len(output)
            output.append(message)
            counts["added"] += 1
            continue

        fingerprint = conservative_message_fingerprint(
            message, incoming_source, incoming_partition
        )
        incoming_fallback_occurrences[fingerprint] += 1
        occurrence = incoming_fallback_occurrences[fingerprint]
        if occurrence <= fallback_counts[fingerprint]:
            counts["duplicates"] += 1
            counts["fingerprint_duplicates"] += 1
            continue
        output.append(message)
        fallback_counts[fingerprint] += 1
        counts["added"] += 1

    counts["total_after"] = len(output)
    merged = _merged_top_level(existing, incoming, output)
    return merged, counts


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("xb") as handle:
            created = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # Only an incomplete file created by this call may be removed.  The
        # source export and every pre-existing archive remain untouched.
        if created and path.exists():
            path.unlink()
        raise


def _copy_file_exclusive(source: Path, destination: Path) -> None:
    created = False
    try:
        with destination.open("xb") as writer:
            created = True
            with source.open("rb") as reader:
                shutil.copyfileobj(reader, writer, length=CHUNK_SIZE)
            writer.flush()
            os.fsync(writer.fileno())
    except Exception:
        if created and destination.exists():
            destination.unlink()
        raise


def _preserve_existing_snapshot(messages_path: Path, history_dir: Path) -> bool:
    if not messages_path.exists():
        return False
    data = messages_path.read_bytes()
    digest = _sha256_bytes(data)
    destination = history_dir / f"messages-{digest}.json"
    if destination.exists():
        if _sha256_file(destination) != digest:
            raise OSError("history_digest_collision")
        return False
    _write_exclusive(destination, data)
    return True


def _atomic_write(path: Path, data: bytes) -> None:
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


@contextmanager
def _exclusive_bundle_lock(messages_path: Path):
    lock_path = messages_path.with_name(f".{messages_path.name}.merge.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # The file is only a stable lock target.  Ownership lives in the operating
    # system, so a crash or forced process termination releases it automatically
    # and cannot leave an O_EXCL sentinel that blocks every future sync.
    handle = lock_path.open("a+b", buffering=0)
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ConcurrentMergeError("bundle_merge_already_running") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ConcurrentMergeError("bundle_merge_already_running") from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def archive_raw_export(source_path: os.PathLike[str] | str, archive_dir: os.PathLike[str] | str) -> dict[str, Any]:
    """Copy a raw export into a content-addressed immutable local archive."""

    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError("raw_export_not_found")
    archive_root = Path(archive_dir).resolve()
    archive_root.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".raw-export-", suffix=".part", dir=str(archive_root)
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with os.fdopen(descriptor, "wb") as writer:
            with source.open("rb") as reader:
                while True:
                    chunk = reader.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    writer.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())

        sha256 = digest.hexdigest()
        suffix = source.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".bin"
        destination = archive_root / sha256[:2] / f"{sha256}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            os.link(temporary, destination)
            created = True
        except FileExistsError:
            pass
        except OSError:
            try:
                _copy_file_exclusive(temporary, destination)
                created = True
            except FileExistsError:
                pass

        if _sha256_file(destination) != sha256:
            if created:
                destination.unlink()
            raise OSError("raw_archive_digest_mismatch")
        return {
            "path": str(destination),
            "sha256": sha256,
            "bytes": byte_count,
            "created": created,
        }
    finally:
        if temporary.exists():
            temporary.unlink()


def merge_into_bundle(
    incoming_payload: dict[str, Any],
    messages_path: os.PathLike[str] | str,
    *,
    raw_export_path: os.PathLike[str] | str | None = None,
    raw_archive_dir: os.PathLike[str] | str | None = None,
    history_dir: os.PathLike[str] | str | None = None,
    authoritative_source_partitions=(),
) -> dict[str, Any]:
    """Archive the raw input and atomically update one derived message bundle."""

    if (raw_export_path is None) != (raw_archive_dir is None):
        raise ValueError("raw_export_and_archive_dir_must_be_used_together")

    target = Path(messages_path).resolve()
    history = Path(history_dir).resolve() if history_dir else target.parent / ".history"
    raw_archive = None

    with _exclusive_bundle_lock(target):
        existing_bytes = target.read_bytes() if target.exists() else None
        existing_payload = (
            json.loads(existing_bytes.decode("utf-8-sig"))
            if existing_bytes is not None
            else None
        )
        merged, counts = merge_payloads(existing_payload, incoming_payload,
                                       authoritative_source_partitions=authoritative_source_partitions)

        # Validate and merge first.  A contact/account mismatch must not create
        # a misleading archive association with this bundle.
        if raw_export_path is not None and raw_archive_dir is not None:
            raw_archive = archive_raw_export(raw_export_path, raw_archive_dir)

        merged_bytes = _json_bytes(merged)
        written = existing_bytes != merged_bytes
        history_created = False
        if written:
            history_created = _preserve_existing_snapshot(target, history)
            _atomic_write(target, merged_bytes)

        counts = dict(counts)
        counts.update({
            "written": int(written),
            "history_snapshots_created": int(history_created),
            "raw_archives_created": int(bool(raw_archive and raw_archive["created"])),
            "raw_archives_reused": int(bool(raw_archive and not raw_archive["created"])),
        })
        return {
            "counts": counts,
            "messages_path": str(target),
            "raw_archive": raw_archive,
        }


def _error_code(exc: Exception) -> str:
    if isinstance(exc, BundleIdentityError):
        return "bundle_identity_mismatch"
    if isinstance(exc, ConcurrentMergeError):
        return "bundle_busy"
    if isinstance(exc, FileNotFoundError):
        return "input_not_found"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, ValueError):
        return "invalid_input"
    return "local_io_error"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="幂等合并本地聊天包（不修改或删除原始导出）"
    )
    parser.add_argument("--input", required=True, help="已标准化的消息 JSON")
    parser.add_argument("--output", required=True, help="目标 messages.json")
    parser.add_argument("--raw-export", help="可选：需保存的原始导出")
    parser.add_argument("--raw-archive-dir", help="可选：原始导出私有归档目录")
    args = parser.parse_args()

    try:
        with open(args.input, encoding="utf-8-sig") as handle:
            incoming = json.load(handle)
        result = merge_into_bundle(
            incoming,
            args.output,
            raw_export_path=args.raw_export,
            raw_archive_dir=args.raw_archive_dir,
        )
        # Deliberately exclude message bodies, contact/account identifiers, raw
        # paths, and upstream message IDs from process output.
        print(json.dumps({"status": "ok", "counts": result["counts"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"status": "error", "error_code": _error_code(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
