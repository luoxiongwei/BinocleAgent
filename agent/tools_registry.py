# -*- coding: utf-8 -*-
"""Agent 工具注册表（阶段一目标的结构化工具层，本文件为 LangGraph 骨架的执行体）。

约定（docs/agent化升级.md §5）：
  - 每个工具接受 JSON 兼容参数，返回 {"status": ok|error|review, "data": ..., "error": ...}；
  - 纯 Python 工具进程内执行；VLM/YOLO/LLM 沿用已验证的脚本入口（子进程），
    骨架稳定后可逐步进程内化——对规划器透明；
  - 工具不修改其他工具的结果，审计由图节点写入 state.tool_calls。

REGISTRY 供图节点按名调用；Step-2 规划器将把 REGISTRY 暴露为 function-calling 工具。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from tools.common import load_json, project_root
from tools.reconcile import center_distance, iou as box_iou, position_match

PROJECT = project_root()
PY = sys.executable


_CODE_DEFAULT_TH = {"fusion_edge_align_min": 0.15, "fusion_edge_align_hard_min": 0.09}


def _thresholds() -> tuple[dict[str, float], list[str]]:
    """融合 QC 阈值（`config/agent.yaml` thresholds 段）。

    2026-09-29 修复：此前配置缺失/损坏会**静默**回退到代码默认值，而代码默认值
    （0.15）与 yaml 现值（0.11）不一致 —— 质量门被无声改变且审计无记录，
    直接制造"行为变了却说不清为什么"的不可判定态。现返回 warnings，
    由调用方判为阈值不可用（拒绝使用未记录的默认值）。
    """
    warnings: list[str] = []
    cfg: dict = {}
    yml = PROJECT / "config" / "agent.yaml"
    if not yml.is_file():
        warnings.append(f"config_missing:{yml.name}")
    else:
        try:
            import yaml
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            cfg = data.get("thresholds") or {}
            if not cfg:
                warnings.append("config_section_missing:thresholds")
        except Exception as exc:
            warnings.append(f"config_unreadable:{type(exc).__name__}")
    th = {k: float(cfg.get(k, v)) for k, v in _CODE_DEFAULT_TH.items()}
    return th, warnings


def _run_tool(name: str, cmd: list[str], run_dir: Path, env: dict[str, str] | None = None,
              timeout: int = 900) -> dict[str, Any]:
    """子进程工具执行体：日志落 run_dir，返回规范化结果。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{name}.log"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=timeout, check=False)
        log_path.write_text((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""),
                            encoding="utf-8")
        if proc.returncode != 0:
            return {"status": "error", "error": f"{name} exit={proc.returncode}，日志 {log_path}"}
        return {"status": "ok", "data": {"log": str(log_path)}}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error": f"{name}: {exc}"}


# ---------- 工具实现 ----------

def fuse_qc(pair_id: str, run_dir: str = "") -> dict[str, Any]:
    """推理前标准化/自动配准 RGB-IR，再按阈值执行融合质量检查。"""
    from tools.image_registration import prepare_pair
    preparation: dict[str, Any] = {}
    try:
        preparation = prepare_pair(pair_id, PROJECT)
    except FileNotFoundError:
        # 兼容已经生成的融合图：没有原始 RGB/IR 时仍可做 QC，不能假造配准成功。
        preparation = {"registration": {"method": "precomputed_fused"}}
    except Exception as exc:
        return {"status": "error", "error": f"输入标准化或配准失败: {exc}"}
    qc_path = PROJECT / "results" / "fuse_qc.json"
    fused = PROJECT / "results" / "fused" / f"{pair_id}.jpg"
    if not fused.is_file():
        return {"status": "error", "error": f"融合图不存在: {fused}"}
    qc = load_json(qc_path) if qc_path.is_file() else {}
    pair = next((x for x in qc.get("pairs", []) if x.get("id") == pair_id), {})
    th, cfg_warnings = _thresholds()
    score = pair.get("edge_align")
    score_f = float(score) if score is not None else None
    if cfg_warnings:
        # 阈值来源不可信：不得静默用默认值通过质量门
        passed, flag = False, "config_unavailable"
    elif score_f is None:
        passed, flag = False, "missing_qc"
    elif score_f < th["fusion_edge_align_hard_min"]:
        passed, flag = False, "below_hard_floor"
    elif score_f < th["fusion_edge_align_min"]:
        passed, flag = True, "borderline_qc"
    else:
        passed, flag = True, "ok"
    data: dict[str, Any] = {"pair_id": pair_id, "edge_align": score, "passed": passed, "flag": flag,
                            "threshold": th["fusion_edge_align_min"],
                            "hard_min": th["fusion_edge_align_hard_min"],
                            "registration": preparation.get("registration", {}),
                            "ir_native": preparation.get("ir_native"),
                            "rgb_native": preparation.get("rgb_native"),
                            "canon": preparation.get("canon")}
    if cfg_warnings:
        data["config_warnings"] = cfg_warnings
    return {"status": "ok" if passed else "review",
            "data": data,
            "error": "; ".join(cfg_warnings) or (None if passed else flag)}


def prepare_pair_tool(pair_id: str, run_dir: str = "") -> dict[str, Any]:
    """独立调用 RGB/IR 标准化与 ECC 仿射配准工具。"""
    from tools.image_registration import prepare_pair
    try:
        return {"status": "ok", "data": prepare_pair(pair_id, PROJECT)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def vlm_open_detect(pair_id: str, run_dir: str) -> dict[str, Any]:
    """VLM 先行开放检测：产出 l2_scene.json（含 equipment_state）。"""
    l2_dir = Path(run_dir) / "l2"
    cmd = [PY, str(PROJECT / "scripts" / "vlm_judge.py"), "--run", "--pair-id", pair_id,
           "--fused-dir", str(PROJECT / "results" / "fused"),
           "--panel-dir", str(PROJECT / "results" / "panels"),
           "--out", str(l2_dir)]
    res = _run_tool("vlm", cmd, Path(run_dir))
    l2_json = l2_dir / "l2_scene.json"
    if res["status"] != "ok" or not l2_json.is_file():
        return {"status": "error", "error": res.get("error") or "VLM 未产出场景 JSON"}
    scene = load_json(l2_json).get("scenes", {}).get(pair_id, {})
    valid = isinstance(scene.get("targets"), list) and not scene.get("_meta", {}).get("parsing_error")
    return {"status": "ok" if valid else "review",
            "data": {"scene": scene, "l2_json": str(l2_json)},
            "error": scene.get("_meta", {}).get("validate_error") or ("" if valid else "VLM 结果无效")}


def yolo_detect(pair_id: str, run_dir: str) -> dict[str, Any]:
    """GDYOLO 独立检测（融合图输入，独立第二观察者）。"""
    run_dir = Path(run_dir)
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    fused = PROJECT / "results" / "fused" / f"{pair_id}.jpg"
    shutil.copy2(fused, input_dir / fused.name)
    l1_dir = run_dir / "l1"
    cmd = [PY, str(PROJECT / "scripts" / "l1_smoke.py"), "--source", str(input_dir),
           "--weights", str(PROJECT / "data" / "weights" / "best.pt"), "--out", str(l1_dir)]
    res = _run_tool("gdyolo", cmd, run_dir)
    l1_json = l1_dir / "l1_detections.json"
    if res["status"] != "ok" or not l1_json.is_file():
        return {"status": "error", "error": res.get("error") or "GDYOLO 未产出检测 JSON"}
    boxes = load_json(l1_json).get("detections", {}).get(f"{pair_id}.jpg", [])
    return {"status": "ok", "data": {"objects": boxes, "l1_json": str(l1_json)}}


def reconcile(pair_id: str, run_dir: str) -> dict[str, Any]:
    """对账补充：VLM targets × GDYOLO 框 → yolo_only 追问 + vlm_only 标记 + 定向重查。"""
    run_dir = Path(run_dir)
    l2_json = run_dir / "l2" / "l2_scene.json"
    l1_json = run_dir / "l1" / "l1_detections.json"
    if not (l2_json.is_file() and l1_json.is_file()):
        return {"status": "error", "error": "对账输入缺失（l2/l1 JSON）"}
    cmd = [PY, str(PROJECT / "scripts" / "vlm_judge.py"), "--run", "--reconcile",
           "--pair-id", pair_id, "--scene-json", str(l2_json), "--yolo-det", str(l1_json),
           "--fused-dir", str(PROJECT / "results" / "fused"),
           "--panel-dir", str(PROJECT / "results" / "panels"), "--out", str(run_dir / "l2")]
    res = _run_tool("reconcile", cmd, run_dir)
    if res["status"] != "ok" or not l2_json.is_file():
        return {"status": "error", "error": res.get("error") or "对账失败"}
    scene = load_json(l2_json).get("scenes", {}).get(pair_id, {})
    meta = scene.get("_meta", {}) or {}
    return {"status": "ok", "data": {"scene": scene, "reconciliation": meta.get("reconciliation", {}),
                                     "supplement": meta.get("supplement", {})}}


def verify_equipment_state(scene: dict, detections: dict) -> dict[str, Any]:
    """设备运行状态几何校验（2026-09-29 用户新增工具）：工人框 ⊆ 机械框 ⇒ 在操作 ⇒ 运行中。

    YOLO 负责主体存在性（工人/机械框），VLM 的 equipment_state 与几何证据对账：
      - 几何在机 + VLM 说静止/不确定 → 硬性不一致，建议复核；
      - VLM 说运行中 + 几何无人在机 → 软性提示（司机可能被遮挡，不作硬冲突）；
      - 机械目标无 YOLO 匹配 → 无法几何验证，标注 unverified。
    纯几何计算，不调任何模型。
    """
    objects = (detections or {}).get("objects", []) or []
    yolo_workers = [o for o in objects if o.get("class_id") == 0 and o.get("bbox_xyxy")]
    yolo_equip = [o for o in objects if o.get("class_id") in (1, 2) and o.get("bbox_xyxy")]
    vlm_targets = [t for t in (scene or {}).get("targets", []) or []
                   if isinstance(t, dict) and t.get("type") in ("机械", "塔吊")]

    def worker_on_machine(mbox: list) -> bool:
        mx1, my1, mx2, my2 = [float(v) for v in mbox]
        for w in yolo_workers:
            x1, y1, x2, y2 = [float(v) for v in w["bbox_xyxy"]]
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if mx1 - 5 <= cx <= mx2 + 5 and my1 - 5 <= cy <= my2 + 5:
                return True
        return False

    def best_yolo_match(t: dict) -> dict | None:
        """找到该 VLM 设备目标对应的 YOLO 框（**位置匹配**，不看框大小）。"""
        tb = t.get("bbox_xyxy")
        if not tb or len(tb) != 4:
            return None
        tb = [float(x) for x in tb]
        best, best_dist = None, None
        for o in yolo_equip:
            ob = [float(x) for x in o["bbox_xyxy"]]
            if not position_match(tb, ob, 0.5):
                continue
            d = center_distance(tb, ob)
            if best_dist is None or d < best_dist:
                best, best_dist = o, d
        return best

    equipments, hard_mismatch = [], False
    for t in vlm_targets:
        vlm_state = ((t.get("equipment_state") or {}).get("value")) or "不确定"
        mbox = (best_yolo_match(t) or {}).get("bbox_xyxy")
        if mbox is None:
            equipments.append({"bbox_id": t.get("bbox_id"), "vlm_state": vlm_state,
                               "yolo_matched": False, "worker_on_machine": None,
                               "consistent": None, "note": "无YOLO匹配，几何不可验证"})
            continue
        on_machine = worker_on_machine(mbox)
        if on_machine and vlm_state in ("静止", "不确定"):
            consistent, note, hard = False, "几何在机但VLM未判运行中", True
        elif vlm_state == "运行中" and not on_machine:
            consistent, note, hard = True, "VLM判运行中但几何无人在机（司机可能遮挡，软提示）", False
        else:
            consistent, note, hard = True, "", False
        hard_mismatch |= hard
        equipments.append({"bbox_id": t.get("bbox_id"), "vlm_state": vlm_state,
                           "yolo_matched": True, "worker_on_machine": on_machine,
                           "consistent": consistent, "note": note})
    return {"status": "review" if hard_mismatch else "ok",
            "data": {"equipments": equipments, "hard_mismatch": hard_mismatch}}


def yolo_recheck(pair_id: str, run_dir: str, conf: float = 0.15,
                 region: list | None = None) -> dict[str, Any]:
    """定点二次检测（Step-3 规划器监控工具）：更低置信度重跑 GDYOLO，可限定画布区域。

    结果仅作规划器研判证据，不改写 state.detections/对账基线；每次规划由执行器限一次。
    """
    run_dir = Path(run_dir)
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    fused = PROJECT / "results" / "fused" / f"{pair_id}.jpg"
    if not fused.is_file():
        return {"status": "error", "error": f"融合图不存在: {fused}"}
    if not (input_dir / fused.name).is_file():
        shutil.copy2(fused, input_dir / fused.name)
    conf = min(0.5, max(0.05, float(conf)))
    l1_dir = run_dir / "l1_recheck"
    cmd = [PY, str(PROJECT / "scripts" / "l1_smoke.py"), "--source", str(input_dir),
           "--weights", str(PROJECT / "data" / "weights" / "best.pt"),
           "--conf", str(conf), "--out", str(l1_dir)]
    res = _run_tool("yolo_recheck", cmd, run_dir)
    l1_json = l1_dir / "l1_detections.json"
    if res["status"] != "ok" or not l1_json.is_file():
        return {"status": "error", "error": res.get("error") or "复检未产出检测 JSON"}
    boxes = load_json(l1_json).get("detections", {}).get(f"{pair_id}.jpg", [])
    if region and len(region) == 4:
        rx1, ry1, rx2, ry2 = [float(v) for v in region]
        kept = []
        for b in boxes:
            xy = b.get("bbox_xyxy") or []
            if len(xy) == 4:
                cx, cy = (xy[0] + xy[2]) / 2, (xy[1] + xy[3]) / 2
                if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                    kept.append(b)
        boxes = kept
    return {"status": "ok", "data": {"conf": conf, "region": region,
                                     "n_boxes": len(boxes), "boxes": boxes}}


def evaluate_risk_tool(scene: dict) -> dict[str, Any]:
    """规则引擎三要素判定（进程内，确定性）。"""
    from tools.rule_engine import evaluate_risk
    return {"status": "ok", "data": evaluate_risk(scene or {})}


def generate_judgement(pair_id: str, run_dir: str, mode: str = "full") -> dict[str, Any]:
    """LLM 判级：mode=tip（推荐提示，无条款）| full（RAG+条款警示）。"""
    run_dir = Path(run_dir)
    l2_json = run_dir / "l2" / "l2_scene.json"
    if not l2_json.is_file():
        return {"status": "error", "error": "l2_scene.json 不存在"}
    l3_dir = run_dir / "l3"
    cmd = [PY, str(PROJECT / "scripts" / "llm_judge.py"), "--run", "--limit", "1",
           "--scenes", str(l2_json), "--rag", str(PROJECT / "results" / "rag_store_v2"),
           "--out", str(l3_dir)]
    if mode == "tip":
        cmd.append("--tip")
    env = None
    if mode == "full":
        import os
        env = os.environ.copy()
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    res = _run_tool("rag_llm", cmd, run_dir, env=env)
    l3_json = l3_dir / "l3_warnings.json"
    if res["status"] != "ok" or not l3_json.is_file():
        return {"status": "error", "error": res.get("error") or "LLM 判级未产出 JSON"}
    report = load_json(l3_json).get("warnings", {}).get(pair_id, {})
    return {"status": "ok", "data": {"report": report}}


def validate_report_tool(report: dict, scene: dict, retrieval: dict,
                         check_clauses: bool = True) -> dict[str, Any]:
    """报告结构校验 + 证据链校验（tip 模式跳过条款门）。"""
    from tools.report_tool import validate_report
    from tools.evidence_tool import validate_report_evidence
    ok, error = validate_report(report or {})
    if not ok:
        return {"status": "review", "data": {"valid": False, "reasons": [error or "报告无效"]}}
    target_ids = {str(t.get("bbox_id")) for t in (scene or {}).get("targets", [])
                  if isinstance(t, dict) and t.get("bbox_id")}
    reasons: list[str] = []
    if check_clauses:
        evidence = validate_report_evidence(report, target_ids=target_ids,
                                            allowed_clauses=set((retrieval or {}).get("retrieved_clause_ids", [])))
        if not evidence.data.get("valid"):
            reasons.append("evidence_gate_failed")
        if (retrieval or {}).get("clause_out_of_scope"):
            reasons.append("clause_out_of_scope")
        if any(not item.get("clause_ids") for item in report.get("warnings", []) or []):
            reasons.append("warning_without_clause")
    unverified = {t.get("bbox_id") for t in (scene or {}).get("targets", [])
                  if isinstance(t, dict) and t.get("unverified_by_yolo")}
    for item in report.get("warnings", []) or []:
        text = json.dumps(item.get("evidence", []) or [], ensure_ascii=False)
        if any(u in text for u in unverified):
            reasons.append("cited_unverified_target")
            break
    return {"status": "review" if reasons else "ok",
            "data": {"valid": not reasons, "reasons": reasons}}


# ---------- 感知链监控工具（2026-09-29：让 LLM 能监控感知链并自主补充动作） ----------

def chain_health(snapshot: dict) -> dict[str, Any]:
    """感知链健康报告（纯函数，只读快照，不调模型）。

    snapshot（规划器从 state 摘取）：quality / scene / detections /
    reconciliation / equipment_check / rule / tool_calls。
    输出各环节状态 + 汇总 health（ok|degraded|failed）+ advice（建议动作）。
    """
    snap = snapshot or {}
    quality = snap.get("quality") or {}
    scene = snap.get("scene") or {}
    scene_meta = scene.get("_meta") or {}
    rec = snap.get("reconciliation") or {}
    equip = snap.get("equipment_check") or {}
    rule = snap.get("rule") or {}
    calls = snap.get("tool_calls") or []

    targets = [t for t in (scene.get("targets") or []) if isinstance(t, dict)]
    unverified = [t.get("bbox_id") for t in targets if t.get("unverified_by_yolo")]
    failures = [{"tool": c.get("tool"), "status": c.get("status"), "error": c.get("error")}
                for c in calls if isinstance(c, dict) and c.get("status") == "error"]
    conflicts = [str(c) for c in (rec.get("conflicts") or [])]
    n_yolo = int(rec.get("n_yolo") or len((snap.get("detections") or {}).get("objects") or []))
    vlm_only = int(rec.get("vlm_only") or 0)
    yolo_only = int(rec.get("yolo_only") or 0)
    if not conflicts:                       # 兜底：state 未标 conflicts 时直接推
        if vlm_only and n_yolo == 0:
            conflicts.append("vlm_only_no_yolo_support")
        if yolo_only:
            conflicts.append("yolo_only_unresolved")

    degraded = bool(scene_meta.get("degraded"))
    parsing_error = bool(scene_meta.get("parsing_error"))
    hard_mismatch = bool(equip.get("hard_mismatch"))
    # 设备运行状态未定的机械/塔吊（equipment_state 决定 R-ZONE 三岔路，是分级关键证据）
    equipment_pending = []
    for t in targets:
        if t.get("type") not in ("机械", "塔吊"):
            continue
        es = t.get("equipment_state")
        if not isinstance(es, dict) or (es.get("value") not in ("运行中", "静止")
                                        or float(es.get("confidence") or 0) < 0.5):
            equipment_pending.append(t.get("bbox_id"))

    health = "ok"
    if failures or degraded or parsing_error or quality.get("passed") is False:
        health = "failed"
    if health == "ok" and (conflicts or hard_mismatch or unverified):
        health = "degraded"

    advice: list[str] = []
    if degraded or parsing_error:
        advice.append("VLM 场景不可信：可 rerun_step(step=vlm) 重跑，或直接声明无法研判")
    if conflicts:
        advice.append("双源对账存在未裁决分歧：建议 recrop_verify(reason=...) 对分歧点做细粒度重感知")
    if hard_mismatch:
        advice.append("设备运行状态与几何证据冲突：建议 recrop_verify(fields=['equipment_state'])")
    if equipment_pending and not hard_mismatch:
        advice.append(f"设备运行状态未定 {equipment_pending}（决定 R-ZONE 三岔路）："
                      "建议 recrop_verify(fields=['equipment_state']) 做局部放大复核")
    if unverified:
        advice.append(f"存在未经 GDYOLO 证实的目标 {unverified}：若用作警示依据会被标注未经证实")
    if rule.get("evidence_gaps"):
        advice.append("规则引擎记录了证据缺口，可对相应字段做定向复核")
    if failures:
        advice.append("存在工具失败：可 rerun_step 重试对应环节（有限次）")

    return {"status": "ok", "data": {
        "health": health,
        "quality": {"flag": quality.get("flag"), "passed": quality.get("passed"),
                    "edge_align": quality.get("edge_align")},
        "perception": {"n_vlm_targets": len(targets), "n_yolo_objects": n_yolo,
                       "matched": rec.get("matched"), "vlm_only": vlm_only,
                       "yolo_only": yolo_only, "conflicts": conflicts},
        "equipment": {"hard_mismatch": hard_mismatch,
                      "state_pending": equipment_pending,
                      "mismatches": [e for e in (equip.get("equipments") or [])
                                     if isinstance(e, dict) and e.get("consistent") is False]},
        "rule": {"overall_risk": rule.get("overall_risk"),
                 "need_human_review": rule.get("need_human_review"),
                 "drivers": rule.get("drivers") or [],
                 "evidence_gaps": rule.get("evidence_gaps") or []},
        "scene_meta": {"degraded": degraded, "parsing_error": parsing_error,
                       "coords_clamped": scene_meta.get("coords_clamped"),
                       "targets_dropped": scene_meta.get("targets_dropped")},
        "unverified_ids": unverified, "failures": failures, "advice": advice,
    }}


def inspect_reconcile(snapshot: dict) -> dict[str, Any]:
    """对账明细深查（纯函数）：逐目标给出 VLM/YOLO 支撑与最佳 IoU，并列出未确认区域。"""
    snap = snapshot or {}
    scene = snap.get("scene") or {}
    rec = snap.get("reconciliation") or {}
    scene_meta = scene.get("_meta") or {}
    yolo_objects = [o for o in ((snap.get("detections") or {}).get("objects") or [])
                    if isinstance(o, dict) and len(o.get("bbox_xyxy") or []) == 4]

    targets = []
    for t in scene.get("targets") or []:
        if not isinstance(t, dict):
            continue
        bbox = t.get("bbox_xyxy") or []
        supported, best_iou, best_cls = False, 0.0, None
        if len(bbox) == 4:
            fb = [float(x) for x in bbox]
            for o in yolo_objects:
                ob = [float(x) for x in o["bbox_xyxy"]]
                best_iou = max(best_iou, box_iou(fb, ob))
                if position_match(fb, ob, 0.5):        # 位置匹配（不看框大小）
                    supported = True
                    best_cls = o.get("class_name")
        targets.append({"bbox_id": t.get("bbox_id"), "type": t.get("type"),
                        "unverified_by_yolo": bool(t.get("unverified_by_yolo")),
                        "source": t.get("_meta_src"),
                        "best_yolo_iou": round(best_iou, 3), "best_yolo_class": best_cls,
                        "yolo_supported": supported})

    yolo_unconfirmed = []
    for i, o in enumerate(yolo_objects, 1):
        ob = [float(x) for x in o["bbox_xyxy"]]
        hit = False
        for t in scene.get("targets") or []:
            tb = (t.get("bbox_xyxy") or []) if isinstance(t, dict) else []
            if len(tb) == 4 and position_match([float(x) for x in tb], ob, 0.5):
                hit = True
                break
        if not hit:
            yolo_unconfirmed.append({"yolo_index": i, "class_name": o.get("class_name"),
                                     "conf": o.get("conf"), "bbox_xyxy": o.get("bbox_xyxy")})

    unsupported = [t["bbox_id"] for t in targets if not t["yolo_supported"]]
    return {"status": "ok", "data": {
        "summary": rec,
        "conflicts": list(rec.get("conflicts") or []),
        "supplement": scene_meta.get("supplement") or {},
        "recheck": scene_meta.get("recheck") or {},
        "targets": targets,
        "vlm_targets_without_yolo": unsupported,
        "yolo_targets_without_vlm": yolo_unconfirmed,
        "advice": ("存在双源未互相确认的区域：建议 recrop_verify 对这些区域做局部放大重感知后再裁决"
                   if (unsupported or yolo_unconfirmed) else ""),
    }}


_RERUN_STEPS = {"vlm": "vlm_open_detect", "yolo": "yolo_detect", "reconcile": "reconcile"}


def rerun_step(pair_id: str, run_dir: str, step: str) -> dict[str, Any]:
    """单步重跑（感知链监控工具）：只重跑一个环节，**不写回 state**。

    白名单 vlm / yolo / reconcile（规则引擎与几何校验是纯计算，无需重跑）。
    重跑产物落 `run_dir/_rerun/<step>/`，不污染本次运行的 l2/l1 基线；
    调用次数由规划器侧限次（每次规划最多一次）。
    """
    key = str(step or "").lower()
    if key not in _RERUN_STEPS:
        return {"status": "error",
                "error": f"不支持重跑的环节: {step}（可选 {sorted(_RERUN_STEPS)}）"}
    probe = Path(run_dir) / "_rerun" / key
    if key == "reconcile":                  # 对账需要输入，先复制基线产物到 probe
        for sub, name in (("l2", "l2_scene.json"), ("l1", "l1_detections.json")):
            src = Path(run_dir) / sub / name
            if not src.is_file():
                return {"status": "error", "error": f"重跑 reconcile 需要 {src} 存在"}
            dst = probe / sub / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    res = call(_RERUN_STEPS[key], pair_id, str(probe))
    return {"status": res.get("status"), "error": res.get("error"),
            "data": {"step": key, "tool": _RERUN_STEPS[key], "result": res.get("data"),
                     "note": "重跑结果仅作研判证据，不写回 state"}}


def recrop_verify(pair_id: str, run_dir: str, targets: list | None = None,
                  fields: list | None = None, reason: str = "") -> dict[str, Any]:
    """定向重感知复核（回应"感知与校验不合时应能细粒度重来一次"）。

    不重跑整图开放检测，只针对**分歧点 / 不确定字段**做局部放大重感知：
    对指定目标（或全部分歧目标）裁剪放大重查 ppe/body_equipment/equipment_state，
    必要时对 GDYOLO 未确认区域追问补漏，最后**重算对账**并落盘到 `run_dir/recrop/`。
    结果仅作研判证据，由规划器决定是否据此修正结论（不静默改写 state）。
    """
    run_dir = Path(run_dir)
    l2_json = run_dir / "l2" / "l2_scene.json"
    l1_json = run_dir / "l1" / "l1_detections.json"
    if not l2_json.is_file():
        return {"status": "error", "error": f"缺少 {l2_json}，无法定向复核"}
    out_dir = run_dir / "recrop"
    cmd = [PY, str(PROJECT / "scripts" / "vlm_judge.py"), "--recrop",
           "--pair-id", pair_id, "--scene-json", str(l2_json),
           "--fused-dir", str(PROJECT / "results" / "fused"),
           "--panel-dir", str(PROJECT / "results" / "panels"),
           "--out", str(out_dir)]
    if l1_json.is_file():
        cmd += ["--yolo-det", str(l1_json)]
    if targets:
        cmd += ["--targets", ",".join(str(t) for t in targets)]
    if fields:
        cmd += ["--fields", ",".join(str(f) for f in fields)]
    if reason:
        cmd += ["--reason", str(reason)[:120]]
    res = _run_tool("recrop", cmd, run_dir)
    out_json = out_dir / "l2_scene.json"
    if res["status"] != "ok" or not out_json.is_file():
        return {"status": "error", "error": res.get("error") or "定向复核未产出结果"}
    scene = load_json(out_json).get("scenes", {}).get(pair_id, {})
    meta = scene.get("_meta") or {}
    return {"status": "ok", "data": {"scene": scene,
                                     "recrop": meta.get("recrop") or {},
                                     "recheck": meta.get("recheck") or {},
                                     "reconciliation": meta.get("reconciliation") or {}}}


# ---------- 注册表 ----------

REGISTRY: dict[str, dict[str, Any]] = {
    # name: {fn, desc, kind}  kind: pure=进程内 / proc=子进程脚本（骨架期，后续进程内化）
    "prepare_pair": {"fn": prepare_pair_tool, "kind": "proc",
                      "desc": "RGB/IR 输入标准化与 ECC 仿射配准，生成统一坐标系产物"},
    "fuse_qc": {"fn": fuse_qc, "kind": "proc",
                "desc": "融合配准 QC，返回 {passed, edge_align, flag}"},
    "vlm_open_detect": {"fn": vlm_open_detect, "kind": "proc",
                        "desc": "VLM 先行开放检测，枚举目标+装备+equipment_state"},
    "yolo_detect": {"fn": yolo_detect, "kind": "proc",
                    "desc": "GDYOLO 独立检测（第二观察者）"},
    "yolo_recheck": {"fn": yolo_recheck, "kind": "proc",
                     "desc": "定点二次检测（低置信度/限定区域），仅作规划器证据不改对账基线"},
    "reconcile": {"fn": reconcile, "kind": "proc",
                  "desc": "VLM×YOLO 对账：补漏/剔误检/unverified 标记/定向重查"},
    "verify_equipment_state": {"fn": verify_equipment_state, "kind": "pure",
                               "desc": "设备运行状态几何校验（工人框⊆机械框⇒在机）"},
    "evaluate_risk": {"fn": evaluate_risk_tool, "kind": "pure",
                      "desc": "规则引擎三要素确定性判级"},
    "generate_judgement": {"fn": generate_judgement, "kind": "proc",
                           "desc": "LLM 判级（mode=tip 推荐提示 / full 条款警示）"},
    "validate_report": {"fn": validate_report_tool, "kind": "pure",
                        "desc": "报告结构与证据链校验"},
    # ---- 感知链监控（2026-09-29：规划器可自主监控并补充动作） ----
    "chain_health": {"fn": chain_health, "kind": "pure",
                     "desc": "感知链健康报告（各步状态/降级/对账分歧/建议动作）"},
    "inspect_reconcile": {"fn": inspect_reconcile, "kind": "pure",
                          "desc": "对账明细深查（逐目标 vlm/yolo 支撑与 IoU、未确认区域）"},
    "rerun_step": {"fn": rerun_step, "kind": "proc",
                   "desc": "单步重跑（step=vlm|yolo|reconcile），结果不写回 state，限次"},
    "recrop_verify": {"fn": recrop_verify, "kind": "proc",
                      "desc": "定向重感知复核：对分歧点/不确定字段局部放大重查并重算对账"},
}


def call(name: str, *args, **kwargs) -> dict[str, Any]:
    """按名调用工具（图节点统一入口；Step-2 规划器复用同一入口接 function-calling）。"""
    tool = REGISTRY.get(name)
    if tool is None:
        return {"status": "error", "error": f"未知工具: {name}"}
    return tool["fn"](*args, **kwargs)
