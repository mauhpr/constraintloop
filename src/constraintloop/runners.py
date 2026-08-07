"""Deterministic constraint runners."""

from __future__ import annotations

import json
import operator
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from constraintloop.models import (
    ArtifactConstraint,
    CommandConstraint,
    CommandRetryPolicy,
    ConstraintResult,
    Enforcement,
    MetricConstraint,
    Verdict,
)

_OPS: dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "eq": operator.eq,
}


def run_command_constraint(
    project_root: Path,
    constraint_id: str,
    spec: CommandConstraint,
    input_digest: str,
    output_limit: int,
    *,
    progress: Callable[[str], None] | None = None,
) -> ConstraintResult:
    started = time.monotonic()
    execution, attempts = _run_with_retries(
        project_root,
        constraint_id,
        spec.command,
        spec.shell,
        spec.cwd,
        spec.timeout_seconds,
        spec.retry,
        progress,
    )
    duration = (time.monotonic() - started) * 1000
    if isinstance(execution, str):
        return _error_result(
            constraint_id, spec.kind, spec.enforcement, input_digest, execution, duration
        )
    returncode, stdout, stderr = execution
    if returncode in spec.pending_codes:
        return ConstraintResult(
            constraint_id=constraint_id,
            kind=spec.kind,
            verdict=Verdict.PENDING,
            enforcement=spec.enforcement,
            input_digest=input_digest,
            message=f"Command is pending (exit code {returncode})",
            duration_ms=duration,
            exit_code=returncode,
            output_tail=_output_tail(stdout, stderr, output_limit) or None,
        )
    passed = returncode in spec.success_codes
    output = _output_tail(stdout, stderr, output_limit)
    attempt_suffix = f" after {attempts} attempts" if attempts > 1 else ""
    return ConstraintResult(
        constraint_id=constraint_id,
        kind=spec.kind,
        verdict=Verdict.PASS if passed else Verdict.FAIL,
        enforcement=spec.enforcement,
        input_digest=input_digest,
        message=(
            f"Command passed{attempt_suffix}"
            if passed
            else f"Command exited with code {returncode}{attempt_suffix}"
        ),
        duration_ms=duration,
        exit_code=returncode,
        output_tail=output or None,
    )


def run_metric_constraint(
    project_root: Path,
    constraint_id: str,
    spec: MetricConstraint,
    input_digest: str,
    output_limit: int,
    *,
    progress: Callable[[str], None] | None = None,
) -> ConstraintResult:
    started = time.monotonic()
    execution, _ = _run_with_retries(
        project_root,
        constraint_id,
        spec.command,
        spec.shell,
        spec.cwd,
        spec.timeout_seconds,
        spec.retry,
        progress,
    )
    duration = (time.monotonic() - started) * 1000
    if isinstance(execution, str):
        return _error_result(
            constraint_id, spec.kind, spec.enforcement, input_digest, execution, duration
        )
    returncode, stdout, stderr = execution
    output = _output_tail(stdout, stderr, output_limit)
    if returncode in spec.pending_codes:
        return ConstraintResult(
            constraint_id=constraint_id,
            kind=spec.kind,
            verdict=Verdict.PENDING,
            enforcement=spec.enforcement,
            input_digest=input_digest,
            message=f"Metric is pending (exit code {returncode})",
            duration_ms=duration,
            exit_code=returncode,
            output_tail=output or None,
        )
    if returncode not in spec.success_codes:
        return ConstraintResult(
            constraint_id=constraint_id,
            kind=spec.kind,
            verdict=Verdict.FAIL,
            enforcement=spec.enforcement,
            input_digest=input_digest,
            message=f"Metric command exited with code {returncode}",
            duration_ms=duration,
            exit_code=returncode,
            output_tail=output or None,
        )
    try:
        value = _parse_metric(project_root / spec.cwd, spec, stdout, stderr)
    except (ValueError, OSError, json.JSONDecodeError, re.error) as exc:
        return _error_result(
            constraint_id,
            spec.kind,
            spec.enforcement,
            input_digest,
            f"Could not parse metric: {exc}",
            duration,
            output,
        )
    passed = _OPS[spec.threshold.operator](value, spec.threshold.value)
    return ConstraintResult(
        constraint_id=constraint_id,
        kind=spec.kind,
        verdict=Verdict.PASS if passed else Verdict.FAIL,
        enforcement=spec.enforcement,
        input_digest=input_digest,
        message=(
            f"Metric {value:g} satisfies {spec.threshold.operator} {spec.threshold.value:g}"
            if passed
            else (
                f"Metric {value:g} does not satisfy "
                f"{spec.threshold.operator} {spec.threshold.value:g}"
            )
        ),
        duration_ms=duration,
        exit_code=returncode,
        value=value,
        output_tail=output or None,
    )


def run_artifact_constraint(
    project_root: Path,
    constraint_id: str,
    spec: ArtifactConstraint,
    input_digest: str,
) -> ConstraintResult:
    started = time.monotonic()
    path = (project_root / spec.path).resolve()
    try:
        path.relative_to(project_root.resolve())
    except ValueError:
        return _error_result(
            constraint_id,
            spec.kind,
            spec.enforcement,
            input_digest,
            "Artifact path escapes the project root",
            0,
        )
    if not path.is_file():
        verdict, message = Verdict.FAIL, f"Required artifact does not exist: {spec.path}"
    elif spec.non_empty and path.stat().st_size == 0:
        verdict, message = Verdict.FAIL, f"Required artifact is empty: {spec.path}"
    else:
        try:
            if spec.format == "json":
                json.loads(path.read_text(encoding="utf-8"))
            elif spec.format == "junit":
                import xml.etree.ElementTree as ET

                ET.parse(path)
            verdict, message = Verdict.PASS, f"Artifact is present: {spec.path}"
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            verdict, message = Verdict.FAIL, f"Artifact is invalid: {exc}"
    return ConstraintResult(
        constraint_id=constraint_id,
        kind=spec.kind,
        verdict=verdict,
        enforcement=spec.enforcement,
        input_digest=input_digest,
        message=message,
        duration_ms=(time.monotonic() - started) * 1000,
    )


def _run_command(
    project_root: Path,
    command: list[str] | str,
    shell: bool,
    cwd: str,
    timeout: float,
) -> tuple[int, str, str] | str:
    working_dir = (project_root / cwd).resolve()
    try:
        working_dir.relative_to(project_root.resolve())
    except ValueError:
        return f"Command cwd escapes the project root: {cwd}"
    if not working_dir.is_dir():
        return f"Command cwd does not exist: {cwd}"
    try:
        result = subprocess.run(
            command,
            shell=shell,
            cwd=working_dir,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout:g}s"
    except (FileNotFoundError, OSError) as exc:
        return f"Command could not start: {exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


def _run_with_retries(
    project_root: Path,
    constraint_id: str,
    command: list[str] | str,
    shell: bool,
    cwd: str,
    timeout: float,
    policy: CommandRetryPolicy | None,
    progress: Callable[[str], None] | None,
) -> tuple[tuple[int, str, str] | str, int]:
    max_attempts = policy.max_attempts if policy is not None else 1
    total_timeout = (
        policy.total_timeout_seconds
        if policy is not None and policy.total_timeout_seconds is not None
        else timeout
    )
    execution: tuple[int, str, str] | str = "Command did not run"
    started = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        remaining = total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            return f"Command timed out after {total_timeout:g}s across retry attempts", attempt
        execution = _run_command(project_root, command, shell, cwd, min(timeout, remaining))
        if policy is None or attempt == max_attempts or not _is_retryable(execution, policy):
            return execution, attempt
        if progress is not None:
            progress(f"RETRY {constraint_id}: attempt {attempt + 1}/{max_attempts}")
        if policy.delay_seconds:
            remaining = total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                return f"Command timed out after {total_timeout:g}s across retry attempts", attempt
            time.sleep(min(policy.delay_seconds, remaining))
    return execution, max_attempts


def _is_retryable(execution: tuple[int, str, str] | str, policy: CommandRetryPolicy) -> bool:
    if isinstance(execution, str):
        if execution.startswith("Command timed out"):
            return bool(policy.retry_timeouts)
        if execution.startswith("Command could not start"):
            return bool(policy.retry_start_errors)
        return False
    return execution[0] in policy.exit_codes


def _parse_metric(
    cwd: Path,
    spec: MetricConstraint,
    stdout: str,
    stderr: str,
) -> float:
    parser = spec.parser
    if parser.source == "stdout":
        raw = stdout
    elif parser.source == "stderr":
        raw = stderr
    else:
        assert parser.file is not None
        metric_path = (cwd / parser.file).resolve()
        try:
            metric_path.relative_to(cwd.resolve())
        except ValueError as exc:
            raise ValueError("metric file escapes the constraint cwd") from exc
        raw = metric_path.read_text(encoding="utf-8")

    if parser.type == "regex":
        assert parser.pattern is not None
        match = re.search(parser.pattern, raw, re.MULTILINE)
        if not match:
            raise ValueError("regex did not match")
        return float(match.group(parser.group))

    assert parser.path is not None
    value: object = json.loads(raw)
    for part in parser.path.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError(f"path stopped before {part!r}")
    return float(cast(Any, value))


def _output_tail(stdout: str, stderr: str, limit: int) -> str:
    combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    encoded = combined.encode()
    if len(encoded) <= limit:
        return combined
    return "[output truncated]\n" + encoded[-limit:].decode("utf-8", errors="replace")


def _error_result(
    constraint_id: str,
    kind: str,
    enforcement: Enforcement,
    input_digest: str,
    message: str,
    duration: float,
    output: str | None = None,
) -> ConstraintResult:
    return ConstraintResult(
        constraint_id=constraint_id,
        kind=kind,
        verdict=Verdict.ERROR,
        enforcement=enforcement,
        input_digest=input_digest,
        message=message,
        duration_ms=duration,
        output_tail=output or None,
    )
