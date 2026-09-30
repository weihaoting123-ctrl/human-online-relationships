"""Durable task and report navigation with wholly synthetic records."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai


class AnalysisHistoryTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / 'scripts/tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='history-review-', dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.root = ai._root(self.data)

    def seed_report(self, index):
        ai._write(self.root / f'report-{index:048x}.json', {
            'id': f'{index:048x}', 'created_at': f'2099-01-{index % 28 + 1:02d}T00:00:00Z',
            'provider': 'openai', 'model': 'synthetic-model',
            'scope': {'bundle_id': 'synthetic-a' if index % 2 else 'synthetic-b',
                      'focus': 'relation_overview'}, 'sample_messages': 1,
            'result': {'summary': 'PRIVATE_SYNTHETIC_RESULT'},
        })

    def test_report_pagination_can_reach_older_than_one_hundred(self):
        for index in range(107):
            self.seed_report(index)
        first = ai.history(self.data, offset=0, limit=20)
        self.assertEqual(first['total'], 107)
        self.assertEqual(first['next_offset'], 20)
        found = []
        for offset in range(0, 107, 20):
            page = ai.history(self.data, offset=offset, limit=20)
            found.extend(item['id'] for item in page['reports'])
            self.assertNotIn('PRIVATE_SYNTHETIC_RESULT', str(page))
        self.assertEqual(len(set(found)), 107)
        self.assertIsNone(ai.history(self.data, offset=100, limit=20)['next_offset'])

    def test_metadata_search_filters_before_paging(self):
        for index in range(7):
            self.seed_report(index)
        page = ai.history(self.data, limit=2, query='synthetic-a')
        self.assertEqual(page['total'], 3)
        self.assertTrue(all(item['scope']['bundle_id'] == 'synthetic-a' for item in page['reports']))
        self.assertEqual(page['next_offset'], 2)

    def test_failed_zero_segment_and_restart_unknown_tasks_remain_listed(self):
        for index, state in enumerate(('error', 'running')):
            ai._write(self.root / f'job-{index:048x}.json', {
                'job_id': f'{index:048x}', 'state': state, 'created_at': '2099-01-01T00:00:00Z',
                'scope': {'bundle_id': 'synthetic-a'},
                'progress': {'attempted_calls': 1, 'completed_segments': 0},
                'error': '合成固定错误',
            })
        with mock.patch.object(ai, '_ACTIVE_JOBS', set()):
            page = ai.jobs(self.data, limit=1)
            other = ai.jobs(self.data, offset=1, limit=1)
        self.assertEqual(page['total'], 2)
        self.assertTrue(all(item['state'] == 'error' for item in page['jobs'] + other['jobs']))
        self.assertTrue(any('服务已重启' in item['error'] for item in page['jobs'] + other['jobs']))
        self.assertEqual(ai.history(self.data)['reports'], [])

    def test_page_request_rejects_unknown_duplicate_and_unbounded_parameters(self):
        self.assertEqual(ai.history_request('offset=20&limit=10&q=synthetic'),
                         {'offset': 20, 'limit': 10, 'query': 'synthetic'})
        for query in ('offset=-1', 'limit=101', 'limit=0', 'offset=1&offset=2',
                      'path=x', 'q=' + 'x' * 201, 'offset=true'):
            with self.subTest(query=query), self.assertRaises(ai.AnalysisError):
                ai.history_request(query)
        for kwargs in ({'offset': True}, {'limit': False}, {'query': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ai.AnalysisError):
                ai.history(self.data, **kwargs)


if __name__ == '__main__':
    unittest.main()
