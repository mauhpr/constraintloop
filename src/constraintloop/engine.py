"""Constraint execution, evidence freshness, quorum, and policy decisions."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from constraintloop.config import contract_digest
from constraintloop.digest import (
    changed_files,
    constraint_input_digest,
    git_diff,
    is_disclosable_path,
    matching_files,
    redact_text,
)
from constraintloop.environment import load_project_environment
from constraintloop.evaluators import EvaluatorError, build_evaluator
from constraintloop.models import (
    ArtifactConstraint,
    CommandConstraint,
    ConstraintResult,
    Contract,
    EvaluationBundle,
    EvaluatorCallMetadata,
    EvidenceRecord,
    FailureCategory,
    MetricConstraint,
    Phase,
    RatchetConstraint,
    RubricConstraint,
    Verdict,
)
from constraintloop.runners import (
    run_artifact_constraint,
    run_command_constraint,
    run_metric_constraint,
    run_ratchet_constraint,
)
from constraintloop.state import load_cached_result, save_cached_result, waiver_reason


class ConstraintEngine:
    """Run a strict contract against one project snapshot."""

    def __init__(
        self,
        project_root: Path,
        contract: Contract,
        *,
        use_cache: bool = True,
        allow_waivers: bool = True,
        goal: str | None = None,
        agent_adapter: str | None = None,
        refresh_pending: bool = False,
        progress: Callable[[str], None] | None = None,
    ):
        self.project_root = project_root.resolve()
        self.contract = contract
        self.use_cache = use_cache
        self.allow_waivers = allow_waivers
        self.goal = goal
        self.agent_adapter = agent_adapter
        self.refresh_pending = refresh_pending
        self.progress = progress
        self._progress_lock = threading.Lock()
        self.contract_digest = contract_digest(contract)

    def run(self, phase: Phase) -> EvidenceRecord:
        """Run applicable constraints in dependency order."""
        started_at = time.time()
        results: dict[str, ConstraintResult] = {}
        pending = {
            constraint_id
            for constraint_id, spec in self.contract.constraints.items()
            if spec.enabled and phase in spec.phases
        }

        while pending:
            ready = sorted(
                constraint_id
                for constraint_id in pending
                if all(
                    dependency not in pending
                    for dependency in self.contract.constraints[constraint_id].needs
                )
            )
            if not ready:  # Contract validation should make this unreachable.
                raise RuntimeError("No runnable constraints remain")
            runnable: list[
                tuple[
                    str,
                    CommandConstraint
                    | MetricConstraint
                    | RatchetConstraint
                    | ArtifactConstraint
                    | RubricConstraint,
                    str,
                ]
            ] = []
            for constraint_id in ready:
                spec = self.contract.constraints[constraint_id]
                pending_dependencies = [
                    dependency
                    for dependency in spec.needs
                    if dependency in results and results[dependency].verdict == Verdict.PENDING
                ]
                unavailable = [
                    dependency
                    for dependency in spec.needs
                    if dependency not in results
                    or results[dependency].verdict
                    not in {
                        Verdict.PASS,
                        Verdict.WAIVED,
                    }
                ]
                digest = constraint_input_digest(
                    self.project_root,
                    constraint_id,
                    spec,
                    contract_digest=self.contract_digest,
                )
                if pending_dependencies:
                    result = ConstraintResult(
                        constraint_id=constraint_id,
                        kind=spec.kind,
                        verdict=Verdict.PENDING,
                        enforcement=spec.enforcement,
                        input_digest=digest,
                        message=f"Dependencies are pending: {', '.join(pending_dependencies)}",
                    )
                elif unavailable:
                    dependency_categories = {
                        results[dependency].failure_category
                        for dependency in unavailable
                        if dependency in results
                    }
                    result = ConstraintResult(
                        constraint_id=constraint_id,
                        kind=spec.kind,
                        verdict=Verdict.ERROR,
                        enforcement=spec.enforcement,
                        input_digest=digest,
                        message=f"Dependencies did not pass: {', '.join(unavailable)}",
                        failure_category=(
                            FailureCategory.ENVIRONMENT
                            if FailureCategory.ENVIRONMENT in dependency_categories
                            else FailureCategory.CONSTRAINT
                        ),
                    )
                else:
                    runnable.append((constraint_id, spec, digest))
                    continue
                results[constraint_id] = result

            deterministic = [item for item in runnable if not isinstance(item[1], RubricConstraint)]
            with ThreadPoolExecutor(
                max_workers=min(self.contract.settings.concurrency, max(1, len(deterministic)))
            ) as executor:
                futures = {
                    constraint_id: executor.submit(
                        self._run_one,
                        constraint_id,
                        spec,
                        digest,
                        phase,
                        [results[key] for key in self.contract.constraints if key in results],
                    )
                    for constraint_id, spec, digest in deterministic
                }
                for constraint_id, _, _ in deterministic:
                    results[constraint_id] = futures[constraint_id].result()

            for constraint_id, spec, digest in runnable:
                if isinstance(spec, RubricConstraint):
                    results[constraint_id] = self._run_one(
                        constraint_id,
                        spec,
                        digest,
                        phase,
                        [results[key] for key in self.contract.constraints if key in results],
                    )
            pending.difference_update(ready)

        record = EvidenceRecord(
            run_id=str(uuid.uuid4()),
            project_root=str(self.project_root),
            contract_digest=self.contract_digest,
            phase=phase,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            results=[results[key] for key in self.contract.constraints if key in results],
        )
        return record

    def _run_one(
        self,
        constraint_id: str,
        spec: CommandConstraint
        | MetricConstraint
        | RatchetConstraint
        | ArtifactConstraint
        | RubricConstraint,
        digest: str,
        phase: Phase,
        prior_results: list[ConstraintResult],
    ) -> ConstraintResult:
        bundle = (
            self._build_bundle(constraint_id, spec, prior_results)
            if isinstance(spec, RubricConstraint)
            else None
        )
        cache_digest = _rubric_cache_digest(digest, bundle) if bundle is not None else digest
        if self.use_cache:
            cached = load_cached_result(self.project_root, constraint_id, cache_digest)
            if (
                cached is not None
                and cached.enforcement == spec.enforcement
                and not (self.refresh_pending and cached.verdict == Verdict.PENDING)
            ):
                if (
                    self.allow_waivers
                    and phase != Phase.CI
                    and not isinstance(spec, RubricConstraint)
                ):
                    reason = waiver_reason(self.project_root, cached, self.contract_digest)
                    if reason:
                        return cached.model_copy(
                            update={
                                "verdict": Verdict.WAIVED,
                                "message": f"Locally waived by a human: {reason}",
                            }
                        )
                self._emit(f"REUSED {constraint_id}: {cached.verdict.value}")
                return cached

        self._emit(f"RUN {constraint_id} ({spec.kind})")
        with self._heartbeat(constraint_id):
            if isinstance(spec, CommandConstraint):
                progress_kwargs = {"progress": self._emit} if self.progress is not None else {}
                result = run_command_constraint(
                    self.project_root,
                    constraint_id,
                    spec,
                    digest,
                    self.contract.settings.evidence_output_limit,
                    **progress_kwargs,
                )
            elif isinstance(spec, MetricConstraint):
                progress_kwargs = {"progress": self._emit} if self.progress is not None else {}
                result = run_metric_constraint(
                    self.project_root,
                    constraint_id,
                    spec,
                    digest,
                    self.contract.settings.evidence_output_limit,
                    **progress_kwargs,
                )
            elif isinstance(spec, RatchetConstraint):
                progress_kwargs = {"progress": self._emit} if self.progress is not None else {}
                result = run_ratchet_constraint(
                    self.project_root,
                    constraint_id,
                    spec,
                    digest,
                    self.contract.settings.evidence_output_limit,
                    **progress_kwargs,
                )
            elif isinstance(spec, ArtifactConstraint):
                result = run_artifact_constraint(self.project_root, constraint_id, spec, digest)
            else:
                assert bundle is not None
                result = self._run_rubric(constraint_id, spec, digest, bundle)
        self._emit(
            f"DONE {constraint_id}: {result.verdict.value} ({result.duration_ms / 1000:.1f}s)"
        )

        if self.use_cache:
            save_cached_result(self.project_root, result, cache_digest=cache_digest)
        return result

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            with self._progress_lock:
                self.progress(message)

    @contextmanager
    def _heartbeat(self, constraint_id: str) -> Iterator[None]:
        if self.progress is None:
            yield
            return
        stopped = threading.Event()
        started = time.monotonic()

        def report() -> None:
            interval = self.contract.settings.progress_interval_seconds
            while not stopped.wait(interval):
                self._emit(f"STILL RUNNING {constraint_id} ({time.monotonic() - started:.0f}s)")

        thread = threading.Thread(target=report, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    def _run_rubric(
        self,
        constraint_id: str,
        spec: RubricConstraint,
        digest: str,
        bundle: EvaluationBundle,
    ) -> ConstraintResult:
        started = time.monotonic()
        try:
            environment = load_project_environment(self.project_root)
        except ValueError as exc:
            return ConstraintResult(
                constraint_id=constraint_id,
                kind=spec.kind,
                verdict=Verdict.UNCERTAIN,
                enforcement=spec.enforcement,
                input_digest=digest,
                message=f"Could not load evaluator environment: {exc}",
                duration_ms=(time.monotonic() - started) * 1000,
                failure_category=FailureCategory.ENVIRONMENT,
            )
        if self.agent_adapter:
            environment["CONSTRAINTLOOP_CALLER_ADAPTER"] = self.agent_adapter
        evaluator = build_evaluator(
            self.contract.evaluators[spec.evaluator],
            cwd=self.project_root,
            environment=environment,
        )
        verdicts = []
        errors: list[str] = []
        evaluator_calls: list[EvaluatorCallMetadata] = []
        for _ in range(spec.runs):
            try:
                verdicts.append(evaluator.evaluate(bundle))
            except EvaluatorError as exc:
                errors.append(str(exc))
            metadata = getattr(evaluator, "last_metadata", None)
            if isinstance(metadata, EvaluatorCallMetadata):
                evaluator_calls.append(metadata)

        passes = sum(verdict.verdict == "pass" for verdict in verdicts)
        fails = sum(verdict.verdict == "fail" for verdict in verdicts)
        quorum = spec.pass_quorum or 1
        findings = [finding for verdict in verdicts for finding in verdict.findings]
        rationales = [verdict.rationale for verdict in verdicts]
        if passes >= quorum:
            verdict = Verdict.PASS
            message = f"Rubric passed quorum ({passes}/{spec.runs}; required {quorum})"
        elif errors or any(item.verdict == "uncertain" for item in verdicts):
            verdict = Verdict.UNCERTAIN
            message = f"Rubric did not reach a reliable quorum ({passes} pass, {fails} fail)"
        else:
            verdict = Verdict.FAIL
            message = f"Rubric failed quorum ({passes}/{spec.runs}; required {quorum})"
        details = rationales + errors
        result = ConstraintResult(
            constraint_id=constraint_id,
            kind=spec.kind,
            verdict=verdict,
            enforcement=spec.enforcement,
            input_digest=digest,
            message=message,
            duration_ms=(time.monotonic() - started) * 1000,
            output_tail="\n\n".join(details)[-self.contract.settings.evidence_output_limit :]
            or None,
            findings=findings,
            evaluator_calls=evaluator_calls,
        )
        if result.verdict not in {Verdict.PASS, Verdict.SKIPPED, Verdict.WAIVED}:
            result.failure_category = (
                FailureCategory.ENVIRONMENT if errors else FailureCategory.CONSTRAINT
            )
        return result

    def _build_bundle(
        self,
        constraint_id: str,
        spec: RubricConstraint,
        deterministic_results: list[ConstraintResult],
    ) -> EvaluationBundle:
        limit = self.contract.settings.evaluation_bundle_limit
        diff = git_diff(self.project_root, patterns=spec.include, limit=limit // 4)
        used = len(diff.encode())
        files: dict[str, str] = {}
        omitted: list[str] = []
        query = f"{constraint_id} {spec.rubric} {self.goal or ''}".lower()
        query_tokens = set(re.findall(r"[a-z0-9]{3,}", query))
        changed = set(changed_files(self.project_root))

        def priority(path: Path) -> tuple[int, int, int, int, str]:
            relative = path.relative_to(self.project_root).as_posix()
            path_tokens = set(re.findall(r"[a-z0-9]{3,}", relative.lower()))
            relevance = len(query_tokens & path_tokens)
            source = int(relative.startswith(("src/", "lib/", "app/")))
            try:
                modified = path.stat().st_mtime_ns
            except OSError:
                modified = 0
            return (-relevance, -int(relative in changed), -source, -modified, relative)

        matched = matching_files(self.project_root, spec.include)
        for path in matched:
            relative = path.relative_to(self.project_root).as_posix()
            if not is_disclosable_path(relative):
                omitted.append(relative)
        candidates = sorted(
            (
                path
                for path in matched
                if is_disclosable_path(path.relative_to(self.project_root).as_posix())
            ),
            key=priority,
        )
        for path in candidates:
            relative = path.relative_to(self.project_root).as_posix()
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                omitted.append(relative)
                continue
            cost = len(relative.encode()) + len(content.encode())
            if used + cost > limit:
                omitted.append(relative)
                continue
            files[relative] = redact_text(content)
            used += cost
        deterministic = [
            cast(dict[str, Any], _redact_value(result.model_dump(mode="json")))
            for result in deterministic_results
        ]
        bundle = EvaluationBundle(
            constraint_id=constraint_id,
            rubric=spec.rubric,
            goal=self.goal,
            diff=diff,
            deterministic_results=deterministic,
            files=files,
            omitted_files=omitted,
        )
        while len(bundle.model_dump_json().encode()) > limit and files:
            relative = next(reversed(files))
            files.pop(relative)
            omitted.append(relative)
            bundle = bundle.model_copy(
                update={"files": dict(files), "omitted_files": list(omitted)}
            )
        if len(bundle.model_dump_json().encode()) > limit and bundle.diff:
            overflow = len(bundle.model_dump_json().encode()) - limit
            keep = max(0, len(bundle.diff.encode()) - overflow - 32)
            bundle = bundle.model_copy(
                update={
                    "diff": bundle.diff.encode()[:keep].decode("utf-8", errors="ignore")
                    + "\n[diff truncated]"
                }
            )
        if len(bundle.model_dump_json().encode()) > limit:
            compact = []
            for item in deterministic:
                compact.append(
                    {
                        key: value
                        for key, value in item.items()
                        if key
                        in {
                            "constraint_id",
                            "kind",
                            "verdict",
                            "enforcement",
                            "message",
                            "cached",
                        }
                    }
                )
            bundle = bundle.model_copy(update={"deterministic_results": compact})
        return bundle


def blocking_results(record: EvidenceRecord) -> list[ConstraintResult]:
    return [result for result in record.results if result.blocks]


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def _rubric_cache_digest(base_digest: str, bundle: EvaluationBundle) -> str:
    payload = bundle.model_dump(mode="json")
    for result in payload["deterministic_results"]:
        for volatile in ("cached", "duration_ms", "evaluator_calls"):
            result.pop(volatile, None)
    digest = hashlib.sha256()
    digest.update(base_digest.encode())
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def format_summary(
    record: EvidenceRecord,
    *,
    include_output: bool = False,
    output_limit: int | None = None,
) -> str:
    has_advisories = record.passed and any(
        result.verdict
        not in {
            Verdict.PASS,
            Verdict.SKIPPED,
            Verdict.WAIVED,
        }
        for result in record.results
    )
    outcome = "PASS WITH ADVISORIES" if has_advisories else "PASS" if record.passed else "BLOCKED"
    lines = [f"ConstraintLoop {record.phase.value}: {outcome} ({len(record.results)} constraints)"]
    verdict_counts = {
        verdict: sum(result.verdict == verdict for result in record.results) for verdict in Verdict
    }
    lines.append(
        "Results: "
        + ", ".join(
            f"{verdict.value}={count}" for verdict, count in verdict_counts.items() if count
        )
    )
    categories = {
        category: sum(result.failure_category == category for result in record.results)
        for category in FailureCategory
    }
    visible_categories = [
        f"{category.value}={count}" for category, count in categories.items() if count
    ]
    if visible_categories:
        lines.append("Failure classes: " + ", ".join(visible_categories))
    icons = {
        Verdict.PASS: "PASS",
        Verdict.PENDING: "PENDING",
        Verdict.FAIL: "FAIL",
        Verdict.ERROR: "ERROR",
        Verdict.SKIPPED: "SKIP",
        Verdict.UNCERTAIN: "UNCERTAIN",
        Verdict.WAIVED: "WAIVED",
    }
    for result in record.results:
        cache = " [cached]" if result.cached else ""
        category = f" [{result.failure_category.value}]" if result.failure_category else ""
        lines.append(
            f"- {icons[result.verdict]} {result.constraint_id}{cache}{category}: {result.message}"
        )
        if result.details:
            details = ", ".join(
                f"{name}={_format_detail(value)}" for name, value in result.details.items()
            )
            lines.append(f"  evidence: {details}")
        if include_output and result.output_tail and result.verdict != Verdict.PASS:
            output = (
                _compact_output(result.output_tail)
                if output_limit is not None
                else result.output_tail
            )
            lines.append(output)
            if output_limit is not None:
                lines.append(f"  full output: constraintloop debug {result.constraint_id}")
    summary = "\n".join(lines)
    return _truncate_utf8(summary, output_limit) if output_limit is not None else summary


def _compact_output(output: str, *, max_lines: int = 12) -> str:
    """Extract high-signal failure lines from common test and command output."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    failures = [line for line in lines if line.lstrip().startswith(("FAILED ", "ERROR "))]
    traceback = next(
        (
            line
            for line in lines
            if re.match(r"^\s*E\s+\S", line) or re.match(r"^\S*(?:Error|Exception):\s", line)
        ),
        None,
    )
    selected = failures[: max_lines - 1]
    if traceback is not None and traceback not in selected:
        selected.append(traceback)
    if not selected:
        meaningful = [line for line in lines if not re.fullmatch(r"[.sSxXfFE%\d\s\[\]/=-]+", line)]
        selected = meaningful[-max_lines:]
    if not selected:
        selected = lines[-max_lines:]
    prefix = "[output summary]"
    if len(selected) < len(lines):
        prefix += f" ({len(lines) - len(selected)} lines omitted)"
    return prefix + ("\n" + "\n".join(selected) if selected else "")


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    marker = "\n[hook summary truncated; use constraintloop debug ID for full output]"
    keep = max(0, limit - len(marker.encode()))
    return encoded[:keep].decode("utf-8", errors="ignore") + marker


def _format_detail(value: object) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)
