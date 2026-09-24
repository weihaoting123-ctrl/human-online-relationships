"""Explicit, time-limited suggestions for ONE pinned source; no automatic sending.

All state is bounded process memory. Reader/network work never holds the session
lock: stop is immediate and invalidates late results. Title binding is a pause
fence, not proof of the active native account or conversation.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time

from dashboard import analysis as ai
from dashboard.copilot import service as cp
from dashboard.copilot.read_transport import read_tail as _read_tail, validate_tail, LiveReadError, _stamp


LIMITS = {'duration_seconds': 900, 'max_calls': 6, 'min_call_interval_seconds': 20,
          'debounce_seconds': 3, 'max_context_messages': 200, 'max_context_chars': 20000,
          'max_batch_messages': 20, 'max_batch_chars': 4000}
NOTICES = (
    '授权仅限你核对的固定账号和单人会话，不随识别标题更换对象；当前微信账号和联系人无法由标题可靠核验。',
    '发送所选历史范围的有限文字与启动后此固定会话的新文字；首次读取只建立基线，不发送已有最近消息。',
    '最多持续15分钟、6次新增调用，相隔至少20秒；每批新文字最多20条/4000字，总上下文最多200条/20000字。',
    '仅文字，不含图片、音频、转写或其他会话；尽力脱敏不能保证自由文本完全匿名。',
    '标题变化或不可用会停止；同名切换可能无法识别。读取有延迟，新发出文字将在下一次读取时清除旧建议。',
    '只提供建议，不发送消息。失败或结果未知即停止，不自动重试；在途请求不能撤回，可能已计费。',
)
_LOCK = threading.RLock()
# HTTP metadata/config mutations share only this short admission/claim gate.
# It is never held across reader/network operations or acquired by stop.
_GATE = threading.RLock()
_PREVIEWS = {}
_SESSIONS = {}
_now = time.monotonic
_wall_now = time.time


class _ScopeChanged(ai.AnalysisError):
    def __init__(self, code):
        self.code = code
        super().__init__('固定会话授权已变化，请重新预览并确认')


def mutate(action, *args, **kwargs):
    """Serialize local HTTP policy/config changes with the final paid claim."""
    with _GATE:
        result = action(*args, **kwargs)
        # Revocation is irreversible even if a setting is later restored (ABA).
        # Failed mutations leave the valid authorization unchanged.
        with _LOCK:
            _PREVIEWS.clear()
            for session in _SESSIONS.values():
                if session['state'] != 'stopped':
                    _end(session, 'SETTINGS_CHANGED')
        return result


def _failure_reason(error):
    return error.code if isinstance(error, (LiveReadError, _ScopeChanged)) else 'READ_OR_SCOPE_UNAVAILABLE'


def _id(value):
    return isinstance(value, str) and ai.ID_RE.fullmatch(value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def _end(session, reason):
    session.update(state='stopped', reason=reason, result=None, pending=[], pending_peer=False,
                   sample=[], names=[], seen={}, local_servers={}, anchors=set(), cursor=None)


def _dto(session, *, report_busy=False):
    state = 'busy' if report_busy and session['busy'] and session['state'] != 'stopped' else session['state']
    return {'status': 'ok', 'session_id': session['session_id'], 'state': state,
            'reason': session['reason'], 'binding_revision': session['binding_revision'],
            'remaining_seconds': max(0, int(session['expires'] - _now())),
            'calls_used': session['attempts'], 'calls_remaining': max(0, 6 - session['attempts']),
            'result': session['result'] if session['state'] != 'stopped' else None}


def _session(workspace, identity):
    session = _SESSIONS.get(workspace)
    if not _id(identity) or not session or session['session_id'] != identity:
        raise ai.AnalysisError('限时建议会话不可用，请重新预览并明确确认')
    if session['state'] != 'stopped' and _now() >= session['expires']:
        _end(session, 'SESSION_EXPIRED')
    return session


def _guard(data_dir, contacts_dir, session, admission):
    """All filesystem reads occur outside the live lock, including metadata."""
    try:
        admission(session['scope']['bundle_id'])
    except Exception:
        raise _ScopeChanged('SCOPE_UNAVAILABLE') from None
    try:
        config = cp._configuration(data_dir)
    except Exception:
        raise _ScopeChanged('CONFIG_UNAVAILABLE') from None
    if cp._config_revision(config) != session['config_revision'] or not config.get('sealed_key'):
        raise _ScopeChanged('CONFIG_CHANGED')
    if cp.PROMPT_REVISION != session['prompt_revision']:
        raise _ScopeChanged('PROMPT_CHANGED')
    try:
        index = cp._binding_index(contacts_dir)
        with cp._LOCK:
            cp._BINDINGS.validate(session['workspace'], session['binding']['binding_token'],
                session['scope']['bundle_id'], lambda: index, expected=session['binding'])
    except Exception:
        raise _ScopeChanged('BINDING_CHANGED') from None
    return config, index


def _bound_history(sample, counts):
    remaining, output = 16000, []
    truncated = counts['truncated_messages']
    for row in reversed(sample):
        text = row['text'][:remaining]
        if text:
            output.append({**row, 'text': text})
            remaining -= len(text)
            truncated += len(text) < len(row['text'])
        if remaining == 0:
            break
    result = list(reversed(output))
    return result, {**counts, 'sample_messages': len(result), 'sample_chars': 16000 - remaining,
                    'sample_date_from': result[0]['date'], 'sample_date_to': result[-1]['date'],
                    'omitted_messages': counts['eligible_messages'] - len(result),
                    'truncated_messages': truncated}


def preview(data_dir, contacts_dir, request, *, admission):
    scope, draft, revision = cp._scope(request)
    if draft or 'binding_token' not in request:
        raise ai.AnalysisError('限时建议需要有效窗口候选，且不能携带手动草稿')
    binding = cp._validate_binding(data_dir, contacts_dir, request['binding_token'], scope['bundle_id'])
    admission(scope['bundle_id'])
    payload, signature = cp._source(contacts_dir, scope['bundle_id'])
    sample, _, counts = cp._sample(payload, {**scope, 'max_messages': min(scope['max_messages'], 180)}, '')
    sample, counts = _bound_history(sample, counts)
    config = cp._configuration(data_dir)
    if not config.get('sealed_key'):
        raise ai.AnalysisError('请先配置模型服务，再预览限时建议')
    names = [payload.get(key) for key in ('contact_display', 'my_display', 'contact_name', 'my_name')
             if isinstance(payload.get(key), str)]
    admission(scope['bundle_id'])
    binding = cp._validate_binding(data_dir, contacts_dir, binding['binding_token'], scope['bundle_id'], expected=binding)
    prepared = {'workspace': cp._workspace(data_dir, contacts_dir), 'scope': scope, 'sample': sample,
                'names': list(set(name for name in names if len(name) <= 100)), 'signature': signature, 'binding': binding,
                'binding_revision': revision, 'config_revision': cp._config_revision(config),
                'prompt_revision': cp.PROMPT_REVISION, 'expires': _now() + 600}
    with _LOCK:
        for identity, item in list(_PREVIEWS.items()):
            if item['expires'] <= _now():
                del _PREVIEWS[identity]
        if len(_PREVIEWS) >= 64:
            raise ai.AnalysisError('待确认限时预览过多，请稍后再试')
        identity = secrets.token_hex(24)
        _PREVIEWS[identity] = prepared
    return {'status': 'ok', 'preview_id': identity, 'binding_revision': revision, 'scope': scope,
            'binding_required': True, 'account_verified': False, 'limits': dict(LIMITS), 'counts': counts,
            'recipient': {'configured': True, 'provider': config.get('provider', ''),
                          'model': config.get('model', ''), 'endpoint': ai.PROVIDERS.get(config.get('provider'), '')},
            'privacy_notices': list(NOTICES)}


def start(data_dir, contacts_dir, request, *, admission):
    if (not isinstance(request, dict) or set(request) != {'preview_id', 'consent', 'binding_confirmed', 'binding_revision'}
            or request['consent'] is not True or request['binding_confirmed'] is not True
            or not _id(request['preview_id']) or type(request['binding_revision']) is not int
            or request['binding_revision'] < 0):
        raise ai.AnalysisError('请明确核对固定账号、联系人、范围与限时调用预算，再确认启动')
    workspace = cp._workspace(data_dir, contacts_dir)
    with _LOCK:
        prepared = _PREVIEWS.get(request['preview_id'])
        if not prepared or prepared['workspace'] != workspace:
            raise ai.AnalysisError('限时预览已使用或不可用，请重新预览')
        del _PREVIEWS[request['preview_id']]
        if prepared['expires'] <= _now() or prepared['binding_revision'] != request['binding_revision']:
            raise ai.AnalysisError('限时预览已过期或范围变化，请重新预览')
        previous = _SESSIONS.get(workspace)
        if previous and previous['busy']:
            raise ai.AnalysisError('上一会话仍有在途操作，请等待完成后重新预览')
        if previous:
            _end(previous, 'REPLACED')
        for key, item in list(_SESSIONS.items()):
            if not item['busy'] and item['expires'] <= _now():
                del _SESSIONS[key]
        if workspace not in _SESSIONS and len(_SESSIONS) >= 64:
            raise ai.AnalysisError('限时会话数量达到上限，请稍后再试')
        session = {**prepared, 'session_id': secrets.token_hex(24), 'expires': _now() + 900,
                   'state': 'active', 'reason': 'BASELINE', 'busy': True, 'attempts': 0,
                   'last_call': float('-inf'), 'result': None, 'pending': [], 'pending_peer': False,
                   'pending_since': _now(), 'seen': {}, 'local_servers': {},
                   'anchors': set(), 'cursor': None, 'identity': None}
        # Wall time bounds the authorized message creation times; monotonic time
        # alone bounds the session lifetime (clock changes never renew it).
        session.update(grant_wall=int(_wall_now()), watermark=float('-inf'))
        _SESSIONS[workspace] = session
    try:
        _guard(data_dir, contacts_dir, session, admission)
        _, signature = cp._source(contacts_dir, session['scope']['bundle_id'])
        if signature != session['signature']:
            raise _ScopeChanged('ARCHIVE_CHANGED')
        value = validate_tail(_read_tail(session['scope']['bundle_id'], None))
        _guard(data_dir, contacts_dir, session, admission)
        with _LOCK:
            _session(workspace, session['session_id'])
            if session['state'] != 'stopped':
                session['identity'] = value['identity']
                _observe(session, value, baseline=True)
    except Exception as error:
        with _LOCK:
            if session['state'] != 'stopped':
                _end(session, _failure_reason(error))
    finally:
        with _LOCK:
            session['busy'] = False
    with _LOCK:
        return _dto(session)


def _row_keys(row):
    keys = {'l:' + _digest(row['id'])}
    if row['server_id'] != '0':
        keys.add('s:' + _digest(row['server_id']))
    return keys


def _observe(session, value, *, baseline=False):
    if not baseline and value['identity'] != session['identity']:
        _end(session, 'SOURCE_IDENTITY_CHANGED')
        return
    keys = set().union(*(_row_keys(row) for row in value['rows'])) if value['rows'] else set()
    if not baseline and session['anchors'] and not keys.intersection(session['anchors']):
        _end(session, 'SOURCE_GAP')
        return
    fresh = []
    for row in value['rows']:
        local = 'l:' + _digest(row['id'])
        server = 's:' + _digest(row['server_id']) if row['server_id'] != '0' else None
        established = session['local_servers'].get(local)
        if established is not None and established != server:
            _end(session, 'SOURCE_CHANGED')
            return
        session['local_servers'][local] = server
        aliases = _row_keys(row)
        fingerprint = _digest({key: row[key] for key in ('timestamp', 'sender', 'text')})
        previous = [session['seen'][key] for key in aliases if key in session['seen']]
        if any(item != fingerprint for item in previous):
            _end(session, 'SOURCE_CHANGED')
            return
        if not previous:
            fresh.append(row)
        for key in aliases:
            session['seen'][key] = fingerprint
    if len(session['seen']) > 10000:
        _end(session, 'SOURCE_OVERFLOW')
        return
    session['anchors'], session['cursor'] = keys, value['cursor']
    if not baseline and any(_stamp(row['timestamp']) < max(session['watermark'], session['grant_wall']) for row in fresh):
        _end(session, 'SOURCE_TIME_REWIND')
        return
    if not baseline and any(_stamp(row['timestamp']) > session['grant_wall'] + LIMITS['duration_seconds'] for row in fresh):
        _end(session, 'SOURCE_TIME_OUT_OF_RANGE')
        return
    session['watermark'] = max([session['watermark']] + [_stamp(row['timestamp']) for row in value['rows']])
    if baseline:
        return
    if session['state'] != 'exhausted' and (len(fresh) > 20 or sum(len(row['text']) for row in fresh) > 4000):
        _end(session, 'BATCH_OVERFLOW')
        return
    for row in fresh:
        if row['sender'] == 'me':
            session['pending'] = []
            session['pending_peer'] = False
            session['result'] = None
        if session['state'] == 'exhausted':
            continue
        session['pending'].append(row)
        if row['sender'] == 'peer':
            session['pending_peer'] = True
        session['pending_since'] = _now()
    if len(session['pending']) > 20 or sum(len(row['text']) for row in session['pending']) > 4000:
        _end(session, 'BATCH_OVERFLOW')


def tick(data_dir, contacts_dir, request, *, admission):
    if (not isinstance(request, dict) or set(request) != {'session_id', 'binding_token', 'binding_revision'}
            or not _id(request['session_id']) or not _id(request['binding_token'])
            or type(request['binding_revision']) is not int or request['binding_revision'] < 0):
        raise ai.AnalysisError('限时建议请求格式无效')
    workspace = cp._workspace(data_dir, contacts_dir)
    with _LOCK:
        session = _session(workspace, request['session_id'])
        if (request['binding_token'] != session['binding']['binding_token']
                or request['binding_revision'] != session['binding_revision']):
            _end(session, 'BINDING_CHANGED')
        if session['state'] == 'stopped' or session['busy']:
            return _dto(session, report_busy=True)
        session['busy'] = True
    try:
        _guard(data_dir, contacts_dir, session, admission)
        value = validate_tail(_read_tail(session['scope']['bundle_id'], session['cursor']))
        config, index = _guard(data_dir, contacts_dir, session, admission)
        with _LOCK:
            _session(workspace, session['session_id'])
            if session['state'] == 'stopped':
                return _dto(session)
            _observe(session, value)
            if (session['state'] != 'active' or not session['pending_peer']
                    or _now() - session['pending_since'] < 3 or _now() - session['last_call'] < 20):
                return _dto(session)
            incoming = [{'date': row['timestamp'][:10], 'sender': '我' if row['sender'] == 'me' else '对方',
                         'text': ai.redact_text(row['text'], session['names'])} for row in session['pending']]
            if (len(incoming) > 20 or sum(len(row['text']) for row in incoming) > 4000
                    or len(session['sample']) + len(incoming) > 200
                    or sum(len(row['text']) for row in session['sample'] + incoming) > 20000):
                _end(session, 'BATCH_OVERFLOW')
                return _dto(session)
            content = {'date_from': session['scope']['date_from'], 'date_to': session['scope']['date_to'],
                       'direction': session['scope']['direction'], 'sample': session['sample'] + incoming,
                       'latest_draft': ''}
        try:
            key = ai._unseal(config['sealed_key'])
            if not isinstance(key, str) or not key:
                raise ValueError
        except Exception:
            raise _ScopeChanged('KEY_UNAVAILABLE') from None
        # No file I/O inside this shared binding/claim lock. Observation revoke
        # either wins before this claim or concerns an already in-flight call.
        with _GATE:
            config, index = _guard(data_dir, contacts_dir, session, admission)
            with cp._LOCK:
                cp._BINDINGS.validate(workspace, session['binding']['binding_token'], session['scope']['bundle_id'],
                                      lambda: index, expected=session['binding'])
                with _LOCK:
                    _session(workspace, session['session_id'])
                    if session['state'] != 'active':
                        return _dto(session)
                    session['attempts'] += 1
                    session['last_call'] = _now()
                    session['pending'], session['pending_peer'] = [], False
        try:
            result = cp.safe_result(ai._request_json(config, key, content, cp.SYSTEM_PROMPT))
        except Exception:
            with _LOCK:
                _end(session, 'CALL_FAILED_POSSIBLY_CHARGED')
            return _dto(session)
        _guard(data_dir, contacts_dir, session, admission)
        with _LOCK:
            _session(workspace, session['session_id'])
            if session['state'] != 'stopped':
                session['result'] = {'result_id': secrets.token_hex(24), **result}
                session['reason'] = 'CALL_LIMIT_REACHED' if session['attempts'] >= 6 else 'READY'
                session['state'] = 'exhausted' if session['attempts'] >= 6 else 'active'
    except Exception as error:
        with _LOCK:
            if session['state'] != 'stopped':
                _end(session, _failure_reason(error))
    finally:
        with _LOCK:
            session['busy'] = False
    with _LOCK:
        return _dto(session)


def stop(data_dir, contacts_dir, request):
    if not isinstance(request, dict) or set(request) != {'session_id'} or not _id(request['session_id']):
        raise ai.AnalysisError('停止限时建议请求格式无效')
    with _LOCK:
        session = _session(cp._workspace(data_dir, contacts_dir), request['session_id'])
        _end(session, 'USER_STOPPED')
        return _dto(session)
