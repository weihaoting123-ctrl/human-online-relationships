"""Explicit reply style uses invented archives and an offline provider boundary."""
import copy
import hashlib
import itertools
import unittest
from unittest import mock

from dashboard import analysis as ai
from dashboard.copilot.context import scope
from tests.test_copilot_service import CopilotFixture, RESPONSE


DEFAULT_STYLE = {'tone': 'natural', 'empathy': 'balanced', 'length': 'short'}


class ReplyStyleScopeTests(unittest.TestCase):
    def setUp(self):
        self.request = {'bundle_id': 'synthetic-person', 'direction': 'relaxed', 'binding_revision': 1}

    def test_legacy_request_receives_complete_natural_short_default(self):
        result, _, _ = scope(self.request)
        self.assertEqual(result.get('reply_style'), DEFAULT_STYLE)

    def test_partial_style_fills_missing_dimensions_without_modifying_request(self):
        selected = {'tone': 'playful'}
        result, _, _ = scope({**self.request, 'reply_style': selected})
        self.assertEqual(result.get('reply_style'), {**DEFAULT_STYLE, 'tone': 'playful'})
        self.assertEqual(selected, {'tone': 'playful'})
        selected['tone'] = 'direct'
        self.assertEqual(result['reply_style']['tone'], 'playful')

    def test_all_documented_style_combinations_are_accepted(self):
        for tone, empathy, length in itertools.product(
                ('natural', 'playful', 'gentle', 'direct'),
                ('restrained', 'balanced', 'attentive'), ('short', 'normal')):
            style = dict(tone=tone, empathy=empathy, length=length)
            with self.subTest(style=style):
                result, _, _ = scope({**self.request, 'reply_style': style})
                self.assertEqual(result.get('reply_style'), style)

    def test_style_rejects_nonobjects_unknown_keys_and_unlisted_values(self):
        invalid = [None, False, '', [], ['natural'], {'PRIVATE_INVALID': 'natural'}]
        for field in DEFAULT_STYLE:
            invalid.extend({field: item} for item in (None, True, 0, [], {}, 'PRIVATE_INVALID', 'Natural', ' natural'))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ai.AnalysisError) as caught:
                scope({**self.request, 'reply_style': value})
            self.assertNotIn('PRIVATE_INVALID', str(caught.exception))


class ReplyStyleServiceTests(CopilotFixture):
    def test_preview_exposes_normalized_style_without_provider_call_or_persistence(self):
        before = set(self.root.rglob('*'))
        with mock.patch.object(ai, '_request_json', side_effect=AssertionError('Unexpected provider call')):
            preview = self.preview(reply_style={'empathy': 'attentive'})
        self.assertEqual(preview['scope'].get('reply_style'), {**DEFAULT_STYLE, 'empathy': 'attentive'})
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_confirmed_request_sends_exact_preview_style_once(self):
        style = {'tone': 'gentle', 'empathy': 'attentive', 'length': 'normal'}
        with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)) as provider:
            preview = self.preview(reply_style=style)
            provider.assert_not_called()
            with self.assertRaises(ai.AnalysisError):
                self.run_preview(preview, consent=False)
            provider.assert_not_called()
            style['tone'] = 'direct'
            result = self.run_preview(preview)
            self.assertEqual(provider.call_args.args[2].get('reply_style'),
                             {'tone': 'gentle', 'empathy': 'attentive', 'length': 'normal'})
            self.assertEqual(result['status'], 'ok')
            self.assertEqual({item['style'] for item in result['replies']}, {'natural', 'warm', 'invite'})
            with self.assertRaises(ai.AnalysisError):
                self.run_preview(preview)
            self.assertEqual(provider.call_count, 1)

    def test_legacy_confirmed_request_sends_default_style(self):
        preview = self.preview()
        with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)) as provider:
            self.run_preview(preview)
        self.assertEqual(provider.call_args.args[2].get('reply_style'), DEFAULT_STYLE)

    def test_run_cannot_override_previously_confirmed_style(self):
        preview = self.preview(reply_style={'tone': 'gentle'})
        with mock.patch.object(ai, '_request_json', return_value=copy.deepcopy(RESPONSE)) as provider:
            with self.assertRaises(ai.AnalysisError):
                self.run_preview(preview, reply_style={'tone': 'direct'})
            provider.assert_not_called()

    def test_changed_style_prompt_invalidates_older_preview_before_provider_call(self):
        with mock.patch.object(self.cp, 'PROMPT_REVISION', 'previous-style-contract'):
            preview = self.preview()
        with mock.patch.object(ai, '_request_json') as provider:
            with self.assertRaisesRegex(ai.AnalysisError, '回复提示版本已变化'):
                self.run_preview(preview)
            provider.assert_not_called()

    def test_prompt_defines_plain_chat_style_and_bounds_empathy_without_manipulation(self):
        prompt = self.cp.SYSTEM_PROMPT
        self.assertEqual(self.cp.PROMPT_REVISION, hashlib.sha256(prompt.encode('utf-8')).hexdigest())
        for expected in ('reply_style', 'natural', 'playful', 'gentle', 'direct', 'restrained',
                         'balanced', 'attentive', 'short', 'normal', '1–2 个短句',
                         '没有情绪线索', '不机械复述', '不每句反问', '不捏造', '不提供操控策略'):
            with self.subTest(expected=expected):
                self.assertIn(expected, prompt)


if __name__ == '__main__':
    unittest.main()
