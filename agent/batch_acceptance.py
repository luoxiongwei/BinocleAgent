# -*- coding: utf-8 -*-
"""Resumable live acceptance over fused pairs; writes a separate run tree."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.common import load_json, save_json, project_root
from tools.config_loader import load_config, configured_path
from .graph import build_graph
from .review_loop import golden_comparison


def _warning_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    report = report or {}
    warnings = report.get("warnings", []) or []
    if not isinstance(warnings, list):
        warnings = []
    clauses = sorted({str(clause) for item in warnings if isinstance(item, dict)
                      for clause in (item.get("clause_ids") or [])})
    return {"overall_risk": report.get("overall_risk"), "warning_count": len(warnings),
            "clause_ids": clauses,
            "warnings_without_clause": sum(not (item.get("clause_ids") or [])
                                            for item in warnings if isinstance(item, dict)),
            "need_human_review": bool(report.get("need_human_review"))}


def _read_if_exists(path: Path) -> dict[str, Any]:
    return load_json(path) if path.is_file() else {}


def evaluate_pair(pair_id: str, pair_dir: Path, baseline_scene: dict[str, Any],
                  baseline_report: dict[str, Any], elapsed_s: float | None = None) -> dict[str, Any]:
    # Graph 是唯一生产状态格式；兼容读取旧 runner 产物，便于历史批次继续比较。
    state = (_read_if_exists(pair_dir / "graph_state.json") or
             _read_if_exists(pair_dir / "agent_state.json"))
    if "workflow" not in state:
        state["workflow"] = {
            "status": state.get("status", "not_started"),
            "need_human_review": state.get("need_human_review"),
            "error": state.get("error"),
            "completed_steps": state.get("steps", []),
        }
    l2 = _read_if_exists(pair_dir / "l2" / "l2_scene.json")
    l3 = _read_if_exists(pair_dir / "l3" / "l3_warnings.json")
    scene = l2.get("scenes", {}).get(pair_id, {})
    report = l3.get("warnings", {}).get(pair_id, {})
    live_summary = _warning_summary(report)
    baseline_summary = _warning_summary(baseline_report)
    review_reasons = []
    quality = state.get("quality", {})
    if quality and not quality.get("passed", False):
        review_reasons.append("fusion_quality_below_threshold_or_missing")
    evidence = state.get("evidence_check", {})
    scene_meta = state.get("scene", {}).get("_meta", {})
    if scene_meta.get("parsing_error") or not state.get("scene", {}).get("targets"):
        review_reasons.append("vlm_scene_invalid_or_missing_targets")
    if evidence.get("invalid_targets"):
        review_reasons.append("report_target_reference_invalid")
    if evidence.get("invalid_clauses"):
        review_reasons.append("report_clause_out_of_retrieval_scope")
    if evidence.get("missing_clause_warnings"):
        review_reasons.append("warning_without_clause_citation")
    if not state.get("detections", {}).get("objects"):
        review_reasons.append("no_detection_or_detection_step_incomplete")
    if state.get("report", {}).get("need_human_review"):
        review_reasons.append("llm_report_requests_human_review")
    if state.get("workflow", {}).get("status") == "failed":
        review_reasons.append("tool_or_workflow_failed")
    risk_comparable = live_summary["overall_risk"] is not None and baseline_summary["overall_risk"] is not None
    # golden 优先：有专家真值的样本，漂移类原因改由真值差异替代
    golden = golden_comparison(pair_id, live_summary["overall_risk"],
                               agent_clauses=live_summary["clause_ids"],
                               agent_disposition=state.get("disposition"))
    if golden:
        if golden.get("risk_match") is False:
            review_reasons.append("risk_level_differs_from_golden")
        if golden.get("risk_match") is True:
            for stale in ("risk_level_differs_from_baseline", "warning_count_differs_from_baseline",
                          "clause_set_differs_from_baseline"):
                if stale in review_reasons:
                    review_reasons.remove(stale)
    else:
        if risk_comparable and live_summary["overall_risk"] != baseline_summary["overall_risk"]:
            review_reasons.append("risk_level_differs_from_baseline")
    if report and live_summary["warning_count"] != baseline_summary["warning_count"] and not golden:
        review_reasons.append("warning_count_differs_from_baseline")
    if live_summary["clause_ids"] != baseline_summary["clause_ids"] and not golden:
        review_reasons.append("clause_set_differs_from_baseline")
    l2_stats, l3_stats = l2.get("stats", {}), l3.get("stats", {})
    if elapsed_s is None:
        elapsed_s = round(sum(call.get("elapsed_seconds", 0) or 0
                              for call in state.get("audit", {}).get("tool_calls", [])), 2) or None
    in_tokens = (l2_stats.get("tokens_in", 0) or 0) + (l3_stats.get("tokens_in", 0) or 0)
    out_tokens = (l2_stats.get("tokens_out", 0) or 0) + (l3_stats.get("tokens_out", 0) or 0)
    return {
        "pair_id": pair_id,
        "run_dir": str(pair_dir.resolve()),
        "status": state.get("workflow", {}).get("status", "not_started"),
        "need_human_review": state.get("workflow", {}).get("need_human_review"),
        "review_reasons": review_reasons,
        "completed_steps": state.get("workflow", {}).get("completed_steps", []),
        "error": state.get("workflow", {}).get("error"),
        "scene_present": bool(scene),
        "live": live_summary,
        "baseline": baseline_summary,
        "comparison": {
            "risk_comparable": risk_comparable,
            "risk_level_match": (live_summary["overall_risk"] == baseline_summary["overall_risk"]
                                 if risk_comparable else None),
            "warning_count_delta": (live_summary["warning_count"] - baseline_summary["warning_count"]
                                    if report else None),
            "clause_jaccard": _jaccard(set(live_summary["clause_ids"]),
                                        set(baseline_summary["clause_ids"])),
            "golden": golden,
        },
        "usage": {"tokens_in": in_tokens, "tokens_out": out_tokens},
        "elapsed_seconds": elapsed_s,
    }


def _jaccard(left: set[str], right: set[str]) -> float | None:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else None


def run_batch(*, pairs: list[str] | None = None, output_dir: str | Path | None = None,
              resume: bool = True, project: Path | None = None,
              pair_dir_overrides: dict[str, Path] | None = None) -> dict[str, Any]:
    root = (project or project_root()).resolve()
    cfg = load_config(root / "config" / "agent.yaml")
    detections_path = configured_path("detections", cfg)
    baseline_scenes = _read_if_exists(configured_path("scenes", cfg)).get("scenes", {})
    baseline_reports = _read_if_exists(configured_path("warnings", cfg)).get("warnings", {})
    candidates = sorted(Path(name).stem for name in
                       (root / "results" / "fused").glob("*.jpg"))
    selected = pairs if pairs is not None else candidates
    invalid = sorted(set(selected) - set(candidates))
    if invalid:
        raise ValueError(f"融合图不存在的 pair_id: {invalid}")
    out = Path(output_dir) if output_dir else root / "results" / "agent" / "batch_live" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "acceptance.json"
    prior = _read_if_exists(manifest_path) if resume else {}
    prior_rows = {row["pair_id"]: row for row in prior.get("pairs", []) if isinstance(row, dict)}
    planner_enabled = bool((cfg.get("runtime", {}) or {}).get("planner_enabled", False))
    app = build_graph(planner_gateway=True if planner_enabled else None)
    rows = []
    for index, pair_id in enumerate(selected, start=1):
        pair_dir = (pair_dir_overrides or {}).get(pair_id, out / pair_id)
        state_path = pair_dir / "graph_state.json"
        if resume and (state_path.is_file() or (pair_dir / "agent_state.json").is_file()):
            row = evaluate_pair(pair_id, pair_dir, baseline_scenes.get(pair_id, {}),
                                baseline_reports.get(pair_id, {}),
                                (None if pair_id in (pair_dir_overrides or {})
                                 else prior_rows.get(pair_id, {}).get("elapsed_seconds")))
            marker = "supplemental" if pair_id in (pair_dir_overrides or {}) else "resume"
            print(f"[{index}/{len(selected)}] {pair_id}: {marker} {row['status']}", flush=True)
            rows.append(row)
            continue
        started = time.perf_counter()
        print(f"[{index}/{len(selected)}] {pair_id}: live start", flush=True)
        try:
            state = app.invoke(
                {"task_id": f"batch-{pair_id}", "pair_id": pair_id,
                 "run_dir": str(pair_dir), "steps": [], "tool_calls": [],
                 "review_reasons": []},
                config={"configurable": {"thread_id": f"batch-{pair_id}"}})
            # node_finalize 已写入 graph_state.json；这里仅在提前异常收尾时保留快照。
            if not state_path.is_file():
                save_json(state_path, dict(state))
            elapsed = round(time.perf_counter() - started, 2)
            row = evaluate_pair(pair_id, pair_dir, baseline_scenes.get(pair_id, {}),
                                baseline_reports.get(pair_id, {}), elapsed)
            print(f"[{index}/{len(selected)}] {pair_id}: {row['status']} "
                  f"tokens={row['usage']['tokens_in'] + row['usage']['tokens_out']} "
                  f"elapsed={elapsed}s", flush=True)
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 2)
            row = {"pair_id": pair_id, "status": "failed", "error": str(exc),
                   "elapsed_seconds": elapsed, "usage": {"tokens_in": 0, "tokens_out": 0}}
            print(f"[{index}/{len(selected)}] {pair_id}: failed ({exc})", flush=True)
        rows.append(row)
        result = _aggregate(rows, selected, out, baseline_scenes_path=str(configured_path("scenes", cfg)),
                            baseline_reports_path=str(configured_path("warnings", cfg)))
        save_json(manifest_path, result)
    result = _aggregate(rows, selected, out, baseline_scenes_path=str(configured_path("scenes", cfg)),
                        baseline_reports_path=str(configured_path("warnings", cfg)))
    save_json(manifest_path, result)
    (out / "acceptance.md").write_text(render_markdown(result), encoding="utf-8")
    write_review_queue(result, out / "expert_review.csv")
    return result


def write_review_queue(result: dict[str, Any], target: Path) -> None:
    """Create a fill-in sheet; model/baseline outputs remain separate from expert labels."""
    fields = ["pair_id", "run_dir", "image_path", "agent_status", "review_reasons",
              "agent_risk", "baseline_risk", "agent_warning_count", "baseline_warning_count",
              "risk_level_match", "agent_clause_ids", "baseline_clause_ids",
              "expert_risk_label", "expert_warning_summary", "expert_clause_ids",
              "reviewer", "review_notes"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in result["pairs"]:
            writer.writerow({
                "pair_id": row["pair_id"], "run_dir": row.get("run_dir", ""),
                "image_path": str(Path(row.get("run_dir", "")) / "input" / f"{row['pair_id']}.jpg"),
                "agent_status": row.get("status", ""),
                "review_reasons": ";".join(row.get("review_reasons", [])),
                "agent_risk": row.get("live", {}).get("overall_risk", ""),
                "baseline_risk": row.get("baseline", {}).get("overall_risk", ""),
                "agent_warning_count": row.get("live", {}).get("warning_count", ""),
                "baseline_warning_count": row.get("baseline", {}).get("warning_count", ""),
                "risk_level_match": row.get("comparison", {}).get("risk_level_match", ""),
                "agent_clause_ids": ";".join(row.get("live", {}).get("clause_ids", [])),
                "baseline_clause_ids": ";".join(row.get("baseline", {}).get("clause_ids", [])),
                "expert_risk_label": "", "expert_warning_summary": "",
                "expert_clause_ids": "", "reviewer": "", "review_notes": "",
            })


def render_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# 阶段二：30 对 live Agent 验收报告", "",
        f"- 验收判定：工作流执行 **{result['verdict']['workflow_execution']}**；结果一致性 **{result['verdict']['baseline_concordance']}**",
        f"- 完成样本：{summary['finished']}/{summary['requested']}",
        f"- 工作流：自动完成 {summary['completed']}，需人工复核 {summary['needs_human_review']}，执行失败 {summary['failed']}",
        f"- 风险等级可比：{summary['risk_comparable']}；一致 {summary['risk_level_match']}；不一致 {summary['risk_level_mismatch']}；一致率 {summary['risk_agreement_rate_comparable']}",
        f"- 风险不可比：{summary['risk_not_comparable']}；警示数量完全一致：{summary['warning_count_exact_match']}",
        f"- 条款 Jaccard 平均值：{summary['mean_clause_jaccard']}",
        f"- Token：输入 {summary['total_tokens_in']}，输出 {summary['total_tokens_out']}；总耗时 {summary['total_elapsed_seconds']} 秒",
        f"- 真值对照（golden set）：覆盖 {summary['golden']['covered']}，准确率 {summary['golden']['accuracy']}"
        f"（命中 {summary['golden']['matched']}/{summary['golden']['comparable']}）",
        "", "## 需要专家优先复核的差异样本", "",
        "| 图对 | Agent 风险 | 基线风险 | Agent/基线警示数 | 条款重合度 | 转人工原因 |",
        "|---|---|---|---:|---:|---|",
    ]
    differing = [row for row in result["pairs"]
                 if row.get("comparison", {}).get("risk_level_match") is not True
                 or row.get("comparison", {}).get("warning_count_delta") not in (0, None)
                 or row.get("comparison", {}).get("clause_jaccard", 1) < 1]
    for row in differing:
        live, base, comp = row["live"], row["baseline"], row["comparison"]
        reasons = ", ".join(row.get("review_reasons", [])) or "需人工核对基线差异"
        lines.append(f"| {row['pair_id']} | {live['overall_risk'] or '无报告'} | {base['overall_risk'] or '无'} | "
                     f"{live['warning_count']}/{base['warning_count']} | {comp['clause_jaccard']} | {reasons} |")
    lines.extend(["", "## 人工复核原因统计", ""])
    for reason, count in result.get("review_reason_counts", {}).items():
        lines.append(f"- `{reason}`：{count} 对")
    lines.extend(["", "## 解释与限制", ""])
    lines.extend(f"- {item}" for item in result.get("limitations", []))
    lines.extend(["- 此报告仅表示流程和基线对照结果，不构成施工安全合规结论。", ""])
    return "\n".join(lines)


def _aggregate(rows: list[dict[str, Any]], selected: list[str], out: Path,
               baseline_scenes_path: str, baseline_reports_path: str) -> dict[str, Any]:
    comparable = [row for row in rows if row.get("comparison", {}).get("risk_comparable")]
    clause_scores = [row["comparison"]["clause_jaccard"] for row in rows
                     if row.get("comparison", {}).get("clause_jaccard") is not None]
    result = {
        "audit_type": "live_agent_batch_acceptance",
        "verdict": {
            "workflow_execution": "pass" if len(rows) == len(selected) and
            not any(row.get("status") == "failed" for row in rows) else "incomplete",
            "baseline_concordance": "expert_review_required",
            "reason": "历史模型输出不是人工真值；当前存在风险等级、警示数量和条款差异。",
        },
        "project_root": str(project_root()),
        "output_dir": str(out.resolve()),
        "supplemental_pairs": [row["pair_id"] for row in rows
                               if row.get("run_dir") and
                               Path(row["run_dir"]).resolve() != (out / row["pair_id"]).resolve()],
        "baseline": {"scenes": baseline_scenes_path, "reports": baseline_reports_path},
        "requested_pairs": selected,
        "summary": {
            "requested": len(selected), "finished": len(rows),
            "completed": sum(row.get("status") == "completed" for row in rows),
            "needs_human_review": sum(bool(row.get("need_human_review")) for row in rows),
            "failed": sum(row.get("status") == "failed" for row in rows),
            "risk_comparable": sum(bool(row.get("comparison", {}).get("risk_comparable")) for row in rows),
            "risk_level_match": sum(row.get("comparison", {}).get("risk_level_match") is True for row in rows),
            "risk_level_mismatch": sum(row.get("comparison", {}).get("risk_level_match") is False for row in rows),
            "risk_not_comparable": sum(row.get("comparison", {}).get("risk_level_match") is None for row in rows),
            "total_tokens_in": sum(row.get("usage", {}).get("tokens_in", 0) for row in rows),
            "total_tokens_out": sum(row.get("usage", {}).get("tokens_out", 0) for row in rows),
            "total_elapsed_seconds": round(sum(row.get("elapsed_seconds") or 0 for row in rows), 2),
            "warning_count_exact_match": sum(
                row.get("comparison", {}).get("warning_count_delta") == 0 for row in rows
                if row.get("comparison", {}).get("warning_count_delta") is not None),
            "mean_clause_jaccard": round(statistics.mean(clause_scores), 3) if clause_scores else None,
        },
        "pairs": rows,
    }
    result["summary"]["risk_agreement_rate_comparable"] = (
        round(result["summary"]["risk_level_match"] / len(comparable), 3) if comparable else None)
    # 真值（golden set）对照统计：有专家标签的样本才是准确率，其余仍是漂移指标
    goldens = [row for row in rows if row.get("comparison", {}).get("golden")]
    g_match = sum(row["comparison"]["golden"].get("action_match") is True for row in goldens)
    g_cmp = sum(row["comparison"]["golden"].get("action_match") is not None for row in goldens)
    result["summary"]["golden"] = {
        "covered": len(goldens), "comparable": g_cmp,
        "matched": g_match,
        "accuracy": round(g_match / g_cmp, 3) if g_cmp else None,
        "under_severe": [row["pair_id"] for row in goldens
                         if row["comparison"]["golden"].get("action_match") is False],
    }
    if g_cmp:
        result["verdict"] = {
            "workflow_execution": result["verdict"]["workflow_execution"],
            "baseline_concordance": "golden_set_evaluated",
            "reason": f"真值准确率 {result['summary']['golden']['accuracy']}"
                      f"（{g_match}/{g_cmp}，专家标注对照）；未标注样本仍为基线漂移指标。",
        }
    result["review_reason_counts"] = {
        reason: sum(reason in row.get("review_reasons", []) for row in rows)
        for reason in sorted({reason for row in rows for reason in row.get("review_reasons", [])})
    }
    result["limitations"] = [
        "与历史模型输出的一致性不是人工真值准确率；有风险等级或条款差异的样本需要专家核验。",
        "API 费用未估算，记录了模型返回的 token 数；实际费用依供应商账单和当时单价。",
        "pair05 使用统一 API Gateway 完成独立补测并纳入汇总；其余 29 对为同模型/提示词下的首轮批测，早于 Gateway 代码收敛。",
    ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="逐对运行 live Agent 并与基线对照；会调用模型 API")
    parser.add_argument("--pairs", default="", help="逗号分隔 pair ID；默认全部融合图")
    parser.add_argument("--out", default="", help="独立输出目录；默认新建带时间戳目录")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有状态并重跑（会重复调用 API）")
    parser.add_argument("--pair-override", action="append", default=[], metavar="PAIR=DIR",
                        help="使用独立补测目录替换某对的结果，不重新调用 API")
    args = parser.parse_args()
    overrides = {}
    for item in args.pair_override:
        if "=" not in item:
            parser.error("--pair-override 格式必须为 pair_id=目录")
        pair_id, directory = item.split("=", 1)
        overrides[pair_id.strip()] = Path(directory)
    result = run_batch(pairs=[x.strip() for x in args.pairs.split(",") if x.strip()] or None,
                       output_dir=args.out or None, resume=not args.no_resume,
                       pair_dir_overrides=overrides)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"acceptance={Path(result['output_dir']) / 'acceptance.json'}")
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
