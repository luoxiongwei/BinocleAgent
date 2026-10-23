"""Stable function contracts exposed to the conversation runtime."""
from __future__ import annotations

TOOLS = [
    {"type": "function", "function": {"name": "knowledge_search", "description": "Search the standards knowledge base for an explicit clause question.", "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}},
    {"type": "function", "function": {"name": "create_safety_log", "description": "Create a safety-log task for an uploaded RGB/infrared pair.", "parameters": {"type": "object", "properties": {"pair_id": {"type": "string"}, "qa_session_id": {"type": "string"}}, "required": ["pair_id"]}}},
    {"type": "function", "function": {"name": "query_task", "description": "Read one task's status, risk and disposition.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "retry_task", "description": "Retry a failed safety-log task.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "list_pending_tasks", "description": "List pending human-review, running and failed tasks with counts.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_task_events", "description": "Read the event stream and execution trace for one task.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "submit_feedback", "description": "Record a human acceptance, correction or uncertainty for a task result.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, "rating": {"type": "string", "enum": ["correct", "incorrect", "uncertain"]}, "corrected_risk": {"type": "string"}, "notes": {"type": "string"}}, "required": ["task_id", "rating"]}}},
    {"type": "function", "function": {"name": "search_memory", "description": "Find similar historical cases and human feedback as reference only.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}},
]
