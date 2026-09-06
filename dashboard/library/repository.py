"""Bounded SQLite store for user preferences, tags and module settings.

No source files are opened here. Callers supply public bundle summaries. Each
operation owns its connection and a byte-zero writer lock shared with backups.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


MAX_CONVERSATIONS = 20_000
MAX_TAGS = 1_000
MAX_TAGS_PER_CONVERSATION = 50
MAX_MODULES = 128
MAX_AUDIT_ROWS = 50_000
MAX_DATABASE_BYTES = 256 * 1024 * 1024
_PROCESS_LOCK = threading.RLock()


class LibraryValidationError(ValueError):
    """The submitted management fields are invalid."""


class LibraryConflictError(RuntimeError):
    """The submitted version is stale or another writer owns the store."""


class LibraryNotFoundError(LookupError):
    """The immutable identity is not present in the management store."""


# Append migrations; never edit an applied migration. Every statement is run
# individually inside the caller's explicit transaction (executescript commits).
_MIGRATIONS = ("""
CREATE TABLE conversations (
    bundle_id TEXT PRIMARY KEY,
    source_json TEXT NOT NULL,
    source_missing INTEGER NOT NULL DEFAULT 0 CHECK (source_missing IN (0, 1)),
    alias TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    hidden_at TEXT,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0)
);
CREATE TABLE tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0)
);
CREATE TABLE conversation_tags (
    bundle_id TEXT NOT NULL REFERENCES conversations(bundle_id),
    tag_id TEXT NOT NULL REFERENCES tags(id),
    PRIMARY KEY (bundle_id, tag_id)
);
CREATE TABLE module_settings (
    id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    version INTEGER NOT NULL CHECK (version >= 0)
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_id TEXT,
    fields_json TEXT NOT NULL,
    version INTEGER
);
""",)

_SOURCE_FIELDS = {
    "id", "contact", "source", "message_count", "date_range", "conversation_kind",
    "primary_topic", "candidate_topics", "has_stats", "has_sample", "has_analysis",
    "reports", "updated_at",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value, limit, *, empty=True, multiline=False, source_format=False):
    if not isinstance(value, str) or len(value) > limit:
        raise LibraryValidationError("文本类型或长度无效")
    for character in value:
        category = unicodedata.category(character)
        if source_format and category == "Cf":
            continue
        if category.startswith("C") and not (multiline and character in "\n\r\t"):
            raise LibraryValidationError("文本不能包含控制字符")
    if not empty and not value.strip():
        raise LibraryValidationError("名称不能为空")
    return value


def _bundle_id(value):
    _text(value, 255, empty=False, source_format=True)
    if value in {".", ".."} or value.startswith(".") or any(c in value for c in '/\\:*?"<>|'):
        raise LibraryValidationError("会话标识无效")
    return value


def _version(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise LibraryValidationError("版本号无效")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise LibraryValidationError("开关值必须为布尔值")
    return value


def _tag_id(value):
    _text(value, 36, empty=False)
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise LibraryValidationError("标签标识无效") from None
    return value


def _module_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise LibraryValidationError("模块标识无效")
    return value


def _color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise LibraryValidationError("标签颜色须为六位十六进制颜色")
    return value.lower()


def _patch(value, allowed):
    if not isinstance(value, dict) or not value or not set(value).issubset(allowed):
        raise LibraryValidationError("管理字段无效")


def _source(summary):
    if not isinstance(summary, dict) or not set(summary).issubset(_SOURCE_FIELDS):
        raise LibraryValidationError("会话摘要字段无效")
    _bundle_id(summary.get("id"))
    _text(summary.get("contact"), 512, empty=False, source_format=True)
    for key in ("source", "conversation_kind", "primary_topic", "updated_at"):
        if key in summary and summary[key] is not None:
            _text(summary[key], 128)
    for key in ("has_stats", "has_sample", "has_analysis"):
        if key in summary:
            _boolean(summary[key])
    if "message_count" in summary:
        count = summary["message_count"]
        if type(count) is not int or not 0 <= count <= 1_000_000_000:
            raise LibraryValidationError("会话计数无效")
    dates = summary.get("date_range")
    if dates is not None:
        if not isinstance(dates, list) or len(dates) != 2:
            raise LibraryValidationError("摘要日期范围无效")
        try:
            if any(type(value) is not str or len(value) != 10 or date.fromisoformat(value).isoformat() != value for value in dates) or dates[0] > dates[1]:
                raise ValueError
        except ValueError:
            raise LibraryValidationError("摘要日期范围无效") from None
    candidates = summary.get("candidate_topics", [])
    if not isinstance(candidates, list) or len(candidates) > 32:
        raise LibraryValidationError("摘要分类无效")
    for value in candidates:
        _text(value, 64)
    reports = summary.get("reports", [])
    if not isinstance(reports, list) or len(reports) > 100:
        raise LibraryValidationError("摘要报告列表无效")
    for report in reports:
        if not isinstance(report, dict) or set(report) != {"name", "url", "updated_at"}:
            raise LibraryValidationError("摘要报告字段无效")
        _text(report["name"], 120)
        _text(report["updated_at"], 64)
        if not isinstance(report["url"], str) or not re.fullmatch(r"/reports/[A-Za-z0-9_-]{1,128}", report["url"]):
            raise LibraryValidationError("摘要报告地址无效")
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise LibraryValidationError("会话摘要过大")
    return encoded


def _guard(path: Path, *, directory=False, missing=False):
    """Check every ancestor, including the input data root, before filesystem I/O."""
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise RuntimeError("本地资料库目录不可用") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("本地资料库路径不安全")
        if component != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("本地资料库目录不可用")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("本地资料库文件不安全")


def _mkdir(path):
    for component in (*reversed(path.parents), path):
        _guard(component, directory=True, missing=True)
        if not component.exists():
            try:
                component.mkdir(mode=0o700)
            except FileExistsError:
                pass
        _guard(component, directory=True)


@contextmanager
def _writer_lock(root):
    path = root / "writer.lock"
    _guard(path, missing=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        actual = os.fstat(handle.fileno())
        _guard(path)
        expected = path.lstat()
        if actual.st_nlink != 1 or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("本地资料库锁文件不安全")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LibraryConflictError("本地资料库正忙，请稍后重试") from None
        try:
            if not actual.st_size:
                handle.write(b"\0")
                handle.flush()
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _migrate(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    known_tables = {"schema_migrations", "conversations", "tags", "conversation_tags", "module_settings", "audit_log"}
    if tables - known_tables or connection.execute("SELECT 1 FROM sqlite_master WHERE type != 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        raise RuntimeError("本地资料库结构不兼容")
    if version > len(_MIGRATIONS) or ("schema_migrations" not in tables and (tables or version)):
        raise RuntimeError("本地资料库版本不兼容")
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    if [row[0] for row in applied] != list(range(1, version + 1)):
        raise RuntimeError("本地资料库迁移记录无效")
    for number, checksum in applied:
        if checksum != hashlib.sha256(_MIGRATIONS[number - 1].encode("utf-8")).hexdigest():
            raise RuntimeError("本地资料库迁移校验失败")
    for index in range(version, len(_MIGRATIONS)):
        migration = _MIGRATIONS[index]
        for statement in migration.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                           (index + 1, hashlib.sha256(migration.encode("utf-8")).hexdigest(), _now()))
        connection.execute(f"PRAGMA user_version={index + 1}")


def _audit(connection, action, entity_id=None, fields=(), version=None):
    connection.execute("INSERT INTO audit_log (occurred_at, action, entity_id, fields_json, version) VALUES (?, ?, ?, ?, ?)",
                       (_now(), action, entity_id, json.dumps(sorted(fields)), version))
    connection.execute("DELETE FROM audit_log WHERE id <= (SELECT MAX(id) - ? FROM audit_log)", (MAX_AUDIT_ROWS,))


def _conversation(connection, bundle_id):
    row = connection.execute("SELECT * FROM conversations WHERE bundle_id=?", (bundle_id,)).fetchone()
    if row is None:
        raise LibraryNotFoundError("会话不存在")
    return {
        "bundle_id": row["bundle_id"], "source": json.loads(row["source_json"]),
        "source_missing": bool(row["source_missing"]), "alias": row["alias"], "note": row["note"],
        "pinned": bool(row["pinned"]), "hidden_at": row["hidden_at"], "version": row["version"],
        "tags": [item[0] for item in connection.execute("SELECT tag_id FROM conversation_tags WHERE bundle_id=? ORDER BY tag_id", (bundle_id,))],
    }


def _tag(connection, tag_id):
    row = connection.execute("SELECT id, name, color, deleted, version FROM tags WHERE id=?", (tag_id,)).fetchone()
    if row is None:
        raise LibraryNotFoundError("标签不存在")
    return {**dict(row), "deleted": bool(row["deleted"])}


def _settings(connection):
    return {row["id"]: {"enabled": bool(row["enabled"]), "version": row["version"]}
            for row in connection.execute("SELECT * FROM module_settings ORDER BY id")}


def _health(connection):
    counts = connection.execute("SELECT COUNT(*), COALESCE(SUM(source_missing),0), COALESCE(SUM(pinned),0), COUNT(hidden_at) FROM conversations").fetchone()
    return {
        "status": "ready", "schema_version": len(_MIGRATIONS),
        "conversation_count": counts[0], "missing_source_count": counts[1],
        "pinned_count": counts[2], "hidden_count": counts[3],
        "tag_count": connection.execute("SELECT COUNT(*) FROM tags WHERE deleted=0").fetchone()[0],
        "deleted_tag_count": connection.execute("SELECT COUNT(*) FROM tags WHERE deleted=1").fetchone()[0],
        "module_setting_count": connection.execute("SELECT COUNT(*) FROM module_settings").fetchone()[0],
        "audit_count": connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
    }


class LibraryRepository:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).absolute()
        self.root = self.data_dir / "private" / "library"
        self.db_path = self.root / "library.sqlite3"

    def _check_files(self):
        _guard(self.root, directory=True)
        for name in ("library.sqlite3", "library.sqlite3-wal", "library.sqlite3-shm", "library.sqlite3-journal", "writer.lock"):
            _guard(self.root / name, missing=True)
        if self.db_path.exists() and self.db_path.stat().st_size > MAX_DATABASE_BYTES:
            raise RuntimeError("本地资料库超过容量限制")

    @contextmanager
    def _session(self):
        connection = None
        # Browser requests share one process; serialize them before attempting
        # the nonblocking cross-process lock used by backups and other servers.
        _PROCESS_LOCK.acquire()
        try:
            _mkdir(self.root)
            self._check_files()
            with _writer_lock(self.root):
                self._check_files()
                connection = sqlite3.connect(self.db_path, timeout=0, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                connection.execute(f"PRAGMA max_page_count={max(1, MAX_DATABASE_BYTES // page_size)}")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _migrate(connection)
                    self._check_files()
                    yield connection
                    self._check_files()
                    connection.commit()
                finally:
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()
                    connection = None
        except sqlite3.IntegrityError:
            raise LibraryConflictError("管理数据已存在或发生冲突") from None
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise LibraryConflictError("本地资料库正忙，请稍后重试") from None
            raise RuntimeError("本地资料库操作失败") from None
        except (sqlite3.Error, OSError):
            raise RuntimeError("本地资料库不可用") from None
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                _PROCESS_LOCK.release()

    def reconcile(self, summaries):
        if not isinstance(summaries, list) or len(summaries) > MAX_CONVERSATIONS:
            raise LibraryValidationError("会话摘要数量无效")
        sources = {}
        for summary in summaries:
            encoded = _source(summary)
            bundle_id = summary["id"]
            if bundle_id in sources:
                raise LibraryValidationError("会话标识重复")
            sources[bundle_id] = encoded
        with self._session() as connection:
            existing = {row["bundle_id"]: row for row in connection.execute("SELECT bundle_id, source_json, source_missing FROM conversations")}
            if len(set(existing) | set(sources)) > MAX_CONVERSATIONS:
                raise LibraryValidationError("会话数量达到上限")
            changed = False
            for bundle_id, encoded in sources.items():
                previous = existing.get(bundle_id)
                if previous is None:
                    connection.execute("INSERT INTO conversations (bundle_id, source_json) VALUES (?, ?)", (bundle_id, encoded))
                    changed = True
                elif previous["source_json"] != encoded or previous["source_missing"]:
                    connection.execute("UPDATE conversations SET source_json=?, source_missing=0 WHERE bundle_id=?", (encoded, bundle_id))
                    changed = True
            for bundle_id, previous in existing.items():
                if bundle_id not in sources and not previous["source_missing"]:
                    connection.execute("UPDATE conversations SET source_missing=1 WHERE bundle_id=?", (bundle_id,))
                    changed = True
            if changed:
                _audit(connection, "source.reconcile", fields=("source", "source_missing"))

    def snapshot(self):
        with self._session() as connection:
            return {
                "conversations": [_conversation(connection, row[0]) for row in connection.execute("SELECT bundle_id FROM conversations ORDER BY bundle_id")],
                "tags": [_tag(connection, row[0]) for row in connection.execute("SELECT id FROM tags ORDER BY name_key, id")],
                "health": _health(connection),
            }

    def update_conversation(self, bundle_id, expected_version, patch):
        _bundle_id(bundle_id)
        _version(expected_version)
        _patch(patch, {"alias", "note", "pinned", "hidden", "tag_ids"})
        if "alias" in patch:
            _text(patch["alias"], 120)
        if "note" in patch:
            _text(patch["note"], 4000, multiline=True)
        for key in ("pinned", "hidden"):
            if key in patch:
                _boolean(patch[key])
        tag_ids = patch.get("tag_ids")
        if "tag_ids" in patch:
            if not isinstance(tag_ids, list) or len(tag_ids) > MAX_TAGS_PER_CONVERSATION:
                raise LibraryValidationError("会话标签数量无效")
            for tag_id in tag_ids:
                _tag_id(tag_id)
            if len(set(tag_ids)) != len(tag_ids):
                raise LibraryValidationError("会话标签重复")
        with self._session() as connection:
            row = _conversation(connection, bundle_id)
            if row["version"] != expected_version:
                raise LibraryConflictError("会话已被修改，请刷新后重试")
            if tag_ids is not None:
                for tag_id in tag_ids:
                    tag = connection.execute("SELECT deleted FROM tags WHERE id=?", (tag_id,)).fetchone()
                    if tag is None or tag[0]:
                        raise LibraryValidationError("标签不存在或已停用")
            hidden_at = row["hidden_at"]
            if "hidden" in patch:
                hidden_at = (hidden_at or _now()) if patch["hidden"] else None
            connection.execute("UPDATE conversations SET alias=?, note=?, pinned=?, hidden_at=?, version=version+1 WHERE bundle_id=?",
                               (patch.get("alias", row["alias"]), patch.get("note", row["note"]),
                                int(patch.get("pinned", row["pinned"])), hidden_at, bundle_id))
            if tag_ids is not None:
                # Clients edit only visible active choices. Keep tombstoned
                # memberships so restoring a tag also restores its assignments.
                connection.execute("DELETE FROM conversation_tags WHERE bundle_id=? AND tag_id IN (SELECT id FROM tags WHERE deleted=0)", (bundle_id,))
                connection.executemany("INSERT INTO conversation_tags VALUES (?, ?)", [(bundle_id, tag_id) for tag_id in tag_ids])
            _audit(connection, "conversation.update", bundle_id, patch, expected_version + 1)
            return _conversation(connection, bundle_id)

    def create_tag(self, name, color):
        name = _text(name, 64, empty=False).strip()
        color = _color(color)
        name_key = unicodedata.normalize("NFKC", name).casefold()
        with self._session() as connection:
            if connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] >= MAX_TAGS:
                raise LibraryValidationError("标签数量达到上限")
            tag_id = str(uuid.uuid4())
            connection.execute("INSERT INTO tags (id, name, name_key, color) VALUES (?, ?, ?, ?)", (tag_id, name, name_key, color))
            _audit(connection, "tag.create", tag_id, ("name", "color"), 0)
            return _tag(connection, tag_id)

    def update_tag(self, tag_id, expected_version, patch):
        _tag_id(tag_id)
        _version(expected_version)
        _patch(patch, {"name", "color", "deleted"})
        if "name" in patch:
            _text(patch["name"], 64, empty=False)
        if "color" in patch:
            _color(patch["color"])
        if "deleted" in patch:
            _boolean(patch["deleted"])
        with self._session() as connection:
            row = _tag(connection, tag_id)
            if row["version"] != expected_version:
                raise LibraryConflictError("标签已被修改，请刷新后重试")
            name = patch.get("name", row["name"]).strip()
            connection.execute("UPDATE tags SET name=?, name_key=?, color=?, deleted=?, version=version+1 WHERE id=?",
                               (name, unicodedata.normalize("NFKC", name).casefold(), _color(patch.get("color", row["color"])),
                                int(patch.get("deleted", row["deleted"])), tag_id))
            _audit(connection, "tag.update", tag_id, patch, expected_version + 1)
            return _tag(connection, tag_id)

    def module_settings(self):
        with self._session() as connection:
            return _settings(connection)

    def update_module(self, module_id, enabled, expected_version, *, expected_settings=None):
        _module_id(module_id)
        _boolean(enabled)
        _version(expected_version)
        if expected_settings is not None:
            if not isinstance(expected_settings, dict) or len(expected_settings) > MAX_MODULES:
                raise LibraryValidationError("模块配置快照无效")
            for setting_id, setting in expected_settings.items():
                _module_id(setting_id)
                if not isinstance(setting, dict) or set(setting) != {"enabled", "version"}:
                    raise LibraryValidationError("模块配置快照无效")
                _boolean(setting["enabled"])
                _version(setting["version"])
        with self._session() as connection:
            settings = _settings(connection)
            if expected_settings is not None and settings != expected_settings:
                raise LibraryConflictError("模块配置已改变，请刷新后重试")
            previous = settings.get(module_id)
            if (previous["version"] if previous else 0) != expected_version:
                raise LibraryConflictError("模块已被修改，请刷新后重试")
            if previous is None and len(settings) >= MAX_MODULES:
                raise LibraryValidationError("模块配置数量达到上限")
            connection.execute("INSERT INTO module_settings (id, enabled, version) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, version=excluded.version",
                               (module_id, int(enabled), expected_version + 1))
            _audit(connection, "module.update", module_id, ("enabled",), expected_version + 1)
            return {"id": module_id, "enabled": enabled, "version": expected_version + 1}

    def health(self):
        with self._session() as connection:
            return _health(connection)
