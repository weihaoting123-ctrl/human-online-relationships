"""Bounded, evidence-addressed AI timeline DTOs. Never reads source files."""
from __future__ import annotations

import re
from datetime import date

MAX_EVENTS = 6
MAX_REPORT_EVENTS = 1200  # 200 existing leaf segments, six events each.
EVENT_KEYS = {"id", "date_from", "date_to", "kind", "title", "summary", "status",
              "evidence_level", "related_event_id", "evidence"}
LIMITATIONS = [
    "仅描述本次所选样本，不代表完整会话；历史窗口不能解释当前未联系的原因。",
    "日期和匿名序号仅指向样本位置，不能证明内容属实；AI 推断须人工核实。",
    "跨段关联仅基于代表事件，可能尚未关联；缺少关联不等于没有推进或结果。",
]


def _fail():
    from dashboard.analysis import AnalysisError
    raise AnalysisError("时间线证据或格式无效；本次不会自动重试")


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail()
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail()
    return value


def _enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        _fail()
    return value


def _text(value, limit, clean):
    if not isinstance(value, str) or len(value) > limit:
        _fail()
    return clean(value, limit)


def unknown_reason():
    return {"kind": "unknown", "summary": "所选样本不足以说明未联系原因。", "evidence": [],
            "limitations": ["没有消息不是感情、动机或因果证据；线下互动与未选记录未知。", LIMITATIONS[0]]}


def coverage(scope=None, sample=(), *, complete=False, analyzed=None):
    scope = scope or {}
    dates = sorted({_day(row["date"]) for row in sample})
    return {"date_from": dates[0] if dates else None, "date_to": dates[-1] if dates else None,
            "analysis_mode": scope.get("analysis_mode", "sample"),
            "analyzed_messages": len({row.get("sample_index", i + 1) for i, row in enumerate(sample)}) if analyzed is None else analyzed,
            "complete": complete, "scope": "selected_sample",
            "limitations": LIMITATIONS + ([] if complete else ["时间线未生成或分段未完成；未覆盖范围不能据此推断。"])}


def empty_timeline():
    return {"version": 1, "generated": False, "events": [], "events_total": 0, "events_truncated": False,
            "no_contact_reason": unknown_reason(), "coverage": coverage()}


def _evidence(value, allowed):
    if not isinstance(value, list) or len(value) > 3:
        _fail()
    result = []
    for ref in value:
        if not isinstance(ref, dict) or set(ref) != {"date", "sample_index"}:
            _fail()
        day, index = _day(ref["date"]), ref["sample_index"]
        if type(index) is not int or not 1 <= index <= 80_000 or (day, index) not in allowed:
            _fail()
        if ref in result:
            _fail()
        result.append({"date": day, "sample_index": index})
    return result


def _event_order(event):
    return (event["date_from"], min((ref["sample_index"] for ref in event["evidence"]), default=80_001))


def _links(events):
    by_id = {item["id"]: item for item in events}
    if len(by_id) != len(events):
        _fail()
    for item in events:
        seen, current = {item["id"]}, item
        while current.get("related_event_id") is not None:
            target = current["related_event_id"]
            if target in seen or target not in by_id:
                _fail()
            previous = by_id[target]
            if _event_order(previous) > _event_order(current):
                _fail()
            seen.add(target)
            current = previous


def validate(value, context, clean):
    """Validate a provider's six-event response against exactly its input.

    Merge responses may propose links between supplied event IDs, but cannot
    rewrite a leaf event's status, evidence or wording. Local union is lossless.
    """
    if value is None:
        return empty_timeline()
    if not isinstance(value, dict) or set(value) - {"version", "generated", "events", "no_contact_reason", "coverage", "events_total", "events_truncated"}:
        _fail()
    if type(value.get("version")) is not int or value["version"] != 1 or type(value.get("generated")) is not bool:
        _fail()
    raw_events = value.get("events")
    if not isinstance(raw_events, list) or len(raw_events) > MAX_EVENTS:
        _fail()
    context = context or {}
    sample = context.get("sample", [])
    packets = context.get("segment_summaries", [])
    existing = {e["id"]: e for p in packets for e in p.get("timeline", {}).get("events", [])}
    allowed = {(row["date"], row.get("sample_index", i + 1)) for i, row in enumerate(sample)}
    if packets:
        allowed |= {(r["date"], r["sample_index"]) for e in existing.values() for r in e["evidence"]}
        allowed |= {(r["date"], r["sample_index"]) for p in packets
                    for r in p.get("timeline", {}).get("no_contact_reason", {}).get("evidence", [])}
    allowed_dates = {day for day, _ in allowed}
    events = []
    prefix = f"s{context.get('segment', {}).get('index', 1)}-"
    for item in raw_events:
        if not isinstance(item, dict) or set(item) != EVENT_KEYS:
            _fail()
        identity, related = item["id"], item["related_event_id"]
        pattern = r"s[1-9]\d{0,2}-e[1-6]" if packets else r"e[1-6]"
        if not isinstance(identity, str) or not re.fullmatch(pattern, identity):
            _fail()
        if related is not None and (not isinstance(related, str) or not re.fullmatch(pattern, related)):
            _fail()
        start, end = _day(item["date_from"]), _day(item["date_to"])
        if start > end or start not in allowed_dates or end not in allowed_dates:
            _fail()
        if (context.get("date_from") and start < context["date_from"]) or (context.get("date_to") and end > context["date_to"]):
            _fail()
        kind = _enum(item["kind"], {"plan", "update", "outcome", "context"})
        status = _enum(item["status"], {"planned", "in_progress", "realized", "cancelled", "unknown"})
        level = _enum(item["evidence_level"], {"reported", "inferred", "insufficient"})
        refs = _evidence(item["evidence"], allowed)
        if any(not start <= r["date"] <= end for r in refs):
            _fail()
        if level != "insufficient" and not refs:
            _fail()
        if level == "insufficient" and status != "unknown":
            _fail()
        if kind == "plan" and status not in {"planned", "unknown"}:
            _fail()
        if status in {"realized", "cancelled"} and (kind != "outcome" or level != "reported" or not refs):
            _fail()
        event = {"id": identity if packets else prefix + identity, "date_from": start, "date_to": end,
                 "kind": kind, "title": _text(item["title"], 40, clean), "summary": _text(item["summary"], 140, clean),
                 "status": status, "evidence_level": level,
                 "related_event_id": related if packets or related is None else prefix + related, "evidence": refs}
        if packets:
            original = existing.get(identity)
            if original is None or any(event[k] != original[k] for k in EVENT_KEYS - {"related_event_id"}):
                _fail()
            if related != original["related_event_id"] and related not in existing:
                _fail()
            if original["related_event_id"] is not None and related != original["related_event_id"]:
                _fail()
        events.append(event)
    if not packets:
        _links(events)
    elif len({e["id"] for e in events}) != len(events):
        _fail()
    reason = value.get("no_contact_reason")
    if not isinstance(reason, dict) or set(reason) != {"kind", "summary", "evidence", "limitations"}:
        _fail()
    kind = _enum(reason["kind"], {"explicit", "inferred", "unknown"})
    summary = _text(reason["summary"], 140, clean)
    refs = _evidence(reason["evidence"], allowed)
    if not isinstance(reason["limitations"], list) or len(reason["limitations"]) > 3:
        _fail()
    limitations = [_text(x, 140, clean) for x in reason["limitations"]]
    if kind != "unknown" and not refs:
        _fail()
    inherited_reason = None
    if packets and kind != "unknown":
        inherited_reason = next((p["timeline"]["no_contact_reason"] for p in packets
            if p.get("timeline", {}).get("no_contact_reason", {}).get("kind") == kind
            and p["timeline"]["no_contact_reason"]["evidence"] == refs
            and p["timeline"]["no_contact_reason"]["summary"] == summary), None)
        if inherited_reason is None:
            _fail()
    reason = unknown_reason() if kind == "unknown" else {
        "kind": kind, "summary": summary, "evidence": refs,
        "limitations": limitations[:1] + ["这是样本中的当事人自述，并非核实的客观因果。" if kind == "explicit"
                                         else "这是模型推测，不是当事人明确说明，可能存在其他原因。", LIMITATIONS[0]]}
    if inherited_reason is not None:
        reason = inherited_reason
    if not value["generated"]:
        if events or kind != "unknown":
            _fail()
        return empty_timeline()
    cov = coverage(context, sample, complete=True)
    if packets:
        dates = sorted(allowed_dates)
        cov.update(date_from=dates[0] if dates else None, date_to=dates[-1] if dates else None,
                   analyzed_messages=sum(p.get("messages", 0) for p in packets))
    return {"version": 1, "generated": True, "events": events, "events_total": len(events), "events_truncated": False,
            "no_contact_reason": reason, "coverage": cov}


def compact(value):
    """Send bounded representatives; the complete graph stays only in cache."""
    value = value or empty_timeline()
    all_events = value["events"]
    indices = ([round(i * (len(all_events) - 1) / (MAX_EVENTS - 1)) for i in range(MAX_EVENTS)]
               if len(all_events) > MAX_EVENTS else range(len(all_events)))
    events = [all_events[index] for index in indices]
    return {**value, "events": events, "events_total": len(value["events"]),
            "events_truncated": len(events) < len(value["events"])}


def combine(values, proposal=None):
    """Keep every leaf event; merge models can add links, never outcomes."""
    values = [value for value in values if value]
    result = empty_timeline()
    events = {event["id"]: dict(event) for value in values for event in value["events"]}
    if len(events) > MAX_REPORT_EVENTS:
        _fail()
    if proposal:
        for event in proposal["events"]:
            if event["id"] not in events or any(event[k] != events[event["id"]][k] for k in EVENT_KEYS - {"related_event_id"}):
                _fail()
            # A later summary may connect an orphan; it must not erase a link
            # already grounded by an earlier segment or merge.
            if event["related_event_id"] is not None:
                if events[event["id"]]["related_event_id"] not in {None, event["related_event_id"]}:
                    _fail()
                events[event["id"]]["related_event_id"] = event["related_event_id"]
    result["events"] = sorted(events.values(), key=lambda e: (_event_order(e), e["date_to"], e["id"]))
    _links(result["events"])
    result["generated"] = any(v["generated"] for v in values) or bool(proposal and proposal["generated"])
    result["events_total"] = len(result["events"])
    if proposal and proposal["generated"]:
        result["no_contact_reason"] = proposal["no_contact_reason"]
    return result
