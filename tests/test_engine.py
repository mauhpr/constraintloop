from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from constraintloop.config import contract_digest
from constraintloop.engine import ConstraintEngine, format_summary
from constraintloop.models import (
    ConstraintResult,
    Contract,
    Enforcement,
    Phase,
    RubricConstraint,
    Verdict,
)
from constraintloop.state import create_waiver, load_latest_result, save_cached_result


def _command_contract() -> Contract:
    return Contract.model_validate(
        {
            "version": 1,
            "constraints": {
                "check": {
                    "kind": "command",
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        "raise SystemExit(Path('value').read_text() != 'ok')",
                    ],
                    "phases": ["stop", "ci"],
                    "watch": ["value"],
                }
            },
        }
    )


def test_cache_is_content_addressed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "value").write_text("ok", encoding="utf-8")
    contract = _command_contract()

    first = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    second = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert first.passed
    assert second.results[0].cached

    (tmp_path / "value").write_text("bad", encoding="utf-8")
    third = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert not third.passed
    assert not third.results[0].cached


def test_engine_streams_start_heartbeat_and_completion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    contract = Contract.model_validate(
        {
            "settings": {"progress_interval_seconds": 0.1},
            "constraints": {
                "slow": {
                    "kind": "command",
                    "command": [sys.executable, "-c", "import time; time.sleep(0.25)"],
                    "phases": ["stop"],
                }
            },
        }
    )
    progress: list[str] = []

    record = ConstraintEngine(tmp_path, contract, progress=progress.append).run(Phase.STOP)

    assert record.passed
    assert progress[0] == "RUN slow (command)"
    assert any(line.startswith("STILL RUNNING slow") for line in progress)
    assert progress[-1].startswith("DONE slow: pass")


def test_local_waiver_is_snapshot_bound_and_ci_ignores_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "value").write_text("bad", encoding="utf-8")
    contract = _command_contract()
    failure = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    create_waiver(tmp_path, failure, contract_digest(contract), "known local issue")

    local = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    ci = ConstraintEngine(tmp_path, contract, use_cache=False, allow_waivers=False).run(Phase.CI)
    assert local.results[0].verdict == Verdict.WAIVED
    assert not ci.passed

    (tmp_path / "value").write_text("different", encoding="utf-8")
    changed = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert changed.results[0].verdict != Verdict.WAIVED


def test_local_waiver_is_bound_to_exact_cached_failure_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "value").write_text("bad", encoding="utf-8")
    contract = _command_contract()
    failure = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    create_waiver(tmp_path, failure, contract_digest(contract), "known local issue")
    save_cached_result(
        tmp_path,
        failure.model_copy(update={"output_tail": "different evidence"}),
    )

    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]

    assert result.verdict == Verdict.FAIL
    assert result.output_tail == "different evidence"


def test_required_rubric_uses_quorum(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    evaluator = [
        sys.executable,
        "-c",
        "import json; print(json.dumps("
        "{'verdict':'pass','score':1,'rationale':'ok','findings':[]}))",
    ]
    contract = Contract.model_validate(
        {
            "version": 1,
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "required",
                    "evaluator": "reviewer",
                    "rubric": "Behavior is correct",
                    "runs": 3,
                    "pass_quorum": 2,
                    "phases": ["stop"],
                }
            },
            "evaluators": {"reviewer": {"type": "command", "command": evaluator}},
        }
    )
    record = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert record.passed
    assert "3/3" in record.results[0].message


def test_evaluator_configuration_change_invalidates_rubric_cache(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))

    def contract_with_rationale(rationale: str) -> Contract:
        evaluator = [
            sys.executable,
            "-c",
            "import json; print(json.dumps("
            f"{{'verdict':'pass','score':1,'rationale':{rationale!r},'findings':[]}}))",
        ]
        return Contract.model_validate(
            {
                "version": 1,
                "constraints": {
                    "review": {
                        "kind": "rubric",
                        "enforcement": "advisory",
                        "evaluator": "reviewer",
                        "rubric": "Behavior is correct",
                        "phases": ["stop"],
                    }
                },
                "evaluators": {"reviewer": {"type": "command", "command": evaluator}},
            }
        )

    first = ConstraintEngine(tmp_path, contract_with_rationale("first")).run(Phase.STOP)
    second = ConstraintEngine(tmp_path, contract_with_rationale("second")).run(Phase.STOP)

    assert not first.results[0].cached
    assert not second.results[0].cached
    assert second.results[0].output_tail == "second"


def test_rubric_cache_is_bound_to_complete_evaluation_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / ".constraintloop" / "cache"))
    counter = tmp_path / ".constraintloop" / "evaluator-count"
    counter.parent.mkdir()
    evaluator = [
        sys.executable,
        "-c",
        "from pathlib import Path; import json; "
        f"p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
        "p.write_text(str(n)); "
        "print(json.dumps({'verdict':'pass','score':1,'rationale':str(n),'findings':[]}))",
    ]
    contract = Contract.model_validate(
        {
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "advisory",
                    "evaluator": "reviewer",
                    "rubric": "Review live evidence",
                    "phases": ["stop"],
                }
            },
            "evaluators": {"reviewer": {"type": "command", "command": evaluator}},
        }
    )

    first = ConstraintEngine(tmp_path, contract, goal="first goal").run(Phase.STOP)
    second = ConstraintEngine(tmp_path, contract, goal="first goal").run(Phase.STOP)
    changed_goal = ConstraintEngine(tmp_path, contract, goal="different goal").run(Phase.STOP)

    assert first.results[0].output_tail == "1"
    assert second.results[0].output_tail == "1"
    assert second.results[0].cached
    assert changed_goal.results[0].output_tail == "2"
    assert not changed_goal.results[0].cached


def test_rubric_cache_is_invalidated_by_prerequisite_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / ".constraintloop" / "cache"))
    source = tmp_path / "source"
    source.write_text("stable", encoding="utf-8")
    counter = tmp_path / ".constraintloop" / "evaluator-count"
    counter.parent.mkdir()
    evaluator = [
        sys.executable,
        "-c",
        "from pathlib import Path; import json; "
        f"p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
        "p.write_text(str(n)); "
        "print(json.dumps({'verdict':'pass','score':1,'rationale':str(n),'findings':[]}))",
    ]
    contract = Contract.model_validate(
        {
            "constraints": {
                "check": {
                    "kind": "command",
                    "command": [sys.executable, "-c", "print('original evidence')"],
                    "watch": ["source"],
                    "phases": ["stop"],
                },
                "review": {
                    "kind": "rubric",
                    "enforcement": "advisory",
                    "evaluator": "reviewer",
                    "rubric": "Review prerequisite evidence",
                    "include": ["source"],
                    "watch": ["source"],
                    "needs": ["check"],
                    "phases": ["stop"],
                },
            },
            "evaluators": {"reviewer": {"type": "command", "command": evaluator}},
        }
    )
    first = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    unchanged = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert unchanged.results[1].cached
    check = load_latest_result(tmp_path, "check")
    assert check is not None
    save_cached_result(
        tmp_path,
        check.model_copy(update={"output_tail": "different prerequisite evidence"}),
    )

    changed = ConstraintEngine(tmp_path, contract).run(Phase.STOP)

    assert first.results[1].output_tail == "1"
    assert changed.results[1].output_tail == "2"
    assert not changed.results[1].cached


def test_rubric_ignores_preexisting_local_waiver(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    evaluator = [
        sys.executable,
        "-c",
        "import json; print(json.dumps("
        "{'verdict':'fail','score':0,'rationale':'reviewed','findings':[]}))",
    ]
    contract = Contract.model_validate(
        {
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "advisory",
                    "evaluator": "reviewer",
                    "rubric": "Review live evidence",
                    "phases": ["stop"],
                }
            },
            "evaluators": {"reviewer": {"type": "command", "command": evaluator}},
        }
    )
    first = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    create_waiver(tmp_path, first, contract_digest(contract), "legacy waiver")

    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]

    assert result.verdict == Verdict.FAIL
    assert result.output_tail == "reviewed"


def test_project_evaluator_environment_is_scoped_per_engine(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("PROJECT_REVIEW_TOKEN", raising=False)
    evaluator = [
        sys.executable,
        "-c",
        "import json, os; print(json.dumps("
        "{'verdict':'pass','score':1,'rationale':os.environ['PROJECT_REVIEW_TOKEN'],"
        "'findings':[]}))",
    ]
    contract = Contract.model_validate(
        {
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "advisory",
                    "evaluator": "reviewer",
                    "rubric": "Review",
                    "phases": ["stop"],
                }
            },
            "evaluators": {"reviewer": {"type": "command", "command": evaluator}},
        }
    )
    roots = [tmp_path / "first", tmp_path / "second"]
    for root, value in zip(roots, ("first-token", "second-token"), strict=True):
        secrets = root / ".constraintloop" / "secrets.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text(f"PROJECT_REVIEW_TOKEN={value}\n", encoding="utf-8")
        secrets.chmod(0o600)

    first = ConstraintEngine(roots[0], contract).run(Phase.STOP).results[0]
    second = ConstraintEngine(roots[1], contract).run(Phase.STOP).results[0]

    assert first.output_tail == "first-token"
    assert second.output_tail == "second-token"
    assert "PROJECT_REVIEW_TOKEN" not in os.environ


def test_evaluation_bundle_prioritizes_goal_relevant_source_files(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x" * 4070, encoding="utf-8")
    relevant = tmp_path / "src" / "repair_loop.py"
    relevant.write_text("def transition(): return 'repair'\n", encoding="utf-8")
    contract = Contract.model_validate(
        {
            "settings": {"evaluation_bundle_limit": 4096},
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "advisory",
                    "evaluator": "reviewer",
                    "rubric": "Verify the repair loop transition",
                    "include": ["docs/*", "src/*"],
                    "phases": ["stop"],
                }
            },
            "evaluators": {
                "reviewer": {
                    "type": "command",
                    "command": [sys.executable, "-c", "raise SystemExit(1)"],
                }
            },
        }
    )
    engine = ConstraintEngine(tmp_path, contract, goal="repair loop")
    spec = contract.constraints["review"]
    assert isinstance(spec, RubricConstraint)

    bundle = engine._build_bundle("review", spec, [])

    assert "src/repair_loop.py" in bundle.files
    assert "docs/a.md" not in bundle.files
    assert "docs/a.md" in bundle.omitted_files


def test_evaluation_bundle_excludes_secret_paths_from_full_file_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "safe.py").write_text("SAFE = True\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=full-file-secret\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text(
        '{"token":"credential-file-secret"}',
        encoding="utf-8",
    )
    (tmp_path / "private.pem").write_text("private-key-secret", encoding="utf-8")
    contract = Contract.model_validate(
        {
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "advisory",
                    "evaluator": "reviewer",
                    "rubric": "Review all files",
                    "include": ["**/*"],
                    "phases": ["stop"],
                }
            },
            "evaluators": {
                "reviewer": {
                    "type": "command",
                    "command": [sys.executable, "-c", "raise SystemExit(1)"],
                }
            },
        }
    )
    spec = contract.constraints["review"]
    assert isinstance(spec, RubricConstraint)
    bundle = ConstraintEngine(tmp_path, contract)._build_bundle("review", spec, [])

    assert "safe.py" in bundle.files
    assert {".env", "credentials.json", "private.pem"} <= set(bundle.omitted_files)
    serialized = bundle.model_dump_json()
    assert "full-file-secret" not in serialized
    assert "credential-file-secret" not in serialized
    assert "private-key-secret" not in serialized


def test_ready_deterministic_constraints_run_concurrently_in_contract_order(
    tmp_path: Path, monkeypatch
) -> None:
    contract = Contract.model_validate(
        {
            "settings": {"concurrency": 3},
            "constraints": {
                name: {
                    "kind": "command",
                    "command": [sys.executable, "-c", "pass"],
                    "phases": ["stop"],
                }
                for name in ("first", "second", "third")
            },
        }
    )
    barrier = threading.Barrier(3)

    def run(project_root, constraint_id, spec, digest, output_limit):
        barrier.wait(timeout=2)
        return ConstraintResult(
            constraint_id=constraint_id,
            kind="command",
            verdict=Verdict.PASS,
            enforcement=Enforcement.REQUIRED,
            input_digest=digest,
            message="passed concurrently",
        )

    monkeypatch.setattr("constraintloop.engine.run_command_constraint", run)
    record = ConstraintEngine(tmp_path, contract, use_cache=False).run(Phase.STOP)
    assert [result.constraint_id for result in record.results] == [
        "first",
        "second",
        "third",
    ]


def test_summary_formats_nested_structured_evidence(tmp_path: Path) -> None:
    contract = Contract.model_validate(
        {
            "constraints": {
                "report": {
                    "kind": "artifact",
                    "path": "report.json",
                    "format": "json",
                    "evidence": {"counts": "counts"},
                    "phases": ["stop"],
                }
            }
        }
    )
    (tmp_path / "report.json").write_text('{"counts":{"current":2}}', encoding="utf-8")

    summary = format_summary(ConstraintEngine(tmp_path, contract).run(Phase.STOP))

    assert 'counts={"current":2}' in summary
