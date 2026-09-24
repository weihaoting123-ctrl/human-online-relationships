"""Bounded reply assistance; no capture, background work, persistence or sending.

Prepared previews are in-process only and expire after ten minutes. Each preview can start
at most one provider request, after fresh admission and explicit confirmation.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dashboard import analysis as ai
from dashboard.copilot.context import MAX_MESSAGES, MAX_CHARS, scope as _scope, sample as _sample
from dashboard.copilot.contract import SYSTEM_PROMPT, PROMPT_REVISION, PRIVACY_NOTICES, safe_result
from dashboard.copilot.binding import BindingRegistry
from dashboard.library import LibraryRepository


PREVIEW_TTL = 600
MAX_PREVIEWS = 64
_LOCK = threading.RLock()
_PREVIEWS = {}
_BINDINGS = BindingRegistry(_LOCK)

def _configuration(data_dir):
    try:
        return ai._configuration(ai._root(data_dir))
    except Exception:
        raise ai.AnalysisError('本机模型配置不可用，请检查 AI 分析设置') from None


def _config_revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _source(contacts_dir, bundle_id):
    try:
        return ai._source(contacts_dir, bundle_id)
    except Exception:
        raise ai.AnalysisError('所选归档不可用或已变化，请重新预览') from None


def _workspace(data_dir, contacts_dir):
    return str(Path(data_dir).absolute()), str(Path(contacts_dir).absolute())


def _binding_index(contacts_dir):
    # Read only the complete existing projection. LibraryService.snapshot() and
    # visible() refresh archive files and would discard unavailable collisions.
    return LibraryRepository(Path(contacts_dir).parent).snapshot()['conversations']


def binding_start(data_dir, contacts_dir, request):
    if not isinstance(request, dict) or request:
        raise ai.AnalysisError('自动识别会话请求格式无效')
    return _BINDINGS.start(_workspace(data_dir, contacts_dir))


def binding_observe(data_dir, contacts_dir, request):
    return _BINDINGS.observe(_workspace(data_dir, contacts_dir), request,
                             lambda: _binding_index(contacts_dir))


def _validate_binding(data_dir, contacts_dir, token, bundle_id, *, expected=None):
    return _BINDINGS.validate(_workspace(data_dir, contacts_dir), token, bundle_id,
                              lambda: _binding_index(contacts_dir), expected=expected)


def _expire(now):
    for identity, value in list(_PREVIEWS.items()):
        if value['expires'] <= now:
            del _PREVIEWS[identity]


def preview(data_dir, contacts_dir, request, *, admission):
    scope, draft, revision = _scope(request)
    binding = None
    if 'binding_token' in request:
        binding = _validate_binding(data_dir, contacts_dir, request['binding_token'], scope['bundle_id'])
    admission(scope['bundle_id'])
    payload, signature = _source(contacts_dir, scope['bundle_id'])
    sample, draft, counts = _sample(payload, scope, draft)
    config = _configuration(data_dir)
    admission(scope['bundle_id'])
    with _LOCK:
        if binding:
            binding = _validate_binding(data_dir, contacts_dir, binding['binding_token'],
                                        scope['bundle_id'], expected=binding)
        now = time.time()
        _expire(now)
        if len(_PREVIEWS) >= MAX_PREVIEWS:
            raise ai.AnalysisError('待确认预览过多，请稍后重新预览')
        identity = secrets.token_hex(24)
        expires = now + PREVIEW_TTL
        _PREVIEWS[identity] = {
            'workspace': _workspace(data_dir, contacts_dir), 'scope': scope, 'sample': sample,
            'latest_draft': draft, 'binding_revision': revision, 'signature': signature,
            'binding': binding,
            'config_revision': _config_revision(config), 'prompt_revision': PROMPT_REVISION, 'expires': expires,
        }
    return {
        'status': 'ok', 'preview_id': identity, 'binding_revision': revision,
        'binding_required': bool(binding), 'account_verified': False,
        **({'observation_seq': binding['observation_seq']} if binding else {}),
        'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat(), 'scope': scope,
        'recipient': {'configured': bool(config.get('sealed_key')), 'provider': config.get('provider', ''),
                      'model': config.get('model', ''), 'endpoint': ai.PROVIDERS.get(config.get('provider'), '')},
        'max_calls': 1, 'counts': counts, 'privacy_notices': list(PRIVACY_NOTICES),
    }


def run(data_dir, contacts_dir, request, *, admission):
    if (not isinstance(request, dict)
            or not {'preview_id', 'consent', 'binding_revision'} <= set(request)
            or set(request) - {'preview_id', 'consent', 'binding_revision', 'binding_confirmed'}
            or request.get('consent') is not True):
        raise ai.AnalysisError('请明确确认仅发送本次预览范围；请求不能增加其他内容')
    identity, revision = request['preview_id'], request['binding_revision']
    if (not isinstance(identity, str) or not ai.ID_RE.fullmatch(identity)
            or type(revision) is not int or revision < 0):
        raise ai.AnalysisError('预览或会话绑定版本无效，请重新预览')
    with _LOCK:
        _expire(time.time())
        prepared = _PREVIEWS.get(identity)
        if not prepared or prepared['workspace'] != _workspace(data_dir, contacts_dir):
            raise ai.AnalysisError('预览已使用、过期或不可用，请重新预览并确认；不会重复发送')
        if prepared['binding'] and request.get('binding_confirmed') is not True:
            raise ai.AnalysisError('请先核对当前微信账号和联系人，再明确确认本次候选归档')
        if not prepared['binding'] and 'binding_confirmed' in request:
            raise ai.AnalysisError('手动选择的会话不接受自动识别确认字段')
        # Consume before all admission/config/source checks and before network.
        # This remains one-use on provider error, timeout and concurrent clicks.
        del _PREVIEWS[identity]
        if prepared['binding_revision'] != revision:
            raise ai.AnalysisError('会话绑定已变化，请重新预览并确认')
        if prepared['binding']:
            _validate_binding(data_dir, contacts_dir, prepared['binding']['binding_token'],
                               prepared['scope']['bundle_id'], expected=prepared['binding'])
        if prepared['prompt_revision'] != PROMPT_REVISION:
            raise ai.AnalysisError('回复提示版本已变化，请重新预览并确认')
        admission(prepared['scope']['bundle_id'])
        config = _configuration(data_dir)
        if _config_revision(config) != prepared['config_revision']:
            raise ai.AnalysisError('服务商配置已变化，请重新预览并确认')
        if not config.get('sealed_key'):
            raise ai.AnalysisError('请先在 AI 分析设置中配置模型服务')
        _, signature = _source(contacts_dir, prepared['scope']['bundle_id'])
        if signature != prepared['signature']:
            raise ai.AnalysisError('会话数据已变化，请重新预览并确认')
        admission(prepared['scope']['bundle_id'])
        try:
            key = ai._unseal(config['sealed_key'])
            if not isinstance(key, str) or not key:
                raise ValueError
        except Exception:
            raise ai.AnalysisError('本机模型密钥不可用，请检查 AI 分析设置') from None
        # Claim while holding the same lock used by observation revocation. A
        # later switch can discard the UI result but cannot undo this request.
        if prepared['binding']:
            _validate_binding(data_dir, contacts_dir, prepared['binding']['binding_token'],
                               prepared['scope']['bundle_id'], expected=prepared['binding'])
    content = {'date_from': prepared['scope']['date_from'], 'date_to': prepared['scope']['date_to'],
               'direction': prepared['scope']['direction'], 'sample': prepared['sample'],
               'latest_draft': prepared['latest_draft']}
    try:
        result = safe_result(ai._request_json(config, key, content, SYSTEM_PROMPT))
    except Exception:
        # Never reflect provider bodies, credentials, arbitrary exception text or
        # unsupported response fields. Unknown outcomes must not trigger retry.
        raise ai.AnalysisError('回复建议未完成；可能已计费，本次不会自动重试。再次尝试须重新预览并确认') from None
    return {'status': 'ok', 'result_id': secrets.token_hex(24), 'preview_id': identity,
            **({'observation_seq': prepared['binding']['observation_seq']} if prepared['binding'] else {}),
            'binding_revision': revision, **result}


def status(data_dir, *, enabled, conversations):
    config = _configuration(data_dir)
    items = []
    def metadata_text(value):
        return re.sub(r'[\x00-\x1f\ud800-\udfff]', '', value)[:200] if isinstance(value, str) else ''

    def metadata_date(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            return ''
        try:
            datetime.strptime(value, '%Y-%m-%d')
            return value
        except ValueError:
            return ''

    for item in conversations:
        dates = item.get('date_range')
        dates = dates if isinstance(dates, list) and len(dates) == 2 else ['', '']
        library = item.get('library')
        library = library if isinstance(library, dict) else {}
        count = item.get('message_count')
        items.append({'bundle_id': item['id'], 'contact_display': metadata_text(item.get('contact')),
                      'alias': metadata_text(library.get('alias')),
                      'message_count': count if type(count) is int and 0 <= count <= 1_000_000_000 else 0,
                      'date_from': metadata_date(dates[0]), 'date_to': metadata_date(dates[1])})
    return {'status': 'ok', 'enabled': enabled, 'configured': bool(config.get('sealed_key')),
            'provider': config.get('provider', ''), 'model': config.get('model', ''),
            'endpoint': ai.PROVIDERS.get(config.get('provider'), ''),
            'capabilities': {'archive_context': True, 'live_capture': False, 'auto_send': False,
                             'max_messages': MAX_MESSAGES, 'max_chars': MAX_CHARS, 'max_calls': 1},
            'conversations': items}
