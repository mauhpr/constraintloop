from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from constraintloop.models import ArtifactConstraint, CommandConstraint, MetricConstraint
from constraintloop.runners import (
    run_artifact_constraint,
    run_command_constraint,
    run_metric_constraint,
)


def test_command_runner_success_failure_errors_and_truncation(tmp_path: Path) -> None:
    passing = CommandConstraint(kind="command", command=[sys.executable, "-c", "print('ok')"])
    result = run_command_constraint(tmp_path, "pass", passing, "digest", 1024)
    assert result.verdict.value == "pass"
    assert result.output_tail == "ok"

    failing = passing.model_copy(
        update={"command": [sys.executable, "-c", "import sys; print('x'*2000); sys.exit(7)"]}
    )
    result = run_command_constraint(tmp_path, "fail", failing, "digest", 1024)
    assert result.verdict.value == "fail"
    assert result.exit_code == 7
    assert result.output_tail is not None and result.output_tail.startswith("[output truncated]")

    missing = passing.model_copy(update={"cwd": "missing"})
    assert run_command_constraint(tmp_path, "missing", missing, "d", 1024).verdict.value == "error"
    escaping = passing.model_copy(update={"cwd": ".."})
    assert "escapes" in run_command_constraint(tmp_path, "escape", escaping, "d", 1024).message
    unknown = passing.model_copy(update={"command": ["definitely-not-a-real-command"]})
    assert (
        "could not start" in run_command_constraint(tmp_path, "unknown", unknown, "d", 1024).message
    )
    timeout = passing.model_copy(
        update={
            "command": [sys.executable, "-c", "import time; time.sleep(1)"],
            "timeout_seconds": 0.01,
        }
    )
    assert "timed out" in run_command_constraint(tmp_path, "timeout", timeout, "d", 1024).message

    pending = passing.model_copy(update={"command": [sys.executable, "-c", "raise SystemExit(75)"]})
    result = run_command_constraint(tmp_path, "pending", pending, "digest", 1024)
    assert result.verdict.value == "pending"
    assert result.exit_code == 75


@pytest.mark.parametrize(
    ("operator", "threshold"),
    [("gt", 4), ("gte", 5), ("lt", 6), ("lte", 5), ("eq", 5)],
)
def test_metric_runner_operators(tmp_path: Path, operator: str, threshold: int) -> None:
    spec = MetricConstraint.model_validate(
        {
            "kind": "metric",
            "command": [sys.executable, "-c", "print('{\"value\": 5}')"],
            "parser": {"type": "json", "path": "value"},
            "threshold": {"operator": operator, "value": threshold},
        }
    )
    assert run_metric_constraint(tmp_path, "metric", spec, "d", 1024).verdict.value == "pass"


def test_metric_runner_sources_and_parse_failures(tmp_path: Path) -> None:
    regex = MetricConstraint.model_validate(
        {
            "kind": "metric",
            "command": [sys.executable, "-c", "import sys; print('score=3.5', file=sys.stderr)"],
            "parser": {"type": "regex", "source": "stderr", "pattern": r"score=(\d+\.\d+)"},
            "threshold": {"operator": "gte", "value": 3},
        }
    )
    assert run_metric_constraint(tmp_path, "regex", regex, "d", 1024).value == 3.5

    (tmp_path / "metric.json").write_text(json.dumps({"totals": [1, 8]}), encoding="utf-8")
    from_file = MetricConstraint.model_validate(
        {
            "kind": "metric",
            "command": [sys.executable, "-c", "pass"],
            "parser": {
                "type": "json",
                "source": "file",
                "file": "metric.json",
                "path": "totals.1",
            },
            "threshold": {"operator": "gt", "value": 9},
        }
    )
    assert run_metric_constraint(tmp_path, "file", from_file, "d", 1024).verdict.value == "fail"
    bad = from_file.model_copy(
        update={"parser": from_file.parser.model_copy(update={"path": "totals.missing"})}
    )
    assert run_metric_constraint(tmp_path, "bad", bad, "d", 1024).verdict.value == "error"
    command_fail = from_file.model_copy(
        update={"command": [sys.executable, "-c", "raise SystemExit(2)"]}
    )
    assert run_metric_constraint(tmp_path, "cmd", command_fail, "d", 1024).exit_code == 2
    pending = from_file.model_copy(
        update={"command": [sys.executable, "-c", "raise SystemExit(75)"]}
    )
    assert run_metric_constraint(tmp_path, "pending", pending, "d", 1024).verdict.value == "pending"


@pytest.mark.parametrize("artifact_format", ["any", "json", "junit"])
def test_artifact_runner_valid_formats(tmp_path: Path, artifact_format: str) -> None:
    path = tmp_path / "artifact"
    content = {"any": "data", "json": "{}", "junit": "<testsuite/>"}[artifact_format]
    path.write_text(content, encoding="utf-8")
    spec = ArtifactConstraint(kind="artifact", path="artifact", format=artifact_format)
    assert run_artifact_constraint(tmp_path, "artifact", spec, "d").verdict.value == "pass"


def test_artifact_runner_rejects_missing_empty_invalid_and_escape(tmp_path: Path) -> None:
    def run(path: str, **updates: object) -> str:
        spec = ArtifactConstraint.model_validate({"kind": "artifact", "path": path, **updates})
        return run_artifact_constraint(tmp_path, "artifact", spec, "d").verdict.value

    assert run("missing") == "fail"
    (tmp_path / "empty").touch()
    assert run("empty") == "fail"
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert run("bad.json", format="json") == "fail"
    assert run("../outside") == "error"
