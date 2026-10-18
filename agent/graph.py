# -*- coding: utf-8 -*-
"""LangGraph 版安全巡检 Agent（骨架，2026-09-29 工程化第一步）。

范式对照（docs/framework_benchmark.md）：
  - 节点 = 纯函数 (state)->patch；steps/tool_calls 用 reducer 追加；
  - 路由 = 命名条件边函数（route_after_qc / route_after_judge / route_after_act），可单测；
  - 人工恢复 = 转人工为终态（登记待审队列，图不挂起等待），专家 verdict 经服务层异步回写；
  - 持久化 = SqliteSaver checkpointer（thread_id=task_id），断点续跑。

判级语义（2026-09-29 用户定稿，全链路统一）：
  - 规则引擎/LLM 给出确定等级 → 一律执行对应处置：高风险无条件 P1_auto_alert、
    低风险 TIP_optional、安全 P4_archive；证据链/配准异常只记审计标记（anomalies），不降级处置；
  - 判不了（无任何等级）→ 纯转人工 P3_manual_judge，且不输出任何风险等级；
  - 禁止"判了等级又转人工"的矛盾态（P2_urgent_human / P3_routine_review 已删除）。

工具经 agent/tools_registry.REGISTRY 调用（测试可注入 fake）。
Step-2 规划器将替换 perceive→…→judge 的固定序列为有界 ReAct 自主决策。
"""
from __future__ import annotations

import json
import cv2
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from agent.graph_state import GraphState
from agent.outcome import FAILED, VALID_RISKS, classify_outcome, disposition_for_outcome
from agent.planner import _open_conflicts as open_conflicts
from agent.planner import run_planning
from agent.tools_registry import call as default_call
from tools.common import project_root, save_json

PROJECT = project_root()
PENDING_PATH = PROJECT / "results" / "agent" / "pending_reviews.json"
HUMAN_ARCHIVE_PATH = PROJECT / "results" / "agent" / "human_archives.json"


# ---------- 待审队列（转人工=任务已完成的终态之一，verdict 异步回写） ----------

def _record_work_type_memory(state: dict[str, Any]) -> dict[str, Any] | None:
    scene = state.get("scene") or {}
    summary = scene.get("scene") or {}
    if not (summary.get("dominant_state") or summary.get("site_type")):
        return None
    path = PROJECT / "results" / "agent" / "work_type_memory.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, json.JSONDecodeError):
        records = []
    record = {"task_id": state.get("task_id"), "dominant_state": summary.get("dominant_state"),
              "site_type": summary.get("site_type"),
              "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    records = [item for item in records if item.get("task_id") != record["task_id"]]
    records.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, records)
    return record


def register_pending_review(values: dict[str, Any]) -> None:
    """登记待审条目（按 task_id 幂等）。

    2026-09-29 修订（终态三分类）：
      kind="manual" —— 无法判断（原因不可定位）→ **阻塞性**人工介入；
      kind="sample" —— 判断失败已自主结案 / 静默归档 → **抽样复核**队列，不阻塞流程。
    """
    # 2026-10-17 用户实测（graph-pair44）：转人工任务只进 human_archives 档案时，
    # 复核台队列（读 pending_reviews）完全看不到——用户只剩"按任务 ID 打开"这一隐藏
    # 入口，总览台待复核 KPI 也不刷新。转人工任务恰恰最需要人工复核入口。
    # 修正：P3_manual_judge 按本函数 docstring 的既有口径以 kind="manual"（阻塞性）入队；
    # human_archives 档案照常双写留痕（finalize 中 register_human_archive 不变）。
    if values.get("disposition") == "P3_manual_judge":
        kind = "manual"
    elif values.get("sample_check"):
        kind = "sample"
    else:
        kind = None
    if kind is None:
        return
    try:
        entries = json.loads(PENDING_PATH.read_text(encoding="utf-8")) if PENDING_PATH.is_file() else []
    except (OSError, json.JSONDecodeError):
        entries = []
    tid = values.get("task_id")
    if any(e.get("task_id") == tid for e in entries):
        return
    judgement = values.get("judgement") or {}
    entries.append({"task_id": tid, "pair_id": values.get("pair_id"), "kind": kind,
                    "overall_risk": (values.get("risk_assessment") or {}).get("overall_risk"),
                    "disposition": values.get("disposition"),
                    "outcome": judgement.get("outcome"),
                    "failure_reason": judgement.get("reason"),
                    "failure_reason_code": judgement.get("reason_code"),
                    "review_reasons": values.get("review_reasons") or [],
                    "anomalies": values.get("anomalies") or [],
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "resolved": False})
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_json(PENDING_PATH, entries)


def mark_review_resolved(task_id: str) -> bool:
    try:
        entries = json.loads(PENDING_PATH.read_text(encoding="utf-8")) if PENDING_PATH.is_file() else []
    except (OSError, json.JSONDecodeError):
        return False
    hit = False
    for e in entries:
        if e.get("task_id") == task_id and not e.get("resolved"):
            e["resolved"] = True
            e["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            hit = True
    if hit:
        save_json(PENDING_PATH, entries)
    return hit


def register_human_archive(values: dict[str, Any]) -> None:
    """记录离线转人工归档，不进入实时待审队列。"""
    path = HUMAN_ARCHIVE_PATH
    try:
        entries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, json.JSONDecodeError):
        entries = []
    tid = values.get("task_id")
    if any(item.get("task_id") == tid for item in entries):
        return
    judgement = values.get("judgement") or {}
    entries.append({"task_id": tid, "pair_id": values.get("pair_id"),
                    "disposition": values.get("disposition"),
                    "outcome": judgement.get("outcome"),
                    "failure_reason_code": judgement.get("reason_code"),
                    "failure_reason": judgement.get("reason"),
                    "review_reasons": values.get("review_reasons") or [],
                    "anomalies": values.get("anomalies") or [],
                    "archived": True,
                    "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, entries)


def apply_verdict(values: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    """专家 verdict 回写到已完成任务（服务层 review 调用；图不再挂起等待）。

    语义（2026-09-29 用户定稿）：安全-记录 / 低风险-提示 / 高风险-告警 / 转人工-记录
    四种都是任务终态，回写只修正结论与处置，不改变"任务已完成"的事实。
    转人工(P3_manual_judge)的回写必须携带合法 risk_label 才算完成判级。
    """
    verdict = verdict or {}
    action = verdict.get("verdict", "接受")
    label = verdict.get("risk_label")
    report = dict(values.get("report") or {})
    meta = dict(report.get("_meta") or {})
    meta["human_review"] = {"verdict": action, "risk_label": label,
                            "notes": verdict.get("notes"),
                            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    risk = report.get("overall_risk")
    was_manual = values.get("disposition") == "P3_manual_judge"
    if label in VALID_RISKS and (action == "修改" or was_manual):
        risk = label
    elif action == "驳回":
        risk = "安全"
    report["overall_risk"] = risk
    report["_meta"] = meta
    if action == "驳回" or risk == "安全":
        disposition = "P4_archive"
    elif risk == "高风险":
        disposition = "P1_auto_alert"
    elif risk == "低风险":
        disposition = "TIP_optional"
    else:
        disposition = values.get("disposition") or "P4_archive"
    ra = dict(values.get("risk_assessment") or {})
    ra["overall_risk"] = risk
    ra["need_human_review"] = False
    return {"report": report, "risk_assessment": ra, "disposition": disposition,
            "need_human_review": False, "status": "completed",
            "human_verdict": {"verdict": action, "risk_label": label,
                              "notes": verdict.get("notes")},
            "steps": ["HUMAN_VERDICT_APPLIED"]}


def _log(name: str, result: dict, **detail: Any) -> dict[str, Any]:
    """工具调用审计 patch（reducer 追加）。

    2026-09-29 不变量：`status="error"`（真失败）必须携带原因。否则会产出
    "有失败、无理由"的审计条目，使下游无法把失败归因 → 退化成不可判定态 → 无谓转人工。
    注意只对 error 强制：`review` 是合法中间态（如配准 below_hard_floor），原因在 data 里。
    """
    status = result.get("status")
    error = result.get("error")
    if status == "error" and not error:
        error = f"{name}: 工具失败但未提供原因（reason_missing）"
    return {"tool_calls": [{"tool": name, "status": status, "error": error, **detail}]}


# ---------- 节点 ----------

def _diagnose_vlm_failure(result: dict[str, Any], pair_id: str | None = None,
                          run_dir: str | None = None) -> dict[str, Any]:
    """将 VLM 失败从笼统 error 转成可解释、可路由的原因码。"""
    text = str(result.get("error") or "").lower()
    # 工具报错信息不足时，直接读取本次融合图做两个低成本输入体检。
    image = None
    for candidate in (Path(run_dir or "") / "input" / f"{pair_id}.jpg",
                      PROJECT / "results" / "fused" / f"{pair_id}.jpg"):
        if candidate.is_file():
            image = cv2.imread(str(candidate), cv2.IMREAD_GRAYSCALE)
            if image is not None:
                break
    if image is not None:
        mean = float(image.mean())
        sharpness = float(cv2.Laplacian(image, cv2.CV_64F).var())
        if mean < 25:
            return {"code": "input_low_light", "reason": "融合图平均亮度过低，可能曝光不足",
                    "mean_brightness": round(mean, 2), "sharpness": round(sharpness, 2),
                    "raw_error": result.get("error")}
        if sharpness < 20:
            return {"code": "input_blurry", "reason": "融合图拉普拉斯方差过低，可能存在严重模糊",
                    "mean_brightness": round(mean, 2), "sharpness": round(sharpness, 2),
                    "raw_error": result.get("error")}
    if any(k in text for k in ("not found", "不存在", "missing", "input")):
        code, reason = "input_missing", "融合图或模型输入不存在"
    elif any(k in text for k in ("timeout", "timed out", "连接", "gateway", "api")):
        code, reason = "vlm_service_unavailable", "VLM 服务不可用或超时"
    elif any(k in text for k in ("decode", "parse", "json", "格式")):
        code, reason = "vlm_output_invalid", "VLM 返回内容无法解析"
    elif any(k in text for k in ("dark", "brightness", "exposure", "暗")):
        code, reason = "input_low_light", "输入图像亮度过低，VLM 语义不稳定"
    elif any(k in text for k in ("blur", "focus", "模糊")):
        code, reason = "input_blurry", "输入图像清晰度不足，VLM 语义不稳定"
    else:
        code, reason = "vlm_unknown_failure", "VLM 推理失败，具体原因未被工具报告"
    return {"code": code, "reason": reason, "raw_error": result.get("error")}

def make_nodes(tool_call: Callable[..., dict] = default_call,
               max_retries: int = 1, planner_gateway: Any = None) -> dict[str, Callable]:
    """构造节点闭包；tool_call 可注入（测试）。planner_gateway=True 自动构建网关，
    传入对象则用注入网关（测试）；None 表示不启用规划器。"""

    def node_qc(state: GraphState) -> dict[str, Any]:
        # 配准质检失败时先允许一次“重标准化+重融合”，只有仍失败才进入人工归档。
        # 该重试不改变原图，只更新本次 run_dir 下的融合产物和 QC。
        res = tool_call("fuse_qc", state["pair_id"], state.get("run_dir", ""))
        attempts = 1
        if res.get("status") != "ok" or not (res.get("data") or {}).get("passed"):
            retry = tool_call("prepare_pair", state["pair_id"], state.get("run_dir", ""))
            attempts += 1
            if retry.get("status") == "ok":
                res = tool_call("fuse_qc", state["pair_id"], state.get("run_dir", ""))
                attempts += 1
        quality = res.get("data") or {"passed": False, "flag": "error", "edge_align": None}
        patch: dict[str, Any] = {"quality": quality, "steps": ["FUSE_AND_QC"],
                                 "qc_retry_count": attempts - 1,
                                 **_log("fuse_qc", res, flag=quality.get("flag"), attempts=attempts)}
        if not quality.get("passed"):
            patch["review_reasons"] = ["fusion_qc_failed", quality.get("flag") or "fusion_quality_unknown"]
        return patch

    def node_perceive(state: GraphState) -> dict[str, Any]:
        pair_id, run_dir = state["pair_id"], state["run_dir"]
        retries = 0
        res: dict[str, Any] = {"status": "error", "error": "not_run"}
        scene: dict[str, Any] = {}
        for attempt in range(max_retries + 1):
            res = tool_call("vlm_open_detect", pair_id, run_dir)
            if res.get("status") == "ok":
                scene = res["data"]["scene"]
                break
            retries = attempt + 1
        if res.get("status") != "ok":
            # VLM 不可用降级（2026-09-29 修复）：空语义场景必须转人工，禁止继续判级。
            # 原实现只写 _meta.degraded 就放行，图会继续走到 judge → 规则引擎见 targets 为空
            # → n_workers==0 → 判"安全"→ 归档，把感知失效误判为"现场安全"（pair28 案例：
            # GDYOLO 实检 1 名 conf=0.926 的工人，最终输出「安全-记录」）。
            # 现与 runner.py 的 GDYOLO_FALLBACK 语义对齐：不输出任何风险等级，直接人工闸口。
            scene = {"image_id": pair_id, "targets": [],
                     "scene": {"dominant_state": "无明确作业", "site_type": ""},
                     "_meta": {"degraded": True, "semantic": "empty"}}
            diag = _diagnose_vlm_failure(res, pair_id, run_dir)
            return {"scene": scene, "vlm_retry_count": retries, "vlm_failure": diag,
                    "steps": ["VLM_DETECT"],
                    "review_reasons": ["vlm_unavailable", diag["code"]],
                    **_log("vlm_open_detect", res, attempts=retries + 1)}
        return {"scene": scene, "vlm_retry_count": retries, "steps": ["VLM_DETECT"],
                **_log("vlm_open_detect", res, attempts=retries + 1)}

    def node_detect(state: GraphState) -> dict[str, Any]:
        res = tool_call("yolo_detect", state["pair_id"], state["run_dir"])
        detections = res.get("data") or {"objects": []}
        return {"detections": detections, "steps": ["GDYOLO_DETECT"],
                **_log("yolo_detect", res, n_objects=len(detections.get("objects", [])))}

    def node_reconcile(state: GraphState) -> dict[str, Any]:
        res = tool_call("reconcile", state["pair_id"], state["run_dir"])
        patch: dict[str, Any] = {"steps": ["RECONCILE"], **_log("reconcile", res)}
        if res.get("status") != "ok":
            return {**patch, "review_reasons": ["reconcile_failed"]}
        data = res["data"]
        rec = dict(data.get("reconciliation") or {})
        supplement = data.get("supplement") or {}
        # 双源一致性判定（2026-09-29 修复）：此前只认 yolo_only（GDYOLO 有 / VLM 无）一个方向，
        # "VLM 说有人、GDYOLO 说没人"（vlm_only 全量）不产生任何未决标记 → 分歧被静默抹平。
        # 后果：归档通道不跑 LLM，unverified 目标仍参与规则引擎计数（甚至凑出"监督陪护"从而
        # 降低等级），而审计里没有"这次双源没对上"的记录 → 无法定位。
        n_vlm = int(rec.get("n_vlm") or 0)
        n_yolo = int(rec.get("n_yolo") or 0)
        vlm_only = int(rec.get("vlm_only") or 0)
        conflicts: list[str] = []
        if vlm_only and n_yolo == 0:
            conflicts.append("vlm_only_no_yolo_support")
        elif n_vlm and vlm_only == n_vlm:
            conflicts.append("vlm_only_all")
        if int(rec.get("yolo_only") or 0):
            conflicts.append("yolo_only_unresolved")
        if supplement.get("rejected"):
            conflicts.append("supplement_rejected")
        if conflicts:
            # 分歧被**显式记录** → 从"无法定位"变成"可定位"，后续由 outcome 归因；
            # 规划器可据此调用 recrop_verify 做一次细粒度重感知再裁决。
            rec["conflicts"] = conflicts
            patch["anomalies"] = conflicts
        patch["scene"] = data["scene"]
        patch["reconciliation"] = rec
        return patch

    def node_verify_equipment(state: GraphState) -> dict[str, Any]:
        res = tool_call("verify_equipment_state", state.get("scene") or {},
                        state.get("detections") or {})
        data = res.get("data") or {}
        patch: dict[str, Any] = {"equipment_check": data, "steps": ["VERIFY_EQUIPMENT"],
                                 **_log("verify_equipment_state", res)}
        if data.get("hard_mismatch"):
            # 几何与 VLM 设备状态冲突：只记审计标记（规划器在链路健康报告可见并核实），
            # 不阻断处置——判级语义（2026-09-29）：证据问题只做标注，处置不变
            patch["anomalies"] = ["equipment_state_mismatch"]
        return patch

    def node_judge(state: GraphState) -> dict[str, Any]:
        res = tool_call("evaluate_risk", state.get("scene") or {})
        rule = res.get("data") or {}
        route = "full"
        if rule.get("need_human_review"):
            route = "manual"
        elif rule.get("overall_risk") == "安全":
            # 双源存在未裁决分歧时不得静默归档为"安全"（2026-09-29）：
            # 该"安全"结论未获双源支撑，归档通道不跑 LLM 也没有机会质疑。
            route = "manual" if open_conflicts(state) else "archive"
        elif rule.get("overall_risk") == "低风险":
            route = "tip"
        return {"rule": rule, "route": route, "steps": ["JUDGE"],
                **_log("evaluate_risk", res, route=route)}

    def node_archive(state: GraphState) -> dict[str, Any]:
        rule = state.get("rule") or {}
        return {"report": {"overall_risk": "安全", "warnings": [],
                           "_meta": {"fast_mode": True, "rule_engine": rule}},
                "risk_assessment": {"overall_risk": "安全", "n_warnings": 0,
                                    "need_human_review": False},
                "disposition": "P4_archive", "steps": ["ARCHIVE"]}

    def node_manual(state: GraphState) -> dict[str, Any]:
        """规则引擎判不了 → 与人工闸口共用同一套终态三分类（2026-09-29）。

        判不了不等于必须人工：原因可定位（作业类型未知 / 危险区状态不确定）→ 判断失败，
        自主结案 + 抽样复核；只有连原因都定位不到才纯转人工。
        两种都不输出风险等级（禁止"安全/低风险 + 转人工"并存）。
        """
        rule = state.get("rule") or {}
        outcome = classify_outcome({**state, "risk_assessment": {"overall_risk": None}})
        failed = outcome["outcome"] == FAILED
        return {
            "judgement": outcome,
            "report": {"overall_risk": None, "warnings": [],
                       "_meta": {"manual_route": True, "unjudged": True,
                                 "outcome": outcome["outcome"],
                                 "failure_reason": outcome["reason"],
                                 "rule_engine": rule}},
            "risk_assessment": {"overall_risk": None, "n_warnings": 0,
                                "need_human_review": True,
                                "outcome": outcome["outcome"],
                                "failure_reason": outcome["reason"]},
            "disposition": "P3_manual_judge",
            "need_human_review": True,
            "human_archive": {"required": True, "archived": True, "channel": "human", "blocking": False},
            "steps": ["MANUAL_JUDGE"]}

    def _run_llm(state: GraphState, mode: str) -> dict[str, Any]:
        res = tool_call("generate_judgement", state["pair_id"], state["run_dir"], mode)
        patch: dict[str, Any] = {"steps": ["LLM_TIP" if mode == "tip" else "LLM_FULL"],
                                 **_log("generate_judgement", res, mode=mode)}
        if res.get("status") != "ok":
            return {**patch, "error": res.get("error") or "LLM 判级失败",
                    "review_reasons": ["llm_tool_failed"]}
        report = res["data"]["report"]
        retrieval = report.get("_meta", {})
        tip_mode = mode == "tip"
        risk = report.get("overall_risk")
        check = tool_call("validate_report", report, state.get("scene") or {},
                          retrieval, check_clauses=not tip_mode)
        # 证据链异常（幻觉防护标记）只记审计，不改变处置：有危险必须告警
        anomalies = list((check.get("data") or {}).get("reasons") or [])
        if report.get("need_human_review"):
            anomalies.append("llm_requested_human_review")
        # 决策 A（2026-09-29 修订）：引用 unverified_by_yolo 目标的警示照常输出，
        # 仅在对应警示上标注"未经 GDYOLO 证实"
        unverified_ids = {t.get("bbox_id") for t in (state.get("scene") or {}).get("targets", []) or []
                          if isinstance(t, dict) and t.get("unverified_by_yolo")}
        cited = False
        for warning in report.get("warnings", []) or []:
            if not isinstance(warning, dict):
                continue
            text = json.dumps(warning.get("evidence", []) or [], ensure_ascii=False)
            hit = sorted(u for u in unverified_ids if u and u in text)
            if hit:
                warning["unverified_by_yolo"] = hit
                cited = True
        if cited and "cited_unverified_target" not in anomalies:
            anomalies.append("cited_unverified_target")
        patch["report"] = report
        patch["evidence_check"] = check.get("data") or {}
        patch["risk_assessment"] = {"overall_risk": risk,
                                    "n_warnings": len(report.get("warnings", []) or []),
                                    "need_human_review": False}
        # 处置由代码推导（LLM 不可绕过）：确定等级 → 执行对应通道；无等级 → 转人工且不输出等级
        patch["disposition"] = disposition_for_outcome("judgement_success", risk)
        patch["need_human_review"] = patch["disposition"] == "P3_manual_judge"
        patch["risk_assessment"]["need_human_review"] = patch["need_human_review"]
        if anomalies:
            patch["anomalies"] = anomalies
        return patch

    def node_tip(state: GraphState) -> dict[str, Any]:
        patch = _run_llm(state, "tip")
        # tip 判级被上调为高风险：补跑 full 链路获取条款后强制告警（不得降级为转人工）；
        # 补跑失败或重跑降级不撤回已判定的高风险——照常告警，条款缺失记审计标记
        if (patch.get("risk_assessment") or {}).get("overall_risk") == "高风险":
            full = _run_llm(state, "full")
            if (not full.get("error")
                    and (full.get("risk_assessment") or {}).get("overall_risk") == "高风险"):
                full["steps"] = (patch.get("steps") or []) + (full.get("steps") or []) + ["TIP_ESCALATED_FULL"]
                full["tool_calls"] = (patch.get("tool_calls") or []) + (full.get("tool_calls") or [])
                return full
            patch["anomalies"] = list(patch.get("anomalies") or []) + ["clause_support_missing"]
            patch["steps"] = (patch.get("steps") or []) + ["TIP_ESCALATION_NO_CLAUSE"]
        return patch

    def node_full(state: GraphState) -> dict[str, Any]:
        return _run_llm(state, "full")

    def node_plan(state: GraphState) -> dict[str, Any]:
        """Step-2 有界 ReAct 规划器：种子研判后由 LLM 自主决定补充动作与收尾。

        2026-09-29：覆盖范围从 tip/full 扩展到 **manual（规则引擎判不了）**——
        否则"最需要定向重感知"的判不了场景反而用不上 recrop_verify。
        """
        rule = state.get("rule") or {}
        undetermined = rule.get("overall_risk") not in VALID_RISKS

        def fallback(extra_calls: list, error: str = "") -> dict[str, Any]:
            """规划器不可用时的确定性回退。

            规则引擎有等级 → 照旧走 tip/full；
            规则引擎判不了 → **不得让 LLM 自行定级**（那等于用模型判断覆盖确定性的"判不了"），
            按判不了收尾，交由终态三分类归因。
            """
            calls = list(extra_calls or [])
            if error:
                calls.append({"tool": "planner", "error": error})
            if undetermined:
                return {"steps": ["PLAN_FALLBACK", "PLAN_FALLBACK_UNDETERMINED"],
                        "plan_fallback": True,
                        "risk_assessment": {"overall_risk": None, "n_warnings": 0,
                                            "need_human_review": True},
                        "disposition": "P3_manual_judge", "need_human_review": True,
                        "human_archive": {"required": True, "archived": True, "channel": "human", "blocking": False},
                        "review_reasons": ["planner_unavailable"],
                        "tool_calls": calls}
            base = _run_llm(state, "full" if rule.get("overall_risk") == "高风险" else "tip")
            base["steps"] = (base.get("steps") or []) + ["PLAN_FALLBACK"]
            base["plan_fallback"] = True
            base["tool_calls"] = (base.get("tool_calls") or []) + calls
            return base

        gw, model_id = planner_gateway, "planner-model"
        # node_plan 仅当 planner_gateway 非 None 时入图（见 build_graph 路由表），
        # 故此处 gw 不可能是 None；True = 按项目配置现建网关。
        if gw is True:
            try:
                from tools.api_config import load_api_config
                from tools.model_gateway import ModelGateway
                gw, model_id = ModelGateway.from_project_config(PROJECT, "llm", temperature=0)
            except Exception as exc:
                return fallback([], f"网关构建失败: {exc}")
        res = run_planning(state, gateway=gw, model_id=model_id, tool_call=tool_call)
        if res.get("plan_fallback"):
            return fallback(res.get("tool_calls") or [])
        return res

    def node_human_gate(state: GraphState) -> dict[str, Any]:
        """判不了 → 终态三分类（2026-09-29 用户定稿）。

        判断失败或无法判断都进入 P3_manual_judge，登记人工复核且不输出风险等级。
        """
        reasons = list(state.get("review_reasons") or [])
        anomalies = list(state.get("anomalies") or [])
        for r in reasons:
            if r not in anomalies:
                anomalies.append(r)
        outcome = classify_outcome({**state, "risk_assessment": {"overall_risk": None}})
        failed = outcome["outcome"] == FAILED
        patch: dict[str, Any] = {
            "judgement": outcome,
            "report": {"overall_risk": None, "warnings": [],
                       "_meta": {"manual_route": True, "unjudged": True, "reasons": reasons,
                                 "outcome": outcome["outcome"],
                                 "failure_reason": outcome["reason"],
                                 "rule_engine": state.get("rule") or {}}},
            "risk_assessment": {"overall_risk": None, "n_warnings": 0,
                                "need_human_review": True,
                                "outcome": outcome["outcome"],
                                "failure_reason": outcome["reason"]},
            "disposition": "P3_manual_judge",
            "need_human_review": True,
            "human_archive": {"required": True, "archived": True, "channel": "human", "blocking": False},
            "steps": ["REVIEW_GATE"]}
        if anomalies:
            patch["anomalies"] = anomalies
        return patch

    def node_finalize(state: GraphState) -> dict[str, Any]:
        patch: dict[str, Any] = {"steps": ["FINALIZE"]}
        outcome = classify_outcome(state)
        risk = (state.get("risk_assessment") or {}).get("overall_risk")
        if outcome["outcome"] != "judgement_success":
            disposition = "P3_manual_judge"
            need = True
        else:
            disposition = state.get("disposition") or disposition_for_outcome(outcome["outcome"], risk)
            need = False
        if disposition == "P4_archive" or state.get("sample_check"):
            patch["sample_check"] = True      # 进抽样复核队列（事后核查，不阻塞）
        patch["judgement"] = outcome
        patch["disposition"] = disposition
        patch["need_human_review"] = need
        if disposition == "P3_manual_judge":
            patch["human_archive"] = {"required": True, "archived": True, "channel": "human", "blocking": False}
        patch["status"] = "completed"        # 三分类都是完成态，差异只在处置通道与队列
        out = Path(state.get("run_dir")
                   or (PROJECT / "results" / "agent" / "runs" / str(state.get("task_id"))))
        try:
            out.mkdir(parents=True, exist_ok=True)
            # 落盘**完整终态**（此前保存节点入参：缺 status/judgement，且落盘失败被静默吞掉）
            save_json(out / "graph_state.json", {**state, **patch})
        except Exception as exc:
            patch["anomalies"] = ["state_save_failed"]
            patch["error"] = f"状态落盘失败: {exc}"[:200]
            outcome = classify_outcome({**state, **patch})
            patch["judgement"] = outcome
            patch["disposition"] = "P3_manual_judge"     # 结果完整性受损 → 必须人工
            patch["need_human_review"] = True
            patch.pop("sample_check", None)
            patch["human_archive"] = {"required": True, "archived": True, "channel": "human", "blocking": False}
            try:
                save_json(out / "graph_state.json", {**state, **patch})
            except Exception:
                pass          # 二次落盘同样失败：原因已记入 anomalies，不再静默
            need = True
        final_values = {**state, **patch}
        if patch["disposition"] == "P3_manual_judge":
            register_human_archive(final_values)
        register_pending_review(final_values)
        try:
            _record_work_type_memory(final_values)
        except Exception:
            pass  # 记忆失败不阻塞收尾
        return patch

    return {"qc": node_qc, "perceive": node_perceive, "detect": node_detect,
            "reconcile": node_reconcile, "verify_equipment": node_verify_equipment,
            "judge": node_judge, "archive": node_archive, "manual": node_manual,
            "tip": node_tip, "full": node_full, "plan": node_plan,
            "human_gate": node_human_gate, "finalize": node_finalize}


# ---------- 条件边（路由一等化，可独立单测） ----------

def route_after_qc(state: GraphState) -> str:
    return "perceive" if (state.get("quality") or {}).get("passed") else "human_gate"


def route_after_perceive(state: GraphState) -> str:
    """感知失败 → 直接人工闸口，不得进入判级。

    VLM 失效时场景被置空，若继续走 detect/reconcile/judge，规则引擎会把"零目标"
    读成"现场无人"并输出"安全"。判不了必须转人工，不能退化成"安全"（2026-09-29 修复）。
    """
    return "human_gate" if "vlm_unavailable" in (state.get("review_reasons") or []) else "detect"


def route_after_reconcile(state: GraphState) -> str:
    """对账失败必须进入人工闸口，禁止带着缺失的双源证据继续判级。"""
    return "human_gate" if "reconcile_failed" in (state.get("review_reasons") or []) else "verify_equipment"


def route_after_judge(state: GraphState) -> str:
    route = state.get("route") or "full"
    return {"archive": "archive", "manual": "manual", "tip": "tip"}.get(route, "full")


def route_after_act(state: GraphState) -> str:
    """tip/full/plan 之后：已有确定等级 → 收尾执行对应处置（证据异常只记审计）；
    无等级（判不了 / 工具失败）→ 人工闸口，不输出任何风险等级。"""
    risk = (state.get("risk_assessment") or {}).get("overall_risk")
    return "finalize" if risk in VALID_RISKS else "human_gate"


def build_graph(tool_call: Callable[..., dict] = default_call,
                max_retries: int = 1, checkpointer=None,
                planner_gateway: Any = None):
    """装配 StateGraph。checkpointer 传入 SqliteSaver/MemorySaver 以启用持久化；
    planner_gateway 非 None 时启用 Step-2 规划器（tip/full 分支改走有界 ReAct）。"""
    nodes = make_nodes(tool_call, max_retries, planner_gateway)
    def route_reconcile_with_planner(state: GraphState) -> str:
        if "reconcile_failed" in (state.get("review_reasons") or []) and planner_gateway is not None:
            return "plan"
        return route_after_reconcile(state)
    g = StateGraph(GraphState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "qc")
    g.add_conditional_edges("qc", route_after_qc,
                            {"perceive": "perceive", "human_gate": "human_gate"})
    g.add_conditional_edges("perceive", route_after_perceive,
                            {"detect": "detect", "human_gate": "human_gate"})
    g.add_edge("detect", "reconcile")
    g.add_conditional_edges("reconcile", route_reconcile_with_planner,
                            {"verify_equipment": "verify_equipment", "human_gate": "human_gate", "plan": "plan"})
    g.add_edge("verify_equipment", "judge")
    if planner_gateway is not None:
        # manual（判不了）也交给规划器：先尝试 recrop_verify 定向重感知，
        # 复核后重跑规则引擎——解决了才定级，仍判不了才按失败收尾。
        g.add_conditional_edges("judge", route_after_judge,
                                {"archive": "archive", "manual": "plan",
                                 "tip": "plan", "full": "plan"})
        g.add_conditional_edges("plan", route_after_act,
                                {"human_gate": "human_gate", "finalize": "finalize"})
    else:
        g.add_conditional_edges("judge", route_after_judge,
                                {"archive": "archive", "manual": "manual",
                                 "tip": "tip", "full": "full"})
        g.add_conditional_edges("tip", route_after_act,
                                {"human_gate": "human_gate", "finalize": "finalize"})
        g.add_conditional_edges("full", route_after_act,
                                {"human_gate": "human_gate", "finalize": "finalize"})
    g.add_edge("archive", "finalize")
    g.add_edge("manual", "finalize")
    g.add_edge("human_gate", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def run_task(pair_id: str, task_id: str | None = None, *, checkpointer=None,
             tool_call: Callable[..., dict] = default_call, max_retries: int = 1) -> dict[str, Any]:
    """命令行直跑入口（无服务）：返回最终状态快照。"""
    task_id = task_id or f"graph-{pair_id}"
    run_dir = str(PROJECT / "results" / "agent" / "graph_runs" / task_id)
    app = build_graph(tool_call=tool_call, max_retries=max_retries, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": task_id}}
    final = app.invoke({"task_id": task_id, "pair_id": pair_id, "run_dir": run_dir,
                        "steps": [], "tool_calls": [], "review_reasons": []}, config=config)
    return dict(final)
