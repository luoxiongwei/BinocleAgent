"""离线评估案例记忆召回，不改变线上判级。

示例：python scripts/evaluate_case_memory.py --limit 3
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.case_memory import search_cases
from tools.common import load_json, project_root, save_json


def evaluate(limit: int = 3) -> dict[str, Any]:
    root = project_root() / "results" / "agent" / "graph_runs"
    cases = []
    for run in root.iterdir() if root.is_dir() else []:
        if not run.is_dir() or not (run / "acceptance.json").is_file():
            continue
        try:
            acceptance = load_json(run / "acceptance.json")
            state = load_json(run / "graph_state.json") if (run / "graph_state.json").is_file() else {}
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(acceptance, list) or not acceptance or not isinstance(state, dict):
            continue
        record = acceptance[-1]
        scene = state.get("scene") or {}
        summary = scene.get("scene") if isinstance(scene.get("scene"), dict) else {}
        work_type = str(summary.get("dominant_state") or "")
        site_type = str(summary.get("site_type") or "")
        query = " ".join(str(value) for value in (work_type, site_type, summary.get("description")) if value)
        retrieved = search_cases(query, work_type=work_type, site_type=site_type,
                                 limit=limit, exclude_task_id=record.get("task_id"))
        cases.append({"task_id": record.get("task_id"), "case_kind": record.get("verdict"),
                      "work_type": work_type, "site_type": site_type,
                      "retrieved_count": len(retrieved),
                      "retrieved_task_ids": [item.get("task_id") for item in retrieved],
                      "retrieved_evidence": [{
                          "task_id": item.get("task_id"),
                          "human_risk": item.get("human_risk"),
                          "human_disposition": item.get("human_disposition"),
                          "retrieval_score": item.get("retrieval_score"),
                          "matched_terms": item.get("matched_terms", []),
                          "retrieval_mode": item.get("retrieval_mode"),
                      } for item in retrieved]})
    kinds = Counter(item["case_kind"] for item in cases)
    matched = sum(1 for item in cases if item["retrieved_count"] > 0)
    return {"total_reviewed_cases": len(cases), "matched_cases": matched,
            "coverage": matched / len(cases) if cases else None,
            "case_kinds": dict(kinds), "limit": limit, "cases": cases,
            "mode": "offline_read_only"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = evaluate(args.limit)
    if args.out:
        save_json(args.out, report)
    print(report)


if __name__ == "__main__":
    main()
