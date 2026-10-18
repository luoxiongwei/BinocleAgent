# -*- coding: utf-8 -*-
"""从项目根目录 config.json 读取 API 配置。"""
from __future__ import annotations

import json
from pathlib import Path


def load_api_config(base: Path) -> dict:
    path = base / "config.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少 API 配置文件: {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg.get("vlm"), dict) or not isinstance(cfg.get("llm"), dict):
        raise ValueError("config.json 必须包含 vlm 和 llm 配置段")
    return cfg


def require_endpoint(cfg: dict, section: str) -> tuple[str, str, str]:
    item = cfg[section]
    for key in ("model_id", "api_base", "api_key"):
        if not item.get(key):
            raise ValueError(f"config.json 的 {section}.{key} 未配置")
    api_key = str(item["api_key"])
    # The distributed template uses a Chinese placeholder.  Letting it reach
    # the HTTP client produces a cryptic ASCII header-encoding exception;
    # reject it at the configuration boundary with an actionable message.
    if "在这里填入" in api_key or "你的" in api_key:
        raise ValueError(f"config.json 的 {section}.api_key 仍是模板占位符，请填入真实 API Key")
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"config.json 的 {section}.api_key 必须是 ASCII 字符串") from exc
    return item["model_id"], item["api_base"], api_key
