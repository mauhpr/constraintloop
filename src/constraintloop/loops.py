"""Bounded, journaled convergence-loop transitions."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from constraintloop.config import contract_digest
from constraintloop.digest import constraint_input_digest
from constraintloop.engine import ConstraintEngine, blocking_results
from constraintloop.models import (
    Contract,
    CycleResult,
    EvidenceRecord,
    LoopConfig,
    LoopJournal,
    LoopState,
    Phase,
    Verdict,
)
from constraintloop.state import _read_json, _write_json, _write_lock, cache_root

CYCLE_EXIT_CODES = {
    LoopState.PASSED: 0,
    LoopState.REPAIR: 10,
    LoopState.WAITING: 11,
    LoopState.HUMAN_REQUIRED: 12,
    LoopState.BUDGET_EXHAUSTED: 13,
    LoopState.ERROR: 14,
}


class LoopError(RuntimeError):
    pass


def _safe_name(name: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in name)
    if not safe or safe != name:
        raise LoopError(f"Invalid loop name {name!r}")
    return safe


def loop_root(project_root: Path) -> Path:
    return cache_root(project_root) / "loops"


def journal_path(project_root: Path, loop_name: str) -> Path:
    return loop_root(project_root) / f"{_safe_name(loop_name)}.json"


def lease_path(project_root: Path, loop_name: str) -> Path:
    return loop_root(project_root) / f"{_safe_name(loop_name)}.lease.json"


def evidence_snapshot(record: EvidenceRecord) -> str:
    payload = [
        {
            "constraint_id": item.constraint_id,
            "input_digest": item.input_digest,
            "verdict": item.verdict.value,
            "findings": [
                finding.model_dump(mode="json", exclude_none=True) for finding in item.findings
            ],
        }
        for item in record.results
    ]
    raw = json.dumps(
        {"contract_digest": record.contract_digest, "results": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _input_snapshot(project_root: Path, contract: Contract, config: LoopConfig) -> str:
    identity = contract_digest(contract)
    inputs = [
        (
            constraint_id,
            constraint_input_digest(
                project_root,
                constraint_id,
                spec,
                contract_digest=identity,
            ),
        )
        for constraint_id, spec in contract.constraints.items()
        if spec.enabled and config.phase in spec.phases
    ]
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def run_cycle(
    project_root: Path,
    contract: Contract,
    loop_name: str,
    *,
    record: EvidenceRecord | None = None,
    now: float | None = None,
    goal: str | None = None,
    agent_adapter: str | None = None,
) -> CycleResult:
    """Execute exactly one bounded transition and persist it atomically."""
    if loop_name not in contract.loops:
        raise LoopError(f"Unknown loop {loop_name!r}")
    config = contract.loops[loop_name]
    current_time = time.time() if now is None else now
    identity = contract_digest(contract)
    path = journal_path(project_root, loop_name)
    with _write_lock(path):
        raw = _read_json(path, {})
        try:
            journal = LoopJournal.model_validate(raw)
        except Exception as exc:
            if raw:
                raise LoopError(f"Loop journal is corrupt: {path}") from exc
            journal = LoopJournal(
                loop=loop_name,
                contract_digest=identity,
                started_at=current_time,
                updated_at=current_time,
            )
        if journal.contract_digest != identity:
            journal = LoopJournal(
                loop=loop_name,
                contract_digest=identity,
                started_at=current_time,
                updated_at=current_time,
            )

        input_snapshot = _input_snapshot(project_root, contract, config)
        last_input = journal.input_snapshot
        if (
            record is None
            and journal.prior_state == LoopState.WAITING
            and last_input == input_snapshot
            and current_time - journal.updated_at < config.interval_seconds
            and journal.last_result is not None
        ):
            previous = CycleResult.model_validate(journal.last_result)
            result = previous.model_copy(
                update={
                    "observation": journal.observation + 1,
                    "wake_after_seconds": max(
                        0.0, config.interval_seconds - (current_time - journal.updated_at)
                    ),
                }
            )
            journal.observation = result.observation
            journal.last_result = result.model_dump(mode="json")
            journal.input_snapshot = input_snapshot
            _write_json(path, journal.model_dump(mode="json"))
            return result

        if record is None:
            record = ConstraintEngine(
                project_root,
                contract,
                use_cache=config.phase != Phase.CI,
                allow_waivers=config.phase != Phase.CI,
                goal=goal,
                agent_adapter=agent_adapter,
                refresh_pending=True,
            ).run(config.phase)

        snapshot = evidence_snapshot(record)
        observation = journal.observation + 1
        repair_attempt = journal.repair_attempt
        unchanged_repairs = journal.unchanged_repairs
        if journal.prior_state == LoopState.REPAIR:
            repair_attempt += 1
            unchanged_repairs = unchanged_repairs + 1 if journal.prior_snapshot == snapshot else 0

        required = blocking_results(record)
        pending = [item for item in required if item.verdict == Verdict.PENDING]
        unreliable = [
            item for item in required if item.verdict in {Verdict.ERROR, Verdict.UNCERTAIN}
        ]
        blocking_ids = [item.constraint_id for item in required]
        elapsed = current_time - journal.started_at

        if not required:
            state = LoopState.PASSED
            action = "Fresh required evidence passes. Stop."
            wake = 0.0
        elif unreliable:
            state = LoopState.ERROR
            action = "Constraint evaluation is unreliable. Inspect evidence and require a human."
            wake = 0.0
        elif elapsed >= config.max_duration_seconds:
            state = LoopState.BUDGET_EXHAUSTED
            action = "The loop duration budget is exhausted. Require a human decision."
            wake = 0.0
        elif pending:
            state = LoopState.WAITING
            action = "Evidence is pending. Make no edits and run one cycle after the wake interval."
            wake = config.interval_seconds
        elif repair_attempt >= config.max_repair_attempts:
            state = LoopState.BUDGET_EXHAUSTED
            action = "The repair-attempt budget is exhausted. Require a human decision."
            wake = 0.0
        elif unchanged_repairs >= config.max_unchanged_repairs:
            state = LoopState.HUMAN_REQUIRED
            action = "Repairs left evidence unchanged. Require a human decision."
            wake = 0.0
        else:
            state = LoopState.REPAIR
            action = "Repair only the listed blocking constraints, then run exactly one new cycle."
            wake = 0.0

        result = CycleResult(
            loop=loop_name,
            state=state,
            snapshot=snapshot,
            observation=observation,
            repair_attempt=repair_attempt,
            next_action=action,
            wake_after_seconds=wake,
            blocking_constraints=blocking_ids,
        )
        journal.updated_at = current_time
        journal.observation = observation
        journal.repair_attempt = repair_attempt
        journal.unchanged_repairs = unchanged_repairs
        journal.prior_state = state
        journal.prior_snapshot = snapshot
        journal.input_snapshot = input_snapshot
        journal.last_result = result.model_dump(mode="json")
        _write_json(path, journal.model_dump(mode="json"))
        return result


@contextmanager
def loop_lease(
    project_root: Path,
    loop_name: str,
    *,
    ttl_seconds: float,
    now: float | None = None,
) -> Iterator[Callable[[], None]]:
    """Acquire a recoverable single-writer supervisor lease."""
    current_time = time.time() if now is None else now
    path = lease_path(project_root, loop_name)
    token = str(uuid.uuid4())
    with _write_lock(path):
        existing = _read_json(path, {})
        if isinstance(existing, dict) and float(existing.get("expires_at", 0)) > current_time:
            raise LoopError(f"Loop {loop_name!r} already has an active supervisor lease")
        _write_json(
            path,
            {
                "schema_version": 1,
                "loop": loop_name,
                "pid": os.getpid(),
                "token": token,
                "expires_at": current_time + ttl_seconds,
            },
        )

    def renew() -> None:
        renewed_at = time.time()
        with _write_lock(path):
            existing = _read_json(path, {})
            if not isinstance(existing, dict) or existing.get("token") != token:
                raise LoopError(f"Loop {loop_name!r} supervisor lease was lost")
            existing["expires_at"] = renewed_at + ttl_seconds
            _write_json(path, existing)

    try:
        yield renew
    finally:
        with _write_lock(path):
            existing = _read_json(path, {})
            if isinstance(existing, dict) and existing.get("token") == token:
                path.unlink(missing_ok=True)


def supervise(
    project_root: Path,
    contract: Contract,
    loop_name: str,
) -> Iterator[CycleResult]:
    """Yield state changes while waiting; return on every non-waiting state."""
    if loop_name not in contract.loops:
        raise LoopError(f"Unknown loop {loop_name!r}")
    config = contract.loops[loop_name]
    ttl = max(60.0, config.interval_seconds * 3)
    cancelled = False

    def cancel(_signum: int, _frame: Any) -> None:
        nonlocal cancelled
        cancelled = True

    previous_handlers = {
        signum: signal.signal(signum, cancel) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with loop_lease(project_root, loop_name, ttl_seconds=ttl) as renew:
            previous_state: LoopState | None = None
            while not cancelled:
                renew()
                result = run_cycle(project_root, contract, loop_name)
                if result.state != previous_state:
                    yield result
                    previous_state = result.state
                if result.state != LoopState.WAITING:
                    return
                wake_at = time.monotonic() + result.wake_after_seconds
                while not cancelled and time.monotonic() < wake_at:
                    time.sleep(min(1.0, wake_at - time.monotonic()))
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def loop_prompt(loop_name: str, adapter: str) -> str:
    if adapter not in {"claude", "codex"}:
        raise LoopError(f"Unsupported loop adapter {adapter!r}")
    return (
        f"Run `constraintloop cycle {loop_name} --json` exactly once. Follow only its "
        "`next_action`. Make at most one repair when state is `repair`; make no edits when "
        "state is `waiting`. Stop on `passed`, `human_required`, `budget_exhausted`, or "
        "`error`. Never edit the ConstraintLoop configuration or create a waiver. Repeat "
        "only after the requested repair or wake interval."
    )
