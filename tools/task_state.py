# -*- coding: utf-8 -*-
"""统一任务状态初始化。"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


def new_task(task_id: str | None = None, task_type: str = "施工安全巡检",
             input_data: dict | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "task_id": task_id or f"inspection-{uuid4().hex[:12]}",
        "task_type": task_type,
        "input": input_data or {},
        "quality": {}, "detections": {}, "scene": {}, "relations": [],
        "regulations": [], "evidence_check": {}, "risk_assessment": {},
        "report": {},
        "workflow": {
            "current_step": "RECEIVED", "completed_steps": [],
            "retry_count": 0, "need_human_review": False, "status": "pending"
        },
        "audit": {
            "tool_calls": [], "prompt_versions": {},
            "created_at": now, "updated_at": now
        }
    }
