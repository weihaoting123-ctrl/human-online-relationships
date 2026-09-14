"""Local diagnostic vocabulary. Never carries provider values or raw output."""
from __future__ import annotations

import re


class AnalysisError(ValueError):
    """Only fixed, locally authored messages may cross the HTTP boundary."""


RULES = {
    'OUTPUT_FIELDS': '缺少必填字段或包含不允许的字段',
    'OUTPUT_TYPE': '字段类型不符合要求',
    'OUTPUT_LIMIT': '文字长度或项目数量超过输出格式限制',
    'OUTPUT_ENUM': '字段值不在允许的枚举范围内',
    'OUTPUT_DATE': '日期必须是有效的 YYYY-MM-DD 字符串',
    'OUTPUT_DATE_SCOPE': '事件日期不在本次允许范围内，或起止顺序不正确',
    'OUTPUT_EVIDENCE_REF': '证据的日期与匿名样本序号不是本次输入中允许的配对',
    'OUTPUT_EVIDENCE_DUP': '同一证据配对被重复引用',
    'OUTPUT_EVIDENCE_RANGE': '证据日期超出所属事件的起止范围',
    'OUTPUT_EVIDENCE_REQUIRED': '此证据等级或原因类型必须附带有效证据',
    'OUTPUT_STATE': '事件类型、状态与证据等级不一致',
    'OUTPUT_ID': '事件编号不符合当前分段或汇总格式',
    'OUTPUT_DUPLICATE_ID': '事件编号重复',
    'OUTPUT_LINK': '事件关联不存在、自我引用、成环或指向较晚事件',
    'OUTPUT_MERGE_CHANGED': '汇总修改了原事件，或更改了已有的非空关联',
    'OUTPUT_REASON_CHANGED': '汇总中的原因未完整沿用已提供的原因和证据',
    'OUTPUT_JSON': '服务商返回的内容不是完整有效的 JSON',
    'OUTPUT_INCOMPLETE': '服务商标记输出未完成、被截断或被拦截',
    'OUTPUT_RESPONSE': '服务商响应结构无效或缺少可读取的内容',
}
_FIELDS = re.compile(
    r'(?:response|report|summary|observations|actions|caveats|timeline'
    r'(?:\.(?:version|generated|coverage|events_total|events_truncated|events'
    r'(?:\[\d{1,4}\](?:\.(?:id|date_from|date_to|kind|title|summary|status|evidence_level|related_event_id|evidence'
    r'(?:\[\d\](?:\.(?:date|sample_index))?)?))?)?'
    r'|no_contact_reason(?:\.(?:kind|summary|evidence(?:\[\d\](?:\.(?:date|sample_index))?)?|limitations(?:\[\d\])?))?))?)'
)


def public_error_detail(value):
    """Allowlist on both persistence and response boundaries; ignore stored prose."""
    if not isinstance(value, dict):
        return None
    code, field = value.get('code'), value.get('field')
    if not isinstance(code, str) or code not in RULES or not isinstance(field, str) or not _FIELDS.fullmatch(field):
        return None
    if any(int(index) >= 1200 for index in re.findall(r'\[(\d+)\]', field)):
        return None
    if any(int(index) >= 3 for index in re.findall(r'(?:evidence|limitations)\[(\d+)\]', field)):
        return None
    result = {'code': code, 'field': field, 'message': RULES[code]}
    if value.get('phase') in ('segment', 'merge'):
        result['phase'] = value['phase']
    for key, maximum in (('segment_index', 200), ('call_index', 400)):
        item = value.get(key)
        if type(item) is int and 1 <= item <= maximum:
            result[key] = item
    return result


class OutputValidationError(AnalysisError):
    def __init__(self, code, field):
        self.detail = public_error_detail({'code': code, 'field': field}) or {
            'code': 'OUTPUT_RESPONSE', 'field': 'response', 'message': RULES['OUTPUT_RESPONSE']}
        super().__init__(f"模型输出校验失败：{self.detail['field']}，{self.detail['message']}；本次不会自动重试")


def exception_detail(error):
    return public_error_detail(error.detail) if isinstance(error, OutputValidationError) else None
