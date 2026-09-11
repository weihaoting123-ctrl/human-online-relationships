"""Synthetic-only regression tests: never inspect the user's real chat stores."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai
from dashboard import app


def message(day, content, sender="them", kind="text"):
    return {"timestamp": datetime.fromisoformat(day + "T12:00:00").timestamp(),
            "sender": sender, "type": kind, "content": content}


RESULT = {"summary": "本次交流以事项确认为主", "observations": ["存在后续事项"],
          "actions": ["在合适时间确认下一步"], "caveats": ["样本有限"]}


class ScopedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contacts = self.root / "contacts"
        self.bundle = self.contacts / "synthetic-contact"
        self.bundle.mkdir(parents=True)
        self.path = self.bundle / "messages.json"
        self.payload = {"contact_display": "测试联系人", "my_display": "测试本人", "messages": [
            message("2026-07-01", "outside-before"),
            message("2026-08-10", "测试联系人联系测试本人 13812345678 test@example.com wxid_abc123", "me"),
            message("2026-08-11", "selected-text-only"),
            message("2026-08-12", "secret-image-payload", kind="image"),
            message("2026-09-01", "outside-after"),
        ]}
        self.write_payload()
        other = self.contacts / "other-contact"
        other.mkdir()
        (other / "messages.json").write_text(json.dumps({"messages": [message("2026-08-10", "OTHER-CONTACT-SECRET")]}), encoding="utf-8")
        self.request = {"bundle_id": self.bundle.name, "date_from": "2026-08-01", "date_to": "2026-08-31",
                        "focus": "communication", "max_messages": 100}
        self.secret = "synthetic-api-key-not-real-12345"
        self.crypto_patch = None
        if os.name != "nt":
            # Crypto availability has its own fail-closed test; use a reversible
            # stand-in for flow tests on non-Windows build hosts only.
            self.crypto_patch = mock.patch.object(ai, "_dpapi", side_effect=lambda data, **_kw: data)
            self.crypto_patch.start()

    def tearDown(self):
        deadline = time.time() + 3
        while ai._ACTIVE_JOBS and time.time() < deadline:
            time.sleep(0.01)
        if self.crypto_patch:
            self.crypto_patch.stop()
        self.temp.cleanup()

    def write_payload(self):
        self.path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")

    def config(self, **overrides):
        return ai.save_config(self.root, {"provider": "openai", "model": "synthetic-test-model",
                                          "api_key": self.secret, **overrides})

    def preview(self, **overrides):
        return ai.preview(self.root, self.contacts, {**self.request, **overrides})

    def run_to_completion(self, preview, side_effect=None):
        with mock.patch.object(ai, "_cloud", return_value=RESULT, side_effect=side_effect) as cloud:
            job = ai.run(self.root, self.contacts, {"preview_id": preview["preview_id"], "consent": True})
            deadline = time.time() + 3
            while time.time() < deadline:
                status = ai.job_status(self.root, job["job_id"])
                if status["state"] in {"completed", "error"}:
                    return status, cloud
                time.sleep(0.01)
        self.fail("synthetic worker did not complete")

    def test_preview_is_local_and_only_returns_counts(self):
        with mock.patch.object(urllib.request, "build_opener") as network:
            result = self.preview()
        network.assert_not_called()
        self.assertEqual(result["eligible_messages"], 2)
        self.assertEqual(result["sample_messages"], 2)
        self.assertEqual(result["stats"]["excluded_nontext"], 1)
        self.assertEqual(result["stats"]["scope_messages"], 3)
        rendered = json.dumps(result)
        self.assertNotIn("selected-text", rendered)
        self.assertNotIn("13812345678", rendered)
        path = ai._root(self.root) / f'preview-{result["preview_id"]}.json'
        self.assertNotIn("selected-text-only", path.read_text(encoding="utf-8"))

    def test_dpapi_key_does_not_appear_in_files_or_status(self):
        result = self.config()
        self.assertTrue(result["configured"])
        self.assertTrue(result["has_key"])
        raw = (ai._root(self.root) / "config.json").read_text(encoding="utf-8")
        self.assertNotIn(self.secret, raw)
        self.assertNotIn(self.secret, json.dumps(result))
        value = json.loads(raw)
        self.assertEqual(ai._unseal(value["sealed_key"]), self.secret)

    def test_save_does_not_connect_and_blank_preserves_same_provider(self):
        with mock.patch.object(urllib.request, "build_opener") as network:
            self.config()
            before = ai._configuration(ai._root(self.root))
            self.config(api_key="", model="another-test-model")
            after = ai._configuration(ai._root(self.root))
        network.assert_not_called()
        self.assertEqual(before["sealed_key"], after["sealed_key"])
        self.assertNotEqual(before["revision"], after["revision"])
        with self.assertRaises(ai.AnalysisError):
            self.config(provider="deepseek", api_key="")

    def test_deepseek_flash_configuration_preserves_key_and_requires_new_preview(self):
        with mock.patch.object(urllib.request, "build_opener") as network:
            self.config(provider="deepseek", model="deepseek-v4-pro")
            before = ai._configuration(ai._root(self.root))
            preview = self.preview()
            with mock.patch.object(ai, "_unseal", side_effect=AssertionError("saving must not decrypt")):
                status = self.config(provider="deepseek", model="deepseek-flash", api_key="")
            after = ai._configuration(ai._root(self.root))
            self.assertEqual(status["model"], "deepseek-flash")
            self.assertEqual(status["endpoint"], ai.PROVIDERS["deepseek"])
            self.assertTrue(status["has_key"])
            self.assertEqual(before["sealed_key"], after["sealed_key"])
            self.assertNotEqual(before["revision"], after["revision"])
            self.assertNotIn(self.secret, json.dumps(status))
            with self.assertRaisesRegex(ai.AnalysisError, "服务商配置已变化"):
                ai.run(self.root, self.contacts, {"preview_id": preview["preview_id"], "consent": True})
        network.assert_not_called()

    def test_clear_only_removes_config(self):
        self.config()
        preview = self.preview()
        path = ai._root(self.root) / f'preview-{preview["preview_id"]}.json'
        result = ai.clear_config(self.root)
        self.assertFalse(result["configured"])
        self.assertTrue(path.exists())
        self.assertTrue(self.path.exists())

    def test_input_validation_and_no_custom_endpoints(self):
        for changes in ({"provider": "evil"}, {"provider": []}, {"model": "bad\nmodel"},
                        {"model": ""}, {"endpoint": "https://evil.invalid"}, {"api_key": "bad\nkey"}):
            with self.subTest(changes=changes), self.assertRaises(ai.AnalysisError):
                self.config(**changes)
        for changes in ({"focus": []}, {"date_from": "2026-02-30"}, {"date_to": "2026-01-01"},
                        {"max_messages": True}, {"max_messages": 9999}, {"bundle_id": "../other-contact"}):
            with self.subTest(changes=changes), self.assertRaises(ai.AnalysisError):
                self.preview(**changes)

    def test_explicit_literal_consent_required(self):
        self.config()
        value = self.preview()
        for consent in (False, None, "true", 1):
            with self.subTest(consent=consent), self.assertRaises(ai.AnalysisError):
                ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": consent})

    def test_configuration_required_and_preview_must_follow_config(self):
        value = self.preview()
        with self.assertRaisesRegex(ai.AnalysisError, "先.*配置"):
            ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})
        self.config()
        with self.assertRaisesRegex(ai.AnalysisError, "配置已变化"):
            ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})

    def test_preview_recipient_is_bound_to_snapshot_not_stale_browser_config(self):
        unconfigured = self.preview()
        self.assertFalse(unconfigured["recipient"]["configured"])
        stale_browser_config = self.config()
        self.config(provider="deepseek", model="other-synthetic-model")
        value = self.preview()
        self.assertEqual(stale_browser_config["provider"], "openai")
        self.assertEqual(value["recipient"], {
            "configured": True, "provider": "deepseek", "model": "other-synthetic-model",
            "endpoint": ai.PROVIDERS["deepseek"],
        })
        self.assertNotIn(self.secret, json.dumps(value))
        self.config(model="third-synthetic-model")
        with self.assertRaisesRegex(ai.AnalysisError, "配置已变化"):
            ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})

    def test_ttl_source_and_provider_changes_rejected_without_cloud(self):
        self.config()
        value = self.preview()
        with mock.patch.object(ai.time, "time", return_value=time.time() + 1000), self.assertRaisesRegex(ai.AnalysisError, "过期"):
            ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})
        value = self.preview()
        self.payload["messages"].append(message("2026-08-30", "new-message"))
        self.write_payload()
        with self.assertRaisesRegex(ai.AnalysisError, "数据已更新"):
            ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})
        value = self.preview()
        self.config(provider="deepseek")
        with self.assertRaisesRegex(ai.AnalysisError, "配置已变化"):
            ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})

    def test_exact_selected_snapshot_single_use_and_local_history(self):
        self.config()
        value = self.preview()
        status, cloud = self.run_to_completion(value)
        self.assertEqual(status["state"], "completed")
        prepared = cloud.call_args.args[2]
        text = json.dumps(prepared["sample"], ensure_ascii=False)
        self.assertIn("selected-text-only", text)
        for excluded in ("outside-before", "outside-after", "OTHER-CONTACT-SECRET", "secret-image-payload",
                         "测试联系人", "测试本人", "13812345678", "test@example.com", "wxid_abc123"):
            self.assertNotIn(excluded, text)
        with self.assertRaisesRegex(ai.AnalysisError, "已使用"):
            ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})
        history = ai.history(self.root)
        self.assertEqual(len(history["reports"]), 1)
        self.assertNotIn("result", history["reports"][0])
        result = ai.report(self.root, status["report_id"])["result"]
        self.assertEqual({key: result[key] for key in RESULT}, RESULT)
        self.assertFalse(result["timeline"]["generated"])
        self.assertFalse(list(ai._root(self.root).glob("consumed-*.json")))

    def test_concurrent_double_submit_sends_at_most_once(self):
        self.config()
        value = self.preview()
        release = threading.Event()
        def cloud_call(*_args):
            release.wait(2)
            return RESULT
        def submit():
            try:
                return ai.run(self.root, self.contacts, {"preview_id": value["preview_id"], "consent": True})
            except ai.AnalysisError:
                return None
        with mock.patch.object(ai, "_cloud", side_effect=cloud_call) as cloud:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: submit(), range(2)))
            self.assertEqual(sum(result is not None for result in results), 1)
            release.set()
            deadline = time.time() + 3
            while ai._ACTIVE_JOBS and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(cloud.call_count, 1)

    def test_error_payload_never_echoes_exception_or_remote_body(self):
        self.config()
        value = self.preview()
        status, _ = self.run_to_completion(value, RuntimeError("LEAK-CHAT-AND-KEY"))
        self.assertEqual(status["state"], "error")
        self.assertNotIn("LEAK", json.dumps(status))
        self.assertEqual(ai.history(self.root)["reports"], [])

    def test_sampling_budget_keeps_beginning_and_end(self):
        payload = {"messages": [message("2026-08-10", f"{i}:" + "字" * 1500) for i in range(1000)]}
        prepared, counts = ai._sample(payload, self.request, 600)
        self.assertEqual(len(prepared), 600)
        self.assertLessEqual(counts["sample_chars"], 60_000)
        self.assertEqual(counts["truncated_messages"], 600)
        self.assertTrue(prepared[0]["text"].startswith("0:"))
        self.assertTrue(prepared[-1]["text"].startswith("999:"))

    def test_common_identifiers_paths_credentials_redacted(self):
        value = ai.redact_text("hello wxid_testid person@test.example +86 13812345678 sk-abcdef1234567890 "
                               "https://example.com/private C:\\Users\\someone\\secret.txt token=abcdefghi")
        for term in ("wxid_testid", "person@test.example", "13812345678", "abcdef1234567890", "example.com", "someone", "abcdefghi"):
            self.assertNotIn(term, value)

    def test_extended_sampling_keeps_selected_count_within_batch_budget(self):
        payload = {"messages": [message("2026-08-10", "长" * 1200) for _ in range(6000)]}
        scope, limit = ai._scope({**self.request, "max_messages": 6000})
        sample, counts = ai._sample(payload, scope, limit)
        self.assertEqual(len(sample), 6000)
        self.assertEqual(counts["truncated_messages"], 6000)
        self.assertLessEqual(counts["sample_chars"], 4_800_000)

    def test_truncated_but_parseable_cloud_response_is_not_accepted_or_retried(self):
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"choices": [{"finish_reason": "length", "message": {"content": json.dumps(RESULT)}}]}).encode()
        with mock.patch.object(urllib.request, "build_opener", return_value=opener), self.assertRaises(ai.AnalysisError):
            ai._request_result({"provider": "deepseek", "model": "deepseek-v4-pro"}, self.secret, {}, ai.SYSTEM_PROMPT)
        self.assertEqual(opener.open.call_count, 1)

    def test_cloud_transport_fixed_destination_no_proxy_no_other_scope(self):
        self.config()
        self.preview()
        sample, counts = ai._sample(self.payload, self.request, 100)
        prepared = {"scope": self.request, "counts": counts, "sample": sample, "signature": "PRIVATE-SIGNATURE"}
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(RESULT)}}]}).encode()
        with mock.patch.object(urllib.request, "build_opener", return_value=opener) as factory:
            result = ai._cloud({"provider": "openai", "model": "synthetic-model"}, self.secret, prepared)
        self.assertEqual(result["summary"], RESULT["summary"])
        self.assertEqual(factory.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ai.PROVIDERS["openai"])
        sent = json.loads(request.data)
        self.assertFalse(sent["store"])
        self.assertIn("max_completion_tokens", sent)
        body = json.dumps(sent, ensure_ascii=False)
        for excluded in (self.secret, self.bundle.name, "PRIVATE-SIGNATURE", "outside-before", "OTHER-CONTACT-SECRET"):
            self.assertNotIn(excluded, body)
        self.assertEqual(request.headers["Authorization"], "Bearer " + self.secret)

    def test_redirect_and_remote_errors_fail_closed(self):
        with self.assertRaises(ai.AnalysisError):
            ai._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")
        sample, counts = ai._sample(self.payload, self.request, 100)
        prepared = {"scope": self.request, "counts": counts, "sample": sample}
        opener = mock.MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://fake.invalid/SECRET", 401, "LEAK-MESSAGE", {}, io.BytesIO(b"LEAK-REMOTE-BODY"))
        with mock.patch.object(urllib.request, "build_opener", return_value=opener), self.assertRaises(ai.AnalysisError) as raised:
            ai._cloud({"provider": "deepseek", "model": "synthetic-model"}, self.secret, prepared)
        self.assertNotIn("LEAK", str(raised.exception))
        sent = json.loads(opener.open.call_args.args[0].data)
        self.assertIn("max_tokens", sent)
        self.assertNotIn("max_completion_tokens", sent)

    def test_deepseek_v4_uses_bounded_nonthinking_json(self):
        sample, counts = ai._sample(self.payload, self.request, 100)
        prepared = {"scope": self.request, "counts": counts, "sample": sample}
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(RESULT)}}]}).encode()
        with mock.patch.object(urllib.request, "build_opener", return_value=opener):
            ai._cloud({"provider": "deepseek", "model": "deepseek-v4-pro"}, self.secret, prepared)
        request = opener.open.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(sent["model"], "deepseek-v4-pro")
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(sent["max_tokens"], 2400)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertNotIn("store", sent)
        self.assertNotIn("max_completion_tokens", sent)
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn(self.secret, request.data.decode())

    def test_deepseek_flash_uses_bounded_nonthinking_json(self):
        sample, counts = ai._sample(self.payload, self.request, 100)
        prepared = {"scope": self.request, "counts": counts, "sample": sample}
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(RESULT)}}]}).encode()
        with mock.patch.object(urllib.request, "build_opener", return_value=opener):
            ai._cloud({"provider": "deepseek", "model": "deepseek-flash"}, self.secret, prepared)
        request = opener.open.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(sent.get("thinking"), {"type": "disabled"})
        self.assertEqual(sent["model"], "deepseek-flash")
        self.assertEqual(sent["max_tokens"], 2400)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(request.full_url, ai.PROVIDERS["deepseek"])
        self.assertNotIn("store", sent)
        self.assertNotIn("max_completion_tokens", sent)
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn(self.secret, request.data.decode())

    def test_other_deepseek_model_ids_do_not_inherit_flash_parameters(self):
        for model in ("deepseek-chat", "deepseek-reasoner", "deepseek-flash-custom", "synthetic-model"):
            with self.subTest(model=model):
                opener = mock.MagicMock()
                response = opener.open.return_value.__enter__.return_value
                response.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(RESULT)}}]}).encode()
                with mock.patch.object(urllib.request, "build_opener", return_value=opener):
                    ai._request_result({"provider": "deepseek", "model": model}, self.secret, {}, ai.SYSTEM_PROMPT)
                sent = json.loads(opener.open.call_args.args[0].data)
                self.assertNotIn("thinking", sent)
                self.assertEqual(sent["model"], model)
                self.assertEqual(sent["max_tokens"], 2400)

    def test_output_schema_bounded_and_quotes_html_removed(self):
        result = ai._safe_result({**RESULT, "summary": "<script>bad</script>「hidden quote」" + "字" * 2000,
                                  "actions": ["action"] * 20, "extra": "SECRET"})
        self.assertEqual(set(result), {"summary", "observations", "actions", "caveats", "timeline"})
        self.assertFalse(result["timeline"]["generated"])
        self.assertLessEqual(len(result["summary"]), 1200)
        self.assertEqual(len(result["actions"]), 8)
        self.assertNotIn("<script>", result["summary"])
        self.assertNotIn("hidden quote", result["summary"])
        with self.assertRaises(ai.AnalysisError):
            ai._safe_result({"summary": "incomplete"})

    def test_job_completion_does_not_race_into_false_restart_error(self):
        job_id = "b" * 48
        path = ai._root(self.root) / f"job-{job_id}.json"
        ai._write(path, {"status": "ok", "job_id": job_id, "state": "running"})
        ai._ACTIVE_JOBS.add(job_id)
        read_started, finished = threading.Event(), threading.Event()
        real_read = ai._read
        def stale_read(*args, **kwargs):
            observed = real_read(*args, **kwargs)
            read_started.set()
            finished.wait(0.15)
            return observed
        def complete():
            read_started.wait(1)
            with ai._LOCK:
                ai._write(path, {"status": "ok", "job_id": job_id, "state": "completed"})
                ai._ACTIVE_JOBS.discard(job_id)
            finished.set()
        worker = threading.Thread(target=complete)
        worker.start()
        with mock.patch.object(ai, "_read", side_effect=stale_read):
            state = ai.job_status(self.root, job_id)
        worker.join(timeout=2)
        self.assertEqual(state["state"], "running")
        self.assertEqual(ai.job_status(self.root, job_id)["state"], "completed")

    def test_incremental_public_counters_are_allowlisted_and_flags_strict(self):
        status_path = self.root / "synthetic-sync.json"
        status_path.write_text(json.dumps({"state": "completed", "sync_strategy": "changed-shards",
                                           "skipped_databases": 40, "read_messages": 3,
                                           "hidden_identifier": "PRIVATE"}), encoding="utf-8")
        (self.root / "archive-status.json").write_text(json.dumps({"state": "completed", "processed_files": 4,
                                                                   "skipped_existing_files": 300,
                                                                   "report_rebuilt": "true", "incremental": True,
                                                                   "secret": "PRIVATE"}), encoding="utf-8")
        (self.root / "classification-status.json").write_text(json.dumps({"state": "completed", "processed_messages": 7,
                                                                         "skipped_conversations": 20,
                                                                         "report_rebuilt": 1}), encoding="utf-8")
        with mock.patch.object(app, "DATA_DIR", self.root), mock.patch.object(app, "SYNC_STATUS_PATH", status_path):
            sync, archive = app.sync_status(), app.archive_status()
        self.assertEqual(sync["sync_strategy"], "changed-shards")
        self.assertEqual(sync["skipped_databases"], 40)
        self.assertEqual(sync["read_messages"], 3)
        self.assertEqual(archive["media"]["processed_files"], 4)
        self.assertTrue(archive["media"]["incremental"])
        self.assertFalse(archive["media"]["report_rebuilt"])
        self.assertFalse(archive["classification"]["report_rebuilt"])
        self.assertEqual(archive["classification"]["skipped_conversations"], 20)
        self.assertNotIn("PRIVATE", json.dumps([sync, archive]))

    def test_http_ai_routes_require_session_and_post_origin(self):
        with mock.patch.object(app, "DATA_DIR", self.root), mock.patch.object(app, "CONTACTS_DIR", self.contacts):
            server = app.create_server(port=0, token="synthetic-local-server-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for path in ("/api/ai/config", "/api/ai/history", "/api/ai/jobs/" + "a" * 48):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(base + path)
                    self.assertEqual(raised.exception.code, 403)
                headers = {"X-Local-Token": "synthetic-local-server-token", "Content-Type": "application/json"}
                req = urllib.request.Request(base + "/api/ai/preview", json.dumps(self.request).encode(), headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(req)
                self.assertEqual(raised.exception.code, 403)
                req.add_header("Origin", base)
                with urllib.request.urlopen(req) as response:
                    value = json.load(response)
                self.assertEqual(value["sample_messages"], 2)
                self.assertNotIn("selected-text", json.dumps(value))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
