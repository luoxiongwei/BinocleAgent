# -*- coding: utf-8 -*-
"""RAG 结果工具接口；检索算法仍由 scripts/llm_judge.py 使用。"""
from __future__ import annotations

from pathlib import Path

from .common import ToolResult, load_json, project_root


def load_rag_meta(path: str | Path | None = None) -> ToolResult:
    target = Path(path) if path else project_root() / "results" / "rag_store_v2" / "meta.json"
    if not target.exists():
        return ToolResult("error", error=f"RAG 元数据不存在: {target}")
    result = ToolResult("ok", load_json(target))
    result.add_trace("rag.load_meta", path=str(target))
    return result
