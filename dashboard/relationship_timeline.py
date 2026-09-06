"""Content-free local relationship activity, distinct from real-world contact.

Only timestamp, sender bucket, and system-message status are inspected. The
caller supplies one conversation's complete archive; date selection changes
historical charts, never the whole-archive recency reference. No I/O or model
calls occur here. All displayed dates use the host's local timezone, while
elapsed days use epoch-second differences (complete 24-hour periods).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import median

from dashboard.analysis_metrics import _date_bounds, _message_time


MAX_WEEKS = 260
MAX_PHASES = 50
MAX_GAPS = 50
MAX_NODES = 100
DAY_SECONDS = 24 * 60 * 60
MIN_GAP_SECONDS = 7 * DAY_SECONDS
UNKNOWN_REASON = "仅凭消息时间不能判断停联原因；可能存在未同步消息、其他平台或线下联系。"
NOTICES = (
    "本机归档不等于现实中的全部联系，不能据此断言线下或其他平台已经停联。",
    "距最近归档消息的天数，以服务端本次时钟为准，统计全会话有效非系统、非未来记录；不以所选历史区间末尾代替现在。",
    "时间按本机时区显示；天数为完整 24 小时，跨过午夜不自动增加一天。",
    "来源最大日期包含可解析的未来及系统记录，仅供时钟和归档核对，不作为最近联系日期。",
    "无效时间（含 2000—2100 年以外）、未来时间和系统消息不进入时间线；排除计数按此顺序互斥统计。",
    "频繁周为消息数达到 max(20, 活跃周消息数中位数 × 1.5 向上取整) 的周；相邻频繁周合并为阶段，不代表感情强弱。",
    "周从本机周一开始，只统计有消息的周；所选范围边界可能是不完整周，阶段日期是该阶段实际记录的首末日期。",
    "归档间隔仅指所选范围相邻有效消息相隔至少 7 个完整天；不含最后记录到现在的尾部间隔，不推断原因。",
    "对方发送时间包含其他参与者（群聊）及无法识别为我的发送方；它不是某个特定人的已读或回复证明。",
    "列表仅保留最近的有限条目，节点另保留范围首末记录；截断标记和总数用于区分显示范围与统计范围。",
)


def _iso(stamp):
    return datetime.fromtimestamp(stamp).astimezone().isoformat() if stamp is not None else None


def _clock_stamp(now):
    try:
        if now is None:
            stamp = datetime.now().timestamp()
        elif isinstance(now, datetime):
            stamp = now.timestamp()
        else:
            raise ValueError
        if not math.isfinite(stamp):
            raise ValueError
        # Validate the clock before processing any records, including empty input.
        _iso(stamp)
        return stamp
    except (ValueError, TypeError, OSError, OverflowError):
        raise ValueError("当前时间格式无效") from None


def _limit(items, maximum):
    kept = items[-maximum:]
    return kept, {"total": len(items), "returned": len(kept), "truncated": len(items) > maximum}


def _frequent_phases(weeks, threshold):
    """Merge adjacent qualifying observed weeks, never materialize empty weeks."""
    phases = []
    previous_week = None
    for week, value in weeks:
        if value["count"] < threshold:
            previous_week = None
            continue
        if previous_week is not None and week - previous_week == timedelta(days=7):
            phase = phases[-1]
            phase["end"] = value["last"]
            phase["weeks"] += 1
            phase["message_count"] += value["count"]
        else:
            phases.append({"start": value["first"], "end": value["last"],
                           "weeks": 1, "message_count": value["count"]})
        previous_week = week
    return phases


def build_relationship_timeline(payload, scope, now=None):
    """Return safe aggregates for one full archive plus a selected date scope.

    ``now`` may be a naive (host-local) or timezone-aware datetime. Omission
    reads the real clock on every call. Malformed scopes/clocks fail closed.
    No IDs, names, content, custom labels, or filesystem paths are returned.
    """
    date_from, date_to = _date_bounds(scope)
    now_stamp = _clock_stamp(now)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        messages = []
    excluded = {"invalid_timestamp": 0, "future_timestamp": 0, "system_messages": 0}
    source_max_stamp = None
    last_by_sender = {"me": None, "other": None}
    last_record = None
    scoped, weekly_counts = [], {}
    for message in messages:
        parsed = _message_time(message.get("timestamp")) if isinstance(message, dict) else None
        if parsed is None:
            excluded["invalid_timestamp"] += 1
            continue
        stamp, local = parsed
        if source_max_stamp is None or stamp > source_max_stamp:
            source_max_stamp = stamp
        if stamp > now_stamp:
            excluded["future_timestamp"] += 1
            continue
        if message.get("type") == "system" or message.get("sender") == "system":
            excluded["system_messages"] += 1
            continue
        sender = "me" if message.get("sender") == "me" else "other"
        if last_by_sender[sender] is None or stamp > last_by_sender[sender]:
            last_by_sender[sender] = stamp
        if last_record is None or stamp > last_record:
            last_record = stamp
        day = local.date()
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        scoped.append(stamp)
        week = day - timedelta(days=day.weekday())
        counts = weekly_counts.setdefault(week, {"count": 0, "first": stamp, "last": stamp})
        counts["count"] += 1
        counts["first"] = min(counts["first"], stamp)
        counts["last"] = max(counts["last"], stamp)

    scoped.sort()
    weeks = sorted(weekly_counts.items())
    baseline = median(value["count"] for _, value in weeks) if weeks else 0
    threshold = max(20, math.ceil(1.5 * baseline))
    all_phases = _frequent_phases(weeks, threshold)
    weekly, weekly_limit = _limit([
        {"week_start": week.isoformat(), "count": value["count"], "frequent": value["count"] >= threshold}
        for week, value in weeks
    ], MAX_WEEKS)
    phases, phase_limit = _limit([
        {"start_date": datetime.fromtimestamp(phase["start"]).date().isoformat(),
         "end_date": datetime.fromtimestamp(phase["end"]).date().isoformat(),
         "weeks": phase["weeks"], "message_count": phase["message_count"]}
        for phase in all_phases
    ], MAX_PHASES)
    all_gaps = [(previous, stamp, int((stamp - previous) // DAY_SECONDS))
                for previous, stamp in zip(scoped, scoped[1:]) if stamp - previous >= MIN_GAP_SECONDS]
    gaps, gap_limit = _limit([
        {"start_at": _iso(start), "end_at": _iso(end), "elapsed_days": days}
        for start, end, days in all_gaps
    ], MAX_GAPS)
    # Retain numeric ordering even across timezone/DST transitions. Gap nodes
    # mark the first record after the interval; phases mark their first record.
    event_nodes = [(phase["start"], {"type": "frequent_phase", "at": _iso(phase["start"]),
                                   "end_at": _iso(phase["end"]), "message_count": phase["message_count"]})
                   for phase in all_phases]
    event_nodes += [(end, {"type": "gap", "at": _iso(end), "start_at": _iso(start), "elapsed_days": days})
                    for start, end, days in all_gaps]
    event_nodes.sort(key=lambda item: item[0])
    nodes = [node for _, node in event_nodes[-(MAX_NODES - 2):]] if scoped else []
    if scoped:
        nodes.insert(0, {"type": "first_record", "at": _iso(scoped[0])})
        nodes.append({"type": "last_record", "at": _iso(scoped[-1])})
    total_nodes = len(event_nodes) + (2 if scoped else 0)
    node_limit = {"total": total_nodes, "returned": len(nodes), "truncated": total_nodes > len(nodes)}

    return {
        "version": 1,
        "basis": "local_archive",
        "as_of": _iso(now_stamp),
        "source_max_date": datetime.fromtimestamp(source_max_stamp).date().isoformat() if source_max_stamp is not None else None,
        "last_contact": {
            "basis": "whole_archive_non_system_non_future",
            "last_record_at": _iso(last_record),
            "elapsed_days": int((now_stamp - last_record) // DAY_SECONDS) if last_record is not None else None,
            "last_me_at": _iso(last_by_sender["me"]),
            "last_other_at": _iso(last_by_sender["other"]),
            "reason_status": "unknown",
            "reason": UNKNOWN_REASON,
        },
        "scope": {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "message_count": len(scoped),
            "first_record_at": _iso(scoped[0]) if scoped else None,
            "last_record_at": _iso(scoped[-1]) if scoped else None,
        },
        "frequency": {
            "week_start": "monday", "threshold": threshold,
            "baseline_active_week_median": baseline, "active_weeks": len(weeks),
            "weekly": weekly, "phases": phases,
        },
        "gaps": gaps,
        "nodes": nodes,
        "excluded": excluded,
        "limits": {"weekly": weekly_limit, "phases": phase_limit, "gaps": gap_limit, "nodes": node_limit},
        "notices": list(NOTICES),
    }
