from __future__ import annotations

from pathlib import Path

import yaml

from constraintloop.eval_corpus import EvaluationCorpus, case_failures
from constraintloop.models import EvaluatorVerdict


def _verdict(value: str, *findings: str) -> EvaluatorVerdict:
    return EvaluatorVerdict.model_validate(
        {
            "verdict": value,
            "rationale": "fixture",
            "findings": [{"message": finding} for finding in findings],
        }
    )


def test_versioned_openai_corpus_is_strict_and_has_known_bad_cases() -> None:
    path = Path(__file__).parent / "fixtures" / "openai_eval_corpus_v1.yml"
    corpus = EvaluationCorpus.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert any(case.expected_verdict == "pass" for case in corpus.cases)
    assert sum(case.expected_verdict == "fail" for case in corpus.cases) >= 2
    assert all(case.diff for case in corpus.cases)


def test_known_bad_case_rejects_any_pass_and_requires_findings() -> None:
    corpus = EvaluationCorpus(
        schema_version=1,
        rubric="Find boundary regressions",
        cases=[
            {
                "id": "bad",
                "expected_verdict": "fail",
                "goal": "preserve checks",
                "diff": "unsafe",
                "required_finding_terms": ["boundary"],
            }
        ],
    )
    case = corpus.cases[0]
    failures = case_failures(
        case,
        [
            _verdict("pass"),
            _verdict("fail", "unrelated defect"),
            _verdict("fail", "boundary bypass"),
        ],
    )
    assert any("known-bad" in failure for failure in failures)


def test_expected_corpus_outcomes_can_succeed() -> None:
    passing = EvaluationCorpus(
        schema_version=1,
        rubric="review",
        cases=[
            {
                "id": "good",
                "expected_verdict": "pass",
                "goal": "safe",
                "diff": "safe",
            },
            {
                "id": "bad",
                "expected_verdict": "fail",
                "goal": "safe",
                "diff": "unsafe",
                "required_finding_terms": ["path"],
            },
        ],
    )
    assert not case_failures(
        passing.cases[0], [_verdict("pass"), _verdict("pass"), _verdict("fail")]
    )
    assert not case_failures(
        passing.cases[1],
        [
            _verdict("fail", "path escape"),
            _verdict("fail", "path traversal"),
            _verdict("uncertain"),
        ],
    )
