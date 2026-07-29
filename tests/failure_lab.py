"""Helpers for creating isolated repositories with controlled failures."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def write_contract(root: Path, constraints: dict[str, Any], **extra: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "constraints": constraints, **extra}
    name = "constraintloop" + ".yml"
    path = root / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def failing_command(exit_code: int = 23, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "command",
        "command": [sys.executable, "-c", f"raise SystemExit({exit_code})"],
        "phases": ["stop", "ci"],
        **extra,
    }
