# -*- coding: utf-8 -*-
"""判断结果三分类与失败归因（2026-09-29 用户定稿）。

终态语义（依据 docs/不可判定性成因与终态三分类设计.md）：

  判断成功 judgement_success —— 得到确定等级（安全/低风险/高风险）→ 执行对应处置，自主结案；
  判断失败 judgement_failed  —— 判不了，但**失败原因可定位**（感知链环节失败 / 证据缺口 /
                                双源分歧未裁决 / 模型自评判不了）→ 自主结案 + 抽样复核，不等人工；
  无法判断 undecidable       —— 既无等级、又定位不到原因（不变量被破坏 / 埋点缺失）
                                → **唯一**转人工的情形：人来是要看"系统为什么不正常"，
                                  不是看"这张图危不危险"。

设计约束（重要）：
  归因只允许两种结果——给出 reason code，或明确承认"没有任何原因记录"。
  后者是系统自检失败的信号，必须响亮暴露，不得被当作业务常态。
  因此终态三分类的前置条件，是每条"判不出"的路径都要留下 reason code。
"""
from __future__ import annotations

from typing import Any

SUCCESS = "judgement_success"
FAILED = "judgement_failed"
UNDECIDABLE = "undecidable"

VALID_RISKS = ("安全", "低风险", "高风险")

# 严重度序（全仓唯一版：planner/acceptance_report/review_loop 曾各自定义同图）
RISK_RANK = {"安全": 0, "低风险": 1, "高风险": 2}


def normalize_risk(value: Any) -> str | None:
    """把带处置后缀的标签归一为三档风险等级。"""
    text = str(value or "").strip()
    for risk in VALID_RISKS:
        if text.startswith(risk):
            return risk
    return None

# reason code → 人类可读原因
REASON_TABLE: dict[str, str] = {
    # ---- A 类：感知链环节失败（代码侧可观测） ----
    "fusion_qc_failed": "配准质量未达硬底线",
    "below_hard_floor": "配准质量低于硬底线",
    "missing_qc": "缺少配准质量记录",
    "config_unavailable": "阈值配置不可用（拒绝静默使用未记录的默认值）",
    "vlm_unavailable": "VLM 服务不可用或未产出有效场景",
    "parsing_error": "VLM 输出非法（结构校验未通过）",
    "reconcile_failed": "对账输入缺失或对账失败",
    "llm_tool_failed": "判级工具失败",
    "tool_failed": "工具调用失败",
    # ---- B 类：证据缺口 / 双源分歧未裁决 ----
    "work_type_unknown": "作业类型无法判定",
    "zone_uncertain": "危险区设备运行状态不确定",
    "evidence_gap": "关键防护/状态字段存在证据缺口",
    "vlm_only_no_yolo_support": "双源不一致：VLM 目标无 GDYOLO 支撑",
    "vlm_only_all": "双源不一致：全部目标均未获 GDYOLO 证实",
    "yolo_only_unresolved": "双源不一致：GDYOLO 目标经追问仍未获 VLM 确认",
    "supplement_rejected": "双源不一致：GDYOLO 目标经定向追问后被剔除",
    "equipment_state_mismatch": "设备运行状态与几何证据冲突",
    "unadjudicated_conflict": "存在未裁决的感知分歧（未做定向重感知复核）",
    "vlm_scene_untrusted": "VLM 场景不可信（降级或解析失败）",
    # ---- C 类：模型自评 ----
    "model_undecidable": "判级模型自评判无法研判（已声明理由）",
    # ---- 不可信态：有原因也必须人工 ----
    "state_save_failed": "状态落盘失败（结果完整性受损）",
}

# 结果完整性受损 → 即便能归因也不得自主结案，必须人工
FORCE_MANUAL = {"state_save_failed", "reconcile_failed"}

# 审计标记 / 复核理由 → reason code
_REASON_FROM_MARK = {
    "fusion_qc_failed": "fusion_qc_failed",
    "vlm_unavailable": "vlm_unavailable",
    "reconcile_failed": "reconcile_failed",
    "llm_tool_failed": "llm_tool_failed",
    "llm_requested": "model_undecidable",
    "llm_requested_human_review": "model_undecidable",
    "planner_requested_human": "model_undecidable",
    "clause_support_missing": "evidence_gap",
    "evidence_gate_failed": "evidence_gap",
    "warning_without_clause": "evidence_gap",
    "clause_out_of_scope": "evidence_gap",
    "cited_unverified_target": "vlm_only_no_yolo_support",
    "vlm_only_no_yolo_support": "vlm_only_no_yolo_support",
    "vlm_only_all": "vlm_only_all",
    "yolo_only_unresolved": "yolo_only_unresolved",
    "supplement_rejected": "supplement_rejected",
    "equipment_state_mismatch": "equipment_state_mismatch",
    "unadjudicated_conflict": "unadjudicated_conflict",
    "vlm_scene_untrusted": "vlm_scene_untrusted",
    "state_save_failed": "state_save_failed",
    "no_detection_or_detection_step_incomplete": "vlm_unavailable",
}

# 工具名 → reason code
_TOOL_CODE = {
    "fuse_qc": "fusion_qc_failed",
    "vlm_open_detect": "vlm_unavailable",
    "reconcile": "reconcile_failed",
    "generate_judgement": "llm_tool_failed",
}


def _mk(outcome: str, code: str | None, detail: str = "") -> dict[str, Any]:
    return {"outcome": outcome,
            "reason_code": code,
            "reason": REASON_TABLE.get(code, code) if code else None,
            "detail": detail or ""}


def _collect_marks(state: dict[str, Any]) -> list[tuple[str, str]]:
    """按"最具体优先"顺序收集可定位的失败标记；无标记 ⇒ 无法定位。"""
    marks: list[tuple[str, str]] = []

    def add(code: str | None, detail: str = "") -> None:
        if code and all(c != code for c, _ in marks):
            marks.append((code, detail))

    # 1) 感知链显式标记（只认"未通过"；borderline_qc 属通过态，不算失败标记）
    quality = state.get("quality") or {}
    flag = str(quality.get("flag") or "")
    if quality and quality.get("passed") is False:
        add(flag if flag in REASON_TABLE else "fusion_qc_failed",
            f"quality.flag={flag}, edge_align={quality.get('edge_align')}")
    scene_meta = (state.get("scene") or {}).get("_meta") or {}
    if scene_meta.get("parsing_error"):
        add("parsing_error", str(scene_meta.get("validate_error") or "")[:200])
    if scene_meta.get("degraded"):
        add("vlm_unavailable", f"scene._meta.semantic={scene_meta.get('semantic')}")
    for conflict in (state.get("reconciliation") or {}).get("conflicts") or []:
        add(str(conflict), "reconciliation.conflicts")

    # 2) 规则引擎（确定性事实推出的证据缺口）
    rule = state.get("rule") or {}
    for driver in rule.get("drivers") or []:
        if str(driver).startswith("R-WORK-UNKNOWN"):
            add("work_type_unknown", str(driver)[:160])
        elif str(driver).startswith("R-ZONE-UNCERTAIN"):
            add("zone_uncertain", str(driver)[:160])
    if rule.get("evidence_gaps"):
        add("evidence_gap", "; ".join(str(g) for g in rule["evidence_gaps"])[:200])

    # 3) 审计标记 / 复核理由
    for mark in list(state.get("review_reasons") or []) + list(state.get("anomalies") or []):
        code = _REASON_FROM_MARK.get(str(mark))
        add(code, str(mark))

    # 4) 工具失败兜底：任何带 error 的失败都算可定位
    for call in state.get("tool_calls") or []:
        if not isinstance(call, dict) or call.get("status") == "ok":
            continue
        if call.get("error"):
            add(_TOOL_CODE.get(str(call.get("tool")), "tool_failed"),
                f"{call.get('tool')}: {call.get('error')}"[:200])

    return marks


def locate_failure_reason(state: dict[str, Any]) -> tuple[str | None, str]:
    """把"判不出等级"归因到可定位的 reason code；`(None, "")` 表示无法定位。"""
    marks = _collect_marks(state)
    return marks[0] if marks else (None, "")


def classify_outcome(state: dict[str, Any]) -> dict[str, Any]:
    """判定终态三分类：success / failed / undecidable。"""
    risk = (state.get("risk_assessment") or {}).get("overall_risk")
    marks = _collect_marks(state)
    forced = next((m for m in marks if m[0] in FORCE_MANUAL), None)

    if forced:
        # 结果完整性受损：无论有无等级都不可自主结案
        return _mk(UNDECIDABLE, forced[0], forced[1])
    if risk in VALID_RISKS:
        return _mk(SUCCESS, None, "")
    if marks:
        return _mk(FAILED, marks[0][0], marks[0][1])
    return _mk(UNDECIDABLE, None,
               "无等级且无任何原因记录（不变量被破坏或埋点缺失）")


def disposition_for_outcome(outcome: str, risk: str | None) -> str:
    """终态三分类 → 处置通道（与 runner.classify_disposition 同一套词汇）。"""
    if outcome == SUCCESS and risk in VALID_RISKS:
        return {"高风险": "P1_auto_alert",
                "低风险": "TIP_optional",
                "安全": "P4_archive"}[risk]
    # 判断失败和无法判断都属于失败终态，统一转人工处置。
    return "P3_manual_judge"
