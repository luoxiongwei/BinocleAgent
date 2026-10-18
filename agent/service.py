# -*- coding: utf-8 -*-
"""施工安全 Agent 服务入口（FastAPI + SqliteSaver，工程化部署形态）。

  POST /tasks                  {pair_id, task_id?} → 创建后台巡检任务，立即返回 queued：
                                                   安全-记录 / 低风险-提示 / 高风险-告警 / 转人工-记录
                                                   pair_id 支持 pairNN 或中文别名（见 /inputs）
  GET  /tasks/{task_id}        → 状态快照（checkpointer 持久化，服务重启不丢）
  POST /tasks/{task_id}/review {verdict, risk_label?, notes?} → 专家 verdict 回写已完成任务
                                                   （update_state 修正结论与处置，不阻塞不等待）
  POST /inputs                 {pair_id?, display_name?, rgb, ir} → 上传双光原图；
                                                   pair_id 传中文名则自动分配 pairNN 并登记别名
  GET  /inputs                 → 图对库（规范 ID + 中文显示名 + 原图完备性）
  POST /inputs/rename          {pair_id, display_name} → 重命名图对（只改中文别名）

设计说明（2026-09-29）：① invoke 在请求线程同步执行（线程+共享 sqlite+interrupt 曾 C 层死锁）；
② 四种处置都是任务完成态——agent 不等待人工，转人工=登记待审队列后照常收尾，
verdict 事后经 update_state 回写修正（可回写前先处理下一张图，互不阻塞）。

启动: uvicorn agent.service:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import sqlite3
import os
import re
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, File, Form, UploadFile
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from agent.graph import apply_verdict, build_graph, mark_review_resolved
from agent.outcome import VALID_RISKS
from agent.tools_registry import call as default_tool_call
from tools.common import project_root
from tools.config_loader import load_config

PROJECT = project_root()
DB_PATH = PROJECT / "results" / "agent" / "checkpoints.sqlite"

app = FastAPI(title="施工安全巡检 Agent", version="0.3-graph-nonblocking")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
_checkpointer = SqliteSaver(_conn)
_checkpointer.setup()
try:
    _runtime_cfg = load_config().get("runtime", {}) or {}
except Exception:
    _runtime_cfg = {}
_planner_enabled = os.getenv("GDYOLO_ENABLE_PLANNER", "1" if _runtime_cfg.get("planner_enabled") else "0") == "1"
_app = build_graph(checkpointer=_checkpointer,
                   planner_gateway=True if _planner_enabled else None)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gdyolo-agent")
_task_lock = threading.RLock()
_task_futures: dict[str, Any] = {}


class TaskIn(BaseModel):
    pair_id: str
    task_id: str | None = None


class ReviewIn(BaseModel):
    verdict: str                      # 接受 | 修改 | 驳回
    risk_label: str | None = None     # 修改/人工判级时的新等级
    notes: str | None = None


# —— 图对命名：规范 ID（pairNN）保持不变，中文名作为别名注册 ——
# 管道的图像匹配依赖 pairNN 编号（_find_pair 按文件名数字配对），不能推倒；
# 别名层让用户可以用「智感筑安」这类中文名指代图对，并支持随时重命名。
ALIAS_PATH = PROJECT / "data" / "pair_aliases.json"
_ALIAS_LOCK = threading.Lock()
_alias_cache: dict[str, Any] | None = None


def _load_aliases() -> dict[str, Any]:
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache
    data: dict[str, Any] = {"aliases": {}, "display": {}, "origin": {}}
    try:
        if ALIAS_PATH.is_file():
            loaded = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data["aliases"] = dict(loaded.get("aliases") or {})
                data["display"] = dict(loaded.get("display") or {})
                data["origin"] = dict(loaded.get("origin") or {})
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    _alias_cache = data
    return data


def _save_aliases(data: dict[str, Any]) -> None:
    global _alias_cache
    _alias_cache = data
    ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALIAS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _next_pair_number() -> int:
    """扫描原图目录，分配下一个未占用的 pair 编号。"""
    root = PROJECT / "data" / "rgbt" / "output"
    best = 0
    if root.is_dir():
        for path in root.iterdir():
            m = re.search(r"(\d+)", path.stem)
            if m:
                best = max(best, int(m.group(1)))
    return best + 1


def _resolve_pair_id(name: str) -> str | None:
    """任意名称 → 规范 pair_id：pairNN 原样返回；已注册别名返回映射；否则 None。"""
    name = (name or "").strip()
    if not name:
        return None
    if re.fullmatch(r"pair(\d+)", name, flags=re.IGNORECASE):
        return name.lower()
    return _load_aliases()["aliases"].get(name)


@app.post("/inputs")
async def upload_inputs(pair_id: str = Form(""), display_name: str = Form(""),
                        rgb: UploadFile = File(...), ir: UploadFile = File(...)) -> dict[str, Any]:
    """上传一对 RGB/IR 原图，落盘后即可用同一个标识创建任务。

    pair_id 可传规范 ID（pair04）或任意名称（中文名，如「智感筑安」）：
    名称已注册 → 更新该图对原图；未注册 → 自动分配下一个 pairNN 并登记名称。
    display_name 可额外指定/修改该图对的显示名（即重命名）。
    """
    raw = (pair_id or "").strip()
    alias = (display_name or "").strip()
    with _ALIAS_LOCK:
        data = _load_aliases()
        canonical = None
        if raw:
            if re.fullmatch(r"pair(\d+)", raw, flags=re.IGNORECASE):
                canonical = raw.lower()
            else:
                canonical = data["aliases"].get(raw)
                if canonical is None:  # 新名称：分配编号并登记
                    canonical = f"pair{_next_pair_number():02d}"
                data["aliases"][raw] = canonical
        elif alias:
            canonical = data["aliases"].get(alias) or f"pair{_next_pair_number():02d}"
            data["aliases"][alias] = canonical
        if canonical is None:
            raise HTTPException(400, "请提供图对 ID（如 pair04）或图对名称（如「智感筑安」）")
        if alias:  # 显示名：主别名 + UI 标签（不与规范 ID 混淆）
            if re.fullmatch(r"pair(\d+)", alias, flags=re.IGNORECASE):
                raise HTTPException(400, "显示名不能使用 pairNN 格式（会与规范 ID 混淆）")
            data["aliases"][alias] = canonical
            data["display"][canonical] = alias
        number = int(canonical[4:])
    allowed = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    root = PROJECT / "data" / "rgbt" / "output"
    root.mkdir(parents=True, exist_ok=True)
    # 先完成两张图的格式与大小校验，再替换旧文件，避免坏上传破坏已有图对。
    payloads: dict[str, tuple[str, bytes]] = {}
    for role, upload in (("rgb", rgb), ("ir", ir)):
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in allowed:
            raise HTTPException(400, f"{role} 仅支持 {sorted(allowed)}")
        payload = await upload.read()
        if not payload or len(payload) > 50 * 1024 * 1024:
            raise HTTPException(400, f"{role} 文件为空或超过 50MB")
        payloads[role] = (suffix, payload)
    # 替换语义：同一编号的既有原图（无论命名风格）先移除，避免 _find_pair 多文件匹配歧义
    replaced: list[str] = []
    if root.is_dir():
        for path in root.iterdir():
            if not path.is_file() or path.suffix.lower() not in allowed:
                continue
            m = re.search(r"(\d+)", path.stem)
            if not m or int(m.group(1)) != number:
                continue
            is_ir = path.stem.lower().startswith("ir")
            replaced.append(path.name)
            path.unlink()
    saved: dict[str, str] = {}
    origin: dict[str, str] = {}
    for role, upload in (("rgb", rgb), ("ir", ir)):
        suffix, payload = payloads[role]
        target = root / f"{role}{number:02d}{suffix}"
        target.write_bytes(payload)
        saved[role] = str(target)
        if upload.filename:
            origin[role] = upload.filename
    with _ALIAS_LOCK:
        data = _load_aliases()
        if origin:  # 原始文件名留痕（上传后规范命名会丢掉中文原名）
            data.setdefault("origin", {})[canonical] = origin
        _save_aliases(data)
    return {"pair_id": canonical, "display": _load_aliases()["display"].get(canonical),
            "origin": origin, "replaced": replaced, "saved": saved, "ready": True}


class RenameIn(BaseModel):
    pair_id: str          # 规范 ID 或已注册名称
    display_name: str     # 新显示名（中文）


@app.post("/inputs/rename")
def rename_pair(body: RenameIn) -> dict[str, Any]:
    """重命名图对（改的是中文别名，规范 ID 不变，历史任务不受影响）。"""
    canonical = _resolve_pair_id(body.pair_id)
    if canonical is None:
        known = sorted(_load_aliases()["aliases"])
        hint = "、".join(known[:8]) if known else "（暂无命名图对）"
        raise HTTPException(404, f"未知图对: {body.pair_id}。已命名: {hint}")
    new_name = (body.display_name or "").strip()
    if not new_name:
        raise HTTPException(400, "显示名不能为空")
    if re.fullmatch(r"pair(\d+)", new_name, flags=re.IGNORECASE):
        raise HTTPException(400, "显示名不能使用 pairNN 格式（会与规范 ID 混淆）")
    with _ALIAS_LOCK:
        data = _load_aliases()
        for key in [k for k, v in data["aliases"].items() if v == canonical]:
            data["aliases"].pop(key, None)
        data["aliases"][new_name] = canonical
        data["display"][canonical] = new_name
        _save_aliases(data)
    return {"pair_id": canonical, "display": new_name}


@app.get("/inputs")
def list_inputs() -> dict[str, Any]:
    """图对库：规范 ID + 中文显示名 + 原图完备性。"""
    data = _load_aliases()
    root = PROJECT / "data" / "rgbt" / "output"
    pairs: dict[str, dict[str, Any]] = {}
    if root.is_dir():
        for path in root.iterdir():
            if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
                continue
            m = re.search(r"(\d+)", path.stem)
            if not m:
                continue
            pid = f"pair{int(m.group(1)):02d}"
            entry = pairs.setdefault(pid, {"pair_id": pid, "rgb": False, "ir": False,
                                           "display": data["display"].get(pid),
                                           "origin": data["origin"].get(pid)})
            if path.stem.lower().startswith("ir"):
                entry["ir"] = True
            else:
                entry["rgb"] = True
    return {"pairs": sorted(pairs.values(), key=lambda e: e["pair_id"]),
            "aliases": data["aliases"]}


def _config(task_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": task_id}}


def _event_path(task_id: str) -> Path:
    return PROJECT / "results" / "agent" / "graph_runs" / task_id / "events.jsonl"


def _event(task_id: str, kind: str, **data: Any) -> None:
    path = _event_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    item = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "task_id": task_id, "event": kind, **data}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def _run_task(task_id: str, pair_id: str, run_dir: str) -> None:
    _event(task_id, "task_started", pair_id=pair_id)
    try:
        with _task_lock:
            def observed_call(name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
                started = datetime.now(timezone.utc)
                _event(task_id, "tool_started", tool=name)
                try:
                    result = default_tool_call(name, *args, **kwargs)
                except Exception as exc:
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
                    _event(task_id, "tool_finished", tool=name, status="error",
                           duration_ms=round(elapsed, 1), error=str(exc)[:500])
                    raise
                elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
                _event(task_id, "tool_finished", tool=name,
                       status=result.get("status", "ok"), duration_ms=round(elapsed, 1),
                       error=result.get("error"))
                return result

            task_app = build_graph(tool_call=observed_call,
                                   checkpointer=_checkpointer,
                                   planner_gateway=True if _planner_enabled else None)
            final = task_app.invoke({"task_id": task_id, "pair_id": pair_id,
                                 "run_dir": run_dir, "steps": [],
                                 "tool_calls": [], "review_reasons": []},
                                config=_config(task_id))
        _event(task_id, "task_completed", status=final.get("status"),
               disposition=final.get("disposition"), steps=final.get("steps") or [])
    except Exception as exc:
        _event(task_id, "task_failed", error=str(exc)[:500])
        raise


def _derive_status(task_id: str, values: dict[str, Any]) -> str:
    """任务级状态派生。

    FINALIZE 之前 state 没有 status 字段，不能默认 "completed"——那会让前端
    轮询在第一个节点后就误判完成，把 fuse_qc 中间态当最终日志钉在页面上
    （2026-10-16 用户实测复现：graph-pair31-4 假 completed + 工具调用 1 次）。
    在飞任务按 future 实况报 running/failed；历史/批跑任务无 status 时维持 completed。
    """
    st = values.get("status")
    if st:
        return st
    fut = _task_futures.get(task_id)
    if fut is None:
        return "completed"
    if not fut.done():
        return "running"
    return "failed" if fut.exception() is not None else "completed"


def _snapshot(task_id: str, final: dict[str, Any] | None = None) -> dict[str, Any]:
    snap = _app.get_state(_config(task_id))
    values: dict[str, Any] = dict(final if final is not None else (snap.values or {}))
    ra = values.get("risk_assessment") or {}
    evidence = {k: values.get(k) for k in _EVIDENCE_KEYS if values.get(k) is not None}
    return {
        "task_id": task_id,
        "pair_id": values.get("pair_id"),
        "status": _derive_status(task_id, values),
        "disposition": values.get("disposition"),
        "overall_risk": ra.get("overall_risk"),
        "n_warnings": ra.get("n_warnings"),
        "need_human_review": values.get("need_human_review"),
        "human_archive": values.get("human_archive"),
        "review_reasons": values.get("review_reasons") or [],
        "human_verdict": values.get("human_verdict"),
        "equipment_check": values.get("equipment_check"),
        "route": values.get("route"),
        "steps": values.get("steps") or [],
        "tool_calls": values.get("tool_calls") or [],
        "evidence": evidence,
        "events_url": f"/tasks/{task_id}/events",
        "error": values.get("error"),
    }


def _free_task_id(base: str) -> str:
    """自动编号避让：base 被占用时递增后缀（-2、-3…）。

    覆盖「run_dir 已删但 checkpointer 残留幽灵状态」与「同一图对再次研判」两类撞车，
    避免用户留空任务 ID 时莫名撞上 409。
    """
    candidate = base
    n = 2
    while _safe_state_values(candidate) or candidate in _task_futures:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


@app.post("/tasks")
def create_task(body: TaskIn) -> dict[str, Any]:
    raw = body.pair_id.strip()
    if not raw or Path(raw).name != raw:
        raise HTTPException(400, "图对标识必须是单个名称，例如 pair04 或「智感筑安」")
    pair_id = _resolve_pair_id(raw)
    if pair_id is None:
        known = sorted(_load_aliases()["aliases"])
        hint = "、".join(known[:8]) if known else "（暂无命名图对）"
        raise HTTPException(400, f"未知图对: {raw}。可用 pair01–pair30 或已命名: {hint}")
    task_id = body.task_id or _free_task_id(f"graph-{pair_id}")
    if _safe_state_values(task_id) or task_id in _task_futures:
        raise HTTPException(409,
                            f"任务 {task_id} 已存在（终态不可重跑）。重复研判请留空任务 ID 自动编号，或换一个新 ID")
    run_dir = str(PROJECT / "results" / "agent" / "graph_runs" / task_id)
    _event(task_id, "task_queued", pair_id=pair_id)
    _task_futures[task_id] = _executor.submit(_run_task, task_id, pair_id, run_dir)
    return {"task_id": task_id, "pair_id": pair_id, "status": "queued",
            "events_url": f"/tasks/{task_id}/events"}


_STATE_DISK_CACHE: dict[str, dict[str, Any]] = {}


def _state_from_disk(task_id: str) -> dict[str, Any] | None:
    """checkpointer 未命中时，从磁盘 graph_state.json 恢复任务状态。

    batch_acceptance / run_all 的批跑不传 checkpointer，状态只落盘；
    待审队列中的历史任务（batch-* 等）因此需要此回退才能打开与回写。
    同一 task_id 存在多份产物时取文件修改时间最新的一份；未命中也缓存（防重复全盘扫描）。
    """
    if task_id in _STATE_DISK_CACHE:
        return _STATE_DISK_CACHE[task_id]
    best: tuple[float, dict[str, Any]] | None = None
    for path in (PROJECT / "results" / "agent").rglob("graph_state.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(state, dict) and state.get("task_id") == task_id:
            if best is None or mtime > best[0]:
                best = (mtime, state)
    _STATE_DISK_CACHE[task_id] = best[1] if best else None
    return _STATE_DISK_CACHE[task_id]


_LIST_FIELDS = ("steps", "tool_calls", "review_reasons", "anomalies")

# _snapshot 附加输出的证据链字段（供前端复核台展示；扁平契约不变，新增 evidence 子对象）
_EVIDENCE_KEYS = ("quality", "scene", "detections", "reconciliation", "rule",
                  "report", "warnings", "warning_text", "evidence_check", "golden",
                  "warning_meta", "warning_overall_risk")


def _merge_states(disk: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """checkpointer 状态为准、磁盘态垫底。

    steps/tool_calls 为追加式 reducer：人工回写只把 patch 写入 checkpointer，
    且 get_state 对未写通道返回空列表默认值，直接 dict 合并会丢磁盘态历史明细。
    列表字段按「磁盘 + checkpointer 去重追加」合并，标量/字典字段 checkpointer 覆盖。
    """
    merged = {**disk, **snap}
    for key in _LIST_FIELDS:
        base = disk.get(key)
        overlay = snap.get(key)
        if isinstance(base, list) and isinstance(overlay, list):
            acc = list(base)
            for item in overlay:
                if item not in acc:
                    acc.append(item)
            merged[key] = acc
    return merged


def _safe_state_values(task_id: str) -> dict[str, Any] | None:
    """读 checkpointer 快照，带瞬时异常容错。

    图执行线程与 FastAPI 读侧共享同一 sqlite 连接，运行中轮询偶发锁冲突/
    游标递归异常属瞬时抖动：重试一次后仍失败则返回 None，由调用方降级到
    磁盘态或最小 running 响应——绝不把 500 抛给 1s 一次的前端轮询
    （2026-10-17 用户实测「不流式、刷新才更新」的诱因之一）。
    """
    for attempt in (0, 1):
        try:
            return _app.get_state(_config(task_id)).values
        except Exception:
            if attempt == 0:
                time.sleep(0.15)
    return None


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    snap_values = _safe_state_values(task_id)
    disk = _state_from_disk(task_id)
    if snap_values:
        if disk is not None:
            return _snapshot(task_id, _merge_states(disk, snap_values))
        return _snapshot(task_id)
    if disk is not None:
        return _snapshot(task_id, disk)
    if task_id not in _task_futures:
        raise HTTPException(404, f"未知任务: {task_id}")
    future = _task_futures.get(task_id)
    return {"task_id": task_id,
            "status": "failed" if future and future.done() and future.exception() else "running",
            "events_url": f"/tasks/{task_id}/events"}


@app.get("/tasks/{task_id}/events")
def task_events(task_id: str) -> dict[str, Any]:
    path = _event_path(task_id)
    if not path.is_file() and task_id not in _task_futures:
        raise HTTPException(404, f"未知任务: {task_id}")
    events: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {"task_id": task_id, "events": events}


@app.post("/tasks/{task_id}/review")
def review_task(task_id: str, body: ReviewIn) -> dict[str, Any]:
    if body.verdict not in ("接受", "修改", "驳回"):
        raise HTTPException(400, "verdict 仅允许 接受/修改/驳回")
    values = dict(_safe_state_values(task_id) or {})
    if not values:
        values = _state_from_disk(task_id) or {}
    if not values:
        raise HTTPException(404, f"未知任务: {task_id}")
    if values.get("human_verdict"):
        raise HTTPException(409, "该任务已完成人工回写，不可重复回写")
    if not values.get("need_human_review"):
        raise HTTPException(409, "该任务为自动通道终态，无需人工回写")
    need_label = body.verdict == "修改" or values.get("disposition") == "P3_manual_judge"
    if need_label and body.risk_label not in VALID_RISKS:
        raise HTTPException(400, "修改/转人工判级必须提供 risk_label（安全/低风险/高风险）")
    patch = apply_verdict(values, {"verdict": body.verdict, "risk_label": body.risk_label,
                                   "notes": body.notes})
    _app.update_state(_config(task_id), values=patch)
    mark_review_resolved(task_id)
    return _snapshot(task_id, {**values, **patch})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
