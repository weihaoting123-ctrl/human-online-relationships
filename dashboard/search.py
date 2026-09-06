"""Bounded, local-only derived message index. Never return or log message text.

The private SQLite file contains normalized text and must not be shared. Source
exports are ordinary read-only inputs; only this disposable derived index changes.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import monotonic


MAX_QUERY_CHARS = 200
MAX_QUERY_TOKENS = 8
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_CONVERSATIONS = 20_000
MAX_MESSAGES_PER_BUNDLE = 1_000_000
MAX_MESSAGE_CHARS = 1_000_000
_INDEX_LOCK = threading.Lock()
_SCHEMA_VERSION = 1


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError("搜索条件无效")
    query = request.get("query")
    if not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY_CHARS or "\x00" in query:
        raise ValueError("搜索词须为 1 至 200 个字符")
    words = _normalize(query).split()
    if not words or len(words) > MAX_QUERY_TOKENS:
        raise ValueError("请输入 1 至 8 个关键词")
    dates = []
    for key in ("date_from", "date_to"):
        value = request.get(key, "")
        if not isinstance(value, str):
            raise ValueError("日期格式无效")
        if value:
            try:
                if len(value) != 10 or datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
                    raise ValueError
            except ValueError:
                raise ValueError("日期格式无效") from None
        dates.append(value)
    if all(dates) and dates[0] > dates[1]:
        raise ValueError("开始日期不能晚于结束日期")
    return tuple(dict.fromkeys(words)), *dates


def _safe_path(path: Path, root: Path) -> bool:
    """Reject links, Windows junctions/reparse points, and escaped descendants."""
    try:
        relative = path.absolute().relative_to(root.absolute())
        cursor = root.absolute()
        for part in (None, *relative.parts):
            if part is not None:
                cursor = cursor / part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _private_root(data_dir: Path) -> Path:
    root = Path(data_dir).absolute()
    if not root.is_dir() or not _safe_path(root, root):
        raise RuntimeError("本地搜索目录不可用")
    current = root
    for name in ("private", "dashboard-search"):
        current = current / name
        if not current.exists():
            current.mkdir(mode=0o700)
        if not current.is_dir() or not _safe_path(current, root):
            raise RuntimeError("本地搜索目录不可用")
    return current


@contextmanager
def _writer_lock(root: Path):
    path = root / "writer.lock"
    if path.exists() and not _safe_path(path, root):
        raise RuntimeError("本地搜索索引不可用")
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            yield
        except OSError:
            raise RuntimeError("本地搜索正忙，请稍后重试") from None
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _message_date(value):
    try:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone()
                return parsed.strftime("%Y-%m-%d") if 2000 <= parsed.year <= 2100 else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        while value >= 100_000_000_000:
            value /= 1000
        parsed = datetime.fromtimestamp(value)
        return parsed.strftime("%Y-%m-%d") if 2000 <= parsed.year <= 2100 else None
    except (ValueError, OSError, OverflowError):
        return None


def _signature(info):
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _read_messages(path: Path, contacts: Path):
    if not _safe_path(path, contacts):
        raise ValueError("不支持该数据包")
    before = path.stat()
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError("数据包超过索引限制")
    with path.open("rb") as handle:
        if _signature(before) != _signature(os.fstat(handle.fileno())):
            raise ValueError("数据包正在更新")
        content = handle.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES or _signature(before) != _signature(path.stat()) or not _safe_path(path, contacts):
        raise ValueError("数据包正在更新")
    payload = json.loads(content.decode("utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ValueError("数据包格式无效")
    if len(payload["messages"]) > MAX_MESSAGES_PER_BUNDLE:
        raise ValueError("数据包超过索引限制")
    rows = []
    partial = False
    for message in payload["messages"]:
        if not isinstance(message, dict):
            partial = True
            continue
        text = message.get("content") or ""
        transcript = message.get("transcript") or message.get("voice_transcript") or ""
        day = _message_date(message.get("timestamp"))
        if not isinstance(text, str) or not isinstance(transcript, str) or day is None or len(text) + len(transcript) > MAX_MESSAGE_CHARS:
            partial = True
            continue
        # Transcript belongs to the same message; no cross-message matches.
        rows.append((day, _normalize(text + ("\n" + transcript if transcript else ""))))
    return before, rows, partial


def _schema(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, _SCHEMA_VERSION):
        raise RuntimeError("本地搜索索引版本不兼容")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS bundles (
            id TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            partial INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            bundle_id TEXT NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
            day TEXT NOT NULL, body TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_bundle ON messages(bundle_id);
        CREATE INDEX IF NOT EXISTS messages_day ON messages(day);
        PRAGMA user_version = 1;
    """)


def _refresh(connection, contacts: Path):
    if not contacts.exists():
        connection.execute("DELETE FROM bundles")
        return 0
    if not contacts.is_dir() or not _safe_path(contacts, contacts):
        raise RuntimeError("本地会话目录不可用")
    present = set()
    skipped = 0
    for bundle in sorted(contacts.iterdir(), key=lambda item: item.name):
        if bundle.name.startswith("."):
            continue
        path = bundle / "messages.json"
        if not bundle.is_dir() or not path.exists():
            continue
        if len(present) >= MAX_CONVERSATIONS:
            skipped += 1
            continue
        present.add(bundle.name)
        if not _safe_path(path, contacts):
            connection.execute("DELETE FROM bundles WHERE id = ?", (bundle.name,))
            skipped += 1
            continue
        try:
            info = path.stat()
            cached = connection.execute("SELECT size, mtime_ns, partial FROM bundles WHERE id = ?", (bundle.name,)).fetchone()
            if cached and cached[:2] == (info.st_size, info.st_mtime_ns):
                skipped += int(bool(cached[2]))
                continue
            info, rows, partial = _read_messages(path, contacts)
            connection.execute("DELETE FROM bundles WHERE id = ?", (bundle.name,))
            connection.execute("INSERT INTO bundles(id,size,mtime_ns,partial) VALUES (?,?,?,?)", (bundle.name, info.st_size, info.st_mtime_ns, int(partial)))
            connection.executemany("INSERT INTO messages(bundle_id,day,body) VALUES (?,?,?)", ((bundle.name, day, text) for day, text in rows))
            skipped += int(partial)
        except (OSError, ValueError, TypeError, RecursionError):
            # Do not retain stale matches for changed, unreadable source files.
            connection.execute("DELETE FROM bundles WHERE id = ?", (bundle.name,))
            skipped += 1
    for (bundle_id,) in connection.execute("SELECT id FROM bundles").fetchall():
        if bundle_id not in present:
            connection.execute("DELETE FROM bundles WHERE id = ?", (bundle_id,))
    return skipped


def search_messages(data_dir: Path, contacts_dir: Path, request):
    """Return only aggregate matches. Query text never leaves this process."""
    tokens, date_from, date_to = validate_request(request)
    if not _INDEX_LOCK.acquire(blocking=False):
        raise RuntimeError("本地搜索正忙，请稍后重试")
    try:
        root = _private_root(data_dir)
        with _writer_lock(root):
            database = root / "index.sqlite3"
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = Path(str(database) + suffix)
                if candidate.exists() and not _safe_path(candidate, root):
                    raise RuntimeError("本地搜索索引不可用")
            connection = sqlite3.connect(database, timeout=2)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA temp_store = MEMORY")
                connection.execute("PRAGMA secure_delete = ON")
                _schema(connection)
                with connection:
                    skipped = _refresh(connection, Path(contacts_dir))
                indexed_conversations = connection.execute("SELECT COUNT(*) FROM bundles").fetchone()[0]
                indexed_messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                clauses = ["instr(body, ?) > 0" for _ in tokens]
                params = list(tokens)
                if date_from:
                    clauses.append("day >= ?")
                    params.append(date_from)
                if date_to:
                    clauses.append("day <= ?")
                    params.append(date_to)
                deadline = monotonic() + 20
                connection.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
                rows = connection.execute(
                    "SELECT bundle_id, COUNT(*), MAX(day) FROM messages WHERE "
                    + " AND ".join(clauses)
                    + " GROUP BY bundle_id ORDER BY COUNT(*) DESC, MAX(day) DESC, bundle_id ASC",
                    params,
                ).fetchall()
                return {
                    "status": "ok",
                    "matches": [{"bundle_id": key, "match_count": count, "last_match_date": day} for key, count, day in rows],
                    "indexed_messages": indexed_messages,
                    "indexed_conversations": indexed_conversations,
                    "skipped_conversations": skipped,
                }
            finally:
                connection.close()
    except (OSError, sqlite3.Error):
        raise RuntimeError("本地搜索索引暂不可用，请稍后重试") from None
    finally:
        _INDEX_LOCK.release()
