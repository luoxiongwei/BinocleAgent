# -*- coding: utf-8 -*-
"""Agent 风险报告的目标引用与法规条款证据校验。"""
from __future__ import annotations

from typing import Any

from .common import ToolResult


def validate_report_evidence(report: dict[str, Any], *, target_ids: set[str],
                             allowed_clauses: set[str]) -> ToolResult:
    """检查报告引用的目标和条款是否存在于上游证据中；不推断缺失信息。"""
    if not isinstance(report, dict):
        return ToolResult("error", error="报告必须是 JSON 对象")
    warnings = report.get("warnings", []) or []
    if not isinstance(warnings, list):
        return ToolResult("error", error="warnings 必须是列表")
    invalid_targets: list[dict[str, Any]] = []
    invalid_clauses: list[dict[str, Any]] = []
    missing_clauses: list[int] = []
    for index, warning in enumerate(warnings):
        if not isinstance(warning, dict):
            invalid_targets.append({"warning_index": index, "reason": "警示项必须是对象"})
            continue
        evidence_text = " ".join(str(value) for value in warning.get("evidence", []) or [])
        # 显式目标字段优先；兼容既有报告中只在 evidence/violation 中提及 t1 等 ID 的格式。
        refs = warning.get("target_ids")
        if refs is None:
            refs = sorted({item for item in target_ids if item in evidence_text or
                           item in str(warning.get("violation", ""))})
        unknown = [item for item in refs if item not in target_ids]
        if unknown:
            invalid_targets.append({"warning_index": index, "target_ids": unknown})
        clause_ids = warning.get("clause_ids", []) or []
        if not clause_ids:
            missing_clauses.append(index)
        out_of_scope = [item for item in clause_ids if item not in allowed_clauses]
        if out_of_scope:
            invalid_clauses.append({"warning_index": index, "clause_ids": out_of_scope})
    needs_review = bool(invalid_targets or invalid_clauses or missing_clauses)
    result = ToolResult("review" if needs_review else "ok", {
        "valid": not needs_review,
        "warning_count": len(warnings),
        "invalid_targets": invalid_targets,
        "invalid_clauses": invalid_clauses,
        "missing_clause_warnings": missing_clauses,
        "need_human_review": needs_review,
    }, confidence=1.0)
    result.add_trace("evidence.validate_report", targets=len(target_ids),
                     allowed_clauses=len(allowed_clauses), warnings=len(warnings))
    return result
