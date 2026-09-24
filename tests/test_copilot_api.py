"""Loopback HTTP fixtures, never a real account or provider request."""
import copy
import json
import threading
import urllib.error
import urllib.request
from unittest import mock

from dashboard import app, analysis as ai
from tests.test_copilot_service import CopilotFixture, RESPONSE


class CopilotApiTests(CopilotFixture):
    def setUp(self):
        super().setUp()
        self.net.stop()
        self.patches = [mock.patch.object(app, 'DATA_DIR', self.root),
                        mock.patch.object(app, 'CONTACTS_DIR', self.contacts)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        # Explicit inventory refresh occurs before selecting a conversation.
        app.list_bundles()
        self.server = app.create_server(port=0, token='synthetic-copilot-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def http(self, path, body=None, **headers):
        defaults = {'X-Local-Token': 'synthetic-copilot-token', 'Origin': self.base, 'Content-Type': 'application/json'}
        defaults.update(headers)
        defaults = {key: value for key, value in defaults.items() if value is not None}
        request = urllib.request.Request(self.base + path, None if body is None else json.dumps(body).encode(), headers=defaults)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_status_is_safe_metadata_with_capabilities(self):
        status, value = self.http('/api/copilot/status')
        self.assertEqual(status, 200)
        self.assertTrue(value['enabled'])
        self.assertTrue(value['configured'])
        self.assertFalse(value['capabilities']['live_capture'])
        self.assertFalse(value['capabilities']['auto_send'])
        self.assertEqual(value['capabilities']['max_calls'], 1)
        self.assertEqual(len(value['conversations']), 2)
        for secret in ('LATEST_TEXT', 'OTHER_PERSON_SECRET', 'synthetic-test-key', 'sealed_key', 'note'):
            self.assertNotIn(secret, json.dumps(value))

    def test_security_guards_and_no_implicit_calls(self):
        for path, body in [('/api/copilot/status', None), ('/api/copilot/preview', self.request),
                           ('/api/copilot/run', {'consent': True})]:
            with self.subTest(path=path):
                self.assertEqual(self.http(path, body, **{'X-Local-Token': None})[0], 403)
                self.assertEqual(self.http(path, body, Host='evil.invalid')[0], 421)
                self.assertEqual(self.http(path, body, Origin='https://evil.invalid')[0], 403)
                if body is not None:
                    self.assertEqual(self.http(path, body, Origin=None)[0], 403)
        with mock.patch.object(ai, '_request_json') as transport:
            self.assertEqual(self.http('/api/copilot/preview', self.request)[0], 200)
            self.assertEqual(self.http('/api/copilot/run', {'consent': False})[0], 400)
        transport.assert_not_called()

    def test_single_sync_result_and_hidden_or_disabled_stale_preview(self):
        code, preview = self.http('/api/copilot/preview', self.request)
        self.assertEqual(code, 200)
        request = {'preview_id': preview['preview_id'], 'consent': True, 'binding_revision': 7}
        with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)) as transport:
            code, result = self.http('/api/copilot/run', request)
            self.assertEqual(code, 200)
            self.assertEqual(len(result['replies']), 3)
            self.assertEqual(self.http('/api/copilot/run', request)[0], 400)
        self.assertEqual(transport.call_count, 1)
        _, preview = self.http('/api/copilot/preview', self.request)
        app.library_service().update_conversation({'bundle_id': self.bundle.name, 'expected_version': 0, 'hidden': True})
        request['preview_id'] = preview['preview_id']
        with mock.patch.object(ai, '_request_json') as transport:
            self.assertEqual(self.http('/api/copilot/run', request)[0], 409)
            self.assertEqual(self.http('/api/copilot/preview', self.request)[0], 409)
        transport.assert_not_called()
        self.registry.update({'id': 'copilot', 'enabled': False, 'expected_version': 1})
        self.assertEqual(self.http('/api/copilot/preview', self.request)[0], 409)
        self.assertFalse(self.http('/api/copilot/status')[1]['enabled'])

    def test_request_validation_and_errors_do_not_expose_private_details(self):
        for body in ([], {'bundle_id': '../PRIVATE'}, {**self.request, 'date_to': 'PRIVATE'}):
            code, value = self.http('/api/copilot/preview', body)
            self.assertEqual(code, 400)
            self.assertNotIn('PRIVATE', json.dumps(value))
        _, preview = self.http('/api/copilot/preview', self.request)
        with mock.patch.object(ai, '_request_json', side_effect=ai.AnalysisError('PRIVATE key raw response')):
            code, value = self.http('/api/copilot/run', {'preview_id': preview['preview_id'], 'consent': True, 'binding_revision': 7})
        self.assertEqual(code, 400)
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertIn('可能已计费', value['error'])

    def test_analysis_disabled_blocks_preview_and_existing_confirmed_scope(self):
        _, preview = self.http('/api/copilot/preview', self.request)
        self.registry.update({'id': 'analysis', 'enabled': False, 'expected_version': 0})
        with mock.patch.object(ai, '_request_json') as transport:
            self.assertEqual(self.http('/api/copilot/preview', self.request)[0], 409)
            self.assertEqual(self.http('/api/copilot/run', {
                'preview_id': preview['preview_id'], 'consent': True, 'binding_revision': 7})[0], 409)
        transport.assert_not_called()

    def test_preview_and_run_never_refresh_other_archive_contents(self):
        with mock.patch.object(app, 'source_bundle_summaries', side_effect=AssertionError('Unselected archive read')):
            code, preview = self.http('/api/copilot/preview', self.request)
            self.assertEqual(code, 200)
            with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)):
                code, _ = self.http('/api/copilot/run', {
                    'preview_id': preview['preview_id'], 'consent': True, 'binding_revision': 7})
                self.assertEqual(code, 200)
