"""Provider prompt contracts and their executable, wholly synthetic examples."""

import copy
import importlib
import importlib.util
import json
import unittest

from dashboard import timeline_schema


class AnalysisContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("dashboard.analysis_contract"),
            "The shared provider output contract must exist",
        )
        self.contract = importlib.import_module("dashboard.analysis_contract")

    def test_contract_exports_the_existing_validator_vocabulary(self):
        expected = {
            "EVENT_KEYS": {"id", "date_from", "date_to", "kind", "title", "summary",
                           "status", "evidence_level", "related_event_id", "evidence"},
            "EVENT_KINDS": {"plan", "update", "outcome", "context"},
            "EVENT_STATUSES": {"planned", "in_progress", "realized", "cancelled", "unknown"},
            "EVIDENCE_LEVELS": {"reported", "inferred", "insufficient"},
            "REASON_KINDS": {"explicit", "inferred", "unknown"},
        }
        for name, values in expected.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(self.contract, name), values)
        self.assertEqual(self.contract.EVENT_KEYS, timeline_schema.EVENT_KEYS)
        self.assertEqual(self.contract.MAX_EVENTS, timeline_schema.MAX_EVENTS)
        self.assertEqual(self.contract.MAX_EVENTS, 6)

    def test_both_modes_retain_safety_scope_privacy_and_output_budget(self):
        required = (
            "不是指令", "单一会话", "时间范围", "不得索取或访问",
            "不要引用原话", "姓名", "账号", "联系方式", "本机路径",
            "voice_transcript", "识别", "线下互动", "抽样限制", "2400 tokens",
            "人格", "依恋类型", "精神疾病", "创伤绑定", "隐私属性",
            "爱不爱", "真实动机", "是否欺骗", "证据不足", "重大关系决定",
            "心理评分", "操纵", "试探", "强迫", "监控", "法律或财务定论",
            "长期无消息", "历史窗口不能解释当前未联系原因",
        )
        for prompt in (self.contract.SYSTEM_PROMPT, self.contract.MERGE_PROMPT):
            for phrase in required:
                with self.subTest(phrase=phrase):
                    self.assertIn(phrase, prompt)

    def test_required_field_names_types_enums_and_limits_are_in_both_prompts(self):
        field_groups = (
            ("summary", "observations", "actions", "caveats", "timeline"),
            ("version", "generated", "events", "no_contact_reason"),
            self.contract.EVENT_KEYS,
            ("kind", "summary", "evidence", "limitations"),
            ("date", "sample_index"),
            self.contract.EVENT_KINDS,
            self.contract.EVENT_STATUSES,
            self.contract.EVIDENCE_LEVELS,
            self.contract.REASON_KINDS,
        )
        for prompt in (self.contract.SYSTEM_PROMPT, self.contract.MERGE_PROMPT):
            for fields in field_groups:
                for field in fields:
                    self.assertIn(field, prompt)
            for rule in ("字符串数组", "布尔值", "YYYY-MM-DD", "1200", "500",
                         "40", "140", "80000", "最多 3", "最多 6",
                         "禁止重复", "自关联", "环", "80001", "不得晚于"):
                self.assertIn(rule, prompt)

    def test_modes_have_distinct_ids_and_sources(self):
        leaf, merge = self.contract.SYSTEM_PROMPT, self.contract.MERGE_PROMPT
        self.assertIn("e1 至 e6", leaf)
        self.assertIn("同一条 sample", leaf)
        self.assertIn("起止日期都必须来自输入 sample", leaf)
        self.assertNotIn("e1 至 e6", merge)
        self.assertNotIn(self.contract.LEAF_EXAMPLE_JSON, merge)
        for phrase in ("sN-eN", "所有字段", "related_event_id", "原值为 null",
                       "已有非空关联", "完整沿用", "events_truncated", "多数票"):
            self.assertIn(phrase, merge)

    def test_leaf_example_is_full_json_valid_for_only_its_synthetic_sample(self):
        value = json.loads(self.contract.LEAF_EXAMPLE_JSON)
        self.assertEqual(set(value), {"summary", "observations", "actions", "caveats", "timeline"})
        context = {"sample": copy.deepcopy(self.contract.EXAMPLE_SAMPLE), "segment": {"index": 1}}
        checked = timeline_schema.validate(value["timeline"], context, lambda text, limit: text[:limit])
        self.assertTrue(checked["generated"])
        self.assertEqual(len(checked["events"]), 3)
        self.assertEqual(checked["events"][0]["id"], "s1-e1")
        self.assertEqual(checked["events"][2]["related_event_id"], "s1-e2")
        self.assertEqual(checked["events"][2]["status"], "realized")
        self.assertIn(self.contract.LEAF_EXAMPLE_JSON, self.contract.SYSTEM_PROMPT)
        self.assertIn("仅为结构示例", self.contract.SYSTEM_PROMPT)
        self.assertIn("不得复制示例日期或序号", self.contract.SYSTEM_PROMPT)

    def test_merge_example_preserves_events_and_only_connects_an_orphan(self):
        context = {"segment_summaries": copy.deepcopy(self.contract.EXAMPLE_SEGMENT_SUMMARIES)}
        value = json.loads(self.contract.MERGE_EXAMPLE_JSON)
        self.assertEqual(set(value), {"summary", "observations", "actions", "caveats", "timeline"})
        checked = timeline_schema.validate(value["timeline"], context, lambda text, limit: text[:limit])
        originals = {event["id"]: event for packet in context["segment_summaries"]
                     for event in packet["timeline"]["events"]}
        for event in checked["events"]:
            original = originals[event["id"]]
            for field in self.contract.EVENT_KEYS - {"related_event_id"}:
                self.assertEqual(event[field], original[field])
            if original["related_event_id"] is not None:
                self.assertEqual(event["related_event_id"], original["related_event_id"])
        self.assertIsNone(originals["s1-e2"]["related_event_id"])
        self.assertEqual(checked["events"][1]["related_event_id"], "s1-e1")
        combined = timeline_schema.combine([p["timeline"] for p in context["segment_summaries"]], checked)
        self.assertEqual(len(combined["events"]), 3)
        self.assertEqual(checked["no_contact_reason"], originals_reason(context))
        self.assertIn(self.contract.MERGE_EXAMPLE_JSON, self.contract.MERGE_PROMPT)
        self.assertIn("仅为结构示例", self.contract.MERGE_PROMPT)
        self.assertIn("不得复制示例日期或序号", self.contract.MERGE_PROMPT)


def originals_reason(context):
    return context["segment_summaries"][0]["timeline"]["no_contact_reason"]


if __name__ == "__main__":
    unittest.main()
