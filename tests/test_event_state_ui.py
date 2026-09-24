"""Synthetic browser coverage for atomic event-state diagnostic paths."""
import os
import unittest

import test_analysis_diagnostics_ui as diagnostics
import test_analysis_ui as fixtures


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1',
                     'Opt-in synthetic browser tests')
class EventStateBrowserTests(unittest.TestCase):
    # Reuse only the isolated lifecycle and helpers, not existing test methods.
    setUpClass = classmethod(diagnostics.AnalysisDiagnosticsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(diagnostics.AnalysisDiagnosticsBrowserTests.tearDownClass.__func__)
    setUp = diagnostics.AnalysisDiagnosticsBrowserTests.setUp
    tearDown = diagnostics.AnalysisDiagnosticsBrowserTests.tearDown
    route_request = diagnostics.AnalysisDiagnosticsBrowserTests.route_request
    job = diagnostics.AnalysisDiagnosticsBrowserTests.job
    result = diagnostics.AnalysisDiagnosticsBrowserTests.result
    plan = diagnostics.AnalysisDiagnosticsBrowserTests.plan
    metrics = diagnostics.AnalysisDiagnosticsBrowserTests.metrics
    report = diagnostics.AnalysisDiagnosticsBrowserTests.report
    open_ai = diagnostics.AnalysisDiagnosticsBrowserTests.open_ai
    select_scope = diagnostics.AnalysisDiagnosticsBrowserTests.select_scope
    preview = diagnostics.AnalysisDiagnosticsBrowserTests.preview
    run_calls = diagnostics.AnalysisDiagnosticsBrowserTests.run_calls
    start_failed_run = diagnostics.AnalysisDiagnosticsBrowserTests.start_failed_run
    assert_consent_cleared = diagnostics.AnalysisDiagnosticsBrowserTests.assert_consent_cleared

    def check_first_failure(self, code, message, field='timeline.events[0].event_state'):
        self.no_report = True
        self.progress_overrides = {'completed_calls': 0, 'completed_segments': 0,
                                   'cached_calls': 0, 'attempted_calls': 1, 'stage': 'segments'}
        self.job_overrides = {
            'report_id': None,
            'error': f'模型输出校验失败：{field}，{message}；本次不会自动重试',
            'error_detail': {'code': code, 'field': field,
                             'message': fixtures.XSS + 'REMOTE_DETAIL_MUST_NOT_RENDER',
                             'phase': 'segment', 'segment_index': 1, 'call_index': 1},
        }
        self.start_failed_run()
        feedback = self.page.locator('#ai-feedback').inner_text()
        self.assertIn(field, feedback)
        self.assertIn(message, feedback)
        self.assertIn('第 1 段', feedback)
        self.assertIn('第 1 次计划调用', feedback)
        self.assertNotIn('REMOTE_DETAIL_MUST_NOT_RENDER', feedback)
        self.assertNotIn(fixtures.XSS, feedback)
        self.assertEqual(feedback.count('自动重试'), 1)
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('新调用尝试 1 次')
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('已完成调用（含复用）0 / 1')
        self.assert_consent_cleared()
        self.page.locator('#ai-refresh-history').click()
        self.expect(self.page.locator('#ai-history-list')).to_contain_text('尚无分析记录')
        self.assert_consent_cleared()
        self.assertEqual(len(self.run_calls()), 1)

    def test_enum_failure_uses_safe_atomic_field_and_requires_fresh_consent(self):
        self.check_first_failure('OUTPUT_ENUM', '字段值不在允许的枚举范围内')

    def test_type_failure_uses_safe_atomic_field_and_requires_fresh_consent(self):
        self.check_first_failure('OUTPUT_TYPE', '字段类型不符合要求')

    def test_fields_failure_uses_safe_atomic_field_and_requires_fresh_consent(self):
        self.check_first_failure('OUTPUT_FIELDS', '缺少必填字段或包含不允许的字段')

    def test_legacy_status_field_keeps_context_and_consent_guards(self):
        self.check_first_failure('OUTPUT_ENUM', '字段值不在允许的枚举范围内',
                                 'timeline.events[0].status')

    def test_atomic_field_suffixes_and_invalid_indices_do_not_authorize_context(self):
        self.has_report = True
        self.job_state = 'error'
        self.current_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                              'date_to': '2026-09-01', 'focus': 'communication'}
        self.open_ai()
        # A history click starts an asynchronous read. Keep that boundary
        # observable even on fast machines; no external request is introduced.
        self.page.evaluate("""() => {
            const fetch = window.fetch.bind(window);
            window.fetch = async (...args) => {
                const response = await fetch(...args);
                if (String(args[0]).startsWith('/api/ai/reports/')) {
                    await new Promise(resolve => setTimeout(resolve, 75));
                }
                return response;
            };
        }""")
        invalid_fields = [
            'timeline.events[0].event_state' + fixtures.XSS,
            'timeline.events[0].event_state.__proto__',
            'timeline.events[0].event_state[0]',
            'timeline.events[1200].event_state',
            'timeline.events[-1].event_state',
        ]
        for index, field in enumerate(invalid_fields):
            with self.subTest(field=field):
                reason_text = f'本地合成错误 {index}'
                self.report_overrides = {
                    'stop_reason': reason_text,
                    'error_detail': {'code': 'OUTPUT_ENUM', 'field': field,
                                     'message': fixtures.XSS, 'phase': 'merge', 'call_index': 4},
                }
                self.page.locator('#ai-history-list .history-item').click()
                # Wait for this response, not an empty node or the previous
                # iteration's reason. Click auto-wait does not await fetch.
                self.expect(self.page.locator('#ai-result-stop-reason')).to_contain_text(reason_text)
                reason = self.page.locator('#ai-result-stop-reason').inner_text()
                self.assertIn(reason_text, reason)
                self.assertNotIn('第 4 次计划调用', reason)
                self.assertNotIn(field, reason)
                self.assertEqual(self.page.locator('#ai-result img, #ai-result script').count(), 0)
                self.assert_consent_cleared()
        self.assertEqual(self.run_calls(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
