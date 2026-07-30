from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from constraintloop.engine import ConstraintEngine
from constraintloop.models import ConstraintResult, Contract, Enforcement, Phase, Verdict
from constraintloop.state import (
    advisory_acknowledgment_reason,
    create_advisory_acknowledgment,
    evidence_path,
    load_cached_result,
    save_cached_result,
)
from tests.failure_lab import failing_command, write_contract


def test_dependency_failure_prevents_downstream_execution(tmp_path: Path) -> None:
    marker = tmp_path / "downstream-ran"
    contract = Contract.model_validate(
        {
            "constraints": {
                "first": failing_command(),
                "downstream": {
                    "kind": "command",
                    "command": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).touch()",
                    ],
                    "needs": ["first"],
                    "phases": ["stop"],
                },
            }
        }
    )
    record = ConstraintEngine(tmp_path, contract, use_cache=False).run(Phase.STOP)
    assert [result.verdict for result in record.results] == [Verdict.FAIL, Verdict.ERROR]
    assert "Dependencies did not pass" in record.results[1].message
    assert not marker.exists()


def test_malformed_evaluator_output_is_uncertain_and_blocks(tmp_path: Path) -> None:
    contract = Contract.model_validate(
        {
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "required",
                    "evaluator": "broken",
                    "rubric": "Reject malformed output",
                    "runs": 2,
                    "pass_quorum": 2,
                    "phases": ["stop"],
                }
            },
            "evaluators": {
                "broken": {
                    "type": "command",
                    "command": [sys.executable, "-c", "print('not json')"],
                }
            },
        }
    )
    result = ConstraintEngine(tmp_path, contract, use_cache=False).run(Phase.STOP)
    assert result.results[0].verdict == Verdict.UNCERTAIN
    assert not result.passed
    assert "invalid structured JSON" in (result.results[0].output_tail or "")


def test_corrupt_evidence_is_treated_as_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    path = evidence_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"check":', encoding="utf-8")
    assert load_cached_result(tmp_path, "check", "digest") is None

    path.write_text('{"check":{"input_digest":"digest","verdict":"invented"}}', encoding="utf-8")
    assert load_cached_result(tmp_path, "check", "digest") is None


def test_concurrent_evidence_writes_preserve_every_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))

    def save(number: int) -> None:
        save_cached_result(
            tmp_path,
            ConstraintResult(
                constraint_id=f"check-{number}",
                kind="command",
                verdict=Verdict.PASS,
                enforcement=Enforcement.REQUIRED,
                input_digest=f"digest-{number}",
                message="passed",
            ),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(40)))

    payload = json.loads(evidence_path(tmp_path).read_text(encoding="utf-8"))
    assert set(payload) == {f"check-{number}" for number in range(40)}


def test_advisory_acknowledgment_is_bound_to_exact_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    result = ConstraintResult(
        constraint_id="review",
        kind="rubric",
        verdict=Verdict.FAIL,
        enforcement=Enforcement.ADVISORY,
        input_digest="snapshot",
        message="review failed",
        output_tail="finding one",
    )
    create_advisory_acknowledgment(tmp_path, result, "explained")
    assert advisory_acknowledgment_reason(tmp_path, result) == "explained"

    changed = result.model_copy(update={"output_tail": "different finding"})
    assert advisory_acknowledgment_reason(tmp_path, changed) is None


def test_real_cli_and_hook_subprocesses_report_controlled_failure(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    write_contract(project, {"deliberate_failure": failing_command()})
    environment = os.environ.copy()
    environment["CONSTRAINTLOOP_CACHE_DIR"] = str(tmp_path / "cache")

    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "constraintloop",
            "run",
            "--project",
            str(project),
            "--json",
            "--no-cache",
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert run.returncode == 1
    record = json.loads(run.stdout)
    assert record["results"][0]["exit_code"] == 23

    hook = subprocess.run(
        [
            sys.executable,
            "-m",
            "constraintloop",
            "hook",
            "--adapter",
            "codex",
            "--event",
            "stop",
            "--project",
            str(project),
        ],
        input=json.dumps({"session_id": "failure-lab", "cwd": str(project)}),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    response = json.loads(hook.stdout)
    assert response["decision"] == "block"
    assert "deliberate_failure" in response["reason"]


def test_documented_examples_match_strict_schema() -> None:
    examples = Path(__file__).parents[1] / "examples"

    for example in sorted(examples.rglob("*.yml")):
        Contract.model_validate(yaml.safe_load(example.read_text(encoding="utf-8")))
