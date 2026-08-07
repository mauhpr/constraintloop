from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from constraintloop.hooks import handle_hook
from constraintloop.setup_hooks import install_hooks, uninstall_hooks
from constraintloop.state import create_advisory_acknowledgment, load_latest_result, load_session


def _write_failing_contract(root: Path) -> None:
    payload = {
        "version": 1,
        "settings": {"max_auto_retries": 1},
        "constraints": {
            "failure": {
                "kind": "command",
                "command": [sys.executable, "-c", "raise SystemExit(1)"],
                "phases": ["stop"],
                "watch": ["source.txt"],
            }
        },
    }
    (root / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    (root / "source.txt").write_text("same", encoding="utf-8")


def test_stop_blocks_then_requires_human(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_failing_contract(tmp_path)
    payload = {"session_id": "s1"}

    first = handle_hook(tmp_path, "claude", "stop", payload)
    second = handle_hook(tmp_path, "claude", "stop", payload)
    assert first["decision"] == "block"
    assert second["continue"] is False
    assert "ask a human" in second["stopReason"]


def test_pre_tool_protects_contract(tmp_path: Path) -> None:
    response = handle_hook(
        tmp_path,
        "codex",
        "pre-tool",
        {"tool_input": {"patch": "*** Update File: constraintloop.yml"}},
    )
    output = response["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert output["hookEventName"] == "PreToolUse"


def test_pre_tool_protects_local_secrets(tmp_path: Path) -> None:
    response = handle_hook(
        tmp_path,
        "codex",
        "pre-tool",
        {"tool_input": {"command": "*** Update File: .constraintloop/secrets.env"}},
    )
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_setup_preserves_existing_hooks_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/constraintloop"])
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]},
                "permissions": {"allow": ["Read"]},
            }
        ),
        encoding="utf-8",
    )
    install_hooks(tmp_path, "claude")
    install_hooks(tmp_path, "claude")
    data = json.loads(path.read_text(encoding="utf-8"))
    commands = [hook["command"] for group in data["hooks"]["Stop"] for hook in group["hooks"]]
    assert commands.count("existing-hook") == 1
    assert sum("constraintloop hook" in command for command in commands) == 1
    assert data["permissions"] == {"allow": ["Read"]}


def test_setup_uses_resolved_project_local_executable(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / ".venv" / "bin" / "constraintloop"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(sys, "argv", [str(executable)])
    path = install_hooks(tmp_path, "codex")
    data = json.loads(path.read_text(encoding="utf-8"))
    command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert command.startswith('"$(git rev-parse --show-toplevel)/.venv/bin/constraintloop"')
    assert '--project "$(git rev-parse --show-toplevel)"' in command
    assert str(tmp_path) not in command


def test_setup_preserves_selected_project_inside_monorepo(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    project = tmp_path / "packages" / "atool"
    executable = project / ".venv" / "bin" / "constraintloop"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(sys, "argv", [str(executable)])

    path = install_hooks(project, "codex")
    command = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0][
        "command"
    ]

    assert '"$(git rev-parse --show-toplevel)/packages/atool/.venv/bin/constraintloop"' in command
    assert '--project "$(git rev-parse --show-toplevel)/packages/atool"' in command


def test_setup_pins_uvx_and_supports_explicit_hook_executable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [str(tmp_path / ".cache" / "uv" / "archive-v0" / "abc" / "bin" / "constraintloop")],
    )
    path = install_hooks(tmp_path, "codex")
    command = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0][
        "command"
    ]
    assert command.startswith("uvx --from constraintloop==0.2.0 constraintloop hook")

    path = install_hooks(tmp_path, "codex", hook_executable="pipx run constraintloop")
    command = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0][
        "command"
    ]
    assert command.startswith("pipx run constraintloop hook")


def test_setup_migrates_unbound_hook_to_project_bound_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/constraintloop"])
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "/usr/local/bin/constraintloop hook --adapter codex "
                                        "--event stop"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    install_hooks(tmp_path, "codex")

    commands = [
        hook["command"]
        for group in json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    assert commands == [
        "constraintloop hook --adapter codex --event stop "
        '--project "$(git rev-parse --show-toplevel)"'
    ]


def test_hook_lifecycle_context_and_gemini_responses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_failing_contract(tmp_path)
    prompt = handle_hook(
        tmp_path, "gemini", "user-prompt", {"sessionId": "g", "input": "  fix it  "}
    )
    assert prompt["hookSpecificOutput"]["hookEventName"] == "BeforeAgent"
    session = handle_hook(tmp_path, "gemini", "session-start", {"sessionId": "g"})
    assert "failure" in session["hookSpecificOutput"]["additionalContext"]
    compact = handle_hook(tmp_path, "codex", "pre-compact", {})
    assert "systemMessage" in compact
    protected_command = "constraintloop " + "waive x"
    denied = handle_hook(
        tmp_path, "gemini", "pre-tool", {"toolInput": {"command": protected_command}}
    )
    assert denied["decision"] == "deny"
    stopped = handle_hook(tmp_path, "gemini", "stop", {"sessionId": "g"})
    assert stopped["decision"] == "deny"


def test_missing_contract_blocks_stop_but_contextualizes_other_events(tmp_path: Path) -> None:
    assert handle_hook(tmp_path, "codex", "stop", {})["decision"] == "block"
    response = handle_hook(tmp_path, "codex", "session-start", {})
    assert "No ConstraintLoop contract" in response["hookSpecificOutput"]["additionalContext"]


def test_advisory_failure_is_delivered_once_then_allows_completion(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    evaluator = [
        sys.executable,
        "-c",
        "import json; print(json.dumps("
        "{'verdict':'fail','score':0,'rationale':'public API is undefined','findings':[]}))",
    ]
    payload = {
        "version": 1,
        "constraints": {
            "review": {
                "kind": "rubric",
                "enforcement": "advisory",
                "evaluator": "reviewer",
                "rubric": "Review the public API",
                "phases": ["stop"],
                "watch": ["source.py"],
            }
        },
        "evaluators": {"reviewer": {"type": "command", "command": evaluator}},
    }
    config_name = "constraintloop" + ".yml"
    (tmp_path / config_name).write_text(yaml.safe_dump(payload), encoding="utf-8")
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    hook_payload = {"session_id": "advisory"}

    first = handle_hook(tmp_path, "codex", "stop", hook_payload)
    second = handle_hook(tmp_path, "codex", "stop", hook_payload)

    assert first["decision"] == "block"
    assert "PASS WITH ADVISORIES" in first["reason"]
    assert "public API is undefined" in first["reason"]
    assert "agent disposition" in first["reason"]
    assert second["decision"] == "block"
    assert "no snapshot-bound disposition" in second["reason"]

    evidence = load_latest_result(tmp_path, "review")
    assert evidence is not None
    create_advisory_acknowledgment(tmp_path, evidence, "No change: API is intentionally internal")
    third = handle_hook(tmp_path, "codex", "stop", hook_payload)
    assert third["continue"] is True
    assert "explicitly acknowledged" in third["systemMessage"]
    assert "intentionally internal" in third["systemMessage"]


def test_synthetic_hook_feedback_does_not_replace_user_goal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    handle_hook(
        tmp_path,
        "codex",
        "user-prompt",
        {"session_id": "goal", "prompt": "Implement the requested feature"},
    )
    feedback = '<hook_prompt hook_run_id="stop:1">Fix the finding</hook_prompt>'
    response = handle_hook(
        tmp_path,
        "codex",
        "user-prompt",
        {"session_id": "goal", "prompt": feedback},
    )
    state = load_session(tmp_path, "goal")
    assert state["goal"] == "Implement the requested feature"
    assert state["last_hook_feedback"] == feedback
    assert "evaluator feedback" in response["hookSpecificOutput"]["additionalContext"]


def test_pre_tool_allows_reads_and_safe_patch_context_but_denies_mutation(tmp_path: Path) -> None:
    policy_name = "constraintloop" + ".yml"
    harmless = handle_hook(
        tmp_path,
        "codex",
        "pre-tool",
        {"tool_name": "exec", "tool_input": {"cmd": f"sed -n 1,10p {policy_name}"}},
    )
    assert harmless == {}

    patch_text = "*** Update File: src/example.py\n-old = " + repr(policy_name) + "\n+new = True"
    safe_patch = handle_hook(
        tmp_path,
        "codex",
        "pre-tool",
        {"tool_name": "apply_patch", "tool_input": {"patch": patch_text}},
    )
    assert safe_patch == {}

    denied = handle_hook(
        tmp_path,
        "codex",
        "pre-tool",
        {"tool_name": "exec", "tool_input": {"cmd": f"rm {policy_name}"}},
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_setup_refuses_invalid_json_and_supports_module_invocation(
    tmp_path: Path, monkeypatch
) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        install_hooks(tmp_path, "claude")
    assert settings.read_text(encoding="utf-8") == "{"

    settings.unlink()
    module_path = tmp_path / "__main__.py"
    monkeypatch.setattr(sys, "argv", [str(module_path)])
    path = install_hooks(tmp_path, "claude")
    command = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0][
        "command"
    ]
    assert "python -m constraintloop" in command
    assert str(tmp_path) not in command


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
def test_setup_never_embeds_project_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    executable = tmp_path / ".venv" / "bin" / "constraintloop"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(sys, "argv", [str(executable)])

    path = install_hooks(tmp_path, adapter)

    contents = path.read_text(encoding="utf-8")
    assert str(tmp_path) not in contents
    assert '--project \\"$(git rev-parse --show-toplevel)\\"' in contents


@pytest.mark.parametrize(
    "stop_value",
    [
        {},
        [{"hooks": "not-a-list"}],
        [{"hooks": ["not-an-object"]}],
    ],
)
def test_setup_refuses_malformed_event_hooks_without_modifying_settings(
    tmp_path: Path, monkeypatch, stop_value: object
) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/constraintloop"])
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"hooks": {"Stop": stop_value}}, separators=(",", ":"))
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="Existing Stop"):
        install_hooks(tmp_path, "codex")

    assert path.read_text(encoding="utf-8") == original


def test_uninstall_removes_only_constraintloop_hooks_and_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/constraintloop"])
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "third-party check"}]}]
                },
                "permissions": {"allow": ["Read"]},
            }
        ),
        encoding="utf-8",
    )
    install_hooks(tmp_path, "claude")

    result_path, removed = uninstall_hooks(tmp_path, "claude")
    assert result_path == path
    assert removed == 6
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Read"]}
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "third-party check"
    assert uninstall_hooks(tmp_path, "claude")[1] == 0


def test_uninstall_handles_missing_and_rejects_invalid_settings(tmp_path: Path) -> None:
    assert uninstall_hooks(tmp_path, "claude")[1] == 0
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to modify invalid"):
        uninstall_hooks(tmp_path, "claude")

    path.write_text('{"hooks":"invalid"}', encoding="utf-8")
    assert uninstall_hooks(tmp_path, "claude")[1] == 0
