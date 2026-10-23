"""离线 A/B 回放：比较当前判级与受控记忆建议，不修改线上状态。

A 组：使用 graph_state 中 Agent 的原始风险等级。
B 组：只允许记忆建议在证据不足或同等级时提供建议；绝不把实时高风险降级。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.case_memory import search_cases, summarize_memory_guidance
from tools.common import load_json, project_root


def _golden() -> dict[str, dict[str, Any]]:
    data = load_json(project_root() / "results" / "golden_set" / "golden_set.json")
    return {str(item.get("pair_id")): item for item in data.get("entries", [])}


def _safe_memory_risk(current: str | None, guidance: dict[str, Any]) -> str | None:
    """B 组模拟策略：高风险不可降级，其余只接受明确多数建议。"""
    if not guidance.get("available"):
        return current
    suggested = guidance.get("suggested_risk")
    if current == "高风险" and suggested != "高风险":
        return current
    if current is None:
        return suggested
    if guidance.get("action") == "advisory_alignment":
        return suggested
    return current


def evaluate() -> dict[str, Any]:
    root = project_root() / "results" / "agent" / "graph_runs"
    golden = _golden()
    rows: list[dict[str, Any]] = []
    for run in root.iterdir() if root.is_dir() else []:
        if not run.is_dir() or not (run / "acceptance.json").is_file():
            continue
        try:
            state = load_json(run / "graph_state.json")
            acceptance = load_json(run / "acceptance.json")
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(state, dict) or not isinstance(acceptance, list) or not acceptance:
            continue
        pair_id = str(state.get("pair_id") or acceptance[-1].get("pair_id") or "")
        if pair_id not in golden:
            continue
        scene = state.get("scene") or {}
        summary = scene.get("scene") if isinstance(scene.get("scene"), dict) else {}
        work = str(summary.get("dominant_state") or "")
        site = str(summary.get("site_type") or "")
        query = " ".join(x for x in (work, site, summary.get("description")) if x)
        cases = search_cases(query, work_type=work, site_type=site, limit=3,
                             exclude_task_id=state.get("task_id"))
        current = state.get("overall_risk") or (state.get("risk_assessment") or {}).get("overall_risk")
        guidance = summarize_memory_guidance(cases, current)
        b_risk = _safe_memory_risk(current, guidance)
        expected = golden[pair_id].get("risk_std")
        rows.append({"task_id": state.get("task_id"), "pair_id": pair_id,
                     "expected": expected, "a_risk": current, "b_risk": b_risk,
                     "a_correct": current == expected, "b_correct": b_risk == expected,
                     "guidance_action": guidance.get("action"),
                     "suggested_risk": guidance.get("suggested_risk"),
                     "memory_count": len(cases)})
    a_correct = sum(row["a_correct"] for row in rows)
    b_correct = sum(row["b_correct"] for row in rows)
    return {"mode": "offline_ab_replay", "cases": len(rows),
            "a_correct": a_correct, "b_correct": b_correct,
            "a_accuracy": a_correct / len(rows) if rows else None,
            "b_accuracy": b_correct / len(rows) if rows else None,
            "action_counts": dict(Counter(row["guidance_action"] for row in rows)),
            "rows": rows}


if __name__ == "__main__":
    print(evaluate())
