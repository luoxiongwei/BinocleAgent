"""Semantic conversation runtime for Binocle."""
from __future__ import annotations
import json
import re
from typing import Any
from agent import qa
from agent.agent_loop import AgentLoop
from agent.tool_contracts import TOOLS
from agent.specialists import SPECIALISTS
from tools.api_config import load_api_config
from tools.common import project_root
from tools.model_gateway import ModelGateway

PROJECT = project_root()

def tool_schemas() -> list[dict[str, Any]]:
    return TOOLS

def _intent(text: str) -> str:
    t = text.lower()
    if re.fullmatch(r"[\s\W]*(你好|您好|嗨|hello|hi|hey|谢谢|感谢|再见|拜拜)[\s\W]*", t): return "smalltalk"
    if any(x in t for x in ("反馈", "验收", "纠正", "判断正确", "判断错误", "判错")): return "task_feedback"
    if any(x in t for x in ("上传", "图对", "图片", "红外", "rgb", "安全日志", "研判")): return "safety_log"
    if any(x in t for x in ("查询任务", "任务状态", "进度", "graph-", "task-")): return "task_query"
    if "重试" in t or "重新执行" in t: return "task_retry"
    return "knowledge_qa"

def _llm_answer(question: str, citations: list[dict[str, Any]], history: list[dict[str, Any]]) -> str | None:
    try:
        cfg = load_api_config(PROJECT)
        if not (cfg.get("llm") or cfg.get("api")): return None
        gateway, model = ModelGateway.from_project_config(PROJECT, "llm", timeout_seconds=60, temperature=0.1)
        sources = "\n".join(f"[{c.get('doc_id')} {c.get('full_path')}] {c.get('text','')[:800]}" for c in citations[:5])
        messages = [{"role": "system", "content": "You are Binocle safety-log Agent. Answer only from cited standards; say when evidence is insufficient."}]
        messages += history[-6:]
        messages.append({"role": "user", "content": f"Question: {question}\nSources:\n{sources}"})
        result = gateway.complete(model=model, messages=messages)
        if result.status == "ok": return ((result.data or {}).get("content") or "").strip() or None
    except Exception: return None
    return None

def run_llm_loop(message: str, history: list[dict[str, Any]], execute: Any) -> dict[str, Any] | None:
    """Semantic model triage followed by a scoped specialist tool loop."""
    try:
        cfg = load_api_config(PROJECT)
        if not (cfg.get("llm") or cfg.get("api")): return None
        # Triage must be repeatable across the direct backend and the Vite
        # proxy; specialist generation may remain creative, but routing is not.
        gateway, model = ModelGateway.from_project_config(PROJECT, "llm", timeout_seconds=90, temperature=0.0)
        triage_messages = [{"role": "system", "content": (
            "You are Binocle's semantic triage agent. Understand intent, do not guess from keywords. "
            'Return JSON only: {"mode":"smalltalk|knowledge|safety_log|task_ops|feedback|unclear", '
            '"reason":"brief reason","needs_clarification":true|false}. '
            "smalltalk is greetings or unrelated conversation; knowledge is an explicit standards question; "
            "safety_log covers image analysis and creating or explaining the safety-log workflow; "
            "task_ops covers task status, events, pending work and retry; feedback covers explicit evaluation or correction of an existing result. "
            "Use the whole conversation and available task context, not isolated keywords. "
            "If work is requested but required information is missing, select the relevant specialist and let it ask a clarifying question. "
            "Use unclear only when no responsible capability can be identified."
        )}]
        normalized_history = []
        for item in history[-12:]:
            if item.get("role") in {"user", "assistant"} and item.get("content"):
                normalized_history.append({"role": item["role"], "content": str(item["content"])})
            elif item.get("question"):
                normalized_history.append({"role": "user", "content": str(item["question"])})
                if item.get("answer"):
                    normalized_history.append({"role": "assistant", "content": str(item["answer"])})
        triage_messages.extend(normalized_history[-12:]); triage_messages.append({"role": "user", "content": message})
        triage = gateway.complete(model=model, messages=triage_messages, response_format={"type": "json_object"})
        try: triage_data = json.loads((triage.data or {}).get("content") or "{}") if triage.status == "ok" else {}
        except (TypeError, ValueError): triage_data = {}
        mode = str(triage_data.get("mode") or "unclear")
        if mode not in {"smalltalk", "knowledge", "safety_log", "task_ops", "feedback", "unclear"}: mode = "unclear"
        if mode in {"smalltalk", "unclear"}:
            msgs = [{"role": "system", "content": "You are Binocle safety-log Agent. Reply naturally. For unrelated conversation, respond normally. If unclear, explain capabilities and ask one clarifying question. Do not retrieve standards or invent task results."}]
            msgs.extend(normalized_history[-8:]); msgs.append({"role": "user", "content": message})
            answer = gateway.complete(model=model, messages=msgs)
            return {"answer": ((answer.data or {}).get("content") or "").strip(), "trace": [{"stage": "triage", "mode": mode}], "mode": "triage_conversation"}
        specialist = SPECIALISTS[mode]
        allowed = specialist.tools
        scoped = [t for t in TOOLS if t.get("function", {}).get("name") in allowed]
        specialist_instruction = f"You are Binocle's {mode} specialist Agent. Call tools for facts, ask for missing parameters, and explain results instead of dumping JSON."
        if mode == "safety_log":
            specialist_instruction += (
                " You have access to the create_safety_log tool for the existing RGB/infrared safety-log pipeline. "
                "Never claim that Binocle has no system integration or cannot create a safety log. "
                "If pair_id is missing, ask for it; explain that creation queues the existing pipeline and the final log is produced by that pipeline."
            )
        msgs = [{"role": "system", "content": specialist_instruction}]
        msgs.extend(normalized_history[-12:]); msgs.append({"role": "user", "content": message})
        result = AgentLoop(gateway.complete, scoped, execute, max_steps=5).run(msgs, model)
        return {"answer": result.get("content", ""), "trace": [{"stage": "triage", "mode": mode}, {"stage": "handoff", "agent": specialist.name}] + result.get("trace", []), "mode": "specialist_tool_loop", "agent": specialist.name}
    except Exception:
        return None

def chat(message: str, session_id: str | None = None, pair_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
    message = str(message or "").strip()
    if not message: raise ValueError("message cannot be empty")
    intent = _intent(message)
    if intent == "smalltalk": return {"intent": intent, "action": "chat", "answer": "你好，我是 Binocle 安全日志 Agent。你可以和我自由交流，也可以询问规范、查询任务或上传图像生成安全日志。"}
    if intent == "knowledge_qa":
        result = qa.ask(message, session_id, pair_id, task_id); session = qa.get_session(result["session_id"]) or {}
        generated = _llm_answer(message, result.get("citations", []), session.get("turns", []))
        if generated: result["answer"] = generated; result["answer_mode"] = "llm_grounded"
        else: result["answer_mode"] = "extractive_grounded"
        result["intent"] = intent; return result
    if intent in {"task_query", "task_retry"}:
        found = task_id or (re.search(r"(?:graph|task)-[A-Za-z0-9_-]+", message) or [None])[0]
        return {"intent": intent, "action": "query_task" if intent == "task_query" else "retry_task", "task_id": found, "answer": "请提供任务 ID，我会查询或重试该任务。"}
    if intent == "task_feedback":
        found = task_id or (re.search(r"(?:graph|task)-[A-Za-z0-9_-]+", message) or [None])[0]
        rating = "incorrect" if any(x in message for x in ("错误", "判错", "不对")) else "correct"
        corrected = next((risk for risk in ("高风险", "低风险", "安全") if risk in message), None)
        return {"intent": intent, "action": "submit_feedback", "task_id": found, "rating": rating, "corrected_risk": corrected, "answer": "请提供任务 ID 和你的验收意见。"}
    return {"intent": intent, "action": "create_safety_log", "session_id": session_id, "pair_id": pair_id, "task_id": task_id, "answer": "请在图像上传区提交 RGB/红外图对，创建安全日志任务。"}
