# -*- coding: utf-8 -*-
"""控制台只读数据 API（FastAPI APIRouter）。

设计约束（2026-09-29）：
  - **只读**：不触发任何模型调用、不写任何产物（唯一的写操作是「标记待审已处理」，
    它只改 pending_reviews.json 的 resolved 字段）；
  - **不依赖图/模型**：不 import graph，故无需 API key 即可启动与测试；
  - **路径安全**：run / pair 参数必须匹配 `[A-Za-z0-9_-]`，杜绝目录穿越。

指标口径（与 agent/acceptance_report.py 完全一致，复用其函数）：
  判断结果二值 —— 成功(安全/低风险/高风险) 或 失败(转人工)；
  判重/判轻仅在「双方都判断成功」时可比；转人工不参与严重度比较。
额外给出 `false_safe`（专家判高风险 / 系统判安全-记录）—— 最该被盯住的漏报。

独立进程启动：uvicorn agent.api_web:standalone --port 8010（与 :8000 任务服务互为独立进程，
本模块不 import graph/service，故无需 API key 即可启动与测试；两端口经 Vite 代理分流）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent.acceptance_report import _sev_of, golden_action, system_action
from agent.outcome import RISK_RANK
from tools.common import project_root, save_json

PROJECT = project_root()
AGENT_ROOT = PROJECT / "results" / "agent"
GOLDEN_PATH = PROJECT / "results" / "golden_set" / "golden_set.json"
PENDING_PATH = AGENT_ROOT / "pending_reviews.json"
HUMAN_ARCHIVE_PATH = AGENT_ROOT / "human_archives.json"
MEMORY_PATH = AGENT_ROOT / "work_type_memory.json"
WEB_DIR = PROJECT / "web"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# 处置 → 前端色板语义（转人工是「判断失败」，不是危险等级）
DISPOSITION_META = {
    "P1_auto_alert": {"label": "高风险-强制告警", "tone": "alert"},
    "TIP_optional": {"label": "低风险-可选提示", "tone": "tip"},
    "P4_archive": {"label": "安全-记录", "tone": "safe"},
    "P3_manual_judge": {"label": "转人工-判断失败", "tone": "fail"},
    "P3_failed_closed": {"label": "历史结果-需迁移", "tone": "fail"},
    "P2_urgent_human": {"label": "转人工(legacy)", "tone": "fail"},
    "P3_routine_review": {"label": "常规复核(legacy)", "tone": "fail"},
}

STEP_CN = {
    "FUSE_AND_QC": "配准质检", "VLM_DETECT": "VLM 开放检测", "GDYOLO_DETECT": "GDYOLO 检测",
    "RECONCILE": "双源对账", "VERIFY_EQUIPMENT": "设备状态校验", "JUDGE": "规则判级",
    "REVIEW_GATE": "人工闸口", "MANUAL_JUDGE": "判不了收尾", "TIP": "提示级警示",
    "FULL": "条款级警示", "FINALIZE": "归档收尾", "FAILED_AUTO_CLOSED": "失败自主结案",
}

TOOL_CN = {
    "fuse_qc": "配准质检", "vlm_open_detect": "VLM 开放检测", "yolo_detect": "GDYOLO 检测",
    "reconcile": "双源对账", "verify_equipment_state": "设备状态校验", "evaluate_risk": "规则引擎",
    "generate_judgement": "生成警示", "validate_report": "报告校验", "planner": "规划器",
    "yolo_recheck": "定点二次检测", "chain_health": "链路健康", "inspect_reconcile": "对账深查",
    "rerun_step": "单步重跑", "recrop_verify": "定向重感知",
}


# ---------- 基础读取 ----------

def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _guard(name: str, what: str) -> str:
    if not name or not _SAFE_NAME.match(name):
        raise HTTPException(400, f"{what} 非法：只允许字母/数字/下划线/连字符")
    return name


def _golden_map() -> dict[str, dict]:
    data = _read_json(GOLDEN_PATH, {}) or {}
    return {e["pair_id"]: e for e in (data.get("entries") or []) if e.get("pair_id")}


def _run_roots() -> list[Path]:
    if not AGENT_ROOT.is_dir():
        return []
    out = []
    for d in sorted(AGENT_ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        if (d / "summary.json").is_file() or any(d.glob("full-pair*")):
            out.append(d)
    return out


def _pair_dirs(run_dir: Path) -> dict[str, Path]:
    return {p.name.replace("full-", ""): p for p in sorted(run_dir.glob("full-pair*")) if p.is_dir()}


def _sorted_pairs(ids: list[str]) -> list[str]:
    return sorted(ids, key=lambda x: int(x[4:]) if x[4:].isdigit() else 999)


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


# ---------- 指标（口径与 acceptance_report 严格一致） ----------

def _metrics(rows: list[dict], gmap: dict[str, dict]) -> dict[str, Any]:
    exact = tier_ok = failed = 0
    table: list[dict] = []
    for s in rows:
        pid = s.get("pair_id")
        g = gmap.get(pid) or {}
        sys_act = system_action(s)
        g_act = golden_action(g) or "未标注"
        risk = s.get("overall_risk")
        g_std = g.get("risk_std")
        if s.get("status") == "failed":
            mark, failed = "ERR", failed + 1
        elif sys_act == g_act:
            mark, exact = "V", exact + 1
        elif g_std and risk in RISK_RANK and g_std in RISK_RANK and RISK_RANK[risk] == RISK_RANK[g_std]:
            mark, tier_ok = "O", tier_ok + 1
        else:
            mark = "X"
        table.append({
            "pair_id": pid, "expert": g_act, "system": sys_act, "risk": risk,
            "mark": mark, "elapsed_s": s.get("elapsed_s"),
            "review_reasons": s.get("review_reasons") or [],
            "disposition": s.get("disposition"), "route": s.get("route"),
            "need_human_review": bool(s.get("need_human_review")),
            "scene_label": g.get("scene_label"), "notes": g.get("notes"),
        })

    def _cmp(a: str, b: str) -> int | None:
        sa, sb = _sev_of(a), _sev_of(b)
        return None if sa is None or sb is None else sa - sb

    over, under, false_safe = [], [], []
    for r in table:
        if r["mark"] in ("X", "O"):
            d = _cmp(r["system"], r["expert"])
            if d is not None and d > 0:
                over.append(r["pair_id"])
            elif d is not None and d < 0:
                under.append(r["pair_id"])
        if r["expert"] == "高风险-强制告警" and r["system"] == "安全-记录":
            false_safe.append(r["pair_id"])
    unjudged = [r["pair_id"] for r in table
                if r["mark"] != "ERR" and (r["system"] == "转人工" or r["expert"] == "转人工")]
    elapsed = [r["elapsed_s"] for r in table if isinstance(r["elapsed_s"], (int, float))]

    disps: dict[str, int] = {}
    risks: dict[str, int] = {}
    for r in table:
        disps[r["system"]] = disps.get(r["system"], 0) + 1
        key = r["risk"] or "未判定"
        risks[key] = risks.get(key, 0) + 1
    n = len(table)
    return {
        "n": n, "exact": exact, "tier_ok": tier_ok, "failed": failed,
        "inconsistent": n - exact - tier_ok - failed,
        "exact_rate": (exact / n) if n else None,
        "tier_rate": ((exact + tier_ok) / n) if n else None,
        "over": over, "under": under, "unjudged": unjudged, "false_safe": false_safe,
        "dispositions": disps, "risks": risks,
        "avg_s": (sum(elapsed) / len(elapsed)) if elapsed else None,
        "max_s": max(elapsed) if elapsed else None,
    }


def _run_entry(run_dir: Path, gmap: dict[str, dict]) -> dict[str, Any]:
    rows = _read_json(run_dir / "summary.json", []) or []
    if not isinstance(rows, list):
        rows = []
    if not rows:                      # 无 summary：从 graph_state 兜底重建
        rows = []
        for pid, d in _pair_dirs(run_dir).items():
            st = _read_json(d / "graph_state.json", {}) or {}
            rows.append({"pair_id": pid, "status": st.get("status") or "completed",
                         "disposition": st.get("disposition"),
                         "overall_risk": (st.get("risk_assessment") or {}).get("overall_risk"),
                         "need_human_review": st.get("need_human_review"),
                         "review_reasons": st.get("review_reasons") or [],
                         "route": st.get("route"), "elapsed_s": None})
        rows.sort(key=lambda r: _sorted_pairs([r["pair_id"]])[0])
    return {"run": run_dir.name, "rows": rows, "metrics": _metrics(rows, gmap),
            "updated": _mtime(run_dir / "summary.json"),
            "has_report": (run_dir / "acceptance_report.md").is_file()}


# ---------- 路由 ----------

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, Any]:
    cfg = project_root() / "config.json"
    schemas = sorted(p.name for p in (PROJECT / "schemas").glob("*.schema.json"))
    return {
        "status": "ok",
        "project": "施工安全巡检 Agent",
        "config_present": cfg.is_file(),
        "schemas": schemas,
        "n_runs": len(_run_roots()),
        "n_golden": len(_golden_map()),
        "n_pending": len(_read_json(PENDING_PATH, []) or []),
        "console_mounted": (WEB_DIR / "console.html").is_file(),
    }


@router.get("/overview")
def overview() -> dict[str, Any]:
    gmap = _golden_map()
    runs = [_run_entry(d, gmap) for d in _run_roots()]
    runs = [r for r in runs if r["metrics"]["n"]]
    runs.sort(key=lambda r: (r["updated"] or ""), reverse=True)
    pending = _read_json(PENDING_PATH, []) or []
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs": runs,
        "totals": {
            "n_runs": len(runs),
            "n_pairs": sum(r["metrics"]["n"] for r in runs),
            "n_pending": len(pending),
            "n_golden": len(gmap),
            "false_safe": sorted({p for r in runs for p in r["metrics"]["false_safe"]}),
            "under": sorted({p for r in runs for p in r["metrics"]["under"]}),
        },
        "golden_note": (_read_json(GOLDEN_PATH, {}) or {}).get("_说明"),
    }


@router.get("/runs")
def list_runs() -> dict[str, Any]:
    gmap = _golden_map()
    return {"runs": [{"run": d.name, **{k: v for k, v in _run_entry(d, gmap).items() if k != "rows"}}
                     for d in _run_roots()]}


@router.get("/task-archives")
def task_archives() -> dict[str, Any]:
    """返回在线 Agent 任务归档，供批次档案页展示最新任务结果。"""
    root = AGENT_ROOT / "graph_runs"
    entries: list[dict[str, Any]] = []
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir():
                continue
            state = _read_json(d / "graph_state.json", {}) or {}
            if not state:
                continue
            risk = (state.get("risk_assessment") or {}).get("overall_risk")
            entries.append({
                "task_id": state.get("task_id") or d.name,
                "pair_id": state.get("pair_id"),
                "status": state.get("status") or "completed",
                "disposition": state.get("disposition"),
                "overall_risk": risk,
                "need_human_review": bool(state.get("need_human_review")),
                "route": state.get("route"),
                "steps": state.get("steps") or [],
                "tool_calls": state.get("tool_calls") or [],
                "updated": _mtime(d / "graph_state.json"),
            })
    entries.sort(key=lambda e: e.get("updated") or "", reverse=True)
    return {"tasks": entries}


@router.get("/runs/{run}")
def run_detail(run: str) -> dict[str, Any]:
    _guard(run, "批次名")
    d = AGENT_ROOT / run
    if not d.is_dir():
        raise HTTPException(404, f"未知批次: {run}")
    return _run_entry(d, _golden_map())


@router.get("/runs/{run}/report")
def run_report(run: str) -> dict[str, Any]:
    _guard(run, "批次名")
    p = AGENT_ROOT / run / "acceptance_report.md"
    if not p.is_file():
        raise HTTPException(404, "该批次没有验收报告")
    return {"run": run, "markdown": p.read_text(encoding="utf-8")}


@router.get("/runs/{run}/pairs")
def run_pairs(run: str) -> dict[str, Any]:
    _guard(run, "批次名")
    d = AGENT_ROOT / run
    if not d.is_dir():
        raise HTTPException(404, f"未知批次: {run}")
    return {"run": run, "pairs": _sorted_pairs(list(_pair_dirs(d).keys()))}


@router.get("/runs/{run}/pairs/{pair}")
def pair_detail(run: str, pair: str) -> dict[str, Any]:
    _guard(run, "批次名")
    _guard(pair, "图对标识")
    pd = AGENT_ROOT / run / f"full-{pair}"
    if not pd.is_dir():
        raise HTTPException(404, f"批次 {run} 中不存在图对 {pair}")
    st = _read_json(pd / "graph_state.json", {}) or {}
    scene_doc = _read_json(pd / "l2" / "l2_scene.json", {}) or {}
    scene = (scene_doc.get("scenes") or {}).get(pair) or scene_doc.get("scene") or {}
    warn_doc = _read_json(pd / "l3" / "l3_warnings.json", {}) or {}
    warn = (warn_doc.get("warnings") or {}).get(pair) or {}
    summary_rows = _read_json(AGENT_ROOT / run / "summary.json", []) or []
    row = next((s for s in summary_rows if s.get("pair_id") == pair), {})
    ann = pd / "l2" / "annotated"
    base = f"/api/runs/{run}/pairs/{pair}/image"
    images = {}
    for kind, name in (("fused", f"{pair}_1fused.jpg"), ("visible", f"{pair}_2visible.jpg"),
                       ("infrared", f"{pair}_3infrared.jpg")):
        images[kind] = f"{base}/{kind}" if (ann / name).is_file() else None
    images["input"] = f"{base}/input" if (pd / "input" / f"{pair}.jpg").is_file() else None
    return {
        "run": run, "pair_id": pair,
        "golden": _golden_map().get(pair),
        "summary_row": row,
        "images": images,
        "quality": st.get("quality") or {},
        "scene": scene,
        "detections": st.get("detections") or {},
        "reconciliation": st.get("reconciliation") or {},
        "equipment_check": st.get("equipment_check") or {},
        "rule": st.get("rule") or {},
        "report": st.get("report"),
        "evidence_check": st.get("evidence_check"),
        "disposition": st.get("disposition"),
        "risk_assessment": st.get("risk_assessment") or {},
        "route": st.get("route"),
        "need_human_review": st.get("need_human_review"),
        "review_reasons": st.get("review_reasons") or [],
        "anomalies": st.get("anomalies") or [],
        "steps": st.get("steps") or [],
        "tool_calls": st.get("tool_calls") or [],
        "retry_count": st.get("retry_count"),
        "warnings": warn.get("warnings") or [],
        "warning_text": warn.get("warning_text") or "",
        "warning_meta": warn.get("_meta") or {},
        "warning_overall_risk": warn.get("overall_risk"),
        "logs": sorted(p.name for p in pd.glob("*.log")),
    }


@router.get("/runs/{run}/pairs/{pair}/image/{kind}")
def pair_image(run: str, pair: str, kind: str) -> FileResponse:
    _guard(run, "批次名")
    _guard(pair, "图对标识")
    pd = AGENT_ROOT / run / f"full-{pair}"
    mapping = {
        "fused": pd / "l2" / "annotated" / f"{pair}_1fused.jpg",
        "visible": pd / "l2" / "annotated" / f"{pair}_2visible.jpg",
        "infrared": pd / "l2" / "annotated" / f"{pair}_3infrared.jpg",
        "input": pd / "input" / f"{pair}.jpg",
    }
    p = mapping.get(kind)
    if p is None:
        raise HTTPException(400, f"未知图类型: {kind}（可选 {'/'.join(mapping)}）")
    if not p.is_file():
        raise HTTPException(404, f"图不存在: {kind}")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/graph-runs/{task_id}/images")
def graph_run_images(task_id: str) -> dict[str, Any]:
    """在线研判任务的图像产物清单（供人工复核台兜底取图）。

    验收批次反查不到 pair 时，复核台用任务自身的 graph_runs 产物展示三联图。
    只读：仅按文件名模式定位 l2/annotated 与 input 下的现有图像。
    """
    _guard(task_id, "任务标识")
    pd = AGENT_ROOT / "graph_runs" / task_id
    base = f"/api/graph-runs/{task_id}/image"
    images: dict[str, str | None] = {}
    for kind, suffix in (("fused", "1fused"), ("visible", "2visible"), ("infrared", "3infrared")):
        hit = next(iter(sorted((pd / "l2" / "annotated").glob(f"*_{suffix}.jpg"))), None)
        images[kind] = f"{base}/{kind}" if hit else None
    inp = next(iter(sorted((pd / "input").glob("*.jpg"))), None)
    images["input"] = f"{base}/input" if inp else None
    return {"task_id": task_id, "images": images}


@router.get("/graph-runs/{task_id}/image/{kind}")
def graph_run_image(task_id: str, kind: str) -> FileResponse:
    _guard(task_id, "任务标识")
    pd = AGENT_ROOT / "graph_runs" / task_id
    suffix = {"fused": "1fused", "visible": "2visible", "infrared": "3infrared"}.get(kind)
    if suffix:
        p = next(iter(sorted((pd / "l2" / "annotated").glob(f"*_{suffix}.jpg"))), None)
    elif kind == "input":
        p = next(iter(sorted((pd / "input").glob("*.jpg"))), None)
    else:
        raise HTTPException(400, "未知图类型: %s（可选 fused/visible/infrared/input）" % kind)
    if p is None or not p.is_file():
        raise HTTPException(404, f"任务 {task_id} 没有该图产物: {kind}")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/pending")
def pending() -> dict[str, Any]:
    entries = _read_json(PENDING_PATH, []) or []
    for e in entries:
        e["kind_label"] = {"manual": "需人工介入", "sample": "抽样复核"}.get(e.get("kind"), "历史（无类型）")
    return {"entries": entries, "n_open": sum(1 for e in entries if not e.get("resolved"))}


@router.get("/human-archives")
def human_archives() -> dict[str, Any]:
    """离线转人工归档；不属于实时阻塞队列。"""
    entries = _read_json(HUMAN_ARCHIVE_PATH, []) or []
    return {"entries": entries, "n": len(entries)}


@router.post("/pending/{task_id}/resolve")
def resolve_pending(task_id: str) -> dict[str, Any]:
    """仅把待审条目标记为已处理（不改结论、不触碰图状态）。"""
    _guard(task_id, "任务标识")
    entries = _read_json(PENDING_PATH, []) or []
    hit = False
    for e in entries:
        if e.get("task_id") == task_id:
            e["resolved"] = True
            e["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            hit = True
    if not hit:
        raise HTTPException(404, f"待审队列中无 {task_id}")
    save_json(PENDING_PATH, entries)
    return {"ok": True, "task_id": task_id}


@router.get("/golden")
def golden() -> dict[str, Any]:
    doc = _read_json(GOLDEN_PATH, {}) or {}
    return {"reviewer": doc.get("reviewer"), "reviewed_at": doc.get("reviewed_at"),
            "note": doc.get("_说明"), "entries": doc.get("entries") or []}


@router.get("/memory")
def memory() -> dict[str, Any]:
    return _read_json(MEMORY_PATH, {}) or {}


@router.get("/meta")
def meta() -> dict[str, Any]:
    """前端渲染用的词表与分组（避免把中文映射写死在 HTML 里）。"""
    return {"dispositions": DISPOSITION_META, "steps": STEP_CN, "tools": TOOL_CN,
            "risks": ["高风险", "低风险", "安全"], "severity": RISK_RANK,
            "ppe_items": ["helmet", "vest", "gloves", "safety_shoes"],
            "pose_items": ["safety_belt", "safety_rope"],
            "attr_items": ["in_crane_load_zone", "in_machinery_radius"]}


# ---------- 静态控制台 ----------

def mount_console(app) -> None:
    """把 web/ 目录挂到 /console（含 console.html 与静态资源）。"""
    if WEB_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=str(WEB_DIR), html=True), name="console")


# 独立启动入口（不依赖 agent.service，不需要 API key）
from fastapi import FastAPI  # noqa: E402

standalone = FastAPI(title="施工安全巡检 Agent · 控制台 API")
standalone.include_router(router)
mount_console(standalone)
