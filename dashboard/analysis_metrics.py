"""Deterministic local aggregates for one selected conversation/date scope.

This module does not read files, open connections, or return message content.
Dates and hours follow the machine's local timezone, as the dashboard does.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta


MESSAGE_TYPES = ("text", "image", "voice", "video", "emoji", "file", "system", "other")
MAX_ACTIVITY_BUCKETS = 366
SESSION_GAP_SECONDS = 30 * 60
MAX_RESPONSE_GAP_SECONDS = 24 * 60 * 60
METHODOLOGY = (
    "统计覆盖所选日期范围内全部有效消息，不以 AI 文本样本代替全量统计。",
    "日期、小时和星期按本机时区统计；无效时间及 2000—2100 年以外的记录不计入。",
    "发送方分为我和对方／其他参与者，后者包含群聊中的其他参与者。",
    "回复间隔仅为发送方切换时，上一连续发言的最后一条与下一发言的第一条之间的时间差；不代表已读时间或实际回复对象。",
    "超过 24 小时的发送方切换间隔不计入回复间隔；中位数和 P90 使用线性插值。",
    "相邻消息间隔超过 30 分钟计为新会话，范围内首条消息计为一个会话。",
    "字符数仅统计文字正文和已有语音转写，包含空格及标点；媒体描述不计入，语音转写可能存在误差。",
    "首末消息覆盖的日历范围超过 366 天时按月份合并，必要时合并相邻月份，使图表不超过 366 个分组。",
    "这些统计描述可观察的交流活动，不判断感情、真实动机或心理状态。",
)


def _message_time(value):
    """Return (epoch seconds, local datetime), or None for ineligible input.

    Only seconds and milliseconds are accepted as numeric units: repeatedly
    shrinking enormous values can turn a malformed timestamp into a real date.
    ISO strings remain compatible with existing offline importers.
    """
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            if len(value) > 128:
                return None
            try:
                number = float(value)
            except ValueError:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                stamp = parsed.timestamp()
                local = datetime.fromtimestamp(stamp)
                return (stamp, local) if 2000 <= local.year <= 2100 else None
        elif isinstance(value, (int, float)):
            number = float(value)
        else:
            return None
        if not math.isfinite(number):
            return None
        if number >= 100_000_000_000:
            number /= 1000
        local = datetime.fromtimestamp(number)
        return (number, local) if 2000 <= local.year <= 2100 else None
    except (ValueError, OSError, OverflowError):
        return None


def _date_bounds(scope):
    """Fail closed on malformed bounds without reflecting untrusted values."""
    if not isinstance(scope, dict):
        raise ValueError("日期范围格式无效")
    bounds = []
    for key in ("date_from", "date_to"):
        value = scope.get(key, "")
        if not isinstance(value, str):
            raise ValueError("日期范围格式无效")
        if not value:
            bounds.append(None)
            continue
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError
            bounds.append(date.fromisoformat(value))
        except ValueError:
            raise ValueError("日期范围格式无效") from None
    if bounds[0] and bounds[1] and bounds[0] > bounds[1]:
        raise ValueError("开始日期不能晚于结束日期")
    return bounds


def _percentile(values, fraction):
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 2)


def _response_stats(values):
    values.sort()
    return {
        "samples": len(values),
        "median_seconds": _percentile(values, 0.5),
        "p90_seconds": _percentile(values, 0.9),
    }


def _activity(daily):
    """Produce dense calendar bins without iterating an untrusted date span."""
    if not daily:
        return [], "day", 1
    first, last = min(daily), max(daily)
    span_days = (last - first).days + 1
    if span_days <= MAX_ACTIVITY_BUCKETS:
        return [
            {"date": day.isoformat(), **daily.get(day, {"count": 0, "me": 0, "other": 0})}
            for day in (first + timedelta(days=index) for index in range(span_days))
        ], "day", 1

    first_month = first.year * 12 + first.month - 1
    last_month = last.year * 12 + last.month - 1
    span_months = last_month - first_month + 1
    month_step = math.ceil(span_months / MAX_ACTIVITY_BUCKETS)
    bins = []
    for offset in range(0, span_months, month_step):
        year, month = divmod(first_month + offset, 12)
        bins.append({"date": f"{year:04d}-{month + 1:02d}", "count": 0, "me": 0, "other": 0})
    for day, counts in daily.items():
        index = (day.year * 12 + day.month - 1 - first_month) // month_step
        for key in ("count", "me", "other"):
            bins[index][key] += counts[key]
    return bins, "month", month_step


def build_metrics(payload, scope, *, now=None):
    """Build a JSON-safe, content-free aggregate of all eligible scoped records.

    Empty/malformed payloads produce empty aggregates. Malformed date scopes
    raise fixed ValueErrors rather than silently expanding the selected scope.
    """
    # Lazy import keeps the existing strict time/scope parsers shared without
    # changing their behavior or introducing an import-time dependency cycle.
    from dashboard.relationship_timeline import build_relationship_timeline

    date_from, date_to = _date_bounds(scope)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        messages = []
    totals = {
        "messages": 0, "me": 0, "other": 0, "text": 0, "voice": 0,
        "transcribed_voice": 0, "characters": 0, "active_days": 0,
        "sessions": 0, "first_date": None, "last_date": None,
    }
    type_counts = dict.fromkeys(MESSAGE_TYPES, 0)
    hour_counts = [0] * 24
    weekday_counts = [0] * 7
    daily, timeline = {}, []
    for message in messages:
        if not isinstance(message, dict):
            continue
        parsed = _message_time(message.get("timestamp"))
        if parsed is None:
            continue
        stamp, local = parsed
        day = local.date()
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        sender = "me" if message.get("sender") == "me" else "other"
        kind = message.get("type", "text")
        if not isinstance(kind, str) or kind not in MESSAGE_TYPES:
            kind = "other"
        totals["messages"] += 1
        totals[sender] += 1
        type_counts[kind] += 1
        text = None
        if kind == "text":
            totals["text"] += 1
            text = message.get("content")
        elif kind == "voice":
            totals["voice"] += 1
            text = next((message.get(key) for key in ("transcript", "voice_transcript")
                         if isinstance(message.get(key), str) and message[key].strip()), None)
            if text is not None:
                totals["transcribed_voice"] += 1
        if isinstance(text, str):
            totals["characters"] += len(text)
        hour_counts[local.hour] += 1
        weekday_counts[local.weekday()] += 1
        counts = daily.setdefault(day, {"count": 0, "me": 0, "other": 0})
        counts["count"] += 1
        counts[sender] += 1
        # Only numeric time and an allowlisted sender enter the sorted timeline.
        timeline.append((stamp, sender))

    timeline.sort(key=lambda item: item[0])
    gaps = {"me": [], "other": []}
    previous = None
    for stamp, sender in timeline:
        if previous is None:
            totals["sessions"] = 1
        else:
            gap = stamp - previous[0]
            if gap > SESSION_GAP_SECONDS:
                totals["sessions"] += 1
            if sender != previous[1] and gap <= MAX_RESPONSE_GAP_SECONDS:
                gaps[sender].append(gap)
        previous = (stamp, sender)
    totals["active_days"] = len(daily)
    if daily:
        totals["first_date"] = min(daily).isoformat()
        totals["last_date"] = max(daily).isoformat()
    activity, granularity, month_step = _activity(daily)
    return {
        "version": 1,
        "basis": "selected_scope_all_messages",
        "totals": totals,
        "activity": activity,
        "activity_granularity": granularity,
        "activity_month_step": month_step,
        "hours": [{"hour": hour, "count": count} for hour, count in enumerate(hour_counts)],
        "weekdays": [{"weekday": day, "count": count} for day, count in enumerate(weekday_counts)],
        "types": [{"type": kind, "count": count} for kind, count in type_counts.items()],
        "response_times": {sender: _response_stats(values) for sender, values in gaps.items()},
        "methodology": list(METHODOLOGY),
        "timeline": build_relationship_timeline(payload, scope, now=now),
    }
