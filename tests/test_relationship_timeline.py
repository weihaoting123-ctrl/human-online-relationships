"""Content-free timeline tests using only synthetic messages and an injected clock."""

import copy
import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dashboard.analysis_metrics import build_metrics

if importlib.util.find_spec("dashboard.relationship_timeline") is not None:
    from dashboard.relationship_timeline import build_relationship_timeline
else:
    build_relationship_timeline = None


def message(at, sender="me", kind="text", **extra):
    stamp = at.timestamp() if isinstance(at, datetime) else at
    return {"timestamp": stamp, "sender": sender, "type": kind, **extra}


class RelationshipTimelineTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(build_relationship_timeline), "Local relationship timeline engine is missing")
        self.now = datetime(2026, 9, 6, 12)

    def build(self, messages, scope=None, now=None):
        return build_relationship_timeline(
            {"messages": messages}, {} if scope is None else scope,
            now=self.now if now is None else now,
        )

    def assertStamp(self, actual, expected):
        self.assertIsNotNone(datetime.fromisoformat(actual).utcoffset())
        self.assertEqual(datetime.fromisoformat(actual).timestamp(), expected.timestamp())

    def test_empty_and_malformed_payloads_have_bounded_json_safe_schema(self):
        for payload in (None, [], {}, {"messages": {}}, {"messages": None}, {"messages": []}):
            with self.subTest(payload=payload):
                result = build_relationship_timeline(payload, {}, now=self.now)
                self.assertEqual(result["version"], 1)
                self.assertEqual(result["basis"], "local_archive")
                self.assertStamp(result["as_of"], self.now)
                self.assertIsNone(result["source_max_date"])
                self.assertIsNone(result["last_contact"]["last_record_at"])
                self.assertIsNone(result["last_contact"]["elapsed_days"])
                self.assertIsNone(result["last_contact"]["last_me_at"])
                self.assertIsNone(result["last_contact"]["last_other_at"])
                self.assertEqual(result["scope"]["message_count"], 0)
                self.assertEqual(result["frequency"]["weekly"], [])
                self.assertEqual(result["frequency"]["phases"], [])
                self.assertEqual(result["gaps"], [])
                self.assertEqual(result["nodes"], [])
                for value in result["limits"].values():
                    self.assertEqual(value, {"total": 0, "returned": 0, "truncated": False})
                json.dumps(result, allow_nan=False)

    def test_old_scope_does_not_replace_last_contact_from_whole_archive(self):
        old = datetime(2024, 1, 10, 8)
        last_me = self.now - timedelta(days=2)
        last_other = self.now - timedelta(hours=12)
        result = self.build([message(last_other, "other"), message(old), message(last_me)],
                            {"date_from": "2024-01-01", "date_to": "2024-12-31"})
        contact = result["last_contact"]
        self.assertEqual(contact["basis"], "whole_archive_non_system_non_future")
        self.assertEqual(contact["elapsed_days"], 0)
        self.assertStamp(contact["last_record_at"], last_other)
        self.assertStamp(contact["last_me_at"], last_me)
        self.assertStamp(contact["last_other_at"], last_other)
        self.assertStamp(result["scope"]["last_record_at"], old)
        self.assertEqual(result["scope"]["message_count"], 1)
        self.assertEqual(result["source_max_date"], last_other.date().isoformat())

    def test_empty_scope_still_has_whole_archive_last_contact(self):
        result = self.build([message(self.now - timedelta(days=3))], {"date_from": "2027-01-01"})
        self.assertEqual(result["scope"]["message_count"], 0)
        self.assertIsNone(result["scope"]["last_record_at"])
        self.assertEqual(result["last_contact"]["elapsed_days"], 3)
        self.assertEqual(result["nodes"], [])

    def test_future_invalid_and_system_records_never_advance_last_contact(self):
        last = self.now - timedelta(days=5)
        rows = [message(last), message(self.now - timedelta(seconds=1), kind="system"),
                message(self.now, sender="system"), message(self.now + timedelta(microseconds=1)),
                message(datetime(2030, 2, 1)), message("bad"), None, {},
                message(float("nan")), message(True), message(10 ** 10000)]
        result = self.build(rows)
        self.assertEqual(result["excluded"], {"invalid_timestamp": 6, "future_timestamp": 2, "system_messages": 2})
        self.assertEqual(result["scope"]["message_count"], 1)
        self.assertEqual(result["last_contact"]["elapsed_days"], 5)
        self.assertEqual(result["source_max_date"], "2030-02-01")
        self.assertStamp(result["last_contact"]["last_record_at"], last)
        self.assertTrue(any("未来" in notice for notice in result["notices"]))
        self.assertTrue(any("无效" in notice for notice in result["notices"]))

    def test_only_future_and_system_archive_has_no_claim_of_elapsed_contact(self):
        result = self.build([message(self.now + timedelta(days=1)), message(self.now, kind="system")])
        self.assertIsNone(result["last_contact"]["elapsed_days"])
        self.assertIsNone(result["last_contact"]["last_record_at"])

    def test_seconds_milliseconds_and_explicit_utc_share_local_date_scope(self):
        start = datetime(2026, 9, 5)
        end = start + timedelta(days=1)
        rows = [message(start), message(start.timestamp() * 1000),
                message(start.astimezone(timezone.utc).isoformat()),
                message(end - timedelta(milliseconds=1), "other"), message(end)]
        result = self.build(rows, {"date_from": "2026-09-05", "date_to": "2026-09-05"})
        self.assertEqual(result["scope"]["message_count"], 4)
        self.assertStamp(result["scope"]["first_record_at"], start)
        self.assertStamp(result["scope"]["last_record_at"], end - timedelta(milliseconds=1))
        self.assertStamp(result["last_contact"]["last_record_at"], end)
        self.assertEqual(result["last_contact"]["elapsed_days"], 0)

    def test_elapsed_days_are_complete_24_hours_and_clock_is_not_scope_date(self):
        stamp = datetime(2026, 9, 4, 23, 30)
        before = self.build([message(stamp)], now=datetime(2026, 9, 5, 23, 29, 59))
        after = self.build([message(stamp)], now=datetime(2026, 9, 5, 23, 30))
        midnight = self.build([message(stamp)], now=datetime(2026, 9, 5, 0, 1))
        self.assertEqual(before["last_contact"]["elapsed_days"], 0)
        self.assertEqual(after["last_contact"]["elapsed_days"], 1)
        self.assertEqual(midnight["last_contact"]["elapsed_days"], 0)

    def test_aware_now_uses_same_instant_as_local_clock(self):
        stamp = self.now - timedelta(days=8, hours=1)
        local = self.build([message(stamp)])
        utc = self.build([message(stamp)], now=self.now.astimezone(timezone.utc))
        self.assertEqual(local, utc)

    def test_default_now_reads_current_clock_on_each_request(self):
        clock_a = self.now
        clock_b = self.now + timedelta(days=1)
        with patch("dashboard.relationship_timeline.datetime", wraps=datetime) as clock:
            clock.now.side_effect = [clock_a, clock_b]
            first = build_relationship_timeline({"messages": [message(self.now)]}, {})
            second = build_relationship_timeline({"messages": [message(self.now)]}, {})
        self.assertEqual(first["last_contact"]["elapsed_days"], 0)
        self.assertEqual(second["last_contact"]["elapsed_days"], 1)

    def test_frequency_threshold_uses_active_week_median_and_merges_adjacent_weeks(self):
        monday = datetime(2026, 7, 6, 12)
        counts = [10, 20, 21, 10, 10, 30]
        rows = [message(monday + timedelta(weeks=week, seconds=index))
                for week, count in enumerate(counts) for index in range(count)]
        result = self.build(list(reversed(rows)))
        frequency = result["frequency"]
        self.assertEqual(frequency["week_start"], "monday")
        self.assertEqual(frequency["baseline_active_week_median"], 15)
        self.assertEqual(frequency["threshold"], 23)
        self.assertEqual(frequency["active_weeks"], 6)
        self.assertEqual([week["frequent"] for week in frequency["weekly"]], [False, False, False, False, False, True])
        self.assertEqual(frequency["phases"], [{"start_date": "2026-08-10", "end_date": "2026-08-10",
                                              "weeks": 1, "message_count": 30}])
        # A low baseline makes two neighboring busy weeks a single phase.
        rows += [message(monday + timedelta(weeks=week)) for week in (-4, -3, -2, -1)]
        merged = self.build(rows)["frequency"]
        self.assertEqual(merged["threshold"], 20)
        self.assertEqual(merged["phases"][0], {"start_date": "2026-07-13", "end_date": "2026-07-20",
                                                "weeks": 2, "message_count": 41})

    def test_inactive_week_breaks_frequent_phase_without_dense_calendar_fill(self):
        monday = datetime(2026, 7, 6, 12)
        rows = [message(monday + timedelta(weeks=week, seconds=index))
                for week, count in [(0, 20), (2, 20), (3, 1), (4, 1), (5, 1)]
                for index in range(count)]
        result = self.build(rows)
        self.assertEqual(len(result["frequency"]["weekly"]), 5)
        self.assertEqual(len(result["frequency"]["phases"]), 2)
        self.assertEqual([phase["weeks"] for phase in result["frequency"]["phases"]], [1, 1])

    def test_gaps_use_consecutive_non_system_records_at_exact_7_day_boundary(self):
        first = datetime(2026, 8, 1, 12)
        second = first + timedelta(days=7)
        third = second + timedelta(days=7, seconds=-1)
        result = self.build([message(third), message(second), message(first),
                             message(first + timedelta(days=3), kind="system")])
        self.assertEqual(len(result["gaps"]), 1)
        gap = result["gaps"][0]
        self.assertStamp(gap["start_at"], first)
        self.assertStamp(gap["end_at"], second)
        self.assertEqual(gap["elapsed_days"], 7)
        self.assertEqual([node["type"] for node in result["nodes"]].count("gap"), 1)
        self.assertEqual(result["nodes"][0]["type"], "first_record")
        self.assertEqual(result["nodes"][-1]["type"], "last_record")

    def test_scope_does_not_inherit_gap_or_frequency_from_other_dates(self):
        result = self.build([message(datetime(2024, 1, 1)), message(datetime(2026, 8, 1))],
                            {"date_from": "2026-01-01", "date_to": "2026-12-31"})
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["frequency"]["active_weeks"], 1)
        self.assertEqual([node["type"] for node in result["nodes"]], ["first_record", "last_record"])

    def test_large_sparse_span_keeps_newest_limits_and_first_last_nodes(self):
        start = datetime(2000, 1, 3, 12)
        rows = [message(start + timedelta(weeks=week)) for week in range(600)]
        result = self.build(rows, {"date_from": "0001-01-01", "date_to": "9999-12-31"})
        self.assertEqual(result["scope"]["message_count"], 600)
        self.assertEqual(result["limits"]["weekly"], {"total": 600, "returned": 260, "truncated": True})
        self.assertEqual(result["limits"]["gaps"], {"total": 599, "returned": 50, "truncated": True})
        self.assertEqual(result["limits"]["nodes"], {"total": 601, "returned": 100, "truncated": True})
        self.assertEqual(result["frequency"]["weekly"][0]["week_start"], (start + timedelta(weeks=340)).date().isoformat())
        self.assertEqual(result["nodes"][0]["type"], "first_record")
        self.assertStamp(result["nodes"][0]["at"], start)
        self.assertEqual(result["nodes"][-1]["type"], "last_record")
        self.assertStamp(result["nodes"][-1]["at"], start + timedelta(weeks=599))

    def test_phases_are_bounded_without_changing_baseline_or_total(self):
        start = datetime(2010, 1, 4, 12)
        rows = [message(start + timedelta(weeks=week, seconds=index))
                for week in range(240) for index in range(20 if week % 3 == 0 else 1)]
        result = self.build(rows)
        self.assertEqual(result["frequency"]["threshold"], 20)
        self.assertEqual(result["limits"]["phases"], {"total": 80, "returned": 50, "truncated": True})
        self.assertEqual(result["frequency"]["phases"][0]["start_date"], (start + timedelta(weeks=90)).date().isoformat())

    def test_untrusted_content_identity_and_keys_are_never_returned_or_mutated(self):
        secret = "PRIVATE-NAME-ID-TEXT-AND-PATH"
        payload = {"messages": [message(self.now, sender=secret, content=secret, message_id=secret,
                                        transcript=secret, sender_name=secret, audio_path=secret)],
                   "contact_display": secret, secret: secret}
        scope = {"bundle_id": secret, "focus": secret}
        original = copy.deepcopy([payload, scope])
        result = build_relationship_timeline(payload, scope, now=self.now)
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn(secret, serialized)
        self.assertEqual(result["last_contact"]["reason_status"], "unknown")
        self.assertIn("不能", result["last_contact"]["reason"])
        self.assertTrue(any("线下" in notice and "归档" in notice for notice in result["notices"]))
        self.assertEqual([payload, scope], original)

    def test_invalid_scopes_and_clock_fail_closed_without_reflecting_input(self):
        for scope in (None, [], {"date_from": "PRIVATE"}, {"date_to": 1},
                      {"date_from": "2026-09-06", "date_to": "2026-09-05"}):
            with self.subTest(scope=scope), self.assertRaises(ValueError) as raised:
                build_relationship_timeline({}, scope, now=self.now)
            self.assertNotIn("PRIVATE", str(raised.exception))
        for now in ("PRIVATE", True, float("nan"), {}):
            with self.subTest(now=now), self.assertRaises(ValueError) as raised:
                build_relationship_timeline({}, {}, now=now)
            self.assertNotIn("PRIVATE", str(raised.exception))


class TimelineMetricsIntegrationTests(unittest.TestCase):
    def test_metrics_include_same_local_timeline_without_changing_old_counting_basis(self):
        now = datetime(2026, 9, 6, 12)
        rows = [message(now), message(now + timedelta(days=1)), message(now, kind="system")]
        result = build_metrics({"messages": rows}, {})
        self.assertIn("timeline", result, "Metrics must expose the new deterministic timeline")
        result = build_metrics({"messages": rows}, {}, now=now)
        self.assertEqual(result["totals"]["messages"], 3)
        self.assertEqual(result["timeline"]["scope"]["message_count"], 1)
        self.assertEqual(result["timeline"], build_relationship_timeline({"messages": rows}, {}, now=now))


if __name__ == "__main__":
    unittest.main()
