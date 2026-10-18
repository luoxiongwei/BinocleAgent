# -*- coding: utf-8 -*-
"""统一模型 API 网关接口。

阶段一只完成边界和错误规范化，具体视觉/文本调用在后续接入 Agent runner 时启用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api_config import load_api_config, require_endpoint
from .common import ToolResult


@dataclass
class GatewayConfig:
    api_base: str
    api_key: str | None = None
    api_key_env: str = "DASHSCOPE_API_KEY"
    timeout_seconds: int = 120
    temperature: float = 0.0


class ModelGateway:
    def __init__(self, config: GatewayConfig):
        self.config = config

    def _client(self):
        key = self.config.api_key or os.environ.get(self.config.api_key_env)
        if not key:
            raise RuntimeError(f"未配置 API Key（优先 config.json，其次环境变量 {self.config.api_key_env}）")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("模型 API 调用需要安装 openai") from exc
        return OpenAI(base_url=self.config.api_base, api_key=key,
                      timeout=self.config.timeout_seconds)

    @classmethod
    def from_project_config(cls, project: str | Path, section: str,
                            *, timeout_seconds: int = 120,
                            temperature: float = 0.0) -> tuple["ModelGateway", str]:
        """Create a gateway from the project's config.json without CLI key arguments."""
        cfg = load_api_config(Path(project))
        model, api_base, api_key = require_endpoint(cfg, section)
        gateway = cls(GatewayConfig(api_base=api_base, api_key=api_key,
                                    timeout_seconds=timeout_seconds,
                                    temperature=temperature))
        return gateway, model

    def complete(self, *, model: str, messages: list[dict[str, Any]],
                 response_format: dict[str, Any] | None = None,
                 tools: list[dict[str, Any]] | None = None) -> ToolResult:
        """统一文本/多模态 Chat Completions 调用入口。

        tools 不为空时启用 function-calling：返回的 data 中带 tool_calls
        （由上层执行工具后回填消息继续对话）；此时不传 response_format。
        """
        try:
            kwargs: dict[str, Any] = {
                "model": model, "messages": messages,
                "temperature": self.config.temperature,
            }
            if tools:
                kwargs["tools"] = tools
            elif response_format:
                kwargs["response_format"] = response_format
            response = self._client().chat.completions.create(**kwargs)
            message = response.choices[0].message
            content = message.content
            usage = getattr(response, "usage", None)
            data: dict[str, Any] = {
                "content": content,
                # ReAct 之 Thought：推理模型的显式推理链（如 deepseek reasoner/flash），
                # 透传给上层写入审计，形成 Thought-Action-Observation 完整轨迹
                "reasoning": getattr(message, "reasoning_content", None),
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                },
            }
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                data["tool_calls"] = [
                    {"id": t.id, "type": "function",
                     "function": {"name": t.function.name,
                                  "arguments": t.function.arguments}}
                    for t in tool_calls]
            result = ToolResult("ok", data)
            result.add_trace("model.complete", model=model, with_tools=bool(tool_calls))
            return result
        except Exception as exc:  # API、解析和网络错误统一交给上层策略处理
            result = ToolResult("error", error=f"模型调用失败: {exc}")
            result.add_trace("model.complete", model=model)
            return result
