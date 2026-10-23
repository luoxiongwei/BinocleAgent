"""Small, provider-neutral Agent Loop.

The loop owns conversation turns and tool-call boundaries; domain tools remain
in service.py and are injected by the caller. This keeps RAG and safety-log
workflows from becoming UI-specific branches.
"""
from __future__ import annotations

from typing import Any, Callable


class AgentLoop:
    def __init__(self, complete: Callable[..., Any], tools: list[dict[str, Any]],
                 execute: Callable[[str, dict[str, Any]], dict[str, Any]], max_steps: int = 5):
        self.complete = complete
        self.tools = tools
        self.execute = execute
        self.max_steps = max(1, max_steps)

    def run(self, messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
        history = list(messages)
        trace: list[dict[str, Any]] = []
        for _ in range(self.max_steps):
            result = self.complete(model=model, messages=history, tools=self.tools)
            data = getattr(result, "data", None) or {}
            if getattr(result, "status", None) != "ok":
                return {"status": "error", "error": getattr(result, "error", "model call failed"), "trace": trace}
            calls = data.get("tool_calls") or []
            content = data.get("content")
            if not calls:
                return {"status": "ok", "content": content or "", "trace": trace, "messages": history}
            assistant = {"role": "assistant", "content": content or "", "tool_calls": calls}
            history.append(assistant)
            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    import json
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                observation = self.execute(name, args)
                trace.append({"tool": name, "arguments": args, "observation": observation})
                history.append({"role": "tool", "tool_call_id": call.get("id"),
                                "name": name, "content": json.dumps(observation, ensure_ascii=False)})
        return {"status": "error", "error": "agent loop exceeded max_steps", "trace": trace}
