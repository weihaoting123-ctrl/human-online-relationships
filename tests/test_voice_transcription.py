import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import archive_wechat_voice as archive
import transcribe_wechat_voice as voice


class VoiceTranscriptionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'scripts/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'scripts/tmp')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.private = self.repo / 'data/private/wechat-voice'
        self.catalog = archive.open_catalog(self.private)
        self.addCleanup(self.catalog.close)
        self.path = self.repo / 'data/contacts/synthetic/messages.json'
        self.path.parent.mkdir(parents=True)
        self.message = {'type': 'voice', 'content': '[语音]', 'timestamp': 1788580000,
                        'sender': 'them', 'server_id': '12345'}
        self.path.write_text(json.dumps({'contact_name': '合成联系人', 'messages': [self.message]}, ensure_ascii=False), 'utf-8')
        raw = b'\x02#!SILK_V3synthetic'
        self.sha = archive.save_asset(self.catalog, self.private, raw)
        self.catalog.execute('INSERT INTO voice_messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                             ('a', 'synthetic', 0, archive.message_fingerprint(self.message), '', '12345',
                              1788580000, self.sha, 'archived', 'media'))
        self.catalog.commit()

    def run_synthetic(self, **kwargs):
        with mock.patch.object(voice, 'decode_audio', return_value=b'\x01\x00' * 1600) as decoder, \
             mock.patch.object(voice, 'recognize', return_value='合成转写：会议排期') as recognizer:
            result = voice.run(self.repo, runtime_check=lambda: {'ready': True},
                               recognizer_factory=lambda: object(), **kwargs)
        return result, decoder, recognizer

    def add_audio(self, count=1):
        for index in range(count):
            archive.save_asset(self.catalog, self.private,
                               b'\x02#!SILK_V3synthetic-extra-' + str(index).encode())
        self.catalog.commit()

    def test_transcribe_attach_and_skip_without_repeating_or_deleting(self):
        before = self.path.read_bytes()
        first, decoder, recognizer = self.run_synthetic()
        self.assertEqual(first['transcribed_audio'], 1)
        self.assertEqual(first['attached_messages'], 1)
        self.assertEqual(first['playable_audio'], 1)
        value = json.loads(self.path.read_text('utf-8'))
        self.assertEqual(value['messages'][0]['transcript'], '合成转写：会议排期')
        self.assertEqual(value['messages'][0]['content'], '[语音]')
        histories = list((self.path.parent / '.history').glob('messages-*.json'))
        self.assertEqual(histories[0].read_bytes(), before)
        updated = self.path.read_bytes()
        second, decoder, recognizer = self.run_synthetic()
        self.assertEqual(second['skipped_audio'], 1)
        self.assertEqual(second['processed_audio'], 0)
        decoder.assert_not_called()
        recognizer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), updated)
        self.assertEqual(len(list(self.private.glob('blobs/*/*'))), 1)
        public = (self.repo / 'data/voice-transcribe-status.json').read_text('utf-8')
        self.assertNotIn('合成转写', public)
        self.assertNotIn('合成联系人', public)

    def test_changed_message_not_wrongly_enriched(self):
        changed = dict(self.message, content='different record', timestamp=1788580050)
        self.path.write_text(json.dumps({'messages': [changed]}), 'utf-8')
        result, _, _ = self.run_synthetic()
        self.assertEqual(result['attached_messages'], 0)
        self.assertNotIn('transcript', json.loads(self.path.read_text())['messages'][0])

    def test_existing_transcript_preserved_and_playback_repaired_without_retranscribing(self):
        message = dict(self.message, transcript='人工核对的文字')
        self.path.write_text(json.dumps({'messages': [message]}), 'utf-8')
        first, _, _ = self.run_synthetic()
        self.assertEqual(first['attached_messages'], 0)
        wav = self.repo / 'data/exports/wechat-voice/audio' / (self.sha + '.wav')
        wav.unlink()
        second, decoder, recognizer = self.run_synthetic()
        decoder.assert_called_once()
        recognizer.assert_not_called()
        self.assertTrue(voice.wav_ready(wav))
        self.assertEqual(json.loads(self.path.read_text())['messages'][0]['transcript'], '人工核对的文字')

    def test_model_initialization_failure_does_not_poison_audio_cache(self):
        with self.assertRaises(RuntimeError):
            voice.run(self.repo, runtime_check=lambda: {'ready': True},
                      recognizer_factory=mock.Mock(side_effect=RuntimeError('model unavailable')))
        db = voice.open_results(self.private)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0], 0)
        self.assertEqual(json.loads((self.repo / 'data/voice-transcribe-status.json').read_text())['state'], 'error')

    def test_failed_audio_not_retried_without_explicit_flag(self):
        with mock.patch.object(voice, 'decode_audio', side_effect=ValueError('audio_format_unsupported')):
            first = voice.run(self.repo, runtime_check=lambda: {'ready': True}, recognizer_factory=lambda: object())
        self.assertEqual(first['failed_audio'], 1)
        self.assertEqual(first['state'], 'partial')
        second, decoder, recognizer = self.run_synthetic()
        decoder.assert_not_called()
        self.assertEqual(second['failed_audio'], 1)
        third, _, recognizer = self.run_synthetic(retry_failed=True)
        self.assertEqual(third['failed_audio'], 0)
        self.assertEqual(third['transcribed_audio'], 1)

    def test_gallery_untrusted_text_cannot_escape_json(self):
        with mock.patch.object(voice, 'decode_audio', return_value=b'\x01\x00' * 1600), \
             mock.patch.object(voice, 'recognize', return_value='</script><img src=x onerror=alert(1)>'):
            voice.run(self.repo, runtime_check=lambda: {'ready': True}, recognizer_factory=lambda: object())
        gallery = (self.repo / 'data/exports/wechat-voice/index.html').read_text('utf-8')
        self.assertNotIn('<img', gallery)
        self.assertIn('\\u003c/script', gallery)

    def test_decode_only_then_transcribe_reuses_playable_audio(self):
        first, decoder, recognizer = self.run_synthetic(decode_only=True)
        recognizer.assert_not_called()
        self.assertEqual(first['pending_audio'], 1)
        self.assertEqual(first['playable_audio'], 1)
        second, decoder, recognizer = self.run_synthetic()
        decoder.assert_not_called()
        recognizer.assert_called_once()
        self.assertEqual(second['transcribed_audio'], 1)

    def test_modified_playback_is_rebuilt_from_original_before_recognition(self):
        self.run_synthetic(decode_only=True)
        wav = self.repo / 'data/exports/wechat-voice/audio' / (self.sha + '.wav')
        voice.save_wav(wav, b'\x02\x00' * 1600)
        result, decoder, recognizer = self.run_synthetic()
        decoder.assert_called_once()
        self.assertEqual(result['transcribed_audio'], 1)

    def test_pending_recognition_failure_becomes_visible_and_not_automatically_retried(self):
        self.run_synthetic(decode_only=True)
        with mock.patch.object(voice, 'recognize', side_effect=RuntimeError('synthetic failure')):
            result = voice.run(self.repo, runtime_check=lambda: {'ready': True}, recognizer_factory=lambda: object())
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['pending_audio'], 0)
        self.assertEqual(result['failed_audio'], 1)
        later, decoder, recognizer = self.run_synthetic()
        recognizer.assert_not_called()

    def test_two_workers_recognize_concurrently_with_independent_models(self):
        self.add_audio()
        barrier = threading.Barrier(2, timeout=5)
        observed = []
        models = []
        main_thread = threading.get_ident()

        def factory():
            self.assertEqual(threading.get_ident(), main_thread)
            model = object()
            models.append(model)
            return model

        def recognition(model, pcm):
            observed.append((id(model), threading.get_ident()))
            barrier.wait()
            return 'synthetic parallel transcript'

        def decode(source, kind):
            self.assertEqual(threading.get_ident(), main_thread)
            return b'\x01\x00' * 1600

        with mock.patch.object(voice, 'decode_audio', side_effect=decode), \
             mock.patch.object(voice, 'recognize', side_effect=recognition):
            result = voice.run(self.repo, workers=2, runtime_check=lambda: {'ready': True},
                               recognizer_factory=factory)
        self.assertEqual(result['transcribed_audio'], 2)
        self.assertEqual(result['processed_audio'], 2)
        self.assertEqual(len(models), 2)
        self.assertEqual(len({model for model, thread in observed}), 2)
        self.assertEqual(len({thread for model, thread in observed}), 2)
        self.assertNotIn(main_thread, {thread for model, thread in observed})

    def test_worker_failure_does_not_lose_other_result_or_retry_failed_cache(self):
        self.add_audio()
        barrier = threading.Barrier(2, timeout=5)
        models = []

        def factory():
            model = len(models)
            models.append(model)
            return model

        def recognition(model, pcm):
            barrier.wait()
            if model == 0:
                raise RuntimeError('synthetic worker failure')
            return 'synthetic successful transcript'

        with mock.patch.object(voice, 'decode_audio', return_value=b'\x01\x00' * 1600), \
             mock.patch.object(voice, 'recognize', side_effect=recognition):
            result = voice.run(self.repo, workers=2, runtime_check=lambda: {'ready': True},
                               recognizer_factory=factory)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['failed_audio'], 1)
        self.assertEqual(result['transcribed_audio'], 1)
        self.assertEqual(result['processed_audio'], 2)
        again, decoder, recognizer = self.run_synthetic(workers=2)
        decoder.assert_not_called()
        recognizer.assert_not_called()
        self.assertEqual(again['skipped_audio'], 2)

    def test_worker_limit_counts_inflight_and_resumes_without_repeating(self):
        self.add_audio(count=3)
        first, decoder, recognizer = self.run_synthetic(workers=2, limit=1)
        self.assertEqual(first['processed_audio'], 1)
        self.assertEqual(first['transcribed_audio'], 1)
        self.assertEqual(first['pending_audio'], 3)
        recognizer.assert_called_once()
        second, decoder, recognizer = self.run_synthetic(workers=2, limit=2)
        self.assertEqual(second['processed_audio'], 2)
        self.assertEqual(second['transcribed_audio'], 3)
        self.assertEqual(second['pending_audio'], 1)
        self.assertEqual(recognizer.call_count, 2)
        third, _, recognizer = self.run_synthetic(workers=2)
        self.assertEqual(third['transcribed_audio'], 4)
        self.assertEqual(third['processed_audio'], 1)
        recognizer.assert_called_once()

    def test_worker_deadline_drains_batch_without_submitting_more(self):
        self.add_audio(count=3)
        clock = [0.0]
        barrier = threading.Barrier(2, timeout=5)
        def recognition(model, pcm):
            barrier.wait()
            clock[0] = 10.0
            return 'synthetic transcript'
        with mock.patch.object(voice.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(voice, 'decode_audio', return_value=b'\x01\x00' * 1600), \
             mock.patch.object(voice, 'recognize', side_effect=recognition) as recognizer:
            result = voice.run(self.repo, workers=2, max_seconds=1,
                               runtime_check=lambda: {'ready': True}, recognizer_factory=lambda: object())
        self.assertEqual(result['processed_audio'], 2)
        self.assertEqual(result['transcribed_audio'], 2)
        self.assertEqual(result['pending_audio'], 2)
        self.assertEqual(recognizer.call_count, 2)

    def test_parallel_model_initialization_failure_does_not_poison_any_asset(self):
        self.add_audio()
        factory = mock.Mock(side_effect=[object(), RuntimeError('second instance failed')])
        with self.assertRaises(RuntimeError):
            voice.run(self.repo, workers=2, runtime_check=lambda: {'ready': True}, recognizer_factory=factory)
        db = voice.open_results(self.private)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0], 0)

    def test_parallel_default_factory_uses_two_threads_per_model(self):
        self.add_audio()
        with mock.patch.object(voice, 'decode_audio', return_value=b'\x01\x00' * 1600), \
             mock.patch.object(voice, 'recognize', return_value='synthetic transcript'), \
             mock.patch.object(voice, 'make_recognizer', side_effect=lambda **kwargs: object()) as factory:
            voice.run(self.repo, workers=2, runtime_check=lambda: {'ready': True})
        self.assertEqual(factory.call_args_list, [mock.call(num_threads=2), mock.call(num_threads=2)])

    def test_invalid_worker_count_rejected_before_processing(self):
        with self.assertRaisesRegex(ValueError, 'voice_worker_count_invalid'):
            voice.run(self.repo, workers=3)


if __name__ == '__main__':
    unittest.main()
