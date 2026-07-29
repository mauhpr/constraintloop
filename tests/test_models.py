from __future__ import annotations

import pytest
from pydantic import ValidationError

from constraintloop.models import Contract


def test_required_rubric_demands_majority_quorum() -> None:
    with pytest.raises(ValidationError, match="at least two runs"):
        Contract.model_validate(
            {
                "version": 1,
                "constraints": {
                    "review": {
                        "kind": "rubric",
                        "enforcement": "required",
                        "evaluator": "reviewer",
                        "rubric": "No regressions",
                    }
                },
                "evaluators": {"reviewer": {"type": "command", "command": ["reviewer"]}},
            }
        )


def test_contract_rejects_dependency_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        Contract.model_validate(
            {
                "version": 1,
                "constraints": {
                    "a": {"kind": "artifact", "path": "a", "needs": ["b"]},
                    "b": {"kind": "artifact", "path": "b", "needs": ["a"]},
                },
            }
        )


@pytest.mark.parametrize(
    "constraint",
    [
        {"kind": "command", "command": "pytest"},
        {"kind": "command", "command": []},
        {
            "kind": "metric",
            "command": "measure",
            "parser": {"type": "json", "path": "score"},
            "threshold": {"operator": "gte", "value": 1},
        },
        {
            "kind": "metric",
            "command": ["measure"],
            "parser": {"type": "json"},
            "threshold": {"operator": "gte", "value": 1},
        },
        {
            "kind": "metric",
            "command": ["measure"],
            "parser": {"type": "regex"},
            "threshold": {"operator": "gte", "value": 1},
        },
        {
            "kind": "metric",
            "command": ["measure"],
            "parser": {"type": "json", "source": "file", "path": "score"},
            "threshold": {"operator": "gte", "value": 1},
        },
    ],
)
def test_constraint_models_reject_unsafe_or_incomplete_commands(
    constraint: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Contract.model_validate({"constraints": {"check": constraint}})


def test_contract_rejects_unknown_dependencies_and_evaluators() -> None:
    with pytest.raises(ValidationError, match="unknown constraints"):
        Contract.model_validate(
            {"constraints": {"a": {"kind": "artifact", "path": "a", "needs": ["missing"]}}}
        )
    with pytest.raises(ValidationError, match="unknown evaluator"):
        Contract.model_validate(
            {
                "constraints": {
                    "review": {
                        "kind": "rubric",
                        "enforcement": "advisory",
                        "evaluator": "missing",
                        "rubric": "review",
                    }
                }
            }
        )


def test_rubric_quorum_may_not_exceed_runs() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        Contract.model_validate(
            {
                "constraints": {
                    "review": {
                        "kind": "rubric",
                        "enforcement": "advisory",
                        "evaluator": "reviewer",
                        "rubric": "review",
                        "runs": 1,
                        "pass_quorum": 2,
                    }
                },
                "evaluators": {"reviewer": {"type": "command", "command": ["reviewer"]}},
            }
        )


def test_loop_schema_is_bounded_and_references_an_active_phase() -> None:
    base = {
        "constraints": {
            "check": {
                "kind": "command",
                "command": ["check"],
                "phases": ["stop"],
            }
        },
        "loops": {
            "completion": {
                "phase": "stop",
                "interval_seconds": 10,
                "max_repair_attempts": 3,
                "max_unchanged_repairs": 2,
                "max_duration_seconds": 1200,
            }
        },
    }
    assert Contract.model_validate(base).loops["completion"].max_repair_attempts == 3

    unbounded = {
        **base,
        "loops": {
            "completion": {
                **base["loops"]["completion"],
                "max_repair_attempts": 0,
            }
        },
    }
    with pytest.raises(ValidationError):
        Contract.model_validate(unbounded)

    missing_phase = {
        **base,
        "loops": {
            "completion": {
                **base["loops"]["completion"],
                "phase": "ci",
            }
        },
    }
    with pytest.raises(ValidationError, match="no enabled constraints"):
        Contract.model_validate(missing_phase)


def test_pending_and_success_codes_cannot_overlap() -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        Contract.model_validate(
            {
                "constraints": {
                    "check": {
                        "kind": "command",
                        "command": ["check"],
                        "success_codes": [0, 75],
                    }
                }
            }
        )


@pytest.mark.parametrize("pattern", ["", "/absolute/**", "../outside/**", "bad\\glob"])
def test_contract_rejects_non_relative_globs(pattern: str) -> None:
    with pytest.raises(ValidationError, match="project-relative"):
        Contract.model_validate(
            {
                "constraints": {
                    "check": {
                        "kind": "artifact",
                        "path": "result.json",
                        "watch": [pattern],
                    }
                }
            }
        )


def test_contract_rejects_invalid_names_empty_commands_and_multiple_stop_loops() -> None:
    with pytest.raises(ValidationError, match="invalid identifier"):
        Contract.model_validate(
            {"constraints": {"bad/name": {"kind": "artifact", "path": "result"}}}
        )
    with pytest.raises(ValidationError, match="argv cannot be empty"):
        Contract.model_validate(
            {
                "constraints": {
                    "metric": {
                        "kind": "metric",
                        "command": [],
                        "parser": {"type": "json", "path": "value"},
                        "threshold": {"operator": "gte", "value": 1},
                    }
                }
            }
        )
    with pytest.raises(ValidationError, match="argv cannot be empty"):
        Contract.model_validate(
            {
                "constraints": {"check": {"kind": "artifact", "path": "result"}},
                "evaluators": {"review": {"type": "command", "command": []}},
            }
        )
    loop = {
        "phase": "stop",
        "interval_seconds": 1,
        "max_repair_attempts": 1,
        "max_unchanged_repairs": 1,
        "max_duration_seconds": 10,
    }
    with pytest.raises(ValidationError, match="at most one"):
        Contract.model_validate(
            {
                "constraints": {
                    "check": {
                        "kind": "artifact",
                        "path": "result",
                        "phases": ["stop"],
                    }
                },
                "loops": {"first": loop, "second": loop},
            }
        )
