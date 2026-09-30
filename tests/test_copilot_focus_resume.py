"""Synthetic focus fences: never read WeChat, secrets, or a real provider."""
import copy
import io
import json
import threading
import unittest
from unittest import mock

from dashboard import analysis as ai
from tests.test_copilot_live import CopilotLiveFixture, live_row, tail
from tests.test_copilot_service import RESPONSE


class FocusResumeTests(CopilotLiveFixture):

    def focus(self, method, session):
        action = getattr(self.live, method, None)
        self.assertTrue(callable(action), 'Live grants need an explicit bounded focus fence')
        return action(self.root, self.contacts, {'session_id': session['session_id'],
            'binding_token': self.binding['binding_token'], 'binding_revision': 7}, admission=self.admission)

    def restore_title(self, **changes):
        self.observation['seq'] += 1
        return self.cp.binding_observe(self.root, self.contacts, {**self.observation, **changes})

    def test_pause_blocks_reads_claims_and_one_shot_binding_validation(self):
        session = self.start_live()
        self.new_peer(session)
        before = self.reader.call_count
        paused = self.focus('suspend', session)
        self.assertEqual(paused['state'], 'suspended')
        self.assertIsNone(paused['result'])
        self.assertEqual(self.tick(session)['state'], 'suspended')
        self.assertEqual(self.reader.call_count, before)
        self.assertEqual(self.cloud.call_count, 1)
        with self.assertRaises(ai.AnalysisError):
            self.cp._validate_binding(self.root, self.contacts, self.binding['binding_token'], self.bundle.name)

    def test_preview_explains_focus_pause_without_grant_renewal_or_replay(self):
        notices = ''.join(self.preview_live()['privacy_notices'])
        self.assertIn('切屏', notices)
        self.assertIn('不续期', notices)
        self.assertIn('不追发', notices)

    def test_resume_keeps_deadline_budget_identity_and_dedup_but_drops_away_text(self):
        session = self.start_live()
        self.new_peer(session)
        original = self.live._SESSIONS[self.cp._workspace(self.root, self.contacts)]
        expires, grant_wall = original['expires'], original['grant_wall']
        self.focus('suspend', session)
        self.now[0] += 70
        self.cp._BINDINGS.clock.return_value += 70
        self.assertEqual(self.restore_title()['binding_token'], self.binding['binding_token'])
        with mock.patch.object(self.live, '_resume_tail', return_value=tail(live_row(1), live_row(2), live_row(3))) as reader:
            resumed = self.focus('resume', session)
        self.assertEqual(resumed['state'], 'active')
        self.assertEqual(resumed['calls_used'], 1)
        self.assertEqual(original['expires'], expires)
        self.assertEqual(original['grant_wall'], grant_wall)
        self.assertIsNotNone(reader.call_args.args[1])
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3))
        self.now[0] += 4
        self.tick(session)
        self.assertEqual(self.cloud.call_count, 1)
        self.reader.return_value = tail(live_row(1), live_row(2), live_row(3), live_row(4))
        self.tick(session)
        self.now[0] += 3
        self.tick(session)
        sent = str(self.cloud.call_args.args[2])
        self.assertIn('LIVE_SYNTHETIC_4', sent)
        self.assertNotIn('LIVE_SYNTHETIC_3', sent)
        # Text observed before focus loss was already granted context. Away
        # text is excluded, while resuming does not itself launch a new call.
        self.assertIn('LIVE_SYNTHETIC_2', sent)
        self.assertNotIn('LIVE_SYNTHETIC_1', sent)
        self.assertEqual(self.cloud.call_count, 2)

    def test_pending_text_is_never_replayed_after_pause(self):
        session = self.start_live()
        self.reader.return_value = tail(live_row(1), live_row(2))
        self.tick(session)
        self.focus('suspend', session)
        self.restore_title()
        with mock.patch.object(self.live, '_resume_tail', return_value=self.reader.return_value):
            self.focus('resume', session)
        self.now[0] += 30
        self.tick(session)
        self.cloud.assert_not_called()

    def test_pause_expiry_and_manual_stop_are_irreversible(self):
        for action in ('expire', 'stop'):
            session = self.start_live()
            self.focus('suspend', session)
            if action == 'expire':
                self.now[0] += 900
            else:
                self.stop(session)
            self.restore_title()
            self.assertEqual(self.focus('resume', session)['state'], 'stopped')
        self.cloud.assert_not_called()

    def test_resume_requires_fresh_same_binding_and_fixed_source(self):
        for changed in ('no_observation', 'target', 'title', 'index', 'config', 'identity', 'gap'):
            with self.subTest(changed=changed):
                self.setUp_binding()
                session = self.start_live()
                self.focus('suspend', session)
                if changed != 'no_observation':
                    self.restore_title(**({'target': 'other-window'} if changed == 'target' else
                        {'title': 'other-title'} if changed == 'title' else {}))
                data = tail(live_row(1))
                if changed == 'identity':
                    data = tail(live_row(1), identity='c' * 64)
                elif changed == 'gap':
                    data = tail(live_row(5))
                if changed == 'index':
                    self.repository.reconcile([{'id': self.bundle.name, 'contact': 'changed', 'source': 'wechat-local-readonly'}])
                with mock.patch.object(self.live, '_resume_tail', return_value=data), \
                     mock.patch.object(self.cp, '_configuration', wraps=self.cp._configuration) as config:
                    if changed == 'config':
                        config.return_value = {'provider': 'openai', 'model': 'changed', 'sealed_key': 'changed'}
                    self.assertEqual(self.focus('resume', session)['state'], 'stopped')
        self.cloud.assert_not_called()

    def setUp_binding(self):
        self.repository.reconcile([{'id': self.bundle.name, 'contact': '合成人物', 'source': 'wechat-local-readonly'}])
        self.binding_session = self.cp.binding_start(self.root, self.contacts, {})
        self.observation.update(session_id=self.binding_session['session_id'], seq=1)
        self.binding = self.cp.binding_observe(self.root, self.contacts, self.observation)
        self.request['binding_token'] = self.binding['binding_token']

    def test_queued_model_claim_is_discarded_after_suspend(self):
        actions = []
        self.launch.side_effect = lambda session, action: actions.append(action)
        session = self.start_live()
        self.new_peer(session)
        self.focus('suspend', session)
        actions[0]()
        self.assertEqual(self.tick(session)['state'], 'suspended')
        self.assertEqual(self.tick(session)['calls_used'], 0)
        self.cloud.assert_not_called()

    def test_resume_requires_sequence_newer_than_suspension_fence(self):
        session = self.start_live()
        self.focus('suspend', session)
        self.restore_title()
        registry = self.cp._BINDINGS
        validate = registry.validate
        fence = self.live._SESSIONS[self.cp._workspace(self.root, self.contacts)]['binding']['observation_seq']
        def stale_snapshot(*args, **kwargs):
            return {**validate(*args, **kwargs), 'observation_seq': fence}
        with mock.patch.object(registry, 'validate', side_effect=stale_snapshot), \
             mock.patch.object(self.live, '_resume_tail', return_value=tail(live_row(1))) as reader:
            self.assertEqual(self.focus('resume', session)['state'], 'stopped')
            reader.assert_not_called()
        self.cloud.assert_not_called()

    def test_live_cloud_content_contains_normalized_reply_style(self):
        session = self.start_live()
        self.new_peer(session)
        self.assertIn('reply_style', self.cloud.call_args.args[2])

    def test_pause_revokes_earlier_one_shot_preview_even_after_resume(self):
        prepared = self.cp.preview(self.root, self.contacts, self.request, admission=self.admission)
        session = self.start_live()
        self.focus('suspend', session)
        self.restore_title()
        with mock.patch.object(self.live, '_resume_tail', return_value=tail(live_row(1))):
            self.focus('resume', session)
        with self.assertRaises(ai.AnalysisError):
            self.run_preview(prepared, binding_confirmed=True)
        self.cloud.assert_not_called()

    def test_sixth_late_completion_cannot_override_new_focus_fence(self):
        session = self.start_live()
        stored = self.live._SESSIONS[self.cp._workspace(self.root, self.contacts)]
        stored['attempts'] = 5
        original_guard = self.live._guard
        suspended = [False]
        def guard(*args, **kwargs):
            result = original_guard(*args, **kwargs)
            if self.cloud.call_count and not suspended[0]:
                suspended[0] = True
                self.focus('suspend', session)
            return result
        with mock.patch.object(self.live, '_guard', side_effect=guard):
            result = self.new_peer(session)
        self.assertEqual(result['state'], 'suspended')
        self.assertIsNone(result['result'])
        self.assertEqual(result['calls_used'], 6)

    def test_inflight_read_finishes_without_publishing_and_unknown_failure_stops(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                self.setUp_binding()
                session = self.start_live()
                def read(*args):
                    self.focus('suspend', session)
                    if failed:
                        raise self.live.LiveReadError('LIVE_READ_FAILED')
                    return tail(live_row(1), live_row(2))
                with mock.patch.object(self.live, '_read_tail', side_effect=read):
                    result = self.tick(session)
                self.assertEqual(result['state'], 'stopped' if failed else 'suspended')
                self.assertIsNone(result['result'])
                self.stop(session)
        self.cloud.assert_not_called()

    def test_inflight_source_identity_change_is_not_hidden_by_focus_pause(self):
        session = self.start_live()
        def read(*args):
            self.focus('suspend', session)
            return tail(live_row(1), identity='c' * 64)
        with mock.patch.object(self.live, '_read_tail', side_effect=read):
            result = self.tick(session)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['reason'], 'SOURCE_IDENTITY_CHANGED')
        self.cloud.assert_not_called()

    def test_pause_after_read_guard_still_rejects_observed_identity_change(self):
        for operation in ('tick', 'resume'):
            with self.subTest(operation=operation):
                self.setUp_binding()
                session = self.start_live()
                if operation == 'resume':
                    self.focus('suspend', session)
                    self.restore_title()
                original_guard = self.live._guard
                guarded = [0]
                def guard(*args, **kwargs):
                    result = original_guard(*args, **kwargs)
                    guarded[0] += 1
                    if guarded[0] == 2:
                        self.focus('suspend', session)
                    return result
                reader = '_read_tail' if operation == 'tick' else '_resume_tail'
                with mock.patch.object(self.live, '_guard', side_effect=guard), \
                     mock.patch.object(self.live, reader, return_value=tail(live_row(1), identity='c' * 64)):
                    result = self.tick(session) if operation == 'tick' else self.focus('resume', session)
                self.assertEqual(result['state'], 'stopped')
                self.assertEqual(result['reason'], 'SOURCE_IDENTITY_CHANGED')
        self.cloud.assert_not_called()

    def test_inflight_model_is_not_replayed_or_published_while_suspended(self):
        self.launch.side_effect = self.real_launch
        entered, release = threading.Event(), threading.Event()
        def cloud(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return copy.deepcopy(RESPONSE)
        self.cloud.side_effect = cloud
        session = self.start_live()
        self.new_peer(session)
        self.assertTrue(entered.wait(2))
        try:
            self.focus('suspend', session)
            self.restore_title()
            waiting = self.focus('resume', session)
            self.assertEqual(waiting['state'], 'suspended')
            self.assertEqual(waiting['reason'], 'FOCUS_WAITING_OPERATIONS')
        finally:
            release.set()
            self.live._SESSIONS[self.cp._workspace(self.root, self.contacts)]['model_worker'].join(3)
        self.assertEqual(self.tick(session)['state'], 'suspended')
        self.assertIsNone(self.tick(session)['result'])
        self.assertEqual(self.cloud.call_count, 1)

    def test_focus_lost_again_during_resume_keeps_original_grant_paused(self):
        session = self.start_live()
        self.focus('suspend', session)
        self.restore_title()
        def read(*args):
            self.focus('suspend', session)
            return tail(live_row(1), live_row(2))
        with mock.patch.object(self.live, '_resume_tail', side_effect=read):
            self.assertEqual(self.focus('resume', session)['state'], 'suspended')
        self.restore_title()
        with mock.patch.object(self.live, '_resume_tail', return_value=tail(live_row(1), live_row(2))):
            resumed = self.focus('resume', session)
        self.assertEqual(resumed['state'], 'active')
        self.assertEqual(resumed['calls_used'], 0)
        self.assertIsNone(resumed['result'])
        self.cloud.assert_not_called()

    def test_model_failure_during_pause_terminates_instead_of_retrying_on_return(self):
        session = self.start_live()
        def cloud(*args):
            self.focus('suspend', session)
            raise RuntimeError('SYNTHETIC_PROVIDER_FAILURE')
        self.cloud.side_effect = cloud
        result = self.new_peer(session)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['reason'], 'CALL_FAILED_POSSIBLY_CHARGED')
        self.restore_title()
        self.assertEqual(self.focus('resume', session)['state'], 'stopped')
        self.assertEqual(self.cloud.call_count, 1)


class FocusReaderTransportTests(unittest.TestCase):
    def test_explicit_resume_forwards_old_cursor_but_normal_failure_never_restarts(self):
        from dashboard.copilot import read_transport as transport
        self.addCleanup(transport.release_reader)
        transport.release_reader()
        output = (json.dumps(tail(live_row(1))) + '\n').encode()
        child = mock.Mock(stdin=io.BytesIO(), stdout=io.BytesIO(output), poll=lambda: None, wait=lambda **kwargs: 0)
        previous = tail(live_row(1))['cursor']
        with mock.patch.object(transport.subprocess, 'Popen', return_value=child) as spawn:
            value = transport.resume_tail('synthetic-person', previous)
            self.assertEqual(value['identity'], previous['identity'])
            self.assertEqual(json.loads(child.stdin.getvalue())['previous'], previous)
            with self.assertRaises(transport.LiveReadError):
                transport.resume_tail('synthetic-person', previous)
            self.assertEqual(spawn.call_count, 1)
            transport.release_reader()
            with self.assertRaises(transport.LiveReadError):
                transport.read_tail('synthetic-person', previous)
            with self.assertRaises(transport.LiveReadError):
                transport.resume_tail('synthetic-person', None)
            self.assertEqual(spawn.call_count, 1)


if __name__ == '__main__':
    unittest.main()
