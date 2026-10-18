# -*- coding: utf-8 -*-
"""全量图批跑（LangGraph + 规划器）：30 对顺序执行，断点续跑，产出验收对照数据。

用法:
  python -m agent.run_all                          # pair01..pair30
  python -m agent.run_all --pairs pair01,pair16    # 指定子集
  python -m agent.run_all --planner false          # 关闭规划器（对照实验）

断点续跑: run_dir/graph_state.json 存在且 status=completed 的对自动跳过，
中断后重跑同一命令即可续。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.graph import build_graph  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="全量图批跑（验收用）")
    ap.add_argument("--out-root", default=str(PROJECT / "results" / "agent" / "full_0929"))
    ap.add_argument("--pairs", default="all", help="all 或逗号分隔，如 pair01,pair16")
    ap.add_argument("--planner", default="true", choices=["true", "false"])
    args = ap.parse_args()

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    pairs = ([f"pair{i:02d}" for i in range(1, 31)] if args.pairs == "all"
             else [p.strip() for p in args.pairs.split(",") if p.strip()])
    planner = args.planner == "true"
    app = build_graph(planner_gateway=(True if planner else None))

    summary_path = out_root / "summary.json"
    summary: list[dict] = []
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = []
    done = {s["pair_id"] for s in summary if s.get("status") == "completed" or s.get("skipped")}

    for pid in pairs:
        if pid in done:
            print(f"[skip] {pid} 已有终态", flush=True)
            continue
        task_id = f"full-{pid}"
        run_dir = out_root / task_id
        t0 = time.perf_counter()
        try:
            final = app.invoke(
                {"task_id": task_id, "pair_id": pid, "run_dir": str(run_dir),
                 "steps": [], "tool_calls": [], "review_reasons": []},
                config={"configurable": {"thread_id": task_id}})
            elapsed = round(time.perf_counter() - t0, 1)
            ra = final.get("risk_assessment") or {}
            row = {"pair_id": pid, "status": final.get("status"),
                   "route": final.get("route"), "disposition": final.get("disposition"),
                   "overall_risk": ra.get("overall_risk"),
                   "n_warnings": ra.get("n_warnings"),
                   "need_human_review": final.get("need_human_review"),
                   "review_reasons": final.get("review_reasons") or [],
                   "elapsed_s": elapsed, "skipped": False}
        except Exception as exc:  # 单对失败不阻塞批次
            elapsed = round(time.perf_counter() - t0, 1)
            row = {"pair_id": pid, "status": "failed", "error": str(exc)[:300],
                   "elapsed_s": elapsed, "skipped": False}
            print(f"[fail] {pid}: {exc}", flush=True)
        summary = [s for s in summary if s["pair_id"] != pid]
        summary.append(row)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        print(f"[done] {pid} status={row.get('status')} disp={row.get('disposition')} "
              f"risk={row.get('overall_risk')} {elapsed}s", flush=True)

    print(f"[all] 批次结束，summary -> {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
