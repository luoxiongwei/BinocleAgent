# -*- coding: utf-8 -*-
"""阶段二单 Agent 状态机。

默认只回放已有结果，不调用模型 API；后续可在工具缺失时接入 ModelGateway。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.common import project_root, save_json, validate_schema
from tools.detection_tool import load_detections
from tools.fusion_tool import inspect_fusion
from tools.rag_tool import load_rag_meta
from tools.report_tool import load_warning_results
from tools.task_state import new_task
from tools.vlm_tool import load_scene_results, validate_scene


def classify_disposition(state: dict[str, Any]) -> str:
    """三级处置（2026-09-29 修订：删除 P2_urgent_human）：

    - 高风险-强制告警 → P1_auto_alert，无条件。已确认的高风险必须告警，
      证据链/配准质量异常只作为审计标记（anomalies）附在告警上，不得据此降级处置；
    - 低风险-可选提示 → TIP_optional（推荐性提示，不引用条款、不强制告警）；
    - 安全-记录 → P4_archive（静默归档，仅进抽样校验队列）；
    - 无等级（overall_risk 为 None，即判不了）→ P3_manual_judge；
      上游已拦截为 manual 路线且不写任何风险等级，禁止"判了等级又转人工"。
    """
    risk = (state.get("risk_assessment", {}) or {}).get("overall_risk")
    if risk == "高风险":
        return "P1_auto_alert"
    if risk == "低风险":
        return "TIP_optional"
    if risk == "安全":
        return "P4_archive"
    return "P3_manual_judge"


def record_work_type_memory(state: dict[str, Any], project: Path) -> dict | None:
    """作业类型记忆（L3 记忆层——输入为零散无关联图，因此只保留作业类型维度）。

    不做人物身份跟踪、不做跨图场景关联（零散图无同一场景连续性假设）。
    记录内容：每次 live 任务的 dominant_state / site_type 及其置信依据，
    用途：(1) 检索查询扩展的统计先验；(2) 快速模式预筛的作业类型分布；(3) 数据集画像。
    """
    scene = state.get("scene", {}) or {}
    s = scene.get("scene", {}) or {}
    if not (s.get("dominant_state") or s.get("site_type")):
        return None
    memory_path = project / "results" / "agent" / "work_type_memory.json"
    try:
        records = json.loads(memory_path.read_text(encoding="utf-8")) if memory_path.is_file() else []
    except (OSError, json.JSONDecodeError):
        records = []
    record = {
        "task_id": state.get("task_id"),
        "dominant_state": s.get("dominant_state"),
        "dominant_state_basis": s.get("dominant_state_basis"),
        "site_type": s.get("site_type"),
        "recorded_at": state.get("audit", {}).get("updated_at"),
    }
    records = [r for r in records if r.get("task_id") != record["task_id"]]
    records.append(record)
    save_json(memory_path, records)
    return record


class SafetyAgent:
    """历史 FSM 兼容入口。

    生产任务统一由 agent.graph 编排；本类只服务于旧 replay 数据和兼容调用，
    避免新功能继续分叉出第二套生产语义。
    """
    def __init__(self, *, fusion_threshold: float | None = None,
                 fusion_hard_min: float | None = None, max_retries: int | None = None,
                 project: str | Path | None = None):
        self.project = Path(project) if project else project_root()
        # 阈值统一从 config/agent.yaml 读取（参数显式传入时优先）；此前 yaml 未接线，实际一直用默认值
        cfg = {}
        yml = self.project / "config" / "agent.yaml"
        if yml.is_file():
            try:
                import yaml
                cfg = (yaml.safe_load(yml.read_text(encoding="utf-8")) or {}).get("thresholds", {}) or {}
            except Exception:
                cfg = {}
        self.fusion_threshold = float(fusion_threshold if fusion_threshold is not None
                                      else cfg.get("fusion_edge_align_min", 0.15))
        # 硬底线：低于它视为配准失败（低于随机错位对照水平），即使放宽主阈值也拦下
        self.fusion_hard_min = float(fusion_hard_min if fusion_hard_min is not None
                                     else cfg.get("fusion_edge_align_hard_min", 0.09))
        self.max_retries = int(max_retries if max_retries is not None
                               else cfg.get("max_tool_retries", 1))

    def _step(self, state: dict[str, Any], name: str, status: str = "ok", **details: Any) -> None:
        state["workflow"]["current_step"] = name
        if status in {"ok", "review"} and name not in state["workflow"]["completed_steps"]:
            state["workflow"]["completed_steps"].append(name)
        state["audit"]["tool_calls"].append({"step": name, "status": status, **details})

    def run_replay(self, *, task_id: str = "replay-001") -> dict[str, Any]:
        """使用当前 results 目录完成一次端到端离线 Agent 回放。"""
        state = new_task(task_id, input_data={"mode": "offline_replay"})
        state["workflow"]["status"] = "running"

        fusion = inspect_fusion()
        self._step(state, "FUSE_AND_QC", fusion.status, error=fusion.error)
        if fusion.status != "ok":
            return self._fail(state, fusion.error or "融合 QC 失败")
        summary = fusion.data.get("summary", {})
        score = summary.get("edge_align_min")
        score_f = float(score) if score is not None else None
        if score_f is None:
            passed, flag = False, "missing_qc"
        elif score_f < self.fusion_hard_min:
            passed, flag = False, "below_hard_floor"          # 配准失败，必须人工
        elif score_f < self.fusion_threshold:
            passed, flag = True, "borderline_qc"              # 放宽区间：可用但审计标记
        else:
            passed, flag = True, "ok"
        state["quality"] = {
            "pairs": summary.get("n_pairs"),
            "edge_align_mean": summary.get("edge_align_mean"),
            "edge_align_min": score,
            "passed": passed, "flag": flag,
            "threshold": self.fusion_threshold, "hard_min": self.fusion_hard_min,
        }
        if not state["quality"]["passed"]:
            state["workflow"]["need_human_review"] = True
            state["workflow"]["status"] = "needs_human_review"

        detections = load_detections()
        self._step(state, "DETECT_OBJECTS", detections.status, error=detections.error)
        if detections.status != "ok":
            return self._fail(state, detections.error or "检测结果加载失败")
        state["detections"] = detections.data

        scenes = load_scene_results()
        self._step(state, "VALIDATE_SCENE", scenes.status, error=scenes.error)
        if scenes.status != "ok":
            return self._fail(state, scenes.error or "VLM 结果加载失败")
        valid, invalid = 0, 0
        scene_errors: list[dict[str, str]] = []
        for image_id, scene in scenes.data.get("scenes", {}).items():
            detection_map = detections.data.get("detections", {})
            detection_key = f"{image_id}.jpg"
            expected = len(detection_map[detection_key]) if detection_key in detection_map else None
            ok, error = validate_scene(scene, expected)
            if ok:
                valid += 1
            else:
                invalid += 1
                scene_errors.append({"image_id": image_id, "error": error})
        state["scene"] = {"n_scenes": len(scenes.data.get("scenes", {})),
                           "valid": valid, "invalid": invalid,
                           "errors": scene_errors}
        if invalid:
            state["workflow"]["need_human_review"] = True

        rag = load_rag_meta()
        self._step(state, "RETRIEVE_REGULATIONS", rag.status, error=rag.error)
        if rag.status != "ok":
            return self._fail(state, rag.error or "RAG 结果加载失败")
        state["regulations"] = rag.data

        report = load_warning_results()
        self._step(state, "LOAD_REPORT", report.status, error=report.error)
        if report.status != "ok":
            return self._fail(state, report.error or "风险报告加载失败")
        state["report"] = report.data
        state["risk_assessment"] = {
            "n_reports": len(report.data.get("warnings", {})),
            "report_stats": report.data.get("stats", {}),
            "evidence_valid": invalid == 0,
        }
        self._step(state, "FINALIZE")
        state["workflow"]["status"] = (
            "needs_human_review" if state["workflow"]["need_human_review"] else "completed"
        )
        return state

    def _run_tool(self, state: dict[str, Any], name: str, cmd: list[str],
                  run_dir: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess | None:
        log_path = run_dir / f"{name.lower().replace('+', '_')}.log"
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", env=env, timeout=900, check=False)
                log_path.write_text((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""),
                                    encoding="utf-8")
                ok = proc.returncode == 0
                self._step(state, name, "ok" if ok else "error", attempt=attempt + 1,
                           returncode=proc.returncode, elapsed_seconds=round(time.perf_counter() - started, 3),
                           log=str(log_path))
                if ok:
                    return proc
                if attempt < self.max_retries:
                    state["workflow"]["retry_count"] += 1
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._step(state, name, "error", attempt=attempt + 1, error=str(exc))
                if attempt < self.max_retries:
                    state["workflow"]["retry_count"] += 1
        return None

    @staticmethod
    def _fail(state: dict[str, Any], error: str) -> dict[str, Any]:
        state["workflow"]["status"] = "failed"
        state["workflow"]["need_human_review"] = True
        state["workflow"]["error"] = error
        state["workflow"]["reason"] = "工具或流程失败，禁止生成自动安全结论，需人工检查"
        state["workflow"]["current_step"] = "FAILED"
        # 失败 = 判不了：不输出任何风险等级（禁止"安全 + 转人工"矛盾态）
        state["risk_assessment"] = {"overall_risk": None, "need_human_review": True}
        state["audit"]["tool_calls"].append({"step": "FAILED", "status": "error", "error": error})
        return state


def save_state(state: dict[str, Any], path: str | Path | None = None) -> Path:
    valid, error = validate_schema(state, "task")
    if not valid:
        raise ValueError(f"Agent task state schema 校验失败: {error}")
    target = Path(path) if path else project_root() / "results" / "agent" / "replay_state.json"
    save_json(target, state)
    return target
