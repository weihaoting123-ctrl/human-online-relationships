"""Trusted, source-controlled analysis perspectives shared with the dashboard.

Only this fixed local catalog supplies system guidance. User/chat content cannot
override it. Historical views have empty guidance to preserve existing semantics.
"""

import json
import re
from pathlib import Path

from dashboard.analysis_errors import AnalysisError

CATALOG_ERROR = "分析视角暂时无法加载，请检查本机安装后重启服务"


def _load():
    catalog = json.loads((Path(__file__).parent / "static" / "analysis-focus.json").read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or catalog.get("version") != 1 or not isinstance(catalog.get("presets"), list):
        raise ValueError("Invalid local analysis perspective catalog")
    presets = {}
    for item in catalog["presets"]:
        if (not isinstance(item, dict)
                or any(not isinstance(item.get(key), str) for key in ("id", "label", "description", "guidance"))
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item["id"])
                or item["id"] in presets or not item["label"].strip() or not item["description"].strip()
                or type(item.get("legacy")) is not bool
                or bool(item["guidance"].strip()) == item["legacy"]):
            raise ValueError("Invalid local analysis perspective entry")
        presets[item["id"]] = item
    default = catalog.get("default")
    if not isinstance(default, str) or default not in presets or presets[default]["legacy"]:
        raise ValueError("Invalid local default analysis perspective")
    return presets, default


try:
    PRESETS, DEFAULT_FOCUS = _load()
except (OSError, ValueError):
    # An incomplete installation must not prevent archive/history/config access.
    # No fallback prompt is safe: new previews/calls fail closed instead.
    PRESETS, DEFAULT_FOCUS = {}, ""
FOCUSES = frozenset(PRESETS)


def prompt_for(base_prompt, focus):
    """Preserve all output/safety constraints; vary only the selected emphasis."""
    if not PRESETS:
        raise AnalysisError(CATALOG_ERROR)
    if not isinstance(focus, str) or focus not in PRESETS:
        raise AnalysisError("请选择有效分析方向")
    guidance = PRESETS[focus]["guidance"]
    if not guidance:
        return base_prompt
    return base_prompt + "\n\n应用内置分析视角（仅改变关注重点，不改变上述输出格式、证据与安全约束）：\n" + guidance
