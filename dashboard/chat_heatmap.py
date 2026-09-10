"""Bounded, content-free activity counts for one host-local date window.

Only timestamps and message types are inspected. No file access, sender lookup,
content inspection, deduplication, or network work belongs in this module.
"""

from datetime import date, datetime, timedelta

from dashboard.analysis_metrics import _date_bounds, _message_time
from dashboard.relationship_timeline import _clock_stamp, _iso


MAX_DAYS = 366
DEFAULT_DAYS = 90
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 12, 31)


def build_chat_heatmap(payload, scope, now=None):
    """Return fixed-schema aggregates; malformed scopes fail with fixed errors.

    Two linear passes keep intermediate storage bounded by the selected window,
    even when the supplied archive contains many years or duplicate records.
    """
    date_from, date_to = _date_bounds(scope)
    has_from, has_to = "date_from" in scope, "date_to" in scope
    if has_from != has_to or (has_from and (date_from is None or date_to is None)):
        raise ValueError("请同时提供有效的开始和结束日期")
    if date_from is not None:
        if date_from < MIN_DATE or date_to > MAX_DATE:
            raise ValueError("日期范围限于 2000—2100 年")
        if (date_to - date_from).days + 1 > MAX_DAYS:
            raise ValueError("日期范围最多 366 天")
    now_stamp = _clock_stamp(now)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        messages = []

    excluded = {"invalid_timestamp": 0, "future_timestamp": 0, "system_messages": 0}
    first_day = last_day = None
    for message in messages:
        parsed = _message_time(message.get("timestamp")) if isinstance(message, dict) else None
        if parsed is None:
            excluded["invalid_timestamp"] += 1
            continue
        stamp, local = parsed
        if stamp > now_stamp:
            excluded["future_timestamp"] += 1
            continue
        if message.get("type") == "system":
            excluded["system_messages"] += 1
            continue
        day = local.date()
        if first_day is None or day < first_day:
            first_day = day
        if last_day is None or day > last_day:
            last_day = day

    if date_from is None:
        date_to = last_day or datetime.fromtimestamp(now_stamp).date()
        if not MIN_DATE <= date_to <= MAX_DATE:
            raise ValueError("当前时间格式无效")
        date_from = max(MIN_DATE, date_to - timedelta(days=DEFAULT_DAYS - 1))
    day_count = (date_to - date_from).days + 1
    daily_counts = [0] * day_count
    weekday_hour = [[0] * 24 for _ in range(7)]
    for message in messages:
        parsed = _message_time(message.get("timestamp")) if isinstance(message, dict) else None
        if parsed is None:
            continue
        stamp, local = parsed
        if stamp > now_stamp or message.get("type") == "system":
            continue
        if date_from <= local.date() <= date_to:
            daily_counts[(local.date() - date_from).days] += 1
            weekday_hour[local.weekday()][local.hour] += 1

    days = [{"date": (date_from + timedelta(days=index)).isoformat(), "count": count}
            for index, count in enumerate(daily_counts)]
    active = [day for day in days if day["count"] > 0]
    top_days = sorted(active, key=lambda day: (-day["count"], day["date"]))[:7]
    message_count = sum(daily_counts)
    return {
        "version": 1,
        "as_of": _iso(now_stamp),
        "timezone": "server_local",
        "available_range": {"date_from": first_day.isoformat() if first_day else None,
                            "date_to": last_day.isoformat() if last_day else None},
        "scope": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "days": day_count},
        "days": days,
        "weekday_hour": weekday_hour,
        "summary": {"message_count": message_count, "active_days": len(active),
                    "daily_average": round(message_count / day_count, 2),
                    "peak_day": top_days[0] if top_days else None},
        "top_days": top_days,
        "excluded": excluded,
    }
