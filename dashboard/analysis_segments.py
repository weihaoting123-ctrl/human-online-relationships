"""Bounded map/reduce analysis, durable billing guards and local checkpoints.

Only the parent analysis.run consent gate calls execute. Raw text is never in
cache records or public job/plan metadata. Completed results are DPAPI sealed.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone

from dashboard import timeline_schema as timeline

CHUNK_MESSAGES = 400
CHUNK_CHARS = 24_000
MAX_SEGMENTS = 200
MERGE_FANIN = 6
PIPELINE_VERSION = "selected-segments-v1"


def _ai():
    from dashboard import analysis
    return analysis


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def prompt_revision():
    a = _ai()
    return _digest([a.SYSTEM_PROMPT, a.MERGE_PROMPT])


def _partition(sample):
    segments, rows, indices, chars = [], [], [], 0

    def flush():
        nonlocal rows, indices, chars
        if not rows:
            return
        segments.append({"index": len(segments) + 1, "sample": rows,
                         "message_indices": indices, "messages": len(set(indices)),
                         "characters": chars, "date_from": rows[0]["date"], "date_to": rows[-1]["date"]})
        rows, indices, chars = [], [], 0
        if len(segments) > MAX_SEGMENTS:
            raise _ai().AnalysisError("所选范围需要超过 200 段，请缩短日期区间；未截断或发送任何内容")

    for index, row in enumerate(sample):
        text = row["text"]
        parts = max(1, (len(text) + CHUNK_CHARS - 1) // CHUNK_CHARS)
        for part in range(parts):
            fragment = text[part * CHUNK_CHARS:(part + 1) * CHUNK_CHARS]
            if rows and (len(rows) >= CHUNK_MESSAGES or chars + len(fragment) > CHUNK_CHARS):
                flush()
            record = dict(row, text=fragment, sample_index=index + 1)
            if parts > 1:
                record.update(fragment_part=part + 1, fragment_total=parts)
            rows.append(record)
            indices.append(index)
            chars += len(fragment)
    flush()
    return segments


def _nodes(config, prepared):
    a = _ai()
    identity = {"version": PIPELINE_VERSION, "provider": config.get("provider"),
                "model": config.get("model"), "revision": config.get("revision"),
                "scope": prepared["scope"], "counts": prepared["counts"],
                "prompts": prompt_revision()}
    segments = _partition(prepared["sample"])
    for item in segments:
        # Equal repeated messages are still distinct chronological occurrences.
        # Position is part of the request's context, never an exported raw ID.
        item["key"] = _digest([identity, "segment", item["index"], len(segments),
                               item["message_indices"], item["sample"]])
    levels, current = [], segments
    while len(current) > 1:
        following, level = [], []
        for start in range(0, len(current), MERGE_FANIN):
            group = current[start:start + MERGE_FANIN]
            if len(group) == 1:
                following.append(group[0])
                continue
            item = {"key": _digest([identity, "merge", [child["key"] for child in group]]),
                    "children": group, "date_from": group[0]["date_from"], "date_to": group[-1]["date_to"],
                    "message_indices": sorted(set(i for child in group for i in child["message_indices"]))}
            item["messages"] = len(item["message_indices"])
            following.append(item)
            level.append(item)
        levels.append(level)
        current = following
    return segments, levels, current[0]


def _cache(root, key):
    a = _ai()
    value = a._read(root / f"call-{key}.json", {})
    if not isinstance(value, dict) or (value and value.get("state") not in {"pending", "completed", "error"}):
        raise a.AnalysisError("本机调用状态不可用；不会自动重新计费")
    if value.get("state") == "completed":
        result = a._unseal(value.get("sealed_result"))
        if not isinstance(result, dict) or not isinstance(result.get("summary"), str) or any(
            not isinstance(result.get(field), list) or any(not isinstance(x, str) for x in result[field])
            for field in ("observations", "actions", "caveats")
        ):
            raise a.AnalysisError("本机分段缓存不可用；不会自动重新计费")
        value = {**value, "result": result}
    return value


def plan(root, config, prepared):
    segments, levels, _ = _nodes(config, prepared)
    nodes = segments + [item for level in levels for item in level]
    caches = [_cache(root, item["key"]) for item in nodes]
    cached = sum(item.get("state") == "completed" for item in caches)
    blocked = sum(item.get("state") in {"pending", "error"} for item in caches)
    chars = sum(item["characters"] for item in segments)
    merge_calls = len(nodes) - len(segments)
    return {"mode": prepared["scope"].get("analysis_mode", "sample"), "segments": len(segments),
            "total_calls": len(nodes), "cached_calls": cached, "new_calls": len(nodes) - cached,
            "blocked_calls": blocked, "merge_calls": merge_calls, "characters": chars,
            "split_messages": sum(len(row["text"]) > CHUNK_CHARS for row in prepared["sample"]),
            # Character/JSON budget estimate, not measured tokenizer usage or a monetary quote.
            "estimated_input_tokens": chars * 3 + len(prepared["sample"]) * 100 + len(segments) * 2000 + merge_calls * 120_000,
            "output_token_limit": 2400, "requires_multiple_calls": len(nodes) > 1,
            "estimate_note": "字符预算粗估，包含多级汇总；不是精确 token 数或费用，实际按服务商计费。"}


def approved_calls(root, config, prepared):
    """Private approval set; cache fingerprints never appear in the API."""
    segments, levels, _ = _nodes(config, prepared)
    states = [(node["key"], _cache(root, node["key"]).get("state"))
              for node in segments + [item for level in levels for item in level]]
    return {"new": [key for key, state in states if state != "completed"],
            "uncertain": [key for key, state in states if state in {"pending", "error"}]}


@contextmanager
def _call_lease(root, key):
    """Per-request OS lease prevents concurrent retries even across processes."""
    a = _ai()
    path = root / f"lease-{key}.lock"
    if path.exists() and not a._safe_path(path, root):
        raise a.AnalysisError("本机分析锁不可用")
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise a.AnalysisError("相同分段正在处理；已阻止重复调用，请稍后查看任务") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _call(root, node, config, job_id, retry_uncertain, approval, invoke):
    a = _ai()
    with _call_lease(root, node["key"]):
        with a._LOCK, a._writer_lock(root):
            current = a._configuration(root)
            if current.get("revision") != config.get("revision") or not current.get("sealed_key"):
                raise a.AnalysisError("模型连接已更改或移除，已停止后续上传；已完成分段保留")
            if a._read(root / f"job-{job_id}.json", {}).get("cancel_requested"):
                raise _Cancelled()
            cached = _cache(root, node["key"])
            if cached.get("state") == "completed":
                return cached["result"], True
            if node["key"] not in approval["new"]:
                raise a.AnalysisError("预览中的已完成缓存不可用，已停止以避免增加费用；请重新预览")
            if cached.get("state") in {"pending", "error"} and not retry_uncertain:
                raise a.AnalysisError("该分段此前失败或结果未知；请重新预览并明确确认可能重复计费后重试")
            if cached.get("state") in {"pending", "error"} and node["key"] not in approval["uncertain"]:
                raise a.AnalysisError("预览后出现结果未知的调用，已阻止重复计费；请重新预览并确认")
            # Commit before dispatch. If the process or saving fails after this,
            # a tombstone survives and no next preview silently sends it again.
            record = {"state": "pending", "job_id": job_id, "created_at": datetime.now(timezone.utc).isoformat()}
            a._write(root / f"call-{node['key']}.json", record)
        try:
            result = invoke()
            a._write(root / f"call-{node['key']}.json", {**record, "state": "completed", "sealed_result": a._seal(result)})
            return result, False
        except Exception:
            try:
                a._write(root / f"call-{node['key']}.json", {**record, "state": "error"})
            except Exception:
                pass
            raise


class _Cancelled(Exception):
    pass


def _compact_packet(node, result):
    return {"date_from": node["date_from"], "date_to": node["date_to"],
            "messages": node["messages"], "summary": result["summary"][:1200],
            "observations": [x[:300] for x in result["observations"][:6]],
            "actions": [x[:300] for x in result["actions"][:6]],
            "caveats": [x[:200] for x in result["caveats"][:3]],
            "timeline": timeline.compact(result.get("timeline"))}


def execute(root, config, key, prepared, job, retry_uncertain=False):
    a = _ai()
    job_id = job["job_id"]
    segments, levels, final_node = _nodes(config, prepared)
    total = len(segments) + sum(len(level) for level in levels)
    progress = {"stage": "segments", "completed_calls": 0, "total_calls": total,
                "completed_segments": 0, "total_segments": len(segments), "cached_calls": 0}
    results, finished, cached_nodes = {}, [], set()
    state, error, final_result = "completed", None, None

    def publish():
        with a._LOCK:
            current = a._read(root / f"job-{job_id}.json", {})
            a._write(root / f"job-{job_id}.json", {**job, "state": "running", "progress": dict(progress),
                                                    "cancel_requested": bool(current.get("cancel_requested"))})

    def gate():
        current = a._read(root / f"job-{job_id}.json", {})
        if current.get("cancel_requested"):
            raise _Cancelled()
        now = a._configuration(root)
        if now.get("revision") != config.get("revision") or not now.get("sealed_key"):
            raise a.AnalysisError("模型连接已更改或移除，已停止后续上传；已完成分段保留")

    def invoke_node(node, callback):
        gate()
        result, cached = _call(root, node, config, job_id, retry_uncertain, prepared["approved_calls"], callback)
        results[node["key"]] = result
        progress["completed_calls"] += 1
        if cached:
            cached_nodes.add(node["key"])
            progress["cached_calls"] += 1

    try:
        publish()
        for segment in segments:
            stats = {"scope_messages": len(segment["sample"]),
                     "me_messages": sum(row["sender"] == "我" for row in segment["sample"]),
                     "other_messages": sum(row["sender"] != "我" for row in segment["sample"]),
                     "sampled_voice_messages": sum(row["source"] == "voice_transcript" for row in segment["sample"])}
            request = {"scope": {**prepared["scope"], "date_from": segment["date_from"], "date_to": segment["date_to"]},
                       "sample": segment["sample"],
                       "counts": {"stats": stats, "sample_messages": segment["messages"],
                                  "eligible_messages": segment["messages"]},
                       "segment": {"index": segment["index"], "total": len(segments)},
                       "analysis_mode": prepared["scope"].get("analysis_mode", "sample")}
            invoke_node(segment, lambda req=request: a._cloud(config, key, req))
            finished.append(segment)
            progress["completed_segments"] += 1
            publish()
        for level in levels:
            progress["stage"] = "merge"
            publish()
            for node in level:
                packets = [_compact_packet(child, results[child["key"]]) for child in node["children"]]
                def merge(packets=packets, node=node):
                    value = a._cloud_merge(config, key, prepared["scope"], packets)
                    value = {**value, "timeline": timeline.combine(
                        [results[child["key"]].get("timeline") for child in node["children"]], value.get("timeline"))}
                    value["timeline"]["coverage"] = timeline.coverage(prepared["scope"],
                        [prepared["sample"][i] for i in node["message_indices"]], complete=True)
                    return value
                invoke_node(node, merge)
                publish()
        final_result = results[final_node["key"]]
    except _Cancelled:
        state, error = "cancelled", "已停止后续分段；当前已提交的调用可能计费，完成结果已保留"
    except Exception as exc:
        state = "error"
        error = str(exc) if isinstance(exc, a.AnalysisError) else "分段分析未完成；可能已计费，不会自动重试"

    report_id = None
    if final_result is not None or finished:
        expected = Counter(i for segment in segments for i in segment["message_indices"])
        actual = Counter(i for segment in finished for i in segment["message_indices"])
        analyzed = sum(actual[index] == count for index, count in expected.items())
        if final_result is None:
            final_result = {"summary": "本次只完成部分分段，尚未形成完整整合结论。请查看分段记录；未分析范围不能据此推断。",
                            "observations": [], "actions": ["核对已完成分段；再次分析前重新预览费用和覆盖范围。"],
                            "caveats": [error or "完整汇总尚未完成", "下方本机统计覆盖全部所选记录，不等于 AI 已完成全部分析。"],
                            "timeline": timeline.combine([results[node["key"]].get("timeline") for node in finished])}
        final_result = {**final_result, "timeline": final_result.get("timeline") or timeline.empty_timeline()}
        # Coverage is authored locally, never accepted from a model. Fragment
        # completion counts only fully processed original anonymous positions.
        final_result["timeline"]["coverage"] = timeline.coverage(prepared["scope"],
            [prepared["sample"][i] for i in actual],
            complete=state == "completed" and final_result["timeline"]["generated"], analyzed=analyzed)
        if state != "completed":
            final_result["timeline"]["no_contact_reason"] = timeline.unknown_reason()
        report_id = a.secrets.token_hex(24)
        report = {"id": report_id, "created_at": datetime.now(timezone.utc).isoformat(),
                  "provider": config["provider"], "model": config["model"], "scope": prepared["scope"],
                  "mode": prepared["scope"].get("analysis_mode", "sample"),
                  "stop_reason": error,
                  "sample_messages": prepared["counts"]["sample_messages"], "result": final_result,
                  "metrics": prepared.get("metrics", {}), "plan": prepared.get("plan", {}),
                  "coverage": {"eligible_messages": prepared["counts"]["eligible_messages"], "analyzed_messages": analyzed,
                               "total_segments": len(segments), "completed_segments": len(finished),
                               "complete": state == "completed"},
                  "segments": [{"index": segment["index"], "date_from": segment["date_from"], "date_to": segment["date_to"],
                                "messages": segment["messages"], "characters": segment["characters"],
                                "state": "completed" if segment in finished else "not_completed",
                                "cached": segment["key"] in cached_nodes,
                                "summary": results.get(segment["key"], {}).get("summary", "")[:240]} for segment in segments]}
        a._write(root / f"report-{report_id}.json", report)
    progress["stage"] = "completed" if state == "completed" else progress["stage"]
    output = {**job, "state": state, "progress": progress}
    if report_id:
        output["report_id"] = report_id
    if state == "completed":
        output["result"] = final_result
    if error:
        output["error"] = error
    a._write(root / f"job-{job_id}.json", output)
    return output
