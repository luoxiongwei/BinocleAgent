# -*- coding: utf-8 -*-
"""人工复核回写闭环（阶段四）。

链路：batch_acceptance 生成 expert_review.csv → 专家填写 → 本模块回收：
  1. ingest：校验标签合法性 → 写入 results/golden_set/golden_set.json（同一 pair 重复复核取最新，保留历史）
  2. report：专家标签 vs Agent 当时输出的风险一致率（真准确率的第一份来源）
  3. 供 batch_acceptance 查询：golden 集合里的样本改用专家标签做对照（取代"与旧模型比"的漂移指标）
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.outcome import RISK_RANK, VALID_RISKS, normalize_risk

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.common import load_json, save_json, project_root

VALID_EXPERT_LABELS = set(VALID_RISKS) | {"转人工"}


def normalize_action(value: str | None) -> str | None:
    text = str(value or "").strip()
    risk = normalize_risk(text)
    if risk:
        return risk
    if "转人工" in text or "人工" in text or "manual" in text.lower():
        return "转人工"
    return None


def golden_set_path(project: Path | None = None) -> Path:
    root = (project or project_root()).resolve()
    return root / "results" / "golden_set" / "golden_set.json"


def load_golden(project: Path | None = None) -> dict[str, Any]:
    """返回 {pair_id: entry}；文件缺失或损坏时返回空表。"""
    p = golden_set_path(project)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {e["pair_id"]: e for e in data.get("entries", []) if isinstance(e, dict) and e.get("pair_id")}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


def ingest_review(csv_path: str | Path, project: Path | None = None) -> dict[str, Any]:
    """回收填写完成的复核清单，更新 golden set 并输出一致率报告。"""
    root = (project or project_root()).resolve()
    src = Path(csv_path)
    if not src.is_file():
        raise FileNotFoundError(f"复核清单不存在: {src}")

    golden_path = golden_set_path(root)
    golden = load_golden(root)
    accepted, skipped, rejected = [], [], []
    with src.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            pid = (row.get("pair_id") or "").strip()
            label = (row.get("expert_risk_label") or "").strip()
            if not pid:
                continue
            if not label:
                skipped.append(pid)
                continue
            if label not in VALID_EXPERT_LABELS:
                rejected.append({"pair_id": pid, "label": label,
                                 "reason": f"非法标签，允许值: {sorted(VALID_RISKS)}"})
                continue
            entry = golden.get(pid, {"pair_id": pid, "history": []})
            prior = {k: entry.get(k) for k in ("expert_risk", "reviewed_at") if entry.get(k)}
            if prior:
                entry["history"].append(prior)          # 保留历次复核，最新为准
            entry.update({
                "expert_risk": label,
                "expert_warning_summary": (row.get("expert_warning_summary") or "").strip(),
                "expert_clause_ids": sorted({c.strip() for c in
                                             (row.get("expert_clause_ids") or "").split(";") if c.strip()}),
                "reviewer": (row.get("reviewer") or "").strip() or "unknown",
                "review_notes": (row.get("review_notes") or "").strip(),
                "agent_risk_at_review": (row.get("agent_risk") or "").strip() or None,
                "source_run_dir": (row.get("run_dir") or "").strip() or None,
                "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            golden[pid] = entry
            accepted.append(pid)

    # 一致率报告（专家 vs Agent 当时输出；无 agent 输出的样本不计入）
    matched: list[str] = []
    mismatched: list[str] = []
    comparisons = []
    for pid, e in golden.items():
        agent_risk = e.get("agent_risk_at_review")
        if not agent_risk or agent_risk not in RISK_RANK:
            if agent_risk:
                # 带后缀的非纯三档值（如「高风险-强制告警」）不静默丢弃：计数进报告，避免一致率悄悄失真
                print(f"警告: {pid} 的 agent_risk_at_review={agent_risk!r} 非纯三档值，不计入一致率",
                      file=sys.stderr)
            continue
        ok = agent_risk == e["expert_risk"]
        comparisons.append({"pair_id": pid, "agent": agent_risk, "expert": e["expert_risk"],
                            "match": ok,
                            "gap": abs(RISK_RANK[agent_risk] - RISK_RANK[e["expert_risk"]])})
        (matched if ok else mismatched).append(pid)
    accuracy = round(len(matched) / len(comparisons), 3) if comparisons else None

    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": sorted(golden.values(), key=lambda e: e["pair_id"]),
    }
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(golden_path, payload)

    report = {
        "ingested": accepted, "skipped_empty": skipped, "rejected": rejected,
        "golden_size": len(golden),
        "comparison": {"n": len(comparisons), "accuracy": accuracy,
                       "matched": sorted(matched), "mismatched": sorted(mismatched),
                       "over_severe": [c["pair_id"] for c in comparisons
                                       if not c["match"] and RISK_RANK[c["agent"]] > RISK_RANK[c["expert"]]],
                       "under_severe": [c["pair_id"] for c in comparisons
                                        if not c["match"] and RISK_RANK[c["agent"]] < RISK_RANK[c["expert"]]]},
        "golden_set": str(golden_path),
    }
    save_json(golden_path.parent / "ingest_report.json", report)
    return report


def golden_comparison(pair_id: str, agent_risk: str | None,
                      agent_clauses: list[str] | None = None,
                      project: Path | None = None,
                      agent_disposition: str | None = None) -> dict[str, Any] | None:
    """batch_acceptance 用：该 pair 有专家真值时返回对照块，否则 None。"""
    entry = load_golden(project).get(pair_id)
    if not entry:
        return None
    expert = entry.get("expert_risk") or entry.get("risk_std")
    agent_norm = normalize_risk(agent_risk)
    expert_norm = normalize_risk(expert)
    match = (agent_norm == expert_norm) if (agent_norm and expert_norm) else None
    expert_action = normalize_action(entry.get("expert_risk") or entry.get("action"))
    agent_action = normalize_action(agent_disposition) or agent_norm
    action_match = (agent_action == expert_action) if (agent_action and expert_action) else None
    out = {"golden_risk": expert, "agent_risk": agent_risk,
           "golden_risk_level": expert_norm, "agent_risk_level": agent_norm,
           "risk_match": match, "expert_action": expert_action,
           "agent_action": agent_action, "action_match": action_match,
           "reviewer": entry.get("reviewer"), "reviewed_at": entry.get("reviewed_at")}
    if agent_clauses is not None and entry.get("expert_clause_ids"):
        gs = set(agent_clauses)
        es = set(entry["expert_clause_ids"])
        out["clause_recall"] = (len(gs & es) / len(es)) if es else None
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="expert_review.csv 回写闭环：回收复核 → golden set → 真值一致率")
    parser.add_argument("--csv", default="", help="填写完成的 expert_review.csv 路径")
    parser.add_argument("--status", action="store_true", help="仅查看 golden set 现状")
    args = parser.parse_args()
    if args.status or not args.csv:
        golden = load_golden()
        print(f"golden set 条目: {len(golden)}")
        for pid, e in sorted(golden.items()):
            print(f"  {pid}: expert={e.get('expert_risk')} agent@review={e.get('agent_risk_at_review')}"
                  f" reviewer={e.get('reviewer')}")
        if not args.csv:
            print("（用 --csv <路径> 回收复核清单）")
            return 0
    report = ingest_review(args.csv)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
