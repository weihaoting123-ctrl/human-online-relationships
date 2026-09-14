"""Invented provider responses only; no network, credentials or chat archive."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai
from dashboard.analysis_errors import OutputValidationError


class ResponseDiagnosticsTests(unittest.TestCase):
    def request(self, body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(ai.urllib.request, 'build_opener', return_value=opener):
            return ai._request_result({'provider': 'openai', 'model': 'synthetic-model'},
                                      'synthetic-key', {}, 'JSON synthetic contract')

    def test_provider_envelope_json_content_and_incomplete_have_distinct_codes(self):
        cases = [(b'not json SYNTHETIC_RAW_DO_NOT_ECHO', 'OUTPUT_RESPONSE'),
                 (b'{"choices": []}', 'OUTPUT_RESPONSE'),
                 (b'{"choices": [{"message": {"content": 7}}]}', 'OUTPUT_RESPONSE'),
                 (json.dumps({'choices': [{'message': {'content': 'SYNTHETIC_RAW_DO_NOT_ECHO'}}]}).encode(), 'OUTPUT_JSON'),
                 (b'{"choices": [{"finish_reason":"length"}]}', 'OUTPUT_INCOMPLETE')]
        for body, code in cases:
            with self.subTest(code=code, body=body):
                with self.assertRaises(OutputValidationError) as caught:
                    self.request(body)
                self.assertEqual(caught.exception.detail['code'], code)
                self.assertNotIn('SYNTHETIC_RAW_DO_NOT_ECHO', str(caught.exception))

    def test_report_type_diagnostics_are_local_fields_only(self):
        for value, field in [(None, 'report'), ({}, 'summary'),
                             ({'summary': 'synthetic', 'observations': 42}, 'observations')]:
            with self.subTest(field=field):
                with self.assertRaises(OutputValidationError) as caught:
                    ai._safe_result(value)
                self.assertEqual(caught.exception.detail['field'], field)

    def test_response_only_details_are_allowlisted_and_do_not_rewrite_history(self):
        scratch = Path(__file__).resolve().parents[1] / 'scripts' / 'tmp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            root = ai._root(temp)
            identity = 'c' * 48
            detail = {'code': 'OUTPUT_TYPE', 'field': 'timeline.events',
                      'message': 'SYNTHETIC_RAW_DO_NOT_ECHO', 'raw': 'SYNTHETIC_RAW_DO_NOT_ECHO'}
            for name in ('job', 'report'):
                path = root / f'{name}-{identity}.json'
                ai._write(path, {'job_id': identity, 'state': 'error', 'error_detail': detail})
                before = path.read_bytes()
                value = ai.job_status(temp, identity) if name == 'job' else ai.report(temp, identity)
                self.assertEqual(value['error_detail']['message'], '字段类型不符合要求')
                self.assertNotIn('SYNTHETIC_RAW_DO_NOT_ECHO', json.dumps(value))
                self.assertEqual(before, path.read_bytes())
            self.assertIn('error_detail', ai.jobs(temp)['jobs'][0])
            ai._write(root / f'job-{identity}.json', {'state': 'error', 'error_detail': {'code': 'UNKNOWN', 'field': 'raw'}})
            self.assertNotIn('error_detail', ai.job_status(temp, identity))


if __name__ == '__main__':
    unittest.main()
