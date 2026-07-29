from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from constraintloop.cli import main
from constraintloop.config import load_contract


def test_init_detects_python_tests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    result = CliRunner().invoke(main, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    contract, _ = load_contract(tmp_path)
    assert {"diff_hygiene", "python_syntax", "tests"} <= set(contract.constraints)


def test_init_uses_existing_uv_environment(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    result = CliRunner().invoke(main, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    contract, _ = load_contract(tmp_path)
    assert contract.constraints["tests"].command == [
        "uv",
        "--cache-dir",
        ".constraintloop/uv-cache",
        "run",
        "pytest",
        "-q",
    ]


def test_init_prefers_existing_project_venv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    binaries = tmp_path / ".venv" / "bin"
    binaries.mkdir(parents=True)
    (binaries / "python").touch()
    (binaries / "pytest").touch()
    result = CliRunner().invoke(main, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    contract, _ = load_contract(tmp_path)
    assert contract.constraints["python_syntax"].command[0] == ".venv/bin/python"
    assert contract.constraints["tests"].command[0] == ".venv/bin/pytest"


def test_enhance_and_author_only_write_proposals(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(main, ["enhance", "--project", str(tmp_path)]).exit_code == 0
    assert runner.invoke(main, ["author", "--project", str(tmp_path)]).exit_code == 0
    assert not (tmp_path / "constraintloop.yml").exists()
    assert (tmp_path / ".constraintloop/proposals/enhance.yml").is_file()
    assert (tmp_path / ".constraintloop/proposals/author.yml").is_file()
