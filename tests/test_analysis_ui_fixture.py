"""Synthetic HTTP framing/security checks for the UI fixture, without a browser."""
from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dashboard import app
from test_analysis_ui import create_analysis_fixture_server, stop_analysis_fixture_server

SYNTHETIC_TOKEN = 'synthetic-http-fixture-token'


class AnalysisFixtureHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.contacts_patch = mock.patch.object(app, 'CONTACTS_DIR', Path(self.temp.name) / 'contacts')
        self.contacts_patch.start()
        self.addCleanup(self.contacts_patch.stop)
        for name, value in [('DATA_DIR', Path(self.temp.name)),
                            ('SYNC_STATUS_PATH', Path(self.temp.name) / 'sync-status.json')]:
            patch = mock.patch.object(app, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.server = create_analysis_fixture_server(SYNTHETIC_TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(stop_analysis_fixture_server, self.server, self.thread)
        self.connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(self.connection.close)

    def request(self, path, headers=None):
        self.connection.request('GET', path, headers=headers or {})
        response = self.connection.getresponse()
        body = response.read()
        self.assertEqual(response.version, 11)
        self.assertEqual(int(response.getheader('Content-Length')), len(body))
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
        self.assertIn("connect-src 'self'", response.getheader('Content-Security-Policy'))
        return response, body

    def test_reuses_socket_without_changing_production_handler(self):
        self.assertEqual(app.DashboardHandler.protocol_version, 'HTTP/1.0')
        response, body = self.request('/')
        self.assertEqual(response.status, 200)
        self.assertIn('HttpOnly', response.getheader('Set-Cookie'))
        self.assertIn('SameSite=Strict', response.getheader('Set-Cookie'))
        self.assertNotIn(SYNTHETIC_TOKEN.encode(), body)
        socket = self.connection.sock
        response, _ = self.request('/app.js')
        self.assertEqual(response.status, 200)
        self.assertIs(self.connection.sock, socket)

    def test_unauthenticated_api_remains_forbidden(self):
        response, _ = self.request('/api/state')
        self.assertEqual(response.status, 403)

    def test_cross_site_host_remains_forbidden(self):
        response, _ = self.request('/', {'Host': 'synthetic-attacker.invalid'})
        self.assertEqual(response.status, 421)

    def test_session_cookie_still_authenticates_without_token_exposure(self):
        response, _ = self.request('/')
        cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        response, _ = self.request('/api/sync-status', {'Cookie': cookie})
        self.assertEqual(response.status, 200)

    def test_cross_site_post_is_rejected_before_import(self):
        with mock.patch.object(app, 'import_payload') as import_payload:
            self.connection.request('POST', '/api/import', body='{}', headers={
                'Content-Type': 'application/json', 'X-Local-Token': SYNTHETIC_TOKEN,
                'Origin': 'https://synthetic-attacker.invalid',
            })
            response = self.connection.getresponse()
            body = response.read()
        self.assertEqual(response.status, 403)
        self.assertEqual(int(response.getheader('Content-Length')), len(body))
        self.assertIn("connect-src 'self'", response.getheader('Content-Security-Policy'))
        import_payload.assert_not_called()

    def test_server_shutdown_is_bounded_and_reports_failure(self):
        release = threading.Event()
        server = mock.Mock()
        server.shutdown.side_effect = lambda: release.wait(timeout=2)
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        try:
            with self.assertRaisesRegex(RuntimeError, 'did not stop within its bound'):
                stop_analysis_fixture_server(server, thread, timeout=.01)
            server.server_close.assert_called_once_with()
        finally:
            release.set()
