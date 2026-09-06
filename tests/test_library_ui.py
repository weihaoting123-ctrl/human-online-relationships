"""Opt-in browser checks: static UI and invented metadata, never production data.

The ephemeral loopback server only serves dashboard/static. Every API response is
a synthetic fixture; the actual frontend performs navigation and editing.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Opt-in synthetic browser tests')
class LibraryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
            QuietStaticHandler, directory=str(ROOT / 'dashboard' / 'static')))
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

    def setUp(self):
        from playwright.sync_api import expect
        self.expect = expect
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(4000)
        # CI cold navigation can exceed the short interaction timeout. Keep
        # interactions strict, but allow normal browser startup/page loading.
        self.page.set_default_navigation_timeout(30000)
        self.errors, self.requests, self.mutations = [], [], []
        self.no_ai = True
        self.conflict = False
        self.hold_state = False
        self.pending_state = None
        self.hold_module_write = False
        self.pending_module_write = None
        self.hold_module_read = False
        self.pending_module_read = None
        self.tags = [{'id': 'tag-1', 'name': '同学', 'color': '#365ecb', 'deleted': False, 'version': 1}]
        self.conversations = []
        for index, name in enumerate(('合成青禾', '合成云舟')):
            source = {'id': f'fixture-{index}', 'contact': name, 'source': 'synthetic',
                      'message_count': 4, 'conversation_kind': 'direct', 'primary_topic': '生活与社交',
                      'date_range': ['2026-01-01', '2026-09-01'], 'has_stats': True}
            self.conversations.append({'bundle_id': source['id'], 'alias': '', 'note': '',
                                       'pinned': False, 'hidden_at': None, 'version': 1,
                                       'tags': [], 'source': source, 'source_missing': False})
        self.modules = [{'id': name, 'label': label, 'description': f'{label}说明', 'enabled': True,
                         'requires': ['media'] if name == 'voice' else [], 'version': 1}
                        for name, label in [('analysis', 'AI 分析'), ('media', '图片与附件'),
                                            ('voice', '语音转写'), ('sync', '自动同步'), ('backup', '本地备份')]]
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('request', lambda request: self.requests.append(request.url))
        self.page.route('**/*', self.route_request)
        self.page.goto(self.url, wait_until='networkidle')
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(2)

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertTrue(all(url.startswith(self.url) for url in self.requests))

    def reply(self, route, payload, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(payload, ensure_ascii=False))

    def route_request(self, route):
        request = route.request
        if not request.url.startswith(self.url):
            route.abort()
            return
        path = urlparse(request.url).path
        if path == '/analysis-ui.js' and self.no_ai:
            route.fulfill(status=200, content_type='text/javascript', body='/* optional module absent */')
            return
        if not path.startswith('/api/'):
            route.continue_()
            return
        response = {'status': 'ok'}
        if request.method == 'POST':
            if path == '/api/modules' and self.hold_module_write:
                self.pending_module_write = route
                return
            body = request.post_data_json
            self.mutations.append((path, copy.deepcopy(body)))
            if self.conflict:
                self.reply(route, {'status': 'error', 'error': '资料已在另一窗口更新，请重新载入。'}, 409)
                return
            if path == '/api/library/conversations':
                item = next(item for item in self.conversations if item['bundle_id'] == body['bundle_id'])
                for key in ('alias', 'note', 'pinned'):
                    if key in body:
                        item[key] = body[key]
                if 'tag_ids' in body:
                    item['tags'] = body['tag_ids']
                if 'hidden' in body:
                    item['hidden_at'] = '2026-09-06T00:00:00Z' if body['hidden'] else None
                item['version'] += 1
                response['conversation'] = item
            elif path == '/api/library/tags':
                if body.get('id'):
                    item = next(item for item in self.tags if item['id'] == body['id'])
                    item.update({key: body[key] for key in ('name', 'color', 'deleted') if key in body})
                    item['version'] += 1
                else:
                    item = {'id': f'tag-{len(self.tags) + 1}', 'name': body['name'], 'color': body['color'], 'deleted': False, 'version': 1}
                    self.tags.append(item)
                response['tag'] = item
            elif path == '/api/modules':
                if body['id'] == 'media' and not body['enabled'] and next(item for item in self.modules if item['id'] == 'voice')['enabled']:
                    self.reply(route, {'status': 'error', 'error': '语音转写依赖图片与附件，请先停用语音转写。'}, 409)
                    return
                item = next(item for item in self.modules if item['id'] == body['id'])
                item['enabled'] = body['enabled']
                item['version'] += 1
                response.update(modules=self.modules, health={'status': 'ok'})
        elif path == '/api/library':
            response.update(conversations=self.conversations, tags=self.tags, health={'status': 'ok', 'schema_version': 1})
        elif path == '/api/modules':
            response.update(modules=self.modules, health={'status': 'ok'})
            if self.hold_module_read:
                self.pending_module_read = (route, copy.deepcopy(response))
                return
        elif path == '/api/state':
            response.update(bundles=[{**item['source'], 'library': {key: item[key] for key in ('alias', 'note', 'pinned', 'hidden_at', 'version', 'tags')}}
                                      for item in self.conversations if not item['hidden_at'] and not item['source_missing']],
                            exporter={'ready': True}, sync={})
            if self.hold_state:
                self.pending_state = (route, copy.deepcopy(response))
                return
        elif path.startswith('/api/bundles/'):
            item = next(item for item in self.conversations if item['bundle_id'] == path.split('/')[3])
            response['bundle'] = {**item['source'], 'stats': {}, 'preview': {}, 'reports': [], 'library': item}
        elif path == '/api/backup/status':
            response['backup'] = {'state': 'idle', 'snapshot_count': 0, 'counts': {}}
        elif path == '/api/backup/history':
            response['snapshots'] = []
        elif path == '/api/ai/config':
            response.update(configured=False, has_key=False, provider='openai', model='synthetic-model')
        elif path == '/api/ai/reports':
            response['reports'] = []
        elif path == '/api/ai/jobs':
            response['jobs'] = []
        self.reply(route, response)

    def open_metadata(self):
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.expect(self.page.locator('#manage-conversation')).to_be_visible()
        self.page.locator('#manage-conversation').click()
        self.expect(self.page.locator('#conversation-dialog')).to_be_visible()

    def capture(self, name):
        if os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS') != '1':
            return
        directory = ROOT / 'scripts' / 'tmp'
        directory.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(directory / name), full_page='drawer' not in name)

    def test_core_navigation_works_without_ai_script(self):
        self.page.locator('[data-view="maintenance"]').click()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#catalog')).to_be_hidden()
        self.page.locator('[data-view="settings"]').click()
        self.expect(self.page.locator('#settings')).to_be_visible()
        self.capture('library-settings-desktop.png')
        self.page.locator('[data-view="trash"]').click()
        self.expect(self.page.locator('#trash')).to_be_visible()
        self.page.locator('[data-view="catalog"]').click()
        self.expect(self.page.locator('#catalog')).to_be_visible()
        self.capture('library-catalog-desktop.png')
        self.assertEqual(self.mutations, [])

    def test_alias_note_pin_and_tag_creation_are_local_and_searchable(self):
        self.open_metadata()
        self.page.locator('#conversation-alias').fill('特别的青禾')
        self.page.locator('#conversation-note').fill('<img src=x onerror=alert(1)> 仅供自己查看')
        self.page.locator('#conversation-pinned').check()
        self.page.locator('#conversation-tags input[value="tag-1"]').check()
        self.page.locator('#conversation-save').click()
        self.expect(self.page.locator('#conversation-feedback')).to_contain_text('已保存')
        self.page.locator('#conversation-close').click()
        self.expect(self.page.locator('#catalog-body')).to_contain_text('特别的青禾')
        self.expect(self.page.locator('#catalog-body')).to_contain_text('原名：合成青禾')
        self.assertEqual(self.page.locator('#catalog-body img').count(), 0)
        self.page.locator('#search-input').fill('同学')
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(1)
        path, body = self.mutations[0]
        self.assertEqual(path, '/api/library/conversations')
        self.assertEqual(body, {'bundle_id': 'fixture-0', 'expected_version': 1, 'alias': '特别的青禾',
                                'note': '<img src=x onerror=alert(1)> 仅供自己查看', 'pinned': True, 'tag_ids': ['tag-1']})
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('#tag-name').fill('项目协作')
        self.page.locator('#tag-save').click()
        self.expect(self.page.locator('#tag-list')).to_contain_text('项目协作')
        self.assertEqual(self.mutations[-1][0], '/api/library/tags')
        self.assertNotIn('expected_version', self.mutations[-1][1])

    def test_hide_and_restore_preserve_original_files(self):
        self.open_metadata()
        self.expect(self.page.locator('#conversation-hide-note')).to_contain_text('不会删除')
        self.page.locator('#conversation-hide').click()
        self.expect(self.page.locator('#conversation-dialog')).to_be_hidden()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(1)
        self.page.locator('[data-view="trash"]').click()
        self.page.locator('#trash-search').fill('青禾')
        self.expect(self.page.locator('.trash-row')).to_have_count(1)
        self.page.locator('.trash-restore').click()
        self.expect(self.page.locator('#trash-list')).to_contain_text('没有符合条件')
        self.page.locator('[data-view="catalog"]').click()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(2)
        self.assertEqual([body['hidden'] for _, body in self.mutations], [True, False])
        self.assertEqual([body['expected_version'] for _, body in self.mutations], [1, 2])

    def test_dependency_error_keeps_toggle_and_shows_readable_error(self):
        self.expect(self.page.locator('[data-view="settings"]')).to_be_visible()
        self.page.locator('[data-view="settings"]').click()
        media = self.page.locator('[data-module-toggle="media"]')
        # This request must be rejected and immediately rolled back to checked.
        # uncheck() asserts an unchecked final state, racing the correct rollback.
        self.expect(media).to_be_checked()
        media.click()
        self.expect(self.page.locator('#module-feedback')).to_contain_text('请先停用语音转写')
        self.expect(media).to_be_checked()
        self.page.locator('[data-module-toggle="voice"]').uncheck()
        media.uncheck()
        self.expect(media).not_to_be_checked()
        self.expect(self.page.locator('.nav-item[data-module-entry="media"]')).to_be_hidden()
        self.expect(self.page.locator('[data-view="catalog"]')).to_be_visible()

    def test_conflict_preserves_unsaved_fields_and_can_reload(self):
        self.open_metadata()
        self.page.locator('#conversation-alias').fill('尚未保存')
        self.conflict = True
        self.page.locator('#conversation-save').click()
        self.expect(self.page.locator('#conversation-feedback')).to_contain_text('另一窗口更新')
        self.expect(self.page.locator('#conversation-alias')).to_have_value('尚未保存')
        self.expect(self.page.locator('#conversation-reload')).to_be_visible()
        self.page.locator('#conversation-reload').click()
        self.expect(self.page.locator('#conversation-alias')).to_have_value('')

    def test_disabled_analysis_keeps_history_and_blocks_new_preview(self):
        self.no_ai = False
        next(item for item in self.modules if item['id'] == 'analysis')['enabled'] = False
        self.page.reload(wait_until='networkidle')
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('#settings a[href="#analysis-workspace"]').click()
        self.expect(self.page.locator('#analysis-workspace')).to_be_visible()
        self.expect(self.page.locator('#analysis-module-stopped')).to_be_visible()
        self.expect(self.page.locator('#ai-bundle')).to_be_disabled()
        self.expect(self.page.locator('.analysis-history')).to_be_visible()
        self.expect(self.page.locator('#ai-settings')).to_be_visible()
        self.page.locator('#ai-scope-form').evaluate('(form) => form.dispatchEvent(new Event("submit", {cancelable:true, bubbles:true}))')
        self.assertEqual(self.mutations, [])

    def test_disabled_backup_and_sync_block_new_operations(self):
        for item in self.modules:
            if item['id'] in ('backup', 'sync'):
                item['enabled'] = False
        self.page.reload(wait_until='networkidle')
        self.page.locator('[data-view="maintenance"]').click()
        self.expect(self.page.locator('#backup-run')).to_be_disabled()
        self.expect(self.page.locator('#backup-panel')).to_be_visible()
        self.expect(self.page.locator('#import-button')).to_be_disabled()
        self.assertEqual(self.mutations, [])

    def test_tag_edit_soft_delete_and_mobile_layout(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.expect(self.page.locator('[data-view="settings"]')).to_be_visible()
        self.page.locator('[data-view="settings"]').click()
        self.capture('library-settings-mobile.png')
        self.page.locator('.tag-edit').first.click()
        self.page.locator('#tag-name').fill('老同学')
        self.page.locator('#tag-save').click()
        self.expect(self.page.locator('#tag-list')).to_contain_text('老同学')
        self.page.locator('.tag-delete').first.click()
        self.expect(self.page.locator('#tag-list')).not_to_contain_text('老同学')
        self.assertEqual(self.mutations[0][1]['expected_version'], 1)
        self.assertEqual(self.mutations[1][1], {'id': 'tag-1', 'expected_version': 2, 'deleted': True})
        for view in ('settings', 'trash', 'maintenance', 'catalog'):
            self.page.locator(f'[data-view="{view}"]').click()
            widths = self.page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
            self.assertLessEqual(widths['document'], widths['viewport'], (view, widths))
        self.open_metadata()
        self.expect(self.page.locator('#conversation-alias')).to_be_enabled()
        self.capture('library-drawer-mobile.png')
        bounds = self.page.locator('#conversation-dialog').bounding_box()
        self.assertGreaterEqual(bounds['x'], 0)
        self.assertLessEqual(bounds['x'] + bounds['width'], 390)
        self.page.keyboard.press('Escape')
        self.expect(self.page.locator('#conversation-dialog')).to_be_hidden()
        self.expect(self.page.locator('#manage-conversation')).to_be_focused()

    def test_deleted_tag_can_be_restored_with_current_version(self):
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('.tag-delete').first.click()
        self.expect(self.page.locator('#tag-deleted')).to_be_visible()
        self.page.locator('#tag-deleted summary').click()
        self.page.locator('.tag-restore').first.click()
        self.expect(self.page.locator('#tag-list')).to_contain_text('同学')
        self.assertEqual(self.mutations[-1], ('/api/library/tags', {'id': 'tag-1', 'expected_version': 2, 'deleted': False}))

    def hold_catalog_refresh(self):
        self.hold_state = True
        with self.page.expect_request('**/api/state'):
            self.page.evaluate('() => { loadState(false); }')
        self.page.wait_for_function('state.stateSequence > 1')

    def release_catalog_refresh(self):
        self.assertIsNotNone(self.pending_state)
        route, response = self.pending_state
        with self.page.expect_response('**/api/state'):
            self.reply(route, response)
        self.hold_state = False
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')

    def test_late_state_response_cannot_restore_hidden_conversation(self):
        self.open_metadata()
        self.hold_catalog_refresh()
        self.page.locator('#conversation-hide').click()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(1)
        self.release_catalog_refresh()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(1)
        self.expect(self.page.locator('#catalog-body')).not_to_contain_text('合成青禾')

    def test_late_state_response_cannot_overwrite_saved_alias(self):
        self.open_metadata()
        self.hold_catalog_refresh()
        self.page.locator('#conversation-alias').fill('刚保存的青禾')
        self.page.locator('#conversation-save').click()
        self.expect(self.page.locator('#conversation-feedback')).to_contain_text('已保存')
        self.release_catalog_refresh()
        self.page.locator('#conversation-close').click()
        self.expect(self.page.locator('#catalog-body')).to_contain_text('刚保存的青禾')

    def test_note_edit_preserves_soft_deleted_tag_assignments(self):
        self.conversations[0]['tags'] = ['tag-1']
        self.page.reload(wait_until='networkidle')
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('.tag-delete').first.click()
        self.expect(self.page.locator('#tag-deleted')).to_be_visible()
        self.page.locator('[data-view="catalog"]').click()
        self.open_metadata()
        self.page.locator('#conversation-note').fill('只修改私人备注')
        self.page.locator('#conversation-save').click()
        self.expect(self.page.locator('#conversation-feedback')).to_contain_text('已保存')
        self.assertNotIn('tag_ids', self.mutations[-1][1])
        self.page.locator('#conversation-close').click()
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('#tag-deleted summary').click()
        self.page.locator('.tag-restore').click()
        self.page.locator('[data-view="catalog"]').click()
        self.page.locator('#search-input').fill('同学')
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(1)

    def test_skip_link_preserves_current_workspace(self):
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('.skip-link').focus()
        self.page.keyboard.press('Enter')
        self.expect(self.page.locator('#settings')).to_be_visible()
        self.expect(self.page.locator('#main')).to_be_focused()

    def test_module_reenable_uses_completed_operation_state(self):
        self.page.evaluate('() => setBusy(document.querySelector("#import-button"), true, "正在导入…")')
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('[data-module-toggle="sync"]').uncheck()
        self.expect(self.page.locator('#module-feedback')).to_contain_text('已停用')
        self.page.evaluate('() => setBusy(document.querySelector("#import-button"), false)')
        self.page.locator('[data-module-toggle="sync"]').check()
        self.expect(self.page.locator('#module-feedback')).to_contain_text('已启用')
        self.expect(self.page.locator('#import-button')).to_be_enabled()

    def test_settings_refresh_during_module_write_cannot_restore_old_toggle(self):
        self.page.locator('[data-view="settings"]').click()
        self.expect(self.page.locator('[data-module-toggle="voice"]')).to_be_checked()
        self.hold_module_write = True
        with self.page.expect_request(lambda request: request.url.endswith('/api/modules') and request.method == 'POST'):
            self.page.locator('[data-module-toggle="voice"]').uncheck()
        self.hold_module_read = True
        with self.page.expect_request(lambda request: request.url.endswith('/api/modules') and request.method == 'GET'):
            self.page.locator('#settings-refresh').click()
        self.assertIsNotNone(self.pending_module_write)
        self.assertIsNotNone(self.pending_module_read)
        self.hold_module_write = False
        self.route_request(self.pending_module_write)
        self.expect(self.page.locator('#module-feedback')).to_contain_text('已停用')
        route, payload = self.pending_module_read
        with self.page.expect_response('**/api/modules'):
            self.reply(route, payload)
        self.hold_module_read = False
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        self.expect(self.page.locator('[data-module-toggle="voice"]')).not_to_be_checked()

    def test_module_switches_align_beside_labels_on_desktop_and_mobile(self):
        self.page.locator('[data-view="settings"]').click()
        for width in (1440, 390):
            self.page.set_viewport_size({'width': width, 'height': 900})
            bounds = self.page.locator('.module-row').evaluate_all('''rows => rows.map(row => ({
                toggle: row.querySelector('input').getBoundingClientRect().toJSON(),
                description: row.querySelector('.module-description').getBoundingClientRect().toJSON()
            }))''')
            self.assertEqual(len(bounds), 5)
            for item in bounds:
                self.assertGreaterEqual(item['toggle']['x'], item['description']['right'], (width, item))
                self.assertLessEqual(item['toggle']['width'], 48, (width, item))


if __name__ == '__main__':
    unittest.main(verbosity=2)
