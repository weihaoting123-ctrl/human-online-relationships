"""Privacy-first localhost dashboard for the she-love-me workflow.

The server intentionally has no remote mode, no CORS support, and no endpoint
that returns raw messages.  It is a visual control surface for local imports,
deterministic statistics, sampling, and already-generated reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Keep configured reparse/symlink ancestors visible to import validation.
DATA_DIR = Path(os.environ.get("SHE_LOVE_ME_DATA_DIR", REPO_ROOT / "data")).absolute()
_CONFIGURED_DATA_DIR = DATA_DIR
CONTACTS_DIR = DATA_DIR / "contacts"
RAW_IMPORT_DIR = DATA_DIR / "raw" / "dashboard"
SYNC_STATUS_PATH = Path(os.environ.get(
    "SHE_LOVE_ME_SYNC_STATUS",
    DATA_DIR / "private" / "wechat-sync" / "status.json",
)).resolve()
MAX_BODY_BYTES = 50 * 1024 * 1024
BUNDLE_ID_RE = re.compile(r"^[^/\\\x00]+$")
REPORT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SESSION_COOKIE = "slm_local_session"
SESSION_TTL_SECONDS = 24 * 60 * 60
_IMPORT_LOCK = threading.RLock()
_BACKUP_LOCK = threading.Lock()
_BACKUP_ACTIVITY = None
_BACKUP_LAST_ERROR = None


def _backup_project_root():
    # Keep alternate/test data directories isolated from the real project.
    data = Path(DATA_DIR).resolve()
    return data.parent if data.name == "data" else data


def backup_status():
    import backup_wechat_local as backup
    value = backup.get_status(_backup_project_root())
    with _BACKUP_LOCK:
        if _BACKUP_ACTIVITY:
            value = {**value, "state": "running", "mode": _BACKUP_ACTIVITY}
        elif _BACKUP_LAST_ERROR:
            value = {**value, "state": "failed", "error_code": _BACKUP_LAST_ERROR}
    return {"status": "ok", "backup": value}


def backup_history():
    import backup_wechat_local as backup
    return {"status": "ok", "snapshots": backup.get_history(_backup_project_root(), limit=20)}


def start_backup(*, verify=False):
    import backup_wechat_local as backup
    global _BACKUP_ACTIVITY, _BACKUP_LAST_ERROR
    project = _backup_project_root()
    mode = "verify" if verify else "backup"
    with _BACKUP_LOCK:
        if _BACKUP_ACTIVITY:
            raise ValueError("本机备份操作正在进行中")
        _BACKUP_ACTIVITY = mode
        _BACKUP_LAST_ERROR = None

    def worker():
        global _BACKUP_ACTIVITY, _BACKUP_LAST_ERROR
        try:
            (backup.verify_backup if verify else backup.run_backup)(project)
        except Exception:
            # Never print a worker traceback containing private source paths.
            with _BACKUP_LOCK:
                _BACKUP_LAST_ERROR = "INTERNAL_ERROR"
        finally:
            with _BACKUP_LOCK:
                _BACKUP_ACTIVITY = None
    try:
        threading.Thread(target=worker, name="local-wechat-backup", daemon=True).start()
    except Exception:
        with _BACKUP_LOCK:
            _BACKUP_ACTIVITY = None
        raise ValueError("本机备份未能启动") from None
    return {"status": "ok", "state": "running", "mode": mode}

SYNC_STATES = {
    "ready",
    "running",
    "completed",
    "awaiting_wechat_restart",
    "needs_account_selection",
    "wechat_not_running",
    "error",
}
SYNC_MODES = {"manual", "scheduled"}
SYNC_ERROR_CODES = {
    "account_selection_required",
    "exporter_not_ready",
    "invalid_status",
    "permission_required",
    "sync_failed",
    "wechat_not_running",
}
SYNC_COUNT_FIELDS = (
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
)

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from contact_bundle import resolve_bundle_paths, safe_slug  # noqa: E402
from convert_ciphertalk import convert_payload as convert_ciphertalk_payload  # noqa: E402
from convert_markdown import parse_markdown  # noqa: E402
from convert_weflow import convert_payload as convert_weflow_payload  # noqa: E402
from convert_weflow_cli import convert_payload as convert_weflow_cli_payload  # noqa: E402
from message_normalizer import normalize_payload  # noqa: E402
from external_chat_import import observed_owner_usernames  # noqa: E402
from import_store import (exclusive_import_lock, fingerprint_import,
                          sync_import_directory, validate_import_path,
                          validated_private_root)  # noqa: E402
from dashboard.search import search_messages  # noqa: E402
from dashboard import analysis as scoped_ai  # noqa: E402
from dashboard.library import (LibraryConflictError, LibraryNotFoundError,
                               LibraryValidationError)  # noqa: E402
from dashboard.library_service import LibraryService  # noqa: E402
from dashboard.modules import ModuleRegistry, ModulePolicyError  # noqa: E402


CLASSIFICATION_TOPICS = frozenset({
    "商户与业务合作", "工作与项目协作", "交易与物流售后", "付款与账务",
    "生活与社交", "待确认／低信号", "混合主题",
})


def _read_json(path: Path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


@contextmanager
def _exclusive_import_lock():
    """Compatibility entrypoint sharing the offline converters' OS lock."""
    with exclusive_import_lock(CONTACTS_DIR):
        yield


def resolve_bundle(bundle_id: str) -> Path:
    bundle_id = unquote(bundle_id)
    if not BUNDLE_ID_RE.fullmatch(bundle_id) or bundle_id in {".", ".."}:
        raise ValueError("无效的数据包标识")
    path = (CONTACTS_DIR / bundle_id).resolve()
    if not _inside(path, CONTACTS_DIR) or not path.is_dir():
        raise FileNotFoundError("找不到该联系人数据包")
    return path


def _public_analysis(analysis):
    """Return conclusions but deliberately omit quote/evidence payloads."""
    if not isinstance(analysis, dict):
        return None
    result = {
        key: _public_text(analysis.get(key))
        for key in (
            "relationship_type",
            "relationship_label",
            "relationship_trend",
            "verdict",
            "simp_description",
            "love_description",
        )
        if _public_text(analysis.get(key))
    }
    warnings = []
    for warning in analysis.get("danger_warnings", []) or []:
        if not isinstance(warning, dict):
            continue
        warnings.append({
            key: _public_text(warning.get(key), limit=120)
            for key in ("type", "severity", "level", "title")
            if _public_text(warning.get(key), limit=120)
        })
    if warnings:
        result["danger_warnings"] = warnings
    return result or None


def _public_text(value, limit=800):
    """Remove common inline-quote forms before conclusions reach the browser."""
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value).replace("\x00", "").strip()
    for pattern in (r"「[^」]*」", r"『[^』]*』", r"“[^”]*”", r'"[^"\r\n]*"'):
        text = re.sub(pattern, "[引文已隐藏]", text)
    return text[:limit] or None


def _public_number(value):
    """Accept finite JSON numbers only; strings and booleans are not statistics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return min(max(value, -1_000_000_000_000), 1_000_000_000_000)


def _public_date(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _number_fields(value, allowed):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in allowed:
        number = _public_number(value.get(key))
        if number is not None:
            result[key] = number
    return result or None


def _public_stats(stats):
    """Deeply rebuild the aggregate schema; never copy arbitrary nested data."""
    if not isinstance(stats, dict):
        return None

    schemas = {
        "basic": (
            "total_messages", "my_messages", "their_messages", "my_ratio",
            "their_ratio", "total_days", "avg_daily",
        ),
        "initiative": ("my_starts", "their_starts", "my_start_ratio"),
        "reply_speed": ("my_avg_seconds", "their_avg_seconds", "speed_ratio"),
        "message_length": ("my_avg_chars", "their_avg_chars"),
        "bombing": (
            "my_bomb_count", "their_bomb_count", "my_max_consecutive",
            "their_max_consecutive",
        ),
        "cold_response": ("my_cold_count", "their_cold_count"),
        "unanswered": ("my_unanswered", "their_unanswered"),
        "goodnight": ("my_goodnight", "their_goodnight"),
        "scores": ("simp_index", "loved_index", "cold_index"),
        "repair": ("me_repair_count", "them_repair_count"),
        "recent_30d": (
            "me_messages", "them_messages", "total_messages",
            "me_initiation_ratio", "them_initiation_ratio",
            "me_repair_count", "them_repair_count", "them_message_density_cv",
        ),
    }
    result = {}
    for section, fields in schemas.items():
        cleaned = _number_fields(stats.get(section), fields)
        if cleaned:
            result[section] = cleaned

    basic = result.get("basic")
    raw_basic = stats.get("basic")
    if isinstance(raw_basic, dict):
        dates = [_public_date(item) for item in raw_basic.get("date_range", [])]
        dates = [item for item in dates if item is not None]
        if len(dates) == 2:
            if basic is None:
                basic = result["basic"] = {}
            basic["date_range"] = dates

    active_hours = stats.get("active_hours")
    if isinstance(active_hours, dict):
        cleaned_hours = {}
        for hour in range(24):
            number = _public_number(active_hours.get(str(hour)))
            if number is not None:
                cleaned_hours[str(hour)] = number
        if cleaned_hours:
            result["active_hours"] = cleaned_hours

    daily_trend = stats.get("daily_trend")
    if isinstance(daily_trend, list):
        cleaned_days = []
        for item in daily_trend[:366]:
            if not isinstance(item, dict):
                continue
            day = _public_date(item.get("date"))
            count = _public_number(item.get("count"))
            if day is not None and count is not None:
                cleaned_days.append({"date": day, "count": count})
        if cleaned_days:
            result["daily_trend"] = cleaned_days

    linguistic = stats.get("linguistic")
    if isinstance(linguistic, dict):
        cleaned_linguistic = {}
        for metric in (
            "pronoun_we_count", "pronoun_i_count", "hedging_words_count",
            "conditional_count", "positive_emotion_count",
            "negative_emotion_count", "positive_emotion_ratio", "revoke_count",
        ):
            pair = _number_fields(linguistic.get(metric), ("me", "them"))
            if pair:
                cleaned_linguistic[metric] = pair
        if cleaned_linguistic:
            result["linguistic"] = cleaned_linguistic
    return result or None


def _timeline(messages):
    daily = defaultdict(lambda: {"me": 0, "them": 0})
    hourly = {str(hour): {"me": 0, "them": 0} for hour in range(24)}
    valid_days = []
    for message in messages:
        sender = message.get("sender")
        timestamp = message.get("timestamp")
        if sender not in ("me", "them") or not isinstance(timestamp, (int, float)):
            continue
        try:
            local_dt = datetime.fromtimestamp(timestamp)
        except (OSError, OverflowError, ValueError):
            continue
        valid_days.append(local_dt.date())
        daily[local_dt.strftime("%Y-%m-%d")][sender] += 1
        hourly[str(local_dt.hour)][sender] += 1
    filled_daily = []
    if valid_days:
        final_day = max(valid_days)
        first_day = max(min(valid_days), final_day - timedelta(days=179))
        cursor = first_day
        while cursor <= final_day:
            day = cursor.isoformat()
            filled_daily.append({"date": day, **daily[day]})
            cursor += timedelta(days=1)
    return {
        "daily": filled_daily,
        "hourly": hourly,
    }


def _report_id(path: Path) -> str:
    relative = path.resolve().relative_to(CONTACTS_DIR.resolve()).as_posix()
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32]


def resolve_report(report_id: str) -> Path:
    if not REPORT_ID_RE.fullmatch(str(report_id)):
        raise ValueError("无效的报告标识")
    if not CONTACTS_DIR.is_dir():
        raise FileNotFoundError("报告不存在")
    for bundle in CONTACTS_DIR.iterdir():
        report_dir = bundle / "reports"
        if not report_dir.is_dir():
            continue
        for path in report_dir.glob("*.html"):
            if secrets.compare_digest(_report_id(path), report_id):
                return path.resolve()
    raise FileNotFoundError("报告不存在")


def _report_files(bundle: Path):
    report_dir = bundle / "reports"
    if not report_dir.is_dir():
        return []
    files = []
    for path in sorted(report_dir.glob("*.html"), key=lambda item: item.stat().st_mtime, reverse=True):
        files.append({
            "name": "完整报告",
            "url": f"/reports/{_report_id(path)}",
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return files


def bundle_summary(bundle: Path):
    messages_path = bundle / "messages.json"
    manifest = _read_json(bundle / "dashboard_manifest.json", {}) or {}
    if not isinstance(manifest, dict):
        manifest = {}
    stats = _read_json(bundle / "stats.json")
    stats = stats if isinstance(stats, dict) else None
    analysis = _read_json(bundle / "analysis.json")
    classification = _read_json(bundle / "classification.json", {})
    if not isinstance(classification, dict):
        classification = {}
    raw_basic = (stats or {}).get("basic")
    basic = raw_basic if isinstance(raw_basic, dict) else {}
    raw_dates = basic.get("date_range")
    date_range = None
    if isinstance(raw_dates, list) and len(raw_dates) == 2:
        dates = [_public_date(value) for value in raw_dates]
        if all(dates) and dates[0] <= dates[1]:
            date_range = dates
    payload = {}
    if not manifest:
        payload = _read_json(messages_path, {}) or {}
        if not isinstance(payload, dict):
            payload = {}
    kind = manifest.get("conversation_kind") or classification.get("conversation_kind")
    primary = classification.get("primary_topic")
    candidates = classification.get("candidate_topics")
    candidates = candidates if isinstance(candidates, list) else []
    return {
        "id": bundle.name,
        "contact": manifest.get("contact") or payload.get("contact_display") or (stats or {}).get("contact") or bundle.name.split("__", 1)[0],
        "source": manifest.get("source") or payload.get("source", "unknown"),
        "message_count": basic.get("total_messages", manifest.get("message_count", len(payload.get("messages", [])))),
        "date_range": date_range,
        "conversation_kind": kind if isinstance(kind, str) and kind in {"direct", "group"} else "unknown",
        "primary_topic": primary if isinstance(primary, str) and primary in CLASSIFICATION_TOPICS else None,
        "candidate_topics": list(dict.fromkeys(value for value in candidates if isinstance(value, str) and value in CLASSIFICATION_TOPICS)),
        "has_stats": isinstance(stats, dict),
        "has_sample": (bundle / "chat_history.txt").is_file(),
        "has_analysis": isinstance(analysis, dict),
        "reports": _report_files(bundle),
        "updated_at": datetime.fromtimestamp(messages_path.stat().st_mtime).isoformat(timespec="seconds")
        if messages_path.is_file() else None,
    }


def source_bundle_summaries():
    """Complete file projection; no user-managed fields or message writes."""
    contacts = _library_contacts()
    contacts.mkdir(parents=True, exist_ok=True)
    bundles = [
        bundle_summary(path)
        for path in contacts.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / "messages.json").is_file()
        and scoped_ai._safe_path(path / "messages.json", contacts)
    ]
    return sorted(bundles, key=lambda item: item.get("updated_at") or "", reverse=True)


def _library_contacts():
    # Legacy callers may override only DATA_DIR or only CONTACTS_DIR. In both
    # cases keep new management reads/writes in the overridden private fixture.
    if Path(DATA_DIR) != _CONFIGURED_DATA_DIR and Path(CONTACTS_DIR) == _CONFIGURED_DATA_DIR / 'contacts':
        return Path(DATA_DIR) / 'contacts'
    return Path(CONTACTS_DIR)


def library_service():
    # Resolve from the archive root so alternate/test CONTACTS_DIRs cannot write
    # management state into the production DATA_DIR by accident.
    return LibraryService(_library_contacts().parent, source_bundle_summaries)


def module_registry():
    return ModuleRegistry(library_service().repository)


def list_bundles():
    return library_service().visible()


def _analysis_admission(bundle_id):
    module_registry().require('analysis')
    library_service().require_visible(bundle_id)


def managed_search(request):
    visible = {item['id'] for item in list_bundles()}
    result = search_messages(DATA_DIR, CONTACTS_DIR, request)
    return {**result, 'matches': [item for item in result['matches'] if item['bundle_id'] in visible]}


def bundle_detail(bundle_id: str):
    from dashboard.relationship_timeline import build_relationship_timeline

    bundle = resolve_bundle(bundle_id)
    payload = _read_json(bundle / "messages.json", {}) or {}
    normalized = normalize_payload(payload, drop_invalid=True)
    stats = _read_json(bundle / "stats.json")
    analysis = _read_json(bundle / "analysis.json")
    preview = run_script(
        "build_chat_history.py",
        ["--input", str(bundle / "messages.json"), "--preview"],
        timeout=45,
    ) if normalized.get("messages") else {
        "total": 0, "date_range": [], "span_days": 0,
        "me_count": 0, "them_count": 0, "suggestions": [],
        "recommended_reason": "本机归档暂无有效记录，无法判断联系时间或原因。",
    }
    return {
        **bundle_summary(bundle),
        "stats": _public_stats(stats),
        "preview": preview,
        "timeline": _timeline(normalized.get("messages", [])),
        "relationship_timeline": build_relationship_timeline(payload, {}),
        "analysis": _public_analysis(analysis),
        "privacy": {
            "raw_messages_returned": False,
            "evidence_quotes_returned": False,
            "network_required": False,
        },
    }


def run_script(script_name: str, args: list[str], timeout: int = 180):
    script = (SCRIPTS_DIR / script_name).resolve()
    if not _inside(script, SCRIPTS_DIR) or not script.is_file():
        raise ValueError("不允许执行该脚本")
    result = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        # Never forward third-party stdout/stderr: it may contain paths, IDs, keys,
        # or even message excerpts. The script name is a fixed internal allowlist.
        raise RuntimeError(f"本地处理失败（{script.stem}）")
    output = result.stdout.strip()
    if not output:
        return {"status": "ok"}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        for line in reversed(output.splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"status": "ok"}


def exporter_status():
    """Return a safe readiness snapshot even when no exporter is installed."""
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "bootstrap_local_exporter.py")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("invalid status")
        safe_keys = {
            "status", "provider", "version", "runtime", "node_available",
            "npm_available", "manifest_valid", "lock_valid", "package_valid",
            "ready", "changed", "next_action", "error_code",
        }
        return {key: payload[key] for key in safe_keys if key in payload}
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {"status": "not_ready", "message": "尚未检测到可用的微信导出器"}


def _safe_sync_timestamp(value):
    """Return only canonical ISO timestamps, never arbitrary status-file text."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if not 2000 <= parsed.year <= 2100:
        return None
    return parsed.isoformat(timespec="seconds")


def _safe_sync_count(value):
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(parsed, 0), 1_000_000_000_000)


def sync_status():
    """Read the automation status through a strict, identifier-free allowlist."""
    missing = not SYNC_STATUS_PATH.exists()
    payload = _read_json(SYNC_STATUS_PATH)
    if not isinstance(payload, dict):
        return {
            "enabled": False,
            "state": "not_configured" if missing else "error",
            "mode": None,
            "last_run_at": None,
            "next_run_at": None,
            "sync_strategy": None,
            **{key: 0 for key in SYNC_COUNT_FIELDS},
            "error_code": None if missing else "invalid_status",
            "attention": not missing,
        }

    state = payload.get("state")
    if state not in SYNC_STATES:
        state = "error"
        error_code = "invalid_status"
    else:
        error_code = payload.get("error_code")
        if error_code not in SYNC_ERROR_CODES:
            error_code = None
    counts = {key: _safe_sync_count(payload.get(key)) for key in SYNC_COUNT_FIELDS}
    attention = (
        state in {
            "awaiting_wechat_restart",
            "needs_account_selection",
            "wechat_not_running",
            "error",
        }
        or counts["failed_conversations"] > 0
    )
    return {
        "enabled": payload.get("enabled") is True,
        "state": state,
        "mode": payload.get("mode") if payload.get("mode") in SYNC_MODES else None,
        "last_run_at": _safe_sync_timestamp(payload.get("last_run_at")),
        "next_run_at": _safe_sync_timestamp(payload.get("next_run_at")),
        "sync_strategy": payload.get("sync_strategy") if payload.get("sync_strategy") in ("changed-shards", "full-reconcile") else None,
        **counts,
        "error_code": error_code,
        "attention": attention,
    }


def _detect_json_kind(data):
    messages = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []
    sample = next((item for item in messages if isinstance(item, dict)), {})
    senders = {item.get("sender") for item in messages[:50] if isinstance(item, dict)}
    if senders and senders.issubset({"me", "them", "unknown", None}):
        return "normalized-json"
    if "parsedContent" in sample or "isSend" in sample:
        if isinstance(data, dict) and "session" in data:
            return "weflow-json"
        return "weflow-cli"
    if "direction" in sample or (isinstance(data, dict) and "meta" in data):
        return "ciphertalk"
    raise ValueError("无法识别 JSON 格式，请在导入时明确选择来源")


def archive_status():
    result = {}
    for label, filename, fields in (
        ("media", "archive-status.json", ("total_files", "archived_files", "archived_bytes", "failed_files", "viewable_images", "unique_viewable_images", "encrypted_images", "duplicate_payloads", "media_refresh_processed", "media_refresh_recovered", "processed_files", "new_files", "changed_files", "skipped_existing_files", "baselined_files", "restored_files", "verified_existing_files")),
        ("classification", "classification-status.json", ("classified_conversations", "classified_messages", "failed_conversations", "processed_conversations", "skipped_conversations", "processed_messages")),
        ("voice", "voice-archive-status.json", ("total_voice_messages", "archived_voice_messages", "missing_voice_messages", "unique_audio_files", "archived_bytes", "processed_databases", "skipped_databases")),
        ("voice_transcription", "voice-transcribe-status.json", ("total_audio", "transcribed_audio", "pending_audio", "failed_audio", "processed_audio", "skipped_audio", "attached_messages", "playable_audio", "empty_audio", "elapsed_seconds", "attachment_conflicts")),
    ):
        raw = _read_json(DATA_DIR / filename, {})
        raw = raw if isinstance(raw, dict) else {}
        state = raw.get("state")
        states = {"running", "completed", "partial", "error", "blocked"}
        if label == "voice_transcription":
            states = {"running", "needs_archive", "needs_runtime", "pending", "complete", "partial", "error"}
        result[label] = {"state": state if isinstance(state, str) and state in states else "not_started",
                         **{key: _safe_sync_count(raw.get(key)) for key in fields}}
        if label in {"media", "classification"}:
            result[label].update(report_rebuilt=raw.get("report_rebuilt") is True,
                                 incremental=raw.get("incremental") is True)
        if label == "voice_transcription":
            code = raw.get("code")
            result[label]["code"] = code if isinstance(code, str) and code in {
                "", "voice_archive_missing", "local_voice_runtime_missing", "local_voice_transcription_failed",
            } else None
        if label == "media":
            result[label]["refreshing"] = raw.get("media_refresh_state") == "running"
    return result


def _source_import_identity(data):
    """Capture source identity before adapters or display overrides discard it."""
    observed = observed_owner_usernames(data)
    if not isinstance(data, dict):
        return {"observed_owner_usernames": observed}
    identity = {key: data.get(key) for key in (
        "source", "contact_username", "contact_display", "own_wxid", "my_display",
    )}
    for container, fields in (("meta", ("ownerId",)),
                              ("session", ("wxid", "remark", "nickname"))):
        value = data.get(container)
        identity[container] = {key: value.get(key) for key in fields} if isinstance(value, dict) else {}
    identity["observed_owner_usernames"] = observed
    return identity


def _verified_web_import(existing, import_fingerprint, identity):
    """A manifest claim alone is insufficient: verify the current stored bytes."""
    if existing.name.startswith("."):
        return False
    try:
        validate_import_path(existing)
        if getattr(existing.lstat(), "st_file_attributes", 0) & 2:
            return False
        manifest_path = validate_import_path(existing / "dashboard_manifest.json")
        messages_path = validate_import_path(existing / "messages.json")
        if not existing.is_dir() or not manifest_path.is_file() or not messages_path.is_file():
            return False
        manifest = _read_json(manifest_path, {})
        if not isinstance(manifest, dict) or manifest.get("import_fingerprint_version") != 2:
            return False
        if manifest.get("import_identity") != identity or manifest.get("import_fingerprint") != import_fingerprint:
            return False
        raw = messages_path.read_bytes()
        if manifest.get("messages_sha256") != hashlib.sha256(raw).hexdigest():
            return False
        return fingerprint_import(json.loads(raw), identity_context=identity) == import_fingerprint
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        # Damaged/legacy material stays untouched; it is not a valid reuse hit.
        return False


def _write_web_import_file(path, content):
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _import_payload_locked(request):
    kind = str(request.get("kind") or "auto").strip().lower()
    content = request.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("文件内容为空")
    contact = str(request.get("contact") or "").strip()
    my_name = str(request.get("my_name") or "我").strip() or "我"
    contact_id = str(request.get("contact_id") or contact).strip()
    filename = safe_slug(Path(str(request.get("filename") or "import.txt")).name, "import.txt")
    if len(content.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("文件超过 50 MB 限制")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    source_identity = {}
    if kind == "markdown":
        if not contact:
            raise ValueError("Markdown 导入需要填写对方称呼")
        messages = parse_markdown(content, my_name)
        payload = normalize_payload({
            "source": "markdown",
            "contact_display": contact,
            "messages": messages,
        }, drop_invalid=True)
    else:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行") from exc
        source_identity = _source_import_identity(data)
        if kind == "auto":
            kind = _detect_json_kind(data)
        if kind == "normalized-json":
            payload = normalize_payload(data, drop_invalid=True)
            contact = contact or payload.get("contact_display") or "对方"
            payload["contact_display"] = contact
        elif kind == "weflow-cli":
            if not contact:
                raise ValueError("WeFlow CLI 导入需要填写联系人称呼")
            payload = normalize_payload(
                convert_weflow_cli_payload(data, contact, contact_id or None),
                drop_invalid=True,
            )
        elif kind == "weflow-json":
            payload, _ = convert_weflow_payload(data)
            contact = contact or payload.get("contact_display") or "对方"
            payload["contact_display"] = contact
        elif kind == "ciphertalk":
            if not contact:
                raise ValueError("CipherTalk 导入需要填写联系人称呼")
            payload = normalize_payload(
                convert_ciphertalk_payload(data, contact, contact_id or None),
                drop_invalid=True,
            )
        else:
            raise ValueError("不支持的数据来源")

    messages = payload.get("messages", [])
    if not messages:
        raise ValueError("没有找到可分析的双方消息")
    contact = contact or payload.get("contact_display") or "对方"
    payload["contact_display"] = contact
    identity = {
        "entrypoint": "dashboard", "kind": kind,
        "request": {"contact": str(request.get("contact") or "").strip(),
                    "contact_id": str(request.get("contact_id") or "").strip(),
                    "my_name": my_name},
        "source": source_identity,
    }
    import_fingerprint = fingerprint_import(payload, identity_context=identity)

    # Exact re-imports are idempotent.  This is checked while holding the
    # process-wide import lock, before creating either a raw archive or bundle.
    if CONTACTS_DIR.is_dir():
        for existing in CONTACTS_DIR.iterdir():
            if _verified_web_import(existing, import_fingerprint, identity):
                return bundle_detail(existing.name)

    bundle_paths = resolve_bundle_paths(
        contact,
        contact_id or contact,
        output_dir=str(CONTACTS_DIR),
    )
    if os.path.lexists(bundle_paths["bundle_dir"]):
        bundle_paths = resolve_bundle_paths(
            contact,
            f"{contact_id or contact}-{stamp}-{secrets.token_hex(4)}",
            output_dir=str(CONTACTS_DIR),
        )
    bundle = Path(bundle_paths["bundle_dir"])
    while os.path.lexists(bundle):
        bundle_paths = resolve_bundle_paths(
            contact,
            f"{contact_id or contact}-{stamp}-{secrets.token_hex(8)}",
            output_dir=str(CONTACTS_DIR),
        )
        bundle = Path(bundle_paths["bundle_dir"])

    private_root = validated_private_root(CONTACTS_DIR)
    validate_import_path(RAW_IMPORT_DIR)
    RAW_IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_IMPORT_DIR / f"{stamp}_{secrets.token_hex(8)}_{filename}"
    raw_fd, raw_temp_name = tempfile.mkstemp(prefix=".dashboard-raw-", suffix=".tmp", dir=RAW_IMPORT_DIR)
    os.close(raw_fd)
    raw_temp = Path(raw_temp_name)
    staged_bundle = None
    raw_committed = False
    bundle_committed = False

    payload["bundle_dir"] = str(bundle)
    try:
        staged_bundle = Path(tempfile.mkdtemp(prefix=".dashboard-import-", dir=private_root))
        with raw_temp.open("wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        messages_path = staged_bundle / "messages.json"
        message_bytes = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        _write_web_import_file(messages_path, message_bytes)
        run_script(
            "stats_analyzer.py",
            ["--input", str(messages_path), "--output", str(staged_bundle / "stats.json")],
        )
        # Windows _commit requires a writable handle; this is our new staged file.
        with (staged_bundle / "stats.json").open("r+b") as handle:
            os.fsync(handle.fileno())
        _write_web_import_file(staged_bundle / "dashboard_manifest.json", json.dumps({
            "contact": contact,
            "source": payload.get("source", kind),
            "message_count": len(messages),
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "import_fingerprint": import_fingerprint,
            "import_fingerprint_version": 2,
            "import_identity": identity,
            "messages_sha256": hashlib.sha256(message_bytes).hexdigest(),
        }, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))

        # Both names are unique and chosen while holding _IMPORT_LOCK. Publish
        # only after all processing succeeds; retries therefore create a safe
        # new version instead of overwriting an earlier import.
        sync_import_directory(staged_bundle)
        validate_import_path(bundle)
        validate_import_path(raw_path)
        if os.path.lexists(bundle) or os.path.lexists(raw_path):
            raise RuntimeError("导入目标已被占用，请检查本机导入状态")
        raw_temp.rename(raw_path)
        raw_committed = True
        staged_bundle.rename(bundle)
        bundle_committed = True
        sync_import_directory(CONTACTS_DIR)
        sync_import_directory(private_root)
        sync_import_directory(RAW_IMPORT_DIR)
        return bundle_detail(bundle.name)
    except Exception:
        if raw_committed and not bundle_committed:
            raw_path.unlink(missing_ok=True)
        raise
    finally:
        raw_temp.unlink(missing_ok=True)
        if staged_bundle is not None and staged_bundle.exists():
            shutil.rmtree(staged_bundle, ignore_errors=True)


def import_payload(request):
    """Serialize imports so target selection and atomic publication cannot race."""
    with _IMPORT_LOCK:
        with _exclusive_import_lock():
            return _import_payload_locked(request)


def analyze_bundle(bundle_id: str):
    bundle = resolve_bundle(bundle_id)
    result = run_script(
        "stats_analyzer.py",
        ["--input", str(bundle / "messages.json"), "--output", str(bundle / "stats.json")],
    )
    return {"result": result, "bundle": bundle_detail(bundle_id)}


def build_sample(bundle_id: str, since=None):
    bundle = resolve_bundle(bundle_id)
    args = [
        "--input", str(bundle / "messages.json"),
        "--output", str(bundle / "chat_history.txt"),
    ]
    if since:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(since)):
            raise ValueError("起始日期必须是 YYYY-MM-DD")
        args.extend(["--since", str(since)])
    result = run_script("build_chat_history.py", args)
    return {"result": result, "bundle": bundle_detail(bundle_id)}


def generate_report(bundle_id: str):
    bundle = resolve_bundle(bundle_id)
    stats_path = bundle / "stats.json"
    analysis_path = bundle / "analysis.json"
    if not analysis_path.is_file():
        raise ValueError("尚无 analysis.json；先完成 AI 深度分析再生成完整报告")
    summary = bundle_summary(bundle)
    result = run_script("generate_html_report.py", [
        "--stats", str(stats_path),
        "--analysis", str(analysis_path),
        "--contact", summary["contact"],
        "--output", str(bundle / "reports"),
    ])
    return {"result": result, "bundle": bundle_detail(bundle_id)}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, token):
        super().__init__(server_address, handler_class)
        self.token = token
        port = self.server_port
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if port == 80:
            self.allowed_hosts.update({"127.0.0.1", "localhost"})
            self.allowed_origins.update({"http://127.0.0.1", "http://localhost"})
        self._sessions = {}
        self._session_lock = threading.Lock()

    def issue_session(self):
        value = secrets.token_urlsafe(32)
        with self._session_lock:
            self._sessions[value] = datetime.now().timestamp()
            while len(self._sessions) > 256:
                self._sessions.pop(next(iter(self._sessions)))
        return value

    def valid_session(self, value):
        if not isinstance(value, str) or not 20 <= len(value) <= 128:
            return False
        with self._session_lock:
            cutoff = datetime.now().timestamp() - SESSION_TTL_SECONDS
            for candidate, issued_at in list(self._sessions.items()):
                if issued_at < cutoff:
                    self._sessions.pop(candidate, None)
            return any(secrets.compare_digest(value, candidate) for candidate in self._sessions)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SheLoveMeLocal/1.0"

    def log_message(self, fmt, *args):
        # Access paths contain opaque bundle identifiers and may contain legacy
        # contact slugs. Keep routine access entirely out of terminal logs.
        return

    def _security_headers(self, content_type="application/json; charset=utf-8"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )

    def _report_security_headers(self):
        """Allow the report's embedded CSS while forbidding scripts and network access."""
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "script-src 'none'; connect-src 'none'; font-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )

    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json({"status": "error", "error": str(message)}, status)

    def _authorized(self):
        header_authorized = secrets.compare_digest(
            self.headers.get("X-Local-Token", ""),
            self.server.token,
        )
        if header_authorized:
            return True
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            session = cookies.get(SESSION_COOKIE)
            return bool(session and self.server.valid_session(session.value))
        except (CookieError, AttributeError):
            return False

    def _valid_host(self):
        host = self.headers.get("Host", "").strip().lower()
        return host in self.server.allowed_hosts

    def _valid_origin(self, required=False):
        origin = self.headers.get("Origin")
        if origin is None:
            return not required
        return origin.strip().lower() in self.server.allowed_origins

    def _request_context_valid(self, require_origin=False):
        if not self._valid_host():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "请求主机不是本机控制台")
            return False
        if not self._valid_origin(required=require_origin):
            self._error(HTTPStatus.FORBIDDEN, "请求来源不是本机控制台")
            return False
        return True

    def _read_request(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的请求长度") from exc
        if length <= 0 or length > MAX_BODY_BYTES + 1024 * 1024:
            raise ValueError("请求为空或超过大小限制")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求结构无效")
        return value

    def do_GET(self):
        if not self._request_context_valid():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._authorized():
                return self._error(HTTPStatus.FORBIDDEN, "本地会话令牌无效")
            try:
                if path == "/api/library":
                    return self._json(library_service().snapshot())
                if path == "/api/modules":
                    return self._json(module_registry().snapshot())
                if path == "/api/state":
                    return self._json({
                        "status": "ok",
                        "local_only": True,
                        "python": sys.version.split()[0],
                        "exporter": exporter_status(),
                        "sync": sync_status(),
                        "archive": archive_status(),
                        "bundles": list_bundles(),
                    })
                if path == "/api/sync-status":
                    return self._json({"status": "ok", "sync": sync_status(), "archive": archive_status()})
                if path == "/api/ai/config":
                    return self._json(scoped_ai.config_status(DATA_DIR))
                if path == "/api/ai/history":
                    return self._json(scoped_ai.history(DATA_DIR))
                if path == "/api/ai/jobs":
                    return self._json(scoped_ai.jobs(DATA_DIR))
                if path == "/api/backup/status":
                    return self._json(backup_status())
                if path == "/api/backup/history":
                    return self._json(backup_history())
                ai_match = re.fullmatch(r"/api/ai/(jobs|reports)/([a-f0-9]{48})", path)
                if ai_match:
                    getter = scoped_ai.job_status if ai_match.group(1) == "jobs" else scoped_ai.report
                    return self._json(getter(DATA_DIR, ai_match.group(2)))
                match = re.fullmatch(r"/api/bundles/([^/]+)", path)
                if match:
                    return self._json({"status": "ok", "bundle": bundle_detail(match.group(1))})
                return self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            except FileNotFoundError:
                return self._error(HTTPStatus.NOT_FOUND, "本机资源不存在")
            except LibraryNotFoundError:
                return self._error(HTTPStatus.NOT_FOUND, "管理记录不存在或来源暂不可用")
            except (LibraryConflictError, ModulePolicyError) as exc:
                return self._error(HTTPStatus.CONFLICT, str(exc))
            except scoped_ai.AnalysisError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                return self._error(HTTPStatus.BAD_REQUEST, "本地读取失败或数据格式无效")

        if path.startswith("/reports/"):
            if not self._authorized():
                return self._error(HTTPStatus.FORBIDDEN, "本地会话令牌无效")
            return self._serve_report_file(path)
        if path.startswith("/exports/"):
            if not self._authorized():
                return self._error(HTTPStatus.FORBIDDEN, "本地会话令牌无效")
            return self._serve_export_file(path)
        return self._serve_static(path)

    def do_POST(self):
        if not self._request_context_valid(require_origin=True):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            return self._error(HTTPStatus.NOT_FOUND, "接口不存在")
        if not self._authorized():
            return self._error(HTTPStatus.FORBIDDEN, "本地会话令牌无效")
        try:
            request = self._read_request()
            if path == "/api/library/conversations":
                return self._json(library_service().update_conversation(request))
            if path == "/api/library/tags":
                return self._json(library_service().update_tag(request))
            if path == "/api/modules":
                return self._json(module_registry().update(request))
            if path == "/api/ai/config":
                return self._json(scoped_ai.save_config(DATA_DIR, request))
            if path == "/api/ai/config/clear":
                return self._json(scoped_ai.clear_config(DATA_DIR))
            if path == "/api/ai/preview":
                _analysis_admission(request.get('bundle_id'))
                return self._json(scoped_ai.preview(DATA_DIR, CONTACTS_DIR, request))
            if path == "/api/ai/run":
                module_registry().require('analysis')
                return self._json(scoped_ai.run(DATA_DIR, CONTACTS_DIR, request,
                                               admission=_analysis_admission), HTTPStatus.ACCEPTED)
            if path in {"/api/backup/run", "/api/backup/verify"}:
                if request:
                    raise ValueError("备份仅使用本机固定目录，不接受自定义路径或参数")
                if path.endswith('/run'):
                    module_registry().require('backup')
                return self._json(start_backup(verify=path.endswith("/verify")), HTTPStatus.ACCEPTED)
            cancel_match = re.fullmatch(r"/api/ai/jobs/([a-f0-9]{48})/cancel", path)
            if cancel_match:
                return self._json(scoped_ai.cancel(DATA_DIR, cancel_match.group(1)))
            if path == "/api/search":
                return self._json(managed_search(request))
            if path == "/api/import":
                module_registry().require('sync')
                return self._json({"status": "ok", "bundle": import_payload(request)})
            match = re.fullmatch(r"/api/bundles/([^/]+)/(analyze|sample|report)", path)
            if not match:
                return self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            bundle_id, action = match.groups()
            library_service().require_visible(unquote(bundle_id))
            if action == "analyze":
                return self._json({"status": "ok", **analyze_bundle(bundle_id)})
            if action == "sample":
                return self._json({"status": "ok", **build_sample(bundle_id, request.get("since"))})
            return self._json({"status": "ok", **generate_report(bundle_id)})
        except FileNotFoundError:
            return self._error(HTTPStatus.NOT_FOUND, "本机资源不存在")
        except LibraryNotFoundError:
            return self._error(HTTPStatus.NOT_FOUND, "管理记录不存在或来源暂不可用")
        except (LibraryConflictError, ModulePolicyError) as exc:
            return self._error(HTTPStatus.CONFLICT, str(exc))
        except LibraryValidationError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except scoped_ai.AnalysisError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            return self._error(HTTPStatus.BAD_REQUEST, "本地处理失败或请求格式无效")

    def _serve_static(self, request_path):
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        target = (STATIC_DIR / relative).resolve()
        if not _inside(target, STATIC_DIR) or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "页面不存在")
        content = target.read_bytes()
        if target.name == "index.html":
            content = content.replace(b"__LOCAL_API_TOKEN__", b"")
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self._security_headers(mime)
        if target.name == "index.html":
            session = self.server.issue_session()
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400",
            )
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_report_file(self, request_path):
        parts = [unquote(part) for part in request_path.split("/") if part]
        if len(parts) != 2 or parts[0] != "reports":
            return self._error(HTTPStatus.NOT_FOUND, "报告不存在")
        try:
            target = resolve_report(parts[1])
        except (FileNotFoundError, ValueError):
            return self._error(HTTPStatus.NOT_FOUND, "报告不存在")
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._report_security_headers()
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_export_file(self, request_path):
        root = DATA_DIR / "exports"
        relative = unquote(request_path[len("/exports/"):])
        target = (root / relative).resolve()
        if not _inside(target, root):
            return self._error(HTTPStatus.NOT_FOUND, "归档视图不存在")
        # Apply feature policy to the canonical path, not the user-supplied
        # prefix (which can contain dot segments or Windows separators).
        relative = target.relative_to(root.resolve()).as_posix()
        try:
            if relative.startswith('wechat-voice/'):
                module_registry().require('voice')
            elif relative.startswith('wechat-media/'):
                module_registry().require('media')
        except ModulePolicyError as exc:
            return self._error(HTTPStatus.CONFLICT, str(exc))
        except (OSError, RuntimeError, ValueError):
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "模块设置暂不可用")
        if re.fullmatch(r"wechat-voice/audio/[a-f0-9]{64}\.wav", relative):
            if not scoped_ai._safe_path(root / relative, root) or not target.is_file():
                return self._error(HTTPStatus.NOT_FOUND, "语音副本不可用")
            return self._serve_voice_audio(target)
        allowed = {".html", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".tiff"}
        if not _inside(target, root) or target.suffix.lower() not in allowed or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "归档视图不存在")
        try:
            content = target.read_bytes()
        except OSError:
            return self._error(HTTPStatus.NOT_FOUND, "归档视图不可用")
        self.send_response(HTTPStatus.OK)
        if relative == "wechat-voice/index.html":
            # Only the generated voice gallery uses local external scripts and
            # same-origin media; other archived reports remain script-free.
            self._security_headers("text/html; charset=utf-8")
        elif target.suffix.lower() == ".html":
            self._report_security_headers()
        else:
            self._security_headers(mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_voice_audio(self, target):
        """Serve only authorized derived WAV files, with bounded range reads."""
        try:
            handle = target.open("rb")
        except OSError:
            return self._error(HTTPStatus.NOT_FOUND, "语音副本不可用")
        with handle:
            size = os.fstat(handle.fileno()).st_size
            start, end, status = 0, size - 1, HTTPStatus.OK
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if not match or not any(match.groups()) or size == 0:
                    return self._voice_range_error(size)
                left, right = match.groups()
                # Bound header integer conversion before parsing untrusted input.
                if max(len(left), len(right)) > 20:
                    return self._voice_range_error(size)
                if not left:
                    length = int(right)
                    if length <= 0:
                        return self._voice_range_error(size)
                    start = max(0, size - length)
                else:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                if start >= size or start > end:
                    return self._voice_range_error(size)
                status = HTTPStatus.PARTIAL_CONTENT
            self.send_response(status)
            self._security_headers("audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            handle.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = handle.read(min(128 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Closing a player must not produce data-bearing logs.

    def _voice_range_error(self, size):
        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self._security_headers("audio/wav")
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def create_server(host="127.0.0.1", port=8765, token=None):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Dashboard 只能绑定本机回环地址")
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    return DashboardServer((host, port), DashboardHandler, token or secrets.token_urlsafe(32))


def main():
    parser = argparse.ArgumentParser(description="启动『人类线上关系可视化』本地控制台")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(json.dumps({
        "status": "ready",
        "url": url,
        "local_only": True,
        "pid": os.getpid(),
    }, ensure_ascii=False), flush=True)
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
