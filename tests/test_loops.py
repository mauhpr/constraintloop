from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from constraintloop.cli import main
from constraintloop.hooks import handle_hook
from constraintloop.loops import (
    LoopError,
    journal_path,
    loop_lease,
    loop_prompt,
    run_cycle,
    supervise,
)
from constraintloop.models import Contract, CycleResult, LoopState


def _contract(command: list[str], *, unchanged: int = 2, attempts: int = 3) -> Contract:
    return Contract.model_validate(
        {
            "constraints": {
                "check": {
                    "kind": "command",
                    "command": command,
                    "phases": ["stop"],
                    "watch": ["status"],
                }
            },
            "loops": {
                "completion": {
                    "phase": "stop",
                    "interval_seconds": 10,
                    "max_repair_attempts": attempts,
                    "max_unchanged_repairs": unchanged,
                    "max_duration_seconds": 100,
                }
            },
        }
    )


def _status_command(counter: Path | None = None) -> list[str]:
    counter_code = ""
    if counter is not None:
        counter_code = (
            f"counter=Path({str(counter)!r}); "
            "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1'); "
        )
    return [
        sys.executable,
        "-c",
        "from pathlib import Path; " + counter_code + "value=Path('status').read_text(); "
        "raise SystemExit(0 if value == 'pass' else 75 if value == 'pending' else 1)",
    ]


def test_pending_polling_waits_without_consuming_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    counter = tmp_path / "counter"
    (tmp_path / "status").write_text("pending", encoding="utf-8")
    contract = _contract(_status_command(counter))

    first = run_cycle(tmp_path, contract, "completion", now=100)
    second = run_cycle(tmp_path, contract, "completion", now=101)
    third = run_cycle(tmp_path, contract, "completion", now=111)

    assert first.state == second.state == third.state == LoopState.WAITING
    assert second.observation == 2
    assert third.observation == 3
    assert third.repair_attempt == 0
    assert counter.read_text(encoding="utf-8") == "2"


def test_repair_accounting_survives_restart_and_changed_evidence_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    status = tmp_path / "status"
    status.write_text("fail", encoding="utf-8")
    contract = _contract(_status_command(), unchanged=2)

    first = run_cycle(tmp_path, contract, "completion", now=100)
    second = run_cycle(tmp_path, contract, "completion", now=101)
    status.write_text("pass", encoding="utf-8")
    third = run_cycle(tmp_path, contract, "completion", now=102)

    assert first.state == LoopState.REPAIR
    assert first.repair_attempt == 0
    assert second.state == LoopState.REPAIR
    assert second.repair_attempt == 1
    assert third.state == LoopState.PASSED
    assert third.repair_attempt == 2


def test_unchanged_repairs_and_budgets_stop_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "status").write_text("fail", encoding="utf-8")
    unchanged = _contract(_status_command(), unchanged=1)
    assert run_cycle(tmp_path, unchanged, "completion", now=100).state == LoopState.REPAIR
    result = run_cycle(tmp_path, unchanged, "completion", now=101)
    assert result.state == LoopState.HUMAN_REQUIRED

    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "other-cache"))
    attempts = _contract(_status_command(), unchanged=5, attempts=1)
    assert run_cycle(tmp_path, attempts, "completion", now=100).state == LoopState.REPAIR
    result = run_cycle(tmp_path, attempts, "completion", now=101)
    assert result.state == LoopState.BUDGET_EXHAUSTED


def test_active_lease_blocks_and_stale_lease_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    with (
        loop_lease(tmp_path, "completion", ttl_seconds=10, now=100),
        pytest.raises(LoopError, match="active supervisor"),
        loop_lease(tmp_path, "completion", ttl_seconds=10, now=101),
    ):
        pass
    with loop_lease(tmp_path, "completion", ttl_seconds=10, now=200):
        pass


def test_cycle_cli_exit_codes_and_native_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "status").write_text("fail", encoding="utf-8")
    payload = _contract(_status_command()).model_dump(mode="json")
    config_name = "constraintloop" + ".yml"
    (tmp_path / config_name).write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(main, ["cycle", "completion", "--json", "--project", str(tmp_path)])
    assert result.exit_code == 10
    assert json.loads(result.output)["state"] == "repair"
    for adapter in ("codex", "claude"):
        prompt = loop_prompt("completion", adapter)
        assert "exactly once" in prompt
        assert "at most one repair" in prompt
        assert "Never edit" in prompt


def test_stop_hook_and_cycle_share_the_attempt_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "status").write_text("fail", encoding="utf-8")
    contract = _contract(_status_command(), unchanged=1)
    config_name = "constraintloop" + ".yml"
    (tmp_path / config_name).write_text(
        yaml.safe_dump(contract.model_dump(mode="json")), encoding="utf-8"
    )

    response = handle_hook(tmp_path, "codex", "stop", {"session_id": "shared"})
    assert response["decision"] == "block"
    result = run_cycle(tmp_path, contract, "completion")
    assert result.repair_attempt == 1
    assert result.state == LoopState.HUMAN_REQUIRED


def test_pass_error_duration_and_corrupt_journal_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(cache))
    status = tmp_path / "status"
    status.write_text("pass", encoding="utf-8")
    contract = _contract(_status_command())
    assert run_cycle(tmp_path, contract, "completion", now=100).state == LoopState.PASSED

    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "error-cache"))
    status.write_text("fail", encoding="utf-8")
    broken = _contract(["definitely-not-a-loop-command"])
    assert run_cycle(tmp_path, broken, "completion", now=100).state == LoopState.ERROR

    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "duration-cache"))
    assert run_cycle(tmp_path, contract, "completion", now=100).state == LoopState.REPAIR
    assert run_cycle(tmp_path, contract, "completion", now=201).state == LoopState.BUDGET_EXHAUSTED

    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(cache))
    path = journal_path(tmp_path, "completion")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"broken":true}', encoding="utf-8")
    with pytest.raises(LoopError, match="journal is corrupt"):
        run_cycle(tmp_path, contract, "completion", now=300)


def test_invalid_and_unknown_loop_names_are_rejected(tmp_path: Path) -> None:
    contract = _contract(_status_command())
    with pytest.raises(LoopError, match="Unknown loop"):
        run_cycle(tmp_path, contract, "missing")
    with pytest.raises(LoopError, match="Invalid loop name"):
        journal_path(tmp_path, "../escape")
    with pytest.raises(LoopError, match="Unsupported loop adapter"):
        loop_prompt("completion", "gemini")


def test_supervisor_emits_a_nonwaiting_transition_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    contract = _contract(_status_command())
    result = CycleResult(
        loop="completion",
        state=LoopState.REPAIR,
        snapshot="sha256:test",
        observation=1,
        repair_attempt=0,
        next_action="repair",
        wake_after_seconds=0,
        blocking_constraints=["check"],
    )
    monkeypatch.setattr("constraintloop.loops.run_cycle", lambda *args, **kwargs: result)
    monkeypatch.setattr("constraintloop.loops.signal.signal", lambda *args: None)

    assert list(supervise(tmp_path, contract, "completion")) == [result]
    with loop_lease(tmp_path, "completion", ttl_seconds=10):
        pass
