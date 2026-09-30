"""Bounded, explicit reply preferences; never free-form prompt instructions."""
from __future__ import annotations

from dashboard import analysis as ai


DEFAULT_STYLE = {'tone': 'natural', 'empathy': 'balanced', 'length': 'short'}
STYLE_OPTIONS = {
    'tone': frozenset({'natural', 'playful', 'gentle', 'direct'}),
    'empathy': frozenset({'restrained', 'balanced', 'attentive'}),
    'length': frozenset({'short', 'normal'}),
}


def normalize_style(value):
    """Return a fresh complete preference set, rejecting any unlisted input."""
    error = '回复风格设置无效，请重新选择语气、共情程度和回复长度'
    if not isinstance(value, dict) or set(value) - set(STYLE_OPTIONS):
        raise ai.AnalysisError(error)
    result = dict(DEFAULT_STYLE)
    for field, choice in value.items():
        if not isinstance(choice, str) or choice not in STYLE_OPTIONS[field]:
            raise ai.AnalysisError(error)
        result[field] = choice
    return result
