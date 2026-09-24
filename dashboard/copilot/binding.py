"""Volatile, metadata-only suggestions; an OCR title never verifies identity.

No archive body, account identifier, filesystem, network or persistent title state
belongs here. The caller supplies a complete already-indexed metadata snapshot.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import unicodedata

from dashboard import analysis as ai


BINDING_TTL = 10
SESSION_TTL = 1800
MAX_SESSIONS = 64
MAX_TITLE = 200
WECHAT_SOURCES = frozenset({'wechat', 'weflow', 'weflow-cli', 'weflow-cli-nt-readonly',
                            'wechat-local-readonly', 'ciphertalk'})
STALE_BINDING = '当前会话候选已变化、过期或不可用，请重新识别、预览并核对'


def _text(value, maximum):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(unicodedata.category(char).startswith('C') for char in value))


def _title(value):
    return _text(value, MAX_TITLE) and not any(marker in value for marker in ('…', '⋯', '...'))


def _result(state, reason, **fields):
    return {'state': state, 'reason': reason, 'account_verified': False, **fields}


def resolve_title(title, conversations):
    """Exact original-name candidate lookup, including unavailable collisions.

    The index is not a complete current WeChat address book: a unique match is
    still unverified. Management aliases, ranking and filename parsing are never
    identity evidence. No normalization or decoration stripping is performed.
    """
    if not _title(title):
        return _result('unavailable', '会话标题不完整或无效，请手动选择归档')
    matches = [item for item in conversations if item['source'].get('contact') == title]
    if len(matches) > 1:
        return _result('ambiguous', '存在同名归档，无法确定当前会话，请手动选择')
    if not matches:
        return _result('unavailable', '没有可用的同名归档，请手动选择或刷新归档列表')
    item = matches[0]
    if (item.get('hidden_at') or item.get('source_missing')
            or item['source'].get('source') not in WECHAT_SOURCES):
        return _result('unavailable', '没有可用的同名微信归档，请手动选择')
    return _result('suggested', '仅名称精确匹配，请核对当前微信账号和联系人',
                   bundle_id=item['bundle_id'])


def _fingerprint(conversations):
    # User-field version does not change when source_json is refreshed. Hash the
    # full source projection and availability across ALL rows, including hidden
    # and missing ones. Alias/note/tag values are not identity evidence.
    rows = sorted(({'bundle_id': item['bundle_id'], 'source': item['source'],
                    'source_missing': bool(item.get('source_missing')),
                    'hidden_at': item.get('hidden_at')} for item in conversations),
                  key=lambda item: item['bundle_id'])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


class BindingRegistry:
    """One current native session per workspace, bounded and in-process only.

    The service shares its preview/send-claim RLock with this registry. A stable
    heartbeat advances observation_seq and expiry, but leaves its token and
    generation unchanged. Changes and expiration cannot revive an old token.
    """
    def __init__(self, lock=None, *, clock=None, max_sessions=MAX_SESSIONS):
        self.lock = lock if lock is not None else threading.RLock()
        self.clock = clock if clock is not None else time.monotonic
        self.max_sessions = max_sessions
        self.sessions = {}

    def _expire(self, now):
        for workspace, session in list(self.sessions.items()):
            if session['expires'] <= now:
                del self.sessions[workspace]
            elif session['binding'] and session['binding']['expires'] <= now:
                self._revoke(session)

    @staticmethod
    def _revoke(session):
        session['binding'] = None
        session['generation'] += 1

    def start(self, workspace):
        with self.lock:
            now = self.clock()
            self._expire(now)
            self.sessions.pop(workspace, None)
            if len(self.sessions) >= self.max_sessions:
                oldest = min(self.sessions, key=lambda key: self.sessions[key]['expires'])
                del self.sessions[oldest]
            identity = secrets.token_hex(24)
            self.sessions[workspace] = {'session_id': identity, 'seq': 0,
                                        'generation': 0, 'binding': None,
                                        'expires': now + SESSION_TTL}
            return {'status': 'ok', 'session_id': identity}

    def observe(self, workspace, request, metadata_reader):
        with self.lock:
            now = self.clock()
            self._expire(now)
            session = self.sessions.get(workspace)
            if (not isinstance(request, dict) or not session
                    or request.get('session_id') != session['session_id']):
                return _result('unavailable', '识别会话已失效，请重新开启自动识别', observation_seq=0)
            seq = request.get('seq')
            if type(seq) is not int or not 1 <= seq <= 2**53 - 1:
                self._revoke(session)
                return _result('unavailable', '识别观察值无效，请重新识别', observation_seq=session['seq'])
            if seq <= session['seq']:
                return _result('unavailable', '识别观察值已过期，请等待最新识别', observation_seq=session['seq'])
            session['seq'] = seq
            session['expires'] = now + SESSION_TTL
            if (set(request) != {'session_id', 'seq', 'target', 'state', 'title', 'source'}
                    or request.get('source') != 'local_ocr'
                    or request.get('state') not in ('observed', 'unavailable')):
                self._revoke(session)
                return _result('unavailable', '识别观察值无效，请重新识别', observation_seq=seq)
            if request['state'] == 'unavailable':
                self._revoke(session)
                return _result('unavailable', '当前会话暂不可识别，请等待或手动选择', observation_seq=seq)
            if not _text(request['target'], 128) or not _title(request['title']):
                self._revoke(session)
                return _result('unavailable', '会话标题或窗口无效，请手动选择归档', observation_seq=seq)
            try:
                conversations = metadata_reader()
                result = resolve_title(request['title'], conversations)
                fingerprint = _fingerprint(conversations)
            except Exception:
                self._revoke(session)
                return _result('unavailable', '归档索引暂不可用，请稍后重新识别', observation_seq=seq)
            if self.clock() >= now + BINDING_TTL:
                self._revoke(session)
                return _result('unavailable', '识别观察值已过期，请等待最新识别', observation_seq=seq)
            if result['state'] != 'suggested':
                self._revoke(session)
                return {**result, 'observation_seq': seq}
            stable = (request['target'], request['title'], result['bundle_id'], fingerprint)
            current = session['binding']
            if not current or current['stable'] != stable:
                self._revoke(session)
                current = {'token': secrets.token_hex(24), 'stable': stable,
                           'bundle_id': result['bundle_id'], 'fingerprint': fingerprint}
                session['binding'] = current
            current['expires'] = now + BINDING_TTL
            return {**result, 'binding_token': current['token'], 'observation_seq': seq}

    def validate(self, workspace, token, bundle_id, metadata_reader, *, expected=None):
        """Validate at preview and at the atomic send claim; never refresh TTL."""
        with self.lock:
            self._expire(self.clock())
            session = self.sessions.get(workspace)
            current = session['binding'] if session else None
            if (not isinstance(token, str) or not ai.ID_RE.fullmatch(token) or not current
                    or not secrets.compare_digest(token, current['token'])
                    or current['bundle_id'] != bundle_id):
                raise ai.AnalysisError(STALE_BINDING)
            try:
                fingerprint = _fingerprint(metadata_reader())
            except Exception:
                self._revoke(session)
                raise ai.AnalysisError('归档索引暂不可用，请重新识别并预览') from None
            if fingerprint != current['fingerprint'] or self.clock() >= current['expires']:
                self._revoke(session)
                raise ai.AnalysisError(STALE_BINDING)
            frozen = {'binding_token': token, 'session_id': session['session_id'],
                      'generation': session['generation'], 'fingerprint': fingerprint,
                      'observation_seq': session['seq']}
            if expected and (any(expected[key] != frozen[key] for key in
                                  ('binding_token', 'session_id', 'generation', 'fingerprint'))
                             or expected['observation_seq'] > frozen['observation_seq']):
                raise ai.AnalysisError(STALE_BINDING)
            return frozen
