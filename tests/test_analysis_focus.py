"""Relationship perspectives: synthetic scopes only, transport always intercepted."""

import copy
import importlib
import json
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai
from dashboard import analysis_segments as segments
import test_analysis_segment_safety as fixtures


ACTIVE = {
    "relation_overview": "关系全景", "interaction_rhythm": "互动节奏",
    "key_moments": "关键节点", "shared_plans": "共同计划",
    "contact_gaps": "联系空白", "communication_boundaries": "沟通与边界",
}
LEGACY = {"overview": "对话摘要与行动项", "communication": "沟通方式与互动变化",
          "business": "业务推进与待办风险", "relationship": "关系观察与边界建议"}


class AnalysisFocusTests(unittest.TestCase):
    setUp = fixtures.AnalysisSegmentSafetyTests.setUp
    tearDown = fixtures.AnalysisSegmentSafetyTests.tearDown
    write_messages = fixtures.AnalysisSegmentSafetyTests.write_messages
    configure = fixtures.AnalysisSegmentSafetyTests.configure
    preview = fixtures.AnalysisSegmentSafetyTests.preview
    start = fixtures.AnalysisSegmentSafetyTests.start
    finish = fixtures.AnalysisSegmentSafetyTests.finish

    def focus_module(self):
        return importlib.import_module("dashboard.analysis_focus")

    def prepared(self, focus):
        scope = {**self.request, "focus": focus}
        sample, counts = ai._sample({"messages": self.messages}, scope, 100)
        return {"scope": scope, "sample": sample, "counts": counts}

    def test_scope_accepts_six_new_and_four_legacy_ids_without_guidance_override(self):
        for focus in {**ACTIVE, **LEGACY}:
            with self.subTest(focus=focus):
                scope, _ = ai._scope({**self.request, "focus": focus,
                                       "focus_guidance": "synthetic-untrusted-override"})
                self.assertEqual(scope["focus"], focus)
                self.assertNotIn("focus_guidance", scope)
        for focus in ("", "invented", None, [], {}):
            with self.subTest(focus=focus), self.assertRaises(ai.AnalysisError):
                ai._scope({**self.request, "focus": focus})

    def test_broken_local_catalog_fails_closed_without_blocking_module_startup(self):
        source = Path(ai.__file__).with_name("analysis_focus.py")
        for failure in (FileNotFoundError("synthetic-untrusted-path"),
                        ValueError("synthetic-untrusted-catalog")):
            with self.subTest(failure=type(failure).__name__):
                spec = importlib.util.spec_from_file_location("synthetic_focus_catalog", source)
                module = importlib.util.module_from_spec(spec)
                with mock.patch.object(Path, "read_text", side_effect=failure):
                    spec.loader.exec_module(module)
                self.assertEqual(module.PRESETS, {})
                self.assertEqual(module.FOCUSES, frozenset())
                with self.assertRaises(ai.AnalysisError) as raised:
                    module.prompt_for("synthetic-base", "relation_overview")
                self.assertNotIn("synthetic-untrusted", str(raised.exception))
        with mock.patch.object(ai, "FOCUSES", frozenset()):
            with self.assertRaisesRegex(ai.AnalysisError, "视角.*加载"):
                ai._scope(self.request)
            self.assertTrue(ai.config_status(self.root)["configured"])
            self.assertEqual(ai.history(self.root)["reports"], [])

    def test_catalog_is_shared_and_has_exact_active_and_legacy_labels(self):
        module = self.focus_module()
        static = Path(module.__file__).parent / "static" / "analysis-focus.json"
        catalog = json.loads(static.read_text(encoding="utf-8"))
        self.assertEqual(catalog["version"], 1)
        self.assertEqual(catalog["default"], "relation_overview")
        self.assertEqual(module.DEFAULT_FOCUS, catalog["default"])
        self.assertEqual(module.PRESETS, {item["id"]: item for item in catalog["presets"]})
        self.assertEqual(ai.FOCUSES, set(ACTIVE) | set(LEGACY))
        for legacy, expected in ((False, ACTIVE), (True, LEGACY)):
            entries = [item for item in catalog["presets"] if item["legacy"] is legacy]
            self.assertEqual({item["id"]: item["label"] for item in entries}, expected)
            for item in entries:
                self.assertTrue(item["description"].strip())
                self.assertEqual(bool(item["guidance"]), not legacy)

    def test_each_new_perspective_reaches_both_system_prompts_not_chat_instructions(self):
        module = self.focus_module()
        with mock.patch.object(urllib.request, "build_opener") as network:
            for focus in ACTIVE:
                prepared = self.prepared(focus)
                prepared["scope"]["focus_guidance"] = "synthetic-untrusted-override"
                with self.subTest(focus=focus), mock.patch.object(ai, "_request_result", return_value={}) as transport:
                    ai._cloud({}, "synthetic-placeholder", prepared)
                    ai._cloud_merge({}, "synthetic-placeholder", prepared["scope"], [])
                    self.assertEqual(transport.call_count, 2)
                    for call, base in zip(transport.call_args_list, (ai.SYSTEM_PROMPT, ai.MERGE_PROMPT)):
                        content, prompt = call.args[2:]
                        self.assertEqual(content["focus"], focus)
                        self.assertTrue(prompt.startswith(base))
                        self.assertIn(module.PRESETS[focus]["guidance"], prompt)
                        self.assertNotIn("synthetic-untrusted-override", prompt + json.dumps(content))
                        self.assertEqual(prompt, module.prompt_for(base, focus))
            network.assert_not_called()

    def test_legacy_provider_prompts_and_revision_remain_byte_identical(self):
        module = self.focus_module()
        previous = segments._digest([ai.SYSTEM_PROMPT, ai.MERGE_PROMPT])
        self.assertEqual(segments.prompt_revision(), previous)
        for focus in LEGACY:
            self.assertEqual(module.prompt_for(ai.SYSTEM_PROMPT, focus), ai.SYSTEM_PROMPT)
            self.assertEqual(module.prompt_for(ai.MERGE_PROMPT, focus), ai.MERGE_PROMPT)
            self.assertEqual(segments.prompt_revision(focus), previous)

    def test_revision_tracks_only_selected_guidance_not_presentation_or_other_views(self):
        module = self.focus_module()
        focus = "shared_plans"
        before = segments.prompt_revision(focus)
        revised = {**module.PRESETS[focus], "label": "合成标题", "description": "合成说明"}
        with mock.patch.dict(module.PRESETS, {focus: revised}):
            self.assertEqual(segments.prompt_revision(focus), before)
        revised["guidance"] += "\n合成契约修订。"
        unrelated = segments.prompt_revision("interaction_rhythm")
        with mock.patch.dict(module.PRESETS, {focus: revised}):
            self.assertNotEqual(segments.prompt_revision(focus), before)
            self.assertEqual(segments.prompt_revision("interaction_rhythm"), unrelated)
            self.assertEqual(segments.prompt_revision("business"), segments.prompt_revision())

    def test_node_keys_isolate_views_and_preserve_legacy_algorithm(self):
        config = {"provider": "synthetic", "model": "synthetic", "revision": "fixture"}
        for focus in LEGACY:
            prepared = self.prepared(focus)
            actual, _, _ = segments._nodes(config, prepared)
            identity = {"version": segments.PIPELINE_VERSION, **config,
                        "scope": prepared["scope"], "counts": prepared["counts"],
                        "prompts": segments._digest([ai.SYSTEM_PROMPT, ai.MERGE_PROMPT])}
            item = segments._partition(prepared["sample"])[0]
            self.assertEqual(actual[0]["key"], segments._digest([
                identity, "segment", item["index"], 1, item["message_indices"], item["sample"]]))
        keys = [segments._nodes(config, self.prepared(focus))[0][0]["key"] for focus in ACTIVE]
        self.assertEqual(len(set(keys)), len(ACTIVE))

    def test_guidance_revision_changes_cache_identity(self):
        module = self.focus_module()
        prepared = self.prepared("key_moments")
        before = segments._nodes({}, prepared)[0][0]["key"]
        revised = {**module.PRESETS["key_moments"], "guidance": "合成修订"}
        with mock.patch.dict(module.PRESETS, {"key_moments": revised}):
            self.assertNotEqual(segments._nodes({}, prepared)[0][0]["key"], before)

    def test_preview_is_offline_and_stale_guidance_rejected_before_any_call(self):
        module = self.focus_module()
        with mock.patch.object(urllib.request, "build_opener") as network:
            preview = self.preview(focus="contact_gaps")
            self.assertEqual(preview["scope"]["focus"], "contact_gaps")
            revised = {**module.PRESETS["contact_gaps"], "guidance": "合成新版本"}
            with mock.patch.dict(module.PRESETS, {"contact_gaps": revised}), self.assertRaisesRegex(ai.AnalysisError, "提示"):
                self.start(preview)
            network.assert_not_called()
        self.assertFalse(ai._ACTIVE_JOBS)

    def test_new_view_preview_requires_literal_consent_and_executes_synthetic_pipeline(self):
        with mock.patch.object(urllib.request, "build_opener") as network:
            preview = self.preview(focus="relation_overview")
            with self.assertRaises(ai.AnalysisError):
                self.start(preview, consent=False)
            result = {"summary": "合成摘要", "observations": [], "actions": [], "caveats": []}
            with mock.patch.object(ai, "_request_result", return_value=result) as transport:
                job = self.finish(self.start(preview))
                self.assertEqual(job["state"], "completed")
                self.assertEqual(transport.call_count, 1)
                self.assertIn(self.focus_module().PRESETS["relation_overview"]["guidance"],
                              transport.call_args.args[3])
            network.assert_not_called()

    def test_estimate_includes_guidance_for_each_leaf_and_merge_call(self):
        module = self.focus_module()
        prepared = self.prepared("interaction_rhythm")
        with mock.patch.object(segments, "CHUNK_MESSAGES", 2):
            actual = segments.plan(self.root, {}, prepared)
            original = copy.deepcopy(prepared)
            original["scope"]["focus"] = "communication"
            previous = segments.plan(self.root, {}, original)
        added = len(module.prompt_for(ai.SYSTEM_PROMPT, "interaction_rhythm")) - len(ai.SYSTEM_PROMPT)
        self.assertGreater(added, 0)
        self.assertEqual(actual["estimated_input_tokens"] - previous["estimated_input_tokens"],
                         added * 3 * actual["total_calls"])


if __name__ == "__main__":
    unittest.main()
