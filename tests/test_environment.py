from __future__ import annotations

import os
from pathlib import Path

import pytest

from constraintloop.environment import load_project_environment


def test_loads_project_secret_without_overriding_process_environment(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / ".constraintloop"
    directory.mkdir()
    secret = directory / "secrets.env"
    secret.write_text("OPENAI_API_KEY=from-file\nEXISTING=from-file\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("EXISTING", "from-process")
    assert load_project_environment(tmp_path) == {"OPENAI_API_KEY": "from-file"}
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["EXISTING"] == "from-process"


def test_project_secrets_do_not_leak_between_sequential_loads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROJECT_API_KEY", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, value in ((first, "first-secret"), (second, "second-secret")):
        directory = root / ".constraintloop"
        directory.mkdir(parents=True)
        secret = directory / "secrets.env"
        secret.write_text(f"PROJECT_API_KEY={value}\n", encoding="utf-8")
        secret.chmod(0o600)

    assert load_project_environment(first) == {"PROJECT_API_KEY": "first-secret"}
    assert load_project_environment(second) == {"PROJECT_API_KEY": "second-secret"}
    assert "PROJECT_API_KEY" not in os.environ


def test_rejects_shell_syntax_in_secret_file(tmp_path: Path) -> None:
    directory = tmp_path / ".constraintloop"
    directory.mkdir()
    secret = directory / "secrets.env"
    secret.write_text("not a valid assignment\n", encoding="utf-8")
    secret.chmod(0o600)
    with pytest.raises(ValueError, match="Invalid environment entry"):
        load_project_environment(tmp_path)


@pytest.mark.parametrize("use_override", [False, True])
def test_rejects_insecure_or_symlinked_secret_file(
    tmp_path: Path, monkeypatch, use_override: bool
) -> None:
    directory = tmp_path / ".constraintloop"
    directory.mkdir()
    secret = (tmp_path / "override.env") if use_override else (directory / "secrets.env")
    if use_override:
        monkeypatch.setenv("CONSTRAINTLOOP_ENV_FILE", str(secret))
    secret.write_text("KEY=value\n", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        load_project_environment(tmp_path)

    secret.unlink()
    target = tmp_path / "target.env"
    target.write_text("KEY=value\n", encoding="utf-8")
    target.chmod(0o600)
    secret.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_project_environment(tmp_path)
