"""Synthetic pipeline failures and billing guards, with provider calls mocked."""
import json
import unittest
from unittest import mock

import test_relationship_timeline_ai as fixtures
from dashboard import analysis as ai
from dashboard import analysis_segments as segments
from dashboard.analysis_errors import OutputValidationError


class PipelineDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TimelineWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def failure(self, *_args):
        raise OutputValidationError('OUTPUT_EVIDENCE_REF', 'timeline.events[0].evidence[0].sample_index')

    def test_first_failure_records_attempt_diagnostic_and_prevents_automatic_retry(self):
        f = self.fixture
        result = f.execute(self.failure, lambda *_: dict(fixtures.BASE))
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['progress']['attempted_calls'], 1)
        self.assertEqual(result['progress']['completed_calls'], 0)
        self.assertEqual(result['error_detail']['segment_index'], 1)
        self.assertEqual(result['error_detail']['call_index'], 1)
        record = ai._read(next(f.root.glob('call-*.json')))
        self.assertEqual(record['error_detail']['code'], 'OUTPUT_EVIDENCE_REF')
        self.assertNotIn('sealed_result', record)
        with mock.patch.object(ai, '_request_result', side_effect=AssertionError('no network')):
            again = f.execute(self.failure, lambda *_: dict(fixtures.BASE))
        self.assertEqual(again['progress']['attempted_calls'], 0)
        self.assertNotIn('error_detail', again)
        self.assertIn('此前失败', again['error'])

    def test_partial_leaf_and_merge_keep_specific_location_and_completed_results(self):
        f = self.fixture
        def leaf(config, key, request):
            if request['segment']['index'] == 2:
                return self.failure()
            return f.cloud(config, key, request)
        partial = f.execute(leaf, lambda *_: dict(fixtures.BASE))
        report = ai._read(f.root / ('report-' + partial['report_id'] + '.json'))
        self.assertEqual(report['error_detail']['segment_index'], 2)
        self.assertEqual(report['failed_segment'], 2)
        self.assertEqual(report['coverage']['completed_segments'], 1)
        self.assertEqual(len(report['result']['timeline']['events']), 1)
        self.assertEqual(partial['progress']['attempted_calls'], 2)

    def test_merge_failure_records_phase_and_cached_replay_makes_no_attempt(self):
        f = self.fixture
        merged = f.execute(f.cloud, self.failure)
        self.assertEqual(merged['error_detail']['phase'], 'merge')
        self.assertEqual(merged['error_detail']['call_index'], 4)
        self.assertNotIn('segment_index', merged['error_detail'])
        report = ai._read(f.root / ('report-' + merged['report_id'] + '.json'))
        self.assertIsNone(report.get('failed_segment'))
        self.assertEqual(report['error_detail'], merged['error_detail'])
        again = f.execute(f.cloud, self.failure)
        self.assertEqual(again['progress']['cached_calls'], 3)
        self.assertEqual(again['progress']['attempted_calls'], 0)

    def test_unknown_exception_does_not_store_provider_text(self):
        f = self.fixture
        def fail(*_):
            raise ValueError('SYNTHETIC_PRIVATE_RESPONSE')
        result = f.execute(fail, lambda *_: dict(fixtures.BASE))
        self.assertNotIn('error_detail', result)
        for path in list(f.root.glob('call-*.json')) + list(f.root.glob('job-*.json')):
            self.assertNotIn('SYNTHETIC_PRIVATE_RESPONSE', path.read_text(encoding='utf-8'))

    def test_preview_estimate_accounts_for_longer_prompt_contracts(self):
        f = self.fixture
        baseline = segments.plan(f.root, f.config, f.prepared)
        with mock.patch.object(ai, 'SYSTEM_PROMPT', ai.SYSTEM_PROMPT + '合成' * 1000):
            expanded = segments.plan(f.root, f.config, f.prepared)
        self.assertGreater(expanded['estimated_input_tokens'], baseline['estimated_input_tokens'])
        self.assertIn('提示版本', baseline['estimate_note'])


if __name__ == '__main__':
    unittest.main()
