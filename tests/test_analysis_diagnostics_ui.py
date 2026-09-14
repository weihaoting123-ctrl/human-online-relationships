"""Synthetic diagnostic UI checks; cloud responses are intercepted locally."""
from __future__ import annotations

import os
import unittest
from urllib.parse import urlparse

import test_analysis_ui as fixtures


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1',
                     'Opt-in synthetic browser tests')
class AnalysisDiagnosticsBrowserTests(unittest.TestCase):
    # Reuse the isolated fixture lifecycle without inheriting its test methods.
    setUpClass = classmethod(fixtures.AnalysisBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.AnalysisBrowserTests.tearDownClass.__func__)
    tearDown = fixtures.AnalysisBrowserTests.tearDown
    result = fixtures.AnalysisBrowserTests.result
    plan = fixtures.AnalysisBrowserTests.plan
    metrics = fixtures.AnalysisBrowserTests.metrics
    report = fixtures.AnalysisBrowserTests.report
    open_ai = fixtures.AnalysisBrowserTests.open_ai
    select_scope = fixtures.AnalysisBrowserTests.select_scope
    preview = fixtures.AnalysisBrowserTests.preview
    run_calls = fixtures.AnalysisBrowserTests.run_calls

    def setUp(self):
        self.job_overrides = {}
        self.progress_overrides = {}
        self.record_attempts = True
        self.no_report = False
        fixtures.AnalysisBrowserTests.setUp(self)

    def route_request(self, route):
        path = urlparse(route.request.url).path
        fixtures.AnalysisBrowserTests.route_request(self, route)
        if path == '/api/ai/run' and self.no_report:
            self.has_report = False

    def job(self):
        job = fixtures.AnalysisBrowserTests.job(self)
        if self.record_attempts:
            job['progress']['attempted_calls'] = 1
        job['progress'].update(self.progress_overrides)
        job.update(self.job_overrides)
        return job

    def start_failed_run(self):
        self.job_state = 'error'
        self.open_ai()
        self.select_scope()
        self.preview()
        self.page.locator('#ai-consent').check()
        self.page.locator('#ai-run-button').click()
        self.expect(self.page.locator('#ai-feedback')).to_have_class('ai-feedback is-error')

    def assert_consent_cleared(self):
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-retry-uncertain')).not_to_be_checked()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()

    def test_first_segment_failure_shows_attempt_without_claiming_success_or_billing(self):
        self.no_report = True
        self.progress_overrides = {'completed_calls': 0, 'completed_segments': 0,
                                   'cached_calls': 0, 'attempted_calls': 1, 'stage': 'segments'}
        self.job_overrides = {
            'report_id': None,
            'error': '模型输出校验失败：timeline.events[0].status，字段值不在允许的枚举范围内；本次不会自动重试',
            'error_detail': {'code': 'OUTPUT_ENUM', 'field': 'timeline.events[0].status',
                             'message': '字段值不在允许的枚举范围内',
                             'phase': 'segment', 'segment_index': 1, 'call_index': 1},
        }
        self.start_failed_run()
        self.expect(self.page.locator('#ai-job-progress')).to_be_visible()
        self.expect(self.page.locator('#ai-progress-title')).to_have_text('按时间顺序分析分段')
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('已完成调用（含复用）0 / 1')
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('新调用尝试 1 次')
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('已复用 0 次')
        self.expect(self.page.locator('#ai-progress-note')).to_contain_text('不代表已送达或已计费')
        feedback = self.page.locator('#ai-feedback').inner_text()
        self.assertIn('timeline.events[0].status', feedback)
        self.assertIn('字段值不在允许的枚举范围内', feedback)
        self.assertIn('第 1 段', feedback)
        self.assertIn('第 1 次计划调用', feedback)
        self.assertIn('仍可能产生费用', feedback)
        self.assertEqual(feedback.count('自动重试'), 1)
        self.assert_consent_cleared()
        self.assertEqual(len(self.run_calls()), 1)
        fixtures.AnalysisBrowserTests.capture(self, 'analysis-diagnostics-first-failure.png')
        self.page.locator('#ai-refresh-history').click()
        self.expect(self.page.locator('#ai-history-list')).to_contain_text('尚无分析记录')
        self.assertEqual(len(self.run_calls()), 1)

    def test_partial_merge_history_preserves_specific_local_diagnostic_and_recheck_consent(self):
        self.current_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                              'date_to': '2026-09-01', 'focus': 'communication',
                              'analysis_mode': 'full'}
        self.has_report = True
        self.job_state = 'error'
        self.blocked_calls = 1
        self.report_overrides = {
            'stop_reason': '模型输出校验失败：timeline.events[0].related_event_id，汇总修改了原事件，或更改了已有的非空关联；本次不会自动重试',
            'error_detail': {'code': 'OUTPUT_MERGE_CHANGED',
                             'field': 'timeline.events[0].related_event_id',
                             'message': '不应直接显示的任意远端描述',
                             'phase': 'merge', 'call_index': 4},
            'coverage': {'eligible_messages': 7, 'analyzed_messages': 7,
                         'total_segments': 3, 'completed_segments': 3, 'complete': False},
        }
        self.open_ai()
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(self.page.locator('#ai-result-status')).to_contain_text('分段已完成，汇总尚未完成')
        reason = self.page.locator('#ai-result-stop-reason').inner_text()
        self.assertIn('timeline.events[0].related_event_id', reason)
        self.assertIn('汇总修改了原事件', reason)
        self.assertIn('汇总 · 第 4 次计划调用', reason)
        self.assertIn('仍可能产生费用', reason)
        self.assertEqual(reason.count('自动重试'), 1)
        self.assertNotIn('不应直接显示', reason)
        self.assert_consent_cleared()
        fixtures.AnalysisBrowserTests.capture(self, 'analysis-diagnostics-merge-history.png')
        self.page.locator('#ai-recheck-partial').click()
        self.expect(self.page.locator('#ai-preview')).to_be_visible()
        self.expect(self.page.locator('#ai-preview')).to_contain_text('提示版本变化后，旧缓存可能无法复用')
        self.assert_consent_cleared()
        self.assertEqual(self.run_calls(), [])

    def test_legacy_job_attempts_are_unknown_and_history_without_detail_remains_usable(self):
        self.record_attempts = False
        self.no_report = True
        self.job_overrides = {'report_id': None, 'error': '分析结果格式不完整；本次不会自动重试'}
        self.start_failed_run()
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('尝试次数未记录')
        self.expect(self.page.locator('#ai-progress-detail')).not_to_contain_text('新调用尝试 0 次')
        self.assert_consent_cleared()
        self.has_report = True
        self.report_overrides = {'failed_segment': 2}
        self.page.locator('#ai-refresh-history').click()
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(self.page.locator('#ai-result-stop-reason')).to_contain_text('这份历史报告未记录具体原因')
        self.expect(self.page.locator('#ai-result-stop-reason')).to_contain_text('第 2 段')
        self.expect(self.page.locator('#ai-result-stop-reason')).to_contain_text('仍可能产生费用')
        self.assert_consent_cleared()
        self.assertEqual(len(self.run_calls()), 1)

    def test_reused_successes_do_not_inflate_recorded_new_attempts(self):
        self.current_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                              'date_to': '2026-09-01', 'focus': 'communication',
                              'analysis_mode': 'full'}
        self.job_state = 'running'
        self.recovery_job = True
        self.progress_overrides = {'completed_calls': 3, 'completed_segments': 3,
                                   'cached_calls': 3, 'attempted_calls': 0, 'stage': 'merge'}
        self.open_ai()
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('已完成调用（含复用）3 / 4')
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('新调用尝试 0 次')
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('已复用 3 次')
        self.assert_consent_cleared()
        self.assertEqual(self.run_calls(), [])

    def test_failed_merge_progress_no_longer_claims_to_be_running(self):
        self.no_report = True
        self.plan_overrides = {'segments': 3, 'total_calls': 4, 'mode': 'full'}
        self.progress_overrides = {'completed_calls': 3, 'completed_segments': 3,
                                   'cached_calls': 1, 'attempted_calls': 3, 'stage': 'merge'}
        self.job_overrides = {
            'report_id': None,
            'error': '模型输出校验失败：response，服务商标记输出未完成、被截断或被拦截；本次不会自动重试',
            'error_detail': {'code': 'OUTPUT_INCOMPLETE', 'field': 'response',
                             'phase': 'merge', 'call_index': 4},
        }
        self.start_failed_run()
        self.expect(self.page.locator('#ai-progress-detail')).to_contain_text('汇总未完成')
        self.expect(self.page.locator('#ai-progress-detail')).not_to_contain_text('正在整合')
        self.expect(self.page.locator('#ai-progress-title')).not_to_contain_text('正在汇总')
        self.expect(self.page.locator('#ai-feedback')).to_contain_text('汇总 · 第 4 次计划调用')
        self.assert_consent_cleared()
        self.assertEqual(len(self.run_calls()), 1)

    def test_untrusted_detail_is_ignored_and_legacy_reason_is_rendered_only_as_text(self):
        self.has_report = True
        self.job_state = 'error'
        self.current_scope = {'bundle_id': 'fixture_000', 'date_from': '2026-01-01',
                              'date_to': '2026-09-01', 'focus': 'communication'}
        invalid_details = [
            {'code': 'OUTPUT_FUTURE_UNKNOWN', 'field': 'timeline.events[0].status',
             'message': fixtures.XSS, 'phase': 'merge', 'call_index': 4},
            {'code': 'OUTPUT_ENUM', 'field': 'timeline.events[0].' + fixtures.XSS,
             'message': fixtures.XSS, 'phase': 'merge', 'call_index': 4},
            {'code': 'OUTPUT_ENUM', 'field': 'timeline.events[1200].status',
             'message': fixtures.XSS, 'phase': 'merge', 'call_index': 4},
            {'code': 'OUTPUT_ENUM', 'field': 'timeline.no_contact_reason.evidence[3].date',
             'message': fixtures.XSS, 'phase': 'merge', 'call_index': 4},
        ]
        self.open_ai()
        for index, detail in enumerate(invalid_details):
            with self.subTest(detail=detail):
                reason_text = f'旧版合成错误 {index} ' + fixtures.XSS
                self.report_overrides = {'stop_reason': reason_text,
                                         'error_detail': detail}
                self.page.locator('#ai-history-list .history-item').click()
                self.expect(self.page.locator('#ai-result-stop-reason')).to_contain_text(reason_text)
                reason = self.page.locator('#ai-result-stop-reason').inner_text()
                self.assertNotIn('第 4 次计划调用', reason)
                self.assertNotIn('OUTPUT_FUTURE_UNKNOWN', reason)
                self.assertEqual(self.page.locator('#ai-result img, #ai-result script').count(), 0)
                self.assert_consent_cleared()
        self.assertEqual(self.run_calls(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
