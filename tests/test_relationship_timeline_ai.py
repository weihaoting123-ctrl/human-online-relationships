"""Synthetic-only timeline semantics; provider transport is always mocked."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai
from dashboard import analysis_segments as segments
from dashboard import timeline_schema as schema


BASE = {"summary": "合成关系观察", "observations": [], "actions": [], "caveats": []}
SAMPLE = [
    {"date": "2026-01-02", "sample_index": 1, "sender": "我", "source": "text", "text": "合成提议准备下个月的共同活动"},
    {"date": "2026-02-03", "sample_index": 2, "sender": "对方／其他参与者", "source": "text", "text": "合成说明正在核对双方的时间安排"},
    {"date": "2026-03-04", "sample_index": 3, "sender": "我", "source": "text", "text": "合成说明活动已经举行并分享感受"},
]
SCOPE = {"bundle_id": "synthetic-only", "focus": "relationship", "date_from": "2026-01-01",
         "date_to": "2026-12-31", "analysis_mode": "sample"}
CONTEXT = {**SCOPE, "sample": SAMPLE, "segment": {"index": 1, "total": 1}}


def event(number=1, **patch):
    row = SAMPLE[number - 1]
    return {"id": f"e{number}", "date_from": row["date"], "date_to": row["date"],
            "kind": "plan", "title": "合成活动计划", "summary": "讨论了共同活动，尚未确认落实。",
            "status": "planned", "evidence_level": "reported", "related_event_id": None,
            "evidence": [{"date": row["date"], "sample_index": number}], **patch}


def timeline(events=None, reason=None):
    return {"version": 1, "generated": True, "events": events if events is not None else [event()],
            "no_contact_reason": reason or {"kind": "unknown", "summary": "原因不明", "evidence": [],
                                            "limitations": ["不能从无消息推断原因。"]}}


class TimelineSchemaTests(unittest.TestCase):
    def validate(self, value, context=None):
        # Explicit assertion gives RED on the missing contract before using it.
        self.assertIn("timeline", ai._safe_result(BASE))
        return ai._safe_result({**BASE, "timeline": value}, context=context or CONTEXT)["timeline"]

    def test_legacy_four_fields_remain_valid_and_timeline_is_not_generated(self):
        result = ai._safe_result(BASE)
        self.assertIn("timeline", result)
        self.assertFalse(result["timeline"]["generated"])
        self.assertEqual(result["timeline"]["events"], [])
        self.assertEqual(result["timeline"]["no_contact_reason"]["kind"], "unknown")
        self.assertEqual(result["summary"], BASE["summary"])

    def test_plan_progress_outcome_and_anonymous_links_survive(self):
        value = timeline([event(), event(2, kind="update", status="in_progress", related_event_id="e1"),
                          event(3, kind="outcome", status="realized", related_event_id="e2")])
        result = self.validate(value)
        self.assertEqual([e["status"] for e in result["events"]], ["planned", "in_progress", "realized"])
        self.assertEqual([e["id"] for e in result["events"]], ["s1-e1", "s1-e2", "s1-e3"])
        self.assertEqual(result["events"][2]["related_event_id"], "s1-e2")
        self.assertEqual(result["coverage"]["date_from"], "2026-01-02")
        self.assertEqual(result["coverage"]["date_to"], "2026-03-04")
        self.assertEqual(result["coverage"]["scope"], "selected_sample")

    def test_bad_dates_indices_types_limits_and_links_fail_closed(self):
        for patch in ({"date_from": "2025-01-01"}, {"date_to": "2026-01-03"}, {"date_from": "2026-02-30"},
                      {"kind": []}, {"status": "done"}, {"evidence_level": True}, {"title": "字" * 41},
                      {"summary": "字" * 141}, {"related_event_id": "missing"}, {"related_event_id": "e1"},
                      {"evidence": [{"date": SAMPLE[0]["date"], "sample_index": True}]},
                      {"evidence": [{"date": SAMPLE[0]["date"], "sample_index": 2}]},
                      {"evidence": [event()["evidence"][0]] * 4}, {"quote": "不可返回原话"}):
            with self.subTest(patch=patch), self.assertRaises(ai.AnalysisError):
                self.validate(timeline([event(**patch)]))
        for value in ([], {**timeline(), "events": [event()] * 7},
                      {**timeline(), "generated": "yes"}, {**timeline(), "version": True},
                      timeline([event(), event()]),
                      timeline([event(related_event_id="e2"), event(2, related_event_id="e1")])):
            with self.subTest(value=value), self.assertRaises(ai.AnalysisError):
                self.validate(value)

    def test_plan_or_inference_cannot_claim_realization(self):
        for patch in ({"status": "realized"}, {"kind": "outcome", "status": "realized", "evidence_level": "inferred"},
                      {"kind": "outcome", "status": "cancelled", "evidence": []}):
            with self.subTest(patch=patch), self.assertRaises(ai.AnalysisError):
                self.validate(timeline([event(**patch)]))
        self.assertEqual(self.validate(timeline())["events"][0]["status"], "planned")

    def test_unknown_reason_cannot_smuggle_a_cause_and_explicit_requires_evidence(self):
        reason = {"kind": "unknown", "summary": "因为不爱所以没有联系", "evidence": [], "limitations": []}
        result = self.validate(timeline(reason=reason))["no_contact_reason"]
        self.assertNotIn("不爱", result["summary"])
        self.assertTrue(result["limitations"])
        for kind in ("explicit", "inferred"):
            with self.subTest(kind=kind), self.assertRaises(ai.AnalysisError):
                self.validate(timeline(reason={**reason, "kind": kind}))
        explicit = {**reason, "kind": "explicit", "summary": "一方自述暂时忙于工作。",
                    "evidence": event()["evidence"]}
        result = self.validate(timeline(reason=explicit))["no_contact_reason"]
        self.assertEqual(result["kind"], "explicit")
        self.assertIn("自述", "".join(result["limitations"]))

    def test_text_is_sanitized_without_quotes_accounts_paths_or_exact_sample(self):
        text = '「不可回显合成引文」 wxid_synthetic C:\\synthetic\\secret.txt ' + SAMPLE[0]["text"]
        result = self.validate(timeline([event(summary=text)]))
        serialized = json.dumps(result, ensure_ascii=False)
        for private in ("不可回显合成引文", "wxid_synthetic", "secret.txt", SAMPLE[0]["text"]):
            self.assertNotIn(private, serialized)

    def test_partition_adds_global_anonymous_ordinals_even_for_split_messages(self):
        with mock.patch.object(segments, "CHUNK_CHARS", 8):
            chunks = segments._partition([{k: v for k, v in row.items() if k != "sample_index"} for row in SAMPLE])
        self.assertTrue(all("sample_index" in row for c in chunks for row in c["sample"]))
        self.assertEqual({row["sample_index"] for c in chunks for row in c["sample"]}, {1, 2, 3})

    def test_compact_merge_packet_keeps_events_and_links(self):
        value = self.validate(timeline([event(), event(2, kind="update", status="in_progress", related_event_id="e1")]))
        packet = segments._compact_packet({"date_from": SAMPLE[0]["date"], "date_to": SAMPLE[1]["date"], "messages": 2},
                                          {**BASE, "timeline": value})
        self.assertIn("timeline", packet)
        self.assertEqual(packet["timeline"]["events"], value["events"])
        self.assertEqual(packet["timeline"]["events"][1]["related_event_id"], "s1-e1")

    def test_merge_adds_cross_segment_link_but_cannot_upgrade_or_invent_an_outcome(self):
        first = self.validate(timeline())
        second_context = {**CONTEXT, "sample": SAMPLE[1:], "segment": {"index": 2}}
        second = self.validate(timeline([event(2, id="e1", kind="update", status="in_progress")]), second_context)
        packets = [{"messages": 1, "timeline": t} for t in (first, second)]
        update = {**second["events"][0], "related_event_id": first["events"][0]["id"]}
        context = {**SCOPE, "segment_summaries": packets}
        merged = self.validate(timeline([update]), context)
        result = schema.combine([first, second], merged)
        self.assertEqual(result["events"][1]["related_event_id"], "s1-e1")
        self.assertEqual(result["events"][0]["status"], "planned")
        for patch in ({"status": "realized", "kind": "outcome"}, {"id": "s3-e1"}, {"summary": "已经完成活动"}):
            with self.subTest(patch=patch), self.assertRaises(ai.AnalysisError):
                self.validate(timeline([{**update, **patch}]), context)

    def test_compact_long_graph_includes_last_event_and_marks_representative_coverage(self):
        values = [self.validate(timeline(), {**CONTEXT, "segment": {"index": n}}) for n in range(1, 9)]
        full = schema.combine(values)
        compact = schema.compact(full)
        self.assertEqual(compact["events_total"], 8)
        self.assertTrue(compact["events_truncated"])
        self.assertLessEqual(len(compact["events"]), 6)
        self.assertEqual(compact["events"][-1]["id"], full["events"][-1]["id"])

    def test_model_cannot_supply_a_coverage_object_claiming_full_archive(self):
        result = self.validate({**timeline(), "coverage": {"scope": "full_archive", "complete": True,
                                                                         "date_to": "2099-01-01"}})
        self.assertEqual(result["coverage"]["scope"], "selected_sample")
        self.assertEqual(result["coverage"]["date_to"], SAMPLE[-1]["date"])

    def test_merge_cannot_relabel_a_reason_or_redirect_an_existing_link(self):
        reason = {"kind": "explicit", "summary": "一方自述短期工作安排紧凑。", "evidence": event()["evidence"], "limitations": []}
        value = self.validate(timeline([event(), event(2, related_event_id="e1"), event(3, related_event_id="e2")], reason))
        context = {**SCOPE, "segment_summaries": [{"messages": 3, "timeline": value}]}
        revised = {**value["no_contact_reason"], "summary": "因为没有感情所以停止联系。"}
        with self.subTest("reason"), self.assertRaises(ai.AnalysisError):
            self.validate(timeline([], revised), context)
        revised_event = {**value["events"][2], "related_event_id": "s1-e1"}
        with self.subTest("link"), self.assertRaises(ai.AnalysisError):
            self.validate(timeline([revised_event]), context)

    def test_cloud_payload_keeps_selected_anonymous_data_and_excludes_archive_recency(self):
        prepared = {"scope": {**SCOPE, "analysis_mode": "full"}, "sample": SAMPLE,
                    "analysis_mode": "full", "counts": {"sample_messages": 3, "eligible_messages": 30,
                    "stats": {"scope_messages": 30}}, "metrics": {"timeline": {"last_contact": "2099-01-01",
                    "as_of": "2099-01-02", "archive_date_from": "1900-01-01", "private": "PRIVATE-ARCHIVE"}}}
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps({"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps({**BASE, "timeline": timeline()})}}]}).encode()
        with mock.patch.object(ai.urllib.request, "build_opener", return_value=opener):
            result = ai._cloud({"provider": "openai", "model": "synthetic"}, "synthetic-key", prepared)
        self.assertEqual(result["timeline"]["coverage"]["analysis_mode"], "full")
        request = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(request["max_completion_tokens"], 2400)
        self.assertEqual(opener.open.call_count, 1)
        content = request["messages"][1]["content"]
        for secret in ("last_contact", "as_of", "archive_date_from", "1900-01-01", "2099-01-01", "PRIVATE-ARCHIVE", SCOPE["bundle_id"]):
            self.assertNotIn(secret, content)
        self.assertIn("sample_index", content)

    def test_same_day_events_sort_by_anonymous_position_not_lexicographic_segment_id(self):
        values = []
        for number in (10, 2):
            row = {**SAMPLE[0], "sample_index": number}
            item = event(evidence=[{"date": row["date"], "sample_index": number}])
            values.append(self.validate(timeline([item]), {**CONTEXT, "sample": [row], "segment": {"index": number}}))
        result = schema.combine(values)
        self.assertEqual([e["id"] for e in result["events"]], ["s2-e1", "s10-e1"])

    def test_same_day_link_cannot_point_to_a_later_anonymous_position(self):
        sample = [dict(row, date=SAMPLE[0]["date"]) for row in SAMPLE[:2]]
        first = event(related_event_id="e2")
        second = event(id="e2", evidence=[{"date": SAMPLE[0]["date"], "sample_index": 2}])
        with self.assertRaises(ai.AnalysisError):
            self.validate(timeline([first, second]), {**CONTEXT, "sample": sample})

    def test_merge_retains_all_1200_events_within_existing_cache_read_budget(self):
        leaf = self.validate(timeline([event(id=f"e{i}", title="题" * 40, summary="述" * 140) for i in range(1, 7)]))
        values = []
        for number in range(1, 201):
            value = copy.deepcopy(leaf)
            for e in value["events"]:
                e["id"] = f"s{number}-" + e["id"].split("-")[-1]
            values.append(value)
        result = schema.combine(values)
        self.assertEqual(result["events_total"], 1200)
        self.assertFalse(result["events_truncated"])
        # DPAPI adds a small header; the existing 2 MiB cache JSON read bound
        # comfortably holds the base64-encoded result at the semantic caps.
        import base64
        sealed_size = len(base64.b64encode(json.dumps(result, ensure_ascii=False).encode()))
        self.assertLess(sealed_size, 2 * 1024 * 1024 - 100_000)
        self.assertIn("代表事件", "".join(schema.coverage(SCOPE, SAMPLE, complete=True)["limitations"]))


class TimelineWorkflowTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="timeline-ai-", dir=root)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.seal = mock.patch.object(ai, "_dpapi", side_effect=lambda data, **kwargs: data)
        self.seal.start()
        self.addCleanup(self.seal.stop)
        self.config = {"provider": "openai", "model": "synthetic-model", "revision": "synthetic", "sealed_key": ai._seal("synthetic-key")}
        ai._write(self.root / "config.json", self.config)
        self.prepared = {"scope": SCOPE, "sample": SAMPLE,
                         "counts": {"sample_messages": 3, "eligible_messages": 30, "sample_chars": 50}}
        self.job = {"job_id": "a" * 48}

    def execute(self, cloud, merge):
        with mock.patch.object(segments, "CHUNK_MESSAGES", 1), mock.patch.object(segments, "MERGE_FANIN", 2):
            self.prepared["approved_calls"] = segments.approved_calls(self.root, self.config, self.prepared)
            with mock.patch.object(ai, "_cloud", side_effect=cloud), mock.patch.object(ai, "_cloud_merge", side_effect=merge):
                return segments.execute(self.root, self.config, "synthetic-key", self.prepared, self.job)

    def cloud(self, _config, _key, req):
        self.assertIn("timeline", ai._safe_result(BASE))
        row = req["sample"][0]
        value = timeline([event(row["sample_index"], id="e1")])
        return ai._safe_result({**BASE, "timeline": value}, context={**req["scope"], "sample": req["sample"], "segment": req["segment"]})

    def test_hierarchical_merge_does_not_drop_events_when_model_returns_legacy_summary(self):
        result = self.execute(self.cloud, lambda *_args: dict(BASE))
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(result["result"]["timeline"]["events"]), 3)
        self.assertEqual(result["result"]["timeline"]["coverage"]["analyzed_messages"], 3)
        self.assertEqual(result["result"]["timeline"]["coverage"]["date_to"], SAMPLE[-1]["date"])

    def test_partial_results_keep_completed_events_but_never_claim_a_complete_reason(self):
        def cloud(config, key, req):
            if req["segment"]["index"] == 2:
                raise ai.AnalysisError("合成中断")
            return self.cloud(config, key, req)
        result = self.execute(cloud, lambda *_args: dict(BASE))
        self.assertIn("report_id", result)
        report = ai._read(self.root / ("report-" + result["report_id"] + ".json"))
        value = report["result"]["timeline"]
        self.assertEqual(len(value["events"]), 1)
        self.assertFalse(value["coverage"]["complete"])
        self.assertEqual(value["coverage"]["analyzed_messages"], 1)
        self.assertEqual(value["coverage"]["date_to"], SAMPLE[0]["date"])
        self.assertEqual(value["no_contact_reason"]["kind"], "unknown")
        self.assertIn("未完成", "".join(value["coverage"]["limitations"]))

    def test_prompt_change_rejects_preview_before_consumption_or_worker_launch(self):
        contacts = self.root / "contacts"
        bundle = contacts / SCOPE["bundle_id"]
        bundle.mkdir(parents=True)
        (bundle / "messages.json").write_text(json.dumps({"messages": [
            {"timestamp": "2026-01-02 12:00:00", "sender": "me", "content": "全合成消息", "type": "text"}]}), encoding="utf-8")
        ai._write(ai._root(self.root) / "config.json", self.config)
        preview = ai.preview(self.root, contacts, {**SCOPE, "max_messages": 100})
        path = ai._root(self.root) / ("preview-" + preview["preview_id"] + ".json")
        with mock.patch.object(ai, "SYSTEM_PROMPT", ai.SYSTEM_PROMPT + "\n新语义版本"), mock.patch.object(ai.threading, "Thread") as thread:
            with self.assertRaises(ai.AnalysisError):
                ai.run(self.root, contacts, {"preview_id": preview["preview_id"], "consent": True})
            thread.assert_not_called()
        self.assertTrue(path.exists())
        # Even if every current call is already cached, an older preview must
        # not be silently consumed under a different prompt version.
        prepared = ai._unseal(ai._read(path)["sealed"])
        prepared.pop("prompt_revision", None)
        ai._write(path, {"expires": prepared["expires"], "sealed": ai._seal(prepared)})
        with mock.patch.object(ai.threading, "Thread") as thread:
            with self.assertRaises(ai.AnalysisError):
                ai.run(self.root, contacts, {"preview_id": preview["preview_id"], "consent": True})
            thread.assert_not_called()
        self.assertTrue(path.exists())

    def test_old_report_and_completed_job_get_empty_timeline_without_rewriting(self):
        root = ai._root(self.root)
        identity = "b" * 48
        report = {"id": identity, "scope": SCOPE, "result": BASE, "coverage": {"complete": True}}
        job = {"job_id": identity, "state": "completed", "result": BASE}
        report_path, job_path = root / f"report-{identity}.json", root / f"job-{identity}.json"
        ai._write(report_path, report)
        ai._write(job_path, job)
        original = (report_path.read_bytes(), job_path.read_bytes())
        for output in (ai.report(self.root, identity), ai.job_status(self.root, identity)):
            self.assertIn("timeline", output["result"])
            self.assertFalse(output["result"]["timeline"]["generated"])
            self.assertFalse(output["result"]["timeline"]["coverage"]["complete"])
        self.assertEqual((report_path.read_bytes(), job_path.read_bytes()), original)
