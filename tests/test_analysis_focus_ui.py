"""Synthetic, random-port browser coverage for fixed analysis perspectives.

All AI endpoints are intercepted by the shared fixture; external traffic is
aborted. These tests never use original chats, credentials, or a cloud model.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import test_analysis_ui as ui


CATALOG_PATH = Path(__file__).resolve().parents[1] / 'dashboard/static/analysis-focus.json'
ACTIVE = {
    'relation_overview': '关系全景',
    'interaction_rhythm': '互动节奏',
    'key_moments': '关键节点',
    'shared_plans': '共同计划',
    'contact_gaps': '联系空白',
    'communication_boundaries': '沟通与边界',
}
LEGACY = {
    'overview': '对话摘要与行动项',
    'communication': '沟通方式与互动变化',
    'business': '业务推进与待办风险',
    'relationship': '关系观察与边界建议',
}


@unittest.skipUnless(ui.UI_ENABLED, 'Opt-in: set SHE_LOVE_ME_UI_TESTS=1 for synthetic browser tests')
class AnalysisFocusBrowserTests(unittest.TestCase):
    # Reuse the server and protocol fixture, without rerunning its test methods.
    setUpClass = classmethod(ui.AnalysisBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(ui.AnalysisBrowserTests.tearDownClass.__func__)
    setUp = ui.AnalysisBrowserTests.setUp
    tearDown = ui.AnalysisBrowserTests.tearDown
    route_request = ui.AnalysisBrowserTests.route_request
    result = ui.AnalysisBrowserTests.result
    plan = ui.AnalysisBrowserTests.plan
    job = ui.AnalysisBrowserTests.job
    metrics = ui.AnalysisBrowserTests.metrics
    report = ui.AnalysisBrowserTests.report
    open_ai = ui.AnalysisBrowserTests.open_ai
    select_scope = ui.AnalysisBrowserTests.select_scope
    preview = ui.AnalysisBrowserTests.preview
    run_calls = ui.AnalysisBrowserTests.run_calls
    delay_get = ui.AnalysisBrowserTests.delay_get
    release_delayed_get = ui.AnalysisBrowserTests.release_delayed_get
    capture = ui.AnalysisBrowserTests.capture

    def catalog(self):
        return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))

    def preview_calls(self):
        return [body for method, path, body in self.api_calls
                if method == 'POST' and path == '/api/ai/preview']

    def set_scope_dates(self):
        self.page.locator('#ai-bundle').select_option('fixture_000')
        self.page.locator('#ai-date-from').fill('2026-01-01')
        self.page.locator('#ai-date-to').fill('2026-09-01')

    def test_six_native_perspectives_default_and_accessible_descriptions(self):
        self.open_ai()
        select = self.page.get_by_role('combobox', name='分析视角', exact=True)
        self.expect(select).to_be_visible()
        self.expect(select).to_have_value('relation_overview')
        self.assertEqual(select.evaluate('(node) => node.tagName'), 'SELECT')
        self.assertEqual(select.locator('option').all_text_contents(), list(ACTIVE.values()))
        self.assertEqual(select.locator('option').evaluate_all('(nodes) => nodes.map(n => n.value)'), list(ACTIVE))
        self.expect(select).to_have_attribute('aria-describedby', 'ai-focus-note')
        self.capture('analysis-focus-desktop.png')
        note = self.page.locator('#ai-focus-note')
        self.expect(note).to_have_attribute('aria-live', 'polite')
        descriptions = {item['id']: item['description'] for item in self.catalog()['presets']}
        self.expect(note).to_have_text(descriptions['relation_overview'])
        for focus in ACTIVE:
            with self.subTest(focus=focus):
                select.select_option(focus)
                self.expect(note).to_have_text(descriptions[focus])
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def test_each_selected_perspective_reaches_preview_as_exact_id_only(self):
        self.open_ai()
        self.set_scope_dates()
        for focus, label in ACTIVE.items():
            with self.subTest(focus=focus):
                self.page.locator('#ai-focus').select_option(focus)
                self.preview()
                self.expect(self.page.locator('#ai-preview-range')).to_contain_text(label)
                body = self.preview_calls()[-1]
                self.assertEqual(body['focus'], focus)
                self.assertEqual(set(body), {'bundle_id', 'date_from', 'date_to', 'focus',
                                            'analysis_mode', 'include_voice_transcripts', 'max_messages'})
                self.assertEqual(self.run_calls(), [])

    def test_perspective_change_revokes_preview_send_and_repeat_billing_consent(self):
        self.blocked_calls = 1
        self.open_ai()
        self.set_scope_dates()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-retry-uncertain').check()
        self.expect(self.page.locator('#ai-run-button')).to_be_enabled()
        self.page.locator('#ai-focus').select_option('contact_gaps')
        self.expect(self.page.locator('#ai-focus-note')).to_have_text(
            next(item['description'] for item in self.catalog()['presets'] if item['id'] == 'contact_gaps'))
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-retry-uncertain')).not_to_be_checked()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.assertEqual(len(self.preview_calls()), 1)
        self.preview()
        self.assertEqual(self.preview_calls()[-1]['focus'], 'contact_gaps')
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-retry-uncertain')).not_to_be_checked()
        self.assertEqual(self.run_calls(), [])

    def test_each_legacy_history_title_and_recheck_preserve_original_scope(self):
        self.has_report = True
        self.job_state = 'error'
        self.blocked_calls = 1
        self.open_ai()
        for focus, label in LEGACY.items():
            with self.subTest(focus=focus):
                original = {'bundle_id': 'fixture_000', 'date_from': '2026-02-01',
                            'date_to': '2026-08-01', 'focus': focus, 'analysis_mode': 'full',
                            'include_voice_transcripts': True}
                self.current_scope = dict(original)
                self.page.locator('#ai-refresh-history').click()
                self.expect(self.page.locator('#ai-history-list')).to_contain_text(label)
                self.page.locator('#ai-history-list .history-item').click()
                self.expect(self.page.locator('#ai-result-title')).to_contain_text(label)
                self.page.locator('#ai-recheck-partial').click()
                self.expect(self.page.locator('#ai-preview')).to_be_visible()
                self.expect(self.page.locator('#ai-focus')).to_have_value(focus)
                self.expect(self.page.locator('#ai-focus-note')).to_contain_text('旧版')
                self.expect(self.page.locator('#ai-focus option')).to_have_count(7)
                self.assertEqual(set(self.page.locator('#ai-focus option').evaluate_all(
                    '(nodes) => nodes.map(n => n.value)')), set(ACTIVE) | {focus})
                self.assertEqual(self.preview_calls()[-1], original)
                self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
                self.expect(self.page.locator('#ai-retry-uncertain')).not_to_be_checked()
                self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
                self.page.locator('#ai-focus').select_option('relation_overview')
                self.expect(self.page.locator('#ai-focus option')).to_have_count(6)
                self.expect(self.page.locator('#ai-focus-note')).not_to_contain_text('旧版')
                self.assertEqual(self.run_calls(), [])

    def test_catalog_loading_disables_select_and_preview_until_validated(self):
        self.delay_get('/analysis-focus.json', self.catalog())
        self.open_ai()
        self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
        self.expect(self.page.locator('#ai-focus')).to_be_disabled()
        self.expect(self.page.locator('#ai-focus')).to_have_value('')
        self.expect(self.page.locator('#ai-preview-button')).to_be_disabled()
        self.set_scope_dates()
        self.page.locator('#ai-scope-form').evaluate('(form) => form.requestSubmit()')
        self.assertEqual(self.preview_calls(), [])
        self.release_delayed_get()
        self.expect(self.page.locator('#ai-focus')).to_be_enabled()
        self.expect(self.page.locator('#ai-focus')).to_have_value('relation_overview')
        self.expect(self.page.locator('#ai-preview-button')).to_be_enabled()
        self.assertEqual(self.run_calls(), [])

    def test_stalled_catalog_keeps_history_report_and_late_labels_independent(self):
        original = {'bundle_id': 'fixture_000', 'focus': 'business',
                    'date_from': '2026-01-01', 'date_to': '2026-09-01'}
        self.current_scope = dict(original)
        self.has_report = True
        self.delay_get('/analysis-focus.json', self.catalog())
        self.open_ai()
        self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
        history = self.page.locator('#ai-history-list .history-item')
        self.expect(history).to_have_count(1)
        history.click()
        self.expect(self.page.locator('#ai-result')).to_be_visible()
        self.expect(self.page.locator('#ai-result-title')).to_contain_text('分析报告')
        self.expect(self.page.locator('#ai-focus')).to_be_disabled()
        self.expect(history).to_be_focused()
        self.page.evaluate('''() => {
          window.__syntheticReportSection = document.querySelector('#ai-result-sections').firstElementChild;
          window.__syntheticReportCoverage = document.querySelector('#ai-result-coverage').firstElementChild;
        }''')
        calls = list(self.api_calls)
        self.release_delayed_get()
        self.expect(history).to_contain_text(LEGACY['business'])
        self.expect(self.page.locator('#ai-result-title')).to_contain_text(LEGACY['business'])
        self.expect(history).to_be_focused()
        self.assertTrue(self.page.evaluate('''() =>
          window.__syntheticReportSection === document.querySelector('#ai-result-sections').firstElementChild
          && window.__syntheticReportCoverage === document.querySelector('#ai-result-coverage').firstElementChild
        '''))
        self.assertEqual(self.api_calls, calls, 'Late labels must not reload reports or history')
        self.assertEqual(self.current_scope, original)
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def test_stalled_catalog_keeps_running_job_progress_and_cancel_available(self):
        self.current_scope = {'bundle_id': 'fixture_000', 'focus': 'communication',
                              'date_from': '2026-01-01', 'date_to': '2026-09-01',
                              'analysis_mode': 'full', 'include_voice_transcripts': False}
        self.recovery_job = True
        self.job_state = 'running'
        self.delay_get('/analysis-focus.json', self.catalog())
        self.open_ai()
        self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
        self.expect(self.page.locator('#ai-job-progress')).to_be_visible()
        self.expect(self.page.locator('#ai-cancel-job')).to_be_enabled()
        self.page.locator('#ai-cancel-job').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=10_000)
        self.assertTrue(self.cancelled)
        self.expect(self.page.locator('#ai-focus')).to_be_disabled()
        self.expect(self.page.locator('#ai-preview-button')).to_be_disabled()
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def test_catalog_failure_does_not_discard_an_independent_pending_report(self):
        self.current_scope = {'bundle_id': 'fixture_000', 'focus': 'relationship',
                              'date_from': '2026-01-01', 'date_to': '2026-09-01'}
        self.has_report = True
        self.delay_get('/analysis-focus.json', {**self.catalog(), 'version': 2})
        self.open_ai()
        self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
        self.page.evaluate('() => { window.__syntheticCatalogRelease = window.__syntheticDelayedGets[0]; }')
        self.expect(self.page.locator('#ai-history-list .history-item')).to_have_count(1)
        self.delay_get('/api/ai/reports/' + ui.REPORT_ID, self.report())
        self.page.locator('#ai-history-list .history-item').click()
        self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
        self.page.evaluate('window.__syntheticCatalogRelease()')
        self.expect(self.page.locator('#ai-focus-note')).to_have_text('分析视角暂时无法加载，请刷新页面后重试。')
        self.release_delayed_get()
        self.expect(self.page.locator('#ai-result')).to_be_visible()
        self.expect(self.page.locator('#ai-result-summary')).to_contain_text('合成分析摘要')
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def assert_catalog_keeps_pending_config_disabled(self, payload):
        self.delay_get('/analysis-focus.json', payload)
        self.page.evaluate('''() => {
          const originalFetch = window.fetch;
          window.__syntheticConfigResponses = [];
          window.fetch = async (url, options) => {
            const response = await originalFetch(url, options);
            if (String(url) === '/api/ai/config' && options?.method === 'POST') {
              return new Promise(resolve => window.__syntheticConfigResponses.push(() => resolve(response)));
            }
            return response;
          };
        }''')
        self.open_ai()
        self.expect(self.page.locator('#ai-config-badge')).to_contain_text('已配置')
        self.page.locator('#ai-settings summary').click()
        self.page.locator('#ai-model').fill('synthetic-pending-model')
        self.page.locator('#ai-save-config').click()
        self.page.wait_for_function('window.__syntheticConfigResponses.length === 1')
        self.expect(self.page.locator('#ai-config-fields')).to_have_js_property('disabled', True)
        self.release_delayed_get()
        self.expect(self.page.locator('#ai-config-fields')).to_have_js_property('disabled', True)
        self.expect(self.page.locator('#ai-save-config')).to_be_disabled()
        self.expect(self.page.locator('#ai-model')).to_have_value('synthetic-pending-model')
        self.page.evaluate('window.__syntheticConfigResponses.shift()()')
        self.expect(self.page.locator('#ai-config-fields')).to_have_js_property('disabled', False)
        saved = [body for method, path, body in self.api_calls
                 if method == 'POST' and path == '/api/ai/config']
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def test_successful_catalog_cannot_unlock_a_pending_config_save(self):
        self.assert_catalog_keeps_pending_config_disabled(self.catalog())

    def test_failed_catalog_cannot_unlock_a_pending_config_save(self):
        self.assert_catalog_keeps_pending_config_disabled({**self.catalog(), 'version': 2})

    def test_failed_catalog_keeps_history_and_settings_readable_without_preview(self):
        self.current_scope = {'bundle_id': 'fixture_000', 'focus': 'communication',
                              'date_from': '2026-01-01', 'date_to': '2026-09-01'}
        self.has_report = True
        self.page.evaluate('''() => {
          const originalFetch = window.fetch;
          window.fetch = (url, options) => String(url) === '/analysis-focus.json'
            ? Promise.resolve(new Response(JSON.stringify({error: 'UNTRUSTED-REMOTE-DETAIL'}),
                {status: 503, headers: {'Content-Type': 'application/json'}}))
            : originalFetch(url, options);
        }''')
        self.open_ai()
        self.expect(self.page.locator('#ai-focus-note')).to_have_text('分析视角暂时无法加载，请刷新页面后重试。')
        self.expect(self.page.locator('#ai-focus')).to_be_disabled()
        self.expect(self.page.locator('#ai-preview-button')).to_be_disabled()
        self.expect(self.page.locator('#ai-history-list .history-item')).to_have_count(1)
        self.expect(self.page.locator('#ai-config-badge')).to_contain_text('已配置')
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible()
        self.set_scope_dates()
        self.page.locator('#ai-scope-form').evaluate('(form) => form.requestSubmit()')
        self.expect(self.page.locator('#ai-feedback')).to_have_text('分析视角暂时无法加载，请刷新页面后重试。')
        self.expect(self.page.locator('body')).not_to_contain_text('UNTRUSTED-REMOTE-DETAIL')
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def test_malformed_catalogs_are_rejected_without_a_default_or_request(self):
        valid = self.catalog()
        malformed = [
            {**valid, 'version': 2},
            {**valid, 'default': 'missing'},
            {**valid, 'default': 'communication'},
            {**valid, 'presets': valid['presets'] + [valid['presets'][0]]},
            {**valid, 'presets': [{**valid['presets'][0], 'description': 99}] + valid['presets'][1:]},
        ]
        for index, payload in enumerate(malformed):
            with self.subTest(case=index):
                self.page.locator('[data-view="catalog"]').click()
                self.page.reload(wait_until='networkidle')
                self.delay_get('/analysis-focus.json', payload)
                self.open_ai()
                self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
                self.release_delayed_get()
                self.expect(self.page.locator('#ai-focus-note')).to_have_text('分析视角暂时无法加载，请刷新页面后重试。')
                self.expect(self.page.locator('#ai-focus')).to_be_disabled()
                self.expect(self.page.locator('#ai-focus')).to_have_value('')
                self.expect(self.page.locator('#ai-preview-button')).to_be_disabled()
                self.set_scope_dates()
                self.page.locator('#ai-scope-form').evaluate('(form) => form.requestSubmit()')
                self.assertEqual(self.preview_calls(), [])
                self.assertEqual(self.run_calls(), [])

    def test_unknown_history_perspective_cannot_silently_recheck_as_default(self):
        self.current_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                              'date_to': '2026-09-01', 'focus': 'unrecognized-perspective',
                              'analysis_mode': 'full', 'include_voice_transcripts': False}
        self.has_report = True
        self.job_state = 'error'
        self.open_ai()
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible()
        self.page.locator('#ai-recheck-partial').click()
        self.expect(self.page.locator('#ai-feedback')).to_contain_text('无法识别这份报告的分析视角')
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def test_unknown_selection_cannot_submit_a_preview(self):
        self.open_ai()
        self.set_scope_dates()
        self.page.locator('#ai-focus').evaluate('''select => {
          select.value = 'missing'; select.dispatchEvent(new Event('change', {bubbles: true}));
        }''')
        self.page.locator('#ai-scope-form').evaluate('(form) => form.requestSubmit()')
        self.expect(self.page.locator('#ai-feedback')).to_contain_text('请选择有效的分析视角')
        self.assertEqual(self.preview_calls(), [])
        self.assertEqual(self.run_calls(), [])

    def test_native_keyboard_selection_and_390px_layout(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.open_ai()
        self.set_scope_dates()
        select = self.page.get_by_role('combobox', name='分析视角', exact=True)
        select.focus()
        self.expect(select).to_be_focused()
        select.press('ArrowDown')
        select.press('Enter')
        self.expect(select).to_have_value('interaction_rhythm')
        self.expect(self.page.locator('#ai-focus-note')).to_have_text(
            next(item['description'] for item in self.catalog()['presets'] if item['id'] == 'interaction_rhythm'))
        self.preview()
        widths = self.page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
        self.assertLessEqual(widths['document'], widths['viewport'], widths)
        self.capture('analysis-focus-mobile.png')
        self.assertEqual(self.run_calls(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
