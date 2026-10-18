# -*- coding: utf-8 -*-
"""图像融合工具接口。融合与配准算法由 tools/image_registration.py 承担。"""
from __future__ import annotations

from pathlib import Path

from .common import ToolResult, project_root, load_json


def inspect_fusion(qc_path: str | Path | None = None) -> ToolResult:
    """读取融合 QC 结果，供 Agent 判断是否允许继续跨模态分析。"""
    path = Path(qc_path) if qc_path else project_root() / "results" / "fuse_qc.json"
    if not path.exists():
        return ToolResult("error", error=f"融合 QC 不存在: {path}")
    qc = load_json(path)
    summary = qc.get("summary", {})
    result = ToolResult("ok", {"summary": summary, "pairs": qc.get("pairs", [])})
    result.add_trace("fusion.inspect", path=str(path))
    return result
