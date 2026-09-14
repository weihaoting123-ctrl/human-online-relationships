"""Fixed-schema diagnostics: no model text, secrets or dynamic field names."""
import importlib.util
import json
import unittest


class AnalysisErrorDetailsTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dashboard.analysis_errors'), 'Safe diagnostics missing')
        from dashboard import analysis_errors
        return analysis_errors

    def test_known_code_and_field_make_a_locally_authored_message(self):
        errors = self.module()
        error = errors.OutputValidationError('OUTPUT_EVIDENCE_REF', 'timeline.events[1].evidence[0].sample_index')
        self.assertIsInstance(error, errors.AnalysisError)
        detail = errors.public_error_detail(error.detail)
        self.assertEqual(detail['code'], 'OUTPUT_EVIDENCE_REF')
        self.assertEqual(detail['field'], 'timeline.events[1].evidence[0].sample_index')
        self.assertIn('证据', detail['message'])
        self.assertIn('不会自动重试', str(error))

    def test_untrusted_diagnostic_fields_are_not_reflected(self):
        errors = self.module()
        marker = '<img src=x onerror=alert(1)>SYNTHETIC-PRIVATE-RESPONSE'
        self.assertIsNone(errors.public_error_detail({'code': marker, 'field': marker, 'message': marker}))
        detail = errors.public_error_detail({'code': 'OUTPUT_TYPE', 'field': 'timeline.events',
            'message': marker, 'phase': marker, 'segment_index': marker, 'call_index': True, 'raw': marker})
        self.assertNotIn(marker, json.dumps(detail))
        self.assertNotIn('raw', detail)
        self.assertNotIn('phase', detail)
        self.assertNotIn('call_index', detail)
        for field in ('timeline.events[999999].title', 'timeline.' + marker, 'password', None):
            self.assertIsNone(errors.public_error_detail({'code': 'OUTPUT_TYPE', 'field': field}))

    def test_attempt_location_is_bounded_and_exception_text_not_saved(self):
        errors = self.module()
        detail = errors.public_error_detail({'code': 'OUTPUT_JSON', 'field': 'response', 'phase': 'segment',
                                              'segment_index': 17, 'call_index': 21})
        self.assertEqual(detail['segment_index'], 17)
        self.assertEqual(detail['call_index'], 21)
        self.assertIsNone(errors.exception_detail(ValueError('SYNTHETIC-PRIVATE-RESPONSE')))
        self.assertEqual(errors.exception_detail(errors.OutputValidationError('OUTPUT_JSON', 'response'))['code'], 'OUTPUT_JSON')


if __name__ == '__main__':
    unittest.main()
