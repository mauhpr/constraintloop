from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from constraintloop import __version__
from constraintloop.cli import main
from constraintloop.config import ContractError, load_contract


def test_init_detects_python_tests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    result = CliRunner().invoke(main, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    contract, _ = load_contract(tmp_path)
    assert {"diff_hygiene", "python_syntax", "tests"} <= set(contract.constraints)
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".constraintloop/state/" in ignored
    assert ".constraintloop/hooks-disabled.json" in ignored
    assert ".claude/settings.local.json" in ignored
    assert ".codex/hooks.json" in ignored
    assert ".gemini/settings.json" in ignored
    assert "constraintloop.local.yml" in ignored


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


def test_local_contract_overlay_recursively_merges_without_changing_base(tmp_path: Path) -> None:
    base = {
        "version": 1,
        "settings": {"concurrency": 4},
        "constraints": {"check": {"kind": "command", "command": ["true"], "watch": ["source"]}},
    }
    overlay = {
        "settings": {"concurrency": 1},
        "constraints": {
            "check": {"timeout_seconds": 12},
            "local-check": {"kind": "command", "command": ["true"], "watch": ["source"]},
        },
    }
    (tmp_path / "source").write_text("data", encoding="utf-8")
    base_path = tmp_path / "constraintloop.yml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    original = base_path.read_text(encoding="utf-8")
    (tmp_path / "constraintloop.local.yml").write_text(yaml.safe_dump(overlay), encoding="utf-8")

    contract, path = load_contract(tmp_path)

    assert path == base_path
    assert contract.settings.concurrency == 1
    assert contract.constraints["check"].command == ["true"]
    assert contract.constraints["check"].timeout_seconds == 12
    assert "local-check" in contract.constraints
    assert base_path.read_text(encoding="utf-8") == original


def test_local_contract_overlay_cannot_weaken_committed_gate(tmp_path: Path) -> None:
    (tmp_path / "constraintloop.yml").write_text(
        yaml.safe_dump(
            {"constraints": {"check": {"kind": "command", "command": ["true"], "phases": ["stop"]}}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "constraintloop.local.yml").write_text(
        yaml.safe_dump({"constraints": {"check": {"enabled": False}}}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="cannot replace committed constraints.check.enabled"):
        load_contract(tmp_path)


def test_contract_rejects_ambiguous_and_non_mapping_overlays(tmp_path: Path) -> None:
    base = {"constraints": {"check": {"kind": "command", "command": ["true"]}}}
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (tmp_path / "constraintloop.local.yml").write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(ContractError, match="top-level value must be a mapping"):
        load_contract(tmp_path)

    (tmp_path / "constraintloop.local.yml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "constraintloop.local.yaml").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ContractError, match="Multiple local contract overlays"):
        load_contract(tmp_path)


def test_unknown_contract_keys_report_runtime_version_and_upgrade_action(tmp_path: Path) -> None:
    payload = {
        "settings": {"future_setting": True},
        "constraints": {"check": {"kind": "command", "command": ["true"]}},
    }
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ContractError) as raised:
        load_contract(tmp_path)

    message = str(raised.value)
    assert "future_setting" in message
    assert f"ConstraintLoop {__version__} does not recognize one or more keys" in message
    assert "constraintloop setup --adapter all --project ." in message


@pytest.mark.parametrize(
    ("overlay", "field"),
    [
        ({"settings": {"evidence_output_limit": 1024}}, "evidence_output_limit"),
        ({"settings": {"evaluation_bundle_limit": 4096}}, "evaluation_bundle_limit"),
        ({"constraints": {"check": {"enforcement": "advisory"}}}, "enforcement"),
        ({"constraints": {"check": {"phases": []}}}, "phases"),
        ({"constraints": {"check": {"needs": []}}}, "needs"),
        ({"constraints": {"check": {"timeout_seconds": 301}}}, "timeout_seconds"),
        ({"constraints": {"check": {"command": ["false"]}}}, "command"),
    ],
)
def test_local_overlay_rejects_other_policy_weakening(
    tmp_path: Path, overlay: dict[str, object], field: str
) -> None:
    base = {
        "settings": {"evidence_output_limit": 65536, "evaluation_bundle_limit": 102400},
        "constraints": {
            "dependency": {"kind": "command", "command": ["true"]},
            "check": {
                "kind": "command",
                "command": ["true"],
                "needs": ["dependency"],
                "phases": ["stop"],
            },
        },
    }
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (tmp_path / "constraintloop.local.yml").write_text(yaml.safe_dump(overlay), encoding="utf-8")

    with pytest.raises(ContractError, match=field):
        load_contract(tmp_path)


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ({"evaluators": {"reviewer": {"timeout_seconds": 61}}}, "evaluator"),
        ({"loops": {"completion": {"interval_seconds": 2}}}, "loop"),
    ],
)
def test_local_overlay_cannot_replace_evaluators_or_loops(
    tmp_path: Path, overlay: dict[str, object], message: str
) -> None:
    base = {
        "constraints": {
            "check": {"kind": "command", "command": ["true"], "phases": ["stop"]},
            "review": {
                "kind": "rubric",
                "enforcement": "advisory",
                "evaluator": "reviewer",
                "rubric": "Review",
                "phases": ["stop"],
            },
        },
        "evaluators": {
            "reviewer": {"type": "command", "command": ["review"], "timeout_seconds": 60}
        },
        "loops": {
            "completion": {
                "phase": "stop",
                "interval_seconds": 1,
                "max_repair_attempts": 1,
                "max_unchanged_repairs": 1,
                "max_duration_seconds": 60,
            }
        },
    }
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (tmp_path / "constraintloop.local.yml").write_text(yaml.safe_dump(overlay), encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        load_contract(tmp_path)
