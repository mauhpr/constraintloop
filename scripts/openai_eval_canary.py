"""Run the versioned semantic corpus against OpenAI when explicitly requested."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from constraintloop.environment import load_project_environment
from constraintloop.eval_corpus import EvaluationCorpus, case_failures
from constraintloop.evaluators import EvaluatorError, OpenAIEvaluator
from constraintloop.models import EvaluationBundle, OpenAIEvaluatorConfig


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Exact model ID or snapshot to evaluate.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("tests/fixtures/openai_eval_corpus_v1.yml"),
    )
    args = parser.parse_args()
    if args.runs < 1 or args.runs > 9:
        parser.error("--runs must be between 1 and 9")

    load_project_environment(Path.cwd())
    corpus = EvaluationCorpus.model_validate(
        yaml.safe_load(args.corpus.read_text(encoding="utf-8"))
    )
    evaluator = OpenAIEvaluator(
        OpenAIEvaluatorConfig(
            type="openai",
            model=args.model,
            max_attempts=2,
            max_output_tokens=2_000,
            reasoning_effort="minimal",
        )
    )
    failed = False
    for case in corpus.cases:
        verdicts = []
        errors = []
        for _ in range(args.runs):
            try:
                verdicts.append(
                    evaluator.evaluate(
                        EvaluationBundle(
                            constraint_id=case.id,
                            rubric=corpus.rubric,
                            goal=case.goal,
                            diff=case.diff,
                            deterministic_results=[],
                            files=case.files,
                        )
                    )
                )
            except EvaluatorError as exc:
                errors.append(str(exc))
        failures = case_failures(case, verdicts)
        if errors:
            failures.append(f"{len(errors)} provider call(s) failed")
        failed = failed or bool(failures)
        print(
            json.dumps(
                {
                    "case": case.id,
                    "expected": case.expected_verdict,
                    "observed": [verdict.verdict for verdict in verdicts],
                    "failures": failures,
                    "provider_errors": errors,
                },
                sort_keys=True,
            )
        )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
