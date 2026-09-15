"""Atomic provider event states; all data is synthetic and validation is offline."""
import copy
import itertools
import json
import os
import subprocess
import sys
import unittest

from dashboard import analysis_contract as contract, analysis_segments as segments
from dashboard import timeline_schema as schema
from dashboard.analysis_errors import OutputValidationError, public_error_detail
import test_timeline_diagnostics as fixtures


def valid_combination(kind, status, level):
    # Independent frozen pre-change acceptance matrix, not implementation logic.
    if level == 'insufficient':
        return status == 'unknown'
    if kind == 'plan':
        return status in ('planned', 'unknown')
    if kind in ('update', 'context') or level == 'inferred':
        return status in ('planned', 'in_progress', 'unknown')
    return True  # reported outcome


def wire(event):
    return {**{key: value for key, value in event.items()
               if key not in ('kind', 'status', 'evidence_level')},
            'event_state': ':'.join(event[key] for key in ('kind', 'status', 'evidence_level'))}


class AtomicEventStateTests(unittest.TestCase):
    def test_all_60_combinations_preserve_exact_old_acceptance_and_canonical_output(self):
        accepted = 0
        for kind, status, level in itertools.product(sorted(contract.EVENT_KINDS),
                                                     sorted(contract.EVENT_STATUSES),
                                                     sorted(contract.EVIDENCE_LEVELS)):
            with self.subTest(kind=kind, status=status, level=level):
                old = fixtures.event(kind=kind, status=status, evidence_level=level)
                new = fixtures.timeline(events=[wire(old)])
                before = copy.deepcopy(new)
                if valid_combination(kind, status, level):
                    expected = fixtures.validate(fixtures.timeline(events=[old]))
                    actual = fixtures.validate(new)
                    self.assertEqual(actual, expected)
                    self.assertNotIn('event_state', actual['events'][0])
                    accepted += 1
                else:
                    with self.assertRaises(OutputValidationError) as caught:
                        fixtures.validate(new)
                    self.assertEqual(caught.exception.detail['code'], 'OUTPUT_ENUM')
                    self.assertEqual(caught.exception.detail['field'], 'timeline.events[0].event_state')
                    with self.assertRaises(OutputValidationError) as legacy:
                        fixtures.validate(fixtures.timeline(events=[old]))
                    self.assertEqual(legacy.exception.detail['code'], 'OUTPUT_STATE')
                self.assertEqual(new, before)
        self.assertEqual(accepted, 28)

    def test_wire_requires_exact_shape_and_never_discards_ambiguous_fields(self):
        original = wire(fixtures.event())
        invalid = [{**original, key: fixtures.event()[key]} for key in ('kind', 'status', 'evidence_level')]
        invalid += [{**original, 'secret_field': 'SYNTHETIC_DO_NOT_ECHO'}, [], None]
        for key in original:
            missing = copy.deepcopy(original)
            del missing[key]
            invalid.append(missing)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(OutputValidationError) as caught:
                fixtures.validate(fixtures.timeline(events=[value]))
            self.assertNotIn('SYNTHETIC_DO_NOT_ECHO', str(caught.exception))

    def test_atomic_enum_is_exact_and_diagnostics_do_not_echo_provider_values(self):
        for value in (None, True, 1, [], {}, '', ' update:in_progress:reported',
                      'UPDATE:in_progress:reported', 'SYNTHETIC_DO_NOT_ECHO' * 100):
            with self.subTest(value_type=type(value)), self.assertRaises(OutputValidationError) as caught:
                fixtures.validate(fixtures.timeline(events=[{**wire(fixtures.event()), 'event_state': value}]))
            self.assertEqual(caught.exception.detail['field'], 'timeline.events[0].event_state')
            self.assertEqual(caught.exception.detail['code'], 'OUTPUT_ENUM' if isinstance(value, str) else 'OUTPUT_TYPE')
            self.assertNotIn('SYNTHETIC_DO_NOT_ECHO', str(caught.exception))

    def test_no_evidence_pair_date_link_or_text_validation_is_bypassed(self):
        for patch, code in (({'evidence': []}, 'OUTPUT_EVIDENCE_REQUIRED'),
                            ({'evidence': [{'date': '2099-01-02', 'sample_index': 1}]}, 'OUTPUT_EVIDENCE_REF'),
                            ({'date_from': '2098-01-01'}, 'OUTPUT_DATE_SCOPE'),
                            ({'related_event_id': 'e1'}, 'OUTPUT_LINK'),
                            ({'title': '字' * 41}, 'OUTPUT_LIMIT')):
            with self.subTest(code=code), self.assertRaises(OutputValidationError) as caught:
                fixtures.validate(fixtures.timeline(events=[wire(fixtures.event(**patch))]))
            self.assertEqual(caught.exception.detail['code'], code)
        unknown = fixtures.event(evidence_level='insufficient', status='unknown', evidence=[])
        self.assertEqual(fixtures.validate(fixtures.timeline(events=[wire(unknown)])),
                         fixtures.validate(fixtures.timeline(events=[unknown])))

    def test_merge_atomic_state_roundtrips_but_cannot_upgrade_a_plan(self):
        leaf = fixtures.validate(fixtures.timeline(events=[fixtures.event(kind='plan', status='planned')]))
        context = {'segment_summaries': [{'messages': 1, 'timeline': leaf}]}
        original = leaf['events'][0]
        try:
            result = fixtures.validate(fixtures.timeline(events=[wire(original)]), context)
        except OutputValidationError as error:
            self.fail(f'A valid atomic event must roundtrip; rejected with {error.detail["code"]}')
        self.assertEqual(result['events'], leaf['events'])
        changed = {**wire(original), 'event_state': 'outcome:realized:reported'}
        with self.assertRaises(OutputValidationError) as caught:
            fixtures.validate(fixtures.timeline(events=[changed]), context)
        self.assertEqual(caught.exception.detail['code'], 'OUTPUT_MERGE_CHANGED')
        self.assertEqual(leaf['events'][0]['status'], 'planned')

    def test_provider_examples_are_atomic_but_merge_inputs_remain_canonical(self):
        for name in ('LEAF_EXAMPLE_JSON', 'MERGE_EXAMPLE_JSON'):
            example = json.loads(getattr(contract, name))
            for event in example['timeline']['events']:
                self.assertEqual(set(event), (contract.EVENT_KEYS - {'kind', 'status', 'evidence_level'}) | {'event_state'})
                self.assertIn(event['event_state'], contract.EVENT_STATES)
        for packet in contract.EXAMPLE_SEGMENT_SUMMARIES:
            for event in packet['timeline']['events']:
                self.assertEqual(set(event), contract.EVENT_KEYS)
        for prompt in (contract.SYSTEM_PROMPT, contract.MERGE_PROMPT):
            self.assertIn('event_state', prompt)
            self.assertIn('8 个字段', prompt)
            self.assertNotIn('10 个字段', prompt)
            self.assertIn('不要分别输出 kind、status、evidence_level', prompt)
            self.assertIn('记录中出现', prompt)

    def test_mapping_and_prompt_revision_are_stable_across_hash_seeds(self):
        self.assertTrue(hasattr(contract, 'EVENT_STATES'))
        expected = {':'.join(values): values for values in itertools.product(sorted(contract.EVENT_KINDS),
                    sorted(contract.EVENT_STATUSES), sorted(contract.EVIDENCE_LEVELS)) if valid_combination(*values)}
        self.assertEqual(contract.EVENT_STATES, expected)
        self.assertEqual(list(contract.EVENT_STATES), sorted(contract.EVENT_STATES))
        outputs = []
        for seed in ('3', '97'):
            env = {**os.environ, 'PYTHONHASHSEED': seed, 'PYTHONDONTWRITEBYTECODE': '1'}
            outputs.append(subprocess.check_output([sys.executable, '-B', '-c',
                'from dashboard.analysis_segments import prompt_revision; print(prompt_revision())'],
                env=env, text=True).strip())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], segments.prompt_revision())

    def test_event_state_error_paths_are_fixed_and_bounded(self):
        detail = public_error_detail({'code': 'OUTPUT_ENUM', 'field': 'timeline.events[0].event_state',
                                      'message': 'SYNTHETIC_DO_NOT_ECHO'})
        self.assertIsNotNone(detail)
        self.assertNotIn('SYNTHETIC_DO_NOT_ECHO', json.dumps(detail))
        for field in ('timeline.events[1200].event_state', 'timeline.events[0].event_state.raw',
                      'timeline.events[0].event_state<script>'):
            self.assertIsNone(public_error_detail({'code': 'OUTPUT_ENUM', 'field': field}))


if __name__ == '__main__':
    unittest.main()
