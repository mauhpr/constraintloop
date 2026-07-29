from __future__ import annotations

import sys
from pathlib import Path

from constraintloop.models import MetricConstraint
from constraintloop.runners import run_metric_constraint


def test_metric_file_may_not_escape_working_directory(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "outside.json").write_text('{"score": 100}', encoding="utf-8")
    spec = MetricConstraint.model_validate(
        {
            "kind": "metric",
            "command": [sys.executable, "-c", "print('ok')"],
            "cwd": "work",
            "parser": {
                "type": "json",
                "source": "file",
                "file": "../outside.json",
                "path": "score",
            },
            "threshold": {"operator": "gte", "value": 1},
        }
    )
    result = run_metric_constraint(tmp_path, "metric", spec, "digest", 4096)
    assert result.verdict.value == "error"
    assert "escapes" in result.message
