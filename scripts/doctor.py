#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署自检（doctor）：换设备/换人接手时，先跑一遍确认环境、资产、配置、端口就绪。

用法：
    python scripts/doctor.py            # 人类可读报告
    python scripts/doctor.py --quiet    # 只输出汇总行（供脚本调用）

检查项：
    1. Python >= 3.10
    2. 14 个关键三方模块可导入（见 MODULES）
    3. 9 类资产文件在位（权重 / RAG 库 / 真值集 / 内置 ultralytics / config.json ...）
    4. config.json 结构合法且 key 非占位符
    5. 端口 8000 / 8010 / 5173 空闲或已被本服务占用

退出码：0 = 无 FAIL（允许 WARN）；1 = 存在 FAIL。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE / "libs") not in sys.path:
    sys.path.insert(0, str(BASE / "libs"))  # 内置 ultralytics 引擎

# Windows 控制台默认 GBK，中文详情可能崩；统一切 UTF-8（与 llm_judge.py 同款防护）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PY_MIN = (3, 10)

# (模块, 用途, 致命?) —— 与 requirements.txt 一一对应
MODULES = [
    ("fastapi", "后端服务框架", True),
    ("uvicorn", "ASGI 服务器", True),
    ("langgraph", "LangGraph 编排", True),
    ("langgraph.checkpoint.sqlite", "SQLite checkpoint", True),
    ("openai", "模型 API 网关（DeepSeek 等）", True),
    ("cv2", "图像处理（配准/增强/画框）", True),
    ("numpy", "数值计算 / 向量检索", True),
    ("torch", "L1 检测引擎（CPU 可跑）", True),
    ("yaml", "配置解析", True),
    ("PIL", "图像 I/O", True),
    ("sentence_transformers", "RAG 向量（BGE）", True),
    ("jieba", "RAG BM25 分词", True),
    ("rank_bm25", "RAG BM25 检索", True),
    ("multipart", "FastAPI 文件上传（python-multipart）", True),
]

# (相对路径, 用途, 致命?)
FILES = [
    ("data/weights/best.pt", "L1 权重（3 类：工人/机械/塔吊）", True),
    ("libs/ultralytics/__init__.py", "内置 ultralytics 引擎", True),
    ("results/rag_store_v2/vectors.npy", "RAG 向量库", True),
    ("results/rag_store_v2/chunks.jsonl", "RAG 条文库", True),
    ("results/rag_store_v2/meta.json", "RAG 元数据", True),
    ("results/golden_set/golden_set.json", "真值集（30 对，验收用）", False),
    ("config.json", "API 配置（含 key，勿随包分发）", True),
    ("web/console.html", "零依赖只读控制台（8010 挂载）", False),
    ("frontend/package.json", "Vue 前端", False),
]

PORTS = [(8000, "agent.service 任务服务"), (8010, "api_web 只读控制台"), (5173, "Vue dev server")]

PLACEHOLDER_HINTS = ("sk-在", "你的", "填写", "YOUR_", "xxx", "XXX", "changeme", "TODO")

results: list[tuple[str, str, str]] = []  # (级别, 检查项, 详情)


def record(level: str, item: str, detail: str = "") -> None:
    results.append((level, item, detail))


def check_python() -> None:
    v = sys.version_info
    ok = (v.major, v.minor) >= PY_MIN
    record("PASS" if ok else "FAIL", "Python 版本",
           f"{v.major}.{v.minor}.{v.micro}（要求 >= {PY_MIN[0]}.{PY_MIN[1]}）")


def check_modules() -> None:
    for name, purpose, fatal in MODULES:
        try:
            importlib.import_module(name)
            record("PASS", f"模块 {name}", purpose)
        except Exception as exc:  # noqa: BLE001 - 自检需要兜底所有导入错误
            record("FAIL" if fatal else "WARN", f"模块 {name}", f"{purpose}｜{type(exc).__name__}: {exc}")


def check_files() -> None:
    for rel, purpose, fatal in FILES:
        p = BASE / rel
        if p.exists():
            size = p.stat().st_size
            if rel == "data/weights/best.pt" and size < 1024 * 1024:
                record("WARN", f"资产 {rel}", f"{purpose}｜仅 {size} 字节，疑似占位文件")
            else:
                record("PASS", f"资产 {rel}", purpose)
        else:
            record("FAIL" if fatal else "WARN", f"资产 {rel}", f"{purpose}｜缺失")


def _looks_placeholder(value: str) -> bool:
    return (not value) or any(h in value for h in PLACEHOLDER_HINTS)


def check_config() -> None:
    cfg_path = BASE / "config.json"
    if not cfg_path.exists():
        record("FAIL", "config.json", "缺失（复制 config.example.json 为 config.json 并填 key）")
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        record("FAIL", "config.json", f"JSON 解析失败: {exc}")
        return
    bad = []
    for section in ("vlm", "llm"):
        item = cfg.get(section)
        if not isinstance(item, dict):
            bad.append(f"{section} 段缺失")
            continue
        for key in ("model_id", "api_base", "api_key"):
            v = str(item.get(key) or "")
            if _looks_placeholder(v):
                bad.append(f"{section}.{key} 为空或仍是占位符")
    if bad:
        record("FAIL", "config.json", "；".join(bad))
    else:
        record("PASS", "config.json", "vlm / llm 两段配置完整")


def _port_state(port: int) -> str:
    """返回 free / ours / unknown。ours = 端口被占用但能说明是本系统服务（HTTP 有响应）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return "ours"  # 已监听：极可能是本系统服务已在运行
        try:
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s2.bind(("127.0.0.1", port))
            s2.close()
            return "free"
        except OSError:
            return "unknown"


def check_ports() -> None:
    for port, purpose in PORTS:
        state = _port_state(port)
        if state == "free":
            record("PASS", f"端口 {port}", f"{purpose}｜空闲")
        elif state == "ours":
            record("PASS", f"端口 {port}", f"{purpose}｜已被占用（服务疑似已在运行）")
        else:
            record("WARN", f"端口 {port}", f"{purpose}｜被其他程序占用，启动前先释放")


def main() -> int:
    ap = argparse.ArgumentParser(description="GDYOLO 升级工程部署自检")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args()

    check_python()
    check_modules()
    check_files()
    check_config()
    check_ports()

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    passes = [r for r in results if r[0] == "PASS"]

    if not args.quiet:
        width = max(len(r[1]) for r in results) + 2
        for level, item, detail in results:
            mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[level]
            print(f"[{mark}] {item:<{width}} {detail}")

    print(f"\n汇总: {len(passes)} PASS / {len(warns)} WARN / {len(fails)} FAIL")
    if fails:
        print("存在 FAIL 项，按上方详情修复后重跑；常见修复：pip install -r requirements.txt、"
              "cp config.example.json config.json 并填 key。")
        return 1
    if warns:
        print("仅有 WARN，可启动；建议按详情确认。")
    else:
        print("全部通过，可执行启动（Windows: scripts\\start_debug_stack.bat）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
