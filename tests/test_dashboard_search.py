"""Local search regression tests using generated, non-personal fixtures only."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from dashboard import app, search


class SearchFixture(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="search-test-", dir=scratch)
        self.data = Path(self.temp.name)
        self.contacts = self.data / "contacts"
        self.contacts.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, key="demo__0001", messages=None):
        bundle = self.contacts / key
        bundle.mkdir(exist_ok=True)
        if messages is None:
            messages = [self.message("合成项目 测试 Alpha 100% _ *", "2026-09-01")]
        path = bundle / "messages.json"
        path.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")
        return bundle

    @staticmethod
    def message(text, day, **extra):
        return {"content": text, "timestamp": datetime.fromisoformat(day + "T12:00:00").timestamp(), **extra}

    def run_search(self, query="合成项目", **extra):
        return search.search_messages(self.data, self.contacts, {"query": query, **extra})


class LocalSearchTests(SearchFixture):
    def test_empty_index_is_lazy_and_has_bounded_public_schema(self):
        self.assertFalse((self.data / "private").exists())
        result = self.run_search()
        self.assertEqual(result, {"status": "ok", "matches": [], "indexed_messages": 0, "indexed_conversations": 0, "skipped_conversations": 0})
        self.assertTrue((self.data / "private/dashboard-search/index.sqlite3").is_file())

    def test_chinese_substrings_casefold_and_fullwidth_normalization(self):
        self.bundle(messages=[self.message("合成项目 ＡＬＰＨＡ Straße", "2026-09-01")])
        result = self.run_search("项目 alpha STRASSE")
        self.assertEqual(result["matches"], [{"bundle_id": "demo__0001", "match_count": 1, "last_match_date": "2026-09-01"}])

    def test_keywords_require_same_message_and_count_messages_not_occurrences(self):
        self.bundle(messages=[
            self.message("alpha", "2026-09-01"),
            self.message("beta", "2026-09-02"),
            self.message("alpha beta beta", "2026-09-03"),
        ])
        result = self.run_search("alpha beta")
        self.assertEqual(result["matches"][0]["match_count"], 1)
        self.assertEqual(result["matches"][0]["last_match_date"], "2026-09-03")

    def test_sql_wildcards_and_regex_characters_are_literal(self):
        self.bundle()
        for query in ("100%", "_", "*"):
            with self.subTest(query=query):
                self.assertEqual(self.run_search(query)["matches"][0]["match_count"], 1)
        for query in (".*", "%anything%", "' OR 1=1 --", "[abc]"):
            with self.subTest(query=query):
                self.assertEqual(self.run_search(query)["matches"], [])

    def test_date_filter_is_inclusive_and_validates_real_calendar_dates(self):
        self.bundle(messages=[self.message("needle", "2026-08-31"), self.message("needle", "2026-09-01"), self.message("needle", "2026-09-02")])
        self.assertEqual(self.run_search("needle", date_from="2026-09-01", date_to="2026-09-01")["matches"][0]["match_count"], 1)
        self.assertEqual(self.run_search("needle", date_to="2026-08-31")["matches"][0]["match_count"], 1)
        for fields in ({"date_from": "2026-02-30"}, {"date_to": "2026-9-01"}, {"date_from": None}, {"date_from": "2026-09-02", "date_to": "2026-09-01"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.run_search(**fields)

    def test_query_limits_reject_without_creating_private_index(self):
        for query in (None, {}, "", " \n\t", "a" * 201, "a b c d e f g h i", "a\x00b"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.run_search(query)
        self.assertFalse((self.data / "private").exists())

    def test_no_query_original_text_or_local_paths_in_response(self):
        self.bundle(messages=[self.message("secret-fixture-phrase with synthetic private text", "2026-09-01")])
        result = self.run_search("secret-fixture-phrase")
        encoded = json.dumps(result)
        self.assertNotIn("secret-fixture-phrase", encoded)
        self.assertNotIn("synthetic private text", encoded)
        self.assertNotIn(str(self.data), encoded)
        self.assertEqual(set(result["matches"][0]), {"bundle_id", "match_count", "last_match_date"})

    def test_incremental_refresh_reuses_unchanged_and_refreshes_changed_exports(self):
        bundle = self.bundle()
        first = self.run_search("alpha")
        self.assertEqual(first["indexed_messages"], 1)
        with mock.patch.object(search, "_read_messages", side_effect=AssertionError("unchanged file was reread")):
            self.assertEqual(self.run_search("alpha")["matches"], first["matches"])
        self.bundle(messages=[self.message("new-content", "2026-09-02"), self.message("new-content", "2026-09-03")])
        self.assertEqual(self.run_search("alpha")["matches"], [])
        self.assertEqual(self.run_search("new-content")["matches"][0]["match_count"], 2)
        self.assertTrue((bundle / "messages.json").is_file())

    def test_refresh_detects_same_size_updated_mtime(self):
        bundle = self.bundle(messages=[self.message("aaaa", "2026-09-01")])
        self.run_search("aaaa")
        path = bundle / "messages.json"
        before = path.stat()
        path.write_text(path.read_text(encoding="utf-8").replace("aaaa", "bbbb"), encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000))
        self.assertEqual(self.run_search("bbbb")["matches"][0]["match_count"], 1)

    def test_removed_bundle_only_prunes_derived_index(self):
        removed = self.bundle("removed__0001")
        retained = self.bundle("retained__0002")
        original = (retained / "messages.json").read_bytes()
        self.assertEqual(self.run_search()["indexed_conversations"], 2)
        # Only this generated fixture is removed, to simulate an external removal.
        (removed / "messages.json").unlink()
        removed.rmdir()
        result = self.run_search()
        self.assertEqual(result["indexed_conversations"], 1)
        self.assertEqual((retained / "messages.json").read_bytes(), original)

    def test_malformed_changed_bundle_does_not_return_stale_matches(self):
        bundle = self.bundle()
        self.run_search()
        (bundle / "messages.json").write_text("{broken", encoding="utf-8")
        result = self.run_search()
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["skipped_conversations"], 1)

    def test_invalid_rows_are_skipped_and_partial_count_survives_cache(self):
        self.bundle(messages=[None, {"content": {"bad": True}, "timestamp": True}, self.message("needle", "2026-09-01")])
        for _ in range(2):
            result = self.run_search("needle")
            self.assertEqual(result["indexed_messages"], 1)
            self.assertEqual(result["skipped_conversations"], 1)

    def test_read_bound_is_enforced_without_changing_source(self):
        bundle = self.bundle()
        original = (bundle / "messages.json").read_bytes()
        with mock.patch.object(search, "MAX_FILE_BYTES", 8):
            self.assertEqual(self.run_search()["skipped_conversations"], 1)
        self.assertEqual((bundle / "messages.json").read_bytes(), original)

    def test_unsafe_source_and_private_index_paths_are_rejected(self):
        bundle = self.bundle()
        real_check = search._safe_path
        def reject_messages(path, root):
            return False if path.name == "messages.json" else real_check(path, root)
        with mock.patch.object(search, "_safe_path", side_effect=reject_messages):
            self.assertEqual(self.run_search()["skipped_conversations"], 1)
        self.assertFalse(search._safe_path(bundle, self.data / "elsewhere"))
        with mock.patch.object(search, "_safe_path", return_value=False), self.assertRaises(RuntimeError):
            self.run_search()

    def test_single_writer_busy_error_is_sanitized(self):
        with search._INDEX_LOCK:
            with self.assertRaisesRegex(RuntimeError, "本地搜索正忙"):
                self.run_search()

    def test_transcript_can_match_and_millisecond_dates_are_normalized(self):
        message = self.message("", "2026-09-02", transcript="合成语音文本")
        message["timestamp"] *= 1000
        self.bundle(messages=[message])
        self.assertEqual(self.run_search("语音", date_from="2026-09-02")["matches"][0]["last_match_date"], "2026-09-02")

    def test_summary_accepts_only_fixed_classification_fields(self):
        bundle = self.bundle()
        (bundle / "dashboard_manifest.json").write_text(json.dumps({"contact": "Synthetic", "conversation_kind": "group"}), encoding="utf-8")
        (bundle / "classification.json").write_text(json.dumps({"primary_topic": "工作与项目协作", "candidate_topics": ["工作与项目协作", "arbitrary-private-quote", {}, "工作与项目协作"], "secret": "not-returned"}, ensure_ascii=False), encoding="utf-8")
        result = app.bundle_summary(bundle)
        self.assertEqual(result["conversation_kind"], "group")
        self.assertEqual(result["primary_topic"], "工作与项目协作")
        self.assertEqual(result["candidate_topics"], ["工作与项目协作"])
        self.assertNotIn("arbitrary-private-quote", json.dumps(result))
        (bundle / "classification.json").write_text('{"primary_topic":{},"candidate_topics":{}}', encoding="utf-8")
        self.assertIsNone(app.bundle_summary(bundle)["primary_topic"])


class SearchHttpTests(SearchFixture):
    def setUp(self):
        super().setUp()
        self.patches = [mock.patch.object(app, "DATA_DIR", self.data), mock.patch.object(app, "CONTACTS_DIR", self.contacts)]
        for patch in self.patches:
            patch.start()
        self.server = app.create_server(port=0, token="synthetic-test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for patch in reversed(self.patches):
            patch.stop()
        super().tearDown()

    def post(self, payload, token=True, origin=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Local-Token"] = "synthetic-test-token"
        if origin:
            headers["Origin"] = self.base
        return urllib.request.urlopen(urllib.request.Request(self.base + "/api/search", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"), timeout=10)

    def test_http_search_is_authenticated_and_does_not_echo_text(self):
        self.bundle(messages=[self.message("private-synthetic-needle with other context", "2026-09-01")])
        for token, origin in ((False, True), (True, False)):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.post({"query": "private-synthetic-needle"}, token=token, origin=origin)
            self.assertEqual(failure.exception.code, 403)
        with self.post({"query": "private-synthetic-needle"}) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertNotIn("private-synthetic-needle", body)
            self.assertNotIn("other context", body)
            self.assertEqual(json.loads(body)["matches"][0]["match_count"], 1)

    def test_http_internal_error_is_sanitized(self):
        with mock.patch.object(app, "search_messages", side_effect=RuntimeError("PRIVATE fake-path fake-key")):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.post({"query": "secret-query"})
        self.assertEqual(failure.exception.code, 400)
        body = failure.exception.read().decode("utf-8")
        self.assertNotIn("PRIVATE", body)
        self.assertNotIn("secret-query", body)


if __name__ == "__main__":
    unittest.main()
