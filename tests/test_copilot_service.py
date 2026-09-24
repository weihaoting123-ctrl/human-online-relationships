"""Copilot boundaries use invented archives and a mocked transport only."""
import copy
import importlib
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai
from dashboard.library import LibraryRepository
from dashboard.modules import ModuleRegistry, ModulePolicyError


RESPONSE = {
    'replies': [{'style': style, 'text': '有空一起散步吗？不方便也没关系。', 'reason': '留出选择空间。'}
                for style in ('natural', 'warm', 'invite')],
    'topics': [{'title': '最近的活动', 'text': '最近有想尝试的小活动吗？'}],
    'caveats': ['建议只基于本次范围。'],
}


def row(day, text, kind='text', **kwargs):
    return {'timestamp': f'2026-09-{day:02d}T12:00:00', 'type': kind,
            'sender': 'them', 'content': text, **kwargs}


class CopilotFixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dashboard.copilot'), 'Copilot service package must exist')
        self.cp = importlib.import_module('dashboard.copilot.service')
        with self.cp._LOCK:
            self.cp._PREVIEWS.clear()
        scratch = Path(__file__).resolve().parents[1] / 'scripts' / 'tmp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.contacts = self.root / 'contacts'
        self.bundle = self.contacts / 'synthetic-person'
        self.bundle.mkdir(parents=True)
        self.path = self.bundle / 'messages.json'
        self.payload = {'contact_display': '合成人物', 'my_display': '合成本人', 'messages': [
            row(1, 'OUTSIDE_EARLIER'), row(10, 'EARLY_TEXT'),
            row(13, 'LATEST_TEXT 合成人物 test@example.com'), row(11, 'MIDDLE_TEXT'),
            row(14, 'VOICE_SECRET', 'voice', transcript='TRANSCRIPT_SECRET'),
            row(15, 'IMAGE_SECRET', 'image'), row(25, 'OUTSIDE_LATER'),
        ]}
        self.write_payload()
        other = self.contacts / 'other-synthetic-person'
        other.mkdir()
        (other / 'messages.json').write_text(json.dumps({'messages': [row(12, 'OTHER_PERSON_SECRET')]}), encoding='utf-8')
        self.request = {'bundle_id': self.bundle.name, 'date_from': '2026-09-10',
                        'date_to': '2026-09-20', 'direction': 'relaxed',
                        'max_messages': 2, 'latest_draft': '合成本人 test@example.com', 'binding_revision': 7}
        self.repository = LibraryRepository(self.root)
        self.registry = ModuleRegistry(self.repository)
        self.registry.update({'id': 'copilot', 'enabled': True, 'expected_version': 0})
        self.hidden = False
        self.crypto = mock.patch.object(ai, '_dpapi', side_effect=lambda data, **kwargs: data)
        self.crypto.start()
        self.addCleanup(self.crypto.stop)
        ai.save_config(self.root, {'provider': 'openai', 'model': 'synthetic-model', 'api_key': 'synthetic-test-key'})
        self.net = mock.patch.object(ai.urllib.request, 'build_opener', side_effect=AssertionError('Unexpected network'))
        self.net.start()
        self.addCleanup(self.net.stop)

    def write_payload(self):
        self.path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding='utf-8')

    def admission(self, bundle_id):
        self.registry.require('copilot')
        if self.hidden or bundle_id != self.bundle.name:
            raise ModulePolicyError('合成会话不可用')

    def preview(self, **overrides):
        return self.cp.preview(self.root, self.contacts, {**self.request, **overrides}, admission=self.admission)

    def run_preview(self, preview, **overrides):
        request = {'preview_id': preview['preview_id'], 'consent': True, 'binding_revision': 7, **overrides}
        return self.cp.run(self.root, self.contacts, request, admission=self.admission)


class CopilotServiceTests(CopilotFixture):
    def test_preview_discloses_sample_coverage_and_omitted_eligible_messages(self):
        preview = self.preview()
        self.assertEqual(preview['counts']['sample_date_from'], '2026-09-11')
        self.assertEqual(preview['counts']['sample_date_to'], '2026-09-13')
        self.assertEqual(preview['counts']['omitted_messages'], 1)

    def test_preview_is_metadata_only_in_memory_and_latest_ordered_text(self):
        before = set(self.root.rglob('*'))
        preview = self.preview()
        self.assertEqual(set(self.root.rglob('*')), before)
        self.assertEqual(preview['counts']['eligible_messages'], 3)
        self.assertEqual(preview['counts']['sample_messages'], 2)
        self.assertEqual(preview['counts']['excluded_nontext'], 2)
        self.assertEqual(preview['max_calls'], 1)
        public = json.dumps(preview, ensure_ascii=False)
        for secret in ('LATEST_TEXT', 'MIDDLE_TEXT', '合成本人', 'test@example.com', 'synthetic-test-key'):
            self.assertNotIn(secret, public)
        with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)) as transport:
            result = self.run_preview(preview)
        content = transport.call_args.args[2]
        self.assertEqual([item['text'] for item in content['sample']], ['MIDDLE_TEXT', 'LATEST_TEXT [称呼] [邮箱]'])
        transmitted = json.dumps(content, ensure_ascii=False)
        for excluded in ('EARLY_TEXT', 'OTHER_PERSON_SECRET', 'OUTSIDE_', 'VOICE_SECRET', 'TRANSCRIPT_SECRET', 'IMAGE_SECRET', self.bundle.name, 'test@example.com', '合成本人'):
            self.assertNotIn(excluded, transmitted)
        self.assertEqual(result['binding_revision'], 7)
        self.assertEqual(len(result['replies']), 3)
        self.assertEqual(result['preview_id'], preview['preview_id'])

    def test_no_consent_or_extra_fields_cannot_send(self):
        preview = self.preview()
        for consent in (False, 'true', 1, None):
            with self.subTest(consent=consent), self.assertRaises(ai.AnalysisError):
                self.run_preview(preview, consent=consent)
        with self.assertRaises(ai.AnalysisError):
            self.run_preview(preview, latest_draft='unapproved')

    def test_one_use_consumed_before_request_even_on_unknown_failure(self):
        preview = self.preview()
        with mock.patch.object(ai, '_request_json', side_effect=RuntimeError('PRIVATE_KEY provider response')) as transport:
            with self.assertRaises(ai.AnalysisError) as caught:
                self.run_preview(preview)
            self.assertNotIn('PRIVATE_KEY', str(caught.exception))
            self.assertIn('可能已计费', str(caught.exception))
            self.assertIn('不会自动重试', str(caught.exception))
            with self.assertRaises(ai.AnalysisError):
                self.run_preview(preview)
            self.assertEqual(transport.call_count, 1)

    def test_concurrent_double_run_sends_once(self):
        preview = self.preview()
        started, release = threading.Event(), threading.Event()
        def cloud(*args):
            started.set()
            release.wait(3)
            return copy.deepcopy(RESPONSE)
        with mock.patch.object(ai, '_request_json', side_effect=cloud) as transport, ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.run_preview, preview)
            self.assertTrue(started.wait(3))
            second = pool.submit(self.run_preview, preview)
            try:
                with self.assertRaises(ai.AnalysisError):
                    second.result(timeout=3)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3)['status'], 'ok')
        self.assertEqual(transport.call_count, 1)

    def test_revision_source_config_prompt_expiry_and_visibility_rechecked(self):
        for change in ('revision', 'source', 'config', 'prompt', 'expired', 'hidden', 'disabled'):
            with self.subTest(change=change):
                preview = self.preview()
                with mock.patch.object(ai, '_request_json', return_value=RESPONSE) as transport:
                    if change == 'revision':
                        action = lambda: self.run_preview(preview, binding_revision=8)
                    else:
                        action = lambda: self.run_preview(preview)
                    if change == 'source':
                        self.payload['messages'].append(row(19, 'NEW_SYNTHETIC_TEXT'))
                        self.write_payload()
                    if change == 'config':
                        ai.save_config(self.root, {'provider': 'openai', 'model': 'changed-model', 'api_key': ''})
                    if change == 'hidden':
                        self.hidden = True
                    if change == 'disabled':
                        self.registry.update({'id': 'copilot', 'enabled': False, 'expected_version': 1})
                    patch = (mock.patch.object(self.cp, 'PROMPT_REVISION', 'changed') if change == 'prompt'
                             else mock.patch.object(self.cp.time, 'time', return_value=time.time() + 601) if change == 'expired'
                             else mock.patch.object(self.cp, 'PREVIEW_TTL', self.cp.PREVIEW_TTL))
                    with patch, self.assertRaises((ai.AnalysisError, ModulePolicyError)):
                        action()
                    transport.assert_not_called()
                self.hidden = False

    def test_preview_cap_expiry_and_wrong_workspace(self):
        with self.cp._LOCK:
            self.cp._PREVIEWS.clear()
        previews = [self.preview() for _ in range(64)]
        with self.assertRaises(ai.AnalysisError):
            self.preview()
        with self.assertRaises(ai.AnalysisError):
            self.cp.run(self.root / 'different', self.contacts,
                        {'preview_id': previews[0]['preview_id'], 'consent': True, 'binding_revision': 7}, admission=self.admission)
        with mock.patch.object(self.cp.time, 'time', return_value=time.time() + 601):
            self.assertEqual(self.preview()['status'], 'ok')

    def test_input_validation_is_strict_and_does_not_echo_values(self):
        bad = [{'max_messages': item} for item in (True, 0, 201, 2.5, '80')]
        bad += [{'binding_revision': item} for item in (True, -1, 2.5, '7')]
        bad += [{'date_from': '2026-9-01'}, {'date_to': '2026-02-30'}, {'date_from': '2026-10-01'},
                {'direction': []}, {'direction': 'PRIVATE_INVALID'}, {'latest_draft': 42},
                {'latest_draft': 'a' * 4001}, {'unknown': 'PRIVATE_INVALID'}, {'bundle_id': '../other'}]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ai.AnalysisError) as caught:
                self.preview(**changes)
            self.assertNotIn('PRIVATE_INVALID', str(caught.exception))

    def test_twenty_thousand_char_budget_includes_draft_and_defaults_to_eighty(self):
        self.payload['messages'] = [row(12, ('文' * 1000) + str(index)) for index in range(205)]
        self.write_payload()
        request = {**self.request, 'latest_draft': '草' * 4000}
        request.pop('max_messages')
        preview = self.cp.preview(self.root, self.contacts, request, admission=self.admission)
        self.assertEqual(preview['scope']['max_messages'], 80)
        self.assertLessEqual(preview['counts']['sample_messages'], 80)
        self.assertLessEqual(preview['counts']['sample_chars'] + preview['counts']['draft_chars'], 20000)

    def test_unconfigured_changed_sealed_config_and_unavailable_archive_fail_closed(self):
        preview = self.preview()
        config_path = ai._root(self.root) / 'config.json'
        config = ai._read(config_path)
        ai._write(config_path, {**config, 'model': 'changed-without-revision'})
        with mock.patch.object(ai, '_request_json') as transport:
            with self.assertRaises(ai.AnalysisError):
                self.run_preview(preview)
            ai.clear_config(self.root)
            preview = self.preview()
            self.assertFalse(preview['recipient']['configured'])
            with self.assertRaises(ai.AnalysisError):
                self.run_preview(preview)
            with mock.patch.object(ai, '_source', side_effect=OSError('PRIVATE source path')):
                with self.assertRaises(ai.AnalysisError) as caught:
                    self.preview()
                self.assertNotIn('PRIVATE', str(caught.exception))
            transport.assert_not_called()

    def test_unpaired_unicode_in_model_output_is_not_returned(self):
        value = copy.deepcopy(RESPONSE)
        value['replies'][0]['text'] = '合成\ud800\udfff输出'
        result = self.cp.safe_result(value)
        self.assertEqual(result['replies'][0]['text'], '合成输出')
        json.dumps(result, ensure_ascii=False).encode('utf-8')

    def test_status_allowlists_and_bounds_conversation_metadata(self):
        status = self.cp.status(self.root, enabled=True, conversations=[{
            'id': 'synthetic-person', 'contact': {'secret': 'PRIVATE'}, 'message_count': float('nan'),
            'date_range': ['not-a-date', '2026-09-12'],
            'library': {'alias': 'a' * 1000, 'note': 'PRIVATE'}, 'content': 'PRIVATE',
        }])
        item = status['conversations'][0]
        self.assertEqual(item['contact_display'], '')
        self.assertEqual(item['message_count'], 0)
        self.assertLessEqual(len(item['alias']), 200)
        self.assertEqual(item['date_from'], '')
        self.assertNotIn('PRIVATE', json.dumps(status))

    def test_reply_schema_sanitizes_and_rejects_invented_fields(self):
        value = copy.deepcopy(RESPONSE)
        value['replies'][0]['text'] = '<script>alert(1)</script>' + '字' * 900
        value['topics'] *= 10
        value['caveats'] *= 10
        safe = self.cp.safe_result(value)
        self.assertNotIn('<', json.dumps(safe))
        self.assertLessEqual(len(safe['replies'][0]['text']), 800)
        self.assertLessEqual(len(safe['topics']), 3)
        self.assertLessEqual(len(safe['caveats']), 4)
        bad = [[], {**RESPONSE, 'score': 99}, {**RESPONSE, 'replies': RESPONSE['replies'][:2]},
               {**RESPONSE, 'topics': [{'title': 'PRIVATE', 'text': 4}]},
               {**RESPONSE, 'replies': [{'style': 'diagnosis', 'text': 'PRIVATE', 'reason': 'PRIVATE'}] * 3}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ai.AnalysisError) as caught:
                self.cp.safe_result(value)
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_relaxed_direction_never_requires_an_invitation(self):
        self.assertIn('relaxed 方向不主动推进邀约', self.cp.SYSTEM_PROMPT)
        self.assertIn('invite 风格也可以只是轻松的后续话题', self.cp.SYSTEM_PROMPT)

    def test_shared_json_transport_preserves_fixed_endpoint_bounds(self):
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            {'choices': [{'message': {'content': json.dumps(RESPONSE)}}]}).encode()
        with mock.patch.object(ai.urllib.request, 'build_opener', return_value=opener) as factory:
            value = ai._request_json({'provider': 'openai', 'model': 'synthetic'}, 'synthetic-test-key', {}, 'test')
        self.assertEqual(value, RESPONSE)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], ai._NoRedirect)
        self.assertEqual(opener.open.call_args.args[0].full_url, ai.PROVIDERS['openai'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 120)
        self.assertEqual(opener.open.return_value.__enter__.return_value.read.call_args.args, (1024 * 1024 + 1,))


if __name__ == '__main__':
    unittest.main()
