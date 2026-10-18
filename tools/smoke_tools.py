# -*- coding: utf-8 -*-
"""阶段一工具层冒烟检查：只读取已有结果，不调用模型 API。"""
from __future__ import annotations

from .detection_tool import load_detections
from .fusion_tool import inspect_fusion
from .rag_tool import load_rag_meta
from .report_tool import load_warning_results
from .vlm_tool import load_scene_results
from .common import validate_schema


def main() -> int:
    checks = {
        "fusion": inspect_fusion(),
        "detection": load_detections(),
        "vlm": load_scene_results(),
        "rag": load_rag_meta(),
        "report": load_warning_results(),
    }
    failed = []
    for name, result in checks.items():
        contract_ok, contract_error = validate_schema(result.to_dict(), "tool_result")
        status = result.status if contract_ok else "invalid_contract"
        error = result.error or contract_error
        print(f"[{name}] {status}" + (f" - {error}" if error else ""))
        if result.status != "ok" or not contract_ok:
            failed.append(name)
    if failed:
        print("失败工具: " + ", ".join(failed))
        return 1
    print("阶段一工具层冒烟检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
