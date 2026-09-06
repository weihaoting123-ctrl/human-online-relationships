"""First-use regressions with invented metadata and only loopback transport mocks."""
from __future__ import annotations

import copy
import os
import time
import unittest
from urllib.parse import urlparse

import test_library_ui as library_fixture


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Opt-in synthetic browser tests')
class ImportNavigationBrowserTests(unittest.TestCase):
    # Compose the existing fixture without inheriting (and rerunning) its tests.
    @classmethod
    def setUpClass(cls):
        library_fixture.LibraryBrowserTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        library_fixture.LibraryBrowserTests.tearDownClass.__func__(cls)

    reply = library_fixture.LibraryBrowserTests.reply
    tearDown = library_fixture.LibraryBrowserTests.tearDown

    def setUp(self):
        self.import_routes = []
        self.pending_detail = None
        self.hold_detail = False
        self.state_failure = False
        library_fixture.LibraryBrowserTests.setUp(self)

    def route_request(self, route):
        path = urlparse(route.request.url).path
        if route.request.url.startswith(self.url):
            if path == '/api/import':
                self.mutations.append((path, copy.deepcopy(route.request.post_data_json)))
                self.import_routes.append(route)
                return
            if path == '/api/state' and self.state_failure:
                route.fulfill(status=503, content_type='text/html', body='<html>synthetic unreadable response</html>')
                return
            if path.startswith('/api/bundles/') and self.hold_detail:
                self.pending_detail = route
                return
        library_fixture.LibraryBrowserTests.route_request(self, route)

    def importer(self):
        self.page.locator('[data-view="maintenance"]').click()
        self.page.locator('summary').filter(has_text='导入已有聊天文件').click()

    def select_file(self, name='synthetic.json', content='[]'):
        self.page.locator('#chat-file').set_input_files({'name': name, 'mimeType': 'text/plain', 'buffer': content.encode()})

    def drop_file(self, name):
        self.page.evaluate('''name => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['[]'], name, {type: 'text/plain'}));
          document.querySelector('#drop-zone').dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: transfer}));
        }''', name)

    def submit(self):
        count = len(self.import_routes)
        with self.page.expect_request('**/api/import'):
            self.page.locator('#import-button').click()
        self.wait_route(lambda: len(self.import_routes) > count)

    def wait_route(self, ready):
        deadline = time.monotonic() + 4
        while not ready() and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        self.assertTrue(ready(), 'Synthetic response route was not reached')

    def imported_bundle(self, hidden=False):
        item = copy.deepcopy(self.conversations[0])
        item['bundle_id'] = 'fixture-imported'
        item['source'].update(id=item['bundle_id'], contact='合成导入会话')
        item['hidden_at'] = '2026-09-06T00:00:00Z' if hidden else None
        self.conversations.append(item)
        return {**item['source'], 'stats': {}, 'preview': {}, 'reports': [], 'library': item}

    def confirm_import(self, hidden=False):
        self.reply(self.import_routes[-1], {'status': 'ok', 'bundle': self.imported_bundle(hidden)})

    def wait_finished(self):
        self.expect(self.page.locator('#import-button')).to_be_enabled()

    def test_new_file_selection_resets_source_both_directions_and_empty(self):
        self.importer()
        for select in (self.select_file, self.drop_file):
            select('synthetic.MD')
            self.expect(self.page.locator('#source-kind')).to_have_value('markdown')
            self.page.locator('#source-kind').select_option('ciphertalk')
            select('synthetic.json')
            self.expect(self.page.locator('#source-kind')).to_have_value('auto')
            select('synthetic.TXT')
            self.expect(self.page.locator('#source-kind')).to_have_value('markdown')
        self.page.locator('#chat-file').set_input_files([])
        self.expect(self.page.locator('#source-kind')).to_have_value('auto')
        self.expect(self.page.locator('#file-label')).to_have_text('选择聊天导出文件')

    def test_manual_source_survives_rejection(self):
        self.importer()
        self.select_file('synthetic.md')
        self.page.locator('#source-kind').select_option('normalized-json')
        self.submit()
        self.reply(self.import_routes[-1], {'status': 'error', 'error': '本地处理失败或请求格式无效'}, 400)
        self.wait_finished()
        self.assertEqual(self.mutations[-1][1]['kind'], 'normalized-json')
        self.expect(self.page.locator('#source-kind')).to_have_value('normalized-json')
        self.expect(self.page.locator('#file-label')).to_have_text('synthetic.md')

    def test_file_read_captures_fields_before_await_and_preserves_new_selection(self):
        self.importer()
        self.select_file('synthetic.md', 'original synthetic text')
        self.page.locator('#contact-name').fill('合成原称呼')
        self.page.locator('#my-name').fill('合成原自己')
        self.page.locator('#source-kind').select_option('ciphertalk')
        self.page.evaluate('''() => {
          const read = File.prototype.text;
          File.prototype.text = function() { return new Promise(resolve => { window.releaseFile = () => read.call(this).then(resolve); }); };
        }''')
        self.page.locator('#import-button').click()
        self.page.wait_for_function('typeof window.releaseFile === "function"')
        self.select_file('next.json')
        self.page.locator('#contact-name').fill('合成新称呼')
        self.page.locator('#my-name').fill('合成新自己')
        with self.page.expect_request('**/api/import'):
            self.page.evaluate('window.releaseFile()')
        self.wait_route(lambda: bool(self.import_routes))
        self.assertEqual(self.mutations[-1][1], {'filename': 'synthetic.md', 'content': 'original synthetic text',
                         'kind': 'ciphertalk', 'contact': '合成原称呼', 'my_name': '合成原自己'})
        self.confirm_import()
        self.wait_finished()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#contact-name')).to_have_value('合成新称呼')
        self.expect(self.page.locator('#file-label')).to_have_text('next.json')

    def test_busy_guard_prevents_programmatic_duplicate_submit(self):
        self.importer()
        self.select_file()
        self.submit()
        self.page.evaluate('''() => {
          document.querySelector('#import-form').dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
          document.querySelector('#import-form').dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
        }''')
        self.confirm_import()
        self.wait_finished()
        self.assertEqual(len(self.import_routes), 1)

    def test_confirmed_import_opens_correct_detail_and_clears_submitted_form(self):
        self.importer()
        self.select_file('synthetic.md')
        self.page.locator('#contact-name').fill('合成提交称呼')
        self.submit()
        self.confirm_import()
        self.wait_finished()
        self.expect(self.page.locator('#dashboard')).to_be_visible()
        self.expect(self.page.locator('#case-title')).to_have_text('合成导入会话')
        self.expect(self.page.locator('#case-title')).to_be_focused()
        self.assertEqual(urlparse(self.page.url).fragment, 'catalog')
        self.expect(self.page.locator('#contact-name')).to_have_value('')
        self.expect(self.page.locator('#source-kind')).to_have_value('auto')
        self.expect(self.page.locator('#import-result')).to_contain_text('导入完成')

    def test_confirmed_import_refresh_failure_retains_success_and_reconciles_without_post(self):
        self.importer()
        self.select_file()
        self.submit()
        self.state_failure = True
        self.confirm_import()
        self.wait_finished()
        self.expect(self.page.locator('#import-result')).to_contain_text('导入完成')
        self.expect(self.page.locator('#import-result')).to_contain_text('刷新')
        self.expect(self.page.locator('#import-result-open')).to_be_visible()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#file-label')).to_have_text('synthetic.json')
        self.state_failure = False
        self.page.locator('#import-result-open').click()
        self.expect(self.page.locator('#case-title')).to_have_text('合成导入会话')
        self.expect(self.page.locator('#dashboard')).to_be_visible()
        self.assertEqual(len(self.import_routes), 1)

    def test_waiting_navigation_keeps_view_focus_and_has_persistent_result(self):
        self.importer()
        self.select_file()
        self.submit()
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('#tag-name').fill('合成等待标签')
        self.confirm_import()
        self.wait_finished()
        self.expect(self.page.locator('#settings')).to_be_visible()
        self.expect(self.page.locator('#tag-name')).to_be_focused()
        self.expect(self.page.locator('#file-label')).to_have_text('synthetic.json')
        self.expect(self.page.locator('#import-result-open')).to_be_visible()
        self.page.locator('#import-result-open').click()
        self.expect(self.page.locator('#dashboard')).to_be_visible()
        self.expect(self.page.locator('#case-title')).to_have_text('合成导入会话')

    def test_waiting_detail_selection_does_not_replace_selected_bundle(self):
        self.importer()
        self.select_file()
        self.submit()
        self.page.locator('[data-view="catalog"]').click()
        self.page.locator('#catalog-body .open-bundle').nth(1).click()
        self.expect(self.page.locator('#case-title')).to_have_text('合成云舟')
        self.confirm_import()
        self.wait_finished()
        self.expect(self.page.locator('#case-title')).to_have_text('合成云舟')
        self.expect(self.page.locator('#import-result-open')).to_be_visible()

    def test_field_edit_then_revert_still_prevents_automatic_open_or_clear(self):
        self.importer()
        self.select_file()
        self.submit()
        self.page.locator('#my-name').fill('合成临时')
        self.page.locator('#my-name').fill('我')
        self.confirm_import()
        self.wait_finished()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#file-label')).to_have_text('synthetic.json')
        self.expect(self.page.locator('#import-result-open')).to_be_visible()

    def test_hidden_identical_import_never_opens_or_restores_bundle(self):
        self.importer()
        self.select_file()
        self.submit()
        self.confirm_import(hidden=True)
        self.wait_finished()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#import-result')).to_contain_text('导入完成')
        self.expect(self.page.locator('#import-result')).to_contain_text('回收站')
        self.expect(self.page.locator('#dashboard')).to_be_hidden()
        self.page.locator('#import-result-open').click()
        self.expect(self.page.locator('#dashboard')).to_be_hidden()
        self.assertEqual([path for path, _ in self.mutations], ['/api/import'])

    def test_unknown_import_responses_retain_input_and_ask_to_reconcile(self):
        self.importer()
        for body in ('<html>synthetic parser detail</html>', 'null', '[]', '{}'):
            with self.subTest(body=body):
                self.select_file()
                self.submit()
                self.import_routes[-1].fulfill(status=200, content_type='application/json', body=body)
                self.wait_finished()
                self.expect(self.page.locator('#import-result')).to_contain_text('无法确认')
                self.expect(self.page.locator('#import-result')).to_contain_text('刷新')
                self.expect(self.page.locator('#file-label')).to_have_text('synthetic.json')
                self.assertNotIn('synthetic parser', self.page.locator('#import-result').inner_text())
        self.assertEqual(len(self.import_routes), 4)

    def test_initial_loading_failed_read_and_confirmed_empty_are_distinct(self):
        self.hold_state = True
        self.page.reload(wait_until='domcontentloaded')
        self.expect(self.page.locator('#empty-state')).to_be_hidden()
        self.expect(self.page.locator('#catalog-empty')).to_be_hidden()
        self.expect(self.page.locator('#result-summary')).to_contain_text('正在读取')
        self.page.wait_for_function('document.readyState === "complete"')
        route, _ = self.pending_state
        route.fulfill(status=503, content_type='text/plain', body='synthetic unavailable')
        self.expect(self.page.locator('#result-summary')).to_contain_text('读取失败')
        self.expect(self.page.locator('#empty-state')).to_be_hidden()
        self.hold_state = False
        self.conversations.clear()
        self.page.locator('#refresh-button').click()
        self.expect(self.page.locator('#empty-state')).to_be_visible()
        self.expect(self.page.locator('#catalog-empty')).to_be_hidden()
        self.page.locator('#empty-state a').click()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#chat-file')).to_be_focused()

    def test_no_filter_matches_and_failed_refresh_preserve_last_good_catalog(self):
        self.page.locator('#search-input').fill('合成不存在名称')
        self.expect(self.page.locator('#catalog-empty')).to_be_visible()
        self.expect(self.page.locator('#empty-state')).to_be_hidden()
        self.page.locator('#clear-filters').click()
        self.state_failure = True
        self.page.locator('#refresh-button').click()
        self.expect(self.page.locator('#refresh-button')).to_be_enabled()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(2)
        self.expect(self.page.locator('#result-summary')).to_contain_text('上次')

    def test_user_navigation_pushes_private_free_history_and_back_forward_restore(self):
        self.assertEqual(urlparse(self.page.url).fragment, 'catalog')
        initial_length = self.page.evaluate('history.length')
        for view in ('analysis-workspace', 'maintenance', 'settings', 'trash'):
            self.page.locator(f'[data-view="{view}"]').click()
            self.assertEqual(urlparse(self.page.url).fragment, view)
        self.assertEqual(self.page.evaluate('history.length'), initial_length + 4)
        self.page.go_back()
        self.expect(self.page.locator('#settings')).to_be_visible()
        self.page.go_back()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.page.go_forward()
        self.expect(self.page.locator('#settings')).to_be_visible()
        self.assertIn(self.page.evaluate('JSON.stringify(history.state)'), ('null', '"settings"', '{"view":"settings"}'))
        self.assertEqual(self.page.evaluate('history.length'), initial_length + 4)
        self.page.goto(self.url + '#invalid-private-route', wait_until='networkidle')
        self.expect(self.page).to_have_url(self.url + '#catalog')
        self.assertEqual(urlparse(self.page.url).fragment, 'catalog')
        self.expect(self.page.locator('#catalog')).to_be_visible()

    def test_detail_focus_returns_to_fresh_row_or_search_after_rerender(self):
        self.conversations[0]['source']['message_count'] = 9
        self.conversations[1]['source']['message_count'] = 2
        self.page.evaluate('loadState(false)')
        opened_row = self.page.locator('#catalog-body [data-bundle-id="fixture-0"]')
        opened_row.click()
        self.expect(self.page.locator('#case-title')).to_be_focused()
        self.page.locator('#sort-select').select_option('messages_asc')
        self.expect(self.page.locator('#catalog-body .open-bundle').nth(1)).to_have_attribute('data-bundle-id', 'fixture-0')
        self.page.locator('#close-detail').click()
        self.expect(opened_row).to_be_focused()
        opened_row.click()
        self.expect(self.page.locator('#case-title')).to_be_focused()
        self.page.locator('#search-input').fill('不存在的合成会话')
        self.page.locator('#close-detail').click()
        self.expect(self.page.locator('#search-input')).to_be_focused()

    def test_late_detail_and_background_reload_never_steal_focus(self):
        self.hold_detail = True
        with self.page.expect_request('**/api/bundles/fixture-0'):
            self.page.locator('#catalog-body .open-bundle').first.click()
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('#tag-name').fill('合成焦点')
        self.hold_detail = False
        library_fixture.LibraryBrowserTests.route_request(self, self.pending_detail)
        self.expect(self.page.locator('#tag-name')).to_be_focused()
        self.expect(self.page.locator('#settings')).to_be_visible()
        self.page.locator('[data-view="catalog"]').click()
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.expect(self.page.locator('#case-title')).to_be_focused()
        self.page.locator('#search-input').focus()
        self.page.evaluate('loadState(true)')
        self.expect(self.page.locator('#search-input')).to_be_focused()

    def test_api_sanitizes_transport_errors_and_preserves_business_conflict(self):
        scenarios = [
            ('html', 502, '<html>synthetic internals</html>'),
            ('null', 200, 'null'), ('array', 200, '[]'),
            ('technical', 400, '{"status":"error","error":"TypeError: synthetic internals"}'),
            ('mixed-technical', 400, '{"status":"error","error":"本机响应失败: Unexpected token synthetic"}'),
            ('local-path', 400, '{"status":"error","error":"本机读取失败: C:/synthetic/private.db"}'),
            ('business-term', 400, '{"status":"error","error":"JSON 文件超过 50MB，请按联系人拆分。"}'),
            ('conflict', 409, '{"status":"error","error":"资料已在另一窗口更新，请重新载入。"}'),
        ]
        for label, status, body in scenarios:
            with self.subTest(label=label):
                self.page.route('**/api/synthetic-check', lambda route, _request, s=status, b=body: route.fulfill(status=s, content_type='application/json', body=b))
                result = self.page.evaluate('''async () => {
                  try { await ArchiveCore.api('/api/synthetic-check'); return {accepted: true}; }
                  catch (error) { return {message:error.message, status:error.status, name:error.name}; }
                }''')
                self.assertNotIn('accepted', result)
                self.assertNotIn('synthetic', result['message'])
                self.assertRegex(result['message'], '[\u4e00-\u9fff]')
                self.assertEqual(result['status'], status)
                if label == 'conflict':
                    self.assertIn('请重新载入', result['message'])
                if label == 'business-term':
                    self.assertEqual(result['message'], 'JSON 文件超过 50MB，请按联系人拆分。')
                self.page.unroute('**/api/synthetic-check')
        for name in ('AbortError', 'TypeError', 'TimeoutError'):
            with self.subTest(name=name):
                result = self.page.evaluate('''async name => {
                  const original = window.fetch;
                  window.fetch = async () => { throw new DOMException('synthetic transport detail', name); };
                  try { await ArchiveCore.api('/api/synthetic-check', {method:'POST'}); }
                  catch (error) { return {message:error.message, name:error.name, outcomeUnknown:error.outcomeUnknown}; }
                  finally { window.fetch = original; }
                }''', name)
                self.assertNotIn('synthetic', result['message'])
                self.assertRegex(result['message'], '[\u4e00-\u9fff]')
                self.assertTrue(result['outcomeUnknown'])
                if name == 'AbortError':
                    self.assertEqual(result['name'], 'AbortError')

        response_error = self.page.evaluate('''async () => {
          const original = window.fetch;
          window.fetch = async () => ({status: 409, ok:false, json:async () => { throw new DOMException('synthetic body detail', 'AbortError'); }});
          try { await ArchiveCore.api('/api/synthetic-check', {method:'POST'}); }
          catch (error) { return {message:error.message, name:error.name, status:error.status, outcomeUnknown:error.outcomeUnknown}; }
          finally { window.fetch = original; }
        }''')
        self.assertEqual(response_error['name'], 'AbortError')
        self.assertEqual(response_error['status'], 409)
        self.assertTrue(response_error['outcomeUnknown'])
        self.assertNotIn('synthetic', response_error['message'])

    def test_new_file_during_state_refresh_is_not_cleared_or_opened(self):
        self.importer()
        self.select_file('synthetic.md')
        self.submit()
        self.hold_state = True
        self.confirm_import()
        self.wait_route(lambda: self.pending_state is not None)
        self.drop_file('next.json')
        self.page.locator('#contact-name').fill('合成后续输入')
        route, response = self.pending_state
        self.reply(route, response)
        self.wait_finished()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#file-label')).to_have_text('next.json')
        self.expect(self.page.locator('#contact-name')).to_have_value('合成后续输入')
        self.expect(self.page.locator('#contact-name')).to_be_focused()

    def test_navigation_away_and_back_during_import_does_not_auto_open(self):
        self.importer()
        self.select_file()
        self.submit()
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('[data-view="maintenance"]').click()
        self.confirm_import()
        self.wait_finished()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#file-label')).to_have_text('synthetic.json')

    def test_result_open_refresh_respects_newer_form_edits(self):
        self.importer()
        self.select_file()
        self.submit()
        self.page.locator('#contact-name').fill('合成保留')
        self.confirm_import()
        self.wait_finished()
        self.hold_state = True
        self.page.locator('#import-result-open').click()
        self.wait_route(lambda: self.pending_state is not None)
        self.page.locator('#contact-name').fill('合成正在编辑')
        route, response = self.pending_state
        self.reply(route, response)
        self.expect(self.page.locator('#import-result-open')).to_be_enabled()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#contact-name')).to_be_focused()

    def test_network_import_loss_never_reposts_and_keeps_input(self):
        self.importer()
        self.drop_file('synthetic.txt')
        self.page.locator('#source-kind').select_option('normalized-json')
        self.submit()
        self.import_routes[-1].abort('failed')
        self.wait_finished()
        self.expect(self.page.locator('#import-result')).to_contain_text('无法确认')
        self.expect(self.page.locator('#import-result')).to_contain_text('先刷新')
        self.expect(self.page.locator('#source-kind')).to_have_value('normalized-json')
        self.expect(self.page.locator('#file-label')).to_have_text('synthetic.txt')
        self.assertEqual(self.mutations[-1][1]['kind'], 'normalized-json')
        self.page.locator('[data-view="catalog"]').click()
        self.page.locator('#refresh-button').click()
        self.expect(self.page.locator('#refresh-button')).to_be_enabled()
        self.assertEqual(len(self.import_routes), 1)

    def test_absent_ai_and_disabled_module_preserve_hash_navigation(self):
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('[data-module-toggle="analysis"]').uncheck()
        self.expect(self.page.locator('[data-view="analysis-workspace"]')).to_be_hidden()
        self.page.evaluate("navigateWorkspace('analysis-workspace')")
        self.expect(self.page.locator('#analysis-workspace')).to_be_visible()
        self.page.locator('[data-view="maintenance"]').click()
        self.page.go_back()
        self.expect(self.page.locator('#analysis-workspace')).to_be_visible()
        self.assertEqual(urlparse(self.page.url).fragment, 'analysis-workspace')

    def test_reduced_motion_uses_auto_scroll_for_navigation_and_detail(self):
        self.page.evaluate('''() => {
          window.scrollModes = [];
          const scroll = Element.prototype.scrollIntoView;
          Element.prototype.scrollIntoView = function(options) { window.scrollModes.push(options.behavior); return scroll.call(this, options); };
          const windowScroll = window.scrollTo;
          window.scrollTo = options => { window.scrollModes.push(options.behavior); return windowScroll.call(window, options); };
        }''')
        self.page.locator('[data-view="maintenance"]').click()
        self.page.locator('[data-view="catalog"]').click()
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.expect(self.page.locator('#case-title')).to_be_focused()
        self.page.locator('#close-detail').click()
        modes = self.page.evaluate('window.scrollModes')
        self.assertGreaterEqual(len(modes), 4)
        self.assertEqual(set(modes), {'auto'})

    def test_last_conversation_hide_settles_invalidated_catalog_refresh(self):
        self.conversations = self.conversations[:1]
        self.page.evaluate('loadState(false)')
        library_fixture.LibraryBrowserTests.open_metadata(self)
        self.hold_state = True
        self.page.evaluate('() => { window.heldCatalogRefresh = loadState(false); }')
        self.wait_route(lambda: self.pending_state is not None)
        self.page.locator('#conversation-hide').click()
        self.expect(self.page.locator('#conversation-dialog')).to_be_hidden()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(0)
        with self.subTest(phase='write-completed'):
            self.expect(self.page.locator('#empty-state')).to_be_visible()
        route, response = self.pending_state
        self.hold_state = False
        self.reply(route, response)
        self.page.evaluate('window.heldCatalogRefresh')
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(0)
        self.expect(self.page.locator('#empty-state')).to_be_visible()
        self.expect(self.page.locator('#catalog-empty')).to_be_hidden()

    def test_tag_write_settles_invalidated_refresh_of_confirmed_empty_catalog(self):
        self.conversations.clear()
        self.page.evaluate('loadState(false)')
        self.page.locator('[data-view="settings"]').click()
        self.hold_state = True
        self.page.evaluate('() => { window.heldCatalogRefresh = loadState(false); }')
        self.wait_route(lambda: self.pending_state is not None)
        self.page.locator('#tag-name').fill('合成加载标签')
        self.page.locator('#tag-save').click()
        self.expect(self.page.locator('#tag-feedback')).to_contain_text('已保存')
        self.page.locator('[data-view="catalog"]').click()
        with self.subTest(phase='write-completed'):
            self.expect(self.page.locator('#empty-state')).to_be_visible()
        route, response = self.pending_state
        self.hold_state = False
        self.reply(route, response)
        self.page.evaluate('window.heldCatalogRefresh')
        self.expect(self.page.locator('#empty-state')).to_be_visible()
        self.page.locator('#empty-state a').click()
        self.expect(self.page.locator('#chat-file')).to_be_focused()

    def test_tag_write_invalidating_first_read_stays_unknown_and_recoverable(self):
        self.hold_state = True
        self.page.reload(wait_until='domcontentloaded')
        self.wait_route(lambda: self.pending_state is not None)
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('#tag-name').fill('合成首次标签')
        self.page.locator('#tag-save').click()
        self.expect(self.page.locator('#tag-feedback')).to_contain_text('已保存')
        self.page.locator('[data-view="catalog"]').click()
        with self.subTest(phase='write-completed'):
            self.expect(self.page.locator('#result-summary')).to_contain_text('读取失败')
        self.expect(self.page.locator('#empty-state')).to_be_hidden()
        self.expect(self.page.locator('#catalog-empty')).to_be_hidden()
        route, response = self.pending_state
        self.hold_state = False
        with self.page.expect_response('**/api/state'):
            self.reply(route, response)
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        with self.subTest(phase='stale-read-released'):
            self.expect(self.page.locator('#result-summary')).to_contain_text('读取失败')
        self.expect(self.page.locator('#empty-state')).to_be_hidden()
        self.page.locator('#refresh-button').click()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(2)
        self.expect(self.page.locator('#result-summary')).not_to_contain_text('读取失败')

    def test_background_refresh_preserves_row_focus_on_success_and_failure(self):
        row = self.page.locator('#catalog-body [data-bundle-id="fixture-1"]')
        for failed in (False, True):
            with self.subTest(failed=failed):
                row.focus()
                self.hold_state = True
                self.pending_state = None
                self.page.evaluate('() => { window.heldCatalogRefresh = loadState(false).catch(() => {}); }')
                self.wait_route(lambda: self.pending_state is not None)
                with self.subTest(stage='pending'):
                    self.expect(row).to_be_focused()
                route, response = self.pending_state
                self.hold_state = False
                if failed:
                    route.fulfill(status=503, content_type='text/plain', body='synthetic unavailable')
                else:
                    self.reply(route, response)
                self.page.evaluate('window.heldCatalogRefresh')
                self.expect(row).to_be_focused()

    def check_import_refresh_row_focus(self, failed):
        self.importer()
        self.select_file()
        self.submit()
        self.page.locator('[data-view="catalog"]').click()
        row = self.page.locator('#catalog-body [data-bundle-id="fixture-1"]')
        row.focus()
        self.hold_state = True
        self.confirm_import()
        self.wait_route(lambda: self.pending_state is not None)
        with self.subTest(stage='pending'):
            self.expect(row).to_be_focused()
        route, response = self.pending_state
        self.hold_state = False
        if failed:
            route.fulfill(status=503, content_type='text/plain', body='synthetic unavailable')
        else:
            self.reply(route, response)
        self.wait_finished()
        self.expect(row).to_be_focused()
        self.expect(self.page.locator('#dashboard')).to_be_hidden()
        self.expect(self.page.locator('#import-result')).to_contain_text('导入完成')
        self.expect(self.page.locator('#import-result-open')).to_be_visible()

    def test_import_refresh_success_preserves_current_row_focus(self):
        self.check_import_refresh_row_focus(False)

    def test_import_refresh_failure_preserves_current_row_focus(self):
        self.check_import_refresh_row_focus(True)

    def test_background_refresh_never_restores_hidden_row_or_steals_control_focus(self):
        removed_row = self.page.locator('#catalog-body [data-bundle-id="fixture-1"]')
        removed_row.focus()
        self.conversations[1]['hidden_at'] = '2026-09-06T00:00:00Z'
        self.page.evaluate('loadState(false)')
        self.expect(removed_row).to_have_count(0)
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(1)
        self.assertIsNone(self.page.evaluate('document.activeElement?.dataset.bundleId || null'))
        self.page.locator('#search-input').focus()
        self.page.evaluate('loadState(false)')
        self.expect(self.page.locator('#search-input')).to_be_focused()
        self.page.locator('[data-view="settings"]').click()
        self.page.locator('#tag-name').focus()
        self.page.evaluate('loadState(false)')
        self.expect(self.page.locator('#tag-name')).to_be_focused()


if __name__ == '__main__':
    unittest.main()
