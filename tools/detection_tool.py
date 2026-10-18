# -*- coding: utf-8 -*-
"""GDYOLO 检测结果工具接口。"""
from __future__ import annotations

from pathlib import Path

from .common import ToolResult, load_json, project_root


def load_detections(path: str | Path | None = None) -> ToolResult:
    """加载 L1 JSON，并统一暴露 Agent 所需的检测摘要。"""
    target = Path(path) if path else project_root() / "results" / "l1_smoke" / "l1_detections.json"
    if not target.exists():
        return ToolResult("error", error=f"检测结果不存在: {target}")
    raw = load_json(target)
    meta = raw.get("meta", {})
    detections = raw.get("detections", {})
    result = ToolResult("ok", {"meta": meta, "detections": detections})
    result.add_trace("detection.load", path=str(target), images=len(detections))
    return result


def validate_detection_result(value: dict) -> tuple[bool, str]:
    if not isinstance(value, dict):
        return False, "检测结果必须是对象"
    if not isinstance(value.get("detections", {}), dict):
        return False, "detections 必须是对象"
    return True, ""
