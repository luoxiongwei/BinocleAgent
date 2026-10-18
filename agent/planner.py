# -*- coding: utf-8 -*-
"""Step-2 有界 ReAct 规划器：种子研判后由 LLM 自主决定补充动作与收尾（2026-09-29）。

边界（借鉴 ReAct + 项目 docs/agent化升级.md §14 的安全域改造）：
  - 种子步骤固定：qc→vlm→yolo→reconcile→verify→rule 由图确定性执行（感知链不可跳过）；
  - 规划器只在"低风险/高风险"分支上获得自主权：决定是否生成 LLM 判级（tip/full）、
    是否请求人工，最后必须 finalize_plan 收尾；
  - 硬界：最多 PLANNER_MAX_ROUNDS 轮工具调用，超限/网关失败回退确定性路由（tip/full 按规则）；
  - 策略守门在代码侧：overall_risk 下界=规则引擎判定（LLM 只能上调不能下调）；
    无条款支撑的"高风险"不得自动告警（转人工）； disposition 最终由代码推导；
  - 全审计：每轮 Thought(reasoning)/Action/Observation 写入 state.tool_calls。

白名单工具（仅 3 个，防成本与行为失控）：
  generate_judgement{mode: tip|full} / request_human_review{reason} / finalize_plan{risk_label, rationale}
"""
from __future__ import annotations

import json
from typing import Any, Callable

from agent.outcome import RISK_RANK, VALID_RISKS
from agent.tools_registry import (chain_health as _chain_health,
                                  inspect_reconcile as _inspect_reconcile)

PLANNER_MAX_ROUNDS = 4

PLANNER_SYSTEM_PROMPT = (
    "你是施工现场安全研判的规划器。感知链（VLM 开放检测/GDYLOLO 检测/对账/几何校验）与规则引擎"
    "已确定性完成，结果在【当前研判状态】与【链路健康报告】中。你的职责是监控链路、决定补充研判"
    "动作并收尾：\n"
    "1) 可调用工具：\n"
    "   generate_judgement{mode}: mode=tip 生成推荐性提示（低风险简单作业，无强制条款）；"
    "mode=full 检索强制条款生成警示（复杂高危作业，警示将要求立即远离危险源）。\n"
    "   request_human_review{reason}: 证据不足/画面不可辨时请求人工。\n"
    "   inspect_chain{}: 读感知链健康报告（各步状态/降级/对账分歧/建议动作），只读。\n"
    "   inspect_reconcile{}: 对账明细深查（逐目标 vlm/yolo 支撑与 IoU、未互相确认的区域），只读。\n"
    "   yolo_recheck{conf, region}: 定点二次检测（更低置信度重跑 GDYOLO，可限定画布区域）；每次规划最多一次。\n"
    "   rerun_step{step}: 单步重跑感知环节（vlm|yolo|reconcile）；每次规划最多一次，不写回基线。\n"
    "   recrop_verify{targets, fields, reason}: **定向重感知复核**——对分歧点/不确定字段做局部放大重查"
    "并重算对账；每次规划最多一次。\n"
    "   finalize_plan{risk_label, rationale}: 收尾，risk_label ∈ 安全/低风险/高风险。\n"
    "2) 链路监控语义（读【链路健康报告】）：\n"
    "   - conflicts 非空（yolo_only_unresolved / vlm_only_* / supplement_rejected）：双源对账存在"
    "**未裁决分歧**——必须先 recrop_verify 对分歧点做细粒度重感知，再据复核结果裁决；\n"
    "   - equipment_hard_mismatch=true：几何与 VLM 设备状态冲突——先 recrop_verify(fields=['equipment_state'])；\n"
    "   - vlm_only>0：VLM 目标未获 YOLO 证实（unverified），引用它们作警示依据会被标注「未经证实」；\n"
    "   - health=failed（VLM 降级/解析失败/工具失败/配准未过）：场景不可信，可 rerun_step 重试对应环节；"
    "仍不可信则 request_human_review；\n"
    "   - qc_flag 非 ok：配准质量降级，跨模态位置判断降权。\n"
    "3) 硬性策略（代码会强制执行，违反无效）：\n"
    "   - **定级前必须先消费完反馈集合**：健康报告 + 规则引擎判定 + 对账结果；"
    "存在未裁决分歧时，未调用 recrop_verify 不允许 finalize_plan（会被拒绝）；\n"
    "   - risk_label 不得低于【规则引擎判定】的 overall_risk（**硬下界，只能抬不能降**）；\n"
    "   - 若规则引擎判定为**判不了**（overall_risk 为 null，原因见 drivers）："
    "**不得由你自行补一个等级**。正确做法是先 recrop_verify 解决那个不确定项——"
    "复核后系统会重跑规则引擎，若不确定性被解决则下界变为确定等级，此时才能定级；"
    "若复核后仍判不了，据实说明无法研判（不要猜，也不要用『低风险』搪塞）；\n"
    "   - 低风险简单作业优先 tip（省时省钱）；高风险/需要条款依据才 full；\n"
    "   - 证据不足或画面过暗过曝无法研判时用 request_human_review，并在 reason 里写明"
    "**是哪一步、哪个字段**导致无法研判（原因可定位 → agent 可自主结案并进抽样复核队列）；\n"
    "   - 最后一步必须调用 finalize_plan。\n"
    "4) 只输出合法工具调用，不臆造事实。"
)

PLANNER_SPEC = [
    {"type": "function", "function": {
        "name": "generate_judgement",
        "description": "生成 LLM 判级报告：tip=推荐性提示（无条款）；full=RAG 检索条款警示",
        "parameters": {"type": "object",
                       "properties": {"mode": {"type": "string", "enum": ["tip", "full"]},
                                      "rationale": {"type": "string"}},
                       "required": ["mode"]}}},
    {"type": "function", "function": {
        "name": "request_human_review",
        "description": "请求人工复核（证据不足/画面不可辨/多源矛盾）",
        "parameters": {"type": "object",
                       "properties": {"reason": {"type": "string"}},
                       "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "inspect_chain",
        "description": "深查感知链健康明细：每步工具状态、对账平衡度、目标数对比。只读，不改任何状态",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "yolo_recheck",
        "description": "定点二次检测：更低置信度重跑 GDYOLO（可限定画布区域）。每次规划最多一次，"
                       "结果仅作研判证据，不改写对账基线",
        "parameters": {"type": "object",
                       "properties": {"conf": {"type": "number",
                                               "description": "置信度阈值，默认 0.15（低于主检 0.25）"},
                                      "region": {"type": "array", "items": {"type": "number"},
                                                 "description": "可选画布区域 [x1,y1,x2,y2]"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "inspect_reconcile",
        "description": "对账明细深查：逐目标列出 VLM/YOLO 支撑与最佳 IoU、双源未互相确认的区域、"
                       "supplement 被剔除原因、unverified 标记。只读，不改任何状态",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "rerun_step",
        "description": "单步重跑一个感知环节，结果仅作研判证据、不写回基线。每次规划最多一次",
        "parameters": {"type": "object",
                       "properties": {"step": {"type": "string",
                                               "enum": ["vlm", "yolo", "reconcile"]}},
                       "required": ["step"]}}},
    {"type": "function", "function": {
        "name": "recrop_verify",
        "description": "定向重感知复核：对感知-校验分歧点或不确定字段做局部放大重查并重算对账。"
                       "链路健康报告显示存在对账分歧/几何冲突时，**必须先完成这一步才能定级**。"
                       "每次规划最多一次",
        "parameters": {"type": "object",
                       "properties": {
                           "targets": {"type": "array", "items": {"type": "string"},
                                       "description": "要复核的目标 bbox_id（如 ['t1','t2']）；留空=全部分歧目标"},
                           "fields": {"type": "array", "items": {"type": "string"},
                                      "description": "要复核的字段，如 ['ppe.helmet','equipment_state']"},
                           "reason": {"type": "string",
                                      "description": "复核缘由，如 vlm_only_no_yolo_support"}},
                       "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "finalize_plan",
        "description": "收尾：给出最终风险等级与理由。等级不得低于规则引擎下界（下界是硬约束）",
        "parameters": {"type": "object",
                       "properties": {"risk_label": {"type": "string",
                                                     "enum": list(VALID_RISKS)},
                                      "rationale": {"type": "string"}},
                       "required": ["risk_label", "rationale"]}}},
]


def chain_report(state: dict[str, Any]) -> dict[str, Any]:
    """感知链健康报告（Step-3 监控）：每步工具状态 + 产物统计，只读。"""
    q = state.get("quality") or {}
    scene = state.get("scene") or {}
    rec = (scene.get("_meta") or {}).get("reconciliation") or state.get("reconciliation") or {}
    return {
        "steps": [{"tool": c.get("tool"), "status": c.get("status"),
                   "error": c.get("error"), **{k: v for k, v in c.items()
                                               if k in ("flag", "n_objects", "attempts")}}
                  for c in state.get("tool_calls", []) if c.get("tool") != "planner"],
        "counts": {
            "qc_flag": q.get("flag"),
            "qc_edge_align": q.get("edge_align"),
            "n_vlm_targets": len([t for t in scene.get("targets", []) or []
                                  if isinstance(t, dict)]),
            "n_yolo_boxes": len((state.get("detections") or {}).get("objects", []) or []),
            "reconciliation": rec,
            "equipment_hard_mismatch": (state.get("equipment_check") or {}).get("hard_mismatch"),
            "degraded_vlm": bool((scene.get("_meta") or {}).get("degraded")),
        },
    }


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """摘取感知链监控工具所需的状态切片（不把整个 state 暴露给工具层）。"""
    return {"quality": state.get("quality") or {},
            "scene": state.get("scene") or {},
            "detections": state.get("detections") or {},
            "reconciliation": state.get("reconciliation") or {},
            "equipment_check": state.get("equipment_check") or {},
            "rule": state.get("rule") or {},
            "tool_calls": state.get("tool_calls") or []}


def _open_conflicts(state: dict[str, Any]) -> list[str]:
    """尚未裁决的感知分歧 —— 前置条件门的输入。

    存在这些分歧时不允许直接定级：必须先 recrop_verify 对分歧点做细粒度重感知，
    再据复核结果裁决（2026-09-29 用户要求：感知与校验不合时应能细粒度重来一次）。
    """
    conflicts = [str(c) for c in ((state.get("reconciliation") or {}).get("conflicts") or [])]
    if (state.get("equipment_check") or {}).get("hard_mismatch"):
        conflicts.append("equipment_state_mismatch")
    scene_meta = (state.get("scene") or {}).get("_meta") or {}
    if scene_meta.get("degraded") or scene_meta.get("parsing_error"):
        conflicts.append("vlm_scene_untrusted")
    seen: set[str] = set()
    out: list[str] = []
    for c in conflicts:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _scene_summary(state: dict[str, Any]) -> str:
    scene = state.get("scene") or {}
    s = scene.get("scene") or {}
    targets = scene.get("targets") or []
    equip = [f"{t.get('type')}:{((t.get('equipment_state') or {}).get('value')) or '不确定'}"
             for t in targets if isinstance(t, dict) and t.get("type") in ("机械", "塔吊")]
    check = state.get("equipment_check") or {}
    return json.dumps({
        "quality": state.get("quality"),
        "scene": {k: s.get(k) for k in ("lighting", "rgb_visibility", "dominant_state",
                                        "site_type") if s.get(k)},
        "n_targets": len(targets),
        "equipments": equip or None,
        "equipment_hard_mismatch": check.get("hard_mismatch"),
        "rule_engine": {"overall_risk": (state.get("rule") or {}).get("overall_risk"),
                        "drivers": (state.get("rule") or {}).get("drivers"),
                        "need_human_review": (state.get("rule") or {}).get("need_human_review")},
        "reconciliation": state.get("reconciliation"),
        "review_reasons": state.get("review_reasons") or [],
    }, ensure_ascii=False)


def run_planning(state: dict[str, Any], *, gateway: Any, model_id: str,
                 tool_call: Callable[..., dict]) -> dict[str, Any]:
    """执行有界 ReAct 循环，返回节点 patch。gateway 接口同 ModelGateway.complete。"""
    pair_id, run_dir = state["pair_id"], state.get("run_dir", "")
    rule = state.get("rule") or {}
    # 下界取规则引擎判定；规则引擎判不了时其 overall_risk 为 None，
    # 此处不得回填成"安全"——那会把"判断失败"静默伪装成"判断成功"（2026-09-29 修复）。
    floor = rule.get("overall_risk")

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": ("【当前研判状态】\n" + _scene_summary(state)
                                     + "\n\n【链路健康报告】\n"
                                     + json.dumps(chain_report(state), ensure_ascii=False)
                                     + f"\n\n规则下界={floor}。请监控链路、规划补充动作并"
                                       " finalize_plan 收尾。")},
    ]
    rounds, audit = 0, []
    report: dict[str, Any] | None = None
    human_reason: str | None = None
    final_label: str | None = None
    gateway_failed = False
    yolo_recheck_used = False
    rerun_used = False
    recrop_used = False
    recrop_result: dict[str, Any] | None = None
    resolved_rule: dict[str, Any] | None = None   # 重感知后重跑规则引擎的结果（可能把 None 变成等级）

    while rounds <= PLANNER_MAX_ROUNDS:
        rounds += 1
        try:
            resp_obj = gateway.complete(model=model_id, messages=messages, tools=PLANNER_SPEC)
        except Exception as exc:  # 网关异常 → 确定性回退
            audit.append({"round": rounds, "error": str(exc)})
            gateway_failed = True
            break
        # ModelGateway 返回 ToolResult 对象，fake/测试可为 dict——统一归一化
        if hasattr(resp_obj, "status"):
            resp = {"status": resp_obj.status,
                    "data": getattr(resp_obj, "data", None) or {},
                    "error": getattr(resp_obj, "error", None)}
        else:
            resp = resp_obj
        if resp.get("status") != "ok":
            audit.append({"round": rounds, "error": resp.get("error")})
            gateway_failed = True
            break
        data = resp.get("data") or {}
        thought = str(data.get("reasoning") or "")[:600]
        tcs = data.get("tool_calls")
        if not tcs:
            audit.append({"round": rounds, "tool": "final_answer", "thought": thought})
            break
        messages.append({"role": "assistant", "content": data.get("content") or None,
                         "tool_calls": [{"id": t["id"], "type": "function",
                                         "function": {"name": t["function"]["name"],
                                                      "arguments": t["function"]["arguments"]}}
                                        for t in tcs]})
        terminal = False
        for t in tcs:
            name = t["function"]["name"]
            try:
                args = json.loads(t["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            obs: dict[str, Any]
            if name == "generate_judgement" and not report:
                mode = args.get("mode") if args.get("mode") in ("tip", "full") else "tip"
                res = tool_call("generate_judgement", pair_id, run_dir, mode)
                report = (res.get("data") or {}).get("report")
                obs = {"status": res.get("status"),
                       "overall_risk": (report or {}).get("overall_risk"),
                       "n_warnings": len((report or {}).get("warnings", []) or []),
                       "error": res.get("error")}
            elif name == "request_human_review":
                human_reason = str(args.get("reason", ""))[:200]
                obs = {"status": "ok"}
            elif name == "inspect_chain":
                # 链路健康报告（纯函数，直接调用不经注入，保证只读且可离线测）
                obs = {"status": "ok", "chain": chain_report(state),
                       "health": (_chain_health(_snapshot(state)).get("data") or {})}
            elif name == "inspect_reconcile":
                obs = {"status": "ok",
                       "detail": (_inspect_reconcile(_snapshot(state)).get("data") or {})}
            elif name == "rerun_step":
                if rerun_used:
                    obs = {"status": "skipped", "reason": "rerun_step 每次规划最多一次"}
                else:
                    rerun_used = True
                    res = tool_call("rerun_step", pair_id, run_dir, args.get("step"))
                    obs = {"status": res.get("status"), "error": res.get("error"),
                           "result": (res.get("data") or {}).get("result")}
            elif name == "recrop_verify":
                if recrop_used:
                    obs = {"status": "skipped", "reason": "recrop_verify 每次规划最多一次"}
                else:
                    recrop_used = True
                    res = tool_call("recrop_verify", pair_id, run_dir,
                                    args.get("targets"), args.get("fields"),
                                    args.get("reason") or "")
                    data = res.get("data") or {}
                    recrop_result = data if res.get("status") == "ok" else None
                    obs = {"status": res.get("status"), "error": res.get("error"),
                           "recrop": data.get("recrop"),
                           "recheck": {k: v for k, v in (data.get("recheck") or {}).items()
                                       if k in ("attempted", "updated", "targets", "notes")},
                           "reconciliation": data.get("reconciliation")}
                    # 重感知之后**重跑规则引擎**（2026-09-29）：这是"先补感知、再重算确定性事实、
                    # 最后才允许定级"的关键一步。不确定性若被复核解决，下界会变成确定等级；
                    # 未解决则下界仍为 None，规划器不得自行补一个等级。
                    new_scene = data.get("scene") or {}
                    if new_scene.get("targets") is not None:
                        rr = tool_call("evaluate_risk", new_scene)
                        new_rule = rr.get("data") or {}
                        if new_rule.get("overall_risk") in RISK_RANK:
                            floor = new_rule["overall_risk"]
                            resolved_rule = new_rule
                        obs["rule_after_recrop"] = {
                            "overall_risk": new_rule.get("overall_risk"),
                            "need_human_review": new_rule.get("need_human_review"),
                            "drivers": new_rule.get("drivers")}
                        if new_rule.get("need_human_review"):
                            obs["recrop_note"] = ("定向复核后规则引擎仍判不了：不得自行补等级，"
                                                  "应据实说明无法研判")
            elif name == "yolo_recheck":
                if yolo_recheck_used:
                    obs = {"status": "skipped", "reason": "yolo_recheck 每次规划最多一次"}
                else:
                    yolo_recheck_used = True
                    conf = args.get("conf")
                    conf = min(0.5, max(0.05, float(conf))) if isinstance(conf, (int, float)) else 0.15
                    region = args.get("region") if isinstance(args.get("region"), list) else None
                    res = tool_call("yolo_recheck", state["pair_id"],
                                    state.get("run_dir", ""), conf, region)
                    data = res.get("data") or {}
                    obs = {"status": res.get("status"),
                           "conf": conf, "n_boxes": data.get("n_boxes", 0),
                           "boxes": (data.get("boxes") or [])[:8],
                           "error": res.get("error")}
            elif name == "finalize_plan":
                gate = _open_conflicts(state)
                if gate and not recrop_used:
                    # 前置条件门：存在未裁决分歧且未做定向重感知 → 拒绝收尾
                    obs = {"status": "rejected",
                           "error": "存在未裁决的感知分歧，须先 recrop_verify 做定向重感知再定级",
                           "conflicts": gate}
                else:
                    final_label = args.get("risk_label")
                    obs = {"status": "ok", "accepted": final_label in VALID_RISKS}
                    terminal = True
            else:
                obs = {"error": f"未知工具: {name}"}
            audit.append({"round": rounds, "tool": name, "thought": thought,
                          "args": args, "obs": obs})
            messages.append({"role": "tool", "tool_call_id": t["id"],
                             "content": json.dumps(obs, ensure_ascii=False)})
        if terminal:
            break

    patch: dict[str, Any] = {"steps": [f"PLAN(r={rounds})"],
                             "tool_calls": [{"tool": "planner", "rounds": rounds,
                                             "calls": audit}]}
    reasons: list[str] = []

    # 风险合成：下界=规则引擎（定向重感知解决不确定性后会更新）；
    # 有报告取报告，否则取 finalize_plan 标签。
    report_risk = (report or {}).get("overall_risk")
    if floor in RISK_RANK:
        candidates = [r for r in (floor, report_risk, final_label) if r in RISK_RANK]
        final_risk = max(candidates, key=lambda r: RISK_RANK[r]) if candidates else floor
    else:
        # 规则引擎判不了、且定向重感知未能解决不确定性 → **不得采信 LLM 自报等级**，
        # 那等于用模型判断覆盖确定性事实推出的"判不了"（2026-09-29）。
        final_risk = None

    # 前置条件门（2026-09-29）：仍存在未裁决分歧且未做定向重感知 → 不得输出"确定等级"。
    # 这是"先集合反馈、再定级"的硬约束；失败原因可定位，下游按"判断失败"自主结案 + 抽样复核。
    gate_open = _open_conflicts(state)
    if gate_open and not recrop_used:
        final_risk = None

    # 处置输出本体守门（2026-10-17）：规划器有权不主动生成判级报告，但 TIP/P1 通道的
    # 播报文本/场景描述/条款依据即处置本体——缺了它，用户端就是一个空壳日志
    # （graph-pair42：规划器以"链路健康、无分歧"为由直接收尾，报告只剩壳）。
    # 收尾时有确定等级（低风险/高风险）却无报告 → 代码侧强制补跑 generate_judgement；
    # 网关已失败的场景（plan_fallback）由 node_plan.fallback 兜底，不在此重复调用。
    backfill_error: str | None = None
    if report is None and not gateway_failed and final_risk in ("低风险", "高风险"):
        mode = "tip" if final_risk == "低风险" else "full"
        res = tool_call("generate_judgement", pair_id, run_dir, mode)
        if res.get("status") == "ok":
            report = (res.get("data") or {}).get("report")
        else:
            backfill_error = str(res.get("error") or "generate_judgement 补跑失败")[:200]
        audit.append({"round": rounds + 1, "tool": "generate_judgement",
                      "thought": "代码侧守门：收尾时无判级报告但有确定等级，自动补跑生成播报/条款依据",
                      "args": {"mode": mode, "backfill": True},
                      "obs": {"status": "ok" if report is not None else "error",
                              "error": backfill_error, "backfill": True}})

    if report is not None:
        retrieval = report.get("_meta", {})
        tip_mode = retrieval.get("retrieval_mode") == "tip_mode"
        check = tool_call("validate_report", report, state.get("scene") or {},
                          retrieval, check_clauses=not tip_mode)
        reasons += list((check.get("data") or {}).get("reasons") or [])
        if retrieval.get("clause_out_of_scope"):
            reasons.append("clause_out_of_scope")
        if not tip_mode and any(not item.get("clause_ids")
                                for item in report.get("warnings", []) or []):
            reasons.append("warning_without_clause")   # tip 模式无条款是设计语义，不算违规
        if report.get("need_human_review") and final_risk == "高风险":
            # 转人工收紧（2026-09-29 用户语义）：LLM 谨慎请求仅在告警通道构成人工理由
            reasons.append("llm_requested")
        patch["report"] = report
        patch["evidence_check"] = check.get("data") or {"valid": not reasons,
                                                        "reasons": reasons}
        patch["route"] = "full" if retrieval.get("retrieval_mode") != "tip_mode" else "tip"
    else:
        patch["report"] = {"overall_risk": final_risk, "warnings": [],
                           "_meta": {"planner_only": True, "rule_engine": rule,
                                     **({"backfill_error": backfill_error} if backfill_error else {})}}
        # 补跑也失败：规则引擎的确定性结论仍然有效（valid 不改，分流只看 overall_risk），
        # 但审计链必须留下"判级缺失"痕迹，不得把"没有报告"粉饰为"核验通过"。
        patch["evidence_check"] = {"valid": True, "skipped": "planner_no_report",
                                   "reasons": (["llm_judgement_unavailable"]
                                               if backfill_error else [])}

    # 转人工收紧（2026-09-29 用户语义）：低风险（提示级）识别成功即按提示通道收尾，
    # 不因引用未证实目标/LLM 谨慎请求而转人工；高风险（告警通道）保留这两道防线
    if final_risk != "高风险":
        reasons = [r for r in reasons
                   if r not in ("cited_unverified_target", "llm_requested")]

    if human_reason:
        reasons.append("planner_requested_human")
        patch["planner_human_reason"] = human_reason

    # ---- 判断结果二值化（2026-09-29 用户定稿，代码侧守门，LLM 不可绕过）----
    # 成功 = 得到确定等级（安全/低风险/高风险）→ 执行对应处置，绝不转人工；
    # 失败 = 判不了 → P3_manual_judge，且不得输出任何等级。
    # 两条收紧规则：
    #   ① 已确认"高风险"即使 planner 请求人工也照常强制告警（有危险必须告警）；
    #      条款缺口只记审计标记 clause_support_missing，不得据此降级；
    #   ② 判不了（无等级，或 planner 明确声明"无法研判"）→ 丢弃已有等级，纯转人工，
    #      禁止"判了等级又转人工"。
    if final_risk == "高风险":
        disposition, need, outcome_risk = "P1_auto_alert", False, "高风险"
        tip_mode = bool((report or {}).get("_meta", {}).get("retrieval_mode") == "tip_mode")
        if report is None or tip_mode:
            reasons.append("clause_support_missing")
            patch["route"] = "full"
    elif human_reason or final_risk not in VALID_RISKS:
        disposition, need, outcome_risk = "P3_manual_judge", True, None
        patch["route"] = "manual"
        if gate_open and not recrop_used:
            # 归因：分歧被显式记录 → 属"判断失败(可定位)"，下游自主结案 + 抽样复核
            reasons = list(reasons) + ["unadjudicated_conflict"] + gate_open
        elif human_reason:
            reasons = list(reasons) + ["model_undecidable"]
    else:
        disposition = {"低风险": "TIP_optional", "安全": "P4_archive"}[final_risk]
        need, outcome_risk = False, final_risk
    if reasons:
        patch["review_reasons"] = reasons

    if gateway_failed:
        patch["plan_fallback"] = True     # 节点层回退确定性路由
    ra = {"overall_risk": outcome_risk,
          "n_warnings": (0 if outcome_risk is None
                         else len((report or {}).get("warnings", []) or [])),
          "need_human_review": need}
    patch.update({"risk_assessment": ra, "disposition": disposition,
                  "need_human_review": need})
    return patch
