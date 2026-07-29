from __future__ import annotations

import re
import runpy
import sys
import tomllib
from pathlib import Path

import pytest

import constraintloop
from constraintloop.cli import main


def test_package_versions_and_canonical_urls_are_consistent() -> None:
    root = Path(__file__).parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    readme = (root / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())

    assert metadata["version"] == constraintloop.__version__
    assert metadata["name"] == "constraintloop"
    assert metadata["urls"]["Repository"] == "https://github.com/mauhpr/constraintloop"
    assert metadata["urls"]["Issues"].endswith("/constraintloop/issues")
    assert "block autonomous completion" in normalized_readme
    assert "CI ignores every waiver and remains blocking" in normalized_readme
    assert "local CLI cannot authenticate whether its caller is human" in normalized_readme
    assert "always block completion" not in normalized_readme
    conduct = (root / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    security = (root / "SECURITY.md").read_text(encoding="utf-8")
    assert "Contributor Covenant Code of Conduct" in conduct
    assert "version 2.1" in conduct
    assert "[INSERT CONTACT METHOD]" not in conduct
    assert "mauricio_perez_r@hotmail.com" in conduct
    assert "mauricio_perez_r@hotmail.com" in security

    undocumented = [
        name
        for name, command in main.commands.items()
        if not command.hidden and f"`constraintloop {name}" not in readme
    ]
    assert undocumented == []


def test_python_module_entrypoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["constraintloop", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("constraintloop", run_name="__main__")

    assert exc_info.value.code == 0
    assert (
        capsys.readouterr().out.strip() == f"constraintloop, version {constraintloop.__version__}"
    )


def test_release_workflows_use_separate_oidc_publish_jobs() -> None:
    root = Path(__file__).parents[1]
    production = (root / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    test = (root / ".github/workflows/test-publish.yml").read_text(encoding="utf-8")

    for workflow in (production, test):
        assert "id-token: write" in workflow
        assert "password:" not in workflow
        assert "needs: build" in workflow
        assert "pypa/gh-action-pypi-publish@" in workflow
    assert "environment:\n      name: pypi" in production
    assert "environment:\n      name: testpypi" in test


def test_workflow_actions_are_immutable_and_checkouts_drop_credentials() -> None:
    root = Path(__file__).parents[1]
    workflow_dir = root / ".github/workflows"
    action_ref = re.compile(r"^\s*-\s+uses:\s+\S+@([^\s#]+)", re.MULTILINE)

    for workflow_path in workflow_dir.glob("*.yml"):
        workflow = workflow_path.read_text(encoding="utf-8")
        refs = action_ref.findall(workflow)
        assert refs, f"{workflow_path.name} contains no actions"
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
        assert workflow.count("actions/checkout@") == workflow.count("persist-credentials: false")
