"""Relationship timeline HTTP integration using an isolated synthetic archive."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from dashboard import app


class RelationshipTimelineApiTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[1] / 'scripts' / 'tmp'
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='timeline-api-', dir=artifacts)
        self.data = Path(self.temp.name) / 'data'
        self.contacts = self.data / 'contacts'
        self.bundle = self.contacts / 'synthetic-timeline'
        self.bundle.mkdir(parents=True)
        self.path = self.bundle / 'messages.json'
        self.path.write_text(json.dumps({
            'contact_display': '合成关系',
            'messages': [
                {'timestamp': '2024-01-01T10:00:00', 'sender': 'me', 'type': 'text', 'content': 'SYNTHETIC_PRIVATE_BODY_A'},
                {'timestamp': '2024-01-02T10:00:00', 'sender': 'them', 'type': 'voice', 'transcript': 'SYNTHETIC_TRANSCRIPT_B'},
                {'timestamp': '2024-02-15T10:00:00', 'sender': 'me', 'type': 'image', 'content': 'SYNTHETIC_IMAGE_PATH'},
                {'timestamp': '2024-03-01T10:00:00', 'sender': 'them', 'type': 'system', 'content': 'SYSTEM_NOT_CONTACT'},
            ],
        }), encoding='utf-8')
        self.original = self.path.read_bytes()
        self.patches = [
            mock.patch.object(app, 'DATA_DIR', self.data),
            mock.patch.object(app, 'CONTACTS_DIR', self.contacts),
            mock.patch.object(app, 'exporter_status', return_value={}),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.server = app.create_server(port=0)
        self.assertNotEqual(self.server.server_port, 8765)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        with urllib.request.urlopen(self.base) as response:
            self.cookie = response.headers['Set-Cookie'].split(';')[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.assertEqual(self.original, self.path.read_bytes())
        self.temp.cleanup()

    def request(self, path, authenticated=True):
        request = urllib.request.Request(self.base + path, headers={'Cookie': self.cookie} if authenticated else {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_detail_exposes_content_free_relationship_timeline_and_keeps_old_charts(self):
        with mock.patch.object(app.scoped_ai, '_cloud', side_effect=AssertionError('Cloud must not run')):
            status, response = self.request('/api/bundles/synthetic-timeline')
        self.assertEqual(status, 200)
        bundle = response['bundle']
        self.assertIn('daily', bundle['timeline'], 'Existing activity charts retain their original contract')
        self.assertIn('relationship_timeline', bundle)
        timeline = bundle['relationship_timeline']
        self.assertEqual(timeline['version'], 1)
        self.assertEqual(timeline['last_contact']['last_record_at'][:10], '2024-02-15')
        self.assertGreaterEqual(timeline['last_contact']['elapsed_days'], 0)
        self.assertEqual(timeline['last_contact']['reason_status'], 'unknown')
        self.assertEqual(timeline['scope']['message_count'], 3)
        self.assertTrue(timeline['gaps'])
        serialized = json.dumps(timeline, ensure_ascii=False)
        for private in ('SYNTHETIC_PRIVATE_BODY_A', 'SYNTHETIC_TRANSCRIPT_B', 'SYNTHETIC_IMAGE_PATH', 'SYSTEM_NOT_CONTACT', '合成关系'):
            self.assertNotIn(private, serialized)
        self.assertFalse(bundle['privacy']['raw_messages_returned'])
        self.assertFalse(bundle['privacy']['network_required'])

    def test_detail_still_requires_local_session(self):
        status, response = self.request('/api/bundles/synthetic-timeline', authenticated=False)
        self.assertEqual(status, 403)
        self.assertNotIn('relationship_timeline', json.dumps(response))

    def test_empty_archive_returns_unknown_recency_not_zero_day_contact(self):
        self.path.write_text(json.dumps({'contact_display': '合成空档案', 'messages': []}), encoding='utf-8')
        self.original = self.path.read_bytes()
        status, response = self.request('/api/bundles/synthetic-timeline')
        self.assertEqual(status, 200)
        self.assertIn('relationship_timeline', response['bundle'])
        timeline = response['bundle']['relationship_timeline']
        self.assertIsNone(timeline['last_contact']['last_record_at'])
        self.assertIsNone(timeline['last_contact']['elapsed_days'])
        self.assertEqual(timeline['nodes'], [])


if __name__ == '__main__':
    unittest.main()
