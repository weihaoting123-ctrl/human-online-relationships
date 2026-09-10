"""Heatmap HTTP boundary using temporary synthetic archives and random ports."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from dashboard import app


class ChatHeatmapApiTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="heatmap-api-", dir=artifacts)
        self.data = Path(self.temp.name) / "data"
        self.contacts = self.data / "contacts"
        self.bundle = self.contacts / "synthetic-heatmap"
        self.bundle.mkdir(parents=True)
        self.path = self.bundle / "messages.json"
        self.path.write_text(json.dumps({"contact_display": "SYNTHETIC_CONTACT", "messages": [
            {"timestamp": "2024-02-28T23:00:00", "type": "text", "sender": "SYNTHETIC_SENDER", "content": "SYNTHETIC_BODY"},
            {"timestamp": "2024-03-01T09:00:00", "type": "voice", "transcript": "SYNTHETIC_TRANSCRIPT"},
            {"timestamp": "2024-03-01T09:00:00", "type": "image", "content": "SYNTHETIC_MEDIA_PATH"},
        ]}), encoding="utf-8")
        self.original = self.path.read_bytes()
        # Projection needs only a manifest, so admission never parses message content.
        (self.bundle / "dashboard_manifest.json").write_text(json.dumps({
            "contact": "SYNTHETIC_CONTACT", "message_count": 3,
        }), encoding="utf-8")
        self.patches = [
            mock.patch.object(app, "DATA_DIR", self.data),
            mock.patch.object(app, "CONTACTS_DIR", self.contacts),
            mock.patch.object(app, "exporter_status", return_value={}),
            mock.patch.object(app.scoped_ai, "_cloud", side_effect=AssertionError("Cloud must not run")),
            mock.patch.object(app.subprocess, "Popen", side_effect=AssertionError("Subprocess must not run")),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.server = app.create_server(port=0)
        self.assertNotEqual(self.server.server_port, 8765)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        with urllib.request.urlopen(self.base) as response:
            self.cookie = response.headers["Set-Cookie"].split(";")[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.temp.cleanup()

    def request(self, body=None, *, path="/api/activity/heatmap", auth=True, origin=True, host=None, raw=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Cookie"] = self.cookie
        if origin:
            headers["Origin"] = self.base if origin is True else origin
        if host:
            headers["Host"] = host
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, headers=headers, data=data)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_returns_exact_content_free_aggregate_with_scoped_dense_days(self):
        status, response = self.request({"bundle_id": "synthetic-heatmap", "date_from": "2024-02-28", "date_to": "2024-03-01"})
        self.assertEqual(status, 200)
        self.assertEqual(set(response), {"status", "heatmap"})
        self.assertEqual(response["status"], "ok")
        heatmap = response["heatmap"]
        self.assertEqual([day["count"] for day in heatmap["days"]], [1, 0, 2])
        self.assertEqual(heatmap["summary"]["message_count"], 3)
        self.assertEqual(sum(sum(row) for row in heatmap["weekday_hour"]), 3)
        self.assertNotIn("SYNTHETIC", json.dumps(response))
        self.assertNotIn(str(self.data), json.dumps(response))

    def test_default_available_without_optional_analysis_module(self):
        self.assertEqual(self.request({"id": "analysis", "enabled": False, "expected_version": 0}, path="/api/modules")[0], 200)
        status, response = self.request({"bundle_id": "synthetic-heatmap"})
        self.assertEqual(status, 200)
        self.assertEqual(response["heatmap"]["scope"], {"date_from": "2023-12-03", "date_to": "2024-03-01", "days": 90})

    def test_requires_session_local_origin_and_local_host_before_reading_source(self):
        with mock.patch.object(app.scoped_ai, "_source", side_effect=AssertionError("Unauthorized source read")):
            for options, expected in [({"auth": False}, 403), ({"origin": False}, 403),
                                      ({"origin": "https://example.invalid"}, 403),
                                      ({"host": "example.invalid"}, 421)]:
                with self.subTest(options=options):
                    self.assertEqual(self.request({"bundle_id": "synthetic-heatmap"}, **options)[0], expected)

    def test_hidden_and_missing_bundles_are_rejected_before_source_read(self):
        status, _ = self.request({"bundle_id": "synthetic-heatmap", "expected_version": 0, "hidden": True},
                                 path="/api/library/conversations")
        self.assertEqual(status, 200)
        with mock.patch.object(app.scoped_ai, "_source", side_effect=AssertionError("Invisible source read")):
            self.assertEqual(self.request({"bundle_id": "synthetic-heatmap"})[0], 409)
            self.assertEqual(self.request({"bundle_id": "synthetic-missing"})[0], 404)

    def test_visibility_is_rechecked_after_source_aggregation(self):
        original_source = app.scoped_ai._source

        def hide_after_read(contacts, bundle_id):
            result = original_source(contacts, bundle_id)
            app.library_service().update_conversation({"bundle_id": bundle_id, "expected_version": 0, "hidden": True})
            return result

        with mock.patch.object(app.scoped_ai, "_source", side_effect=hide_after_read):
            status, response = self.request({"bundle_id": "synthetic-heatmap"})
        self.assertEqual(status, 409)
        self.assertNotIn("heatmap", response)

    def test_invalid_identifiers_and_unknown_fields_fail_without_reflection(self):
        bodies = [{}, {"bundle_id": None}, {"bundle_id": 4}, {"bundle_id": "../SYNTHETIC_PATH"},
                  {"bundle_id": "SYNTHETIC\\PATH"}, {"bundle_id": ".."},
                  {"bundle_id": "synthetic-heatmap", "path": "SYNTHETIC_INJECTED"},
                  {"bundle_id": "synthetic-heatmap", "content": "SYNTHETIC_INJECTED"}]
        with mock.patch.object(app.scoped_ai, "_source", side_effect=AssertionError("Invalid source read")):
            for body in bodies:
                with self.subTest(body=body):
                    status, response = self.request(body)
                    self.assertEqual(status, 400)
                    self.assertNotIn("SYNTHETIC", json.dumps(response))

    def test_invalid_ranges_are_rejected_without_expanding_selection(self):
        scopes = [{"date_from": "2024-01-01"}, {"date_to": "2024-01-01"},
                  {"date_from": "", "date_to": ""},
                  {"date_from": "2024-01-02", "date_to": "2024-01-01"},
                  {"date_from": "2024-01-01", "date_to": "2025-01-01"},
                  {"date_from": "1999-12-31", "date_to": "2000-01-01"},
                  {"date_from": "2100-12-31", "date_to": "2101-01-01"},
                  {"date_from": "SYNTHETIC_DATE", "date_to": "2024-01-01"}]
        for scope in scopes:
            with self.subTest(scope=scope):
                status, response = self.request({"bundle_id": "synthetic-heatmap", **scope})
                self.assertEqual(status, 400)
                self.assertNotIn("SYNTHETIC", json.dumps(response))

    def test_malformed_json_and_nondict_requests_fail_with_fixed_errors(self):
        for raw in (b'{"SYNTHETIC_BAD_JSON"', b"[]", b"null", b'"SYNTHETIC_STRING"'):
            with self.subTest(raw=raw):
                status, response = self.request(raw=raw)
                self.assertEqual(status, 400)
                self.assertNotIn("SYNTHETIC", json.dumps(response))

    def test_query_parameters_are_rejected_before_source_read(self):
        with mock.patch.object(app.scoped_ai, "_source", wraps=app.scoped_ai._source) as source:
            status, response = self.request({"bundle_id": "synthetic-heatmap"},
                                             path="/api/activity/heatmap?extra=SYNTHETIC_QUERY")
        self.assertEqual(status, 400)
        source.assert_not_called()
        self.assertNotIn("SYNTHETIC", json.dumps(response))

    def test_source_limit_is_rejected_without_disclosing_content(self):
        with mock.patch.object(app.scoped_ai, "MAX_FILE_BYTES", 1):
            status, response = self.request({"bundle_id": "synthetic-heatmap"})
        self.assertEqual(status, 400)
        self.assertNotIn("SYNTHETIC", json.dumps(response))

    def test_malformed_source_fails_with_fixed_error(self):
        for raw in (b'{"SYNTHETIC_BAD_SOURCE"', b'\xffSYNTHETIC_BAD_UTF8',
                    b'{"messages":"SYNTHETIC_INVALID_SHAPE"}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.original = raw
                status, response = self.request({"bundle_id": "synthetic-heatmap"})
                self.assertEqual(status, 400)
                self.assertNotIn("SYNTHETIC", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
