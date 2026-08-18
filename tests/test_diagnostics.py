from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import constraintloop.diagnostics as diagnostics_module
from constraintloop.diagnostics import deep_diagnostics
from constraintloop.hygiene import is_path_ignored, tracked_state_files
from constraintloop.models import Contract


def test_deep_diagnostics_covers_command_and_repository_failures(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "source").write_text("data", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        ".constraintloop/state/\nconstraintloop.local.yml\nconstraintloop.local.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "constraintloop.diagnostics.tracked_state_files",
        lambda root: [".constraintloop/state/evidence.json"],
    )
    contract = Contract.model_validate(
        {
            "constraints": {
                "bad-cwd": {
                    "kind": "command",
                    "command": ["tool"],
                    "cwd": "missing",
                    "watch": ["source"],
                },
                "bad-executable": {
                    "kind": "command",
                    "command": ["./missing-tool"],
                    "watch": ["source"],
                },
                "bad-script": {
                    "kind": "command",
                    "command": [sys.executable, "missing.py"],
                    "watch": ["source"],
                },
                "stdlib-module": {
                    "kind": "command",
                    "command": [sys.executable, "-m", "json"],
                    "watch": ["source"],
                },
                "malformed-shell": {
                    "kind": "command",
                    "command": "'unterminated",
                    "shell": True,
                    "watch": ["source"],
                },
            },
            "evaluators": {"reviewer": {"type": "openai", "model": "pinned-model"}},
        }
    )

    issues = deep_diagnostics(tmp_path, contract, {})

    assert any("OPENAI_API_KEY" in issue for issue in issues)
    assert any("cwd does not exist" in issue for issue in issues)
    assert any("executable not found" in issue for issue in issues)
    assert any("Python script not found" in issue for issue in issues)
    assert any("contains tracked files" in issue for issue in issues)
    assert not any("stdlib-module" in issue for issue in issues)


def test_hygiene_git_failures_are_safe(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", fail)

    assert not is_path_ignored(tmp_path, ".constraintloop/state/")
    assert tracked_state_files(tmp_path) == []


def test_deep_diagnostics_identifies_worktree_prerequisites(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    (tmp_path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        ".constraintloop/state/\nconstraintloop.local.yml\nconstraintloop.local.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "source").write_text("data", encoding="utf-8")
    contract = Contract.model_validate(
        {
            "constraints": {
                "tests": {
                    "kind": "command",
                    "command": [".venv/bin/pytest"],
                    "watch": ["source"],
                },
                "inventory": {
                    "kind": "ratchet",
                    "command": ["inventory"],
                    "parser": {"type": "json", "path": "count"},
                    "watch": ["source"],
                },
                "report": {"kind": "artifact", "path": "report.json", "watch": ["source"]},
                "containers": {
                    "kind": "command",
                    "command": ["docker", "compose", "config"],
                    "watch": ["source"],
                },
            }
        }
    )
    monkeypatch.setattr(diagnostics_module, "_resolve_executable", lambda cwd, executable: None)

    issues = deep_diagnostics(tmp_path, contract, {})

    assert any(".env is missing" in issue for issue in issues)
    assert any("linked worktree prerequisite" in issue for issue in issues)
    assert any("virtual environment prerequisite is missing" in issue for issue in issues)
    assert any("ratchet baseline is missing" in issue for issue in issues)
    assert any("container runtime prerequisite is unavailable" in issue for issue in issues)


def test_docker_runtime_diagnostics_cover_daemon_outcomes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        diagnostics_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("socket missing")),
    )
    assert (
        "daemon check failed"
        in diagnostics_module._docker_runtime_issues("containers", tmp_path, "docker")[0]
    )

    monkeypatch.setattr(
        diagnostics_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "daemon down\n"),
    )
    assert (
        "daemon down"
        in diagnostics_module._docker_runtime_issues("containers", tmp_path, "docker")[0]
    )

    monkeypatch.setattr(
        diagnostics_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
    )
    assert (
        "exit code 1"
        in diagnostics_module._docker_runtime_issues("containers", tmp_path, "docker")[0]
    )

    monkeypatch.setattr(
        diagnostics_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "27.0", ""),
    )
    assert diagnostics_module._docker_runtime_issues("containers", tmp_path, "docker") == []


def test_deep_diagnostics_includes_docker_daemon_result(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "source").write_text("data", encoding="utf-8")
    contract = Contract.model_validate(
        {
            "constraints": {
                "containers": {
                    "kind": "command",
                    "command": ["docker", "compose", "config"],
                    "watch": ["source"],
                }
            }
        }
    )
    monkeypatch.setattr(
        diagnostics_module, "_resolve_executable", lambda cwd, executable: Path("/bin/docker")
    )
    monkeypatch.setattr(
        diagnostics_module,
        "_docker_runtime_issues",
        lambda constraint_id, cwd, executable: ["constraint containers: daemon unavailable"],
    )

    issues = deep_diagnostics(tmp_path, contract, {})

    assert "constraint containers: daemon unavailable" in issues
