from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from constraintloop.checkout import checkout_context
from constraintloop.cli import main
from constraintloop.config import contract_digest
from constraintloop.digest import constraint_input_digest
from constraintloop.engine import ConstraintEngine
from constraintloop.hooks import handle_hook
from constraintloop.loops import journal_path, loop_lease, run_cycle, show_challenge, supervise
from constraintloop.models import ChallengeConfig, Contract, LoopState, Phase, Verdict
from constraintloop.setup_hooks import install_hooks
from constraintloop.state import (
    advisory_acknowledgment_reason,
    cache_root,
    create_advisory_acknowledgment,
    create_waiver,
    load_latest_result,
    load_session,
    save_session,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CONSTRAINTLOOP_CACHE_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (root / "source").write_text("same\n")
    git(root, "add", "source")
    git(root, "commit", "-qm", "initial")
    return root


def contract(*, failure: bool = False) -> Contract:
    return Contract.model_validate(
        {
            "settings": {"max_auto_retries": 1},
            "constraints": {
                "check": {
                    "kind": "command",
                    "command": [sys.executable, "-c", f"raise SystemExit({int(failure)})"],
                    "watch": ["source"],
                    "phases": ["stop", "ci"],
                }
            },
            "loops": {
                "completion": {
                    "phase": "stop",
                    "interval_seconds": 10,
                    "max_repair_attempts": 3,
                    "max_unchanged_repairs": 2,
                    "max_duration_seconds": 100,
                }
            },
        }
    )


@pytest.mark.parametrize("shared_cache", [False, True])
@pytest.mark.parametrize("detached", [False, True])
def test_worktrees_isolate_all_state(
    repository: Path, monkeypatch: pytest.MonkeyPatch, shared_cache: bool, detached: bool
) -> None:
    if shared_cache:
        monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(repository.parent / "cache"))
    linked = repository.parent / "linked"
    options = ["--detach"] if detached else ["-b", "feature"]
    git(repository, "worktree", "add", *options, str(linked), "HEAD")
    policy = contract(failure=True)
    first = ConstraintEngine(repository, policy).run(Phase.STOP).results[0]
    save_session(repository, "same-session", {"goal": "main task", "attempts": 4})
    create_waiver(repository, first, contract_digest(policy), "local exception")
    create_advisory_acknowledgment(repository, first, "main disposition")
    run_cycle(repository, policy, "completion")

    assert cache_root(repository) != cache_root(linked)
    assert load_latest_result(linked, "check") is None
    assert load_session(linked, "same-session") == {}
    assert advisory_acknowledgment_reason(linked, first) is None
    assert not journal_path(linked, "completion").exists()
    with (
        loop_lease(repository, "completion", ttl_seconds=60),
        loop_lease(linked, "completion", ttl_seconds=60),
    ):
        other = ConstraintEngine(linked, policy).run(Phase.STOP).results[0]
    assert other.verdict == Verdict.FAIL
    assert not other.cached
    assert other.input_digest != first.input_digest
    assert ConstraintEngine(repository, policy).run(Phase.STOP).results[0].verdict == Verdict.WAIVED
    assert ConstraintEngine(linked, policy).run(Phase.STOP).results[0].cached


def test_branch_switch_isolates_evidence_goals_and_budgets(repository: Path) -> None:
    policy = contract(failure=True)
    # Exercise ordinary Stop retries separately from the loop's repair budget.
    hook_policy = policy.model_dump(mode="json", exclude={"loops"})
    (repository / "constraintloop.yml").write_text(yaml.safe_dump(hook_policy))
    payload = {"session_id": "session"}
    handle_hook(repository, "claude", "user-prompt", {**payload, "prompt": "main task"})
    assert handle_hook(repository, "claude", "stop", payload)["decision"] == "block"
    assert handle_hook(repository, "claude", "stop", payload)["continue"] is False
    first = run_cycle(repository, policy, "completion", now=100)
    second = run_cycle(repository, policy, "completion", now=101)
    assert second.repair_attempt > first.repair_attempt
    original_state = cache_root(repository)
    original_head = checkout_context(repository).head

    git(repository, "checkout", "-qb", "other")
    assert checkout_context(repository).head == original_head
    assert cache_root(repository) != original_state
    assert load_latest_result(repository, "check") is None
    assert load_session(repository, "session") == {}
    assert handle_hook(repository, "claude", "stop", payload)["decision"] == "block"
    other = run_cycle(repository, policy, "completion", now=102)
    assert other.observation == 1
    assert other.repair_attempt == 0

    git(repository, "checkout", "-q", "main")
    assert cache_root(repository) == original_state
    assert load_session(repository, "session")["goal"] == "main task"
    assert handle_hook(repository, "claude", "stop", payload)["continue"] is False
    assert run_cycle(repository, policy, "completion", now=103).repair_attempt > 0


@pytest.mark.parametrize("failure", [False, True])
def test_commit_invalidates_evidence_without_resetting_budgets(
    repository: Path, failure: bool
) -> None:
    policy = contract(failure=failure)
    first = ConstraintEngine(repository, policy).run(Phase.STOP).results[0]
    if failure:
        create_waiver(repository, first, contract_digest(policy), "old HEAD only")
    save_session(repository, "session", {"attempts": 2})
    original_state = cache_root(repository)
    git(repository, "commit", "--allow-empty", "-qm", "new HEAD, same watched bytes")

    assert cache_root(repository) == original_state
    assert load_session(repository, "session")["attempts"] == 2
    # Write the same policy for the CLI's freshness diagnostics.
    (repository / "constraintloop.yml").write_text(yaml.safe_dump(policy.model_dump(mode="json")))
    status = CliRunner().invoke(main, ["status", "--project", str(repository)])
    assert status.exit_code == 0, status.output
    assert "STALE check" in status.output
    second = ConstraintEngine(repository, policy).run(Phase.STOP).results[0]
    assert not second.cached
    assert second.input_digest != first.input_digest
    assert second.verdict == first.verdict
    assert ConstraintEngine(repository, policy).run(Phase.STOP).results[0].cached


def test_detached_and_unborn_checkout_identity(repository: Path) -> None:
    policy = contract()
    branch = checkout_context(repository)
    initial = constraint_input_digest(repository, "check", policy.constraints["check"])
    git(repository, "checkout", "--detach", "-q")
    detached = checkout_context(repository)
    assert detached.branch is None
    assert detached.head == branch.head
    assert detached.state_key() != branch.state_key()
    assert constraint_input_digest(repository, "check", policy.constraints["check"]) != initial
    git(repository, "commit", "--allow-empty", "-qm", "detached commit")
    assert checkout_context(repository).state_key() != detached.state_key()

    unborn = repository.parent / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-q", "-b", "main")
    context = checkout_context(unborn)
    assert context.head is None
    assert context.branch == "refs/heads/main"
    assert context.state_key() is not None


def test_git_environment_cannot_redirect_worktree_scope(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = repository.parent / "linked"
    git(repository, "worktree", "add", "--detach", str(linked), "HEAD")
    expected = checkout_context(linked)
    monkeypatch.setenv("GIT_DIR", str(repository / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repository))
    assert checkout_context(linked) == expected
    alias = repository.parent / "alias"
    alias.symlink_to(linked, target_is_directory=True)
    assert cache_root(alias) == cache_root(linked)


def test_checkout_change_during_execution_fails_closed(repository: Path) -> None:
    policy = Contract.model_validate(
        {
            "constraints": {
                "switch": {
                    "kind": "command",
                    "command": ["git", "checkout", "-qb", "switched"],
                    "watch": ["source"],
                }
            }
        }
    )
    (repository / "constraintloop.yml").write_text(yaml.safe_dump(policy.model_dump(mode="json")))
    response = handle_hook(repository, "claude", "stop", {"session_id": "switch"})
    assert response["continue"] is False
    assert "checkout changed" in response["stopReason"]
    assert load_latest_result(repository, "switch") is None
    git(repository, "checkout", "-q", "main")
    assert load_latest_result(repository, "switch") is None
    git(repository, "branch", "-D", "switched")
    result = CliRunner().invoke(main, ["run", "--project", str(repository)])
    assert result.exit_code == 1
    assert "checkout changed" in result.output


def test_doctor_explain_and_copied_hooks_target_nested_linked_project(repository: Path) -> None:
    project = repository / "packages" / "atool"
    project.mkdir(parents=True)
    policy = contract()
    (project / "constraintloop.yml").write_text(yaml.safe_dump(policy.model_dump(mode="json")))
    git(repository, "add", "packages")
    git(repository, "commit", "-qm", "nested contract")
    hook_path = install_hooks(
        project, "claude", hook_executable=shlex.join([sys.executable, "-m", "constraintloop"])
    )
    command = json.loads(hook_path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    linked = repository.parent / "linked"
    git(repository, "worktree", "add", "--detach", str(linked), "HEAD")
    linked_project = linked / "packages" / "atool"
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    hook = subprocess.run(
        command,
        shell=True,
        cwd=linked_project,
        env=environment,
        input=json.dumps({"session_id": "linked"}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(hook.stdout).get("continue") is not False
    assert load_latest_result(linked_project, "check") is not None
    assert load_latest_result(project, "check") is None

    runner = CliRunner()
    doctor = runner.invoke(main, ["doctor", "--project", str(linked_project)])
    assert doctor.exit_code == 0, doctor.output
    assert f"worktree: {linked}" in doctor.output
    assert "branch: (detached HEAD)" in doctor.output
    assert str(cache_root(linked_project)) in doctor.output
    explain = runner.invoke(main, ["explain", "--json", "--project", str(linked_project)])
    assert explain.exit_code == 0, explain.output
    scope = json.loads(explain.output)["scope"]
    assert scope["project_root"] == str(linked_project)
    assert scope["worktree_root"] == str(linked)
    assert scope["branch"] is None
    assert scope["state_directory"] == str(cache_root(linked_project))


@pytest.mark.parametrize("error", [OSError("missing git"), subprocess.TimeoutExpired("git", 5)])
def test_unreadable_checkout_is_not_treated_as_non_git(
    repository: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("constraintloop.checkout.subprocess.run", fail)
    with pytest.raises(ValueError, match="Could not inspect Git checkout"):
        checkout_context(repository)


def test_broken_git_marker_fails_closed(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: missing\n")
    with pytest.raises(ValueError, match="Could not inspect Git checkout"):
        checkout_context(tmp_path)


def test_non_git_projects_keep_local_state(tmp_path: Path) -> None:
    assert checkout_context(tmp_path).git_dir is None
    assert cache_root(tmp_path) == tmp_path / ".constraintloop" / "state"


def test_unscoped_legacy_evidence_is_not_imported(repository: Path) -> None:
    policy = contract()
    result = ConstraintEngine(repository, policy, use_cache=False).run(Phase.STOP).results[0]
    legacy = repository / ".constraintloop" / "state"
    legacy.mkdir(parents=True)
    evidence = legacy / "evidence.json"
    evidence.write_text(json.dumps({"check": result.model_dump(mode="json")}))

    assert load_latest_result(repository, "check") is None
    assert not ConstraintEngine(repository, policy).run(Phase.STOP).results[0].cached
    assert evidence.is_file()


def test_supervisor_does_not_follow_branch_switch_and_releases_original_lease(
    repository: Path,
) -> None:
    policy = contract()
    policy.constraints["check"] = policy.constraints["check"].model_copy(
        update={"command": [sys.executable, "-c", "raise SystemExit(75)"]}
    )
    policy.loops["completion"].interval_seconds = 0.001
    runner = supervise(repository, policy, "completion")
    try:
        assert next(runner).state == LoopState.WAITING
        git(repository, "checkout", "-qb", "other")
        with pytest.raises(ValueError, match="checkout changed"):
            next(runner)
        assert not journal_path(repository, "completion").exists()
        git(repository, "checkout", "-q", "main")
        with loop_lease(repository, "completion", ttl_seconds=60):
            pass
    finally:
        runner.close()


def test_challenge_only_loop_refreshes_request_on_new_head(repository: Path) -> None:
    policy = contract()
    policy.constraints = {}
    policy.loops["completion"].challenge = ChallengeConfig(count=1, watch=["source"])
    assert run_cycle(repository, policy, "completion").state == LoopState.CHALLENGE
    first = show_challenge(repository, policy, "completion")["request"]
    git(repository, "commit", "--allow-empty", "-qm", "new HEAD")
    assert run_cycle(repository, policy, "completion").state == LoopState.CHALLENGE
    second = show_challenge(repository, policy, "completion")["request"]
    assert second["request_id"] != first["request_id"]
    assert second["input_snapshot"] != first["input_snapshot"]
