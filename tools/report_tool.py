# -*- coding: utf-8 -*-
"""L3 报告结果工具接口。"""
from __future__ import annotations

from pathlib import Path

from .common import ToolResult, load_json, project_root


def load_warning_results(path: str | Path | None = None) -> ToolResult:
    target = Path(path) if path else project_root() / "results" / "l3_warn" / "l3_warnings.json"
    if not target.exists():
        return ToolResult("error", error=f"风险结果不存在: {target}")
    raw = load_json(target)
    result = ToolResult("ok", {"warnings": raw.get("warnings", {}), "stats": raw.get("stats", {})})
    result.add_trace("report.load", path=str(target))
    return result


def validate_report(report: dict) -> tuple[bool, str]:
    """Reject malformed LLM output instead of interpreting it as a clean inspection."""
    if not isinstance(report, dict):
        return False, "报告根节点必须是对象"
    meta = report.get("_meta", {})
    if isinstance(meta, dict) and meta.get("parsing_error"):
        return False, meta.get("validate_error") or "LLM 输出解析/结构校验失败"
    warnings = report.get("warnings")
    if not isinstance(warnings, list):
        return False, "报告 warnings 必须是列表"
    for index, warning in enumerate(warnings):
        if not isinstance(warning, dict):
            return False, f"报告 warnings[{index}] 必须是对象"
        if "clause_ids" in warning and not isinstance(warning["clause_ids"], list):
            return False, f"报告 warnings[{index}].clause_ids 必须是列表"
        if "evidence" in warning and not isinstance(warning["evidence"], list):
            return False, f"报告 warnings[{index}].evidence 必须是列表"
    return True, ""
