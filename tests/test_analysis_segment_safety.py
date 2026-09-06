"""Synthetic-only segmented cloud safety tests; never read real chat stores."""

import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai
from dashboard import analysis_segments as segments


RESULT = {"summary": "合成沟通观察", "observations": ["合成事项"],
          "actions": ["人工核实合成结果"], "caveats": ["仅用于测试"]}
TERMINAL_STATES = {"completed", "partial", "error", "cancelled", "canceled"}


class AnalysisSegmentSafetyTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="analysis-safety-", dir=temp_root)
        self.root = Path(self.temp.name)
        self.contacts = self.root / "contacts"
        self.bundle = self.contacts / "synthetic-segment-contact"
        self.bundle.mkdir(parents=True)
        self.path = self.bundle / "messages.json"
        self.request = {"bundle_id": self.bundle.name, "date_from": "2026-08-01",
                        "date_to": "2026-08-31", "focus": "communication",
                        "max_messages": 100, "analysis_mode": "full",
                        "include_voice_transcripts": False}
        self.secret = "synthetic-safety-key-never-real"
        self.release = threading.Event()
        self.stack = ExitStack()
        # Exercise serialized storage boundaries without requiring a real key
        # or tying the synthetic fixture to a Windows logon profile.
        self.stack.enter_context(mock.patch.object(ai, "_dpapi", side_effect=lambda data, **_kw: data))
        self.write_messages(7)
        self.configure()

    def tearDown(self):
        self.release.set()
        deadline = time.monotonic() + 5
        while ai._ACTIVE_JOBS and time.monotonic() < deadline:
            time.sleep(0.01)
        self.stack.close()
        self.temp.cleanup()

    def write_messages(self, count, content=None):
        stamp = datetime(2026, 8, 10, 12).timestamp()
        self.messages = [{"timestamp": stamp + index, "sender": "me" if index % 2 else "them",
                          "type": "text", "content": content or "合成消息" + chr(0x4E00 + index)}
                         for index in range(count)]
        self.path.write_text(json.dumps({"messages": self.messages}, ensure_ascii=False), encoding="utf-8")

    def configure(self, **overrides):
        return ai.save_config(self.root, {"provider": "openai", "model": "synthetic-safety-model",
                                         "api_key": self.secret, **overrides})

    def preview(self, **overrides):
        return ai.preview(self.root, self.contacts, {**self.request, **overrides})

    def start(self, preview, **overrides):
        return ai.run(self.root, self.contacts, {"preview_id": preview["preview_id"],
                                                "consent": True, **overrides})

    def finish(self, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = ai.job_status(self.root, job["job_id"])
            if status["state"] in TERMINAL_STATES:
                return status
            time.sleep(0.01)
        self.fail("Synthetic segmented worker did not finish")

    def small_chunks(self, size=2, fanin=2):
        self.stack.enter_context(mock.patch.object(segments, "CHUNK_MESSAGES", size))
        self.stack.enter_context(mock.patch.object(segments, "MERGE_FANIN", fanin))

    def mock_cloud(self, side_effect=None, merge_side_effect=None):
        cloud = self.stack.enter_context(mock.patch.object(ai, "_cloud", return_value=RESULT,
                                                          side_effect=side_effect))
        merge = self.stack.enter_context(mock.patch.object(ai, "_cloud_merge", return_value=RESULT,
                                                          side_effect=merge_side_effect))
        return cloud, merge

    def test_full_preview_selects_every_eligible_message_without_network(self):
        self.write_messages(601)
        with mock.patch.object(urllib.request, "build_opener") as network:
            preview = self.preview()
        network.assert_not_called()
        self.assertEqual(preview["sample_messages"], 601)
        self.assertEqual(preview["eligible_messages"], 601)
        self.assertEqual(preview["truncated_messages"], 0)
        self.assertEqual(preview["plan"]["mode"], "full")
        self.assertGreaterEqual(preview["plan"]["segments"], 2)
        self.assertEqual(preview["plan"]["cached_calls"], 0)
        self.assertEqual(preview["plan"]["new_calls"], preview["plan"]["total_calls"])
        self.assertNotIn("合成消息", json.dumps(preview, ensure_ascii=False))

    def test_full_16610_messages_finish_all_42_segments_and_replay_all_caches(self):
        """Exercise production chunk sizes beyond the former partial UI count."""
        self.write_messages(16_610)
        network_guards = [self.stack.enter_context(mock.patch.object(owner, name,
            side_effect=AssertionError("synthetic test must never access the network")))
            for owner, name in ((urllib.request, "build_opener"),
                                (urllib.request, "urlopen"),
                                (socket, "create_connection"),
                                (socket.socket, "connect"))]
        request_guard = self.stack.enter_context(mock.patch.object(ai, "_request_result",
            side_effect=AssertionError("only synthetic cloud responses are allowed")))
        visited_segments, visited_text = [], []

        def cloud_result(_config, _key, prepared):
            index = prepared["segment"]["index"]
            self.assertEqual(prepared["segment"]["total"], 42)
            self.assertLessEqual(len(prepared["sample"]), 400)
            visited_segments.append(index)
            visited_text.extend(row["text"] for row in prepared["sample"])
            return {**RESULT, "summary": f"LEAF_{index:02d}"}

        def merge_result(_config, _key, _scope, packets):
            self.assertLessEqual(len(packets), 6)
            labels = sorted(set(re.findall(r"LEAF_\d{2}", json.dumps(packets))))
            return {**RESULT, "summary": " ".join(labels)}

        def finish_large_job(job):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = ai.job_status(self.root, job["job_id"])
                if status["state"] in TERMINAL_STATES:
                    return status
                time.sleep(0.01)
            self.fail("Synthetic 16,610-message worker did not finish")

        cloud, merge = self.mock_cloud(side_effect=cloud_result, merge_side_effect=merge_result)
        first_preview = self.preview()
        self.assertEqual(first_preview["eligible_messages"], 16_610)
        self.assertEqual(first_preview["sample_messages"], 16_610)
        self.assertEqual(first_preview["truncated_messages"], 0)
        self.assertEqual(first_preview["plan"]["segments"], 42)
        # 42 leaves -> 7 summaries -> 2 summaries -> the final summary.
        self.assertEqual(first_preview["plan"]["merge_calls"], 9)
        self.assertEqual(first_preview["plan"]["total_calls"], 51)
        self.assertEqual(first_preview["plan"]["new_calls"], 51)
        first = finish_large_job(self.start(first_preview))
        self.assertEqual(first["state"], "completed", first)
        self.assertEqual(cloud.call_count, 42)
        self.assertEqual(merge.call_count, 9)
        self.assertEqual(visited_segments, list(range(1, 43)))
        self.assertEqual(visited_text, [row["content"] for row in self.messages])
        expected_labels = [f"LEAF_{index:02d}" for index in range(1, 43)]
        self.assertEqual(re.findall(r"LEAF_\d{2}", first["result"]["summary"]), expected_labels)
        first_report = ai.report(self.root, first["report_id"])
        self.assertEqual(first_report["coverage"], {
            "eligible_messages": 16_610, "analyzed_messages": 16_610,
            "total_segments": 42, "completed_segments": 42, "complete": True})

        cloud.reset_mock()
        merge.reset_mock()
        replay_preview = self.preview()
        self.assertEqual(replay_preview["plan"]["cached_calls"], 51)
        self.assertEqual(replay_preview["plan"]["new_calls"], 0)
        self.assertEqual(replay_preview["plan"]["blocked_calls"], 0)
        replay = finish_large_job(self.start(replay_preview))
        self.assertEqual(replay["state"], "completed", replay)
        self.assertEqual(replay["progress"]["cached_calls"], 51)
        self.assertEqual(ai.report(self.root, replay["report_id"])["coverage"], first_report["coverage"])
        self.assertEqual(replay["result"], first["result"])
        cloud.assert_not_called()
        merge.assert_not_called()
        request_guard.assert_not_called()
        for guard in network_guards:
            guard.assert_not_called()

    def test_full_long_message_text_is_preserved_through_all_leaf_calls(self):
        text = "甲" * (segments.CHUNK_CHARS + 13) + "长消息末尾标记"
        self.write_messages(1, text)
        preview = self.preview()
        cloud, _ = self.mock_cloud()
        status = self.finish(self.start(preview))
        self.assertEqual(status["state"], "completed")
        uploaded = [record for call in cloud.call_args_list for record in call.args[2]["sample"]]
        self.assertEqual("".join(record["text"] for record in uploaded), text)
        self.assertEqual(preview["sample_messages"], 1)
        self.assertEqual(preview["truncated_messages"], 0)
        self.assertEqual(preview["plan"]["split_messages"], 1)
        for call in cloud.call_args_list:
            records = call.args[2]["sample"]
            self.assertLessEqual(len(records), segments.CHUNK_MESSAGES)
            self.assertLessEqual(sum(len(record["text"]) for record in records), segments.CHUNK_CHARS)

    def test_full_mode_hard_cap_rejects_preview_without_silent_sampling(self):
        self.small_chunks(size=1)
        cloud, merge = self.mock_cloud()
        with mock.patch.object(segments, "MAX_SEGMENTS", 3), self.assertRaises(ai.AnalysisError):
            self.preview()
        cloud.assert_not_called()
        merge.assert_not_called()

    def test_full_snapshot_larger_than_legacy_read_limit_can_be_consumed(self):
        self.write_messages(40, "合成文字" * 5000)
        preview = self.preview()
        snapshot = ai._root(self.root) / ("preview-" + preview["preview_id"] + ".json")
        self.assertGreater(snapshot.stat().st_size, 2 * 1024 * 1024)
        cloud, _ = self.mock_cloud()
        self.assertEqual(self.finish(self.start(preview))["state"], "completed")
        self.assertEqual(cloud.call_count, preview["plan"]["segments"])

    def test_complete_cache_replay_requires_new_consent_but_no_cloud_calls(self):
        self.small_chunks()
        cloud, merge = self.mock_cloud()
        first = self.preview()
        self.assertEqual(self.finish(self.start(first))["state"], "completed")
        first_calls = cloud.call_count + merge.call_count
        self.assertEqual(first_calls, first["plan"]["total_calls"])
        second = self.preview()
        self.assertEqual(second["plan"]["new_calls"], 0)
        self.assertEqual(second["plan"]["cached_calls"], first_calls)
        with self.assertRaises(ai.AnalysisError):
            self.start(second, consent=False)
        self.assertEqual(self.finish(self.start(second))["state"], "completed")
        self.assertEqual(cloud.call_count + merge.call_count, first_calls)

    def test_cache_is_bound_to_full_consent_scope_and_configuration_revision(self):
        self.write_messages(1)
        self.mock_cloud()
        self.assertEqual(self.finish(self.start(self.preview()))["state"], "completed")
        self.assertGreater(self.preview()["plan"]["cached_calls"], 0)
        for overrides in ({"focus": "business"}, {"date_from": "2026-08-02"},
                          {"include_voice_transcripts": True}, {"analysis_mode": "sample"}):
            with self.subTest(overrides=overrides):
                changed = self.preview(**overrides)
                self.assertEqual(changed["plan"]["cached_calls"], 0)
                self.assertGreater(changed["plan"]["new_calls"], 0)
        self.configure(model="synthetic-new-model", api_key="")
        self.assertEqual(self.preview()["plan"]["cached_calls"], 0)

    def test_cache_disappearing_after_preview_requires_new_cost_confirmation(self):
        self.write_messages(1)
        cloud, _ = self.mock_cloud()
        self.assertEqual(self.finish(self.start(self.preview()))["state"], "completed")
        preview = self.preview()
        self.assertEqual(preview["plan"]["new_calls"], 0)
        cache_files = list(ai._root(self.root).glob("call-*.json"))
        self.assertEqual(len(cache_files), 1)
        cache_files[0].unlink()
        with self.assertRaises(ai.AnalysisError):
            self.start(preview)
        self.assertEqual(cloud.call_count, 1)

    def test_cache_disappearing_during_run_cannot_add_unapproved_calls(self):
        self.small_chunks()
        cloud, merge = self.mock_cloud()
        self.assertEqual(self.finish(self.start(self.preview()))["state"], "completed")
        calls_before = cloud.call_count + merge.call_count
        preview = self.preview()
        root = ai._root(self.root)
        prepared = ai._unseal(ai._read(root / ("preview-" + preview["preview_id"] + ".json"),
                                      max_bytes=ai.MAX_PREVIEW_BYTES)["sealed"])
        leaves, _, _ = segments._nodes(ai._configuration(root), prepared)
        next_cache = root / ("call-" + leaves[1]["key"] + ".json")
        original_call = segments._call
        dropped = False
        def drop_after_first(*args, **kwargs):
            nonlocal dropped
            result = original_call(*args, **kwargs)
            if not dropped:
                next_cache.unlink()
                dropped = True
            return result
        with mock.patch.object(segments, "_call", side_effect=drop_after_first):
            status = self.finish(self.start(preview))
        self.assertNotEqual(status["state"], "completed")
        self.assertTrue(dropped)
        self.assertEqual(cloud.call_count + merge.call_count, calls_before)

    def test_uncertain_failure_does_not_silently_repeat_from_a_fresh_preview(self):
        self.write_messages(1)
        cloud, merge = self.mock_cloud(side_effect=RuntimeError("SYNTHETIC-PRIVATE-ERROR"))
        failed = self.finish(self.start(self.preview()))
        self.assertEqual(failed["state"], "error")
        self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(failed))
        self.assertEqual(cloud.call_count, 1)
        next_preview = self.preview()
        self.assertGreaterEqual(next_preview["plan"]["blocked_calls"], 1)
        try:
            rejected = self.start(next_preview)
        except ai.AnalysisError:
            pass
        else:
            self.assertNotEqual(self.finish(rejected)["state"], "completed")
        self.assertEqual(cloud.call_count, 1)
        merge.assert_not_called()

    def test_pending_record_must_be_written_before_any_billable_call(self):
        self.write_messages(1)
        preview = self.preview()
        cloud, merge = self.mock_cloud()
        original_write = ai._write
        def fail_pending(path, value):
            if path.name.startswith("call-") and value.get("state") == "pending":
                raise OSError("synthetic disk unavailable before dispatch")
            return original_write(path, value)
        with mock.patch.object(ai, "_write", side_effect=fail_pending):
            status = self.finish(self.start(preview))
        self.assertEqual(status["state"], "error")
        cloud.assert_not_called()
        merge.assert_not_called()

    def test_failed_success_checkpoint_preserves_a_billing_tombstone(self):
        self.write_messages(1)
        preview = self.preview()
        cloud, _ = self.mock_cloud()
        original_write = ai._write
        def fail_success(path, value):
            if path.name.startswith("call-") and value.get("state") == "completed":
                raise OSError("synthetic disk unavailable after response")
            return original_write(path, value)
        with mock.patch.object(ai, "_write", side_effect=fail_success):
            status = self.finish(self.start(preview))
        self.assertEqual(status["state"], "error")
        self.assertEqual(cloud.call_count, 1)
        cache_files = list(ai._root(self.root).glob("call-*.json"))
        self.assertEqual(len(cache_files), 1)
        self.assertIn(ai._read(cache_files[0])["state"], {"pending", "error"})
        next_preview = self.preview()
        self.assertEqual(next_preview["plan"]["blocked_calls"], 1)
        with self.assertRaises(ai.AnalysisError):
            self.start(next_preview)
        self.assertEqual(cloud.call_count, 1)

    def test_completed_cache_hides_model_output_and_never_stores_api_key(self):
        self.write_messages(1)
        self.mock_cloud()
        self.assertEqual(self.finish(self.start(self.preview()))["state"], "completed")
        cache_files = list(ai._root(self.root).glob("call-*.json"))
        self.assertEqual(len(cache_files), 1)
        raw = cache_files[0].read_text(encoding="utf-8")
        self.assertNotIn(RESULT["summary"], raw)
        self.assertNotIn(self.secret, raw)
        self.assertNotIn("合成消息", raw)
        self.assertEqual(ai._unseal(json.loads(raw)["sealed_result"]), RESULT)

    def test_unknown_retry_needs_literal_opt_in_and_fresh_preview(self):
        self.write_messages(1)
        cloud, _ = self.mock_cloud(side_effect=RuntimeError("synthetic uncertain response"))
        stale = self.preview()
        self.assertEqual(self.finish(self.start(self.preview()))["state"], "error")
        with self.assertRaises(ai.AnalysisError):
            self.start(stale, retry_uncertain=True)
        for opt_in in (False, None, 1, "true"):
            with self.subTest(opt_in=opt_in), self.assertRaises(ai.AnalysisError):
                self.start(self.preview(), retry_uncertain=opt_in)
        self.assertEqual(cloud.call_count, 1)
        cloud.side_effect = None
        self.assertEqual(self.finish(self.start(self.preview(), retry_uncertain=True))["state"], "completed")
        self.assertEqual(cloud.call_count, 2)

    def test_source_change_after_preview_never_sends_stale_scope(self):
        cloud, merge = self.mock_cloud()
        preview = self.preview()
        self.write_messages(8)
        with self.assertRaises(ai.AnalysisError):
            self.start(preview)
        cloud.assert_not_called()
        merge.assert_not_called()

    def test_configuration_change_between_chunks_stops_further_requests(self):
        self.small_chunks()
        def change_configuration(*_args):
            self.configure(model="synthetic-changed-model", api_key="")
            return RESULT
        cloud, merge = self.mock_cloud(side_effect=change_configuration)
        status = self.finish(self.start(self.preview()))
        self.assertNotEqual(status["state"], "completed")
        self.assertEqual(cloud.call_count, 1)
        merge.assert_not_called()

    def test_cancel_after_current_call_then_resume_reuses_completed_chunk(self):
        self.small_chunks()
        entered = threading.Event()
        def slow_cloud(*_args):
            entered.set()
            if not self.release.wait(3):
                raise RuntimeError("synthetic release was not signaled")
            return RESULT
        cloud, merge = self.mock_cloud(side_effect=slow_cloud)
        first = self.preview()
        job = self.start(first)
        self.assertTrue(entered.wait(2))
        ai.cancel(self.root, job["job_id"])
        self.release.set()
        status = self.finish(job)
        self.assertNotEqual(status["state"], "completed")
        self.assertEqual(cloud.call_count, 1)
        merge.assert_not_called()
        second = self.preview()
        self.assertGreaterEqual(second["plan"]["cached_calls"], 1)
        self.assertEqual(self.finish(self.start(second))["state"], "completed")
        self.assertEqual(cloud.call_count, first["plan"]["segments"])

    def test_partial_long_message_is_not_counted_as_fully_analyzed(self):
        self.write_messages(1, "乙" * (segments.CHUNK_CHARS + 1))
        entered = threading.Event()
        def slow_cloud(*_args):
            entered.set()
            if not self.release.wait(3):
                raise RuntimeError("synthetic release was not signaled")
            return RESULT
        self.mock_cloud(side_effect=slow_cloud)
        job = self.start(self.preview())
        self.assertTrue(entered.wait(2))
        ai.cancel(self.root, job["job_id"])
        self.release.set()
        status = self.finish(job)
        self.assertEqual(status["state"], "cancelled")
        report = ai.report(self.root, status["report_id"])
        self.assertEqual(report["coverage"]["analyzed_messages"], 0)
        self.assertEqual(report["coverage"]["eligible_messages"], 1)
        self.assertFalse(report["coverage"]["complete"])
        resumed = self.finish(self.start(self.preview()))
        report = ai.report(self.root, resumed["report_id"])
        self.assertEqual(report["coverage"]["analyzed_messages"], 1)
        self.assertTrue(report["coverage"]["complete"])

    def test_hierarchical_summary_covers_each_leaf_and_matches_preview_calls(self):
        self.small_chunks(size=1)
        leaf_labels = []
        def cloud_result(*_args):
            label = "LEAF_" + chr(ord("A") + len(leaf_labels))
            leaf_labels.append(label)
            return {**RESULT, "summary": label}
        def merge_result(_config, _key, _scope, packets):
            labels = sorted(set(re.findall(r"LEAF_[A-Z]+", json.dumps(packets))))
            return {**RESULT, "summary": " ".join(labels)}
        cloud, merge = self.mock_cloud(side_effect=cloud_result, merge_side_effect=merge_result)
        preview = self.preview()
        status = self.finish(self.start(preview))
        self.assertEqual(status["state"], "completed")
        self.assertEqual(cloud.call_count, 7)
        self.assertEqual(merge.call_count, preview["plan"]["merge_calls"])
        self.assertEqual(cloud.call_count + merge.call_count, preview["plan"]["total_calls"])
        summary = status["result"]["summary"]
        self.assertEqual(sorted(re.findall(r"LEAF_[A-Z]+", summary)), leaf_labels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
