"""Specialist-agent registry used by the semantic conversation runtime.

This is intentionally provider-neutral: the existing model gateway remains the
LLM provider, while these specs define ownership, tools and handoff context.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Specialist:
    mode: str
    name: str
    description: str
    tools: frozenset[str]


SPECIALISTS = {
    "knowledge": Specialist("knowledge", "Knowledge Agent", "Answer explicit standards questions with citations.", frozenset({"knowledge_search", "search_memory"})),
    "safety_log": Specialist("safety_log", "Safety Log Agent", "Create and explain RGB/infrared safety-log tasks.", frozenset({"create_safety_log", "search_memory", "knowledge_search"})),
    "task_ops": Specialist("task_ops", "Task Operations Agent", "Inspect task state, events, pending work and retries.", frozenset({"query_task", "retry_task", "list_pending_tasks", "get_task_events"})),
    "feedback": Specialist("feedback", "Feedback Agent", "Record acceptance, correction and uncertainty for completed tasks.", frozenset({"submit_feedback", "query_task", "search_memory"})),
}


def get_specialist(mode: str) -> Specialist | None:
    return SPECIALISTS.get(mode)
