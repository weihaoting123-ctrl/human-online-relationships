"""Incremental, entirely local voice decoding/transcription and message enrichment.

Inputs are archived audio and derived chat bundles, never live WeChat files.
No cloud API or automatic model download is available in this command.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import wave

from archive_wechat_local import archive_lock, atomic_json, digest_file, fingerprint
from archive_wechat_voice import message_fingerprint
from bootstrap_voice_runtime import MODEL, MODEL_SHA, runtime_status
from message_merge import _exclusive_bundle_lock, _preserve_existing_snapshot, _atomic_write, _json_bytes

REPO = Path(__file__).resolve().parents[1]
ENGINE = 'local:sensevoice-int8-2024-07-17'
MAX_AUDIO_BYTES = 64 * 1024**2
MAX_SECONDS = 300
SAMPLE_RATE = 16000
DECODER_VERSION = 'silk-python:0.2.8;pcm16k-s16le;v1'


def safe_path(path, root):
    from dashboard.search import _safe_path
    return _safe_path(path, root)


def open_results(private):
    private.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(private / 'transcripts.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS transcripts(sha TEXT PRIMARY KEY,engine TEXT NOT NULL,
        state TEXT NOT NULL,text TEXT NOT NULL,duration REAL NOT NULL,code TEXT NOT NULL,
        updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS playback(sha TEXT PRIMARY KEY,wav_sha TEXT NOT NULL,
        file_signature TEXT NOT NULL);
    ''')
    return db


class BoundedPCM(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_SECONDS * SAMPLE_RATE * 2:
            raise ValueError('audio_duration_limit')
        return super().write(data)


def decode_audio(source, kind):
    """SILK decoder directly emits 16 kHz s16le; no lossy resampling step."""
    if source.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError('audio_size_limit')
    if kind == 'silk':
        import pysilk
        pcm = BoundedPCM()
        with source.open('rb') as f:
            pysilk.decode(f, pcm, SAMPLE_RATE)
        value = pcm.getvalue()
    elif kind == 'wav':
        with wave.open(str(source), 'rb') as f:
            if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (1, 2, SAMPLE_RATE):
                raise ValueError('wav_format_unsupported')
            if f.getnframes() > MAX_SECONDS * SAMPLE_RATE:
                raise ValueError('audio_duration_limit')
            value = f.readframes(f.getnframes())
    else:
        raise ValueError('audio_format_unsupported')
    if not value or len(value) % 2 or len(value) > MAX_SECONDS * SAMPLE_RATE * 2:
        raise ValueError('audio_pcm_invalid')
    return value


def save_wav(path, pcm):
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm)
    _atomic_write(path, buffer.getvalue())


def wav_ready(path):
    try:
        with wave.open(str(path), 'rb') as f:
            return (f.getnchannels() == 1 and f.getsampwidth() == 2 and f.getframerate() == SAMPLE_RATE
                    and 0 < f.getnframes() <= MAX_SECONDS * SAMPLE_RATE
                    and path.stat().st_size >= f.getnframes() * 2 + 44)
    except (OSError, EOFError, wave.Error):
        return False


def playback_ready(results, sha, path, audio_dir):
    if not safe_path(path, audio_dir) or not wav_ready(path):
        return False
    cached = results.execute('SELECT wav_sha,file_signature FROM playback WHERE sha=?', (sha,)).fetchone()
    if not cached:
        return False
    try:
        signature = json.loads(cached['file_signature'])
    except ValueError:
        return False
    if not isinstance(signature, dict) or signature.get('decoder') != DECODER_VERSION:
        return False
    if signature.get('stat') == list(fingerprint(path)):
        return True
    return digest_file(path) == cached['wav_sha']


def remember_playback(results, sha, path):
    results.execute('INSERT OR REPLACE INTO playback VALUES(?,?,?)',
                    (sha, digest_file(path), json.dumps({'decoder': DECODER_VERSION, 'stat': fingerprint(path)})))
    results.commit()


def verified_playback_pcm(results, sha, path, audio_dir):
    """Hash the exact bytes entering ASR, even if timestamps were restored."""
    if not playback_ready(results, sha, path, audio_dir):
        return None
    cached = results.execute('SELECT wav_sha FROM playback WHERE sha=?', (sha,)).fetchone()
    with path.open('rb') as f:
        content = f.read(MAX_SECONDS * SAMPLE_RATE * 2 + 1025)
    if len(content) > MAX_SECONDS * SAMPLE_RATE * 2 + 1024 or hashlib.sha256(content).hexdigest() != cached['wav_sha']:
        return None
    with wave.open(io.BytesIO(content), 'rb') as f:
        return f.readframes(f.getnframes())


def make_recognizer(num_threads=4):
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL / 'model.int8.onnx'), tokens=str(MODEL / 'tokens.txt'),
        num_threads=num_threads, use_itn=True, debug=False, provider='cpu')


def recognize(recognizer, pcm):
    import numpy as np
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
    # Keep near-silence out of the recognizer instead of inventing a transcript.
    if float(np.max(np.abs(samples))) < 0.001:
        return ''
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    recognizer.decode_stream(stream)
    text = re.sub(r'<\|[^|]*\|>', '', stream.result.text).replace('\x00', '').strip()
    return text[:12000]


def attach_transcripts(repo, catalog, results):
    """Under the existing merge lock, enrich exact message matches and keep history."""
    by_bundle = defaultdict(list)
    for row in catalog.execute("SELECT * FROM voice_messages WHERE state='archived'"):
        result = results.execute("SELECT text FROM transcripts WHERE sha=? AND engine=? AND state='done'",
                                 (row['sha'], ENGINE)).fetchone()
        if result and result['text']:
            by_bundle[row['bundle_id']].append((dict(row), result['text']))
    attached, changed, skipped = 0, 0, 0
    contacts = repo / 'data/contacts'
    for bundle_id, rows in by_bundle.items():
        path = contacts / bundle_id / 'messages.json'
        if not safe_path(path, contacts) or path.stat().st_size > 256 * 1024**2:
            skipped += len(rows)
            continue
        with _exclusive_bundle_lock(path):
            payload = json.loads(path.read_text('utf-8-sig'))
            messages = payload['messages']
            lookup = defaultdict(list)
            for index, message in enumerate(messages):
                if message.get('type') == 'voice':
                    lookup[message_fingerprint(message)].append(index)
            updated = 0
            for row, text in rows:
                index = row['message_index']
                matching = lookup.get(row['message_fingerprint'], [])
                if index not in matching:
                    if len(matching) != 1:
                        skipped += 1
                        continue
                    index = matching[0]
                message = messages[index]
                if message.get('transcript') or message.get('voice_transcript'):
                    continue
                message.update(transcript=text, transcript_source=ENGINE,
                               transcript_status='machine_generated', voice_archive_sha256=row['sha'])
                updated += 1
            if updated:
                _preserve_existing_snapshot(path, path.parent / '.history')
                _atomic_write(path, _json_bytes(payload))
                attached += updated
                changed += 1
    return attached, changed, skipped


def counts(catalog, results, audio_dir):
    assets = catalog.execute('SELECT sha FROM assets').fetchall()
    done, empty, failed, playable = 0, 0, 0, 0
    for row in assets:
        cached = results.execute('SELECT state,engine FROM transcripts WHERE sha=?', (row['sha'],)).fetchone()
        if cached and cached['engine'] == ENGINE:
            done += cached['state'] == 'done'
            empty += cached['state'] == 'empty'
            failed += cached['state'] == 'error'
        playable += (audio_dir / (row['sha'] + '.wav')).is_file()
    return {'total_audio': len(assets), 'transcribed_audio': done,
            'empty_audio': empty, 'failed_audio': failed,
            'pending_audio': len(assets) - done - empty - failed, 'playable_audio': playable}


def run(repo=REPO, limit=0, max_seconds=900, retry_failed=False, recognizer_factory=None,
        runtime_check=runtime_status, decode_only=False, workers=1):
    repo = Path(repo)
    if type(workers) is not int or workers not in (1, 2):
        raise ValueError('voice_worker_count_invalid')
    if limit < 0 or max_seconds < 0:
        raise ValueError('voice_processing_limit_invalid')
    if repo.drive.upper() == 'C:':
        raise ValueError('non_c_project_required')
    private = repo / 'data/private/wechat-voice'
    status_path = repo / 'data/voice-transcribe-status.json'
    audio_dir = repo / 'data/exports/wechat-voice/audio'
    audio_dir.mkdir(parents=True, exist_ok=True)
    status = {'state': 'running', 'code': '', 'processed_audio': 0, 'skipped_audio': 0,
              'attached_messages': 0, 'changed_bundles': 0, 'attachment_conflicts': 0,
              'started_at': datetime.now(timezone.utc).isoformat(), 'engine': ENGINE,
              'workers': 1 if decode_only else workers}
    if not (private / 'catalog.sqlite3').is_file():
        status.update(state='needs_archive', code='voice_archive_missing')
        atomic_json(status_path, status)
        return status
    with archive_lock(private / 'transcription'):
        catalog = sqlite3.connect((private / 'catalog.sqlite3').resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        catalog.row_factory = sqlite3.Row
        results = open_results(private)
        pool = None
        try:
            status.update(counts(catalog, results, audio_dir))
            atomic_json(status_path, status)
            # Always finish attaching durable prior results after interruption,
            # even if the model/runtime later becomes unavailable.
            a, b, c = attach_transcripts(repo, catalog, results)
            status.update(attached_messages=a, changed_bundles=b, attachment_conflicts=c)
            runtime = runtime_check()
            ready = runtime.get('packages_ready', runtime['ready']) if decode_only else runtime['ready']
            recognizers, started = [], time.monotonic()
            pending = {}

            def initialize_recognizers():
                nonlocal pool
                if recognizers:
                    return
                # Initialize every independent instance before submitting any
                # work: a model/DLL failure must not poison individual assets.
                factory = recognizer_factory or (lambda: make_recognizer(num_threads=2 if workers == 2 else 4))
                instances = [factory() for _ in range(workers)]
                recognizers.extend(instances)
                if workers == 2:
                    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='local-voice-asr')

            def save_result(sha, text, duration):
                results.execute('INSERT OR REPLACE INTO transcripts VALUES(?,?,?,?,?,?,?)',
                                (sha, ENGINE, 'done' if text else 'empty', text, duration, '',
                                 datetime.now(timezone.utc).isoformat()))
                results.commit()

            def save_error(sha, exc):
                code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z_]{1,50}', str(exc)) else 'local_audio_processing_failed'
                results.execute('INSERT OR REPLACE INTO transcripts VALUES(?,?,?,?,?,?,?)',
                                (sha, ENGINE, 'error', '', 0, code, datetime.now(timezone.utc).isoformat()))
                results.commit()

            def processed():
                status['processed_audio'] += 1
                if status['processed_audio'] % 25 == 0:
                    status.update(counts(catalog, results, audio_dir))
                    status['elapsed_seconds'] = round(time.monotonic() - started, 1)
                    atomic_json(status_path, status)
                if status['processed_audio'] % 250 == 0:
                    a, b, c = attach_transcripts(repo, catalog, results)
                    status['attached_messages'] += a
                    status['changed_bundles'] += b
                    status['attachment_conflicts'] += c
                    from voice_gallery import write_gallery
                    write_gallery(repo, catalog, results, status)
                    atomic_json(status_path, status)

            def drain_batch():
                # Workers never receive a DB connection, path, message or key.
                # At most two validated PCM buffers are in flight; each model
                # is used once per batch and batches never overlap.
                for future in as_completed(tuple(pending)):
                    sha, duration = pending[future]
                    try:
                        text = future.result()
                    except Exception as exc:
                        save_error(sha, exc)
                    else:
                        save_result(sha, text, duration)
                    processed()
                pending.clear()

            if not decode_only and ready and (status['pending_audio'] or (retry_failed and status['failed_audio'])):
                # A DLL/model initialization failure must fail this run, not
                # incorrectly mark every intact recording as unreadable.
                initialize_recognizers()
            assets = catalog.execute('SELECT * FROM assets ORDER BY sha').fetchall()
            for item in assets:
                sha = item['sha']
                if not re.fullmatch('[a-f0-9]{64}', sha):
                    continue
                cached = results.execute('SELECT * FROM transcripts WHERE sha=?', (sha,)).fetchone()
                same = cached and cached['engine'] == ENGINE
                wav = audio_dir / (sha + '.wav')
                if decode_only and playback_ready(results, sha, wav, audio_dir):
                    status['skipped_audio'] += 1
                    continue
                if same and (cached['state'] in {'done', 'empty'} or (cached['state'] == 'error' and not retry_failed)):
                    if cached['state'] == 'error' or playback_ready(results, sha, wav, audio_dir):
                        status['skipped_audio'] += 1
                        continue
                if (limit and status['processed_audio'] + len(pending) >= limit) or (max_seconds and time.monotonic() - started >= max_seconds):
                    break
                if not ready:
                    status.update(state='needs_runtime', code='local_voice_runtime_missing')
                    break
                source = private / item['relative_path']
                submitted = False
                try:
                    if not safe_path(source, private) or digest_file(source) != sha:
                        raise ValueError('audio_archive_invalid')
                    pcm = verified_playback_pcm(results, sha, wav, audio_dir)
                    if pcm is None:
                        pcm = decode_audio(source, item['format'])
                        save_wav(wav, pcm)
                        remember_playback(results, sha, wav)
                    if decode_only:
                        if not same or cached['state'] not in {'done', 'empty'}:
                            results.execute('INSERT OR REPLACE INTO transcripts VALUES(?,?,?,?,?,?,?)',
                                            (sha, ENGINE, 'pending', '', len(pcm) / SAMPLE_RATE / 2, '',
                                             datetime.now(timezone.utc).isoformat()))
                            results.commit()
                    elif not same or cached['state'] not in {'done', 'empty'}:
                        initialize_recognizers()
                        duration = len(pcm) / SAMPLE_RATE / 2
                        if pool is not None:
                            future = pool.submit(recognize, recognizers[len(pending)], pcm)
                            pending[future] = (sha, duration)
                            submitted = True
                        else:
                            save_result(sha, recognize(recognizers[0], pcm), duration)
                except Exception as exc:
                    # A missing playback derivative must not destroy a prior transcript.
                    if not same or cached['state'] not in {'done', 'empty'}:
                        save_error(sha, exc)
                if submitted:
                    if len(pending) == workers:
                        drain_batch()
                else:
                    processed()
            # A limit or deadline stops new submissions, not durable handling
            # of the bounded batch already running.
            drain_batch()
            a, b, c = attach_transcripts(repo, catalog, results)
            status['attached_messages'] += a
            status['changed_bundles'] += b
            status['attachment_conflicts'] += c
            status.update(counts(catalog, results, audio_dir))
            if status['state'] == 'running':
                status['state'] = 'partial' if status['failed_audio'] else ('pending' if status['pending_audio'] else 'complete')
            status['elapsed_seconds'] = round(time.monotonic() - started, 1)
            status['completed_at'] = datetime.now(timezone.utc).isoformat()
            from voice_gallery import write_gallery
            write_gallery(repo, catalog, results, status)
            atomic_json(status_path, status)
            return status
        except Exception:
            status.update(state='error', code='local_voice_transcription_failed')
            atomic_json(status_path, status)
            raise
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            results.close()
            catalog.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--max-seconds', type=int, default=900)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--decode-only', action='store_true')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2,
                        help='Independent local ASR models (default: 2); decoding and writes stay serial.')
    args = parser.parse_args()
    if args.limit < 0 or args.max_seconds < 0:
        return 2
    try:
        # Native audio libraries must never put an utterance or file path on stdout.
        from sync_all_wechat import _silence_native_output
        with _silence_native_output():
            status = run(limit=args.limit, max_seconds=args.max_seconds, retry_failed=args.retry_failed,
                         decode_only=args.decode_only, workers=args.workers)
        print(json.dumps(status, ensure_ascii=False))
        return 0 if status['state'] in {'complete', 'pending'} else 1
    except Exception:
        print(json.dumps({'state': 'error', 'code': 'local_voice_transcription_failed'}))
        return 1


if __name__ == '__main__':
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
