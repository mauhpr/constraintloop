from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from click.testing import CliRunner

import constraintloop.cli as cli_module
from constraintloop.cli import main


def _write_contract(root: Path, *, passing: bool = True) -> None:
    code = "pass" if passing else "raise SystemExit(1)"
    payload = {
        "version": 1,
        "constraints": {
            "check": {
                "kind": "command",
                "command": [sys.executable, "-c", code],
                "phases": ["change", "stop", "ci"],
                "watch": ["source"],
            }
        },
    }
    contract_name = "constraintloop" + ".yml"
    (root / contract_name).write_text(yaml.safe_dump(payload), encoding="utf-8")
    (root / "source").write_text("data", encoding="utf-8")


def test_run_ci_status_doctor_and_waive_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_contract(tmp_path)
    runner = CliRunner()

    run = runner.invoke(main, ["run", "--project", str(tmp_path), "--json"])
    assert run.exit_code == 0
    assert json.loads(run.output)["phase"] == "stop"
    assert runner.invoke(main, ["status", "--project", str(tmp_path)]).output.startswith("PASS")
    doctor = runner.invoke(main, ["doctor", "--project", str(tmp_path)])
    assert doctor.exit_code == 0
    assert "contract digest:" in doctor.output
    assert runner.invoke(main, ["ci", "--project", str(tmp_path)]).exit_code == 0
    waiver = runner.invoke(
        main, ["waive", "check", "--reason", "local investigation", "--project", str(tmp_path)]
    )
    assert waiver.exit_code != 0
    assert "already pass" in waiver.output


def test_push_phase_runs_only_push_gates_without_local_waivers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    payload = {
        "constraints": {
            "fast": {
                "kind": "command",
                "command": [sys.executable, "-c", "raise SystemExit(1)"],
                "phases": ["stop"],
            },
            "integration": {
                "kind": "command",
                "command": [sys.executable, "-c", "print('integration ok')"],
                "phases": ["push", "ci"],
            },
        }
    }
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(
        main, ["run", "--phase", "push", "--project", str(tmp_path), "--json"]
    )

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["phase"] == "push"
    assert [item["constraint_id"] for item in record["results"]] == ["integration"]


def test_doctor_deep_reports_missing_inputs_environment_and_python_module(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("REQUIRED_TOKEN", raising=False)
    (tmp_path / ".gitignore").write_text(
        ".constraintloop/state/\nconstraintloop.local.yml\nconstraintloop.local.yaml\n",
        encoding="utf-8",
    )
    payload = {
        "constraints": {
            "integration": {
                "kind": "command",
                "command": [
                    sys.executable,
                    "-m",
                    "definitely_missing_constraintloop_module",
                    "--env-file",
                    ".env",
                    "$REQUIRED_TOKEN",
                ],
                "watch": ["missing/**/*.py"],
            }
        }
    }
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(main, ["doctor", "--deep", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "watch globs match no files" in result.output
    assert "environment variable is missing: REQUIRED_TOKEN" in result.output
    assert "Python module not found" in result.output
    assert "referenced environment file is missing: .env" in result.output


def test_ci_ignores_local_contract_overlay(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_contract(tmp_path, passing=True)
    overlay = {"constraints": {"check": {"command": [sys.executable, "-c", "raise SystemExit(1)"]}}}
    (tmp_path / "constraintloop.local.yml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
    runner = CliRunner()

    assert runner.invoke(main, ["run", "--project", str(tmp_path)]).exit_code == 1
    assert runner.invoke(main, ["ci", "--project", str(tmp_path)]).exit_code == 0


def test_waive_requires_fresh_non_passing_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_contract(tmp_path, passing=False)
    runner = CliRunner()
    command = [
        "waive",
        "check",
        "--reason",
        "accepted local failure",
        "--project",
        str(tmp_path),
    ]

    missing = runner.invoke(main, command)
    assert missing.exit_code != 0
    assert "run the constraint before" in missing.output

    assert runner.invoke(main, ["run", "--project", str(tmp_path)]).exit_code == 1
    (tmp_path / "source").write_text("changed", encoding="utf-8")
    stale = runner.invoke(main, command)
    assert stale.exit_code != 0
    assert "No fresh evidence exists" in stale.output

    assert runner.invoke(main, ["run", "--project", str(tmp_path)]).exit_code == 1
    waiver = runner.invoke(main, command)
    assert waiver.exit_code == 0
    assert "CI ignores local waivers" in waiver.output
    assert runner.invoke(main, ["run", "--project", str(tmp_path)]).exit_code == 0
    assert runner.invoke(main, ["ci", "--project", str(tmp_path)]).exit_code == 1


def test_waive_rejects_an_empty_reason(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_contract(tmp_path)
    result = CliRunner().invoke(
        main,
        ["waive", "check", "--reason", "   ", "--project", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "Waiver reason must not be empty" in result.output


def test_waive_rejects_rubric_constraints(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    payload = {
        "constraints": {
            "review": {
                "kind": "rubric",
                "enforcement": "advisory",
                "evaluator": "reviewer",
                "rubric": "Review",
                "phases": ["stop"],
            }
        },
        "evaluators": {
            "reviewer": {
                "type": "command",
                "command": [sys.executable, "-c", "raise SystemExit(1)"],
            }
        },
    }
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["waive", "review", "--reason", "skip review", "--project", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "Rubric constraints cannot be waived" in result.output


def test_cli_failures_and_hook_payloads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_contract(tmp_path, passing=False)
    runner = CliRunner()
    assert runner.invoke(main, ["run", "--project", str(tmp_path), "--no-cache"]).exit_code == 1
    unknown = runner.invoke(main, ["waive", "missing", "--reason", "x", "--project", str(tmp_path)])
    assert unknown.exit_code != 0
    invalid = runner.invoke(
        main,
        ["hook", "--adapter", "codex", "--event", "session-start", "--project", str(tmp_path)],
        input="{",
    )
    assert json.loads(invalid.output)["continue"] is False
    for malformed in ("[]", '"payload"', "null", "1"):
        wrong_type = runner.invoke(
            main,
            ["hook", "--adapter", "codex", "--event", "session-start", "--project", str(tmp_path)],
            input=malformed,
        )
        response = json.loads(wrong_type.output)
        assert response["continue"] is False
        assert response["stopReason"] == "Invalid hook JSON: top-level value must be an object"
    valid = runner.invoke(
        main,
        ["hook", "--adapter", "codex", "--event", "session-start", "--project", str(tmp_path)],
        input=json.dumps({"cwd": str(tmp_path / "untrusted-repository")}),
    )
    assert "Required gates: check" in valid.output

    missing_project = runner.invoke(
        main,
        ["hook", "--adapter", "codex", "--event", "session-start"],
        input="{}",
    )
    assert missing_project.exit_code != 0
    assert "Missing option '--project'" in missing_project.output


def test_cycle_operational_error_has_stable_json_and_exit_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    _write_contract(tmp_path)
    result = CliRunner().invoke(
        main,
        ["cycle", "missing", "--project", str(tmp_path), "--json"],
    )
    assert result.exit_code == 14
    payload = json.loads(result.output)
    assert payload["state"] == "error"
    assert payload["loop"] == "missing"


def test_setup_and_init_existing_contract_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/constraintloop"])
    runner = CliRunner()
    setup = runner.invoke(main, ["setup", "--adapter", "all", "--project", str(tmp_path)])
    assert setup.exit_code == 0
    assert setup.output.count("Updated") == 3
    uninstall = runner.invoke(
        main,
        ["uninstall", "--adapter", "all", "--project", str(tmp_path)],
    )
    assert uninstall.exit_code == 0
    assert uninstall.output.count("Removed 6 ConstraintLoop hook(s)") == 3
    _write_contract(tmp_path)
    existing = runner.invoke(main, ["init", "--project", str(tmp_path)])
    assert existing.exit_code != 0


def test_setup_reports_ignore_protection_failure_and_tracked_state(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        cli_module,
        "ensure_local_files_ignored",
        lambda root: (_ for _ in ()).throw(OSError("read-only ignore file")),
    )
    failed = runner.invoke(main, ["setup", "--adapter", "codex", "--project", str(tmp_path)])
    assert failed.exit_code != 0
    assert "Could not protect local ConstraintLoop files" in failed.output

    monkeypatch.setattr(cli_module, "ensure_local_files_ignored", lambda root: (root, []))
    monkeypatch.setattr(
        cli_module, "tracked_state_files", lambda root: [".constraintloop/state/evidence.json"]
    )
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/constraintloop"])
    warned = runner.invoke(main, ["setup", "--adapter", "codex", "--project", str(tmp_path)])
    assert warned.exit_code == 0
    assert "already tracked" in warned.output


def test_setup_and_uninstall_report_hook_filesystem_errors(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    def deny_install(root: Path, adapter: str) -> Path:
        raise PermissionError("read-only settings")

    def deny_uninstall(root: Path, adapter: str) -> tuple[Path, int]:
        raise PermissionError("read-only settings")

    monkeypatch.setattr(cli_module, "install_hooks", deny_install)
    setup = runner.invoke(main, ["setup", "--adapter", "codex", "--project", str(tmp_path)])
    assert setup.exit_code != 0
    assert setup.output == "Error: Could not install codex hooks: read-only settings\n"
    assert "Traceback" not in setup.output

    monkeypatch.setattr(cli_module, "uninstall_hooks", deny_uninstall)
    uninstall = runner.invoke(main, ["uninstall", "--adapter", "codex", "--project", str(tmp_path)])
    assert uninstall.exit_code != 0
    assert uninstall.output == "Error: Could not remove codex hooks: read-only settings\n"
    assert "Traceback" not in uninstall.output


def test_debug_reports_stale_evidence_and_missing_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    contract_name = "constraintloop" + ".yml"
    payload = {
        "version": 1,
        "constraints": {
            "review": {
                "kind": "rubric",
                "enforcement": "advisory",
                "evaluator": "reviewer",
                "rubric": "Review",
                "phases": ["stop"],
                "watch": ["source.py"],
            }
        },
        "evaluators": {
            "reviewer": {
                "type": "command",
                "command": ["definitely-missing-constraintloop-evaluator"],
            }
        },
    }
    (tmp_path / contract_name).write_text(yaml.safe_dump(payload), encoding="utf-8")
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner = CliRunner()
    runner.invoke(main, ["run", "--project", str(tmp_path)])
    acknowledgment = runner.invoke(
        main,
        [
            "acknowledge",
            "review",
            "--reason",
            "Evaluator is intentionally unavailable in this fixture",
            "--project",
            str(tmp_path),
        ],
    )
    assert acknowledgment.exit_code == 0
    assert "verdict remains advisory and unchanged" in acknowledgment.output

    payload["evaluators"]["reviewer"]["timeout_seconds"] = 61
    (tmp_path / contract_name).write_text(yaml.safe_dump(payload), encoding="utf-8")
    debug = runner.invoke(main, ["debug", "review", "--project", str(tmp_path)])

    assert debug.exit_code == 0
    assert "evidence: STALE (UNCERTAIN)" in debug.output
    assert "Evaluator could not start" in debug.output
    assert "executable: NOT FOUND" in debug.output
    assert "did not execute the evaluator" in debug.output


def test_ratchet_baseline_update_run_status_and_regression_guard(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    count = tmp_path / "count"
    count.write_text("5", encoding="utf-8")
    payload = {
        "constraints": {
            "database_consumers": {
                "kind": "ratchet",
                "command": [
                    sys.executable,
                    "-c",
                    "import json; from pathlib import Path; "
                    "print(json.dumps({'count': int(Path('count').read_text())}))",
                ],
                "parser": {"type": "json", "path": "count"},
                "watch": ["count"],
                "phases": ["stop"],
            }
        }
    }
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    runner = CliRunner()

    initialized = runner.invoke(
        main, ["baseline", "update", "database_consumers", "--project", str(tmp_path)]
    )
    assert initialized.exit_code == 0, initialized.output
    baseline = json.loads((tmp_path / "constraintloop-baselines.json").read_text())
    assert baseline["ratchets"]["database_consumers"]["value"] == 5
    assert len(baseline["ratchets"]["database_consumers"]["evidence_sha256"]) == 64

    passing = runner.invoke(main, ["run", "--project", str(tmp_path)])
    assert passing.exit_code == 0
    assert "change=0" in passing.output
    assert "baseline_evidence_sha256=" in passing.output
    status = runner.invoke(main, ["status", "--project", str(tmp_path)])
    assert "value=5" in status.output
    assert "baseline=5" in status.output

    count.write_text("6", encoding="utf-8")
    failing = runner.invoke(main, ["run", "--project", str(tmp_path)])
    assert failing.exit_code == 1
    assert "Failure classes: constraint=1" in failing.output
    assert "[constraint]" in failing.output

    refused = runner.invoke(
        main, ["baseline", "update", "database_consumers", "--project", str(tmp_path)]
    )
    assert refused.exit_code != 0
    assert "Refusing to weaken" in refused.output
    allowed = runner.invoke(
        main,
        [
            "baseline",
            "update",
            "database_consumers",
            "--allow-regression",
            "--project",
            str(tmp_path),
        ],
    )
    assert allowed.exit_code == 0


def test_explain_reports_phase_skips_watch_paths_and_dependency_chains(tmp_path: Path) -> None:
    payload = {
        "constraints": {
            "inventory": {
                "kind": "command",
                "command": ["true"],
                "watch": ["src/**/*.py"],
                "phases": ["stop"],
            },
            "migration": {
                "kind": "command",
                "command": ["true"],
                "watch": ["src/**/*.py"],
                "needs": ["inventory"],
                "phases": ["ci"],
            },
        }
    }
    (tmp_path / "src").mkdir()
    (tmp_path / "src/consumer.py").write_text("CONSUMER = True\n", encoding="utf-8")
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(main, ["explain", "--phase", "stop", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "RUN inventory" in result.output
    assert "SKIP migration" in result.output
    assert "src/consumer.py" in result.output
    assert "inventory -> migration" in result.output


def test_explain_json_covers_disabled_and_stale_rubric_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CONSTRAINTLOOP_CACHE_DIR", str(tmp_path / "cache"))
    payload = {
        "constraints": {
            "disabled": {"kind": "artifact", "path": "report", "enabled": False},
            "review": {
                "kind": "rubric",
                "enforcement": "advisory",
                "evaluator": "reviewer",
                "rubric": "Review",
                "watch": ["source.py"],
                "phases": ["stop"],
            },
        },
        "evaluators": {
            "reviewer": {
                "type": "command",
                "command": [
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps({'verdict':'pass','score':1,"
                    "'rationale':'ok','findings':[]}))",
                ],
            }
        },
    }
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(main, ["run", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = runner.invoke(
        main, ["explain", "--phase", "stop", "--json", "--project", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    explanation = json.loads(result.output)
    by_id = {item["constraint_id"]: item for item in explanation["constraints"]}
    assert by_id["disabled"]["decision"] == "skip"
    assert by_id["review"]["cache"] == "stale"

    missing = runner.invoke(main, ["explain", "--project", str(tmp_path / "missing")])
    assert missing.exit_code != 0
    assert "No ConstraintLoop contract" in missing.output


def test_baseline_update_reports_usage_measurement_and_write_errors(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    missing = runner.invoke(main, ["baseline", "update", "--all", "--project", str(tmp_path)])
    assert missing.exit_code != 0
    assert "No ConstraintLoop contract" in missing.output

    plain = {"constraints": {"check": {"kind": "command", "command": ["true"]}}}
    path = tmp_path / "constraintloop.yml"
    path.write_text(yaml.safe_dump(plain), encoding="utf-8")
    conflict = runner.invoke(
        main, ["baseline", "update", "check", "--all", "--project", str(tmp_path)]
    )
    assert "Choose constraint IDs or --all" in conflict.output
    empty = runner.invoke(main, ["baseline", "update", "--project", str(tmp_path)])
    assert "Provide at least one" in empty.output
    unknown = runner.invoke(main, ["baseline", "update", "check", "--project", str(tmp_path)])
    assert "Unknown ratchet" in unknown.output

    ratchet = {
        "constraints": {
            "count": {
                "kind": "ratchet",
                "command": [sys.executable, "-c", "raise SystemExit(2)"],
                "parser": {"type": "json", "path": "count"},
            }
        }
    }
    path.write_text(yaml.safe_dump(ratchet), encoding="utf-8")
    failed = runner.invoke(main, ["baseline", "update", "count", "--project", str(tmp_path)])
    assert "Could not measure count" in failed.output

    ratchet["constraints"]["count"]["command"] = [
        sys.executable,
        "-c",
        "print('{\"count\": 1}')",
    ]
    path.write_text(yaml.safe_dump(ratchet), encoding="utf-8")
    monkeypatch.setattr(
        cli_module,
        "save_ratchet_baseline",
        lambda *args: (_ for _ in ()).throw(OSError("read-only baseline")),
    )
    write_error = runner.invoke(main, ["baseline", "update", "--all", "--project", str(tmp_path)])
    assert "Could not update baseline for count" in write_error.output


def test_status_value_formats_nested_json() -> None:
    assert cli_module._format_status_value({"count": [1, 2]}) == '{"count":[1,2]}'
