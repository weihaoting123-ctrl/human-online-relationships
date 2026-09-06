import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from unittest import mock


from dashboard import app


class DashboardServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.contacts = self.root / "contacts"
        self.raw = self.root / "raw"
        self.sync_status = self.root / "private" / "wechat-sync" / "status.json"
        self.patches = [
            mock.patch.object(app, "CONTACTS_DIR", self.contacts),
            mock.patch.object(app, "RAW_IMPORT_DIR", self.raw),
            mock.patch.object(app, "SYNC_STATUS_PATH", self.sync_status),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp_dir.cleanup()

    def test_markdown_import_generates_private_bundle_and_stats(self):
        detail = app.import_payload({
            "kind": "markdown",
            "filename": "聊天.md",
            "contact": "小林",
            "my_name": "我",
            "content": (
                "[2026-08-10 20:10] 我: 今天怎么样\n"
                "[2026-08-10 20:12] 小林: 挺好的，你呢\n"
                "[2026-08-11 08:00] 小林: 早安\n"
            ),
        })
        self.assertEqual(detail["contact"], "小林")
        self.assertEqual(detail["message_count"], 3)
        self.assertTrue(detail["has_stats"])
        self.assertTrue(next(self.raw.iterdir()).is_file())
        self.assertNotIn("messages", detail)
        self.assertNotIn("今天怎么样", json.dumps(detail, ensure_ascii=False))

    def test_analysis_summary_omits_evidence_quotes(self):
        detail = app.import_payload({
            "kind": "markdown",
            "filename": "chat.md",
            "contact": "测试对象",
            "my_name": "我",
            "content": "[2026-08-10 20:10] 我: 你好\n[2026-08-10 20:11] 测试对象: 你好呀\n",
        })
        bundle = app.resolve_bundle(detail["id"])
        (bundle / "analysis.json").write_text(json.dumps({
            "relationship_label": "稳定交流",
            "verdict": "保持观察",
            "danger_warnings": [{
                "type": "测试提醒",
                "severity": "low",
                "evidence": ["不应返回的原话"],
            }],
            "key_findings": [{"quote": "另一条原话"}],
        }, ensure_ascii=False), encoding="utf-8")
        refreshed = app.bundle_detail(detail["id"])
        serialized = json.dumps(refreshed, ensure_ascii=False)
        self.assertIn("稳定交流", serialized)
        self.assertNotIn("不应返回的原话", serialized)
        self.assertNotIn("另一条原话", serialized)

    def test_timeline_keeps_silent_calendar_days(self):
        from datetime import datetime

        first = datetime(2026, 8, 1, 9).timestamp()
        third = datetime(2026, 8, 3, 9).timestamp()
        timeline = app._timeline([
            {"sender": "me", "timestamp": first},
            {"sender": "them", "timestamp": third},
        ])
        self.assertEqual([item["date"] for item in timeline["daily"]], [
            "2026-08-01", "2026-08-02", "2026-08-03",
        ])
        self.assertEqual(timeline["daily"][1], {"date": "2026-08-02", "me": 0, "them": 0})

    def test_reimport_same_contact_creates_a_new_version(self):
        request = {
            "kind": "markdown",
            "filename": "chat.md",
            "contact": "同名对象",
            "my_name": "我",
            "content": "[2026-08-10 20:10] 我: 第一版\n[2026-08-10 20:11] 同名对象: 收到\n",
        }
        first = app.import_payload(request)
        first_bundle = app.resolve_bundle(first["id"])
        (first_bundle / "analysis.json").write_text('{"verdict":"旧结论"}', encoding="utf-8")
        request["content"] = "[2026-08-11 20:10] 我: 第二版\n[2026-08-11 20:11] 同名对象: 收到\n"
        second = app.import_payload(request)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue((first_bundle / "analysis.json").is_file())
        self.assertFalse((app.resolve_bundle(second["id"]) / "analysis.json").exists())

    def test_concurrent_exact_imports_are_idempotent_without_temp_artifacts(self):
        request = {
            "kind": "markdown",
            "filename": "chat.md",
            "contact": "并发对象",
            "my_name": "我",
            "content": "[2026-08-10 20:10] 我: 你好\n[2026-08-10 20:11] 并发对象: 收到\n",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: app.import_payload(request), range(2)))
        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertEqual(len(list(self.raw.iterdir())), 1)
        self.assertFalse(any(path.name.startswith(".") for path in self.contacts.iterdir()))
        self.assertFalse(any(path.name.startswith(".") for path in self.raw.iterdir()))

    def test_failed_import_leaves_no_raw_or_bundle_half_product(self):
        request = {
            "kind": "markdown",
            "filename": "chat.md",
            "contact": "失败对象",
            "my_name": "我",
            "content": "[2026-08-10 20:10] 我: 你好\n[2026-08-10 20:11] 失败对象: 收到\n",
        }
        with mock.patch.object(app, "run_script", side_effect=RuntimeError("analysis failed")):
            with self.assertRaises(RuntimeError):
                app.import_payload(request)
        self.assertEqual(list(self.contacts.iterdir()), [])
        self.assertEqual(list(self.raw.iterdir()), [])

    def test_import_lock_is_owned_by_the_operating_system(self):
        with app._exclusive_import_lock():
            with self.assertRaises(RuntimeError):
                with app._exclusive_import_lock():
                    pass

    def test_public_stats_remove_raw_short_replies_and_quality_details(self):
        public = app._public_stats({
            "cold_response": {
                "my_cold_count": 1,
                "cold_words": {"嗯": 1},
                "nested": {"quote": "不应出现的原话"},
            },
            "basic": {
                "total_messages": 2,
                "date_range": ["2026-08-01", "2026-08-02"],
                "quote": "聊天正文",
            },
            "active_hours": {"20": 2, "secret-hour": "账号信息"},
            "daily_trend": [
                {"date": "2026-08-01", "count": 2, "text": "消息原文"},
                {"date": "invalid private text", "count": 3},
            ],
            "linguistic": {
                "pronoun_we_count": {"me": 2, "them": 1, "quote": "秘密"},
                "arbitrary": {"text": "不可外泄"},
            },
            "data_quality": {"warnings": ["wxid_secret"]},
        })
        serialized = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("嗯", serialized)
        self.assertNotIn("wxid_secret", serialized)
        for private in ("不应出现", "聊天正文", "账号信息", "消息原文", "秘密", "不可外泄"):
            self.assertNotIn(private, serialized)
        self.assertEqual(public["cold_response"]["my_cold_count"], 1)
        self.assertEqual(public["active_hours"], {"20": 2})
        self.assertEqual(public["daily_trend"], [{"date": "2026-08-01", "count": 2}])

    def test_script_failures_never_forward_child_output(self):
        failed = subprocess.CompletedProcess([], 1, stdout="聊天原话", stderr="token=secret")
        with mock.patch.object(app.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as raised:
                app.run_script("stats_analyzer.py", [])
        self.assertNotIn("聊天原话", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_bundle_path_traversal_is_rejected(self):
        self.contacts.mkdir(parents=True)
        with self.assertRaises(ValueError):
            app.resolve_bundle("../outside")

    def test_server_rejects_non_loopback_binding(self):
        with self.assertRaises(ValueError):
            app.create_server("0.0.0.0", 0)

    def test_sync_status_exposes_only_allowlisted_aggregates(self):
        self.sync_status.parent.mkdir(parents=True)
        self.sync_status.write_text(json.dumps({
            "enabled": True,
            "state": "completed",
            "mode": "scheduled",
            "last_run_at": "2026-09-05T08:30:00+08:00",
            "next_run_at": "2026-09-05T14:30:00+08:00",
            "scanned_conversations": 12,
            "imported_conversations": 8,
            "failed_conversations": 1,
            "imported_messages": 345,
            "deduplicated_messages": 67,
            "unchanged_messages": 900,
            "account_name": "不应出现的账号",
            "session_ids": ["不应出现的会话"],
            "last_error": "不应出现的聊天正文",
        }, ensure_ascii=False), encoding="utf-8")
        status = app.sync_status()
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["imported_messages"], 345)
        self.assertEqual(status["deduplicated_messages"], 67)
        self.assertTrue(status["attention"])
        self.assertNotIn("不应出现", serialized)
        self.assertNotIn("account_name", status)
        self.assertNotIn("session_ids", status)

    def test_sync_status_invalid_values_are_safely_reduced(self):
        self.sync_status.parent.mkdir(parents=True)
        self.sync_status.write_text(json.dumps({
            "enabled": "yes",
            "state": "wxid_secret",
            "mode": "remote",
            "last_run_at": "some private text",
            "imported_messages": -50,
            "deduplicated_messages": "not-a-number",
            "error_code": "token=secret",
        }), encoding="utf-8")
        status = app.sync_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["error_code"], "invalid_status")
        self.assertIsNone(status["mode"])
        self.assertIsNone(status["last_run_at"])
        self.assertEqual(status["imported_messages"], 0)
        self.assertTrue(status["attention"])

    def test_sync_status_allows_safe_restart_wait_state(self):
        self.sync_status.parent.mkdir(parents=True)
        self.sync_status.write_text(json.dumps({
            "enabled": True,
            "state": "awaiting_wechat_restart",
            "mode": "scheduled",
            "attention": "private free text must not pass through",
            "account_id": "private-account",
        }), encoding="utf-8")
        status = app.sync_status()
        self.assertEqual(status["state"], "awaiting_wechat_restart")
        self.assertTrue(status["attention"])
        rendered = json.dumps(status)
        self.assertNotIn("private free text", rendered)
        self.assertNotIn("private-account", rendered)

    def test_corrupt_sync_status_is_an_error_not_not_configured(self):
        self.sync_status.parent.mkdir(parents=True)
        self.sync_status.write_text("{broken json", encoding="utf-8")
        status = app.sync_status()
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["error_code"], "invalid_status")
        self.assertTrue(status["attention"])


class DashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.contacts = Path(self.temp_dir.name) / "contacts"
        self.sync_status = Path(self.temp_dir.name) / "sync-status.json"
        self.patches = [
            mock.patch.object(app, "DATA_DIR", Path(self.temp_dir.name)),
            mock.patch.object(app, "CONTACTS_DIR", self.contacts),
            mock.patch.object(app, "SYNC_STATUS_PATH", self.sync_status),
        ]
        for patch in self.patches:
            patch.start()
        self.server = app.create_server("127.0.0.1", 0, token="test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _bootstrap_session(self):
        response = urllib.request.urlopen(f"{self.base}/", timeout=10)
        cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        return response.read().decode("utf-8"), cookie, response.headers["Set-Cookie"]

    def test_exports_require_session_block_private_json_and_traversal(self):
        root=Path(self.temp_dir.name)/'exports'/'classification'
        root.mkdir(parents=True)
        (root/'index.html').write_text('<h1>fixture</h1>')
        (root/'private.json').write_text('{"secret":"fixture"}')
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.base+'/exports/classification/index.html')
        self.assertEqual(denied.exception.code,403)
        _,cookie,_=self._bootstrap_session()
        response=urllib.request.urlopen(urllib.request.Request(self.base+'/exports/classification/index.html',headers={'Cookie':cookie}))
        self.assertIn("script-src 'none'",response.headers['Content-Security-Policy'])
        for path in ('/exports/classification/private.json','/exports/../sync-status.json'):
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(urllib.request.Request(self.base+path,headers={'Cookie':cookie}))
            self.assertEqual(missing.exception.code,404)

    def test_archive_summary_drops_user_controlled_fields(self):
        (Path(self.temp_dir.name)/'archive-status.json').write_text(json.dumps({'state':'completed','archived_files':3,'account':'private-user','categories':{'private-message':5}}))
        payload=app.archive_status()
        self.assertEqual(payload['media']['archived_files'],3)
        self.assertNotIn('private',json.dumps(payload))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        for patch in reversed(self.patches):
            patch.stop()
        self.temp_dir.cleanup()

    def test_api_requires_local_token(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base}/api/state", timeout=10)
        self.assertEqual(raised.exception.code, 403)

    def test_api_state_does_not_expose_repository_path(self):
        _html, cookie, _set_cookie = self._bootstrap_session()
        request = urllib.request.Request(
            f"{self.base}/api/state",
            headers={"Cookie": cookie},
        )
        payload = urllib.request.urlopen(request, timeout=10).read().decode("utf-8")
        self.assertNotIn("repository", payload)
        self.assertNotIn(str(app.REPO_ROOT), payload)

    def test_index_uses_httponly_session_without_exposing_api_token(self):
        html, _cookie, set_cookie = self._bootstrap_session()
        self.assertNotIn("test-secret", html)
        self.assertNotIn("__LOCAL_API_TOKEN__", html)
        self.assertNotIn("https://", html)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)

    def test_server_side_session_expiry_is_enforced(self):
        value = self.server.issue_session()
        self.server._sessions[value] = 0
        self.assertFalse(self.server.valid_session(value))

    def test_host_header_must_be_loopback_at_the_configured_port(self):
        request = urllib.request.Request(
            f"{self.base}/",
            headers={"Host": f"attacker.invalid:{self.server.server_port}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(raised.exception.code, HTTPStatus.MISDIRECTED_REQUEST)

        wrong_port = urllib.request.Request(
            f"{self.base}/",
            headers={"Host": "127.0.0.1:8765"},
        )
        if self.server.server_port != 8765:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(wrong_port, timeout=10)
            self.assertEqual(raised.exception.code, HTTPStatus.MISDIRECTED_REQUEST)

    def test_write_api_requires_exact_local_origin(self):
        headers = {
            "X-Local-Token": "test-secret",
            "Content-Type": "application/json",
        }
        for origin in (None, "http://attacker.invalid"):
            request_headers = dict(headers)
            if origin:
                request_headers["Origin"] = origin
            request = urllib.request.Request(
                f"{self.base}/api/import",
                data=b"{}",
                headers=request_headers,
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(raised.exception.code, 403)

        local = urllib.request.Request(
            f"{self.base}/api/import",
            data=b"{}",
            headers={**headers, "Origin": self.base},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(local, timeout=10)
        self.assertEqual(raised.exception.code, 400)

    def test_report_route_forbids_scripts_and_network_connections(self):
        bundle = self.contacts / "demo__12345678"
        report_dir = bundle / "reports"
        report_dir.mkdir(parents=True)
        (bundle / "messages.json").write_text('{"messages": []}', encoding="utf-8")
        (report_dir / "report.html").write_text("<style>body{color:black}</style><p>ok</p>", encoding="utf-8")
        report_url = app._report_files(bundle)[0]["url"]
        self.assertNotIn("demo", report_url)
        self.assertNotIn("report.html", report_url)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base}{report_url}?token=test-secret", timeout=10)
        self.assertEqual(raised.exception.code, 403)
        _html, cookie, _set_cookie = self._bootstrap_session()
        request = urllib.request.Request(f"{self.base}{report_url}", headers={"Cookie": cookie})
        response = urllib.request.urlopen(request, timeout=10)
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("script-src 'none'", csp)
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("style-src 'unsafe-inline'", csp)

    def test_frontend_uses_csp_safe_svg_graphics_and_responsive_catalog(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css = (app.STATIC_DIR / "app.css").read_text(encoding="utf-8")
        self.assertNotIn(".style.", javascript)
        self.assertIn('id="catalog-search"', html)
        self.assertIn('id="catalog-body"', html)
        self.assertIn('id="page-size"', html)
        self.assertIn('id="sync-badge"', html)
        self.assertIn("/api/sync-status", javascript)
        self.assertIn("部分完成", javascript)
        self.assertIn("同步状态已失联", javascript)
        self.assertNotIn("X-Local-Token", javascript)
        self.assertNotIn("Git 已忽略", javascript)
        self.assertNotIn("data/contacts", javascript)
        self.assertNotIn("Git 已忽略", html)
        self.assertNotIn("__LOCAL_API_TOKEN__", html)
        self.assertIn("@media (max-width: 560px)", css)
        self.assertIn(".table-scroll { overflow-x: auto;", css)
        self.assertIn(".search-form { flex-wrap: wrap", css)

    def test_sync_status_api_never_returns_private_fields(self):
        self.sync_status.write_text(json.dumps({
            "enabled": True,
            "state": "running",
            "mode": "scheduled",
            "imported_messages": 10,
            "account": "wxid_private",
            "session": "room@chatroom",
            "message": "聊天正文",
            "message_body": "另一段聊天正文",
            "free_text": "任意自由文本",
            "internal_error_code": "database_private_detail",
            "attention": "带路径和账号的错误详情",
        }, ensure_ascii=False), encoding="utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/sync-status",
            headers={"X-Local-Token": "test-secret"},
        )
        response = urllib.request.urlopen(request, timeout=10)
        payload = json.loads(response.read().decode("utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["sync"]["state"], "running")
        self.assertNotIn("wxid_private", serialized)
        self.assertNotIn("room@chatroom", serialized)
        self.assertNotIn("聊天正文", serialized)
        self.assertNotIn("自由文本", serialized)
        self.assertNotIn("database_private_detail", serialized)
        self.assertNotIn("错误详情", serialized)


if __name__ == "__main__":
    unittest.main()
