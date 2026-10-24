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

设计说明（2026-09-29）：① 任务在后台 Worker 执行，每个新任务使用独立 checkpoint sqlite；
② 四种处置都是任务完成态——agent 不等待人工，转人工=登记待审队列后照常收尾，
verdict 事后经 update_state 回写修正（可回写前先处理下一张图，互不阻塞）。

启动: uvicorn agent.service:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import sqlite3
import os
import re
import json
import hashlib
import time
import threading
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, File, Form, UploadFile
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from agent.graph import apply_verdict, build_graph, mark_review_resolved
from agent.case_memory import record_acceptance, search_cases
from agent.outcome import VALID_RISKS
from agent import qa as qa_service
from agent import chat as chat_service
from agent.tools_registry import call as default_tool_call
from tools.common import load_json, project_root, save_json
from tools.config_loader import load_config

PROJECT = project_root()
DB_PATH = PROJECT / "results" / "agent" / "checkpoints.sqlite"


def _policy_version() -> dict[str, Any]:
    """返回当前判级代码与规范库的可审计指纹，不修改运行时策略。"""
    digest = hashlib.sha256()
    tracked = [PROJECT / "agent" / "outcome.py", PROJECT / "agent" / "graph.py"]
    for path in tracked:
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return {"version": digest.hexdigest()[:16],
            "tracked_files": [str(path.relative_to(PROJECT)) for path in tracked],
            "mode": "runtime_read_only"}

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
try:
    _worker_limit = max(1, min(5, int(os.getenv("GDYOLO_MAX_WORKERS", "2"))))
except ValueError:
    _worker_limit = 2
_executor = ThreadPoolExecutor(max_workers=_worker_limit, thread_name_prefix="gdyolo-agent")
_task_lock = threading.RLock()
_task_futures: dict[str, Any] = {}
_task_apps: dict[str, Any] = {}
_task_conns: dict[str, sqlite3.Connection] = {}
_task_runtime_lock = threading.RLock()
_FEEDBACK_LOCK = threading.Lock()
FEEDBACK_DIR = PROJECT / "results" / "agent" / "feedback"
_METRICS_LOCK = threading.Lock()
_runtime_metrics: dict[str, Any] = {
    "submitted": 0, "started": 0, "completed": 0, "failed": 0,
    "retries": 0, "total_duration_ms": 0.0, "last_error": None,
}
_task_started_at: dict[str, float] = {}


def _metric_submit(task_id: str, attempt: int) -> None:
    with _METRICS_LOCK:
        _runtime_metrics["submitted"] += 1
        if attempt > 1:
            _runtime_metrics["retries"] += 1


def _metric_start(task_id: str) -> None:
    with _METRICS_LOCK:
        _runtime_metrics["started"] += 1
        _task_started_at[task_id] = time.perf_counter()


def _metric_finish(task_id: str, status: str, error: str | None = None) -> None:
    with _METRICS_LOCK:
        started = _task_started_at.pop(task_id, None)
        if started is not None:
            _runtime_metrics["total_duration_ms"] += (time.perf_counter() - started) * 1000
        _runtime_metrics[status] += 1
        if error:
            _runtime_metrics["last_error"] = error[:300]


def _feedback_path(task_id: str) -> Path:
    return FEEDBACK_DIR / f"{task_id}.json"


def _acceptance_path(task_id: str) -> Path:
    return PROJECT / "results" / "agent" / "graph_runs" / task_id / "acceptance.json"


def _load_acceptance(task_id: str) -> list[dict[str, Any]]:
    path = _acceptance_path(task_id)
    try:
        value = load_json(path) if path.is_file() else []
    except (OSError, json.JSONDecodeError, TypeError):
        value = []
    return value if isinstance(value, list) else []


def _load_feedback(task_id: str) -> list[dict[str, Any]]:
    path = _feedback_path(task_id)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, json.JSONDecodeError, ValueError):
        loaded = []
    return loaded if isinstance(loaded, list) else []


def _feedback_summary() -> dict[str, Any]:
    """汇总人工/用户反馈，只读统计，不直接修改规则或模型。"""
    entries: list[dict[str, Any]] = []
    if FEEDBACK_DIR.is_dir():
        for path in FEEDBACK_DIR.glob("*.json"):
            try:
                value = load_json(path)
            except (OSError, json.JSONDecodeError, TypeError):
                value = []
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, dict))
    by_rating: dict[str, int] = {}
    by_risk: dict[str, dict[str, int]] = {}
    by_error: dict[str, int] = {}
    for item in entries:
        rating = item.get("rating") or "unknown"
        by_rating[rating] = by_rating.get(rating, 0) + 1
        risk = item.get("agent_risk") or "unknown"
        bucket = by_risk.setdefault(risk, {"correct": 0, "incorrect": 0, "uncertain": 0})
        if rating in bucket:
            bucket[rating] += 1
        error_type = item.get("error_type")
        if error_type:
            by_error[error_type] = by_error.get(error_type, 0) + 1
    judged = by_rating.get("correct", 0) + by_rating.get("incorrect", 0)
    accuracy = by_rating.get("correct", 0) / judged if judged else None
    return {"total": len(entries), "by_rating": by_rating, "by_agent_risk": by_risk,
            "by_error_type": by_error, "accuracy_on_judged": accuracy,
            "learning_mode": "offline_review_only",
            "note": "反馈仅用于评估和规则校准候选，不会在运行时自动修改模型或规则"}


def _feedback_candidates() -> list[dict[str, Any]]:
    """生成离线校准候选；少于 3 条同类错误不进入候选，避免过拟合单条反馈。"""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    if FEEDBACK_DIR.is_dir():
        for path in FEEDBACK_DIR.glob("*.json"):
            try:
                value = load_json(path)
            except (OSError, json.JSONDecodeError, TypeError):
                value = []
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or item.get("rating") != "incorrect":
                    continue
                risk = str(item.get("agent_risk") or "unknown")
                target = str(item.get("corrected_risk") or item.get("corrected_disposition") or "unspecified")
                error = str(item.get("error_type") or "unspecified")
                groups.setdefault((risk, target, error), []).append(item)
    candidates = []
    for (risk, target, error), items in groups.items():
        if len(items) < 3:
            continue
        candidates.append({
            "candidate_id": f"{risk}|{target}|{error}",
            "agent_risk": risk,
            "suggested_target": target,
            "error_type": error,
            "sample_count": len(items),
            "status": "pending_human_review",
            "action": "review_threshold_or_prompt",
        })
    return sorted(candidates, key=lambda item: (-item["sample_count"], item["candidate_id"]))


def _task_checkpoint_path(task_id: str) -> Path:
    return PROJECT / "results" / "agent" / "graph_runs" / task_id / "checkpoint.sqlite"


def _batch_meta_path(task_id: str) -> Path:
    return PROJECT / "results" / "agent" / "graph_runs" / task_id / "batch.json"


def _write_batch_meta(task_id: str, batch_id: str, pair_id: str,
                      attempt: int = 1, parent_task_id: str | None = None) -> None:
    save_json(_batch_meta_path(task_id), {
        "batch_id": batch_id,
        "task_id": task_id,
        "pair_id": pair_id,
        "attempt": attempt,
        "parent_task_id": parent_task_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _task_app(task_id: str, run_dir: str | None = None):
    """为任务懒加载独立 checkpoint；旧任务继续使用全局兼容库。"""
    with _task_runtime_lock:
        if task_id in _task_apps:
            return _task_apps[task_id]
        db_path = Path(run_dir) / "checkpoint.sqlite" if run_dir else _task_checkpoint_path(task_id)
        if not run_dir and not db_path.is_file():
            return _app
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        task_app = build_graph(checkpointer=checkpointer,
                                planner_gateway=True if _planner_enabled else None)
        _task_conns[task_id] = conn
        _task_apps[task_id] = task_app
        return task_app


class TaskIn(BaseModel):
    pair_id: str
    task_id: str | None = None
    batch_id: str | None = None
    qa_session_id: str | None = None


class QAAskIn(BaseModel):
    question: str
    session_id: str | None = None
    pair_id: str | None = None
    task_id: str | None = None


class QABindIn(BaseModel):
    session_id: str
    task_id: str | None = None
    pair_id: str | None = None


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    pair_id: str | None = None
    task_id: str | None = None


def _chat_list_pending_tasks() -> dict[str, Any]:
    pending_path = PROJECT / "results" / "agent" / "pending_reviews.json"
    try:
        entries = load_json(pending_path) if pending_path.is_file() else []
    except (OSError, json.JSONDecodeError, TypeError):
        entries = []
    if isinstance(entries, dict):
        entries = entries.get("entries", [])
    unresolved = [e for e in entries if isinstance(e, dict) and not e.get("resolved")]
    running = [tid for tid, fut in _task_futures.items() if not fut.done()]
    failed = [tid for tid, fut in _task_futures.items() if fut.done() and fut.exception() is not None]
    return {"pending_review": len(unresolved), "running": len(running), "failed": len(failed),
            "total_actionable": len(unresolved) + len(running) + len(failed),
            "items": unresolved[-50:], "running_task_ids": running, "failed_task_ids": failed}


def _chat_task_events(task_id: str) -> dict[str, Any]:
    path = _event_path(task_id)
    if not path.is_file():
        raise HTTPException(404, f"任务事件流不存在: {task_id}")
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict): events.append(item)
        except json.JSONDecodeError:
            continue
    return {"task_id": task_id, "events": events}


class RetryIn(BaseModel):
    batch_id: str | None = None


class ReviewIn(BaseModel):
    verdict: str                      # 接受 | 修改 | 驳回
    risk_label: str | None = None     # 修改/人工判级时的新等级
    notes: str | None = None


class FeedbackIn(BaseModel):
    """任务完成后的用户质量反馈，作为可审计评估样本保存。"""

    rating: str  # correct | incorrect | uncertain
    corrected_risk: str | None = None
    corrected_disposition: str | None = None
    error_type: str | None = None
    notes: str | None = None


class AcceptanceIn(BaseModel):
    """成功任务的异步人工验收，不阻塞原始 Agent 结果。"""

    verdict: str  # accept | correct | reject
    risk_label: str | None = None
    disposition: str | None = None
    error_type: str | None = None
    notes: str | None = None
    reviewer: str | None = None


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


@app.post("/qa/ask")
def ask_knowledge(body: QAAskIn) -> dict[str, Any]:
    """规范问答：回答始终带规范库引用，并保留会话上下文。"""
    try:
        return qa_service.ask(body.question, body.session_id, body.pair_id, body.task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/chat")
def chat_agent(body: ChatIn) -> dict[str, Any]:
    """统一对话入口：规范问答、任务意图识别和上下文连续性。"""
    try:
        active_session_id = qa_service.ensure_session(body.session_id)
        session = qa_service.get_session(active_session_id)
        def execute_chat_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            if name == "knowledge_search":
                return qa_service.ask(str(args.get("question") or body.message), body.session_id)
            if name == "query_task":
                return _snapshot(str(args.get("task_id")))
            if name == "retry_task":
                return retry_task(str(args.get("task_id")), RetryIn())
            if name == "list_pending_tasks":
                return _chat_list_pending_tasks()
            if name == "get_task_events":
                return _chat_task_events(str(args.get("task_id")))
            if name == "submit_feedback":
                feedback = FeedbackIn(rating=str(args.get("rating") or "uncertain"),
                                      corrected_risk=args.get("corrected_risk"),
                                      notes=args.get("notes"))
                return add_task_feedback(str(args.get("task_id")), feedback)
            if name == "search_memory":
                query = str(args.get("query") or "")
                return {"query": query, "reference_only": True,
                        "cases": search_cases(query, limit=max(1, min(10, int(args.get("limit", 5) or 5))))}
            if name == "create_safety_log":
                return create_task(TaskIn(pair_id=str(args.get("pair_id")), qa_session_id=args.get("qa_session_id") or body.session_id))
            return {"status": "error", "error": f"unknown tool: {name}"}
        loop_result = chat_service.run_llm_loop(body.message, (session or {}).get("turns", []), execute_chat_tool)
        if loop_result and loop_result.get("answer"):
            qa_service.record_turn(active_session_id, body.message, loop_result["answer"], loop_result.get("trace", []))
            return {"intent": "agent_loop", "action": "conversation", "session_id": active_session_id,
                    "answer": loop_result["answer"], "trace": loop_result.get("trace", []),
                    "answer_mode": loop_result.get("mode")}
        # The LLM runtime is the only authoritative conversational router.  Do
        # not fall through to the legacy RAG classifier when it is unavailable:
        # that path turns unrelated questions into fabricated standards answers.
        if loop_result is None:
            return {"intent": "agent_unavailable", "action": "conversation", "session_id": active_session_id,
                    "answer": "我暂时无法可靠判断这句话的意图，因此不会擅自检索规范或创建任务。请稍后重试，或明确说明你要进行规范问答、任务查询还是安全日志分析。",
                    "trace": [{"stage": "triage", "mode": "unavailable"}], "answer_mode": "honest_fallback"}
        result = chat_service.chat(body.message, active_session_id, body.pair_id, body.task_id)
        if result.get("action") == "query_task" and result.get("task_id"):
            query_id = str(result["task_id"])
            try:
                snapshot = _snapshot(query_id)
                result.update({"task": snapshot, "answer": f"任务 {query_id} 当前状态：{snapshot.get('status')}，处置：{snapshot.get('disposition') or '待判定'}。"})
            except Exception as exc:
                result["error"] = f"任务查询失败：{exc}"
        elif result.get("action") == "create_safety_log" and body.pair_id:
            try:
                queued = create_task(TaskIn(pair_id=body.pair_id, task_id=body.task_id,
                                             qa_session_id=body.session_id))
                result.update({"task": queued, "answer": f"已创建安全日志任务 {queued['task_id']}，正在执行。"})
            except HTTPException as exc:
                result["error"] = str(exc.detail)
        elif result.get("action") == "retry_task" and result.get("task_id"):
            try:
                queued = retry_task(str(result["task_id"]), RetryIn())
                result.update({"task": queued, "answer": f"已提交任务 {result['task_id']} 的重试。"})
            except HTTPException as exc:
                result["error"] = str(exc.detail)
        elif result.get("action") == "submit_feedback" and result.get("task_id"):
            try:
                feedback = FeedbackIn(rating=result.get("rating", "uncertain"),
                                      corrected_risk=result.get("corrected_risk"))
                saved = add_task_feedback(str(result["task_id"]), feedback)
                result.update({"feedback": saved, "answer": f"已记录任务 {result['task_id']} 的反馈：{feedback.rating}。"})
            except HTTPException as exc:
                result["error"] = str(exc.detail)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/chat/tools")
def chat_tools() -> dict[str, Any]:
    return {"tools": chat_service.tool_schemas(), "mode": "semantic_triage_specialists"}


@app.get("/qa/sessions/{session_id}")
def get_qa_session(session_id: str) -> dict[str, Any]:
    result = qa_service.get_session(session_id)
    if result is None:
        raise HTTPException(404, "问答会话不存在")
    return result


@app.get("/qa/sessions")
def list_qa_sessions(limit: int = 20) -> dict[str, Any]:
    return {"sessions": qa_service.list_sessions(limit)}


@app.post("/qa/sessions/bind")
def bind_qa_session(body: QABindIn) -> dict[str, Any]:
    try:
        return qa_service.bind_task(body.session_id, body.task_id, body.pair_id)
    except KeyError as exc:
        raise HTTPException(404, "问答会话不存在") from exc


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


def _failure_path(task_id: str) -> Path:
    return PROJECT / "results" / "agent" / "graph_runs" / task_id / "failure.json"


def _task_attempt(task_id: str) -> int:
    """从任务标识推导尝试次数；batch.json 中的值会在调用处优先使用。"""
    if "-retry-" in task_id:
        try:
            return max(2, int(task_id.rsplit("-retry-", 1)[1]))
        except ValueError:
            return 2
    return 2 if task_id.endswith("-retry") else 1


def _classify_failure(exc: BaseException, stage: str | None = None) -> dict[str, Any]:
    """将底层异常归一化为前端和批次档案可读的失败诊断。"""
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    if "timeout" in lowered or "timed out" in lowered or "超时" in message:
        code = "timeout"
    elif any(token in lowered for token in ("vlm", "openai", "llm", "api")):
        code = "vlm_error"
    elif any(token in lowered for token in ("yolo", "ultralytics")):
        code = "yolo_error"
    elif any(token in lowered for token in ("fuse", "fusion")) or any(token in message for token in ("配准", "融合")):
        code = "fusion_error"
    elif "json" in lowered or "schema" in lowered:
        code = "invalid_output"
    else:
        code = "runtime_error"
    recovery = {
        "timeout": {"action": "bounded_retry", "label": "服务超时，建议稍后重试"},
        "vlm_error": {"action": "retry_vlm_or_manual", "label": "VLM识别失败，建议检查亮度/清晰度后重试"},
        "yolo_error": {"action": "retry_yolo_or_manual", "label": "YOLO校验失败，建议检查输入尺寸或模型服务"},
        "fusion_error": {"action": "retry_fusion_or_manual", "label": "配准融合失败，建议重新配准；再次失败转人工"},
        "invalid_output": {"action": "retry_or_manual", "label": "工具输出不符合协议，建议重试；再次失败转人工"},
        "runtime_error": {"action": "manual_archive", "label": "运行时异常，建议转人工归档"},
    }[code]
    return {"reason_code": code, "failed_stage": stage or "graph",
            "error": message[:1000], "error_type": exc.__class__.__name__,
            "recovery_action": recovery["action"], "recovery_label": recovery["label"]}


def _snapshot_pair_inputs(pair_id: str, run_dir: str) -> list[str]:
    """在排队时冻结输入，避免同编号重新上传影响等待中的任务。"""
    number = int(pair_id[4:])
    source = PROJECT / "data" / "rgbt" / "output"
    target = Path(run_dir) / "source"
    target.mkdir(parents=True, exist_ok=True)
    allowed = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    copied: list[str] = []
    for item in source.iterdir() if source.is_dir() else []:
        if not item.is_file() or item.suffix.lower() not in allowed:
            continue
        match = re.search(r"(\d+)", item.stem)
        if not match or int(match.group(1)) != number:
            continue
        shutil.copy2(item, target / item.name)
        copied.append(item.name)
    roles = {"ir" if name.lower().startswith("ir") else "rgb" for name in copied}
    if roles != {"rgb", "ir"}:
        raise HTTPException(400, f"{pair_id} 缺少完整 RGB/IR 输入，无法创建任务")
    return copied


def _event(task_id: str, kind: str, **data: Any) -> None:
    path = _event_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    item = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "task_id": task_id, "event": kind, **data}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def _run_task(task_id: str, pair_id: str, run_dir: str) -> None:
    _event(task_id, "task_started", pair_id=pair_id)
    _metric_start(task_id)
    current_tool: str | None = None
    try:
        def observed_call(name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal current_tool
            current_tool = name
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
            current_tool = None
            return result

        # 每个任务使用自己的 checkpoint，避免并发任务争用全局 SQLite。
        db_path = Path(run_dir) / "checkpoint.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        task_app = build_graph(tool_call=observed_call,
                               checkpointer=checkpointer,
                               planner_gateway=True if _planner_enabled else None)
        with _task_runtime_lock:
            _task_conns[task_id] = conn
            _task_apps[task_id] = task_app
        qa_context = load_json(Path(run_dir) / "qa_context.json") if (Path(run_dir) / "qa_context.json").is_file() else None
        final = task_app.invoke({"task_id": task_id, "pair_id": pair_id,
                                 "run_dir": run_dir, "steps": [],
                                 "tool_calls": [], "review_reasons": [],
                                 "qa_context": qa_context},
                                config=_config(task_id))
        _event(task_id, "task_completed", status=final.get("status"),
               disposition=final.get("disposition"), steps=final.get("steps") or [])
        _metric_finish(task_id, "completed")
    except Exception as exc:
        failure = _classify_failure(exc, current_tool)
        attempt = _task_attempt(task_id)
        failure.update({"task_id": task_id, "pair_id": pair_id,
                        "attempt": attempt, "retryable": attempt <= 2,
                        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")})
        save_json(_failure_path(task_id), failure)
        _event(task_id, "task_failed", **{k: v for k, v in failure.items() if k != "task_id"})
        _metric_finish(task_id, "failed", failure["error"])
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
    snap = _task_app(task_id).get_state(_config(task_id))
    values: dict[str, Any] = dict(final if final is not None else (snap.values or {}))
    try:
        batch_meta = load_json(_batch_meta_path(task_id)) if _batch_meta_path(task_id).is_file() else {}
    except (OSError, json.JSONDecodeError, TypeError):
        batch_meta = {}
    try:
        failure = load_json(_failure_path(task_id)) if _failure_path(task_id).is_file() else None
    except (OSError, json.JSONDecodeError, TypeError):
        failure = None
    acceptance = _load_acceptance(task_id)
    ra = values.get("risk_assessment") or {}
    evidence = {k: values.get(k) for k in _EVIDENCE_KEYS if values.get(k) is not None}
    execution_status = _derive_status(task_id, values)
    return {
        "task_id": task_id,
        "pair_id": values.get("pair_id"),
        "batch_id": batch_meta.get("batch_id") or values.get("batch_id"),
        "attempt": batch_meta.get("attempt", _task_attempt(task_id)),
        "parent_task_id": batch_meta.get("parent_task_id"),
        "status": execution_status,
        "execution_status": execution_status,
        "disposition": values.get("disposition"),
        # 前端安全日志需要完整判级依据；此前摘要只返回 overall_risk，
        # 导致任务已完成但 report/warnings 在安全日志页面为空。
        "risk_assessment": values.get("risk_assessment"),
        "overall_risk": ra.get("overall_risk"),
        "n_warnings": ra.get("n_warnings"),
        "need_human_review": values.get("need_human_review"),
        "human_archive": values.get("human_archive"),
        "review_reasons": values.get("review_reasons") or [],
        "human_verdict": values.get("human_verdict"),
        "equipment_check": values.get("equipment_check"),
        "quality": values.get("quality"),
        "scene": values.get("scene"),
        "reconciliation": values.get("reconciliation"),
        "rule": values.get("rule"),
        "report": values.get("report"),
        "judgement": values.get("judgement"),
        "evidence_check": values.get("evidence_check"),
        "case_memories": values.get("case_memories") or [],
        "route": values.get("route"),
        "steps": values.get("steps") or [],
        "tool_calls": values.get("tool_calls") or [],
        "evidence": evidence,
        "events_url": f"/tasks/{task_id}/events",
        "feedback_url": f"/tasks/{task_id}/feedback",
        "feedback_count": len(_load_feedback(task_id)),
        "error": values.get("error"),
        "failure": failure,
        "acceptance": acceptance[-1] if acceptance else None,
        "acceptance_history": acceptance,
    }


def _free_task_id(base: str) -> str:
    """自动编号避让：base 被占用时递增后缀（-2、-3…）。

    覆盖「run_dir 已删但 checkpointer 残留幽灵状态」与「同一图对再次研判」两类撞车，
    避免用户留空任务 ID 时莫名撞上 409。
    """
    with _task_lock:
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
    attempt = _task_attempt(task_id)
    parent_task_id = task_id.rsplit("-retry-", 1)[0] if "-retry-" in task_id else (task_id[:-6] if task_id.endswith("-retry") else None)
    batch_id = (body.batch_id or "").strip() or f"batch-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    with _task_lock:
        if _safe_state_values(task_id) or task_id in _task_futures:
            raise HTTPException(409,
                                f"任务 {task_id} 已存在（终态不可重跑）。重复研判请留空任务 ID 自动编号，或换一个新 ID")
    run_dir = str(PROJECT / "results" / "agent" / "graph_runs" / task_id)
    try:
        input_snapshot = _snapshot_pair_inputs(pair_id, run_dir)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(500, f"输入快照保存失败: {exc}") from exc
    if body.qa_session_id:
        session = qa_service.get_session(body.qa_session_id)
        if session is None:
            raise HTTPException(400, "qa_session_id 不存在或已失效")
        qa_service.bind_task(body.qa_session_id, task_id=task_id, pair_id=pair_id)
        save_json(Path(run_dir) / "qa_context.json", {
            "session_id": body.qa_session_id,
            "active_clauses": session.get("active_clauses", []),
            "turns": session.get("turns", [])[-5:],
        })
    _write_batch_meta(task_id, batch_id, pair_id, attempt=attempt, parent_task_id=parent_task_id)
    _metric_submit(task_id, attempt)
    with _task_lock:
        _event(task_id, "task_queued", pair_id=pair_id)
        _task_futures[task_id] = _executor.submit(_run_task, task_id, pair_id, run_dir)
    return {"task_id": task_id, "pair_id": pair_id, "batch_id": batch_id, "status": "queued",
            "events_url": f"/tasks/{task_id}/events", "input_snapshot": input_snapshot}


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
                  "warning_meta", "warning_overall_risk", "case_memories")


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
            return _task_app(task_id).get_state(_config(task_id)).values
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


@app.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, body: RetryIn | None = None) -> dict[str, Any]:
    """为执行失败任务创建有上限的新的尝试，业务终态不会被重复运行。"""
    values = _safe_state_values(task_id) or _state_from_disk(task_id) or {}
    future = _task_futures.get(task_id)
    status = _derive_status(task_id, values)
    if not values and future is None:
        raise HTTPException(404, f"未知任务: {task_id}")
    if status != "failed":
        raise HTTPException(409, f"任务当前状态为 {status}，只允许重试执行失败任务")
    pair_id = values.get("pair_id")
    if not pair_id:
        meta = _batch_meta_path(task_id)
        try:
            pair_id = load_json(meta).get("pair_id") if meta.is_file() else None
        except (OSError, json.JSONDecodeError, TypeError):
            pair_id = None
    if not pair_id:
        raise HTTPException(409, "失败任务缺少 pair_id，无法安全重试")
    try:
        meta = load_json(_batch_meta_path(task_id)) if _batch_meta_path(task_id).is_file() else {}
    except (OSError, json.JSONDecodeError, TypeError):
        meta = {}
    attempt = int(meta.get("attempt") or _task_attempt(task_id))
    max_retries = 2
    if attempt >= max_retries + 1:
        raise HTTPException(409, f"任务已达到最大重试次数（{max_retries} 次）")
    retry_id = f"{task_id}-retry" if attempt == 1 else f"{task_id}-retry-{attempt + 1}"
    if _safe_state_values(retry_id) or retry_id in _task_futures:
        raise HTTPException(409, f"任务已存在重试实例: {retry_id}")
    return create_task(TaskIn(pair_id=pair_id, task_id=retry_id,
                              batch_id=(body.batch_id if body and body.batch_id else meta.get("batch_id"))))


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
    _task_app(task_id).update_state(_config(task_id), values=patch)
    mark_review_resolved(task_id)
    return _snapshot(task_id, {**values, **patch})


@app.post("/tasks/{task_id}/acceptance")
def accept_completed_task(task_id: str, body: AcceptanceIn) -> dict[str, Any]:
    """对自动成功任务做异步验收；原始 Agent 判定永不覆盖。"""
    if body.verdict not in ("accept", "correct", "reject"):
        raise HTTPException(400, "verdict 仅允许 accept/correct/reject")
    values = _safe_state_values(task_id) or _state_from_disk(task_id)
    if not values and task_id not in _task_futures:
        raise HTTPException(404, f"未知任务: {task_id}")
    status = _derive_status(task_id, values or {})
    if status != "completed":
        raise HTTPException(409, f"任务当前状态为 {status}，仅允许验收已完成任务")
    if body.verdict in ("correct", "reject") and body.risk_label not in VALID_RISKS:
        raise HTTPException(400, "纠正/驳回必须提供 risk_label（安全/低风险/高风险）")
    with _FEEDBACK_LOCK:
        history = _load_acceptance(task_id)
        if history:
            raise HTTPException(409, "该任务已完成验收，不可重复提交")
        current_risk = ((values or {}).get("risk_assessment") or {}).get("overall_risk")
        record = {
            "acceptance_id": str(uuid.uuid4()), "task_id": task_id,
            "pair_id": (values or {}).get("pair_id"), "verdict": body.verdict,
            "agent_risk": current_risk, "agent_disposition": (values or {}).get("disposition"),
            "human_risk": body.risk_label if body.verdict != "accept" else current_risk,
            "human_disposition": body.disposition if body.verdict != "accept" else (values or {}).get("disposition"),
            "error_type": body.error_type, "notes": body.notes, "reviewer": body.reviewer,
            "policy_version": _policy_version()["version"],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        try:
            record["memory_id"] = record_acceptance(record, values or {})
        except Exception as exc:
            # 案例记忆是增强能力，不能阻塞正式验收；错误仍留在验收记录中。
            record["memory_error"] = str(exc)[:300]
        save_json(_acceptance_path(task_id), [record])
    _event(task_id, "human_acceptance_recorded", acceptance_id=record["acceptance_id"],
           verdict=record["verdict"], error_type=record["error_type"])
    return _snapshot(task_id, values)


@app.get("/tasks/{task_id}/feedback")
def get_task_feedback(task_id: str) -> dict[str, Any]:
    """读取任务的全部用户反馈，保留历史评价而不是覆盖上一条。"""
    values = _safe_state_values(task_id) or _state_from_disk(task_id)
    if not values and task_id not in _task_futures and not _feedback_path(task_id).is_file():
        raise HTTPException(404, f"未知任务: {task_id}")
    return {"task_id": task_id, "feedback": _load_feedback(task_id)}


@app.post("/tasks/{task_id}/feedback")
def add_task_feedback(task_id: str, body: FeedbackIn) -> dict[str, Any]:
    """记录任务完成后的质量反馈，供离线评估和后续版本比较使用。"""
    allowed_ratings = {"correct", "incorrect", "uncertain"}
    if body.rating not in allowed_ratings:
        raise HTTPException(400, "rating 仅允许 correct/incorrect/uncertain")
    if body.corrected_risk is not None and body.corrected_risk not in VALID_RISKS:
        raise HTTPException(400, "corrected_risk 必须是安全/低风险/高风险")
    values = _safe_state_values(task_id) or _state_from_disk(task_id)
    if not values and task_id not in _task_futures:
        raise HTTPException(404, f"未知任务: {task_id}")
    record = {
        "feedback_id": str(uuid.uuid4()),
        "task_id": task_id,
        "pair_id": values.get("pair_id") if values else None,
        "rating": body.rating,
        "agent_risk": ((values or {}).get("risk_assessment") or {}).get("overall_risk"),
        "agent_disposition": (values or {}).get("disposition"),
        "corrected_risk": body.corrected_risk,
        "corrected_disposition": body.corrected_disposition,
        "error_type": body.error_type,
        "notes": body.notes,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    with _FEEDBACK_LOCK:
        entries = _load_feedback(task_id)
        entries.append(record)
        save_json(_feedback_path(task_id), entries)
    _event(task_id, "user_feedback_recorded", feedback_id=record["feedback_id"],
           rating=record["rating"], error_type=record["error_type"])
    return {"task_id": task_id, "feedback": record, "feedback_count": len(entries)}


@app.get("/feedback/summary")
def feedback_summary() -> dict[str, Any]:
    """返回可审计的反馈分布，为后续离线校准提供数据边界。"""
    return _feedback_summary()


@app.get("/feedback/candidates")
def feedback_candidates() -> dict[str, Any]:
    """返回达到样本门槛的离线校准候选，不会自动应用。"""
    candidates = _feedback_candidates()
    return {"candidates": candidates, "count": len(candidates),
            "learning_mode": "offline_review_only"}


@app.get("/policy/version")
def policy_version() -> dict[str, Any]:
    """提供规则/判级代码指纹，供反馈校准和回归结果绑定版本。"""
    return _policy_version()


@app.get("/memory/cases")
def search_case_memories(q: str = "", work_type: str | None = None,
                         site_type: str | None = None, limit: int = 5) -> dict[str, Any]:
    """检索人工验收案例；仅返回可信记忆，不改变当前任务判定。"""
    from agent.case_memory import count_cases, search_cases
    cases = search_cases(q, work_type=work_type, site_type=site_type, limit=limit)
    return {"cases": cases, "count": len(cases), "total": count_cases(), "mode": "reference_only"}


def _batch_summaries() -> dict[str, dict[str, Any]]:
    root = PROJECT / "results" / "agent" / "graph_runs"
    groups: dict[str, dict[str, Any]] = {}
    for run in root.iterdir() if root.is_dir() else []:
        if not run.is_dir():
            continue
        try:
            meta = load_json(run / "batch.json") if (run / "batch.json").is_file() else {}
        except (OSError, json.JSONDecodeError, TypeError):
            meta = {}
        batch_id = meta.get("batch_id")
        if not batch_id:
            continue
        try:
            state = load_json(run / "graph_state.json") if (run / "graph_state.json").is_file() else {}
        except (OSError, json.JSONDecodeError, TypeError):
            state = {}
        try:
            failure = load_json(run / "failure.json") if (run / "failure.json").is_file() else None
        except (OSError, json.JSONDecodeError, TypeError):
            failure = None
        item = groups.setdefault(batch_id, {"batch_id": batch_id, "tasks": [], "created_at": meta.get("created_at")})
        disposition = state.get("disposition")
        status = "failed" if failure else ("completed" if state.get("status") == "completed" or disposition else "running")
        item["tasks"].append({"task_id": meta.get("task_id", run.name), "pair_id": meta.get("pair_id"),
                               "status": status, "disposition": disposition,
                               "overall_risk": (state.get("risk_assessment") or {}).get("overall_risk"),
                               "attempt": meta.get("attempt", _task_attempt(meta.get("task_id", run.name))),
                               "parent_task_id": meta.get("parent_task_id"),
                               "failure": failure})
    for item in groups.values():
        counts: dict[str, int] = {}
        for task in item["tasks"]:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        item["total"] = len(item["tasks"])
        item["counts"] = counts
        item["updated_at"] = max((t.get("task_id", "") for t in item["tasks"]), default="")
    return groups


@app.get("/batches")
def list_batches() -> dict[str, Any]:
    batches = list(_batch_summaries().values())
    batches.sort(key=lambda x: x.get("batch_id", ""), reverse=True)
    return {"batches": batches}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    item = _batch_summaries().get(batch_id)
    if not item:
        raise HTTPException(404, f"未知批次: {batch_id}")
    return item


@app.get("/health")
def health() -> dict[str, Any]:
    futures = list(_task_futures.values())
    active = sum(1 for future in futures if not future.done())
    feedback_count = 0
    if FEEDBACK_DIR.is_dir():
        feedback_count = sum(1 for item in FEEDBACK_DIR.glob("*.json") if item.is_file())
    with _METRICS_LOCK:
        metrics = dict(_runtime_metrics)
    finished = metrics["completed"] + metrics["failed"]
    metrics["avg_duration_ms"] = round(metrics["total_duration_ms"] / finished, 1) if finished else 0.0
    metrics.pop("total_duration_ms", None)
    return {
        "status": "ok",
        "ready": True,
        "worker_limit": getattr(_executor, "_max_workers", 1),
        "checkpoint_mode": "per_task",
        "active_tasks": active,
        "queue_depth": max(0, len([f for f in futures if not f.done()]) - getattr(_executor, "_max_workers", 1)),
        "known_tasks": len(futures),
        "metrics": metrics,
        "feedback_files": feedback_count,
        "checkpoint": DB_PATH.name,
    }
