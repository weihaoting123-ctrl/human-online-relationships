"""Pure synthetic aggregate tests; no files, private data, or cloud traffic."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from dashboard.analysis_metrics import MAX_ACTIVITY_BUCKETS, MESSAGE_TYPES, build_metrics


def message(stamp, sender="me", kind="text", content="合成"):
    if isinstance(stamp, datetime):
        stamp = stamp.timestamp()
    return {"timestamp": stamp, "sender": sender, "type": kind, "content": content}


def metrics(messages, **scope):
    return build_metrics({"messages": messages}, scope)


class AnalysisMetricsTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 10, 12)

    def test_empty_and_malformed_payloads_have_json_safe_zero_schema(self):
        for payload in (None, [], {}, {"messages": None}, {"messages": {}}, {"messages": []}):
            with self.subTest(payload=payload):
                result = build_metrics(payload, {})
                self.assertEqual(result["version"], 1)
                self.assertEqual(result["basis"], "selected_scope_all_messages")
                self.assertEqual(result["totals"], {
                    "messages": 0, "me": 0, "other": 0, "text": 0, "voice": 0,
                    "transcribed_voice": 0, "characters": 0, "active_days": 0,
                    "sessions": 0, "first_date": None, "last_date": None,
                })
                self.assertEqual(result["activity"], [])
                self.assertEqual(len(result["hours"]), 24)
                self.assertEqual(len(result["weekdays"]), 7)
                self.assertEqual([row["type"] for row in result["types"]], list(MESSAGE_TYPES))
                self.assertEqual(result["response_times"]["me"], {
                    "samples": 0, "median_seconds": None, "p90_seconds": None,
                })
                json.dumps(result, allow_nan=False)

    def test_date_scope_is_inclusive_in_local_timezone_for_seconds_ms_and_iso(self):
        local_day = datetime(2026, 8, 10)
        before = local_day - timedelta(milliseconds=1)
        after = local_day + timedelta(days=1)
        last = after - timedelta(milliseconds=1)
        iso_utc = local_day.astimezone(timezone.utc).isoformat()
        result = metrics([
            message(before), message(local_day), message(local_day.timestamp() * 1000),
            message(iso_utc), message(last), message(after),
        ], date_from="2026-08-10", date_to="2026-08-10")
        self.assertEqual(result["totals"]["messages"], 4)
        self.assertEqual(result["totals"]["first_date"], "2026-08-10")
        self.assertEqual(result["totals"]["last_date"], "2026-08-10")
        self.assertEqual(result["hours"][0]["count"], 3)
        self.assertEqual(result["hours"][23]["count"], 1)
        self.assertEqual(result["weekdays"][0]["count"], 4)

    def test_explicit_offset_is_converted_to_machine_local_calendar(self):
        source = datetime(2026, 8, 10, 23, 59, tzinfo=timezone(timedelta(hours=-11)))
        local = datetime.fromtimestamp(source.timestamp())
        result = metrics([message(source.isoformat())],
                         date_from=local.date().isoformat(), date_to=local.date().isoformat())
        self.assertEqual(result["totals"]["messages"], 1)
        self.assertEqual(result["hours"][local.hour]["count"], 1)
        self.assertEqual(result["weekdays"][local.weekday()]["count"], 1)

    def test_open_bounds_select_only_eligible_records(self):
        rows = [message(self.start - timedelta(days=1)), message(self.start),
                message(self.start + timedelta(days=1))]
        self.assertEqual(metrics(rows, date_from="2026-08-10")["totals"]["messages"], 2)
        self.assertEqual(metrics(rows, date_to="2026-08-10")["totals"]["messages"], 2)
        self.assertEqual(metrics(rows, date_from="2026-08-12")["totals"]["messages"], 0)

    def test_invalid_timestamps_are_excluded_without_overflow_or_unit_guessing(self):
        bad = [None, True, False, [], {}, "bad", "", "NaN", "Infinity", "1" * 1000,
               float("nan"), float("inf"), -float("inf"), 0, -1, 10 ** 10000,
               10 ** 300, self.start.timestamp() * 1_000_000,
               datetime(1999, 12, 31, 12).timestamp(), datetime(2101, 1, 1, 12).timestamp()]
        rows = [None, [], "PRIVATE", {}] + [message(value) for value in bad]
        rows += [message(str(self.start.timestamp())), message(str(self.start.timestamp() * 1000))]
        result = metrics(rows)
        self.assertEqual(result["totals"]["messages"], 2)
        self.assertEqual(result["totals"]["sessions"], 1)
        json.dumps(result, allow_nan=False)

    def test_malformed_scopes_fail_closed_with_fixed_errors(self):
        bad_scopes = [None, [], {"date_from": None}, {"date_to": []},
                      {"date_from": "PRIVATE"}, {"date_to": "2026-8-10"},
                      {"date_from": "2026-02-30"}, {"date_from": "20260810"},
                      {"date_from": "2026-08-11", "date_to": "2026-08-10"}]
        for scope in bad_scopes:
            with self.subTest(scope=scope), self.assertRaises(ValueError) as raised:
                build_metrics({"messages": [message(self.start)]}, scope)
            self.assertNotIn("PRIVATE", str(raised.exception))

    def test_types_senders_and_characters_are_allowlisted(self):
        rows = [message(self.start, kind=kind, content="正文") for kind in MESSAGE_TYPES]
        rows += [message(self.start, sender="PRIVATE-CONTACT", kind="PRIVATE-TYPE"),
                 message(self.start, sender=None, kind={}),
                 {"timestamp": self.start.timestamp(), "content": "默认文字"}]
        rows[1]["transcript"] = "PRIVATE-IMAGE-TRANSCRIPT"
        rows[2]["transcript"] = " 已有转写 "
        rows[2]["voice_transcript"] = "PRIVATE-DUPLICATE-TRANSCRIPT"
        result = metrics(rows)
        self.assertEqual(result["totals"]["messages"], 11)
        self.assertEqual(result["totals"]["me"], 8)
        self.assertEqual(result["totals"]["other"], 3)
        self.assertEqual(result["totals"]["text"], 2)
        self.assertEqual(result["totals"]["voice"], 1)
        self.assertEqual(result["totals"]["transcribed_voice"], 1)
        self.assertEqual(result["totals"]["characters"], len("正文") + len(" 已有转写 ") + len("默认文字"))
        counts = {row["type"]: row["count"] for row in result["types"]}
        self.assertEqual(counts["other"], 3)
        self.assertEqual(sum(counts.values()), 11)
        self.assertEqual(result["activity"], [{"date": "2026-08-10", "count": 11, "me": 8, "other": 3}])

    def test_voice_transcript_fallback_ignores_blank_and_nonstring_values(self):
        rows = [message(self.start, kind="voice") for _ in range(5)]
        rows[0].update(transcript="", voice_transcript="兼容转写")
        rows[1].update(transcript="   ", voice_transcript="第二条")
        rows[2].update(transcript={}, voice_transcript="第三条")
        rows[3].update(transcript=[], voice_transcript={})
        rows[4].update(transcript="\n  ", voice_transcript="")
        result = metrics(rows, include_voice_transcripts=False)
        self.assertEqual(result["totals"]["voice"], 5)
        self.assertEqual(result["totals"]["transcribed_voice"], 3)
        self.assertEqual(result["totals"]["characters"], len("兼容转写第二条第三条"))

    def test_activity_fills_days_without_changing_active_days(self):
        result = metrics([message(self.start), message(self.start + timedelta(days=2), sender="other")])
        self.assertEqual(result["activity_granularity"], "day")
        self.assertEqual(result["totals"]["active_days"], 2)
        self.assertEqual(result["activity"], [
            {"date": "2026-08-10", "count": 1, "me": 1, "other": 0},
            {"date": "2026-08-11", "count": 0, "me": 0, "other": 0},
            {"date": "2026-08-12", "count": 1, "me": 0, "other": 1},
        ])

    def test_daily_boundary_switches_to_months_only_after_366_days(self):
        day_result = metrics([message(self.start), message(self.start + timedelta(days=365))])
        month_result = metrics([message(self.start), message(self.start + timedelta(days=366))])
        self.assertEqual(len(day_result["activity"]), 366)
        self.assertEqual(day_result["activity_granularity"], "day")
        self.assertEqual(month_result["activity_granularity"], "month")
        self.assertEqual(month_result["activity_month_step"], 1)
        self.assertEqual(sum(row["count"] for row in month_result["activity"]), 2)

    def test_extreme_date_span_stays_bounded_and_preserves_every_count(self):
        rows = [message(datetime(year, month, 15, 12), sender="me" if month % 2 else "other")
                for year in range(2000, 2101) for month in range(1, 13)]
        result = metrics(rows, date_from="0001-01-01", date_to="9999-12-31")
        self.assertEqual(result["activity_granularity"], "month")
        self.assertGreater(result["activity_month_step"], 1)
        self.assertLessEqual(len(result["activity"]), MAX_ACTIVITY_BUCKETS)
        self.assertEqual(result["totals"]["messages"], len(rows))
        self.assertEqual(result["totals"]["active_days"], len(rows))
        self.assertEqual(result["totals"]["first_date"], "2000-01-15")
        self.assertEqual(result["totals"]["last_date"], "2100-12-15")
        for key in ("count", "me", "other"):
            total = result["totals"]["messages" if key == "count" else key]
            self.assertEqual(sum(row[key] for row in result["activity"]), total)

    def test_sessions_sort_chronology_and_use_strict_30_minute_gap(self):
        rows = [message(self.start + timedelta(seconds=seconds)) for seconds in (3601, 1800, 0)]
        result = metrics(rows)
        self.assertEqual(result["totals"]["sessions"], 2)
        self.assertEqual(result["response_times"]["me"]["samples"], 0)

    def test_response_intervals_use_last_message_of_same_sender_run(self):
        rows = [message(self.start + timedelta(seconds=seconds), sender=sender)
                for seconds, sender in [(0, "me"), (100, "me"), (130, "other"),
                                        (150, "other"), (160, "me"), (300, "me"),
                                        (400, "other")]]
        result = metrics(list(reversed(rows)))
        self.assertEqual(result["response_times"]["me"], {
            "samples": 1, "median_seconds": 10.0, "p90_seconds": 10.0,
        })
        self.assertEqual(result["response_times"]["other"], {
            "samples": 2, "median_seconds": 65.0, "p90_seconds": 93.0,
        })

    def test_response_excludes_over_24_hours_and_keeps_exact_boundary(self):
        rows = [message(self.start, "me"), message(self.start + timedelta(hours=24), "other"),
                message(self.start + timedelta(hours=48, seconds=1), "me"),
                message(self.start + timedelta(hours=48, seconds=2), "other")]
        result = metrics(rows)
        self.assertEqual(result["totals"]["sessions"], 3)
        self.assertEqual(result["response_times"]["me"]["samples"], 0)
        self.assertEqual(result["response_times"]["other"]["samples"], 2)
        self.assertEqual(result["response_times"]["other"]["median_seconds"], 43200.5)

    def test_same_timestamp_order_is_stable_and_zero_gap_is_valid(self):
        result = metrics([message(self.start, "me"), message(self.start, "other"),
                          message(self.start, "other"), message(self.start, "me")])
        for sender in ("me", "other"):
            self.assertEqual(result["response_times"][sender], {
                "samples": 1, "median_seconds": 0.0, "p90_seconds": 0.0,
            })

    def test_scope_does_not_inherit_outside_reply_or_session_history(self):
        rows = [message(datetime(2026, 8, 9, 23, 59), "me"),
                message(datetime(2026, 8, 10, 0, 0), "other")]
        result = metrics(rows, date_from="2026-08-10", date_to="2026-08-10")
        self.assertEqual(result["totals"]["sessions"], 1)
        self.assertEqual(result["response_times"]["other"]["samples"], 0)

    def test_all_scoped_records_are_counted_independently_of_sample_options(self):
        rows = [message(self.start + timedelta(seconds=index)) for index in range(1001)]
        result = metrics(rows, max_messages=100, sample_messages=10)
        self.assertEqual(result["totals"]["messages"], 1001)
        self.assertEqual(result["totals"]["characters"], 2002)

    def test_no_content_identity_path_or_untrusted_keys_enter_output(self):
        secret = "UNIQUE-PRIVATE-TEXT-AND-IDENTITY"
        row = message(self.start, sender=secret, kind=secret, content=secret)
        row.update(transcript=secret, sender_name=secret, audio_path=secret, **{secret: secret})
        voice = {**row, "type": "voice"}
        text_row = {**row, "type": "text"}
        payload = {"messages": [row, voice, text_row], "contact_display": secret,
                   "my_name": secret, "account": secret, secret: secret}
        result = build_metrics(payload, {"bundle_id": secret, "focus": secret})
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn(secret, serialized)
        self.assertEqual(result["totals"]["characters"], len(secret) * 2)
        self.assertEqual(set(result), {"version", "basis", "totals", "activity",
                                     "activity_granularity", "activity_month_step", "hours",
                                     "weekdays", "types", "response_times", "methodology", "timeline"})
        self.assertNotIn("score", serialized)

    def test_inputs_are_not_mutated(self):
        payload = {"messages": [message(self.start, "other"), message(self.start - timedelta(seconds=1))]}
        scope = {"date_from": "2026-08-01", "date_to": "2026-08-31"}
        original = json.dumps([payload, scope], ensure_ascii=False)
        build_metrics(payload, scope)
        self.assertEqual(json.dumps([payload, scope], ensure_ascii=False), original)


if __name__ == "__main__":
    unittest.main()
