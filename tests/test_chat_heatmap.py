"""Pure activity aggregates from invented timestamps; never real archives."""

import importlib
import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone


class MetadataOnlyMessage(dict):
    def get(self, key, default=None):
        if key not in {"timestamp", "type"}:
            raise AssertionError("Heatmap inspected a non-metadata field")
        return super().get(key, default)


class ChatHeatmapTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("dashboard.chat_heatmap"),
                             "The pure chat heatmap module must exist")
        self.build = importlib.import_module("dashboard.chat_heatmap").build_chat_heatmap
        self.now = datetime(2024, 3, 10, 12)

    def heatmap(self, messages, scope=None, now=None):
        return self.build({"messages": messages}, scope if scope is not None else {},
                          now=now or self.now)

    def test_dense_leap_days_inclusive_scope_and_conservation(self):
        result = self.heatmap([
            {"timestamp": "2024-03-01T23:59:59", "type": "image"},
            {"timestamp": "2024-02-28T00:00:00", "type": "text"},
            {"timestamp": "2024-02-28T21:00:00", "type": "voice"},
            {"timestamp": "2024-02-27T23:59:59"},
            {"timestamp": "2024-03-02T00:00:00"},
        ], {"date_from": "2024-02-28", "date_to": "2024-03-01"})
        self.assertEqual(result["days"], [
            {"date": "2024-02-28", "count": 2},
            {"date": "2024-02-29", "count": 0},
            {"date": "2024-03-01", "count": 1},
        ])
        self.assertEqual(result["summary"], {"message_count": 3, "active_days": 2,
            "daily_average": 1.0, "peak_day": {"date": "2024-02-28", "count": 2}})
        self.assertEqual(result["scope"], {"date_from": "2024-02-28", "date_to": "2024-03-01", "days": 3})
        self.assertEqual(len(result["weekday_hour"]), 7)
        self.assertTrue(all(len(row) == 24 for row in result["weekday_hour"]))
        self.assertEqual(result["weekday_hour"][2][0], 1)
        self.assertEqual(result["weekday_hour"][2][21], 1)
        self.assertEqual(result["weekday_hour"][4][23], 1)
        self.assertEqual(sum(sum(row) for row in result["weekday_hour"]), 3)
        self.assertEqual(result["available_range"], {"date_from": "2024-02-27", "date_to": "2024-03-02"})

    def test_exclusions_are_whole_archive_and_mutually_exclusive(self):
        invalid = [None, True, "garbage", "2024-02-30", float("nan"), float("inf"),
                   "1999-12-31T12:00:00", "2101-01-01T12:00:00", 10**30]
        messages = [{"timestamp": value, "type": "system"} for value in invalid]
        messages += [None, [], {"timestamp": "2024-03-10T12:00:01", "type": "system"},
                     {"timestamp": "2024-03-11T12:00:00"},
                     {"timestamp": "2024-01-01T12:00:00", "type": "system"},
                     {"timestamp": "2024-03-10T12:00:00", "type": "text"}]
        result = self.heatmap(messages, {"date_from": "2024-03-10", "date_to": "2024-03-10"})
        self.assertEqual(result["excluded"], {"invalid_timestamp": 11, "future_timestamp": 2, "system_messages": 1})
        self.assertEqual(result["summary"]["message_count"], 1)
        self.assertEqual(result["available_range"], {"date_from": "2024-03-10", "date_to": "2024-03-10"})

    def test_all_types_duplicates_and_unknown_senders_count_without_reading_content(self):
        kinds = ["text", "image", "voice", "video", "emoji", "file", "other", "new-type", None, {}]
        messages = [MetadataOnlyMessage(timestamp="2024-03-04T08:00:00", type=kind,
            sender="SYNTHETIC_ID", content="SYNTHETIC_BODY", transcript="SYNTHETIC_TRANSCRIPT",
            path="SYNTHETIC_MEDIA_PATH") for kind in kinds]
        messages.append(messages[0])
        result = self.heatmap(messages)
        self.assertEqual(result["summary"]["message_count"], 11)
        self.assertEqual(result["weekday_hour"][0][8], 11)
        self.assertEqual(set(result), {"version", "as_of", "timezone", "available_range", "scope", "days",
                                      "weekday_hour", "summary", "top_days", "excluded"})
        self.assertNotIn("SYNTHETIC", json.dumps(result))
        self.assertEqual(result["timezone"], "server_local")
        self.assertEqual(result["version"], 1)
        self.assertEqual(datetime.fromisoformat(result["as_of"]).timestamp(), self.now.timestamp())

    def test_seconds_milliseconds_and_offset_iso_use_host_local_clock(self):
        local = datetime(2024, 3, 4, 0, 30)
        stamp = local.timestamp()
        offset_iso = datetime.fromtimestamp(stamp, timezone(timedelta(hours=-7))).isoformat()
        result = self.heatmap([{"timestamp": value} for value in (stamp, stamp * 1000, str(stamp), offset_iso)])
        self.assertEqual(result["days"][-1], {"date": "2024-03-04", "count": 4})
        self.assertEqual(result["weekday_hour"][0][0], 4)
        self.assertEqual(result["summary"]["message_count"], 4)

    def test_default_is_ninety_days_ending_at_latest_eligible_archive_day(self):
        result = self.heatmap([
            {"timestamp": "2023-01-01T12:00:00"},
            {"timestamp": "2024-02-29T12:00:00"},
            {"timestamp": "2024-03-01T12:00:00", "type": "system"},
            {"timestamp": "2025-01-01T12:00:00"},
        ])
        self.assertEqual(result["scope"], {"date_from": "2023-12-02", "date_to": "2024-02-29", "days": 90})
        self.assertEqual(result["summary"]["message_count"], 1)
        self.assertEqual(result["summary"]["daily_average"], 0.01)

    def test_default_clamps_to_supported_year(self):
        result = self.heatmap([{"timestamp": "2000-01-02T12:00:00"}])
        self.assertEqual(result["scope"], {"date_from": "2000-01-01", "date_to": "2000-01-02", "days": 2})

    def test_empty_malformed_and_all_excluded_archives_have_no_available_range(self):
        for payload in (None, [], {}, {"messages": None}, {"messages": "invalid"},
                        {"messages": [{"timestamp": None}, {"timestamp": "2025-01-01"}]}):
            with self.subTest(payload=payload):
                result = self.build(payload, {}, now=self.now)
                self.assertEqual(result["available_range"], {"date_from": None, "date_to": None})
                self.assertEqual(result["scope"], {"date_from": "2023-12-12", "date_to": "2024-03-10", "days": 90})
                self.assertEqual(result["summary"], {"message_count": 0, "active_days": 0,
                    "daily_average": 0, "peak_day": None})
                self.assertEqual(result["top_days"], [])
                self.assertEqual(len(result["days"]), 90)

    def test_empty_selected_range_still_has_dense_zeroes_and_archive_bounds(self):
        result = self.heatmap([{"timestamp": "2024-01-01"}],
                              {"date_from": "2024-03-01", "date_to": "2024-03-03"})
        self.assertEqual([day["count"] for day in result["days"]], [0, 0, 0])
        self.assertEqual(result["available_range"], {"date_from": "2024-01-01", "date_to": "2024-01-01"})
        self.assertIsNone(result["summary"]["peak_day"])

    def test_top_seven_positive_days_count_order_and_earliest_tie(self):
        messages = [{"timestamp": f"2024-03-{day:02d}T00:00:00"}
                    for day in range(1, 10) for _ in range(2 if day in (1, 3) else 1)]
        result = self.heatmap(messages)
        self.assertEqual(result["top_days"], [
            {"date": "2024-03-01", "count": 2}, {"date": "2024-03-03", "count": 2},
            *[{"date": f"2024-03-{day:02d}", "count": 1} for day in (2, 4, 5, 6, 7)]])
        self.assertEqual(result["summary"]["peak_day"], result["top_days"][0])

    def test_scope_rejects_one_sided_malformed_reversed_and_excessive_bounds(self):
        bad = [None, [], {"date_from": "2024-01-01"}, {"date_to": "2024-01-01"},
               {"date_from": "", "date_to": ""},
               {"date_from": "2024-01-01", "date_to": "2024-12-31T00:00:00"},
               {"date_from": "20240101", "date_to": "2024-01-01"},
               {"date_from": "2024-1-01", "date_to": "2024-01-01"},
               {"date_from": "2024-02-30", "date_to": "2024-03-01"},
               {"date_from": "1999-12-31", "date_to": "2000-01-01"},
               {"date_from": "2100-12-31", "date_to": "2101-01-01"},
               {"date_from": "2024-01-02", "date_to": "2024-01-01"},
               {"date_from": "2024-01-01", "date_to": "2025-01-01"},
               {"date_from": None, "date_to": "2024-01-01"},
               {"date_from": True, "date_to": "2024-01-01"},
               {"date_from": "SYNTHETIC_UNTRUSTED", "date_to": "2024-01-01"}]
        for scope in bad:
            with self.subTest(scope=scope), self.assertRaises(ValueError) as raised:
                self.build({}, scope, now=self.now)
            self.assertNotIn("SYNTHETIC", str(raised.exception))

    def test_three_hundred_sixty_six_days_is_allowed_and_bounded(self):
        result = self.heatmap([], {"date_from": "2024-01-01", "date_to": "2024-12-31"})
        self.assertEqual(len(result["days"]), 366)
        self.assertEqual(result["scope"]["days"], 366)

    def test_invalid_clock_fails_even_with_empty_payload(self):
        with self.assertRaisesRegex(ValueError, "当前时间格式无效"):
            self.build({}, {}, now="SYNTHETIC_BAD_CLOCK")

    def test_default_rejects_clock_outside_supported_calendar(self):
        for clock in (datetime(1999, 12, 30), datetime(2101, 1, 1)):
            with self.subTest(clock=clock), self.assertRaisesRegex(ValueError, "当前时间格式无效"):
                self.build({}, {}, now=clock)


if __name__ == "__main__":
    unittest.main()
