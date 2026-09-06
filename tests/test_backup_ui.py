"""Opt-in browser checks of backup controls, with invented aggregate responses only."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from dashboard import app
import test_dashboard_ui as fixtures


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Opt-in synthetic browser tests')
class BackupBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        fixtures.ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='backup-ui-', dir=fixtures.ARTIFACTS)
        cls.data = Path(cls.temp.name)
        cls.contacts = cls.data / 'contacts'
        fixtures.make_bundle(cls.contacts, 0, '合成备份测试会话', 4)
        cls.patches = [
            mock.patch.object(app, 'DATA_DIR', cls.data),
            mock.patch.object(app, 'CONTACTS_DIR', cls.contacts),
            mock.patch.object(app, 'RAW_IMPORT_DIR', cls.data / 'raw'),
            mock.patch.object(app, 'SYNC_STATUS_PATH', cls.data / 'sync-status.json'),
            mock.patch.object(app, 'exporter_status', return_value={'status': 'ready', 'ready': True}),
        ]
        for patch in cls.patches:
            patch.start()
        cls.server = app.create_server(port=0, token='synthetic-backup-test-token')
        assert cls.server.server_port != 8765
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/'
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.browser = cls.playwright.chromium.launch(headless=True, channel='msedge')

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
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1024}, reduced_motion='reduce')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors, self.requests, self.mutations = [], [], []
        self.fail_history = False
        self.post_state = 'running'
        self.backup = {
            'schema_version': 1, 'state': 'completed', 'mode': 'backup',
            'last_success_at': '2026-09-05T12:00:00+00:00', 'snapshot_count': 2,
            'verification_complete': True,
            'counts': {'files': 1536, 'bytes': 2684354560, 'objects_copied': 36,
                       'bytes_copied': 4194304, 'unchanged': 1500, 'skipped': 2,
                       'verified': 1536, 'categories': {'text': 86, 'voice': 350, 'images': 1100, 'other': 0}},
            'path': 'Z:/synthetic-private-path', 'error_code': '<img src=x onerror=alert(1)>',
        }
        self.history = [
            {'snapshot_id': 'synthetic-id-not-for-display', 'created_at': '2026-09-05T12:00:00+00:00',
             'files': 1536, 'bytes': 2684354560, 'categories': self.backup['counts']['categories'],
             'private_content': 'synthetic-private-text'},
            {'created_at': '2026-09-04T12:00:00+00:00', 'files': 1500, 'bytes': 2680160256,
             'categories': {'text': 80, 'voice': 320, 'images': 1100, 'other': 0}},
        ]
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('request', lambda request: self.requests.append(request.url))
        self.page.route('**/*', self.route_request)
        self.page.goto(self.url, wait_until='networkidle')
        self.page.locator('[data-view="maintenance"]').click()
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('备份完成')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertTrue(all(url.startswith(self.url) for url in self.requests))

    def route_request(self, route):
        request = route.request
        if not request.url.startswith(self.url):
            route.abort()
            return
        path = urlparse(request.url).path
        if not path.startswith('/api/backup/'):
            route.continue_()
            return
        if request.method == 'POST':
            self.mutations.append((path, request.post_data_json))
            self.backup.update(state=self.post_state, mode='verify' if path.endswith('/verify') else 'backup')
            response = {'status': 'ok', 'state': 'running'}
        elif path.endswith('/status'):
            response = {'status': 'ok', 'backup': self.backup}
        else:
            if self.fail_history:
                route.fulfill(status=200, content_type='application/json', body='{"status":"error"}')
                return
            response = {'status': 'ok', 'snapshots': self.history}
        route.fulfill(status=202 if request.method == 'POST' else 200, content_type='application/json',
                      body=json.dumps(response, ensure_ascii=False))

    def refresh(self):
        self.page.locator('#backup-refresh').click()
        self.expect(self.page.locator('#backup-refresh')).to_be_enabled()

    def test_aggregate_history_stays_local_and_fits_desktop_and_mobile(self):
        self.expect(self.page.locator('#backup-text-count')).to_have_text('86 个')
        self.expect(self.page.locator('#backup-image-count')).to_have_text('1,100 个')
        self.expect(self.page.locator('#backup-bytes')).to_have_text('2.5 GiB')
        self.expect(self.page.locator('#backup-changes')).to_contain_text('未变化 1,500 个，跳过 2 个')
        self.page.locator('.backup-history summary').click()
        self.expect(self.page.locator('.backup-history-row')).to_have_count(2)
        text = self.page.locator('#backup-panel').inner_text()
        for secret in ('synthetic-private', 'synthetic-id', '<img'):
            self.assertNotIn(secret, text)
        self.assertEqual(self.page.locator('#backup-panel img').count(), 0)
        self.assertEqual(self.mutations, [])
        self.capture('backup-desktop.png')
        self.page.set_viewport_size({'width': 375, 'height': 812})
        widths = self.page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
        self.assertLessEqual(widths['document'], widths['viewport'], widths)
        self.capture('backup-mobile.png')

    def test_manual_backup_and_verify_disable_duplicates_and_poll_without_reposting(self):
        self.page.locator('#backup-run').click()
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('正在备份')
        self.expect(self.page.locator('#backup-run')).to_be_disabled()
        self.expect(self.page.locator('#backup-verify')).to_be_disabled()
        self.backup['state'] = 'completed'
        self.expect(self.page.locator('#backup-run')).to_be_enabled(timeout=10000)
        self.assertEqual(self.mutations, [('/api/backup/run', {})])
        self.page.locator('#backup-verify').click()
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('正在校验')
        self.expect(self.page.locator('#backup-run')).to_be_disabled()
        self.backup['state'] = 'completed'
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('校验完成', timeout=10000)
        self.expect(self.page.locator('#backup-verification')).to_contain_text('1,536 个独立内容副本')
        self.assertEqual(self.mutations, [('/api/backup/run', {}), ('/api/backup/verify', {})])

    def test_partial_verification_does_not_claim_whole_snapshot_is_verified(self):
        self.backup.update(state='completed', mode='verify', verification_complete=False)
        self.backup['counts']['verified'] = 40
        self.refresh()
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('局部校验完成')
        self.expect(self.page.locator('#backup-status-badge.is-ready')).to_have_count(0)
        self.expect(self.page.locator('#backup-verification')).to_contain_text('40 个独立内容副本')
        self.expect(self.page.locator('#backup-verification')).to_contain_text('尚未完成整个快照的校验')
        self.expect(self.page.locator('#backup-verify')).to_be_enabled()
        self.assertEqual(self.mutations, [])

    def test_accepted_backup_response_loss_only_reconciles_without_reposting(self):
        self.page.evaluate('''() => {
          const originalFetch = window.fetch;
          window.fetch = async (url, options) => {
            const response = await originalFetch(url, options);
            if (String(url) === '/api/backup/run') {
              throw new TypeError('synthetic response lost after acceptance');
            }
            return response;
          };
        }''')
        before = len(self.requests)
        self.page.locator('#backup-run').click()
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('正在备份')
        self.expect(self.page.locator('#backup-feedback')).to_contain_text('没有自动重新提交')
        self.expect(self.page.locator('#backup-run')).to_be_disabled()
        self.assertEqual(self.mutations, [('/api/backup/run', {})])
        for path in ('/api/backup/status', '/api/backup/history'):
            self.assertTrue(any(url.endswith(path) for url in self.requests[before:]))
        self.backup['state'] = 'completed'
        self.expect(self.page.locator('#backup-run')).to_be_enabled(timeout=10000)
        self.assertEqual(self.mutations, [('/api/backup/run', {})])

    def test_writer_lock_busy_allows_manual_retry_without_automatic_resubmission(self):
        self.post_state = 'busy'
        self.page.locator('#backup-run').click()
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('其他操作占用')
        self.expect(self.page.locator('#backup-summary')).to_have_text('其他本机操作占用，请稍后重试。')
        self.expect(self.page.locator('#backup-feedback')).to_contain_text('没有开始，也不会自动重新提交')
        self.expect(self.page.locator('#backup-run')).to_be_enabled()
        self.expect(self.page.locator('#backup-verify')).to_be_enabled()
        self.expect(self.page.locator('#backup-panel')).to_have_attribute('aria-busy', 'false')
        self.refresh()
        self.page.reload(wait_until='networkidle')
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('其他操作占用')
        self.expect(self.page.locator('#backup-run')).to_be_enabled()
        self.assertEqual(self.mutations, [('/api/backup/run', {})])
        self.post_state = 'running'
        self.page.locator('#backup-run').click()
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('正在备份')
        self.expect(self.page.locator('#backup-run')).to_be_disabled()
        self.assertEqual(self.mutations, [('/api/backup/run', {}), ('/api/backup/run', {})])

    def test_empty_backup_disables_verify_and_history_failure_keeps_status(self):
        self.backup.update(state='idle', snapshot_count=0, counts={})
        self.history = []
        self.refresh()
        self.expect(self.page.locator('#backup-verify')).to_be_disabled()
        self.expect(self.page.locator('#backup-run')).to_be_enabled()
        self.expect(self.page.locator('#backup-summary')).to_contain_text('还没有备份快照')
        self.fail_history = True
        self.refresh()
        self.page.locator('.backup-history summary').click()
        self.expect(self.page.locator('#backup-history-list')).to_contain_text('暂时无法读取历史快照')
        self.expect(self.page.locator('#backup-status-badge')).to_have_text('尚未备份')
        self.assertEqual(self.mutations, [])

    def capture(self, name):
        if os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS') != '1':
            return
        self.page.evaluate('() => { document.activeElement?.blur(); window.scrollTo(0, 0); }')
        self.page.screenshot(path=str(fixtures.ARTIFACTS / name), full_page=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
