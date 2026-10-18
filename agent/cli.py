# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
from pathlib import Path

from .runner import SafetyAgent, save_state
from .graph import run_task


def main() -> int:
    parser = argparse.ArgumentParser(description="施工安全单 Agent")
    parser.add_argument("--task-id", default="replay-001")
    parser.add_argument("--out", default="")
    parser.add_argument("--mode", choices=["replay", "live"], default="replay")
    parser.add_argument("--pair-id", default="", help="live 模式必填，例如 pair04")
    # 处置路由内置三级语义（2026-09-29）：安全→归档；判不了→人工；低风险→tip；高风险→完整条款链路
    args = parser.parse_args()
    if args.mode == "live":
        if not args.pair_id:
            parser.error("live 模式需要 --pair-id")
        # live 统一走 Graph；runner 仅保留 replay/历史兼容职责。
        state = run_task(pair_id=args.pair_id, task_id=args.task_id)
    else:
        agent = SafetyAgent()
        state = agent.run_replay(task_id=args.task_id)
    if args.mode == "live":
        target = str(Path(state.get("run_dir", "")) / "graph_state.json")
        status = state.get("status", "completed")
        steps = state.get("steps", [])
        human_review = state.get("need_human_review", False)
    else:
        target = save_state(state, None)
        status = state["workflow"]["status"]
        steps = state["workflow"]["completed_steps"]
        human_review = state["workflow"]["need_human_review"]
    print(f"status={status}")
    print(f"steps={','.join(steps)}")
    print(f"human_review={human_review}")
    print(f"state={target}")
    # live 模式 state 是 Graph 扁平 dict（无 workflow 层），status 已在上面按模式取好
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
