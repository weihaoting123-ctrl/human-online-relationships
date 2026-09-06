"""Synthetic voice UI/API boundaries; no real messages, keys or model traffic."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from dashboard import analysis as ai
from dashboard import app


class VoiceAnalysisTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "scripts" / "tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="voice-api-test-", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = {"bundle_id": "synthetic", "date_from": "2026-08-01", "date_to": "2026-08-31",
                        "focus": "communication", "max_messages": 100}
        stamp = datetime(2026, 8, 10, 12).timestamp()
        self.payload = {"contact_display": "合成人名", "messages": [
            {"type": "text", "timestamp": stamp, "sender": "me", "content": "合成文字"},
            {"type": "voice", "timestamp": stamp + 1, "sender": "them", "content": "PRIVATE-AUDIO-METADATA",
             "transcript": "合成人名 13812345678 合成转写", "transcript_source": "PRIVATE-MODEL-PATH",
             "audio_path": "PRIVATE-AUDIO-PATH"},
            {"type": "voice", "timestamp": stamp + 2, "voice_transcript": "合成兼容转写"},
            {"type": "voice", "timestamp": stamp + 3, "content": "PRIVATE-VOICE-XML"},
            {"type": "image", "timestamp": stamp + 4, "transcript": "PRIVATE-IMAGE-TEXT"},
            {"type": "voice", "timestamp": datetime(2026, 9, 1).timestamp(), "transcript": "PRIVATE-OUTSIDE"},
        ]}

    def test_voice_is_excluded_by_default_and_switch_is_literal_boolean(self):
        scope, limit = ai._scope(self.request)
        self.assertIs(scope["include_voice_transcripts"], False)
        sample, counts = ai._sample(self.payload, scope, limit)
        self.assertEqual(len(sample), 1)
        self.assertEqual(counts["stats"]["voice_messages"], 3)
        self.assertEqual(counts["stats"]["transcribed_voice_messages"], 2)
        self.assertEqual(counts["stats"]["sampled_voice_messages"], 0)
        self.assertNotIn("转写", json.dumps(sample, ensure_ascii=False))
        for value in ("true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ai.AnalysisError):
                ai._scope({**self.request, "include_voice_transcripts": value})

    def test_opt_in_uses_transcripts_only_and_keeps_scope_redaction_and_source(self):
        scope, limit = ai._scope({**self.request, "include_voice_transcripts": True})
        sample, counts = ai._sample(self.payload, scope, limit)
        self.assertEqual(len(sample), 3)
        self.assertEqual(counts["stats"]["sampled_voice_messages"], 2)
        self.assertEqual(counts["stats"]["excluded_nontext"], 2)
        self.assertEqual([item["source"] for item in sample], ["text", "voice_transcript", "voice_transcript"])
        serialized = json.dumps(sample, ensure_ascii=False)
        self.assertIn("合成转写", serialized)
        for excluded in ("合成人名", "13812345678", "PRIVATE"):
            self.assertNotIn(excluded, serialized)

    def test_transcripts_share_text_sampling_caps(self):
        stamp = datetime(2026, 8, 10, 12).timestamp()
        payload = {"messages": [{"type": "voice", "timestamp": stamp + index,
                                 "transcript": "合成内容" * 1000} for index in range(1000)]}
        scope, limit = ai._scope({**self.request, "include_voice_transcripts": True, "max_messages": 600})
        sample, counts = ai._sample(payload, scope, limit)
        self.assertEqual(len(sample), 600)
        self.assertEqual(counts["stats"]["sampled_voice_messages"], 600)
        self.assertLessEqual(sum(len(item["text"]) for item in sample), 60_000)
        self.assertEqual(counts["sample_chars"], 60_000)

    def test_local_preview_only_exposes_counts_and_binds_voice_selection(self):
        contacts = self.root / "contacts"
        bundle = contacts / "synthetic"
        bundle.mkdir(parents=True)
        (bundle / "messages.json").write_text(json.dumps(self.payload), encoding="utf-8")
        with mock.patch.object(ai, "_dpapi", side_effect=lambda value, **kwargs: value), \
                mock.patch.object(urllib.request, "build_opener") as network:
            result = ai.preview(self.root, contacts, {**self.request, "include_voice_transcripts": True})
        network.assert_not_called()
        self.assertIs(result["scope"]["include_voice_transcripts"], True)
        self.assertEqual(result["stats"]["sampled_voice_messages"], 2)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("合成转写", json.dumps(result, ensure_ascii=False))

    def test_voice_status_strips_private_metadata_and_invalid_values(self):
        (self.root / "voice-archive-status.json").write_text(json.dumps({
            "state": "partial", "total_voice_messages": 8, "archived_voice_messages": 6,
            "missing_voice_messages": 2, "unique_audio_files": 5, "audio_path": "PRIVATE",
            "archived_bytes": "PRIVATE", "processed_databases": True, "report_rebuilt": True,
        }), encoding="utf-8")
        (self.root / "voice-transcribe-status.json").write_text(json.dumps({
            "state": "needs_runtime", "code": "local_voice_runtime_missing", "transcribed_audio": 3,
            "pending_audio": 2, "failed_audio": -1, "transcript": "PRIVATE", "model_path": "PRIVATE",
        }), encoding="utf-8")
        with mock.patch.object(app, "DATA_DIR", self.root):
            status = app.archive_status()
        self.assertEqual(status["voice"]["archived_voice_messages"], 6)
        self.assertEqual(status["voice"]["archived_bytes"], 0)
        self.assertEqual(status["voice"]["processed_databases"], 0)
        self.assertNotIn("report_rebuilt", status["voice"])
        self.assertEqual(status["voice_transcription"]["code"], "local_voice_runtime_missing")
        self.assertEqual(status["voice_transcription"]["failed_audio"], 0)
        self.assertNotIn("PRIVATE", json.dumps(status))
        (self.root / "voice-transcribe-status.json").write_text(json.dumps({
            "state": ["PRIVATE"], "code": "PRIVATE",
        }), encoding="utf-8")
        with mock.patch.object(app, "DATA_DIR", self.root):
            value = app.archive_status()["voice_transcription"]
        self.assertEqual(value["state"], "not_started")
        self.assertIsNone(value["code"])

    def test_derived_audio_requires_session_and_only_exact_export_location(self):
        audio_name = "a" * 64 + ".wav"
        audio = self.root / "exports" / "wechat-voice" / "audio" / audio_name
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"RIFF-synthetic-WAVE-test")
        (audio.parent / "private.wav").write_bytes(b"PRIVATE")
        (audio.parent.parent / "index.html").write_text('<script src="/voice-gallery.js"></script>', encoding="utf-8")
        legacy = self.root / "exports" / "classification"
        legacy.mkdir()
        (legacy / audio_name).write_bytes(b"PRIVATE")
        (legacy / "index.html").write_text("synthetic report", encoding="utf-8")
        with mock.patch.object(app, "DATA_DIR", self.root), mock.patch.object(app, "CONTACTS_DIR", self.root / "contacts"):
            server = app.create_server(port=0, token="synthetic-voice-local-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                path = "/exports/wechat-voice/audio/" + audio_name
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(base + path)
                self.assertEqual(denied.exception.code, 403)
                headers = {"X-Local-Token": "synthetic-voice-local-token"}
                with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers)) as response:
                    self.assertEqual(response.headers["Content-Type"], "audio/wav")
                    self.assertEqual(response.read(), audio.read_bytes())
                for requested, expected in (("bytes=0-3", b"RIFF"), ("bytes=-4", b"test"), ("bytes=20-", b"test")):
                    with urllib.request.urlopen(urllib.request.Request(base + path, headers={**headers, "Range": requested})) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.read(), expected)
                for requested in ("bytes=100-", "bytes=0-3,5-6", "bytes=-0"):
                    with self.assertRaises(urllib.error.HTTPError) as invalid:
                        urllib.request.urlopen(urllib.request.Request(base + path, headers={**headers, "Range": requested}))
                    self.assertEqual(invalid.exception.code, 416)
                for forbidden in ("/exports/wechat-voice/audio/private.wav", "/exports/classification/" + audio_name,
                                  "/exports/wechat-voice/audio/../../private.wav"):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(urllib.request.Request(base + forbidden, headers=headers))
                    self.assertEqual(denied.exception.code, 404)
                for gallery, policy in (("wechat-voice", "script-src 'self'"), ("classification", "script-src 'none'")):
                    with urllib.request.urlopen(urllib.request.Request(base + f"/exports/{gallery}/index.html", headers=headers)) as response:
                        self.assertIn(policy, response.headers["Content-Security-Policy"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
