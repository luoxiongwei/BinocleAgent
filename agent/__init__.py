# -*- coding: utf-8 -*-
"""施工安全单 Agent。"""

__all__ = ["SafetyAgent"]


def __getattr__(name: str):
    """兼容旧 import；生产 graph/service 导入 agent 包时不加载旧 runner。"""
    if name == "SafetyAgent":
        from .runner import SafetyAgent
        return SafetyAgent
    raise AttributeError(name)
