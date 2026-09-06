"""Opt-in browser regression tests, using invented local data only.

Run from the active D-drive project with SHE_LOVE_ME_UI_TESTS=1. The tests
start their own randomly allocated loopback server and never visit port 8765.
No Playwright browser download or user browser profile is needed: a bundled
Chromium is preferred and the installed Edge channel is the offline fallback.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from dashboard import app


UI_ENABLED = os.environ.get("SHE_LOVE_ME_UI_TESTS") == "1"
ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "scripts" / "tmp"
XSS_NAME = '<img src=x onerror=alert(1)>'


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_bundle(contacts, index, contact, count, kind="direct",
                topic="待确认／低信号", first="2026-01-01", last="2026-06-30",
                content="纯虚构的自动化测试文本"):
    bundle = contacts / f"fixture_{index:03d}"
    timestamp = datetime.fromisoformat(first).timestamp()
    end = datetime.fromisoformat(last).timestamp()
    messages = [{"sender": "me" if number % 2 == 0 else "them",
                 "timestamp": timestamp + (end - timestamp) * number / max(1, count - 1),
                 "content": content, "type": "text"} for number in range(count)]
    write_json(bundle / "messages.json", {
        "source": "synthetic-ui-fixture", "contact_display": contact,
        "my_display": "合成测试用户", "messages": messages,
    })
    write_json(bundle / "dashboard_manifest.json", {
        "contact": contact, "source": "synthetic-ui-fixture",
        "message_count": count, "conversation_kind": kind,
        "date_range": [first, last],
    })
    write_json(bundle / "stats.json", {
        "basic": {"total_messages": count, "my_messages": (count + 1) // 2,
                  "their_messages": count // 2, "date_range": [first, last]},
    })
    write_json(bundle / "classification.json", {
        "conversation_kind": kind, "primary_topic": topic,
        "candidate_topics": [topic], "message_count": count,
    })
    return bundle.name


@unittest.skipUnless(UI_ENABLED, "Opt-in: set SHE_LOVE_ME_UI_TESTS=1 for synthetic browser tests")
class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="dashboard-ui-", dir=ARTIFACTS)
        cls.data = Path(cls.temp.name)
        cls.contacts = cls.data / "contacts"
        make_bundle(cls.contacts, 0, "青禾·二条", 2, topic="工作与项目协作",
                    content="合成检索词：星轨")
        make_bundle(cls.contacts, 1, "青禾·十条", 10, topic="工作与项目协作",
                    first="2026-02-01", last="2026-09-01", content="合成检索词：星轨")
        make_bundle(cls.contacts, 2, "青禾·百条", 100, topic="生活与社交",
                    first="2026-07-01", last="2026-07-31")
        make_bundle(cls.contacts, 3, "云舟协作群", 7, kind="group", topic="工作与项目协作",
                    first="2026-08-01", last="2026-09-01", content="合成检索词：星轨")
        make_bundle(cls.contacts, 4, XSS_NAME, 3, kind="unknown")
        for index in range(5, 60):
            make_bundle(cls.contacts, index, f"合成档案{index:03d}", 1)
        write_json(cls.data / "sync-status.json", {
            "enabled": True, "state": "completed", "mode": "scheduled",
            "last_run_at": "2026-09-05T10:00:00+00:00",
            "next_run_at": "2026-09-05T16:00:00+00:00",
            "scanned_conversations": 60, "imported_conversations": 60,
            "imported_messages": 177, "failed_conversations": 0,
        })
        write_json(cls.data / "archive-status.json", {
            "state": "completed", "total_files": 420, "archived_files": 420,
            "archived_bytes": 53_000_000, "failed_files": 0,
            "viewable_images": 320, "unique_viewable_images": 310,
            "encrypted_images": 2, "duplicate_payloads": 10,
        })
        write_json(cls.data / "classification-status.json", {
            "state": "completed", "classified_conversations": 60,
            "classified_messages": 177, "failed_conversations": 0,
        })
        cls.patches = [
            mock.patch.object(app, "DATA_DIR", cls.data),
            mock.patch.object(app, "CONTACTS_DIR", cls.contacts),
            mock.patch.object(app, "RAW_IMPORT_DIR", cls.data / "raw"),
            mock.patch.object(app, "SYNC_STATUS_PATH", cls.data / "sync-status.json"),
            mock.patch.object(app, "exporter_status", return_value={"status": "ready", "ready": True}),
        ]
        for patch in cls.patches:
            patch.start()
        cls.server = app.create_server(port=0, token="synthetic-test-token")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        assert cls.server.server_port != 8765, "Fixture must never use production port"
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.browser = cls.playwright.chromium.launch(headless=True, channel="msedge")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        for patch in reversed(cls.patches):
            patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        from playwright.sync_api import expect

        self.expect = expect
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1024},
                                                reduced_motion="reduce")
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.requests = []
        self.dialogs = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.errors.append(message.text)
                     if message.type == "error" else None)
        self.page.on("request", lambda request: self.requests.append((request.method, request.url)))
        self.page.on("dialog", lambda dialog: (self.dialogs.append(dialog.message), dialog.dismiss()))
        self.page.route("**/api/backup/*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({
                "status": "ok", "backup": {"state": "idle", "snapshot_count": 0}, "snapshots": []})))
        self.page.goto(self.url, wait_until="networkidle")
        self.assertEqual(self.errors, [], "Frontend initialization must not raise browser errors")
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(25)

    def tearDown(self):
        self.assertEqual(self.errors, [], "No browser console or uncaught errors")
        self.assertEqual(self.dialogs, [], "Untrusted display names must not execute")
        self.assertTrue(all(url.startswith(self.url) for _, url in self.requests),
                        "Every frontend request must stay on fixture loopback origin")

    def names(self):
        return self.page.locator("#catalog-body .contact-label strong").all_text_contents()

    def search_names(self, query, count):
        self.page.locator("#search-input").fill(query)
        self.page.locator("#search-submit").click()
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(count)

    def test_cjk_search_numeric_sort_and_literal_xss(self):
        self.search_names("青禾", 3)
        self.page.locator("#sort-select").select_option("messages_desc")
        self.expect(self.page.locator("#catalog-body .open-bundle").first).to_contain_text("青禾·百条")
        self.assertEqual(self.names(), ["青禾·百条", "青禾·十条", "青禾·二条"])
        self.page.locator("#sort-select").select_option("messages_asc")
        self.expect(self.page.locator("#catalog-body .open-bundle").first).to_contain_text("青禾·二条")
        self.search_names("onerror", 1)
        self.expect(self.page.locator("#catalog-body .contact-label strong")).to_have_text(XSS_NAME)
        self.assertEqual(self.page.locator("#catalog-body img").count(), 0)

    def test_kind_topic_date_overlap_and_clear(self):
        self.page.locator("#filter-topic").select_option("工作与项目协作")
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(3)
        self.page.locator("#date-from").fill("2026-08-15")
        self.page.locator("#date-to").fill("2026-08-20")
        self.page.locator("#search-submit").click()
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(2)
        self.page.locator("#filter-kind").select_option("group")
        self.expect(self.page.locator("#catalog-body .contact-label strong")).to_have_text("云舟协作群")
        self.page.locator("#clear-filters").click()
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(25)
        self.expect(self.page.locator("#date-from")).to_have_value("")

    def test_pagination_page_size_and_empty_state(self):
        first_page = self.names()
        self.page.locator("#next-page").click()
        self.expect(self.page.locator("#prev-page")).to_be_enabled()
        self.assertNotEqual(self.names(), first_page)
        self.page.locator("#next-page").click()
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(10)
        self.expect(self.page.locator("#next-page")).to_be_disabled()
        self.page.locator("#page-size").select_option("100")
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(60)
        self.search_names("绝不会出现的合成称呼", 0)
        self.expect(self.page.locator("#catalog-empty")).to_be_visible()

    def test_keyboard_and_selection_only_detail(self):
        self.expect(self.page.locator("#dashboard")).to_be_hidden()
        self.assertFalse(any("/api/bundles/" in url for _, url in self.requests))
        self.page.locator("body").click(position={"x": 10, "y": 10})
        self.page.keyboard.press("/")
        self.expect(self.page.locator("#search-input")).to_be_focused()
        self.page.locator("#search-input").fill("青禾")
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(3)
        self.page.keyboard.press("Escape")
        self.expect(self.page.locator("#search-input")).to_have_value("")
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(25)
        self.search_names("青禾·二条", 1)
        self.page.locator("#catalog-body .open-bundle").click()
        self.expect(self.page.locator("#dashboard")).to_be_visible()
        self.expect(self.page.locator("#case-title")).to_have_text("青禾·二条")
        self.page.keyboard.press("Escape")
        self.expect(self.page.locator("#dashboard")).to_be_hidden()

    def test_content_search_uses_post_and_never_returns_message_text(self):
        self.page.locator("#search-mode").select_option("content")
        self.page.locator("#search-input").fill("星轨")
        with self.page.expect_response(lambda response: "/api/search" in response.url) as response_info:
            self.page.locator("#search-submit").click()
        response = response_info.value
        self.assertEqual(response.status, 200)
        self.assertEqual(response.request.method, "POST")
        self.assertNotIn("星轨", response.url)
        body = response.json()
        self.assertNotIn("合成检索词", json.dumps(body, ensure_ascii=False))
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(3)
        self.assertEqual(set(self.names()), {"青禾·二条", "青禾·十条", "云舟协作群"})
        self.page.locator("#sort-select").select_option("matches_desc")
        self.expect(self.page.locator("#catalog-body .contact-label strong").first).to_have_text("青禾·十条")

    def test_clear_filters_ignores_late_content_search_response(self):
        # Deliberately ignore AbortSignal in this fake transport: sequence
        # protection must also work when an already-received result arrives late.
        self.page.evaluate("""() => {
          const originalFetch = window.fetch;
          window.__pendingSyntheticSearches = [];
          window.fetch = (url, options) => {
            if (String(url) === '/api/search') {
              return new Promise(resolve => {
                window.__pendingSyntheticSearches.push(() => resolve(new Response(
                  JSON.stringify({status: 'ok', matches: [
                    {bundle_id: 'fixture_000', match_count: 2, last_match_date: '2026-06-30'}
                  ], indexed_messages: 177, indexed_conversations: 60, skipped_conversations: 0}),
                  {status: 200, headers: {'Content-Type': 'application/json'}})));
              });
            }
            return originalFetch(url, options);
          };
        }""")
        self.page.locator("#search-mode").select_option("content")
        self.page.locator("#search-input").fill("星轨")
        self.page.locator("#search-submit").click()
        self.page.wait_for_function("window.__pendingSyntheticSearches.length === 1")
        self.page.locator("#clear-filters").click()
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(25)
        self.page.evaluate("async () => { window.__pendingSyntheticSearches[0](); await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); }")
        self.expect(self.page.locator("#catalog-body .catalog-row")).to_have_count(25)
        self.expect(self.page.locator("#search-input")).to_have_value("")

    def test_desktop_and_mobile_no_horizontal_overflow(self):
        self.expect(self.page.locator("#environment-badge")).not_to_contain_text("undefined")
        if os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS') == '1':
            self.page.screenshot(path=str(ARTIFACTS / "dashboard-ui-desktop.png"), full_page=True)
        self.page.set_viewport_size({"width": 375, "height": 812})
        self.page.wait_for_load_state("networkidle")
        if os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS') == '1':
            self.page.screenshot(path=str(ARTIFACTS / "dashboard-ui-mobile.png"), full_page=True)
        widths = self.page.evaluate("({viewport: innerWidth, document: document.documentElement.scrollWidth})")
        self.assertLessEqual(widths["document"], widths["viewport"], widths)
        self.search_names("青禾", 3)
        self.expect(self.page.locator("#catalog-body .open-bundle").first).to_be_visible()


@unittest.skipUnless(UI_ENABLED, 'Opt-in synthetic browser tests')
class ImportEndpointBrowserTests(unittest.TestCase):
    """Real import endpoint in a separate disposable fixture, without cloud APIs."""
    @classmethod
    def setUpClass(cls):
        DashboardBrowserTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        DashboardBrowserTests.tearDownClass()

    def setUp(self):
        self.fixture = DashboardBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.page, self.expect = self.fixture.page, self.fixture.expect

    def tearDown(self):
        self.fixture.tearDown()

    def test_import_view_and_exact_reimport_preserve_existing_archives(self):
        contacts = self.fixture.contacts
        originals = {path: path.read_bytes() for path in contacts.rglob('messages.json')}
        payload = json.dumps({
            'source': 'synthetic-ui-fixture', 'contact_display': '合成导入闭环',
            'my_display': '合成测试用户', 'messages': [
                {'sender': 'me', 'timestamp': 1788000000, 'type': 'text', 'content': '虚构测试甲'},
                {'sender': 'them', 'timestamp': 1788000060, 'type': 'text', 'content': '虚构测试乙'},
            ],
        }, ensure_ascii=False).encode('utf-8')
        imports = []
        for _ in range(2):
            self.page.locator('[data-view="maintenance"]').click()
            self.page.locator('#import-form').evaluate('form => { form.closest("details").open = true; }')
            self.page.locator('#chat-file').set_input_files({
                'name': 'synthetic-import.json', 'mimeType': 'application/json', 'buffer': payload})
            with self.page.expect_response(lambda response: response.url.endswith('/api/import')) as result:
                self.page.locator('#import-button').click()
            response = result.value
            self.assertEqual(response.status, 200)
            body = response.json()
            imports.append(body['bundle']['id'])
            self.expect(self.page.locator('#dashboard')).to_be_visible()
            self.expect(self.page.locator('#case-title')).to_have_text('合成导入闭环')
            self.expect(self.page.locator('#case-title')).to_be_focused()
            self.page.locator('#close-detail').click()
        self.assertEqual(imports[0], imports[1])
        self.assertEqual(len(list(contacts.glob('*/messages.json'))), len(originals) + 1)
        self.assertEqual(len(list((self.fixture.data / 'raw').iterdir())), 1)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content, 'Existing synthetic archive must not change')
        self.assertFalse(any('/api/ai/' in url for _, url in self.fixture.requests))


if __name__ == "__main__":
    unittest.main(verbosity=2)
