"""Bounded, evidence-addressed AI timeline DTOs. Never reads source files."""
from __future__ import annotations

import re
from datetime import date

from dashboard.analysis_contract import (EVENT_KEYS, EVENT_KINDS, EVENT_STATUSES,
                                         EVIDENCE_LEVELS, MAX_EVENTS, REASON_KINDS)
from dashboard.analysis_errors import OutputValidationError

MAX_REPORT_EVENTS = 1200  # 200 existing leaf segments, six events each.
LIMITATIONS = [
    "仅描述本次所选样本，不代表完整会话；历史窗口不能解释当前未联系的原因。",
    "日期和匿名序号仅指向样本位置，不能证明内容属实；AI 推断须人工核实。",
    "跨段关联仅基于代表事件，可能尚未关联；缺少关联不等于没有推进或结果。",
]


def _fail(code, field):
    raise OutputValidationError(code, field) from None


def _object(value, keys, field, *, optional=()):
    if not isinstance(value, dict):
        _fail("OUTPUT_TYPE", field)
    # Only append locally declared keys, never provider-controlled key names.
    for key in sorted(keys):
        if key not in value:
            _fail("OUTPUT_FIELDS", f"{field}.{key}")
    if set(value) - set(keys) - set(optional):
        _fail("OUTPUT_FIELDS", field)


def _array(value, limit, field):
    if not isinstance(value, list):
        _fail("OUTPUT_TYPE", field)
    if len(value) > limit:
        _fail("OUTPUT_LIMIT", field)


def _event_field(index):
    return f"timeline.events[{index}]" if 0 <= index < MAX_REPORT_EVENTS else "timeline.events"


def _day(value, field="timeline.coverage"):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("OUTPUT_DATE", field)
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail("OUTPUT_DATE", field)
    return value


def _enum(value, choices, field):
    if not isinstance(value, str):
        _fail("OUTPUT_TYPE", field)
    if value not in choices:
        _fail("OUTPUT_ENUM", field)
    return value


def _text(value, limit, clean, field):
    if not isinstance(value, str):
        _fail("OUTPUT_TYPE", field)
    if len(value) > limit:
        _fail("OUTPUT_LIMIT", field)
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


def _evidence(value, allowed, field):
    _array(value, 3, field)
    result = []
    for position, ref in enumerate(value):
        ref_field = f"{field}[{position}]"
        _object(ref, {"date", "sample_index"}, ref_field)
        day, index = _day(ref["date"], ref_field + ".date"), ref["sample_index"]
        if type(index) is not int:
            _fail("OUTPUT_TYPE", ref_field + ".sample_index")
        if not 1 <= index <= 80_000 or (day, index) not in allowed:
            _fail("OUTPUT_EVIDENCE_REF", ref_field + ".sample_index")
        if ref in result:
            _fail("OUTPUT_EVIDENCE_DUP", ref_field)
        result.append({"date": day, "sample_index": index})
    return result


def _event_order(event):
    return (event["date_from"], min((ref["sample_index"] for ref in event["evidence"]), default=80_001))


def _links(events, *, fields=None, allow_external=False):
    by_id = {}
    for index, item in enumerate(events):
        if item["id"] in by_id:
            _fail("OUTPUT_DUPLICATE_ID", _event_field(index) + ".id")
        by_id[item["id"]] = item
    if fields is None:
        fields = {item["id"]: _event_field(index) for index, item in enumerate(events)}
    for item in events:
        seen, current = {item["id"]}, item
        while current.get("related_event_id") is not None:
            target = current["related_event_id"]
            field = fields.get(current["id"], fields.get(item["id"]))
            field = field + ".related_event_id" if field else "timeline.events"
            if target in seen:
                _fail("OUTPUT_LINK", field)
            if target not in by_id:
                if allow_external:
                    break  # An immutable old link can leave a compact packet.
                _fail("OUTPUT_LINK", field)
            previous = by_id[target]
            if _event_order(previous) > _event_order(current):
                _fail("OUTPUT_LINK", field)
            seen.add(target)
            current = previous


def validate(value, context, clean):
    """Validate a provider's six-event response against exactly its input.

    Merge responses may propose links between supplied event IDs, but cannot
    rewrite a leaf event's status, evidence or wording. Local union is lossless.
    """
    if value is None:
        return empty_timeline()
    _object(value, {"version", "generated", "events", "no_contact_reason"}, "timeline",
            optional={"coverage", "events_total", "events_truncated"})
    if type(value["version"]) is not int:
        _fail("OUTPUT_TYPE", "timeline.version")
    if value["version"] != 1:
        _fail("OUTPUT_ENUM", "timeline.version")
    if type(value["generated"]) is not bool:
        _fail("OUTPUT_TYPE", "timeline.generated")
    raw_events = value["events"]
    _array(raw_events, MAX_EVENTS, "timeline.events")
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
    if packets:
        # Leaf endpoints can name sample dates absent from their chosen evidence.
        # Admit supplied dates only; the exact immutable-field check below still
        # prevents borrowing another event's endpoint to rewrite an event.
        allowed_dates |= {e[key] for e in existing.values() for key in ("date_from", "date_to")}
    events = []
    prefix = f"s{context.get('segment', {}).get('index', 1)}-"
    for index, item in enumerate(raw_events):
        field = _event_field(index)
        _object(item, EVENT_KEYS, field)
        identity, related = item["id"], item["related_event_id"]
        pattern = r"s[1-9]\d{0,2}-e[1-6]" if packets else r"e[1-6]"
        if not isinstance(identity, str) or not re.fullmatch(pattern, identity):
            _fail("OUTPUT_ID", field + ".id")
        if related is not None and (not isinstance(related, str) or not re.fullmatch(pattern, related)):
            _fail("OUTPUT_ID", field + ".related_event_id")
        start, end = _day(item["date_from"], field + ".date_from"), _day(item["date_to"], field + ".date_to")
        if start > end:
            _fail("OUTPUT_DATE_SCOPE", field + ".date_to")
        if start not in allowed_dates or (context.get("date_from") and start < context["date_from"]):
            _fail("OUTPUT_DATE_SCOPE", field + ".date_from")
        if end not in allowed_dates or (context.get("date_to") and end > context["date_to"]):
            _fail("OUTPUT_DATE_SCOPE", field + ".date_to")
        kind = _enum(item["kind"], EVENT_KINDS, field + ".kind")
        status = _enum(item["status"], EVENT_STATUSES, field + ".status")
        level = _enum(item["evidence_level"], EVIDENCE_LEVELS, field + ".evidence_level")
        refs = _evidence(item["evidence"], allowed, field + ".evidence")
        for position, ref in enumerate(refs):
            if not start <= ref["date"] <= end:
                _fail("OUTPUT_EVIDENCE_RANGE", f"{field}.evidence[{position}].date")
        if level != "insufficient" and not refs:
            _fail("OUTPUT_EVIDENCE_REQUIRED", field + ".evidence")
        if level == "insufficient" and status != "unknown":
            _fail("OUTPUT_STATE", field + ".status")
        if kind == "plan" and status not in {"planned", "unknown"}:
            _fail("OUTPUT_STATE", field + ".status")
        if status in {"realized", "cancelled"} and (kind != "outcome" or level != "reported" or not refs):
            _fail("OUTPUT_STATE", field + ".status")
        event = {"id": identity if packets else prefix + identity, "date_from": start, "date_to": end,
                 "kind": kind, "title": _text(item["title"], 40, clean, field + ".title"),
                 "summary": _text(item["summary"], 140, clean, field + ".summary"),
                 "status": status, "evidence_level": level,
                 "related_event_id": related if packets or related is None else prefix + related, "evidence": refs}
        if packets:
            original = existing.get(identity)
            if original is None:
                _fail("OUTPUT_MERGE_CHANGED", field + ".id")
            for key in sorted(EVENT_KEYS - {"related_event_id"}):
                if event[key] != original[key]:
                    _fail("OUTPUT_MERGE_CHANGED", field + "." + key)
            if original["related_event_id"] is not None and related != original["related_event_id"]:
                _fail("OUTPUT_MERGE_CHANGED", field + ".related_event_id")
            if related != original["related_event_id"] and related not in existing:
                _fail("OUTPUT_LINK", field + ".related_event_id")
        events.append(event)
    if not packets:
        _links(events)
    else:
        fields = {}
        for index, event in enumerate(events):
            if event["id"] in fields:
                _fail("OUTPUT_DUPLICATE_ID", _event_field(index) + ".id")
            fields[event["id"]] = _event_field(index)
        # Resolve new links against every supplied representative, including
        # targets omitted from the response. The full union is checked again by
        # combine, where missing targets cannot be excused by packet truncation.
        merged = {**{e["id"]: e for e in events}, **{key: e for key, e in existing.items() if key not in fields}}
        _links(list(merged.values()), fields=fields, allow_external=True)
    reason = value.get("no_contact_reason")
    reason_field = "timeline.no_contact_reason"
    _object(reason, {"kind", "summary", "evidence", "limitations"}, reason_field)
    kind = _enum(reason["kind"], REASON_KINDS, reason_field + ".kind")
    summary = _text(reason["summary"], 140, clean, reason_field + ".summary")
    refs = _evidence(reason["evidence"], allowed, reason_field + ".evidence")
    _array(reason["limitations"], 3, reason_field + ".limitations")
    limitations = [_text(x, 140, clean, f"{reason_field}.limitations[{index}]")
                   for index, x in enumerate(reason["limitations"])]
    if kind != "unknown" and not refs:
        _fail("OUTPUT_EVIDENCE_REQUIRED", reason_field + ".evidence")
    inherited_reason = None
    if packets and kind != "unknown":
        inherited_reason = next((p["timeline"]["no_contact_reason"] for p in packets
            if p.get("timeline", {}).get("no_contact_reason", {}).get("kind") == kind
            and p["timeline"]["no_contact_reason"]["evidence"] == refs
            and p["timeline"]["no_contact_reason"]["summary"] == summary
            and p["timeline"]["no_contact_reason"]["limitations"] == limitations), None)
        if inherited_reason is None:
            _fail("OUTPUT_REASON_CHANGED", reason_field)
    reason = unknown_reason() if kind == "unknown" else {
        "kind": kind, "summary": summary, "evidence": refs,
        "limitations": limitations[:1] + ["这是样本中的当事人自述，并非核实的客观因果。" if kind == "explicit"
                                         else "这是模型推测，不是当事人明确说明，可能存在其他原因。", LIMITATIONS[0]]}
    if inherited_reason is not None:
        reason = inherited_reason
    if not value["generated"]:
        if events or kind != "unknown":
            _fail("OUTPUT_STATE", "timeline.generated")
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
        _fail("OUTPUT_LIMIT", "timeline.events")
    if proposal:
        for index, event in enumerate(proposal["events"]):
            field = _event_field(index)
            if event["id"] not in events:
                _fail("OUTPUT_MERGE_CHANGED", field + ".id")
            original = events[event["id"]]
            for key in sorted(EVENT_KEYS - {"related_event_id"}):
                if event[key] != original[key]:
                    _fail("OUTPUT_MERGE_CHANGED", field + "." + key)
            # A later summary may connect an orphan; it must not erase a link
            # already grounded by an earlier segment or merge.
            if event["related_event_id"] is not None:
                if events[event["id"]]["related_event_id"] not in {None, event["related_event_id"]}:
                    _fail("OUTPUT_MERGE_CHANGED", field + ".related_event_id")
                events[event["id"]]["related_event_id"] = event["related_event_id"]
    result["events"] = sorted(events.values(), key=lambda e: (_event_order(e), e["date_to"], e["id"]))
    _links(result["events"])
    result["generated"] = any(v["generated"] for v in values) or bool(proposal and proposal["generated"])
    result["events_total"] = len(result["events"])
    if proposal and proposal["generated"]:
        result["no_contact_reason"] = proposal["no_contact_reason"]
    return result
