"""Versioned semantic evaluator corpus models and acceptance rules."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from constraintloop.models import EvaluatorVerdict, StrictModel


class EvaluationCase(StrictModel):
    id: str
    expected_verdict: Literal["pass", "fail"]
    goal: str
    diff: str
    files: dict[str, str] = Field(default_factory=dict)
    required_finding_terms: list[str] = Field(default_factory=list)


class EvaluationCorpus(StrictModel):
    schema_version: Literal[1]
    rubric: str
    cases: list[EvaluationCase] = Field(min_length=1)


def case_failures(case: EvaluationCase, verdicts: list[EvaluatorVerdict]) -> list[str]:
    """Return deterministic reasons a repeated live evaluation missed its expectation."""
    failures: list[str] = []
    passes = sum(verdict.verdict == "pass" for verdict in verdicts)
    fails = sum(verdict.verdict == "fail" for verdict in verdicts)
    majority = len(verdicts) // 2 + 1
    if case.expected_verdict == "pass" and passes < majority:
        failures.append(f"expected a passing majority, observed {passes}/{len(verdicts)}")
    if case.expected_verdict == "fail":
        if passes:
            failures.append(f"known-bad case received {passes} passing verdict(s)")
        if fails < majority:
            failures.append(f"expected a failing majority, observed {fails}/{len(verdicts)}")
        finding_text = " ".join(
            finding.message.lower() for verdict in verdicts for finding in verdict.findings
        )
        missing = [term for term in case.required_finding_terms if term.lower() not in finding_text]
        if missing:
            failures.append(f"required finding terms were absent: {missing}")
    return failures
