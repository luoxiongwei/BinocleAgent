# -*- coding: utf-8 -*-
"""LangGraph 版 Agent 的图状态声明（framework_benchmark 行动项②：状态字段代码级收敛）。

字段与 schemas/task.schema.json 同源；节点只通过 state patch 读写（纯函数约定），
steps/tool_calls 用 reducer 追加，避免手写去重合并。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class GraphState(TypedDict, total=False):
    # ---- 输入 ----
    task_id: str
    pair_id: str
    run_dir: str                      # 本任务的产物目录（工具读写契约的根）

    # ---- 中间产物 ----
    quality: dict[str, Any]           # {passed, edge_align, threshold, hard_min, flag}
    scene: dict[str, Any]             # L2 场景 JSON（VLM 先行开放检测 + 对账后）
    detections: dict[str, Any]        # {image_id, objects:[GDYOLO 框]}
    reconciliation: dict[str, Any]    # reconcile 摘要 + supplement
    equipment_check: dict[str, Any]   # verify_equipment_state 几何校验输出
    rule: dict[str, Any]              # 规则引擎三要素判定
    route: str                        # archive | manual | tip | full
    report: dict[str, Any]            # L3 警示/提示 JSON（单对）
    evidence_check: dict[str, Any]

    # ---- 处置 ----
    disposition: str                  # P1_auto_alert/P4_archive/TIP_optional/P3_manual_judge
    risk_assessment: dict[str, Any]   # {overall_risk(None=未判定), n_warnings, need_human_review}
    judgement: dict[str, Any]         # 三分类终态 {outcome, reason_code, reason, detail}
    sample_check: bool                # 判断失败/静默归档 → 抽样复核队列（不阻塞流程）
    status: str                       # completed | needs_human_review | failed
    review_reasons: Annotated[list[str], operator.add]   # 转人工理由（判不了的原因）
    anomalies: Annotated[list[str], operator.add]        # 审计标记：证据链/配准/对账异常，不改变处置
    need_human_review: bool
    human_archive: dict[str, Any]
    human_verdict: dict[str, Any]     # interrupt 恢复输入 {verdict, risk_label, notes}

    # ---- 审计 ----
    steps: Annotated[list[str], operator.add]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    retry_count: int
    error: str
