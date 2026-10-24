"""本地、可审计的人工验收案例记忆。

第一版只使用 SQLite 结构化字段和 FTS5/LIKE 检索，不改变线上判级结果；
只有人工验收过的案例才会写入，供后续离线评估和受控检索使用。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from tools.common import project_root

PROJECT = project_root()
MEMORY_DB = PROJECT / "results" / "agent" / "case_memory.sqlite"
_LOCK = threading.RLock()


def _connect() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MEMORY_DB, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS case_memories (
        memory_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL, pair_id TEXT,
        work_type TEXT, site_type TEXT, scene_text TEXT NOT NULL,
        agent_risk TEXT, human_risk TEXT, agent_disposition TEXT,
        human_disposition TEXT, verdict TEXT, error_type TEXT, notes TEXT,
        policy_version TEXT, case_kind TEXT, facts_json TEXT,
        created_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 1
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(case_memories)").fetchall()}
    if "case_kind" not in columns:
        conn.execute("ALTER TABLE case_memories ADD COLUMN case_kind TEXT")
    if "facts_json" not in columns:
        conn.execute("ALTER TABLE case_memories ADD COLUMN facts_json TEXT")
    conn.execute("""UPDATE case_memories SET case_kind = CASE verdict
        WHEN 'accept' THEN 'positive' WHEN 'correct' THEN 'correction'
        WHEN 'reject' THEN 'counterexample' ELSE 'reviewed' END
        WHERE case_kind IS NULL""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_case_work_site ON case_memories(work_type, site_type)")
    conn.commit()
    return conn


def record_acceptance(record: dict[str, Any], state: dict[str, Any]) -> str:
    """将人工验收结果沉淀为一条可追溯案例；同一任务幂等更新。"""
    scene = state.get("scene") or {}
    summary = scene.get("scene") if isinstance(scene.get("scene"), dict) else {}
    work_type = str(summary.get("dominant_state") or "")
    site_type = str(summary.get("site_type") or "")
    scene_text = " ".join(str(x) for x in (
        work_type, site_type, summary.get("description"),
        record.get("error_type"), record.get("notes")) if x)
    targets = scene.get("targets") or []
    equipment_states = []
    machinery_radius = False
    crane_zone = False
    for target in targets:
        if not isinstance(target, dict):
            continue
        if target.get("type") in ("机械", "塔吊"):
            state_value = (target.get("equipment_state") or {}).get("value")
            if state_value:
                equipment_states.append(state_value)
        attrs = target.get("state_attrs") or {}
        machinery_radius = machinery_radius or (attrs.get("in_machinery_radius") or {}).get("value") is True
        crane_zone = crane_zone or (attrs.get("in_crane_load_zone") or {}).get("value") is True
    facts = {
        "equipment_states": sorted(set(equipment_states)),
        "machinery_radius": machinery_radius,
        "crane_load_zone": crane_zone,
        "place": summary.get("place"),
        "work_height": summary.get("work_height"),
        "env_protections": summary.get("env_protections") or [],
    }
    case_kind = {"accept": "positive", "correct": "correction", "reject": "counterexample"}.get(
        record.get("verdict"), "reviewed")
    memory_id = str(uuid.uuid4())
    with _LOCK:
        conn = _connect()
        conn.execute("""INSERT INTO case_memories
            (memory_id, task_id, pair_id, work_type, site_type, scene_text,
             agent_risk, human_risk, agent_disposition, human_disposition,
             verdict, error_type, notes, policy_version, case_kind, facts_json, created_at, approved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(task_id) DO UPDATE SET
             pair_id=excluded.pair_id, work_type=excluded.work_type, site_type=excluded.site_type,
             scene_text=excluded.scene_text, agent_risk=excluded.agent_risk,
             human_risk=excluded.human_risk, agent_disposition=excluded.agent_disposition,
             human_disposition=excluded.human_disposition, verdict=excluded.verdict,
             error_type=excluded.error_type, notes=excluded.notes,
             policy_version=excluded.policy_version, case_kind=excluded.case_kind,
             facts_json=excluded.facts_json,
             created_at=excluded.created_at,
             approved=excluded.approved""", (
                memory_id, record.get("task_id"), record.get("pair_id"), work_type, site_type,
                scene_text, record.get("agent_risk"), record.get("human_risk"),
                record.get("agent_disposition"), record.get("human_disposition"),
                record.get("verdict"), record.get("error_type"), record.get("notes"),
                record.get("policy_version"), case_kind, json.dumps(facts, ensure_ascii=False),
                record.get("created_at")))
        conn.commit()
        conn.close()
    return memory_id


def count_cases() -> int:
    """已沉淀（approved）案例总数，供经验记忆面板展示。"""
    with _LOCK:
        conn = _connect()
        return int(conn.execute("SELECT COUNT(*) FROM case_memories WHERE approved = 1").fetchone()[0])


def search_cases(query: str = "", work_type: str | None = None,
                 site_type: str | None = None, limit: int = 5,
                 exclude_task_id: str | None = None) -> list[dict[str, Any]]:
    """检索已人工验收案例。

    精确字段命中优先；回退检索只接受共享的领域词/作业族或较长短语，
    不再把“现场、人员、作业、机械”等泛词当作相似依据。
    """
    limit = max(1, min(20, int(limit)))
    clauses = ["approved = 1"]
    params: list[Any] = []
    if exclude_task_id:
        clauses.append("task_id != ?")
        params.append(exclude_task_id)
    if work_type:
        clauses.append("work_type = ?")
        params.append(work_type)
    if site_type:
        clauses.append("site_type = ?")
        params.append(site_type)
    terms = [term for term in re.split(r"\s+", str(query).strip()) if term]
    for term in terms:
        clauses.append("scene_text LIKE ?")
        params.append(f"%{term}%")
    where = " AND ".join(clauses)
    with _LOCK:
        conn = _connect()
        rows = conn.execute(f"SELECT * FROM case_memories WHERE {where} ORDER BY created_at DESC LIMIT ?",
                            [*params, limit]).fetchall()
        names = [item[1] for item in conn.execute("PRAGMA table_info(case_memories)").fetchall()]
        mode = "exact"
        if not rows and (work_type or site_type or query):
            text = " ".join(str(value or "") for value in (work_type, site_type, query))
            critical = {"基坑", "塔吊", "砌筑", "土方", "脚手", "模板", "钢筋",
                        "木工", "切割", "装卸", "巡查", "灌注", "浇筑", "反光衣",
                        "安全带", "吊装", "焊接", "高处", "临边", "洞口", "PPE"}
            families = {
                "砌筑": {"砌筑", "砖", "墙", "抹灰"},
                "土方": {"土方", "基坑", "开挖", "挖机", "装车"},
                "塔吊": {"塔吊", "吊装", "起重", "吊运"},
                "脚手架": {"脚手", "架体", "高处", "临边"},
                "钢筋": {"钢筋", "绑扎", "模板", "混凝土", "浇筑", "灌注"},
                "切割": {"切割", "焊接", "打磨"},
            }
            def phrases(value: str) -> set[str]:
                # 仅保留四字短语，避免二字泛词带来的跨作业误召回。
                return {value[i:i+4] for i in range(max(0, len(value)-3))
                        if len(value[i:i+4].strip()) == 4 and
                        re.search(r"[\u4e00-\u9fff]", value[i:i+4])}
            query_phrases = phrases(text)
            query_critical = {word for word in critical if word in text}
            query_family = {name for name, words in families.items()
                            if any(word in text for word in words)}
            fallback = ["approved = 1"]
            fallback_params: list[Any] = []
            if exclude_task_id:
                fallback.append("task_id != ?")
                fallback_params.append(exclude_task_id)
            rows = conn.execute(
                f"SELECT * FROM case_memories WHERE {' AND '.join(fallback)} ORDER BY created_at DESC LIMIT 200",
                fallback_params).fetchall()
            mode = "semantic_structured_fallback"
        conn.close()
    result = [dict(zip(names, row)) for row in rows]
    if mode == "semantic_structured_fallback":
        scored = []
        for item in result:
            scene_text = str(item.get("scene_text") or "")
            candidate_phrases = phrases(scene_text)
            shared_phrases = query_phrases & candidate_phrases
            shared_critical = {word for word in query_critical if word in scene_text}
            shared_family = {name for name in query_family
                             if any(word in scene_text for word in families[name])}
            score = 2 * len(shared_phrases) + 4 * len(shared_critical) + 3 * len(shared_family)
            # 至少共享一个领域词/作业族，或两个四字短语；泛词不得单独触发。
            if score >= 4 and (shared_critical or shared_family or len(shared_phrases) >= 2):
                item["retrieval_score"] = score
                item["matched_terms"] = sorted(shared_critical | shared_family | shared_phrases)
                scored.append(item)
        result = sorted(scored, key=lambda item: (-item["retrieval_score"], item.get("created_at", "")))[:limit]
    for item in result:
        item["retrieval_mode"] = mode
    return result


def summarize_memory_guidance(cases: list[dict[str, Any]],
                              current_risk: str | None = None) -> dict[str, Any]:
    """将检索结果压缩成可审计的建议，不直接改写实时判级。

    只有至少两条人工验收案例形成同一风险/处置多数时才产生建议；
    若历史建议低于当前高风险，则明确标记冲突并要求人工/规则复核，
    防止历史案例把新的即时高危证据降级。
    """
    valid = [item for item in cases
             if item.get("human_risk") and item.get("human_disposition")]
    if not valid:
        return {"available": False, "reason": "no_approved_labels", "count": 0}
    from collections import Counter
    votes = Counter((item.get("human_risk"), item.get("human_disposition")) for item in valid)
    (risk, disposition), count = votes.most_common(1)[0]
    ratio = count / len(valid)
    guidance: dict[str, Any] = {
        "available": count >= 2 and ratio >= 0.67,
        "count": len(valid),
        "support": count,
        "support_ratio": round(ratio, 3),
        "suggested_risk": risk,
        "suggested_disposition": disposition,
        "vote_breakdown": [{"risk": r, "disposition": d, "count": n}
                            for (r, d), n in votes.most_common()],
        "source_memory_ids": [item.get("memory_id") for item in valid[:5]],
    }
    if not guidance["available"]:
        guidance["reason"] = "insufficient_consensus"
        return guidance
    risk_rank = {"安全": 0, "低风险": 1, "高风险": 2}
    if current_risk == "高风险" and risk_rank.get(str(risk), 2) < 2:
        guidance.update({"action": "conflict_review", "reason": "historical_lower_than_current_high"})
    elif current_risk and risk == current_risk:
        guidance.update({"action": "advisory_alignment", "reason": "historical_agrees"})
    else:
        guidance.update({"action": "advisory_only", "reason": "never_override_live_evidence"})
    return guidance
