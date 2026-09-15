"""Atomic wire events through real parser and pipeline; urllib stays mocked."""
import copy
import json
import unittest
from unittest import mock

import test_relationship_timeline_ai as fixtures
from dashboard import analysis as ai
from dashboard import analysis_segments as segments


CANONICAL_KEYS = {'id', 'date_from', 'date_to', 'kind', 'title', 'summary',
                  'status', 'evidence_level', 'related_event_id', 'evidence'}
WIRE_KEYS = (CANONICAL_KEYS - {'kind', 'status', 'evidence_level'}) | {'event_state'}


def wire_event(event):
    event = copy.deepcopy(event)
    event['event_state'] = ':'.join(event.pop(key) for key in ('kind', 'status', 'evidence_level'))
    return event


class EventStatePipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TimelineWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.data = self.fixture.root
        self.fixture.root = ai._root(self.data)
        ai._write(self.fixture.root / 'config.json', self.fixture.config)
        self.fixture.prepared['scope'] = {**fixtures.SCOPE, 'analysis_mode': 'full'}
        self.fixture.prepared['counts']['eligible_messages'] = len(fixtures.SAMPLE)
        self.requests = []

    def model_response(self, content):
        if 'sample' in content:
            index = content['sample'][0]['sample_index']
            state = [('plan', 'planned'), ('update', 'in_progress'), ('outcome', 'realized')][index - 1]
            events = [fixtures.event(index, id='e1', kind=state[0], status=state[1])]
        else:
            events = [copy.deepcopy(event) for packet in content['segment_summaries']
                      for event in packet['timeline']['events']]
            events.sort(key=lambda event: (event['date_from'], event['evidence'][0]['sample_index']))
            for index, event in enumerate(events):
                self.assertEqual(set(event), CANONICAL_KEYS)
                if index and event['related_event_id'] is None:
                    event['related_event_id'] = events[index - 1]['id']
        return {**fixtures.BASE, 'timeline': fixtures.timeline([wire_event(event) for event in events])}

    def execute(self, response_builder=None):
        f = self.fixture
        response_builder = response_builder or self.model_response
        opener = mock.Mock()

        def open_request(request, *, timeout):
            self.assertEqual(timeout, 120)
            body = json.loads(request.data)
            self.requests.append(body)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(body['max_completion_tokens'], 2400)
            content = json.loads(body['messages'][1]['content'])
            result = response_builder(content)
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}],
            }).encode('utf-8')
            return response

        opener.open.side_effect = open_request
        with mock.patch.object(segments, 'CHUNK_MESSAGES', 1), \
                mock.patch.object(segments, 'MERGE_FANIN', 2), \
                mock.patch.object(ai.urllib.request, 'build_opener', return_value=opener):
            f.prepared['approved_calls'] = segments.approved_calls(f.root, f.config, f.prepared)
            result = segments.execute(f.root, f.config, 'synthetic-key', f.prepared, f.job)
        return result, opener

    def assert_canonical(self, result):
        events = result['timeline']['events']
        self.assertTrue(events)
        for event in events:
            self.assertEqual(set(event), CANONICAL_KEYS)
            self.assertNotIn('event_state', event)
        return events

    def test_wire_leaf_and_hierarchical_merge_seal_canonical_results_and_reuse_without_dispatch(self):
        result, opener = self.execute()
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(opener.open.call_count, 5)
        self.assertEqual(result['progress']['attempted_calls'], 5)
        self.assertEqual(result['progress']['completed_calls'], 5)
        events = self.assert_canonical(result['result'])
        self.assertEqual([event['id'] for event in events], ['s1-e1', 's2-e1', 's3-e1'])
        self.assertEqual([event['status'] for event in events], ['planned', 'in_progress', 'realized'])
        self.assertEqual([event['related_event_id'] for event in events], [None, 's1-e1', 's2-e1'])
        self.assertTrue(result['result']['timeline']['coverage']['complete'])
        records = [ai._read(path) for path in self.fixture.root.glob('call-*.json')]
        self.assertEqual(len(records), 5)
        for record in records:
            self.assertEqual(record['state'], 'completed')
            self.assert_canonical(ai._unseal(record['sealed_result']))
        report = ai.report(self.data, result['report_id'])
        self.assertEqual(self.assert_canonical(report['result']), events)
        self.assertEqual(self.assert_canonical(ai.job_status(self.data, self.fixture.job['job_id'])['result']), events)
        for request in self.requests:
            self.assertIn('event_state', request['messages'][0]['content'])
        again, cached_transport = self.execute()
        self.assertEqual(again['state'], 'completed', again)
        cached_transport.open.assert_not_called()
        self.assertEqual(again['progress']['attempted_calls'], 0)
        self.assertEqual(again['progress']['cached_calls'], 5)
        self.assertEqual(self.assert_canonical(again['result']), events)

    def check_first_failure(self, patch, code, field='timeline.events[0].event_state'):
        marker = 'SYNTHETIC_INVALID_RESPONSE_NOT_FOR_STORAGE'

        def invalid(content):
            result = self.model_response(content)
            event = result['timeline']['events'][0]
            self.assertEqual(set(event), WIRE_KEYS)
            event.update(patch)
            result['summary'] = marker
            return result

        result, opener = self.execute(invalid)
        self.assertEqual(result['state'], 'error', result)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(result['progress']['attempted_calls'], 1)
        self.assertEqual(result['progress']['completed_calls'], 0)
        self.assertEqual(result['progress']['completed_segments'], 0)
        self.assertNotIn('result', result)
        self.assertNotIn('report_id', result)
        self.assertEqual(result['error_detail']['code'], code)
        self.assertEqual(result['error_detail']['field'], field)
        self.assertEqual(result['error_detail']['phase'], 'segment')
        self.assertEqual(result['error_detail']['segment_index'], 1)
        self.assertEqual(result['error_detail']['call_index'], 1)
        paths = list(self.fixture.root.glob('call-*.json'))
        self.assertEqual(len(paths), 1)
        record = ai._read(paths[0])
        self.assertEqual(record['state'], 'error')
        self.assertEqual(record['error_detail']['code'], code)
        self.assertEqual(record['error_detail']['field'], field)
        self.assertNotIn('sealed_result', record)
        self.assertEqual(list(self.fixture.root.glob('report-*.json')), [])
        for path in self.fixture.root.glob('*.json'):
            self.assertNotIn(marker, path.read_text(encoding='utf-8'))
        self.assertEqual(ai.job_status(self.data, self.fixture.job['job_id'])['error_detail'],
                         result['error_detail'])
        again, repeated_transport = self.execute(invalid)
        repeated_transport.open.assert_not_called()
        self.assertEqual(again['state'], 'error')
        self.assertEqual(again['progress']['attempted_calls'], 0)
        self.assertIn('此前失败', again['error'])
        self.assertEqual(ai._read(paths[0]), record)
        self.assertEqual(list(self.fixture.root.glob('report-*.json')), [])

    def test_invalid_atomic_combination_fails_first_attempt_without_result_seal_or_retry(self):
        self.check_first_failure({'event_state': 'plan:realized:reported'}, 'OUTPUT_ENUM')

    def test_nonstring_atomic_state_fails_first_attempt_without_result_seal_or_retry(self):
        self.check_first_failure({'event_state': ['plan', 'planned', 'reported']}, 'OUTPUT_TYPE')

    def test_mixed_wire_and_legacy_fields_fail_first_attempt_without_result_seal_or_retry(self):
        self.check_first_failure({'kind': 'plan', 'status': 'planned', 'evidence_level': 'reported'},
                                 'OUTPUT_FIELDS', 'timeline.events[0]')


if __name__ == '__main__':
    unittest.main(verbosity=2)
