# -*- coding: utf-8 -*-
"""VLM 结果工具接口；API 调用仍由 scripts/vlm_judge.py 负责。"""
from __future__ import annotations

from pathlib import Path

from .common import ToolResult, load_json, project_root, validate_schema


def load_scene_results(path: str | Path | None = None) -> ToolResult:
    target = Path(path) if path else project_root() / "results" / "l2_scene" / "l2_scene.json"
    if not target.exists():
        return ToolResult("error", error=f"VLM 场景结果不存在: {target}")
    raw = load_json(target)
    scenes = raw.get("scenes", raw)
    result = ToolResult("ok", {"scenes": scenes, "stats": raw.get("stats", {})})
    result.add_trace("vlm.load_scene", path=str(target), scenes=len(scenes))
    return result


def validate_scene_contract(scene: dict) -> tuple[bool, str]:
    """按 `schemas/scene.schema.json` 校验 L2 场景契约（契约漂移检测入口）。

    与 `validate_scene` 的分工：
      - `validate_scene`：能否进入判级的**硬门槛**（结构必需项，失败 → parsing_error → 转人工）；
      - `validate_scene_contract`：字段形状是否仍与 prompt 一致（**审计/回归**用，不改处置）。
    两者规则不同是设计使然，勿合并。
    """
    if not isinstance(scene, dict):
        return False, "场景结果必须是对象"
    return validate_schema(scene, "scene")


def validate_scene(scene: dict, expected_boxes: int | None = None) -> tuple[bool, str]:
    if not isinstance(scene, dict):
        return False, "场景结果必须是对象"
    targets = scene.get("targets")
    if targets is None:
        return False, "缺少 targets"
    if not isinstance(targets, list):
        return False, "targets 必须是列表"
    ids = [x.get("bbox_id") for x in targets if isinstance(x, dict)]
    if len(ids) != len(set(ids)):
        return False, "targets 存在重复 bbox_id"
    if expected_boxes is not None and len(targets) != expected_boxes:
        return False, f"targets 数量 {len(targets)} != 检测框数量 {expected_boxes}"
    if expected_boxes is not None:
        expected_ids = {f"t{index}" for index in range(1, expected_boxes + 1)}
        if set(ids) != expected_ids:
            return False, f"bbox_id 必须与检测目标顺序一一对应: 期望 {sorted(expected_ids)}，实际 {sorted(ids)}"
    return True, ""
