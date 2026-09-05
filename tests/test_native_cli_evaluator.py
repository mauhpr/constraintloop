from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from constraintloop.models import EvaluationBundle
from constraintloop.native_cli_evaluator import (
    _CODEX_TOOL_FEATURES,
    NativeEvaluatorError,
    evaluate_with_native_cli,
    main_for,
    probe_adapter,
    select_adapter,
    strict_output_schema,
)


def _bundle() -> EvaluationBundle:
    return EvaluationBundle(
        constraint_id="review",
        rubric="Reject boundary regressions",
        goal="Keep paths inside the project",
        diff="diff --git a/a b/a",
        deterministic_results=[],
        files={"a.py": "safe = True"},
    )


def _verdict() -> dict[str, Any]:
    return {
        "verdict": "pass",
        "score": 1,
        "rationale": "The boundary is preserved.",
        "findings": [],
    }


def test_auto_selection_prefers_calling_agent_then_falls_back() -> None:
    def available(name: str) -> str | None:
        return f"/bin/{name}" if name in {"codex", "claude"} else None

    assert (
        select_adapter(
            "auto",
            environment={"CONSTRAINTLOOP_CALLER_ADAPTER": "claude"},
            which=available,
        )
        == "claude"
    )
    assert (
        select_adapter(
            "auto",
            environment={"CONSTRAINTLOOP_CALLER_ADAPTER": "codex"},
            which=available,
        )
        == "codex"
    )

    def codex_only(name: str) -> str | None:
        return "/bin/codex" if name == "codex" else None

    assert select_adapter("auto", environment={}, which=codex_only) == "codex"


def test_selection_rejects_missing_or_unknown_adapters() -> None:
    with pytest.raises(NativeEvaluatorError, match="not installed"):
        select_adapter("claude", environment={}, which=lambda _: None)
    with pytest.raises(NativeEvaluatorError, match="Unknown"):
        select_adapter("other", environment={}, which=lambda _: None)
    with pytest.raises(NativeEvaluatorError, match="Neither"):
        select_adapter("auto", environment={}, which=lambda _: None)


def test_codex_evaluator_is_ephemeral_read_only_and_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("constraintloop.native_cli_evaluator.shutil.which", lambda _: "/bin/codex")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, kwargs=kwargs)
        schema_path = Path(command[command.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(_verdict()), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("constraintloop.native_cli_evaluator.run_bounded", fake_run)
    monkeypatch.setenv("CONSTRAINTLOOP_CALLER_ADAPTER", "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-native-agent")
    monkeypatch.setenv("UNRELATED_REPOSITORY_SECRET", "also-must-not-reach-agent")
    result = evaluate_with_native_cli(
        _bundle(), adapter="codex", timeout_seconds=30, model="codex-model"
    )

    assert result.verdict == "pass"
    command = captured["command"]
    assert command[:2] == ["/bin/codex", "exec"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--output-schema" in command
    disabled = [command[index + 1] for index, item in enumerate(command) if item == "--disable"]
    assert {"shell_tool", "unified_exec", "code_mode_host", "apps"} <= set(disabled)
    assert command[command.index("--model") + 1] == "codex-model"
    assert captured["kwargs"]["input_text"] == _bundle().model_dump_json()
    assert "CONSTRAINTLOOP_CALLER_ADAPTER" not in captured["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in captured["kwargs"]["env"]
    assert "UNRELATED_REPOSITORY_SECRET" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["cwd"] != Path.cwd()
    assert set(captured["schema"]["required"]) == {
        "verdict",
        "score",
        "rationale",
        "findings",
    }
    assert set(captured["schema"]["$defs"]["Finding"]["required"]) == {
        "message",
        "file_path",
        "line",
        "suggestion",
    }


def test_claude_evaluator_disables_tools_and_extracts_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("constraintloop.native_cli_evaluator.shutil.which", lambda _: "/bin/claude")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, kwargs=kwargs)
        stdout = json.dumps({"type": "result", "structured_output": _verdict()})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("constraintloop.native_cli_evaluator.run_bounded", fake_run)
    result = evaluate_with_native_cli(
        _bundle(), adapter="claude", timeout_seconds=45, model="claude-model"
    )

    assert result.verdict == "pass"
    command = captured["command"]
    assert command[:2] == ["/bin/claude", "-p"]
    assert "--safe-mode" in command
    assert "--bare" not in command
    assert "--disable-slash-commands" in command
    assert "--max-budget-usd" in command
    assert "--no-session-persistence" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    assert command[command.index("--max-turns") + 1] == "1"
    assert "--json-schema" in command
    assert command[command.index("--model") + 1] == "claude-model"


def test_strict_output_schema_requires_nullable_fields_and_removes_defaults() -> None:
    source = {
        "type": "object",
        "properties": {
            "optional": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            }
        },
    }

    normalized = strict_output_schema(source)

    assert normalized["required"] == ["optional"]
    assert normalized["additionalProperties"] is False
    assert "default" not in normalized["properties"]["optional"]
    assert source["properties"]["optional"]["default"] is None


def test_native_evaluator_fails_on_process_and_output_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("constraintloop.native_cli_evaluator.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "constraintloop.native_cli_evaluator.run_bounded",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 7, stdout="", stderr="authentication failed"
        ),
    )
    with pytest.raises(NativeEvaluatorError, match="authentication failed"):
        evaluate_with_native_cli(_bundle(), adapter="claude", timeout_seconds=10)

    monkeypatch.setattr(
        "constraintloop.native_cli_evaluator.run_bounded",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps({"type": "result"}), stderr=""
        ),
    )
    with pytest.raises(NativeEvaluatorError, match="without structured_output"):
        evaluate_with_native_cli(_bundle(), adapter="claude", timeout_seconds=10)


def test_native_evaluator_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("constraintloop.native_cli_evaluator.shutil.which", lambda _: "/bin/codex")

    def time_out(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("constraintloop.native_cli_evaluator.run_bounded", time_out)
    with pytest.raises(NativeEvaluatorError, match="timed out after 5s"):
        evaluate_with_native_cli(_bundle(), adapter="codex", timeout_seconds=5)


def test_claude_rejects_bad_result_envelopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("constraintloop.native_cli_evaluator.shutil.which", lambda _: "/bin/claude")
    outcomes = [
        "not json",
        json.dumps({"type": "message"}),
        json.dumps({"type": "result", "is_error": True, "subtype": "budget"}),
        json.dumps({"type": "result", "structured_output": {"verdict": "maybe"}}),
    ]

    for raw in outcomes:
        monkeypatch.setattr(
            "constraintloop.native_cli_evaluator.run_bounded",
            lambda *args, raw=raw, **kwargs: subprocess.CompletedProcess(
                args[0], 0, stdout=raw, stderr=""
            ),
        )
        with pytest.raises(NativeEvaluatorError):
            evaluate_with_native_cli(_bundle(), adapter="claude", timeout_seconds=5)


def test_probe_adapter_reports_auth_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("constraintloop.native_cli_evaluator.shutil.which", lambda _: None)
    assert probe_adapter("claude")["reason"] == "CLI is not installed"

    monkeypatch.setattr("constraintloop.native_cli_evaluator.shutil.which", lambda _: "/bin/tool")

    def authenticated(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "auth" in command:
            output = '{"loggedIn":true}'
        elif "--help" in command:
            output = (
                "--disable-slash-commands --json-schema --max-budget-usd "
                "--safe-mode --strict-mcp-config --tools"
            )
        else:
            output = "2.1.220"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr("constraintloop.native_cli_evaluator.subprocess.run", authenticated)
    status = probe_adapter("claude")
    assert status["healthy"] is True
    assert status["version"] == "2.1.220"

    def codex_ready(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "features" in command:
            output = "\n".join(f"{feature} stable true" for feature in _CODEX_TOOL_FEATURES)
        elif "login" in command:
            output = "Logged in using ChatGPT"
        else:
            output = "codex-cli 0.145.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr("constraintloop.native_cli_evaluator.subprocess.run", codex_ready)
    codex_status = probe_adapter("codex")
    assert codex_status["healthy"] is True
    assert codex_status["capabilities"] == "isolated"

    monkeypatch.setattr(
        "constraintloop.native_cli_evaluator.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="codex-cli 0.145.0", stderr=""
        ),
    )
    missing = probe_adapter("codex")
    assert missing["healthy"] is False
    assert missing["missing_capabilities"]

    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr("constraintloop.native_cli_evaluator.subprocess.run", timeout)
    assert probe_adapter("codex")["healthy"] is False


def test_native_main_doctor_and_canary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "constraintloop.native_cli_evaluator.probe_adapter",
        lambda adapter: {
            "adapter": adapter,
            "installed": True,
            "healthy": adapter == "claude",
            "reason": "ready",
        },
    )
    monkeypatch.setattr(sys, "argv", ["native", "--doctor"])
    with pytest.raises(SystemExit, match="0"):
        main_for()
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_adapter"] == "claude"

    monkeypatch.setattr("constraintloop.native_cli_evaluator.select_adapter", lambda _: "claude")
    monkeypatch.setattr(
        "constraintloop.native_cli_evaluator.evaluate_with_native_cli",
        lambda *args, **kwargs: type(
            "Verdict",
            (),
            {"verdict": "pass", "model_dump_json": lambda self: json.dumps(_verdict())},
        )(),
    )
    monkeypatch.setattr(sys, "argv", ["native", "--canary", "--adapter", "claude"])
    with pytest.raises(SystemExit, match="0"):
        main_for()
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"


def test_native_main_validates_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for arguments in (
        ["native", "--timeout-seconds", "0"],
        ["native", "--max-budget-usd", "0"],
    ):
        monkeypatch.setattr(sys, "argv", arguments)
        with pytest.raises(SystemExit, match="2"):
            main_for()
