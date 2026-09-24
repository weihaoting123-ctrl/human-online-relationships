"""Fixed-conversation live sessions use invented archives and mocked I/O only."""
import copy
import importlib
import json
import subprocess
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from dashboard import analysis as ai
from dashboard import app
from tests import test_copilot_api as api_tests
from tests.test_copilot_service import CopilotFixture, RESPONSE


def live_row(number, sender='peer', text=None):
    return {'id': f'partition:{number}', 'server_id': str(number),
            'timestamp': f'2026-09-24T12:00:{number:02d}', 'sender': sender,
            'text': text or f'LIVE_SYNTHETIC_{number}'}


def tail(*rows, identity='a' * 64):
    return {'ok': True, 'identity': identity, 'rows': list(rows),
            'cursor': {'version': 1, 'identity': identity, 'digest': 'b' * 64},
            'observed_at': '2026-09-24T12:01:00'}


class CopilotLiveTests(CopilotFixture):
    def setUp(self):
        super().setUp()
        self.assertIsNotNone(importlib.util.find_spec('dashboard.copilot.live'),
                             'A bounded live session service must exist')
        self.live = importlib.import_module('dashboard.copilot.live')
        self.live._PREVIEWS.clear()
        self.live._SESSIONS.clear()
        self.cp._BINDINGS.sessions.clear()
        self.now = [100.0]
        patches = [mock.patch.object(self.live, '_now', side_effect=lambda: self.now[0]),
                   mock.patch.object(self.cp._BINDINGS, 'clock', return_value=1.0),
                   mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)),
                   mock.patch.object(self.live, '_read_tail', return_value=tail(live_row(1)))]
        self.clock, _, self.cloud, self.reader = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        wall = mock.patch.object(self.live, '_wall_now', return_value=datetime.fromisoformat('2026-09-24T12:00:00').timestamp())
        wall.start()
        self.addCleanup(wall.stop)
        self.repository.reconcile([{'id': self.bundle.name, 'contact': '合成人物',
                                    'source': 'wechat-local-readonly'}])
        self.binding_session = self.cp.binding_start(self.root, self.contacts, {})
        self.observation = {'session_id': self.binding_session['session_id'], 'seq': 1,
                            'target': 'synthetic-window', 'title': '合成人物',
                            'state': 'observed', 'source': 'local_ocr'}
        self.binding = self.cp.binding_observe(self.root, self.contacts, self.observation)
        self.request.update(latest_draft='', binding_token=self.binding['binding_token'])

    def preview_live(self, **overrides):
        return self.live.preview(self.root, self.contacts, {**self.request, **overrides}, admission=self.admission)

    def start_live(self, prepared=None, **overrides):
        prepared = prepared or self.preview_live()
        return self.live.start(self.root, self.contacts, {
            'preview_id': prepared['preview_id'], 'consent': True,
            'binding_confirmed': True, 'binding_revision': 7, **overrides}, admission=self.admission)

    def tick(self, session, **overrides):
        return self.live.tick(self.root, self.contacts, {'session_id': session['session_id'],
            'binding_token': self.binding['binding_token'], 'binding_revision': 7, **overrides}, admission=self.admission)

    def stop(self, session):
        return self.live.stop(self.root, self.contacts, {'session_id': session['session_id']})

    def new_peer(self, session, number=2):
        self.reader.return_value = tail(live_row(1), live_row(number))
        self.tick(session)
        self.now[0] += 3
        return self.tick(session)

    def test_preview_is_bounded_metadata_without_live_reader_or_cloud(self):
        prepared = self.preview_live()
        self.assertEqual(prepared['limits']['max_calls'], 6)
        self.assertEqual(prepared['limits']['duration_seconds'], 900)
        self.assertEqual(prepared['limits']['max_context_chars'], 20000)
        self.assertFalse(prepared['account_verified'])
        self.assertTrue(prepared['binding_required'])
        for secret in ('LATEST_TEXT', 'LIVE_SYNTHETIC', 'synthetic-test-key'):
            self.assertNotIn(secret, json.dumps(prepared))
        self.reader.assert_not_called()
        self.cloud.assert_not_called()

    def test_preview_requires_binding_and_empty_draft(self):
        for patch in ({'binding_token': None}, {'latest_draft': 'not allowed'}, {'max_messages': True}):
            with self.subTest(patch=patch), self.assertRaises(ai.AnalysisError):
                self.preview_live(**patch)
        self.reader.assert_not_called()
        self.cloud.assert_not_called()

    def test_start_requires_both_explicit_confirmations_and_is_one_use(self):
        prepared = self.preview_live()
        for patch in ({'consent': False}, {'binding_confirmed': False}, {'binding_confirmed': 1}):
            with self.subTest(patch=patch), self.assertRaises(ai.AnalysisError):
                self.start_live(prepared, **patch)
        active = self.start_live(prepared)
        self.assertEqual(active['state'], 'active')
        self.assertIsNone(active['result'])
        self.assertEqual(active['calls_used'], 0)
        with self.assertRaises(ai.AnalysisError):
            self.start_live(prepared)
        self.cloud.assert_not_called()

    def test_baseline_never_triggers_then_new_peer_debounces_and_sends_once(self):
        active = self.start_live()
        self.now[0] += 20
        self.assertIsNone(self.tick(active)['result'])
        result = self.new_peer(active)
        self.assertEqual(result['calls_used'], 1)
        self.assertEqual(len(result['result']['replies']), 3)
        sent = json.dumps(self.cloud.call_args.args[2])
        self.assertIn('LIVE_SYNTHETIC_2', sent)
        self.assertNotIn('LIVE_SYNTHETIC_1', sent)
        self.assertNotIn('OTHER_PERSON_SECRET', sent)
        self.assertNotIn('partition:', sent)
        self.assertEqual(self.tick(active)['result']['result_id'], result['result']['result_id'])
        self.assertEqual(self.cloud.call_count, 1)

    def test_outbound_message_cancels_pending_peer(self):
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2))
        self.tick(active)
        self.now[0] += 4
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3, 'me'))
        self.tick(active)
        self.now[0] += 4
        self.tick(active)
        self.cloud.assert_not_called()

    def test_new_peer_resets_debounce_and_interval_limits_calls(self):
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2))
        self.tick(active)
        self.now[0] += 2
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3))
        self.tick(active)
        self.now[0] += 2
        self.tick(active)
        self.cloud.assert_not_called()
        self.now[0] += 1
        self.assertIsNotNone(self.tick(active)['result'])
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3), live_row(4))
        self.tick(active)
        self.now[0] += 19
        self.tick(active)
        self.assertEqual(self.cloud.call_count, 1)
        self.now[0] += 1
        self.assertIsNotNone(self.tick(active)['result'])
        self.assertEqual(self.cloud.call_count, 2)

    def test_failure_consumes_attempt_and_stops_without_leaking_or_retry(self):
        active = self.start_live()
        self.cloud.side_effect = RuntimeError('PRIVATE_PROVIDER_BODY synthetic-test-key')
        result = self.new_peer(active)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['reason'], 'CALL_FAILED_POSSIBLY_CHARGED')
        self.assertEqual(result['calls_used'], 1)
        self.assertNotIn('PRIVATE_PROVIDER', json.dumps(result))
        self.tick(active)
        self.assertEqual(self.cloud.call_count, 1)

    def test_six_calls_and_fifteen_minutes_are_hard_limits(self):
        active = self.start_live()
        for number in range(2, 8):
            self.now[0] += 20
            response = self.new_peer(active, number)
        self.assertEqual(response['calls_used'], 6)
        self.assertEqual(response['state'], 'exhausted')
        self.now[0] += 30
        self.reader.return_value = tail(live_row(1), live_row(8))
        self.tick(active)
        self.assertEqual(self.cloud.call_count, 6)
        fresh = self.start_live()
        self.now[0] += 900
        self.assertEqual(self.tick(fresh)['reason'], 'SESSION_EXPIRED')

    def test_binding_config_prompt_visibility_identity_and_revision_stop(self):
        for change in ('binding', 'config', 'prompt', 'hidden', 'identity', 'revision'):
            with self.subTest(change=change):
                active = self.start_live()
                self.reader.return_value = tail(live_row(1), live_row(2))
                self.tick(active)
                self.now[0] += 3
                with mock.patch.object(self.cp, '_configuration', wraps=self.cp._configuration) as config, \
                     mock.patch.object(self.cp, 'PROMPT_REVISION', self.cp.PROMPT_REVISION):
                    if change == 'binding':
                        self.cp.binding_observe(self.root, self.contacts, {**self.observation, 'seq': 2, 'state': 'unavailable'})
                    elif change == 'config':
                        config.return_value = {'provider': 'openai', 'model': 'changed', 'sealed_key': 'changed'}
                    elif change == 'prompt':
                        self.cp.PROMPT_REVISION = 'changed'
                    elif change == 'hidden':
                        self.hidden = True
                    elif change == 'identity':
                        self.reader.return_value = tail(live_row(2), identity='c' * 64)
                    result = self.tick(active, **({'binding_revision': 8} if change == 'revision' else {}))
                self.assertEqual(result['state'], 'stopped')
                self.cloud.assert_not_called()
                self.hidden = False
                self.reader.return_value = tail(live_row(1))
                self.binding_session = self.cp.binding_start(self.root, self.contacts, {})
                self.observation['session_id'] = self.binding_session['session_id']
                self.binding = self.cp.binding_observe(self.root, self.contacts, self.observation)
                self.request['binding_token'] = self.binding['binding_token']

    def test_stop_is_fast_during_read_and_discards_late_read(self):
        active = self.start_live()
        entered, release = threading.Event(), threading.Event()
        def read(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return tail(live_row(1), live_row(2))
        self.reader.side_effect = read
        with ThreadPoolExecutor(2) as pool:
            task = pool.submit(self.tick, active)
            self.assertTrue(entered.wait(3))
            try:
                self.assertEqual(pool.submit(self.stop, active).result(1)['state'], 'stopped')
            finally:
                release.set()
            self.assertEqual(task.result(3)['state'], 'stopped')
        self.cloud.assert_not_called()

    def test_singleflight_claim_and_stop_discard_inflight_cloud_result(self):
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2))
        self.tick(active)
        self.now[0] += 3
        entered, release = threading.Event(), threading.Event()
        def cloud(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return copy.deepcopy(RESPONSE)
        self.cloud.side_effect = cloud
        with ThreadPoolExecutor(2) as pool:
            task = pool.submit(self.tick, active)
            self.assertTrue(entered.wait(3))
            try:
                self.assertEqual(self.tick(active)['state'], 'busy')
                self.assertEqual(pool.submit(self.stop, active).result(1)['state'], 'stopped')
            finally:
                release.set()
            result = task.result(3)
        self.assertEqual(result['state'], 'stopped')
        self.assertIsNone(result['result'])
        self.assertEqual(result['calls_used'], 1)
        self.assertEqual(self.cloud.call_count, 1)

    def test_transport_uses_bounded_hidden_subprocess_and_safe_errors(self):
        transport = importlib.import_module('dashboard.copilot.read_transport')
        response = subprocess.CompletedProcess([], 0, json.dumps(tail(live_row(1))).encode())
        with mock.patch.object(transport.subprocess, 'run', return_value=response) as child:
            value = transport.read_tail(self.bundle.name, None)
        self.assertEqual(value['identity'], 'a' * 64)
        self.assertFalse(child.call_args.kwargs['shell'])
        self.assertLessEqual(child.call_args.kwargs['timeout'], 90)
        self.assertEqual(child.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(json.loads(child.call_args.kwargs['input']), {'bundle_id': self.bundle.name})
        for bad in (b'PRIVATE_NOT_JSON', b'x' * (1024 * 1024 + 1), json.dumps({'ok': False, 'code': 'PRIVATE_SECRET'}).encode()):
            with mock.patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, bad)):
                with self.assertRaises(ai.AnalysisError) as caught:
                    transport.read_tail(self.bundle.name, None)
                self.assertNotIn('PRIVATE_', str(caught.exception))

    def test_known_reader_error_code_survives_without_child_body(self):
        transport = importlib.import_module('dashboard.copilot.read_transport')
        with mock.patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 1, json.dumps({'ok': False, 'code': 'LIVE_KEY_UNAVAILABLE'}).encode())):
            with self.assertRaises(ai.AnalysisError) as caught:
                transport.read_tail(self.bundle.name, None)
        self.assertEqual(getattr(caught.exception, 'code', None), 'LIVE_KEY_UNAVAILABLE')

    def test_redaction_expansion_stops_instead_of_sending_over_budget(self):
        self.payload['contact_display'] = '小明'
        self.write_payload()
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2, text='小明' * 1500))
        self.tick(active)
        self.now[0] += 3
        self.assertEqual(self.tick(active)['reason'], 'BATCH_OVERFLOW')
        self.cloud.assert_not_called()

    def test_config_mutation_during_unseal_is_rechecked_before_claim(self):
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2))
        self.tick(active)
        self.now[0] += 3
        def unseal(value):
            ai.save_config(self.root, {'provider': 'openai', 'model': 'changed', 'api_key': 'synthetic-test-key'})
            return 'synthetic-test-key'
        with mock.patch.object(ai, '_unseal', side_effect=unseal):
            self.assertEqual(self.tick(active)['state'], 'stopped')
        self.cloud.assert_not_called()

    def test_malformed_tail_or_reader_failure_stops_before_any_call(self):
        for value in ({'ok': True}, tail({**live_row(2), 'sender': 'unknown'}),
                      tail(live_row(2), live_row(2)), tail(live_row(2, text='x' * 4001))):
            with self.subTest(value=str(value)[:60]):
                active = self.start_live()
                self.reader.return_value = value
                self.assertEqual(self.tick(active)['state'], 'stopped')
                self.reader.return_value = tail(live_row(1))
        self.cloud.assert_not_called()

    def test_live_context_redacts_and_caps_combined_history_and_new_rows(self):
        self.payload['messages'][2]['content'] = 'HISTORY_' + 'y' * 19000
        self.write_payload()
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2, text='合成人物 test@example.com ' + 'x' * 3900))
        self.tick(active)
        self.now[0] += 3
        self.tick(active)
        content = self.cloud.call_args.args[2]
        self.assertLessEqual(sum(len(row['text']) for row in content['sample']), 20000)
        self.assertLessEqual(len(content['sample']), 200)
        self.assertNotIn('test@example.com', json.dumps(content))
        self.assertNotIn('合成人物', json.dumps(content, ensure_ascii=False))

    def test_new_session_stops_previous_and_wrong_workspace_cannot_access(self):
        first = self.start_live()
        second = self.start_live()
        with self.assertRaises(ai.AnalysisError):
            self.tick(first)
        with self.assertRaises(ai.AnalysisError):
            self.live.stop(self.root / 'other', self.contacts, {'session_id': second['session_id']})
        self.assertEqual(self.tick(second)['state'], 'active')

    def test_http_routes_security_and_stop_works_when_module_disabled(self):
        self.net.stop()
        for patch in (mock.patch.object(app, 'DATA_DIR', self.root), mock.patch.object(app, 'CONTACTS_DIR', self.contacts)):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = app.create_server(port=0, token='synthetic-copilot-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(lambda: api_tests.CopilotApiTests.close_server(self))
        http = lambda path, body, **headers: api_tests.CopilotApiTests.http(self, path, body, **headers)
        for suffix in ('preview', 'start', 'tick', 'stop'):
            path = '/api/copilot/live/' + suffix
            self.assertEqual(http(path, {}, **{'X-Local-Token': None})[0], 403)
            self.assertEqual(http(path, {}, Host='evil.invalid')[0], 421)
            self.assertEqual(http(path, {}, Origin='https://evil.invalid')[0], 403)
        self.assertEqual(http('/api/copilot/live/preview?unexpected=1', self.request)[0], 400)
        self.assertEqual(http('/api/copilot/live/preview', {'padding': 'x' * 8192})[0], 400)
        code, prepared = http('/api/copilot/live/preview', self.request)
        self.assertEqual(code, 200)
        code, session = http('/api/copilot/live/start', {'preview_id': prepared['preview_id'],
            'consent': True, 'binding_confirmed': True, 'binding_revision': 7})
        self.assertEqual(code, 200)
        self.assertEqual(session['state'], 'active')
        self.registry.update({'id': 'copilot', 'enabled': False, 'expected_version': 1})
        code, stopped = http('/api/copilot/live/stop', {'session_id': session['session_id']})
        self.assertEqual(code, 200)
        self.assertEqual(stopped['state'], 'stopped')
        self.cloud.assert_not_called()

    def test_successful_mutation_revokes_grant_even_if_settings_are_restored(self):
        active = self.start_live()
        prepared = self.preview_live()
        self.live.mutate(self.registry.update, {'id': 'copilot', 'enabled': False, 'expected_version': 1})
        self.live.mutate(self.registry.update, {'id': 'copilot', 'enabled': True, 'expected_version': 2})
        self.assertEqual(self.tick(active)['state'], 'stopped')
        with self.assertRaises(ai.AnalysisError):
            self.start_live(prepared)
        self.cloud.assert_not_called()

    def test_rejected_mutation_does_not_revoke_valid_grant(self):
        active = self.start_live()
        with self.assertRaises(Exception):
            self.live.mutate(self.registry.update, {'id': 'copilot', 'enabled': False, 'expected_version': 0})
        self.assertEqual(self.tick(active)['state'], 'active')

    def test_server_id_alias_and_upgrade_never_retrigger_and_content_changes_stop(self):
        initial = {**live_row(1), 'server_id': '0'}
        self.reader.return_value = tail(initial)
        active = self.start_live()
        self.reader.return_value = tail(live_row(1))
        self.tick(active)
        moved = {**live_row(1), 'id': 'other-partition:93'}
        self.reader.return_value = tail(moved)
        self.now[0] += 4
        self.tick(active)
        self.cloud.assert_not_called()
        self.reader.return_value = tail({**moved, 'text': 'CHANGED'})
        self.assertEqual(self.tick(active)['reason'], 'SOURCE_CHANGED')

    def test_duplicate_server_id_across_partitions_within_tail_is_one_message(self):
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2), {**live_row(2), 'id': 'second:2'})
        self.tick(active)
        self.now[0] += 3
        self.assertIsNotNone(self.tick(active)['result'])
        sent = self.cloud.call_args.args[2]['sample']
        self.assertEqual(sum(row['text'] == 'LIVE_SYNTHETIC_2' for row in sent), 1)

    def test_established_server_id_cannot_rebind_or_downgrade(self):
        for server_id in ('999', '0'):
            with self.subTest(server_id=server_id):
                self.reader.return_value = tail(live_row(1))
                active = self.start_live()
                self.reader.return_value = tail({**live_row(1), 'server_id': server_id})
                self.assertEqual(self.tick(active)['reason'], 'SOURCE_CHANGED')
        self.cloud.assert_not_called()

    def test_me_clears_current_result_and_exhausted_session_still_observes(self):
        active = self.start_live()
        self.new_peer(active)
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3, 'me'))
        self.assertIsNone(self.tick(active)['result'])
        session = self.live._SESSIONS[self.cp._workspace(self.root, self.contacts)]
        session['state'] = 'exhausted'
        session['attempts'] = 6
        session['result'] = {'result_id': 'synthetic', **RESPONSE}
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3, 'me'), live_row(4, 'me'))
        value = self.tick(active)
        self.assertEqual(value['state'], 'exhausted')
        self.assertIsNone(value['result'])
        self.assertEqual(self.cloud.call_count, 1)

    def test_tail_without_previous_anchor_stops_and_new_batch_overflow_is_not_truncated(self):
        active = self.start_live()
        self.reader.return_value = tail(live_row(2))
        self.assertEqual(self.tick(active)['reason'], 'SOURCE_GAP')
        for rows in ([live_row(number) for number in range(2, 23)],
                     [live_row(2, text='x' * 3000), live_row(3, text='y' * 3000)]):
            self.reader.return_value = tail(live_row(1))
            active = self.start_live()
            self.reader.return_value = tail(live_row(1), *rows)
            self.assertEqual(self.tick(active)['reason'], 'BATCH_OVERFLOW')
        self.cloud.assert_not_called()

    def test_peer_me_peer_only_responds_to_post_self_peer_and_identical_text_ids_are_new(self):
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3, 'me'), live_row(4, text='SAME'))
        self.tick(active)
        self.now[0] += 3
        self.tick(active)
        sent = json.dumps(self.cloud.call_args.args[2])
        self.assertNotIn('LIVE_SYNTHETIC_2', sent)
        self.assertIn('LIVE_SYNTHETIC_3', sent)
        self.assertIn('SAME', sent)
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3, 'me'), live_row(4, text='SAME'), live_row(5, text='SAME'))
        self.tick(active)
        self.now[0] += 20
        self.tick(active)
        self.assertEqual(self.cloud.call_count, 2)

    def test_new_id_older_than_observed_watermark_never_sends(self):
        self.reader.return_value = tail(live_row(20))
        active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(20))
        self.assertEqual(self.tick(active)['reason'], 'SOURCE_TIME_REWIND')
        self.now[0] += 3
        self.tick(active)
        self.cloud.assert_not_called()

    def test_new_id_before_wall_clock_grant_is_not_authorized_even_after_old_baseline(self):
        with mock.patch.object(self.live, '_wall_now', return_value=datetime.fromisoformat('2026-09-24T12:00:30').timestamp(), create=True):
            active = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(20))
        self.assertEqual(self.tick(active)['reason'], 'SOURCE_TIME_REWIND')
        self.cloud.assert_not_called()

    def test_empty_baseline_does_not_authorize_old_history_and_same_second_new_id_is_allowed(self):
        with mock.patch.object(self.live, '_wall_now', return_value=datetime.fromisoformat('2026-09-24T12:00:30').timestamp(), create=True):
            self.reader.return_value = tail()
            active = self.start_live()
        self.reader.return_value = tail(live_row(20))
        self.assertEqual(self.tick(active)['reason'], 'SOURCE_TIME_REWIND')
        self.reader.return_value = tail(live_row(30))
        with mock.patch.object(self.live, '_wall_now', return_value=datetime.fromisoformat('2026-09-24T12:00:30').timestamp(), create=True):
            active = self.start_live()
        same_second = {**live_row(31), 'timestamp': live_row(30)['timestamp']}
        self.reader.return_value = tail(live_row(30), same_second)
        self.tick(active)
        self.now[0] += 3
        self.assertIsNotNone(self.tick(active)['result'])

    def test_new_message_after_grant_upper_bound_stops_for_utc_and_offset_times(self):
        baseline = {**live_row(1), 'timestamp': '2026-09-24T12:00:01Z'}
        grant = datetime.fromisoformat('2026-09-24T12:00:00+00:00').timestamp()
        for timestamp in ('2026-09-24T12:15:00.001Z', '2026-09-24T20:15:00.001+08:00',
                          '2027-01-01T00:00:00Z'):
            with self.subTest(timestamp=timestamp):
                self.reader.return_value = tail(baseline)
                with mock.patch.object(self.live, '_wall_now', return_value=grant):
                    active = self.start_live()
                self.reader.return_value = tail(baseline, {**live_row(2), 'timestamp': timestamp})
                result = self.tick(active)
                self.assertEqual(result['state'], 'stopped')
                self.assertEqual(result['reason'], 'SOURCE_TIME_OUT_OF_RANGE')
                self.assertIsNone(result['result'])
                self.now[0] += 3
                self.tick(active)
        self.cloud.assert_not_called()

    def test_grant_upper_bound_is_inclusive_and_timezone_independent(self):
        baseline = {**live_row(1), 'timestamp': '2026-09-24T12:00:01Z'}
        grant = datetime.fromisoformat('2026-09-24T12:00:00+00:00').timestamp()
        for timestamp in ('2026-09-24T12:14:59.999Z', '2026-09-24T12:15:00Z',
                          '2026-09-24T20:15:00+08:00'):
            with self.subTest(timestamp=timestamp):
                self.reader.return_value = tail(baseline)
                with mock.patch.object(self.live, '_wall_now', return_value=grant):
                    active = self.start_live()
                self.reader.return_value = tail(baseline, {**live_row(2), 'timestamp': timestamp})
                self.assertEqual(self.tick(active)['state'], 'active')
                self.now[0] += 3
                self.assertIsNotNone(self.tick(active)['result'])

    def test_exhausted_session_keeps_last_result_during_large_peer_burst(self):
        active = self.start_live()
        state = self.live._SESSIONS[self.cp._workspace(self.root, self.contacts)]
        state.update(state='exhausted', attempts=6, result={'result_id': 'last-success', **RESPONSE})
        self.reader.return_value = tail(live_row(1), *[live_row(number) for number in range(2, 23)])
        observed = self.tick(active)
        self.assertEqual(observed['state'], 'exhausted')
        self.assertEqual(observed['result']['result_id'], 'last-success')
        self.cloud.assert_not_called()
