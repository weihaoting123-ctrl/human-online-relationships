"""Synthetic backup HTTP and historical partial-report integration checks."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from dashboard import app, analysis
import backup_wechat_local as backup


class BackupApiTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="backup-api-", dir=scratch)
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(app, "DATA_DIR", self.root / "data")
        self.patch.start()
        self.server = app.create_server("127.0.0.1", 0, token="synthetic-local-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.patch.stop()
        self.temp.cleanup()

    def request(self, path, body=None, authorized=True, origin=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["X-Local-Token"] = "synthetic-local-token"
        if origin:
            headers["Origin"] = self.base
        request = urllib.request.Request(self.base + path, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def test_backup_read_and_write_require_local_session(self):
        for path, body in (("/api/backup/status", None), ("/api/backup/history", None),
                           ("/api/backup/run", {}), ("/api/backup/verify", {})):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(path, body, authorized=False)
            self.assertEqual(raised.exception.code, 403)

    def test_backup_posts_require_origin_and_reject_paths(self):
        with mock.patch.object(app, "start_backup") as start:
            for path in ("/api/backup/run", "/api/backup/verify"):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.request(path, {}, origin=False)
                self.assertEqual(raised.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.request(path, {"destination": "C:/untrusted"})
                self.assertEqual(raised.exception.code, 400)
            start.assert_not_called()

    def test_reads_are_local_and_do_not_start_backup(self):
        with mock.patch.object(backup, "run_backup") as run:
            status, result = self.request("/api/backup/status")
            self.assertEqual(status, 200)
            self.assertEqual(result["backup"]["state"], "idle")
            _, history = self.request("/api/backup/history")
            self.assertEqual(history["snapshots"], [])
            run.assert_not_called()
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(app._backup_project_root(), self.root)

    def test_routes_dispatch_only_fixed_operations(self):
        with mock.patch.object(app, "start_backup", return_value={"status": "ok", "state": "running"}) as start:
            self.assertEqual(self.request("/api/backup/run", {})[0], 202)
            start.assert_called_once_with(verify=False)
            start.reset_mock()
            self.assertEqual(self.request("/api/backup/verify", {})[0], 202)
            start.assert_called_once_with(verify=True)

    def test_busy_thread_rejects_double_start(self):
        with mock.patch.object(app, "_BACKUP_ACTIVITY", "backup"), mock.patch.object(backup, "run_backup") as run:
            with self.assertRaises(ValueError):
                app.start_backup()
            self.assertEqual(app.backup_status()["backup"]["state"], "running")
            run.assert_not_called()

    def test_http_worker_completes_synthetic_backup_and_full_verification(self):
        sources = {
            "data/contacts/synthetic/messages.json": b'{"messages":[]}',
            "data/exports/wechat-media/synthetic.png": b"synthetic-image-bytes",
            "data/private/wechat-voice/synthetic.silk": b"synthetic-audio-bytes",
        }
        for relative, content in sources.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for operation in ("run", "verify"):
            self.assertEqual(self.request("/api/backup/" + operation, {})[0], 202)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, result = self.request("/api/backup/status")
                state = result["backup"]
                if state["state"] != "running":
                    break
                time.sleep(0.02)
            self.assertEqual(state["state"], "completed", state)
            # New-operation admission initializes the persistent module-settings
            # database, which is deliberately covered by the v2 backup as well.
            self.assertEqual(state["counts"]["files"], 4)
            if operation == 'run':
                self.assertEqual(state["counts"]["sqlite_snapshots"], 1)
            self.assertEqual(state["snapshot_count"], 1)
        self.assertTrue(state["verification_complete"])
        self.assertEqual(state["counts"]["verified"], 4)
        for relative, content in sources.items():
            self.assertEqual((self.root / relative).read_bytes(), content)
        _, result = self.request("/api/backup/history")
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertNotIn("synthetic-audio", json.dumps(result))


class PartialReportReasonTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        self.temp = tempfile.TemporaryDirectory(prefix="partial-reason-", dir=scratch)
        self.root = Path(self.temp.name)
        self.report_id = "a" * 48
        self.store = analysis._root(self.root)
        self.store.mkdir(parents=True, exist_ok=True)
        self.report_path = self.store / f"report-{self.report_id}.json"
        self.saved = {"id": self.report_id, "coverage": {"complete": False}, "scope": {"synthetic": True}}
        analysis._write(self.report_path, self.saved)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **overrides):
        analysis._write(self.store / ("job-" + "b" * 48 + ".json"), {
            "report_id": self.report_id, "state": "error",
            "error": "分析结果格式不完整；本次不会自动重试",
            "progress": {"stage": "segments", "completed_segments": 22}, **overrides,
        })

    def test_old_partial_report_explains_stop_without_rewriting_or_network(self):
        self.job()
        before = self.report_path.read_bytes()
        with mock.patch.object(analysis, "_cloud") as cloud:
            result = analysis.report(self.root, self.report_id)
            cloud.assert_not_called()
        self.assertIn("不是消息条数上限", result["stop_reason"])
        self.assertEqual(result["failed_segment"], 23)
        self.assertEqual(self.report_path.read_bytes(), before)

    def test_unknown_job_error_is_not_exposed(self):
        self.job(error="private-path-or-key-must-not-appear")
        result = analysis.report(self.root, self.report_id)
        self.assertNotIn("private-path", json.dumps(result))
        self.assertIn("中途停止", result["stop_reason"])

    def test_existing_reason_and_completed_report_are_not_overlaid(self):
        self.job()
        for saved in ({**self.saved, "stop_reason": "已保存原因"},
                      {**self.saved, "coverage": {"complete": True}}):
            analysis._write(self.report_path, saved)
            result = analysis.report(self.root, self.report_id)
            self.assertEqual(result, {"status": "ok", **saved})


if __name__ == "__main__":
    unittest.main()
