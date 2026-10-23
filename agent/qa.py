"""Grounded regulation Q&A and session continuity for Binocle.

The first implementation is deliberately local and deterministic: it searches the
existing RAG chunks with a token-overlap scorer and stores the selected clauses in
SQLite. An optional LLM can be layered on later without changing the API contract.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from tools.common import project_root

PROJECT = project_root()
QA_DB = PROJECT / "results" / "agent" / "qa_sessions.sqlite"
CHUNKS = PROJECT / "results" / "rag_store_v2" / "chunks.jsonl"


def _conn() -> sqlite3.Connection:
    QA_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(QA_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        active_clauses TEXT NOT NULL DEFAULT '[]', turns TEXT NOT NULL DEFAULT '[]',
        bound_task_id TEXT, bound_pair_id TEXT, events TEXT NOT NULL DEFAULT '[]')""")
    # Forward-compatible migration for sessions created before the unified
    # event stream was introduced.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "events" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN events TEXT NOT NULL DEFAULT '[]'")
    conn.commit()
    return conn


def _tokens(text: str) -> set[str]:
    # Chinese character bigrams plus alphanumeric terms gives useful retrieval
    text = re.sub(r"\s+", "", str(text or "").lower())
    terms = set(re.findall(r"[a-z0-9_\-\.]+", text))
    terms.update(text[i:i + 2] for i in range(max(0, len(text) - 1)))
    return {t for t in terms if t}


def _chunks() -> list[dict[str, Any]]:
    if not CHUNKS.exists():
        return []
    rows = []
    for line in CHUNKS.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("text"):
                rows.append(item)
        except json.JSONDecodeError:
            continue
    return rows


def search_clauses(question: str, topk: int = 5) -> list[dict[str, Any]]:
    q = _tokens(question)
    scored = []
    for item in _chunks():
        text = str(item.get("text", ""))
        overlap = len(q & _tokens(text))
        # exact document/clause references are strong signals
        if str(item.get("doc_id", "")).lower() in question.lower():
            overlap += 4
        score = overlap / max(1, len(q) ** 0.5)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    hits = []
    for score, item in scored[:topk]:
        hits.append({**item, "score": round(float(score), 4), "retrieval_mode": "local_token_overlap"})
    return hits


def _ensure_session(session_id: str | None) -> tuple[sqlite3.Connection, str]:
    conn = _conn()
    sid = session_id or f"qa-{uuid.uuid4().hex[:12]}"
    now = time.time()
    conn.execute("INSERT OR IGNORE INTO sessions(session_id,created_at,updated_at) VALUES(?,?,?)", (sid, now, now))
    return conn, sid


def ask(question: str, session_id: str | None = None, pair_id: str | None = None,
        task_id: str | None = None) -> dict[str, Any]:
    question = str(question or "").strip()
    if not question:
        raise ValueError("question 不能为空")
    conn, sid = _ensure_session(session_id)
    hits = search_clauses(question)
    now = time.time()
    clauses = [{k: h.get(k) for k in ("doc_id", "doc_name", "clause_ids", "full_path", "text", "score", "retrieval_mode")} for h in hits]
    turn = {"question": question, "asked_at": now, "citations": clauses}
    row = conn.execute("SELECT turns FROM sessions WHERE session_id=?", (sid,)).fetchone()
    turns = json.loads(row[0] or "[]") if row else []
    turns.append(turn)
    active = [{k: h.get(k) for k in ("doc_id", "doc_name", "clause_ids", "full_path", "text")} for h in hits[:3]]
    row_events = conn.execute("SELECT events FROM sessions WHERE session_id=?", (sid,)).fetchone()
    events = json.loads(row_events[0] or "[]") if row_events else []
    events.extend([
        {"role": "user", "content": question, "created_at": now, "pair_id": pair_id, "task_id": task_id},
        {"role": "tool", "tool_name": "knowledge_search", "content": clauses, "created_at": now, "pair_id": pair_id, "task_id": task_id},
    ])
    conn.execute("UPDATE sessions SET updated_at=?, active_clauses=?, turns=?, events=?, bound_pair_id=COALESCE(?,bound_pair_id), bound_task_id=COALESCE(?,bound_task_id) WHERE session_id=?",
                 (now, json.dumps(active, ensure_ascii=False), json.dumps(turns[-20:], ensure_ascii=False), json.dumps(events[-100:], ensure_ascii=False), pair_id, task_id, sid))
    conn.commit(); conn.close()
    if hits:
        answer = "根据规范库检索，相关条款为：\n" + "\n".join(f"- {h.get('full_path') or h.get('clause_ids')}: {h['text'][:420]}" for h in hits[:3])
    else:
        answer = "规范库中没有检索到足够相关的条款，不能据此作出合规结论。"
    return {"session_id": sid, "answer": answer, "citations": clauses, "active_clauses": active,
            "bound_pair_id": pair_id, "bound_task_id": task_id, "grounded": bool(hits)}


def get_session(session_id: str) -> dict[str, Any] | None:
    conn = _conn(); row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone(); conn.close()
    if not row: return None
    return {"session_id": row["session_id"], "active_clauses": json.loads(row["active_clauses"] or "[]"),
            "turns": json.loads(row["turns"] or "[]"), "events": json.loads(row["events"] or "[]"),
            "bound_task_id": row["bound_task_id"], "bound_pair_id": row["bound_pair_id"]}


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """Return recent conversations without exposing full message bodies."""
    conn = _conn()
    rows = conn.execute(
        "SELECT session_id, created_at, updated_at, turns, bound_task_id, bound_pair_id "
        "FROM sessions ORDER BY updated_at DESC LIMIT ?", (max(1, min(100, int(limit))),)
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        turns = json.loads(row["turns"] or "[]")
        first = next((t for t in turns if t.get("question")), {})
        result.append({
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "title": str(first.get("question") or "新对话")[:48],
            "turn_count": len(turns),
            "bound_task_id": row["bound_task_id"],
            "bound_pair_id": row["bound_pair_id"],
        })
    return result


def ensure_session(session_id: str | None = None) -> str:
    conn, sid = _ensure_session(session_id)
    conn.commit(); conn.close()
    return sid


def bind_task(session_id: str, task_id: str | None = None, pair_id: str | None = None) -> dict[str, Any]:
    conn = _conn(); now = time.time()
    cur = conn.execute("UPDATE sessions SET updated_at=?, bound_task_id=COALESCE(?,bound_task_id), bound_pair_id=COALESCE(?,bound_pair_id) WHERE session_id=?", (now, task_id, pair_id, session_id))
    conn.commit(); conn.close()
    if not cur.rowcount: raise KeyError(session_id)
    return get_session(session_id) or {}


def record_turn(session_id: str, user_message: str, assistant_message: str,
                trace: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Persist a non-RAG conversational turn in the same session store."""
    conn, sid = _ensure_session(session_id)
    row = conn.execute("SELECT turns FROM sessions WHERE session_id=?", (sid,)).fetchone()
    turns = json.loads(row[0] or "[]") if row else []
    now = time.time()
    turns.append({"question": user_message, "answer": assistant_message,
                  "trace": trace or [], "asked_at": now})
    event_row = conn.execute("SELECT events FROM sessions WHERE session_id=?", (sid,)).fetchone()
    events = json.loads(event_row[0] or "[]") if event_row else []
    events.append({"role": "user", "content": user_message, "created_at": now})
    for step in trace or []:
        if step.get("tool"):
            events.append({"role": "tool", "tool_name": step.get("tool"), "tool_call": step.get("arguments", {}), "tool_result": step.get("observation"), "created_at": now})
    events.append({"role": "assistant", "content": assistant_message, "trace": trace or [], "created_at": now})
    conn.execute("UPDATE sessions SET updated_at=?, turns=?, events=? WHERE session_id=?",
                 (now, json.dumps(turns[-40:], ensure_ascii=False), json.dumps(events[-100:], ensure_ascii=False), sid))
    conn.commit(); conn.close()
    return get_session(sid) or {}
