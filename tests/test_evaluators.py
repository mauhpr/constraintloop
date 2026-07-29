from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from constraintloop.engine import ConstraintEngine
from constraintloop.evaluators import (
    AnthropicEvaluator,
    CommandEvaluator,
    EvaluatorError,
    EvaluatorTerminalError,
    OpenAIEvaluator,
    _parse_verdict,
    build_evaluator,
)
from constraintloop.models import (
    AnthropicEvaluatorConfig,
    CommandEvaluatorConfig,
    Contract,
    EvaluationBundle,
    EvaluatorVerdict,
    OpenAIEvaluatorConfig,
    Phase,
)


def _bundle() -> EvaluationBundle:
    return EvaluationBundle(
        constraint_id="review",
        rubric="Be correct",
        diff="",
        deterministic_results=[],
        files={},
    )


def test_command_evaluator_success_and_fenced_json(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        'print(\'```json\\n{"verdict":"pass","rationale":"ok","findings":[]}\\n```\')',
    ]
    evaluator = build_evaluator(
        CommandEvaluatorConfig(type="command", command=command), cwd=tmp_path
    )
    assert evaluator.evaluate(_bundle()).verdict == "pass"


def test_command_evaluator_receives_agent_preference(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "import json, os; print(json.dumps({"
        "'verdict':'pass','rationale':os.environ['CONSTRAINTLOOP_CALLER_ADAPTER'],"
        "'findings':[]}))",
    ]
    evaluator = build_evaluator(
        CommandEvaluatorConfig(type="command", command=command),
        cwd=tmp_path,
        environment={"CONSTRAINTLOOP_CALLER_ADAPTER": "claude"},
    )
    assert evaluator.evaluate(_bundle()).rationale == "claude"


@pytest.mark.parametrize(
    ("command", "match"),
    [
        (["definitely-not-a-real-evaluator"], "could not start"),
        ([sys.executable, "-c", "raise SystemExit(3)"], "exited 3"),
    ],
)
def test_command_evaluator_process_errors(command: list[str], match: str) -> None:
    with pytest.raises(EvaluatorError, match=match):
        CommandEvaluator(CommandEvaluatorConfig(type="command", command=command)).evaluate(
            _bundle()
        )


def test_command_evaluator_redacts_scoped_secrets_from_failures() -> None:
    secret = "project-only-secret"
    evaluator = CommandEvaluator(
        CommandEvaluatorConfig(
            type="command",
            command=[
                sys.executable,
                "-c",
                "import os, sys; print(os.environ['PROJECT_TOKEN'], file=sys.stderr); "
                "raise SystemExit(2)",
            ],
        ),
        environment={"PROJECT_TOKEN": secret},
    )

    with pytest.raises(EvaluatorError) as exc_info:
        evaluator.evaluate(_bundle())

    assert secret not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_command_evaluator_timeout_and_invalid_schema() -> None:
    config = CommandEvaluatorConfig(
        type="command",
        command=[sys.executable, "-c", "import time; time.sleep(1)"],
        timeout_seconds=0.01,
    )
    with pytest.raises(EvaluatorError, match="timed out"):
        CommandEvaluator(config).evaluate(_bundle())
    with pytest.raises(EvaluatorError, match="invalid structured JSON"):
        _parse_verdict('{"verdict":"maybe"}')


@pytest.mark.parametrize(
    "config",
    [
        OpenAIEvaluatorConfig(type="openai", model="test"),
        AnthropicEvaluatorConfig(type="anthropic", model="test"),
    ],
)
def test_remote_evaluators_require_keys(config: object, monkeypatch) -> None:
    key = config.api_key_env  # type: ignore[attr-defined]
    monkeypatch.delenv(key, raising=False)
    evaluator = build_evaluator(config)  # type: ignore[arg-type]
    with pytest.raises(EvaluatorError, match="Missing API key"):
        evaluator.evaluate(_bundle())


def _install_openai_fake(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "clients": []}

    class Responses:
        def parse(self, **kwargs: Any) -> Any:
            state["calls"].append(kwargs)
            outcome = outcomes[len(state["calls"]) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    class FakeOpenAI:
        def __init__(self, **kwargs: Any):
            state["clients"].append(kwargs)
            self.responses = Responses()

    fake_module = ModuleType("openai")
    fake_module.OpenAI = FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("constraintloop.evaluators.time.sleep", lambda _: None)
    return state


def _openai_response(**updates: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": "resp_test",
        "model": "model-snapshot",
        "status": "completed",
        "incomplete_details": None,
        "output": [],
        "output_parsed": EvaluatorVerdict(
            verdict="pass", score=1, rationale="correct", findings=[]
        ),
        "usage": SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_openai_request_shape_and_safe_metadata(monkeypatch) -> None:
    state = _install_openai_fake(monkeypatch, [_openai_response()])
    config = OpenAIEvaluatorConfig(
        type="openai",
        model="requested-model",
        max_attempts=2,
        max_output_tokens=777,
        reasoning_effort="low",
        timeout_seconds=12,
    )
    evaluator = OpenAIEvaluator(config)

    assert evaluator.evaluate(_bundle()).verdict == "pass"
    assert state["clients"] == [{"api_key": "test-key", "timeout": 12.0}]
    assert len(state["calls"]) == 1
    request = state["calls"][0]
    assert request["model"] == "requested-model"
    assert request["input"] == _bundle().model_dump_json()
    assert request["max_output_tokens"] == 777
    assert request["reasoning"] == {"effort": "low"}
    assert request["text_format"] is EvaluatorVerdict
    assert "independent software quality evaluator" in request["instructions"]

    metadata = evaluator.last_metadata
    assert metadata is not None
    assert metadata.model == "model-snapshot"
    assert metadata.response_id == "resp_test"
    assert metadata.total_tokens == 120
    assert "test-key" not in metadata.model_dump_json()


def test_openai_accepts_evaluator_scoped_api_key(monkeypatch) -> None:
    state = _install_openai_fake(monkeypatch, [_openai_response()])
    monkeypatch.delenv("OPENAI_API_KEY")
    evaluator = OpenAIEvaluator(
        OpenAIEvaluatorConfig(type="openai", model="test"),
        environment={"OPENAI_API_KEY": "project-scoped-key"},
    )

    assert evaluator.evaluate(_bundle()).verdict == "pass"
    assert state["clients"] == [{"api_key": "project-scoped-key", "timeout": 60.0}]
    assert "OPENAI_API_KEY" not in os.environ


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            _openai_response(
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                output_parsed=None,
            ),
            "incomplete: max_output_tokens",
        ),
        (
            _openai_response(
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="refusal", refusal="policy refusal")],
                    )
                ],
                output_parsed=None,
            ),
            "refused structured evaluation",
        ),
    ],
)
def test_openai_terminal_responses_are_not_retried(
    monkeypatch, response: SimpleNamespace, message: str
) -> None:
    state = _install_openai_fake(monkeypatch, [response])
    evaluator = OpenAIEvaluator(OpenAIEvaluatorConfig(type="openai", model="test", max_attempts=3))
    with pytest.raises(EvaluatorTerminalError, match=message):
        evaluator.evaluate(_bundle())
    assert len(state["calls"]) == 1


def test_openai_retries_transient_errors_and_records_attempts(monkeypatch) -> None:
    state = _install_openai_fake(monkeypatch, [RuntimeError("rate limit"), _openai_response()])
    evaluator = OpenAIEvaluator(OpenAIEvaluatorConfig(type="openai", model="test", max_attempts=3))
    assert evaluator.evaluate(_bundle()).verdict == "pass"
    assert len(state["calls"]) == 2
    assert evaluator.last_metadata is not None
    assert evaluator.last_metadata.attempts == 2


def test_openai_retry_exhaustion_fails_closed(monkeypatch) -> None:
    state = _install_openai_fake(
        monkeypatch, [TimeoutError("provider timeout"), TimeoutError("provider timeout")]
    )
    evaluator = OpenAIEvaluator(OpenAIEvaluatorConfig(type="openai", model="test", max_attempts=2))
    with pytest.raises(EvaluatorError, match="provider timeout"):
        evaluator.evaluate(_bundle())
    assert len(state["calls"]) == 2
    assert evaluator.last_metadata is not None
    assert evaluator.last_metadata.status == "error"
    assert evaluator.last_metadata.attempts == 2


def test_engine_persists_safe_openai_call_metadata(tmp_path: Path, monkeypatch) -> None:
    _install_openai_fake(monkeypatch, [_openai_response()])
    contract = Contract.model_validate(
        {
            "constraints": {
                "review": {
                    "kind": "rubric",
                    "enforcement": "advisory",
                    "evaluator": "openai",
                    "rubric": "Check the patch",
                    "runs": 1,
                    "phases": ["stop"],
                }
            },
            "evaluators": {
                "openai": {
                    "type": "openai",
                    "model": "requested-model",
                    "max_attempts": 1,
                }
            },
        }
    )
    record = ConstraintEngine(tmp_path, contract, use_cache=False).run(Phase.STOP)
    call = record.results[0].evaluator_calls[0]
    assert call.response_id == "resp_test"
    assert call.input_tokens == 100
    assert "test-key" not in record.model_dump_json()


def test_command_evaluator_accepts_metadata_envelope(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "result": {
            "verdict": "pass",
            "rationale": "native review passed",
            "findings": [],
        },
        "metadata": {
            "provider": "claude-code",
            "model": "default",
            "status": "completed",
            "attempts": 1,
            "cli_version": "2.1.220",
            "cost_usd": 0.01,
            "duration_ms": 10,
        },
    }
    command = [sys.executable, "-c", f"print({json.dumps(json.dumps(payload))})"]
    evaluator = CommandEvaluator(CommandEvaluatorConfig(type="command", command=command))
    assert evaluator.evaluate(_bundle()).verdict == "pass"
    assert evaluator.last_metadata is not None
    assert evaluator.last_metadata.cost_usd == 0.01


def _install_anthropic_fake(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "clients": []}

    class Messages:
        def create(self, **kwargs: Any) -> Any:
            state["calls"].append(kwargs)
            outcome = outcomes[len(state["calls"]) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    class FakeAnthropic:
        def __init__(self, **kwargs: Any):
            state["clients"].append(kwargs)
            self.messages = Messages()

    module = ModuleType("anthropic")
    module.Anthropic = FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setattr("constraintloop.evaluators.time.sleep", lambda _: None)
    return state


def _anthropic_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="msg_test",
        model="claude-test",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=30, output_tokens=10),
    )


def test_anthropic_request_retry_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _anthropic_message(
        json.dumps({"verdict": "pass", "rationale": "correct", "findings": []})
    )
    state = _install_anthropic_fake(monkeypatch, [RuntimeError("rate limit"), response])
    evaluator = AnthropicEvaluator(
        AnthropicEvaluatorConfig(
            type="anthropic",
            model="requested",
            max_attempts=2,
            max_output_tokens=777,
            timeout_seconds=12,
        )
    )
    assert evaluator.evaluate(_bundle()).verdict == "pass"
    assert state["clients"] == [{"api_key": "anthropic-test-key", "timeout": 12.0}]
    assert state["calls"][1]["max_tokens"] == 777
    assert "untrusted evidence" in state["calls"][1]["system"]
    assert evaluator.last_metadata is not None
    assert evaluator.last_metadata.total_tokens == 40
    assert evaluator.last_metadata.attempts == 2


def test_anthropic_accepts_evaluator_scoped_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _anthropic_message(
        json.dumps({"verdict": "pass", "rationale": "correct", "findings": []})
    )
    state = _install_anthropic_fake(monkeypatch, [response])
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    evaluator = AnthropicEvaluator(
        AnthropicEvaluatorConfig(type="anthropic", model="test"),
        environment={"ANTHROPIC_API_KEY": "project-scoped-key"},
    )

    assert evaluator.evaluate(_bundle()).verdict == "pass"
    assert state["clients"] == [{"api_key": "project-scoped-key", "timeout": 60.0}]
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_anthropic_empty_and_exhausted_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_anthropic_fake(monkeypatch, [_anthropic_message("")])
    evaluator = AnthropicEvaluator(
        AnthropicEvaluatorConfig(type="anthropic", model="test", max_attempts=3)
    )
    with pytest.raises(EvaluatorTerminalError, match="no text"):
        evaluator.evaluate(_bundle())

    _install_anthropic_fake(monkeypatch, [TimeoutError("down"), TimeoutError("down")])
    evaluator = AnthropicEvaluator(
        AnthropicEvaluatorConfig(type="anthropic", model="test", max_attempts=2)
    )
    with pytest.raises(EvaluatorError, match="down"):
        evaluator.evaluate(_bundle())
    assert evaluator.last_metadata is not None
    assert evaluator.last_metadata.attempts == 2
