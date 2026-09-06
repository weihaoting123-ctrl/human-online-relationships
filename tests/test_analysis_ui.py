"""Opt-in scoped-AI browser tests with invented data and mocked cloud APIs.

No request reaches a real model; the isolated loopback server never uses 8765.
Run with SHE_LOVE_ME_UI_TESTS=1, alongside the existing dashboard UI suite.
"""
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


UI_ENABLED = os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1'
PREVIEW_ID = 'a' * 48
JOB_ID = 'b' * 48
REPORT_ID = 'c' * 48
SYNTHETIC_KEY = 'synthetic-ui-key-never-a-real-credential'
XSS = '<img src=x onerror=alert(1)>'


@unittest.skipUnless(UI_ENABLED, 'Opt-in: set SHE_LOVE_ME_UI_TESTS=1 for synthetic browser tests')
class AnalysisBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        fixtures.ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='analysis-ui-', dir=fixtures.ARTIFACTS)
        cls.data = Path(cls.temp.name)
        cls.contacts = cls.data / 'contacts'
        fixtures.make_bundle(cls.contacts, 0, '合成青禾', 8,
                             first='2026-01-01', last='2026-09-01')
        fixtures.make_bundle(cls.contacts, 1, '合成云舟群', 6, kind='group',
                             first='2026-01-01', last='2026-09-01')
        fixtures.write_json(cls.data / 'sync-status.json', {
            'enabled': True, 'state': 'completed', 'mode': 'scheduled',
            'scanned_conversations': 2, 'imported_conversations': 2,
            'imported_messages': 14, 'failed_conversations': 0,
        })
        fixtures.write_json(cls.data / 'voice-archive-status.json', {
            'state': 'partial', 'total_voice_messages': 12, 'archived_voice_messages': 10,
            'missing_voice_messages': 2, 'unique_audio_files': 9,
        })
        fixtures.write_json(cls.data / 'voice-transcribe-status.json', {
            'state': 'running', 'total_audio': 9, 'transcribed_audio': 6, 'pending_audio': 3,
            'playable_audio': 9, 'attached_messages': 7, 'processed_audio': 2, 'skipped_audio': 4,
        })
        cls.patches = [
            mock.patch.object(app, 'DATA_DIR', cls.data),
            mock.patch.object(app, 'CONTACTS_DIR', cls.contacts),
            mock.patch.object(app, 'RAW_IMPORT_DIR', cls.data / 'raw'),
            mock.patch.object(app, 'SYNC_STATUS_PATH', cls.data / 'sync-status.json'),
            mock.patch.object(app, 'exporter_status', return_value={'status': 'ready', 'ready': True}),
        ]
        for patch in cls.patches:
            patch.start()
        cls.server = app.create_server(port=0, token='synthetic-analysis-test-token')
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/'
        assert cls.server.server_port != 8765, 'Never use the production port'
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
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1024},
                                                reduced_motion='reduce')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors, self.dialogs, self.requests, self.api_calls = [], [], [], []
        self.config = {'status': 'ok', 'configured': True, 'provider': 'openai',
                       'model': 'synthetic-model', 'has_key': True,
                       'endpoint': 'https://api.openai.com/v1/chat/completions'}
        self.current_scope = None
        self.recipient_override = None
        self.has_report = False
        self.job_state = 'completed'
        self.recovery_job = False
        self.cancelled = False
        self.blocked_calls = 0
        self.plan_overrides = {}
        self.report_overrides = {}
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('console', lambda message: self.errors.append(message.text)
                     if message.type == 'error' else None)
        self.page.on('request', lambda request: self.requests.append((request.method, request.url)))
        self.page.on('dialog', lambda dialog: (self.dialogs.append(dialog.message), dialog.dismiss()))
        self.page.route('**/*', self.route_request)
        self.page.goto(self.url, wait_until='networkidle')
        self.expect(self.page.locator('#catalog-body .catalog-row')).to_have_count(2)

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dialogs, [], 'Cloud result text must not execute HTML')
        self.assertTrue(all(url.startswith(self.url) for _, url in self.requests),
                        'All traffic must remain on the synthetic loopback server')

    def route_request(self, route):
        request = route.request
        if not request.url.startswith(self.url):
            route.abort()
            return
        path = urlparse(request.url).path
        if path.startswith('/api/backup/'):
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'status': 'ok', 'backup': {'state': 'idle', 'snapshot_count': 0}, 'snapshots': []}))
            return
        if not path.startswith('/api/ai/'):
            route.continue_()
            return
        body = request.post_data_json if request.method == 'POST' else None
        self.api_calls.append((request.method, path, body))
        response = None
        if path == '/api/ai/config':
            if request.method == 'POST':
                self.config.update(provider=body['provider'], model=body['model'],
                                   configured=True, has_key=True)
                host = 'api.deepseek.com/chat/completions' if body['provider'] == 'deepseek' else 'api.openai.com/v1/chat/completions'
                self.config['endpoint'] = 'https://' + host
            response = self.config
        elif path == '/api/ai/config/clear':
            self.config.update(configured=False, has_key=False)
            response = self.config
        elif path == '/api/ai/preview':
            self.current_scope = {key: body[key] for key in ('bundle_id', 'date_from', 'date_to', 'focus', 'include_voice_transcripts', 'analysis_mode')}
            if self.recipient_override:
                # Another tab updated the server configuration after this
                # page's initial GET; its cached config must not win consent.
                self.config.update(self.recipient_override)
            response = {'status': 'ok', 'preview_id': PREVIEW_ID,
                        'expires_at': '2099-01-01T00:00:00+00:00', 'scope': self.current_scope,
                        'recipient': self.recipient_override or {
                            key: self.config[key] for key in ('configured', 'provider', 'model', 'endpoint')},
                        'eligible_messages': 7, 'sample_messages': 7, 'sample_chars': 82,
                        'plan': self.plan(), 'metrics': self.metrics(),
                        'truncated_messages': 0,
                        'stats': {'scope_messages': 8, 'me_messages': 4, 'other_messages': 4,
                                  'excluded_nontext': 1, 'invalid_messages': 0, 'voice_messages': 3,
                                  'transcribed_voice_messages': 2,
                                  'sampled_voice_messages': 2 if body['include_voice_transcripts'] else 0}}
        elif path == '/api/ai/run':
            self.has_report = True
            response = {'status': 'ok', 'job_id': JOB_ID, 'state': 'pending',
                        'scope': self.current_scope, 'plan': self.plan()}
        elif path == '/api/ai/jobs':
            response = {'status': 'ok', 'jobs': [self.job()] if self.recovery_job else []}
        elif path == '/api/ai/jobs/' + JOB_ID:
            response = self.job()
        elif path == '/api/ai/jobs/' + JOB_ID + '/cancel':
            self.cancelled = True
            self.job_state = 'cancelled'
            response = {'status': 'ok', 'job_id': JOB_ID, 'state': 'running', 'cancel_requested': True}
        elif path == '/api/ai/history':
            report = self.report()
            response = {'status': 'ok', 'reports': [
                {key: value for key, value in report.items() if key not in {'status', 'result'}}
            ] if self.has_report else []}
        elif path == '/api/ai/reports/' + REPORT_ID:
            response = self.report()
        if response is None:
            self.errors.append('Unexpected synthetic AI endpoint: ' + path)
            route.fulfill(status=404, content_type='application/json',
                          body=json.dumps({'status': 'error', 'error': 'synthetic endpoint not mocked'}))
        else:
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps(response, ensure_ascii=False))

    def result(self):
        return {'summary': '合成分析摘要 ' + XSS, 'observations': [XSS],
                'actions': ['合成行动项'], 'caveats': ['仅用于自动化测试，不是真实聊天结论']}

    def plan(self):
        full = (self.current_scope or {}).get('analysis_mode') == 'full'
        return {'mode': 'full' if full else 'sample', 'segments': 3 if full else 1,
                'total_calls': 4 if full else 1, 'cached_calls': 1 if full else 0,
                'new_calls': 3 if full else 1, 'merge_calls': 1 if full else 0,
                'characters': 82, 'split_messages': 1 if full else 0,
                'estimated_input_tokens': 3000, 'output_token_limit': 2400,
                'requires_multiple_calls': full, 'blocked_calls': self.blocked_calls,
                **self.plan_overrides}

    def job(self):
        terminal = self.job_state in {'completed', 'error', 'cancelled'}
        return {'status': 'ok', 'job_id': JOB_ID, 'state': self.job_state,
                'scope': self.current_scope, 'plan': self.plan(),
                'report_id': REPORT_ID if terminal else None,
                'cancel_requested': self.cancelled,
                'error': '合成网络错误' if self.job_state == 'error' else None,
                'progress': {'stage': 'completed' if terminal else 'segments',
                             'completed_calls': 4 if self.job_state == 'completed' else 1,
                             'total_calls': self.plan()['total_calls'],
                             'completed_segments': self.plan()['segments'] if self.job_state == 'completed' else 1,
                             'total_segments': self.plan()['segments'],
                             'cached_calls': self.plan()['cached_calls']}}

    def metrics(self):
        return {'version': 1, 'basis': 'selected_scope_all_messages',
                'totals': {'messages': 8, 'me': 5, 'other': 3, 'text': 5,
                           'voice': 3, 'transcribed_voice': 2, 'characters': 82,
                           'active_days': 4, 'sessions': 3},
                'activity': [{'date': f'2026-0{index + 1}-01', 'count': 2,
                              'me': 1, 'other': 1} for index in range(4)],
                'activity_granularity': 'day',
                'hours': [{'hour': index, 'count': index % 3} for index in range(24)],
                'weekdays': [{'weekday': index, 'count': index + 1} for index in range(7)],
                'types': [{'type': 'text', 'count': 5}, {'type': 'voice', 'count': 3}],
                'response_times': {'me': {'samples': 3, 'median_seconds': 35, 'p90_seconds': 60},
                                   'other': {'samples': 2, 'median_seconds': 90, 'p90_seconds': 120}},
                'methodology': ['合成统计口径，只按时间差计算，不代表回复意愿。']}

    def report(self):
        return {'status': 'ok', 'id': REPORT_ID, 'created_at': '2026-09-06T02:00:00+00:00',
                'provider': self.config['provider'], 'model': self.config['model'],
                'scope': self.current_scope, 'sample_messages': 7, 'result': self.result(),
                'mode': self.plan()['mode'], 'plan': self.plan(), 'metrics': self.metrics(),
                'coverage': {'eligible_messages': 7, 'analyzed_messages': 7 if self.job_state == 'completed' else 3,
                             'total_segments': self.plan()['segments'],
                             'completed_segments': self.plan()['segments'] if self.job_state == 'completed' else 1,
                             'complete': self.job_state == 'completed'},
                'segments': [{'index': index, 'date_from': f'2026-0{index + 1}-01',
                              'date_to': f'2026-0{index + 1}-28', 'messages': 3, 'characters': 30,
                              'state': 'completed' if index == 0 or self.job_state == 'completed' else 'pending',
                              'cached': index == 0, 'summary': XSS if index == 0 else '合成分段摘要'}
                             for index in range(self.plan()['segments'])], **self.report_overrides}

    def open_ai(self):
        self.page.locator('[data-view="analysis-workspace"]').click()
        self.expect(self.page.locator('#analysis-workspace')).to_be_visible()
        self.expect(self.page.locator('#ai-bundle option[value="fixture_000"]')).to_have_count(1)

    def select_scope(self):
        self.page.locator('#ai-bundle').select_option('fixture_000')
        self.page.locator('#ai-date-from').fill('2026-01-01')
        self.page.locator('#ai-date-to').fill('2026-09-01')
        self.page.locator('#ai-focus').select_option('communication')
        self.page.locator('#ai-max-messages').select_option('100')

    def preview(self):
        self.page.locator('#ai-preview-button').click()
        self.expect(self.page.locator('#ai-preview')).to_be_visible()

    def run_calls(self):
        return [body for method, path, body in self.api_calls
                if method == 'POST' and path == '/api/ai/run']

    def delay_get(self, path, response):
        # Delay delivery rather than the socket: a response that has already
        # arrived must still be rejected after the user changes current state.
        self.page.evaluate('''({path, response}) => {
          const originalFetch = window.fetch;
          window.__syntheticDelayedGets = [];
          window.fetch = (url, options) => {
            if (String(url) === path && (!options?.method || options.method === 'GET')) {
              return new Promise(resolve => {
                window.__syntheticDelayedGets.push(() => resolve(new Response(
                  JSON.stringify(response),
                  {status: 200, headers: {'Content-Type': 'application/json'}})));
              });
            }
            return originalFetch(url, options);
          };
        }''', {'path': path, 'response': response})

    def release_delayed_get(self):
        self.page.evaluate('''async () => {
          window.__syntheticDelayedGets.shift()();
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
        }''')

    def begin_delayed_config(self):
        self.delay_get('/api/ai/config', dict(self.config))
        self.open_ai()
        self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
        self.page.locator('#ai-settings summary').click()

    def test_navigation_and_preview_never_starts_analysis(self):
        self.open_ai()
        self.expect(self.page.locator('#catalog')).to_be_hidden()
        self.expect(self.page.locator('#maintenance')).to_be_hidden()
        self.select_scope()
        self.assertEqual(self.run_calls(), [])
        self.preview()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.assertEqual(self.run_calls(), [])
        preview_body = [body for method, path, body in self.api_calls
                        if method == 'POST' and path == '/api/ai/preview'][-1]
        self.assertEqual(preview_body, {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                                       'date_to': '2026-09-01', 'focus': 'communication', 'max_messages': 100,
                                       'include_voice_transcripts': False, 'analysis_mode': 'sample'})
        self.capture('analysis-ui-desktop.png')
        self.page.locator('[data-view="maintenance"]').click()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        self.expect(self.page.locator('#catalog')).to_be_hidden()
        self.expect(self.page.locator('#analysis-workspace')).to_be_hidden()

    def test_scope_changes_clear_consent_and_invalidate_preview(self):
        self.open_ai()
        self.select_scope()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.expect(self.page.locator('#ai-run-button')).to_be_enabled()
        self.page.locator('#ai-date-to').fill('2026-08-01')
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-bundle').select_option('fixture_001')
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.assertEqual(self.run_calls(), [])

    def test_full_text_is_separate_from_all_dates_and_authorizes_complete_plan(self):
        self.open_ai()
        self.select_scope()
        self.page.locator('[data-ai-range="all"]').click()
        self.expect(self.page.locator('input[name="ai-analysis-mode"][value="sample"]')).to_be_checked()
        self.expect(self.page.locator('#ai-max-messages option')).to_have_count(6)
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('input[name="ai-analysis-mode"][value="full"]').check()
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-max-messages')).to_be_disabled()
        self.expect(self.page.locator('#ai-sample-limit')).to_be_hidden()
        self.preview()
        body = [body for method, path, body in self.api_calls if path == '/api/ai/preview'][-1]
        self.assertEqual(body['analysis_mode'], 'full')
        self.assertNotIn('max_messages', body)
        self.expect(self.page.locator('#ai-call-plan')).to_contain_text('3 段 · 3 次新调用')
        self.expect(self.page.locator('#ai-call-plan')).to_contain_text('3,000 tokens')
        self.expect(self.page.locator('#ai-consent-text')).to_contain_text('全部 3 个分段及 1 次汇总')
        self.expect(self.page.locator('#ai-preview-limit')).to_contain_text('1 条长文字被拆分')
        self.expect(self.page.locator('#ai-preview-coverage')).to_contain_text('计划 7 / 7 条')
        self.expect(self.page.locator('#ai-metrics')).to_be_visible()
        self.assertEqual(self.run_calls(), [])
        self.capture('analysis-full-preview-desktop.png')

    def test_local_charts_have_accessible_values_and_do_not_send_metrics(self):
        self.open_ai()
        self.select_scope()
        self.preview()
        self.expect(self.page.locator('#ai-metrics h3')).to_have_count(6)
        self.expect(self.page.locator('#ai-metrics svg[role="img"]')).to_have_count(3)
        self.page.locator('#ai-metrics .ai-chart-data summary').first.click()
        self.expect(self.page.locator('#ai-metrics .ai-chart-data table').first).to_be_visible()
        self.expect(self.page.locator('#ai-metrics')).to_contain_text('62.5%')
        self.expect(self.page.locator('#ai-metrics')).to_contain_text('35 秒')
        self.assertFalse(any('metrics' in (body or {}) for _, _, body in self.api_calls))
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.page.locator('#ai-result-segments summary').click()
        self.expect(self.page.locator('#ai-result-segments')).to_contain_text(XSS)
        self.assertEqual(self.page.locator('#ai-result img, #ai-result script').count(), 0)

    def test_stop_after_current_call_preserves_partial_report(self):
        self.job_state = 'running'
        self.open_ai()
        self.select_scope()
        self.page.locator('input[name="ai-analysis-mode"][value="full"]').check()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-job-progress')).to_be_visible()
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('分段 1 / 3')
        self.expect(self.page.locator('#ai-bundle')).to_be_disabled()
        self.capture('analysis-full-progress.png')
        self.page.locator('#ai-cancel-job').click()
        self.expect(self.page.locator('#ai-result-status')).to_contain_text('部分结果', timeout=15_000)
        self.expect(self.page.locator('#ai-history-list')).to_contain_text('部分结果 · 1 / 3 段')
        self.assertEqual(len(self.run_calls()), 1)
        self.assertEqual(len([path for method, path, _ in self.api_calls
                              if method == 'POST' and path.endswith('/cancel')]), 1)
        self.page.locator('#ai-result-segments summary').click()
        self.capture('analysis-full-partial.png')

    def test_reload_recovers_running_job_without_new_analysis(self):
        self.current_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                              'date_to': '2026-09-01', 'focus': 'communication', 'analysis_mode': 'full'}
        self.recovery_job = True
        self.job_state = 'running'
        self.open_ai()
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('分段 1 / 3')
        self.page.reload(wait_until='networkidle')
        self.expect(self.page.locator('#ai-job-progress')).to_be_visible()
        self.expect(self.page.locator('#ai-bundle')).to_be_disabled()
        self.assertEqual(self.run_calls(), [])
        self.job_state = 'completed'
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.assertEqual(self.run_calls(), [])

    def test_wait_budget_scales_with_planned_calls_and_never_reposts(self):
        self.job_state = 'running'
        self.plan_overrides = {'segments': 8, 'total_calls': 9, 'new_calls': 9, 'merge_calls': 1}
        self.open_ai()
        self.select_scope()
        self.page.locator('input[name="ai-analysis-mode"][value="full"]').check()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('分段 1 / 8')
        # Move the clock past the former six-minute cutoff without waiting or
        # causing another billable request. Nine planned calls still have time.
        self.page.evaluate('''() => {
          const originalNow = Date.now;
          Date.now = () => originalNow() + 7 * 60 * 1000;
        }''')
        with self.page.expect_response(lambda response: response.url.endswith('/api/ai/jobs/' + JOB_ID)):
            pass
        self.expect(self.page.locator('#ai-resume-poll')).to_be_hidden()
        self.expect(self.page.locator('#ai-bundle')).to_be_disabled()
        self.assertEqual(len(self.run_calls()), 1)
        self.job_state = 'completed'
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.assertEqual(len(self.run_calls()), 1)

    def test_error_is_partial_and_requires_separate_uncertain_retry_consent(self):
        self.job_state = 'error'
        self.open_ai()
        self.select_scope()
        self.page.locator('input[name="ai-analysis-mode"][value="full"]').check()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-result-status')).to_contain_text('部分结果', timeout=15_000)
        self.assertEqual(len(self.run_calls()), 1)
        self.blocked_calls = 1
        self.preview()
        self.page.locator('#ai-consent').check()
        self.expect(self.page.locator('#ai-retry-line')).to_be_visible()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.page.locator('#ai-retry-uncertain').check()
        self.expect(self.page.locator('#ai-run-button')).to_be_enabled()
        self.job_state = 'completed'
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.assertEqual(self.run_calls()[-1], {'preview_id': PREVIEW_ID, 'consent': True, 'retry_uncertain': True})

    def test_full_result_and_charts_fit_mobile(self):
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.open_ai()
        self.select_scope()
        self.page.locator('input[name="ai-analysis-mode"][value="full"]').check()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.page.locator('#ai-result-segments summary').click()
        self.page.locator('#ai-metrics .ai-chart-data summary').first.click()
        widths = self.page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
        self.assertLessEqual(widths['document'], widths['viewport'], widths)
        self.capture('analysis-full-result-mobile.png')

    def test_partial_report_recheck_restores_scope_without_sending_to_cloud(self):
        original_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-02-01',
                          'date_to': '2026-08-01', 'focus': 'communication',
                          'analysis_mode': 'full', 'include_voice_transcripts': True}
        self.current_scope = dict(original_scope)
        self.has_report = True
        self.job_state = 'error'
        self.blocked_calls = 1
        self.plan_overrides = {'segments': 42, 'total_calls': 51, 'cached_calls': 22,
                               'new_calls': 29, 'merge_calls': 9}
        self.report_overrides = {
            'scope': original_scope,
            'stop_reason': '分析结果格式不完整；本次不会自动重试',
            'failed_segment': 23,
            'coverage': {'eligible_messages': 16610, 'analyzed_messages': 8800,
                         'total_segments': 42, 'completed_segments': 22, 'complete': False},
        }
        self.open_ai()
        self.page.locator('#ai-bundle').select_option('fixture_001')
        self.page.locator('#ai-date-from').fill('2026-03-01')
        self.page.locator('#ai-date-to').fill('2026-07-01')
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(self.page.locator('#ai-result-status')).to_contain_text('全量计划 · 部分完成')
        self.expect(self.page.locator('#ai-result-status')).to_contain_text('7,810 条文字、20 段未完成')
        self.expect(self.page.locator('#ai-result-stop-reason')).to_contain_text('分析结果格式不完整')
        self.expect(self.page.locator('#ai-result-coverage')).to_contain_text('8,800 / 16,610 条')
        self.capture('analysis-partial-guidance-desktop.png')
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.capture('analysis-partial-guidance-mobile.png')
        widths = self.page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
        self.assertLessEqual(widths['document'], widths['viewport'], widths)
        self.page.locator('#ai-recheck-partial').click()
        self.expect(self.page.locator('#ai-preview')).to_be_visible()
        self.assertEqual(self.current_scope, original_scope)
        self.expect(self.page.locator('#ai-bundle')).to_have_value('fixture_000')
        self.expect(self.page.locator('input[name="ai-analysis-mode"][value="full"]')).to_be_checked()
        self.expect(self.page.locator('#ai-include-voice')).to_be_checked()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-retry-uncertain')).not_to_be_checked()
        self.expect(self.page.locator('#ai-retry-line')).to_be_visible()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.assertEqual(self.run_calls(), [])
        self.page.locator('#ai-consent').check()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()


    def test_voice_toggle_requires_new_preview_and_new_consent(self):
        self.open_ai()
        self.select_scope()
        self.expect(self.page.locator('#ai-include-voice')).not_to_be_checked()
        self.preview()
        self.expect(self.page.locator('#ai-preview-voice')).to_contain_text('含 0 条语音转写')
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-include-voice').check()
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.preview()
        self.expect(self.page.locator('#ai-preview-voice')).to_contain_text('含 2 条语音转写')
        self.expect(self.page.locator('#ai-preview-voice')).to_contain_text('原始音频不上传')
        self.assertTrue(self.current_scope['include_voice_transcripts'])
        self.assertEqual(self.run_calls(), [])
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.expect(self.page.locator('#ai-history-list')).to_contain_text('已选择加入语音转写')
        self.expect(self.page.locator('#ai-result-meta')).to_contain_text('已选择加入语音转写')

    def test_maintenance_displays_voice_aggregate_progress(self):
        self.page.locator('[data-view="maintenance"]').click()
        self.expect(self.page.locator('#voice-archive-summary')).to_contain_text('10 / 12 条语音')
        self.expect(self.page.locator('#voice-archive-counts')).to_contain_text('6 个音频')
        self.expect(self.page.locator('#voice-transcribe-summary')).to_contain_text('正在本机转写')
        self.expect(self.page.locator('#voice-gallery-link')).to_have_attribute('href', '/exports/wechat-voice/index.html')
        self.assertEqual(self.run_calls(), [])

    def test_preview_recipient_overrides_stale_configuration_from_other_tab(self):
        self.recipient_override = {'configured': True, 'provider': 'deepseek',
                                   'model': 'synthetic-updated-model',
                                   'endpoint': 'https://api.deepseek.com/chat/completions'}
        self.open_ai()
        self.select_scope()
        self.preview()
        recipient = self.page.locator('#ai-preview-recipient')
        self.expect(recipient).to_contain_text('DeepSeek')
        self.expect(recipient).to_contain_text('synthetic-updated-model')
        self.expect(recipient).to_contain_text('https://api.deepseek.com/chat/completions')
        self.expect(recipient).not_to_contain_text('api.openai.com')
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.assertEqual(self.run_calls(), [])
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.assertEqual(self.run_calls(), [{'preview_id': PREVIEW_ID, 'consent': True}])

    def test_confirmed_run_and_history_render_cloud_output_as_text(self):
        self.open_ai()
        self.select_scope()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-result')).to_be_visible(timeout=15_000)
        self.expect(self.page.locator('#ai-result-summary')).to_contain_text(XSS)
        self.assertEqual(self.page.locator('#ai-result img, #ai-result script').count(), 0)
        self.assertEqual(self.run_calls(), [{'preview_id': PREVIEW_ID, 'consent': True}])
        self.expect(self.page.locator('#ai-history-list')).to_contain_text('synthetic-model')

    def test_saved_key_clears_password_and_never_enters_browser_storage(self):
        self.open_ai()
        self.select_scope()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-settings summary').click()
        self.page.locator('#ai-provider').select_option('deepseek')
        self.page.locator('#ai-model').fill('synthetic-deepseek-model')
        self.page.locator('#ai-api-key').fill(SYNTHETIC_KEY)
        self.page.locator('#ai-save-config').click()
        self.expect(self.page.locator('#ai-api-key')).to_have_value('')
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        storage = self.page.evaluate('JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})')
        self.assertNotIn(SYNTHETIC_KEY, storage)
        saved = [body for method, path, body in self.api_calls if method == 'POST' and path == '/api/ai/config']
        self.assertEqual(saved, [{'provider': 'deepseek', 'model': 'synthetic-deepseek-model', 'api_key': SYNTHETIC_KEY}])
        self.assertEqual(self.run_calls(), [])

    def test_switch_provider_updates_default_but_preserves_custom_model(self):
        self.open_ai()
        self.page.locator('#ai-settings summary').click()
        self.page.locator('#ai-model').fill('gpt-5.6-terra')
        self.page.locator('#ai-provider').select_option('deepseek')
        self.expect(self.page.locator('#ai-model')).to_have_value('deepseek-v4-pro')
        self.expect(self.page.locator('#ai-endpoint')).to_contain_text('api.deepseek.com')
        self.page.locator('#ai-provider').select_option('openai')
        self.expect(self.page.locator('#ai-model')).to_have_value('gpt-5.6-terra')
        self.page.locator('#ai-model').fill('synthetic-custom-model')
        self.page.locator('#ai-provider').select_option('deepseek')
        self.expect(self.page.locator('#ai-model')).to_have_value('synthetic-custom-model')
        self.assertEqual(self.run_calls(), [])

    def test_late_config_get_does_not_erase_unsaved_form_input(self):
        self.begin_delayed_config()
        self.page.locator('#ai-provider').select_option('deepseek')
        self.page.locator('#ai-model').fill('synthetic-unsaved-model')
        self.page.locator('#ai-api-key').fill(SYNTHETIC_KEY)
        self.release_delayed_get()
        self.expect(self.page.locator('#ai-provider')).to_have_value('deepseek')
        self.expect(self.page.locator('#ai-model')).to_have_value('synthetic-unsaved-model')
        self.expect(self.page.locator('#ai-api-key')).to_have_value(SYNTHETIC_KEY)
        self.assertEqual(self.run_calls(), [])

    def test_late_config_get_does_not_overwrite_newly_saved_connection(self):
        self.begin_delayed_config()
        self.page.locator('#ai-provider').select_option('deepseek')
        self.page.locator('#ai-model').fill('synthetic-newly-saved-model')
        self.page.locator('#ai-api-key').fill(SYNTHETIC_KEY)
        self.page.locator('#ai-save-config').click()
        self.expect(self.page.locator('#ai-config-badge')).to_contain_text('DeepSeek')
        self.expect(self.page.locator('#ai-api-key')).to_have_value('')
        self.release_delayed_get()
        self.expect(self.page.locator('#ai-provider')).to_have_value('deepseek')
        self.expect(self.page.locator('#ai-model')).to_have_value('synthetic-newly-saved-model')
        self.expect(self.page.locator('#ai-config-badge')).to_contain_text('DeepSeek')
        self.assertEqual(self.run_calls(), [])

    def test_late_config_get_does_not_restore_cleared_connection(self):
        self.begin_delayed_config()
        self.page.locator('#ai-clear-config').click()
        self.expect(self.page.locator('#ai-key-note')).to_contain_text('尚未保存密钥')
        self.release_delayed_get()
        self.expect(self.page.locator('#ai-config-badge')).to_have_text('尚未配置模型')
        self.expect(self.page.locator('#ai-key-note')).to_contain_text('尚未保存密钥')
        self.expect(self.page.locator('#ai-clear-config')).to_be_disabled()
        self.assertEqual(self.run_calls(), [])

    def test_late_history_report_cannot_replace_new_scope_preview(self):
        self.has_report = True
        self.current_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                              'date_to': '2026-09-01', 'focus': 'communication'}
        self.open_ai()
        self.expect(self.page.locator('#ai-history-list .history-item')).to_have_count(1)
        self.delay_get('/api/ai/reports/' + REPORT_ID, self.report())
        self.page.locator('#ai-history-list .history-item').click()
        self.page.wait_for_function('window.__syntheticDelayedGets.length === 1')
        self.page.locator('#ai-bundle').select_option('fixture_001')
        self.page.locator('#ai-date-from').fill('2026-02-01')
        self.page.locator('#ai-date-to').fill('2026-08-01')
        self.preview()
        self.release_delayed_get()
        self.expect(self.page.locator('#ai-preview')).to_be_visible()
        self.expect(self.page.locator('#ai-preview-title')).to_have_text('合成云舟群')
        self.expect(self.page.locator('#ai-result')).to_be_hidden()
        self.assertEqual(self.run_calls(), [])

    def test_mobile_analysis_and_maintenance_have_no_document_overflow(self):
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.open_ai()
        self.select_scope()
        self.preview()
        self.page.locator('#ai-settings summary').click()
        self.page.wait_for_load_state('networkidle')
        widths = self.page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
        self.assertLessEqual(widths['document'], widths['viewport'], widths)
        self.capture('analysis-ui-mobile.png')
        self.page.locator('[data-view="maintenance"]').click()
        self.expect(self.page.locator('#maintenance')).to_be_visible()
        widths = self.page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
        self.assertLessEqual(widths['document'], widths['viewport'], widths)

    def capture(self, name):
        # Reset test-driven scrolling/focus so fixed headers are photographed at
        # the top of a full-page artifact, not halfway through the document.
        self.page.evaluate('() => { document.activeElement?.blur(); window.scrollTo(0, 0); }')
        self.page.screenshot(path=str(fixtures.ARTIFACTS / name), full_page=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
