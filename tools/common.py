# -*- coding: utf-8 -*-
"""工具层公共类型和路径解析。"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass
class ToolResult:
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    confidence: float | None = None

    def add_trace(self, tool: str, **details: Any) -> None:
        self.trace.append({"tool": tool, "ts": time.time(), **details})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)


def validate_schema(value: dict[str, Any], schema_name: str,
                    schema_dir: str | Path | None = None) -> tuple[bool, str]:
    """Validate a tool/task payload against its checked-in JSON Schema."""
    root = Path(schema_dir) if schema_dir else project_root() / "schemas"
    schema_path = root / f"{schema_name}.schema.json"
    if not schema_path.is_file():
        return False, f"Schema 不存在: {schema_path}"
    try:
        import jsonschema
        schema = load_json(schema_path)
        jsonschema.Draft202012Validator(schema).validate(value)
    except ImportError:
        return False, "Schema 校验需要安装 jsonschema"
    except Exception as exc:
        return False, str(exc)
    return True, ""
