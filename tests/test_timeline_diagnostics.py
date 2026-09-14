"""Synthetic-only field diagnostics and immutable merge regression cases."""
import copy
import unittest
from unittest import mock

from dashboard import timeline_schema as schema
from dashboard.analysis_errors import OutputValidationError


SAMPLE = [{"date": "2099-01-01", "sample_index": 1},
          {"date": "2099-01-02", "sample_index": 2},
          {"date": "2099-01-03", "sample_index": 3}]
CONTEXT = {"sample": SAMPLE, "date_from": "2099-01-01", "date_to": "2099-01-03",
           "segment": {"index": 1}}


def event(**patch):
    return {"id": "e1", "date_from": "2099-01-02", "date_to": "2099-01-02",
            "kind": "update", "title": "合成进度", "summary": "合成材料记载进度。",
            "status": "in_progress", "evidence_level": "reported", "related_event_id": None,
            "evidence": [{"date": "2099-01-02", "sample_index": 2}], **patch}


def timeline(events=None, **patch):
    return {"version": 1, "generated": True, "events": [event()] if events is None else events,
            "no_contact_reason": {"kind": "unknown", "summary": "原因未知。", "evidence": [],
                                  "limitations": []}, **patch}


def validate(value, context=None):
    return schema.validate(value, CONTEXT if context is None else context, lambda text, limit: text)


class _DiagnosticAssertions:
    def assert_error(self, value, code, field, context=None):
        with self.assertRaises(OutputValidationError) as caught:
            validate(value, context)
        self.assertEqual(caught.exception.detail["code"], code)
        self.assertEqual(caught.exception.detail["field"], field)
        return caught.exception


class TimelineDiagnosticTests(_DiagnosticAssertions, unittest.TestCase):
    def test_timeline_type_required_fields_and_counts(self):
        cases = [([], "OUTPUT_TYPE", "timeline"),
                 ({**timeline(), "version": True}, "OUTPUT_TYPE", "timeline.version"),
                 ({**timeline(), "version": 2}, "OUTPUT_ENUM", "timeline.version"),
                 ({**timeline(), "generated": "yes"}, "OUTPUT_TYPE", "timeline.generated"),
                 (timeline(events="invalid"), "OUTPUT_TYPE", "timeline.events"),
                 (timeline(events=[event()] * 7), "OUTPUT_LIMIT", "timeline.events")]
        missing = timeline()
        del missing["events"]
        cases.append((missing, "OUTPUT_FIELDS", "timeline.events"))
        for value, code, field in cases:
            with self.subTest(code=code, field=field):
                self.assert_error(value, code, field)

    def test_unknown_field_names_and_bad_values_never_enter_diagnostics(self):
        marker = "SYNTHETIC-SECRET-DO-NOT-ECHO"
        error = self.assert_error(timeline(events=[event(**{marker: marker})]),
                                  "OUTPUT_FIELDS", "timeline.events[0]")
        self.assertNotIn(marker, str(error))
        self.assertNotIn(marker, str(error.detail))
        error = self.assert_error(timeline(events=[event(title=marker * 10)]),
                                  "OUTPUT_LIMIT", "timeline.events[0].title")
        self.assertNotIn(marker, str(error))

    def test_event_fields_types_enums_and_text_lengths(self):
        cases = [({"kind": []}, "OUTPUT_TYPE", "kind"),
                 ({"status": "done"}, "OUTPUT_ENUM", "status"),
                 ({"evidence_level": "certain"}, "OUTPUT_ENUM", "evidence_level"),
                 ({"title": False}, "OUTPUT_TYPE", "title"),
                 ({"title": "字" * 41}, "OUTPUT_LIMIT", "title"),
                 ({"summary": "字" * 141}, "OUTPUT_LIMIT", "summary"),
                 ({"id": "s1-e1"}, "OUTPUT_ID", "id"),
                 ({"related_event_id": "null"}, "OUTPUT_ID", "related_event_id")]
        for patch, code, field in cases:
            with self.subTest(field=field, code=code):
                self.assert_error(timeline(events=[event(**patch)]), code, "timeline.events[0]." + field)
        missing = event()
        del missing["title"]
        self.assert_error(timeline(events=[missing]), "OUTPUT_FIELDS", "timeline.events[0].title")
        self.assert_error(timeline(events=[None]), "OUTPUT_TYPE", "timeline.events[0]")

    def test_date_format_calendar_membership_and_order(self):
        cases = [({"date_from": "2099-02-30"}, "OUTPUT_DATE", "date_from"),
                 ({"date_to": "Jan 2"}, "OUTPUT_DATE", "date_to"),
                 ({"date_from": "2098-12-31"}, "OUTPUT_DATE_SCOPE", "date_from"),
                 ({"date_to": "2099-01-04"}, "OUTPUT_DATE_SCOPE", "date_to"),
                 ({"date_from": "2099-01-03"}, "OUTPUT_DATE_SCOPE", "date_to")]
        for patch, code, field in cases:
            with self.subTest(field=field, code=code):
                self.assert_error(timeline(events=[event(**patch)]), code, "timeline.events[0]." + field)
        self.assert_error(timeline(), "OUTPUT_DATE_SCOPE", "timeline.events[0].date_from",
                          {**CONTEXT, "date_from": "2099-01-03"})
        self.assert_error(timeline(), "OUTPUT_DATE_SCOPE", "timeline.events[0].date_to",
                          {**CONTEXT, "date_to": "2099-01-01"})

    def test_evidence_shape_count_date_and_integer_type(self):
        cases = [(None, "OUTPUT_TYPE", "evidence"),
                 ([{}] * 4, "OUTPUT_LIMIT", "evidence"),
                 ([None], "OUTPUT_TYPE", "evidence[0]"),
                 ([{"date": "2099-01-02"}], "OUTPUT_FIELDS", "evidence[0].sample_index"),
                 ([{"date": "2099-01-02", "sample_index": 2, "private-key": "secret"}],
                  "OUTPUT_FIELDS", "evidence[0]"),
                 ([{"date": "2099-02-30", "sample_index": 2}], "OUTPUT_DATE", "evidence[0].date"),
                 ([{"date": "2099-01-02", "sample_index": True}], "OUTPUT_TYPE", "evidence[0].sample_index"),
                 ([{"date": "2099-01-02", "sample_index": 80001}], "OUTPUT_EVIDENCE_REF", "evidence[0].sample_index")]
        for refs, code, field in cases:
            with self.subTest(field=field, code=code):
                self.assert_error(timeline(events=[event(evidence=refs)]), code, "timeline.events[0]." + field)

    def test_evidence_pair_membership_duplicates_range_and_requirement(self):
        self.assert_error(timeline(events=[event(evidence=[{"date": "2099-01-02", "sample_index": 1}])]),
                          "OUTPUT_EVIDENCE_REF", "timeline.events[0].evidence[0].sample_index")
        self.assert_error(timeline(events=[event(evidence=event()["evidence"] * 2)]),
                          "OUTPUT_EVIDENCE_DUP", "timeline.events[0].evidence[1]")
        self.assert_error(timeline(events=[event(evidence=[{"date": "2099-01-01", "sample_index": 1}])]),
                          "OUTPUT_EVIDENCE_RANGE", "timeline.events[0].evidence[0].date")
        self.assert_error(timeline(events=[event(evidence=[])]),
                          "OUTPUT_EVIDENCE_REQUIRED", "timeline.events[0].evidence")

    def test_state_mismatches_and_generated_false_have_exact_fields(self):
        for patch in ({"evidence_level": "insufficient"}, {"kind": "plan"},
                      {"status": "realized"}, {"status": "cancelled", "kind": "outcome", "evidence_level": "inferred"}):
            with self.subTest(patch=patch):
                self.assert_error(timeline(events=[event(**patch)]), "OUTPUT_STATE", "timeline.events[0].status")
        self.assert_error(timeline(generated=False), "OUTPUT_STATE", "timeline.generated")

    def test_duplicate_self_missing_future_and_cyclic_links(self):
        self.assert_error(timeline(events=[event(), event()]), "OUTPUT_DUPLICATE_ID", "timeline.events[1].id")
        for related in ("e1", "e2"):
            with self.subTest(related=related):
                self.assert_error(timeline(events=[event(related_event_id=related)]),
                                  "OUTPUT_LINK", "timeline.events[0].related_event_id")
        future = event(id="e2", date_from="2099-01-03", date_to="2099-01-03",
                       evidence=[{"date": "2099-01-03", "sample_index": 3}])
        self.assert_error(timeline(events=[event(related_event_id="e2"), future]),
                          "OUTPUT_LINK", "timeline.events[0].related_event_id")
        self.assert_error(timeline(events=[event(related_event_id="e2"), event(id="e2", related_event_id="e1")]),
                          "OUTPUT_LINK", "timeline.events[1].related_event_id")

    def test_reason_has_precise_shape_type_enum_length_and_evidence_fields(self):
        reason = timeline()["no_contact_reason"]
        cases = [(None, "OUTPUT_TYPE", "timeline.no_contact_reason"),
                 ({**reason, "kind": "guess"}, "OUTPUT_ENUM", "timeline.no_contact_reason.kind"),
                 ({**reason, "summary": 1}, "OUTPUT_TYPE", "timeline.no_contact_reason.summary"),
                 ({**reason, "kind": "explicit"}, "OUTPUT_EVIDENCE_REQUIRED", "timeline.no_contact_reason.evidence"),
                 ({**reason, "limitations": "text"}, "OUTPUT_TYPE", "timeline.no_contact_reason.limitations"),
                 ({**reason, "limitations": ["x"] * 4}, "OUTPUT_LIMIT", "timeline.no_contact_reason.limitations"),
                 ({**reason, "limitations": ["字" * 141]}, "OUTPUT_LIMIT", "timeline.no_contact_reason.limitations[0]")]
        for value, code, field in cases:
            with self.subTest(code=code, field=field):
                self.assert_error(timeline(no_contact_reason=value), code, field)


class TimelineMergeDiagnosticTests(_DiagnosticAssertions, unittest.TestCase):
    def packet_context(self, values):
        return {k: v for k, v in {**CONTEXT, "segment_summaries": [
            {"messages": 3, "timeline": value} for value in values]}.items() if k != "sample"}

    def test_merge_preserves_endpoints_without_evidence_on_each_endpoint(self):
        leaf = validate(timeline(events=[event(date_from="2099-01-01")]))
        checked = validate(timeline(events=copy.deepcopy(leaf["events"])), self.packet_context([leaf]))
        self.assertEqual(checked["events"], leaf["events"])
        self.assertEqual(checked["coverage"]["date_from"], "2099-01-01")

    def test_merge_cannot_borrow_another_original_endpoint_to_rewrite_event(self):
        leaf = validate(timeline(events=[event(date_from="2099-01-01"), event(id="e2")]))
        revised = {**leaf["events"][1], "date_from": "2099-01-01"}
        self.assert_error(timeline(events=[revised]), "OUTPUT_MERGE_CHANGED", "timeline.events[0].date_from",
                          self.packet_context([leaf]))

    def test_merge_rejects_new_dates_and_does_not_expand_evidence_pairs(self):
        leaf = validate(timeline(events=[event(date_from="2099-01-01")]))
        ctx = self.packet_context([leaf])
        self.assert_error(timeline(events=[{**leaf["events"][0], "date_to": "2099-01-03"}]),
                          "OUTPUT_DATE_SCOPE", "timeline.events[0].date_to", ctx)
        self.assert_error(timeline(events=[{**leaf["events"][0], "evidence": [SAMPLE[0]]}]),
                          "OUTPUT_EVIDENCE_REF", "timeline.events[0].evidence[0].sample_index", ctx)

    def test_merge_rejects_missing_original_and_modified_wording(self):
        leaf = validate(timeline())
        for patch, field in (({"id": "s2-e1"}, "id"), ({"summary": "合成改写"}, "summary")):
            with self.subTest(field=field):
                self.assert_error(timeline(events=[{**leaf["events"][0], **patch}]),
                                  "OUTPUT_MERGE_CHANGED", "timeline.events[0]." + field, self.packet_context([leaf]))

    def test_merge_preserves_nonnull_link_outside_compact_packet(self):
        leaf = validate(timeline(events=[event(), event(id="e2", related_event_id="e1")]))
        compact = {**leaf, "events": [leaf["events"][1]], "events_truncated": True}
        checked = validate(timeline(events=copy.deepcopy(compact["events"])), self.packet_context([compact]))
        self.assertEqual(checked["events"][0]["related_event_id"], "s1-e1")
        self.assertEqual(len(schema.combine([leaf], checked)["events"]), 2)
        with self.assertRaises(OutputValidationError) as caught:
            schema.combine([compact], checked)
        self.assertEqual(caught.exception.detail["code"], "OUTPUT_LINK")

    def test_merge_new_links_must_be_local_earlier_and_acyclic(self):
        leaf = validate(timeline(events=[event(), event(id="e2", date_from="2099-01-03", date_to="2099-01-03",
                                                       evidence=[SAMPLE[2]])]))
        ctx = self.packet_context([leaf])
        for related in ("s9-e1", "s1-e1", "s1-e2"):
            with self.subTest(related=related):
                self.assert_error(timeline(events=[{**leaf["events"][0], "related_event_id": related}]),
                                  "OUTPUT_LINK", "timeline.events[0].related_event_id", ctx)
        same_day = validate(timeline(events=[event(), event(id="e2")]))
        self.assert_error(timeline(events=[{**same_day["events"][0], "related_event_id": "s1-e2"},
                                           {**same_day["events"][1], "related_event_id": "s1-e1"}]),
                          "OUTPUT_LINK", "timeline.events[1].related_event_id", self.packet_context([same_day]))

    def test_merge_existing_link_cannot_be_erased_or_redirected(self):
        leaf = validate(timeline(events=[event(), event(id="e2", related_event_id="e1"), event(id="e3")]))
        for related in (None, "s1-e3"):
            with self.subTest(related=related):
                self.assert_error(timeline(events=[{**leaf["events"][1], "related_event_id": related}]),
                                  "OUTPUT_MERGE_CHANGED", "timeline.events[0].related_event_id", self.packet_context([leaf]))

    def test_merge_reason_must_inherit_limitations_as_well_as_cause_and_evidence(self):
        reason = {"kind": "explicit", "summary": "合成自述暂停联系原因。", "evidence": [SAMPLE[1]],
                  "limitations": ["合成限制。"]}
        leaf = validate(timeline(no_contact_reason=reason))
        inherited = leaf["no_contact_reason"]
        self.assertEqual(validate(timeline(events=[], no_contact_reason=copy.deepcopy(inherited)),
                                  self.packet_context([leaf]))["no_contact_reason"], inherited)
        self.assert_error(timeline(events=[], no_contact_reason={**inherited, "limitations": []}),
                          "OUTPUT_REASON_CHANGED", "timeline.no_contact_reason", self.packet_context([leaf]))

    def test_combine_reports_immutable_changes_links_and_capacity(self):
        leaf = validate(timeline(events=[event(), event(id="e2", related_event_id="e1")]))
        for patch, field in (({"summary": "合成改写"}, "summary"), ({"related_event_id": "s2-e1"}, "related_event_id")):
            proposal = timeline(events=[{**leaf["events"][1], **patch}])
            with self.subTest(field=field), self.assertRaises(OutputValidationError) as caught:
                schema.combine([leaf], proposal)
            self.assertEqual(caught.exception.detail["code"], "OUTPUT_MERGE_CHANGED")
            self.assertEqual(caught.exception.detail["field"], "timeline.events[0]." + field)
        with mock.patch.object(schema, "MAX_REPORT_EVENTS", 1), self.assertRaises(OutputValidationError) as caught:
            schema.combine([leaf])
        self.assertEqual(caught.exception.detail["code"], "OUTPUT_LIMIT")
        self.assertEqual(caught.exception.detail["field"], "timeline.events")


if __name__ == "__main__":
    unittest.main()
