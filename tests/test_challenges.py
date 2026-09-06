from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from constraintloop.cli import main
from constraintloop.engine import ConstraintEngine
from constraintloop.hooks import handle_hook
from constraintloop.loops import (
    LoopError,
    journal_path,
    run_cycle,
    show_challenge,
    submit_challenge,
)
from constraintloop.models import Contract, Enforcement, LoopJournal, LoopState, Phase
from constraintloop.state import load_session


@pytest.fixture
def contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Contract:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "status").write_text("pass")
    (tmp_path / "domain.md").write_text("Pending evidence must never authorize completion.")
    return Contract.model_validate(
        {
            "constraints": {
                "tests": {
                    "kind": "command",
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        "raise SystemExit(0 if Path('status').read_text() == 'pass' else 1)",
                    ],
                    "phases": ["stop"],
                    "watch": ["status", "domain.md"],
                }
            },
            "loops": {
                "completion": {
                    "phase": "stop",
                    "interval_seconds": 1,
                    "max_repair_attempts": 2,
                    "max_unchanged_repairs": 1,
                    "max_duration_seconds": 600,
                    "challenge": {
                        "count": 2,
                        "max_rounds": 1,
                        "max_continuations": 4,
                        "watch": ["status", "domain.md"],
                        "domain_context": ["domain.md"],
                    },
                }
            },
        }
    )


def _write_contract(root: Path, contract: Contract) -> None:
    (root / "constraintloop.yml").write_text(yaml.safe_dump(contract.model_dump(mode="json")))


def _submission(root: Path, contract: Contract, kind: str) -> dict[str, Any]:
    request = show_challenge(root, contract, "completion")["request"]
    payload = {
        "kind": kind,
        "request_id": request["request_id"],
        "input_snapshot": request["input_snapshot"],
    }
    if kind == "discovery":
        payload.update(
            domain_summary="Completion requires fresh evidence, including after restarts.",
            domain_sources=["domain.md"],
            challenges=[
                {
                    "id": f"r{request['round']}-c{index}",
                    "perspective": perspective,
                    "assumption": "Pending evidence cannot pass.",
                    "scenario": f"Round {request['round']}: {scenario}",
                    "expected_behavior": "Completion remains blocked.",
                    "verification_plan": "Run tests covering the described transition.",
                    "source_refs": ["domain.md"],
                }
                for index, (perspective, scenario) in enumerate(
                    [
                        ("state transitions", "Evidence becomes pending during repair."),
                        ("recovery", "The process restarts with pending evidence."),
                    ],
                    start=1,
                )
            ],
        )
    else:
        payload["resolutions"] = [
            {
                "challenge_id": item["id"],
                "outcome": "verified",
                "evidence": "The tests gate exercises this transition and passes.",
                "constraint_ids": ["tests"],
                "source_refs": ["domain.md"],
            }
            for item in request["challenges"]
        ]
    return payload


def _discover(root: Path, contract: Contract) -> None:
    assert run_cycle(root, contract, "completion").state == LoopState.CHALLENGE
    submit_challenge(root, contract, "completion", _submission(root, contract, "discovery"))
    assert run_cycle(root, contract, "completion").state == LoopState.VERIFY


def test_complete_session_challenge_without_any_evaluator(
    tmp_path: Path, contract: Contract, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_evaluator(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Session challenge work must not call an external evaluator")

    monkeypatch.setattr("constraintloop.engine.build_evaluator", unexpected_evaluator)
    _discover(tmp_path, contract)
    request = show_challenge(tmp_path, contract, "completion")
    assert request["request"]["domain_briefs"][0]["sources"] == ["domain.md"]
    assert "resolutions" in request["submission_schema"]["properties"]
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    result = run_cycle(tmp_path, contract, "completion")
    assert result.state == LoopState.PASSED
    assert result.repair_attempt == 0
    assert not result.blocking_challenges


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
def test_all_adapters_continue_in_same_session_and_preserve_goal(
    tmp_path: Path, contract: Contract, adapter: str
) -> None:
    _write_contract(tmp_path, contract)
    session = {"session_id": "author"}
    handle_hook(tmp_path, adapter, "user-prompt", {**session, "prompt": "Fix pending evidence"})
    started = handle_hook(tmp_path, adapter, "session-start", session)
    assert "challenge gate" in started["hookSpecificOutput"]["additionalContext"]
    first = handle_hook(tmp_path, adapter, "stop", session)
    assert first["decision"] == ("deny" if adapter == "gemini" else "block")
    assert "Generate 2" in first["reason"]
    handle_hook(tmp_path, adapter, "user-prompt", {**session, "prompt": first["reason"]})
    assert load_session(tmp_path, "author")["goal"] == "Fix pending evidence"
    submit_challenge(tmp_path, contract, "completion", _submission(tmp_path, contract, "discovery"))
    second = handle_hook(tmp_path, adapter, "stop", {**session, "stop_hook_active": True})
    assert second["decision"] == ("deny" if adapter == "gemini" else "block")
    assert "unresolved challenges" in second["reason"]
    compact = handle_hook(tmp_path, adapter, "pre-compact", session)
    assert "Resume saved challenge work" in json.dumps(compact)
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    final = handle_hook(tmp_path, adapter, "stop", {**session, "stopHookActive": True})
    assert final.get("continue", final.get("decision") == "allow") is True
    assert (
        LoopJournal.model_validate_json(
            journal_path(tmp_path, "completion").read_text()
        ).repair_attempt
        == 0
    )


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
def test_continuation_budget_survives_restart_and_halts_every_adapter(
    tmp_path: Path, contract: Contract, adapter: str
) -> None:
    _write_contract(tmp_path, contract)
    for _ in range(4):
        response = handle_hook(tmp_path, adapter, "stop", {"stop_hook_active": True})
        assert response["decision"] == ("deny" if adapter == "gemini" else "block")
    exhausted = handle_hook(tmp_path, adapter, "stop", {"session_id": "restarted"})
    assert exhausted["continue"] is False
    assert "continuation budget" in exhausted["stopReason"]
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.BUDGET_EXHAUSTED
    with pytest.raises(LoopError, match="not accepting"):
        submit_challenge(
            tmp_path, contract, "completion", _submission(tmp_path, contract, "discovery")
        )


@pytest.mark.parametrize("mutation", ["count", "duplicate", "perspectives", "blank", "outside"])
def test_invalid_discovery_cannot_satisfy_gate(
    tmp_path: Path, contract: Contract, mutation: str
) -> None:
    run_cycle(tmp_path, contract, "completion")
    payload = _submission(tmp_path, contract, "discovery")
    if mutation == "count":
        payload["challenges"].pop()
    elif mutation == "duplicate":
        payload["challenges"][1]["scenario"] = payload["challenges"][0]["scenario"].upper()
    elif mutation == "perspectives":
        payload["challenges"][1]["perspective"] = payload["challenges"][0]["perspective"]
    elif mutation == "blank":
        payload["domain_summary"] = "  "
    else:
        payload["challenges"][0]["source_refs"] = ["../outside.md"]
    with pytest.raises(LoopError):
        submit_challenge(tmp_path, contract, "completion", payload)
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.CHALLENGE


def test_source_changes_invalidate_evidence_but_preserve_plans(
    tmp_path: Path, contract: Contract
) -> None:
    _discover(tmp_path, contract)
    previous = _submission(tmp_path, contract, "verification")
    submit_challenge(tmp_path, contract, "completion", previous)
    (tmp_path / "domain.md").write_text("Pending evidence must remain blocking after a restart.")
    with pytest.raises(LoopError, match="Stale|Inputs changed"):
        submit_challenge(tmp_path, contract, "completion", previous)
    result = run_cycle(tmp_path, contract, "completion")
    assert result.state == LoopState.VERIFY
    assert result.blocking_challenges == ["r1-c1", "r1-c2"]
    assert len(show_challenge(tmp_path, contract, "completion")["request"]["challenges"]) == 2
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.PASSED


def test_defect_is_blocking_and_repair_triggers_bounded_followup(
    tmp_path: Path, contract: Contract
) -> None:
    contract.loops["completion"].challenge.max_rounds = 2  # type: ignore[union-attr]
    _discover(tmp_path, contract)
    payload = _submission(tmp_path, contract, "verification")
    payload["resolutions"][0]["outcome"] = "defect"
    submit_challenge(tmp_path, contract, "completion", payload)
    repair = run_cycle(tmp_path, contract, "completion")
    assert repair.state == LoopState.REPAIR
    assert repair.blocking_challenges == ["r1-c1"]
    (tmp_path / "domain.md").write_text("The repaired implementation preserves pending state.")
    verify = run_cycle(tmp_path, contract, "completion")
    assert verify.state == LoopState.VERIFY
    assert verify.repair_attempt == 1
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.CHALLENGE
    submit_challenge(tmp_path, contract, "completion", _submission(tmp_path, contract, "discovery"))
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.VERIFY
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.PASSED
    assert len(show_challenge(tmp_path, contract, "completion")["request"]["challenges"]) == 4


def test_verified_claim_does_not_override_failing_deterministic_evidence(
    tmp_path: Path, contract: Contract
) -> None:
    _discover(tmp_path, contract)
    (tmp_path / "status").write_text("fail")
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.REPAIR
    # Discovery snapshots are refreshed only after the prerequisite gates pass.
    with pytest.raises(LoopError, match="Inputs changed"):
        submit_challenge(
            tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
        )


def test_replay_unknown_ids_missing_evidence_and_plan_replacement_are_rejected(
    tmp_path: Path, contract: Contract
) -> None:
    run_cycle(tmp_path, contract, "completion")
    discovery = _submission(tmp_path, contract, "discovery")
    submit_challenge(tmp_path, contract, "completion", discovery)
    with pytest.raises(LoopError, match="Stale"):
        submit_challenge(tmp_path, contract, "completion", discovery)
    run_cycle(tmp_path, contract, "completion")
    with pytest.raises(LoopError, match="already recorded"):
        submit_challenge(
            tmp_path, contract, "completion", _submission(tmp_path, contract, "discovery")
        )
    for field, value in [
        ("challenge_id", "missing"),
        ("constraint_ids", []),
        ("constraint_ids", ["missing"]),
    ]:
        payload = _submission(tmp_path, contract, "verification")
        payload["resolutions"][0][field] = value
        with pytest.raises(LoopError):
            submit_challenge(tmp_path, contract, "completion", payload)


def test_duration_expiration_blocks_submission_and_false_completion(
    tmp_path: Path, contract: Contract, monkeypatch: pytest.MonkeyPatch
) -> None:
    _discover(tmp_path, contract)
    payload = _submission(tmp_path, contract, "verification")
    journal = LoopJournal.model_validate_json(journal_path(tmp_path, "completion").read_text())
    monkeypatch.setattr("constraintloop.loops.time.time", lambda: journal.started_at + 601)
    with pytest.raises(LoopError, match="duration budget"):
        submit_challenge(tmp_path, contract, "completion", payload)
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.BUDGET_EXHAUSTED


def test_new_task_after_pass_requires_fresh_discovery(tmp_path: Path, contract: Contract) -> None:
    _discover(tmp_path, contract)
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.PASSED
    result = run_cycle(tmp_path, contract, "completion", goal="Handle concurrent supervisors")
    assert result.state == LoopState.CHALLENGE
    assert not show_challenge(tmp_path, contract, "completion")["request"]["challenges"]


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
def test_cli_roundtrip_and_native_prompt(tmp_path: Path, contract: Contract, adapter: str) -> None:
    _write_contract(tmp_path, contract)
    runner = CliRunner()
    project = ["--project", str(tmp_path)]
    prompt = runner.invoke(main, ["loop-prompt", "completion", "--adapter", adapter, *project])
    assert prompt.exit_code == 0
    assert "same coding session" in prompt.output
    cycle = runner.invoke(main, ["cycle", "completion", "--json", *project])
    assert cycle.exit_code == 15
    shown = runner.invoke(main, ["challenge", "show", "completion", *project])
    assert json.loads(shown.output)["request"]["discovery_pending"]
    submission_file = tmp_path / ".constraintloop" / "submission.json"
    submission_file.parent.mkdir(exist_ok=True)
    for kind, exit_code in [("discovery", 16), ("verification", 0)]:
        submission_file.write_text(json.dumps(_submission(tmp_path, contract, kind)))
        submitted = runner.invoke(
            main, ["challenge", "submit", "completion", "--file", str(submission_file), *project]
        )
        assert submitted.exit_code == 0, submitted.output
        cycle = runner.invoke(main, ["cycle", "completion", "--json", *project])
        assert cycle.exit_code == exit_code, cycle.output


@pytest.mark.parametrize(
    "change",
    [
        {"count": 0},
        {"max_rounds": 0},
        {"max_continuations": 0},
        {"watch": ["../outside"]},
        {"unknown": True},
    ],
)
def test_strict_challenge_configuration(contract: Contract, change: dict[str, Any]) -> None:
    raw = contract.model_dump(mode="json")
    raw["loops"]["completion"]["challenge"].update(change)
    with pytest.raises(ValidationError):
        Contract.model_validate(raw)


def test_ci_cannot_request_work_from_an_absent_session(contract: Contract) -> None:
    raw = contract.model_dump(mode="json")
    raw["loops"]["completion"]["phase"] = "ci"
    with pytest.raises(ValidationError, match="phase: stop"):
        Contract.model_validate(raw)


@pytest.mark.parametrize("use_hook", [False, True])
def test_inputs_changing_during_evaluation_cannot_complete(
    tmp_path: Path, contract: Contract, monkeypatch: pytest.MonkeyPatch, use_hook: bool
) -> None:
    _discover(tmp_path, contract)
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    _write_contract(tmp_path, contract)
    original_run = ConstraintEngine.run

    def changing_run(engine: ConstraintEngine, phase: Phase):
        record = original_run(engine, phase)
        (tmp_path / "domain.md").write_text("Domain rules changed while tests were running.")
        return record

    monkeypatch.setattr(ConstraintEngine, "run", changing_run)
    if use_hook:
        response = handle_hook(tmp_path, "gemini", "stop", {})
        assert response["decision"] == "deny"
        assert "changed during evaluation" in response["reason"]
    else:
        assert run_cycle(tmp_path, contract, "completion").state == LoopState.WAITING


def test_failing_advisory_gate_does_not_prove_verified_challenge(
    tmp_path: Path, contract: Contract
) -> None:
    contract.constraints["tests"].enforcement = Enforcement.ADVISORY
    (tmp_path / "status").write_text("fail")
    _discover(tmp_path, contract)
    submit_challenge(
        tmp_path, contract, "completion", _submission(tmp_path, contract, "verification")
    )
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.VERIFY


def test_unresolved_and_rejected_outcomes_are_explicit(tmp_path: Path, contract: Contract) -> None:
    _discover(tmp_path, contract)
    payload = _submission(tmp_path, contract, "verification")
    payload["resolutions"][0].update(outcome="unresolved", constraint_ids=[])
    payload["resolutions"][1].update(outcome="rejected", constraint_ids=[])
    submit_challenge(tmp_path, contract, "completion", payload)
    result = run_cycle(tmp_path, contract, "completion")
    assert result.state == LoopState.VERIFY
    assert result.blocking_challenges == ["r1-c1"]


def test_unchanged_confirmed_defect_requires_human(tmp_path: Path, contract: Contract) -> None:
    _discover(tmp_path, contract)
    payload = _submission(tmp_path, contract, "verification")
    payload["resolutions"][0]["outcome"] = "defect"
    submit_challenge(tmp_path, contract, "completion", payload)
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.REPAIR
    assert run_cycle(tmp_path, contract, "completion").state == LoopState.HUMAN_REQUIRED


def test_changed_defects_cannot_replenish_repair_budget(tmp_path: Path, contract: Contract) -> None:
    _discover(tmp_path, contract)
    for attempt in range(3):
        payload = _submission(tmp_path, contract, "verification")
        payload["resolutions"][0]["outcome"] = "defect"
        submit_challenge(tmp_path, contract, "completion", payload)
        result = run_cycle(tmp_path, contract, "completion")
        if attempt == 2:
            assert result.state == LoopState.BUDGET_EXHAUSTED
        else:
            assert result.state == LoopState.REPAIR
            (tmp_path / "domain.md").write_text(f"Attempted correction {attempt}")
            assert run_cycle(tmp_path, contract, "completion").state == LoopState.VERIFY


def test_corrupt_journal_halts_hook_and_cannot_reset_budget(
    tmp_path: Path, contract: Contract
) -> None:
    _write_contract(tmp_path, contract)
    handle_hook(tmp_path, "codex", "stop", {})
    journal_path(tmp_path, "completion").write_text("{")
    result = handle_hook(tmp_path, "codex", "stop", {"stop_hook_active": True})
    assert result["continue"] is False
    assert "corrupt" in result["stopReason"]


def test_new_session_without_prompt_can_resume_saved_goal(
    tmp_path: Path, contract: Contract
) -> None:
    _write_contract(tmp_path, contract)
    run_cycle(tmp_path, contract, "completion", goal="Fix restart handling")
    response = handle_hook(tmp_path, "gemini", "stop", {"session_id": "resumed"})
    assert response["decision"] == "deny"
    assert "Generate 2" in response["reason"]


def test_submissions_require_existing_request_and_unchanged_policy(
    tmp_path: Path, contract: Contract
) -> None:
    with pytest.raises(LoopError, match="No valid"):
        show_challenge(tmp_path, contract, "completion")
    with pytest.raises(LoopError, match="no session challenge"):
        show_challenge(tmp_path, contract, "missing")
    run_cycle(tmp_path, contract, "completion")
    payload = _submission(tmp_path, contract, "discovery")
    contract.loops["completion"].max_duration_seconds = 700
    with pytest.raises(LoopError, match="contract changed"):
        submit_challenge(tmp_path, contract, "completion", payload)
