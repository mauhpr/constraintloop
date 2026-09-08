"""Bounded, journaled convergence-loop transitions."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from constraintloop.challenges import (
    ChallengeError,
    accept_submission,
    advance_challenge,
    challenge_input_digest,
    challenge_request,
)
from constraintloop.checkout import checkout_context, ensure_checkout_unchanged
from constraintloop.config import contract_digest
from constraintloop.digest import constraint_input_digest
from constraintloop.engine import ConstraintEngine, blocking_causes
from constraintloop.models import (
    Contract,
    CycleResult,
    DiscoverySubmission,
    EvidenceRecord,
    LoopConfig,
    LoopJournal,
    LoopState,
    Phase,
    Verdict,
    VerificationSubmission,
)
from constraintloop.state import _read_json, _write_json, _write_lock, cache_root

CYCLE_EXIT_CODES = {
    LoopState.PASSED: 0,
    LoopState.REPAIR: 10,
    LoopState.WAITING: 11,
    LoopState.HUMAN_REQUIRED: 12,
    LoopState.BUDGET_EXHAUSTED: 13,
    LoopState.ERROR: 14,
    LoopState.CHALLENGE: 15,
    LoopState.VERIFY: 16,
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


def _input_snapshot(
    project_root: Path, contract: Contract, config: LoopConfig, goal: str | None = None
) -> str:
    identity = contract_digest(contract)
    checkout = checkout_context(project_root)
    inputs = [
        (
            constraint_id,
            constraint_input_digest(
                project_root,
                constraint_id,
                spec,
                contract_digest=identity,
                checkout=checkout,
            ),
        )
        for constraint_id, spec in contract.constraints.items()
        if spec.enabled and config.phase in spec.phases
    ]
    inputs.append(("goal", goal or ""))
    inputs.append(("checkout", checkout.snapshot()))
    if config.challenge is not None:
        inputs.extend(
            [
                ("challenge", challenge_input_digest(project_root, config.challenge)),
            ]
        )
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
    continuation: bool = False,
    record_input_snapshot: str | None = None,
    on_record: Callable[[EvidenceRecord], None] | None = None,
) -> CycleResult:
    """Execute exactly one bounded transition and persist it atomically."""
    if loop_name not in contract.loops:
        raise LoopError(f"Unknown loop {loop_name!r}")
    config = contract.loops[loop_name]
    current_time = time.time() if now is None else now
    identity = contract_digest(contract)
    checkout = checkout_context(project_root)
    path = journal_path(project_root, loop_name)
    with _write_lock(path):
        try:
            raw = json.loads(path.read_text()) if path.exists() else {}
            journal = LoopJournal.model_validate(raw)
        except Exception as exc:
            if path.exists():
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

        if goal is not None:
            journal.goal = goal
        input_snapshot = _input_snapshot(project_root, contract, config, journal.goal)
        if journal.prior_state == LoopState.PASSED and journal.input_snapshot != input_snapshot:
            journal = LoopJournal(
                loop=loop_name,
                contract_digest=identity,
                started_at=current_time,
                updated_at=current_time,
                goal=journal.goal,
            )
        last_input = journal.input_snapshot
        if (
            record is None
            and journal.prior_state == LoopState.WAITING
            and last_input == input_snapshot
            and current_time - journal.updated_at < config.interval_seconds
            and journal.last_result is not None
            and (on_record is None or journal.last_evidence is not None)
            and current_time - journal.started_at < config.max_duration_seconds
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
            ensure_checkout_unchanged(project_root, checkout)
            journal.input_snapshot = input_snapshot
            _write_json(path, journal.model_dump(mode="json"))
            if on_record is not None and journal.last_evidence is not None:
                on_record(journal.last_evidence)
            return result

        if record is None:
            record = ConstraintEngine(
                project_root,
                contract,
                use_cache=config.phase != Phase.CI,
                allow_waivers=config.phase.allows_local_waivers,
                goal=journal.goal,
                agent_adapter=agent_adapter,
                refresh_pending=True,
            ).run(config.phase)

        ensure_checkout_unchanged(project_root, checkout)
        if on_record is not None:
            on_record(record)

        snapshot = evidence_snapshot(record)
        if config.challenge is not None:
            payload = {
                "evidence": snapshot,
                "inputs": input_snapshot,
                "challenge": journal.challenge.model_dump(mode="json", exclude={"request_id"})
                if journal.challenge is not None
                else None,
            }
            snapshot = (
                "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            )
        observation = journal.observation + 1
        repair_attempt = journal.repair_attempt
        unchanged_repairs = journal.unchanged_repairs
        if journal.prior_state == LoopState.REPAIR:
            repair_attempt += 1
            unchanged_repairs = unchanged_repairs + 1 if journal.prior_snapshot == snapshot else 0

        required = blocking_causes(record)
        pending = [item for item in required if item.verdict == Verdict.PENDING]
        unreliable = [
            item for item in required if item.verdict in {Verdict.ERROR, Verdict.UNCERTAIN}
        ]
        blocking_ids = [item.constraint_id for item in required]
        elapsed = (time.time() if now is None else current_time) - journal.started_at
        blocking_challenges: list[str] = []

        if not required and config.challenge is not None:
            after_checks = _input_snapshot(project_root, contract, config, journal.goal)
            if after_checks != input_snapshot or (
                record_input_snapshot is not None and record_input_snapshot != input_snapshot
            ):
                state = LoopState.WAITING
                action = (
                    "Watched inputs changed during evaluation. "
                    "Run one fresh cycle after the wake interval."
                )
                wake = config.interval_seconds
            else:
                state, action, blocking_challenges = advance_challenge(
                    config.challenge, journal, record, input_snapshot
                )
                wake = 0.0
            if elapsed >= config.max_duration_seconds and not (
                journal.prior_state == LoopState.PASSED and state == LoopState.PASSED
            ):
                state = LoopState.BUDGET_EXHAUSTED
                action = "The loop duration budget is exhausted. Require a human decision."
                wake = 0.0
            elif state == LoopState.REPAIR and repair_attempt >= config.max_repair_attempts:
                state = LoopState.BUDGET_EXHAUSTED
                action = "The repair-attempt budget is exhausted. Require a human decision."
            elif state == LoopState.REPAIR and unchanged_repairs >= config.max_unchanged_repairs:
                state = LoopState.HUMAN_REQUIRED
                action = "Repairs left challenge evidence unchanged. Require a human decision."
        elif not required:
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

        if config.challenge is not None and state in {
            LoopState.CHALLENGE,
            LoopState.VERIFY,
            LoopState.REPAIR,
        }:
            if journal.challenge_continuations >= config.challenge.max_continuations:
                state = LoopState.BUDGET_EXHAUSTED
                action = "The session continuation budget is exhausted. Require a human decision."
            elif continuation:
                journal.challenge_continuations += 1

        result = CycleResult(
            loop=loop_name,
            state=state,
            snapshot=snapshot,
            observation=observation,
            repair_attempt=repair_attempt,
            next_action=action,
            wake_after_seconds=wake,
            blocking_constraints=blocking_ids,
            blocking_challenges=blocking_challenges,
        )
        journal.updated_at = current_time
        journal.observation = observation
        journal.repair_attempt = repair_attempt
        journal.unchanged_repairs = unchanged_repairs
        journal.prior_state = state
        journal.prior_snapshot = snapshot
        journal.input_snapshot = input_snapshot
        journal.last_result = result.model_dump(mode="json")
        journal.last_evidence = record
        ensure_checkout_unchanged(project_root, checkout)
        _write_json(path, journal.model_dump(mode="json"))
        return result


def _challenge_journal(project_root: Path, contract: Contract, loop_name: str) -> LoopJournal:
    if loop_name not in contract.loops or contract.loops[loop_name].challenge is None:
        raise LoopError(f"Loop {loop_name!r} has no session challenge gate")
    try:
        journal = LoopJournal.model_validate_json(journal_path(project_root, loop_name).read_text())
    except (OSError, ValueError) as exc:
        raise LoopError("No valid challenge journal. Run one cycle first.") from exc
    if journal.contract_digest != contract_digest(contract):
        raise LoopError("The contract changed. Run one cycle before submitting challenge work.")
    return journal


def loop_input_snapshot(
    project_root: Path, contract: Contract, loop_name: str, goal: str | None = None
) -> str:
    """Capture inputs before a caller evaluates the record supplied to a cycle."""
    if goal is None:
        raw = _read_json(journal_path(project_root, loop_name), {})
        if isinstance(raw, dict) and isinstance(raw.get("goal"), str):
            goal = raw["goal"]
    return _input_snapshot(project_root, contract, contract.loops[loop_name], goal)


def show_challenge(project_root: Path, contract: Contract, loop_name: str) -> dict[str, Any]:
    journal = _challenge_journal(project_root, contract, loop_name)
    config = contract.loops[loop_name].challenge
    assert config is not None
    try:
        return challenge_request(config, journal)
    except ChallengeError as exc:
        raise LoopError(str(exc)) from exc


def submit_challenge(
    project_root: Path,
    contract: Contract,
    loop_name: str,
    payload: dict[str, Any],
) -> None:
    path = journal_path(project_root, loop_name)
    with _write_lock(path):
        journal = _challenge_journal(project_root, contract, loop_name)
        config = contract.loops[loop_name]
        if time.time() - journal.started_at >= config.max_duration_seconds:
            raise LoopError(
                "The loop duration budget is exhausted; challenge work cannot be accepted."
            )
        try:
            submission = (
                DiscoverySubmission.model_validate(payload)
                if payload.get("kind") == "discovery"
                else VerificationSubmission.model_validate(payload)
            )
            accept_submission(
                project_root,
                contract,
                journal,
                submission,
                _input_snapshot(project_root, contract, config, journal.goal),
            )
        except ValueError as exc:
            raise LoopError(str(exc)) from exc
        _write_json(path, journal.model_dump(mode="json"))


@contextmanager
def loop_lease(
    project_root: Path,
    loop_name: str,
    *,
    ttl_seconds: float,
    now: float | None = None,
) -> Iterator[Callable[[], None]]:
    """Acquire a recoverable single-writer supervisor lease."""
    if ttl_seconds <= 0:
        raise LoopError("Supervisor lease TTL must be positive")
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

    failures: list[Exception] = []

    def renew() -> None:
        if failures:
            raise LoopError("Supervisor lease renewal failed") from failures[0]
        renewed_at = time.time()
        with _write_lock(path):
            existing = _read_json(path, {})
            if not isinstance(existing, dict) or existing.get("token") != token:
                raise LoopError(f"Loop {loop_name!r} supervisor lease was lost")
            existing["expires_at"] = renewed_at + ttl_seconds
            _write_json(path, existing)

    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(ttl_seconds / 3):
            try:
                renew()
            except (LoopError, OSError) as exc:
                failures.append(exc)
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield renew
        if failures:
            raise LoopError("Supervisor lease renewal failed") from failures[0]
    finally:
        stopped.set()
        thread.join()
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
    checkout = checkout_context(project_root)
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
                ensure_checkout_unchanged(project_root, checkout)
                renew()
                result = run_cycle(project_root, contract, loop_name)
                renew()
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
    if adapter not in {"claude", "codex", "gemini"}:
        raise LoopError(f"Unsupported loop adapter {adapter!r}")
    return (
        f"Run `constraintloop cycle {loop_name} --json` exactly once. Follow only its "
        "`next_action`. Make at most one repair when state is `repair`; make no edits when "
        "state is `waiting`. Stop on `passed`, `human_required`, `budget_exhausted`, or "
        "`error`. Never edit the ConstraintLoop configuration or create a waiver. Repeat "
        "only after the requested repair, challenge work, or wake interval. When state is "
        "`challenge` or `verify`, perform the requested discovery or verification in this "
        "same coding session using its existing context and tools. Use `constraintloop "
        f"challenge show {loop_name}` for the request and submission schema. Submit work "
        f"with `constraintloop challenge submit {loop_name} --file PATH`; never edit the "
        "loop journal directly. After edits, run one cycle to refresh the input snapshot "
        "before submitting verification. No external model evaluator is needed for this gate."
    )
