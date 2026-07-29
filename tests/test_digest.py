from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from constraintloop.config import ContractError, discover_project_root, load_contract
from constraintloop.digest import (
    changed_files,
    constraint_input_digest,
    git_diff,
    matching_files,
    project_key,
    redact_text,
)
from constraintloop.models import CommandConstraint


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_matching_files_digest_and_project_discovery(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "a.py"
    source.write_text("one", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("ignored", encoding="utf-8")
    outside = tmp_path.parent / "outside-constraintloop-test"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "src" / "escape.py"
    link.symlink_to(outside)

    assert [path.name for path in matching_files(tmp_path, ["**/*.py"])] == ["a.py"]
    spec = CommandConstraint(
        kind="command", command=[sys.executable, "-c", "pass"], watch=["src/*"]
    )
    first = constraint_input_digest(tmp_path, "check", spec)
    source.write_text("two", encoding="utf-8")
    assert constraint_input_digest(tmp_path, "check", spec) != first
    assert project_key(tmp_path) == project_key(tmp_path)

    contract_name = "constraintloop" + ".yml"
    (tmp_path / contract_name).write_text("version: 1\nconstraints: {}\n", encoding="utf-8")
    nested = tmp_path / "src" / "nested"
    nested.mkdir()
    assert discover_project_root(nested) == tmp_path
    contract, path = load_contract(tmp_path)
    assert contract.constraints == {}
    assert path.name == contract_name
    outside.unlink()


def test_git_change_detection_untracked_rename_and_bounded_diff(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "old.txt"
    tracked.write_text("old\n", encoding="utf-8")
    _git(tmp_path, "add", "old.txt")
    _git(tmp_path, "commit", "-qm", "initial")

    tracked.rename(tmp_path / "new.txt")
    (tmp_path / "untracked.txt").write_text("x" * 200, encoding="utf-8")
    _git(tmp_path, "add", "-A", "--", "old.txt", "new.txt")
    changes = changed_files(tmp_path)
    assert "old.txt" in changes
    assert "new.txt" in changes
    assert "untracked.txt" in changes
    assert "untracked.txt" not in changed_files(tmp_path, include_untracked=False)
    assert "[diff truncated]" in git_diff(tmp_path, limit=20)


def test_git_diff_omits_rename_when_either_path_is_not_disclosable(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    secret = tmp_path / "credentials.json"
    secret.write_text('{"token":"do-not-disclose"}\n', encoding="utf-8")
    _git(tmp_path, "add", "credentials.json")
    _git(tmp_path, "commit", "-qm", "initial")

    secret.rename(tmp_path / "safe.json")
    _git(tmp_path, "add", "-A")

    assert {"credentials.json", "safe.json"} <= set(changed_files(tmp_path))
    diff = git_diff(tmp_path, patterns=["**/*"])
    assert "credentials.json" not in diff
    assert "safe.json" not in diff
    assert "do-not-disclose" not in diff


def test_git_diff_respects_disclosure_patterns_and_secret_denylist(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "safe.py").write_text("SAFE = True\n", encoding="utf-8")
    (tmp_path / "docs" / "private.md").write_text("not selected\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=do-not-send\n", encoding="utf-8")

    diff = git_diff(tmp_path, patterns=["src/**"])

    assert "src/safe.py" in diff
    assert "private.md" not in diff
    assert "do-not-send" not in diff


def test_redacts_loaded_and_credential_shaped_secrets(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "environment-secret-value")
    redacted = redact_text(
        "token=environment-secret-value\napi_key='literal-secret-value'\nSAFE = True"
    )
    assert "environment-secret-value" not in redacted
    assert "literal-secret-value" not in redacted
    assert redacted.count("[REDACTED]") == 2
    assert "SAFE = True" in redacted


def test_invalid_and_missing_contract_errors(tmp_path: Path) -> None:
    try:
        load_contract(tmp_path)
    except ContractError as exc:
        assert "No ConstraintLoop contract" in str(exc)
    else:
        raise AssertionError("missing contract should fail")

    contract_name = "constraintloop" + ".yml"
    (tmp_path / contract_name).write_text("version: [", encoding="utf-8")
    try:
        load_contract(tmp_path)
    except ContractError as exc:
        assert "Invalid contract" in str(exc)
    else:
        raise AssertionError("invalid contract should fail")
