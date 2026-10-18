# -*- coding: utf-8 -*-
"""Offline 30-pair baseline preflight; does not call model APIs."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tools.common import load_json, save_json, project_root
from tools.config_loader import configured_path, load_config
from tools.vlm_tool import validate_scene


def audit_baseline(project: Path | None = None) -> dict[str, Any]:
    root = (project or project_root()).resolve()
    config = load_config(root / "config" / "agent.yaml")
    paths = {name: configured_path(name, config) for name in
             ("detections", "scenes", "warnings")}
    detections = load_json(paths["detections"]).get("detections", {})
    scenes = load_json(paths["scenes"]).get("scenes", {})
    warnings = load_json(paths["warnings"]).get("warnings", {})
    detection_ids = {Path(name).stem for name in detections}
    scene_ids, warning_ids = set(scenes), set(warnings)
    rows = []
    for image_id in sorted(detection_ids | scene_ids | warning_ids):
        scene = scenes.get(image_id)
        warning = warnings.get(image_id)
        count = len(detections.get(f"{image_id}.jpg", []))
        valid, error = validate_scene(scene, count) if scene is not None else (False, "缺少场景结果")
        warning_items = (warning or {}).get("warnings", []) if isinstance(warning, dict) else []
        missing_clause = [i for i, item in enumerate(warning_items)
                          if not (item.get("clause_ids") or [])]
        rows.append({
            "pair_id": image_id,
            "detection_count": count,
            "scene_present": image_id in scene_ids,
            "scene_valid": valid,
            "scene_error": error,
            "report_present": image_id in warning_ids,
            "warning_count": len(warning_items),
            "warnings_without_clause": missing_clause,
            "baseline_need_human_review": bool((warning or {}).get("need_human_review"))
            if isinstance(warning, dict) else None,
        })
    total = len(rows)
    return {
        "audit_type": "offline_baseline_preflight",
        "api_calls": 0,
        "project_root": str(root),
        "inputs": {key: str(value) for key, value in paths.items()},
        "summary": {
            "pairs": total,
            "detection_scene_report_complete": sum(
                row["scene_present"] and row["report_present"] for row in rows),
            "scene_valid": sum(row["scene_valid"] for row in rows),
            "scene_invalid": sum(not row["scene_valid"] for row in rows),
            "reports_with_unreferenced_warnings": sum(
                bool(row["warnings_without_clause"]) for row in rows),
        },
        "pairs": rows,
        "limitations": [
            "本审计只校验已有基线文件的覆盖度和结构，不执行实时 GDYOLO/VLM/RAG/LLM。",
            "阶段二 30 对 live Agent 验收仍需显式运行真实工具链，并与该基线逐对比较。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="离线审计 30 对既有结果；不调用 API")
    parser.add_argument("--out", default="", help="审计 JSON 输出文件")
    args = parser.parse_args()
    result = audit_baseline()
    target = Path(args.out) if args.out else project_root() / "results" / "agent" / "phase2_baseline_audit.json"
    save_json(target, result)
    print(f"pairs={result['summary']['pairs']} scene_valid={result['summary']['scene_valid']} "
          f"scene_invalid={result['summary']['scene_invalid']} api_calls=0")
    print(f"audit={target}")
    return 0 if result["summary"]["pairs"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
