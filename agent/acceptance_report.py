# -*- coding: utf-8 -*-
"""验收报告生成器：全量批跑 summary.json × golden_set.json → report.md + 控制台对照表。

处置映射（2026-09-29 用户语义）:
  安全-记录     ↔ P4_archive (risk=安全)
  低风险-可选提示 ↔ TIP_optional (risk=低风险)
  高风险-强制告警 ↔ P1_auto_alert (risk=高风险)
  转人工        ↔ P3_manual_judge / P2_urgent_human / P3_routine_review

用法: python -m agent.acceptance_report --summary <summary.json> [--out report.md]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT = Path(__file__).resolve().parents[1]

from agent.outcome import RISK_RANK, normalize_risk


def golden_action(entry: dict) -> str:
    return entry.get("expert_risk", "")


def system_action(row: dict) -> str:
    disp = row.get("disposition") or ""
    if disp == "P4_archive":
        return "安全-记录"
    if disp == "TIP_optional":
        return "低风险-可选提示"
    if disp == "P1_auto_alert":
        return "高风险-强制告警"
    if disp in ("P3_manual_judge", "P2_urgent_human", "P3_routine_review"):
        return "转人工"
    return disp or "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default=str(PROJECT / "results" / "agent" / "full_0929" / "summary.json"))
    ap.add_argument("--golden", default=str(PROJECT / "results" / "golden_set" / "golden_set.json"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    gmap = {e["pair_id"]: e for e in golden["entries"]}

    # 只统计本批次实际运行的 pair（summary 未包含 = 未参与本批，不计入 ERR）
    rows, exact, tier_ok, failed = [], 0, 0, 0
    batch_ids = [s["pair_id"] for s in summary]
    for pid in sorted(batch_ids, key=lambda x: int(x[4:]) if x[4:].isdigit() else 999):
        if pid not in gmap:
            continue
        g, row = gmap[pid], next((s for s in summary if s["pair_id"] == pid), {})
        sys_act = system_action(row)
        g_act = golden_action(g)
        risk = normalize_risk(row.get("overall_risk"))
        g_std = normalize_risk(g.get("risk_std") or g.get("expert_risk"))
        if row.get("status") == "failed" or not row:
            mark, failed = "ERR", failed + 1
        elif sys_act == ({"安全": "安全-记录", "低风险": "低风险-可选提示", "高风险": "高风险-强制告警"}.get(g_std) or "转人工"):
            mark, exact = "V", exact + 1
        elif g_std and risk in RISK_RANK and g_std in RISK_RANK and RISK_RANK[risk] == RISK_RANK[g_std]:
            mark, tier_ok = "O", tier_ok + 1          # 通道不同但等级同档
        else:
            mark = "X"
        rows.append((pid, g_act, sys_act, risk or "-", mark,
                     row.get("elapsed_s"), row.get("review_reasons") or []))

    n = len(rows)
    # 判重/判轻只在"双方都判断成功"时可比（2026-09-29 用户定稿）：
    # 转人工 = 判断失败，不是"最低档"，不得参与严重度比较。
    over = [r[0] for r in rows if r[4] in ("X", "O")
            and _sev_of(r[2]) is not None and _sev_of(r[1]) is not None
            and _sev_of(r[2]) > _sev_of(r[1])]
    under = [r[0] for r in rows if r[4] in ("X", "O")
             and _sev_of(r[2]) is not None and _sev_of(r[1]) is not None
             and _sev_of(r[2]) < _sev_of(r[1])]
    unjudged = [r[0] for r in rows
                if r[4] not in ("ERR",) and (r[2] == "转人工" or r[1] == "转人工")]
    elapsed = [r[5] for r in rows if isinstance(r[5], (int, float))]
    disps: dict[str, int] = {}
    for r in rows:
        disps[r[2]] = disps.get(r[2], 0) + 1

    lines = ["# 全量验收报告（LangGraph + 规划器）", "",
             f"- 样本: {n} 对 | 处置通道一致 {exact} | 同档异通道 {tier_ok} | 不一致 {n - exact - tier_ok - failed} | 失败 {failed}",
             f"- 一致率（通道精确）: {exact}/{n} = {exact / n:.0%}",
             f"- 等级一致率（含同档异通道）: {(exact + tier_ok)}/{n} = {(exact + tier_ok) / n:.0%}",
             f"- 判重（系统>专家，仅「双方判断成功」时可比）: {','.join(over) or '无'}",
             f"- 判轻（系统<专家，仅「双方判断成功」时可比）: {','.join(under) or '无'}",
             f"- 判断失败（转人工，不参与判重/判轻）: {len(unjudged)} 对",
             f"- 处置分布: {disps}",
             f"- 单对耗时: 平均 {statistics.mean(elapsed):.0f}s / 最大 {max(elapsed):.0f}s" if elapsed else "",
             "", "| pair | 专家(新三级) | 系统 | 系统risk | 判 | 耗时s | 审计/复核标记（非转人工理由） |",
             "|---|---|---|---|---|---|---|"]
    for pid, g_act, s_act, risk, mark, el, reasons in rows:
        lines.append(f"| {pid} | {g_act} | {s_act} | {risk} | {mark} | {el or '-'} | "
                     f"{';'.join(reasons) or '-'} |")

    report = "\n".join(lines) + "\n"
    out = Path(args.out) if args.out else Path(args.summary).parent / "acceptance_report.md"
    out.write_text(report, encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[report] -> {out}")
    return 0


def _sev_of(action: str) -> int | None:
    """严重度只对"判断成功"的三档定义；转人工=判断失败，不参与比较（返回 None）。

    2026-09-29 用户定稿：判断结果只有成功（安全/低风险/高风险）与失败（转人工）
    二值。转人工不是"最低档"——此前把 -1 代入比较，导致所有转人工样本被误判为"判轻"。
    """
    return {"安全-记录": 0, "低风险-可选提示": 1, "高风险-强制告警": 2}.get(action)


if __name__ == "__main__":
    raise SystemExit(main())
