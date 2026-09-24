"""Synthetic metadata and token-fence tests; no live chat or model transport."""
import copy
import importlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from dashboard import analysis as ai
from tests.test_copilot_service import CopilotFixture, RESPONSE


def indexed(bundle_id='synthetic-person', title='合成人物', **changes):
    return {'bundle_id': bundle_id, 'source': {'id': bundle_id, 'contact': title,
            'source': 'wechat-local-readonly'}, 'source_missing': False,
            'hidden_at': None, 'alias': '', 'version': 0, **changes}


class BindingFixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dashboard.copilot.binding'),
                             'Metadata resolver and binding fence must exist')
        self.binding = importlib.import_module('dashboard.copilot.binding')


class BindingResolverTests(BindingFixture):
    def test_exact_original_name_is_only_an_unverified_suggestion(self):
        result = self.binding.resolve_title('合成人物', [indexed()])
        self.assertEqual(result['state'], 'suggested')
        self.assertFalse(result['account_verified'])
        self.assertEqual(result['bundle_id'], 'synthetic-person')
        self.assertNotIn('title', result)

    def test_alias_fuzzy_case_suffix_and_unicode_normalization_never_identify(self):
        items = [indexed(title='Example Friend', alias='合成人物')]
        for title in ('合成人物', 'Example', 'example friend', 'Example Friend (2)',
                      ' Example Friend', 'Ｅxample Friend'):
            with self.subTest(title=title):
                self.assertEqual(self.binding.resolve_title(title, items)['state'], 'unavailable')

    def test_hidden_missing_and_cross_source_entries_still_block_duplicates(self):
        for changes in ({'hidden_at': '2026-09-01'}, {'source_missing': True},
                        {'source': {'id': 'other', 'contact': '合成人物', 'source': 'qq'}}):
            result = self.binding.resolve_title('合成人物', [indexed(), indexed('other', **changes)])
            self.assertEqual(result['state'], 'ambiguous')
            self.assertNotIn('bundle_id', result)
            self.assertNotIn('binding_token', result)

    def test_unavailable_or_non_wechat_single_source_cannot_suggest(self):
        for item in (indexed(hidden_at='2026-09-01'), indexed(source_missing=True),
                     indexed(source={'id': 'synthetic-person', 'contact': '合成人物', 'source': 'qq'})):
            self.assertEqual(self.binding.resolve_title('合成人物', [item])['state'], 'unavailable')

    def test_invalid_titles_fail_closed_without_echo(self):
        for title in (None, 42, '', '  ', '合成…', '合成...', '合成⋯', 'A\x00B',
                      'A\nB', 'A\u202eB', '\ud800', '字' * 201):
            with self.subTest(title=repr(title)):
                result = self.binding.resolve_title(title, [indexed()])
                self.assertEqual(result['state'], 'unavailable')
                self.assertNotIn('bundle_id', result)
                self.assertNotIn('title', result)


class BindingStateTests(BindingFixture):
    def setUp(self):
        super().setUp()
        self.now = 100.0
        self.registry = self.binding.BindingRegistry(clock=lambda: self.now, max_sessions=2)
        self.workspace = ('synthetic-root', 'synthetic-contacts')
        self.items = [indexed(), indexed('other', '合成其他')]
        self.session = self.registry.start(self.workspace)['session_id']

    def observe(self, seq, title='合成人物', **changes):
        return self.registry.observe(self.workspace, {'session_id': self.session, 'seq': seq,
            'target': 'synthetic-window', 'state': 'observed', 'title': title,
            'source': 'local_ocr', **changes}, lambda: self.items)

    def validate(self, token):
        return self.registry.validate(self.workspace, token, 'synthetic-person', lambda: self.items)

    def test_identical_heartbeat_refreshes_ttl_without_replacing_token(self):
        first = self.observe(1)
        self.now += 9
        second = self.observe(2)
        self.assertEqual(first['binding_token'], second['binding_token'])
        self.now += 9
        self.assertEqual(self.validate(first['binding_token'])['observation_seq'], 2)
        self.now += 2
        with self.assertRaises(ai.AnalysisError):
            self.validate(first['binding_token'])
        third = self.observe(3)
        self.assertNotEqual(first['binding_token'], third['binding_token'])

    def test_a_b_a_and_unavailable_never_revive_old_token(self):
        first = self.observe(1)['binding_token']
        self.observe(2, '合成其他')
        third = self.observe(3)['binding_token']
        self.assertNotEqual(first, third)
        with self.assertRaises(ai.AnalysisError):
            self.validate(first)
        self.observe(4, '', state='unavailable')
        with self.assertRaises(ai.AnalysisError):
            self.validate(third)
        self.assertNotEqual(third, self.observe(5)['binding_token'])

    def test_late_and_repeated_frames_do_not_override_newer_observation(self):
        first = self.observe(2)
        for seq in (1, 2):
            rejected = self.observe(seq, '合成其他')
            self.assertEqual(rejected['state'], 'unavailable')
            self.assertNotIn('binding_token', rejected)
        self.assertEqual(self.validate(first['binding_token'])['observation_seq'], 2)

    def test_new_session_target_and_workspace_evict_old_tokens(self):
        token = self.observe(1)['binding_token']
        replacement = self.registry.start(self.workspace)['session_id']
        self.assertEqual(self.observe(2)['state'], 'unavailable')
        with self.assertRaises(ai.AnalysisError):
            self.validate(token)
        self.session = replacement
        token = self.observe(1)['binding_token']
        changed = self.observe(2, target='other-window')['binding_token']
        self.assertNotEqual(token, changed)
        self.registry.start(('other-root', 'other-contacts'))
        self.registry.start(('third-root', 'third-contacts'))
        with self.assertRaises(ai.AnalysisError):
            self.validate(changed)

    def test_index_source_name_changes_without_user_version_revoke_binding(self):
        token = self.observe(1)['binding_token']
        self.items[0]['source']['contact'] = '合成新名称'
        self.assertEqual(self.items[0]['version'], 0)
        with self.assertRaises(ai.AnalysisError):
            self.validate(token)
        self.items[0]['source']['contact'] = '合成人物'
        self.assertNotEqual(token, self.observe(2)['binding_token'])

    def test_expiry_while_reading_metadata_cannot_claim_a_stale_token(self):
        token = self.observe(1)['binding_token']
        def delayed_index():
            self.now += 11
            return self.items
        with self.assertRaises(ai.AnalysisError):
            self.registry.validate(self.workspace, token, 'synthetic-person', delayed_index)

    def test_observation_that_exceeds_ttl_while_reading_index_is_unavailable(self):
        def delayed_index():
            self.now += 11
            return self.items
        result = self.registry.observe(self.workspace, {'session_id': self.session, 'seq': 1,
            'target': 'synthetic-window', 'state': 'observed', 'title': '合成人物',
            'source': 'local_ocr'}, delayed_index)
        self.assertEqual(result['state'], 'unavailable')
        self.assertNotIn('binding_token', result)

    def test_management_alias_edits_do_not_change_identity(self):
        token = self.observe(1)['binding_token']
        self.items[0]['alias'] = '合成新别名'
        self.items[0]['version'] += 1
        self.assertEqual(token, self.observe(2)['binding_token'])
        self.assertEqual(self.observe(3, '合成新别名')['state'], 'unavailable')

    def test_index_errors_revoke_tokens_and_only_return_fixed_safe_messages(self):
        token = self.observe(1)['binding_token']
        def failed_index():
            raise OSError('PRIVATE PATH AND TITLE')
        with self.assertRaises(ai.AnalysisError) as caught:
            self.registry.validate(self.workspace, token, 'synthetic-person', failed_index)
        self.assertNotIn('PRIVATE', str(caught.exception))
        with self.assertRaises(ai.AnalysisError):
            self.validate(token)
        result = self.registry.observe(self.workspace, {'session_id': self.session, 'seq': 2,
            'target': 'synthetic-window', 'state': 'observed', 'title': '合成人物',
            'source': 'local_ocr'}, failed_index)
        self.assertEqual(result['state'], 'unavailable')
        self.assertNotIn('PRIVATE', str(result))

    def test_expired_session_and_other_workspace_cannot_validate(self):
        token = self.observe(1)['binding_token']
        with self.assertRaises(ai.AnalysisError):
            self.registry.validate(('wrong', 'workspace'), token, 'synthetic-person', lambda: self.items)
        self.now += 1801
        self.assertEqual(self.observe(2)['state'], 'unavailable')
        with self.assertRaises(ai.AnalysisError):
            self.validate(token)

    def test_malformed_new_observations_clear_current_token_without_echo(self):
        for change in ({'seq': True}, {'seq': 0}, {'seq': 2**53}, {'target': ''},
                       {'target': 'x' * 129}, {'source': 'PRIVATE'}, {'state': 'PRIVATE'},
                       {'title': 'x' * 201}, {'extra': 'PRIVATE'}):
            self.session = self.registry.start(self.workspace)['session_id']
            token = self.observe(1)['binding_token']
            result = self.observe(2, **change) if 'seq' not in change else self.observe(change['seq'])
            self.assertEqual(result['state'], 'unavailable')
            self.assertNotIn('PRIVATE', str(result))
            with self.assertRaises(ai.AnalysisError):
                self.validate(token)


class CopilotAutomaticBindingTests(CopilotFixture):
    def setUp(self):
        super().setUp()
        self.assertTrue(hasattr(self.cp, 'binding_start'), 'Copilot must expose binding sessions')
        self.repository.reconcile([indexed()['source']])
        self.session = self.cp.binding_start(self.root, self.contacts, {})['session_id']

    def observe(self, seq, title='合成人物', **changes):
        return self.cp.binding_observe(self.root, self.contacts, {'session_id': self.session,
            'seq': seq, 'target': 'synthetic-window', 'state': 'observed',
            'title': title, 'source': 'local_ocr', **changes})

    def test_resolution_does_not_read_message_json_or_create_preview(self):
        with mock.patch.object(ai, '_source', side_effect=AssertionError('body read')):
            result = self.observe(1)
        self.assertEqual(result['state'], 'suggested')
        self.assertFalse(self.cp._PREVIEWS)

    def test_automatic_preview_requires_explicit_candidate_confirmation_and_no_title_upload(self):
        candidate = self.observe(1)
        preview = self.preview(binding_token=candidate['binding_token'])
        self.assertTrue(preview['binding_required'])
        self.assertFalse(preview['account_verified'])
        with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)) as transport:
            for value in (None, False, 1, 'true'):
                with self.assertRaises(ai.AnalysisError):
                    self.run_preview(preview, binding_confirmed=value)
            transport.assert_not_called()
            result = self.run_preview(preview, binding_confirmed=True)
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn(candidate['binding_token'], str(transport.call_args))
        self.assertNotIn('合成人物', str(transport.call_args))

    def test_stale_preview_and_runtime_token_rejected_before_provider(self):
        for mutation in ('unavailable', 'session', 'index'):
            with self.subTest(mutation=mutation):
                self.session = self.cp.binding_start(self.root, self.contacts, {})['session_id']
                self.repository.reconcile([indexed()['source']])
                token = self.observe(1)['binding_token']
                preview = self.preview(binding_token=token)
                if mutation == 'unavailable':
                    self.observe(2, '', state='unavailable')
                elif mutation == 'session':
                    self.cp.binding_start(self.root, self.contacts, {})
                else:
                    self.repository.reconcile([indexed(title='新名称')['source']])
                with mock.patch.object(ai, '_request_json') as transport, \
                     mock.patch.object(ai, '_source', side_effect=AssertionError('stale body read')):
                    with self.assertRaises(ai.AnalysisError):
                        self.preview(binding_token=token)
                    with self.assertRaises(ai.AnalysisError):
                        self.run_preview(preview, binding_confirmed=True)
                    transport.assert_not_called()

    def test_heartbeat_during_prepared_preview_is_valid_but_wrong_bundle_is_rejected(self):
        token = self.observe(1)['binding_token']
        preview = self.preview(binding_token=token)
        self.observe(2)
        with self.assertRaises(ai.AnalysisError):
            self.preview(binding_token=token, bundle_id='other-synthetic-person')
        with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)):
            self.assertEqual(self.run_preview(preview, binding_confirmed=True)['status'], 'ok')

    def test_revocation_wins_before_atomic_send_claim_with_zero_provider_calls(self):
        token = self.observe(1)['binding_token']
        preview = self.preview(binding_token=token)
        waiting = threading.Event()
        def run():
            waiting.set()
            return self.run_preview(preview, binding_confirmed=True)
        with mock.patch.object(ai, '_request_json') as transport, ThreadPoolExecutor(1) as pool:
            with self.cp._LOCK:
                future = pool.submit(run)
                self.assertTrue(waiting.wait(3))
                self.observe(2, '', state='unavailable')
            with self.assertRaises(ai.AnalysisError):
                future.result(timeout=3)
        transport.assert_not_called()

    def test_claimed_send_does_not_hold_revocation_lock_or_claim_cancellation(self):
        token = self.observe(1)['binding_token']
        preview = self.preview(binding_token=token)
        started, release = threading.Event(), threading.Event()
        def cloud(*args):
            started.set()
            self.assertTrue(release.wait(3))
            return copy.deepcopy(RESPONSE)
        with mock.patch.object(ai, '_request_json', side_effect=cloud) as transport, ThreadPoolExecutor(2) as pool:
            result = pool.submit(self.run_preview, preview, binding_confirmed=True)
            self.assertTrue(started.wait(3))
            try:
                revoked = pool.submit(self.observe, 2, '', state='unavailable')
                self.assertEqual(revoked.result(timeout=2)['state'], 'unavailable')
            finally:
                release.set()
            self.assertEqual(result.result(timeout=3)['status'], 'ok')
        self.assertEqual(transport.call_count, 1)
