# -*- coding: utf-8 -*-
"""阶段一配置读取，避免工具依赖硬编码绝对路径。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # 允许只使用默认配置运行基础工具
    yaml = None

from .common import project_root


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else project_root() / "config" / "agent.yaml"
    if not target.exists():
        raise FileNotFoundError(target)
    if yaml is None:
        raise RuntimeError("读取 agent.yaml 需要安装 PyYAML")
    value = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    value["_config_path"] = str(target)
    value["_project_root"] = str(target.resolve().parent.parent)
    return value


def configured_path(name: str, config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    relative = cfg.get("paths", {}).get(name)
    if not relative:
        raise KeyError(f"未配置路径: {name}")
    return Path(cfg.get("_project_root", project_root())) / relative
