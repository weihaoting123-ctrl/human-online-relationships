"""Pure single-conversation text scope and latest-message selection."""
from __future__ import annotations

import re
from datetime import datetime

from dashboard import analysis as ai
from dashboard.analysis_metrics import _message_time


MAX_MESSAGES = 200
MAX_CHARS = 20_000
MAX_DRAFT_CHARS = 4000


def scope(request):
    allowed = {'bundle_id', 'date_from', 'date_to', 'max_messages', 'direction', 'latest_draft',
               'binding_revision', 'binding_token'}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ai.AnalysisError('回复建议请求格式无效')
    if 'binding_token' in request and (not isinstance(request['binding_token'], str)
                                      or not ai.ID_RE.fullmatch(request['binding_token'])):
        raise ai.AnalysisError('当前会话候选无效，请重新识别并预览')
    bundle_id = request.get('bundle_id')
    if (not isinstance(bundle_id, str) or not bundle_id or len(bundle_id) > 240
            or any(char in bundle_id for char in '/\\\x00') or bundle_id in {'.', '..'}):
        raise ai.AnalysisError('请选择一个有效会话')
    scope = {'bundle_id': bundle_id}
    for field in ('date_from', 'date_to'):
        value = request.get(field, '')
        if not isinstance(value, str):
            raise ai.AnalysisError('日期格式无效')
        if value:
            try:
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                    raise ValueError
                datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                raise ai.AnalysisError('日期格式无效') from None
        scope[field] = value
    if scope['date_from'] and scope['date_to'] and scope['date_from'] > scope['date_to']:
        raise ai.AnalysisError('开始日期不能晚于结束日期')
    limit = request.get('max_messages', 80)
    if type(limit) is not int or not 1 <= limit <= MAX_MESSAGES:
        raise ai.AnalysisError('文字条数上限须为 1 至 200 的整数')
    direction = request.get('direction')
    if not isinstance(direction, str) or direction not in {'closer', 'relaxed', 'invite'}:
        raise ai.AnalysisError('请选择有效的回复方向')
    draft = request.get('latest_draft', '')
    if not isinstance(draft, str) or len(draft) > MAX_DRAFT_CHARS:
        raise ai.AnalysisError('手动草稿须为不超过 4000 字的文字')
    revision = request.get('binding_revision')
    if type(revision) is not int or revision < 0:
        raise ai.AnalysisError('会话绑定版本无效，请重新选择会话')
    return {**scope, 'max_messages': limit, 'direction': direction}, draft, revision


def sample(payload, scope, draft):
    names = [payload.get(key) for key in ('contact_display', 'my_display', 'contact_name', 'my_name')
             if isinstance(payload.get(key), str)]
    candidates = []
    scoped_count = excluded = 0
    for index, message in enumerate(payload['messages']):
        if not isinstance(message, dict):
            continue
        stamp = _message_time(message.get('timestamp'))
        if stamp is None:
            continue
        order, local_time = stamp
        day = local_time.date().isoformat()
        if (scope['date_from'] and day < scope['date_from']) or (scope['date_to'] and day > scope['date_to']):
            continue
        scoped_count += 1
        if message.get('type', 'text') != 'text':
            excluded += 1
            continue
        text = message.get('content')
        if not isinstance(text, str) or not text.strip():
            continue
        for field in ('sender_name', 'senderName', 'sender_display', 'senderUsername'):
            if isinstance(message.get(field), str):
                names.append(message[field])
        candidates.append((order, index, day, message.get('sender') == 'me', text))
    draft = ai.redact_text(draft, names)
    budget = MAX_CHARS - len(draft)
    selected = []
    chars = truncated = 0
    # Walk newest first so the character bound never drops the latest message.
    for _, _, day, me, original in sorted(candidates, reverse=True)[:scope['max_messages']]:
        text = ai.redact_text(original, names)
        if not text:
            continue
        bounded = text[:budget]
        if len(bounded) < len(text):
            truncated += 1
        if bounded:
            selected.append({'date': day, 'sender': '我' if me else '对方／其他参与者', 'text': bounded})
            chars += len(bounded)
            budget -= len(bounded)
        if budget <= 0:
            break
    if not selected:
        raise ai.AnalysisError('所选范围内没有可用的聊天文字；回复建议不读取语音或转写')
    return list(reversed(selected)), draft, {
        'scope_messages': scoped_count, 'eligible_messages': len(candidates),
        'sample_messages': len(selected), 'sample_chars': chars,
        'sample_date_from': selected[-1]['date'], 'sample_date_to': selected[0]['date'],
        'omitted_messages': len(candidates) - len(selected),
        'excluded_nontext': excluded, 'truncated_messages': truncated, 'draft_chars': len(draft),
    }
