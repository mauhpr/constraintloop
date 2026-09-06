"""Behavioral regressions from the v0.5 pre-release review."""

from __future__ import annotations

import json
import sys
import time

import pytest
import yaml
from click.testing import CliRunner

from constraintloop.cli import main
from constraintloop.config import contract_digest, load_loop_contract
from constraintloop.digest import constraint_input_digest
from constraintloop.engine import ConstraintEngine
from constraintloop.hooks import handle_hook
from constraintloop.loops import LoopError, journal_path, lease_path, loop_lease, run_cycle
from constraintloop.models import Contract, LoopState, MetricThreshold, Phase, Verdict
from constraintloop.redaction import redact_value
from constraintloop.state import create_waiver, load_ratchet_baseline, save_ratchet_baseline


@pytest.fixture(autouse=True)
def local_cache(monkeypatch):
    monkeypatch.delenv("CONSTRAINTLOOP_CACHE_DIR", raising=False)


def loop(phase="stop", **extra):
    return dict(
        phase=phase,
        interval_seconds=10,
        max_repair_attempts=2,
        max_unchanged_repairs=2,
        max_duration_seconds=600,
        **extra,
    )


def command(code="raise SystemExit(1)", **extra):
    return dict(kind="command", command=[sys.executable, "-c", code], watch=["source"], **extra)


def write_contract(root, contract):
    (root / "constraintloop.yml").write_text(yaml.safe_dump(contract.model_dump(mode="json")))


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("with_loop", [False, True])
def test_recursive_completion_is_bounded_even_when_failures_change(tmp_path, adapter, with_loop):
    contract = Contract.model_validate(
        {
            "constraints": {"tests": command()},
            "loops": {"completion": loop()} if with_loop else {},
        }
    )
    write_contract(tmp_path, contract)
    for attempt in range(3):
        (tmp_path / "source").write_text(str(attempt))
        response = handle_hook(
            tmp_path,
            adapter,
            "stop",
            {
                "session_id": "review",
                "stop_hook_active": attempt > 0,
            },
        )
        if attempt < 2:
            assert response["decision"] in {"deny", "block"}
        else:
            assert response["continue"] is False


def test_advisory_continuations_are_bounded_and_do_not_claim_pass(tmp_path):
    contract = Contract.model_validate({"constraints": {"lint": command(enforcement="advisory")}})
    write_contract(tmp_path, contract)
    for attempt in range(3):
        response = handle_hook(tmp_path, "codex", "stop", {"stop_hook_active": True})
        if attempt < 2:
            assert response["decision"] == "block"
        else:
            assert response["continue"] is False


@pytest.mark.parametrize("enforcement", ["required", "advisory"])
@pytest.mark.parametrize(
    "code,expected",
    [
        ("raise SystemExit(1)", LoopState.REPAIR),
        ("raise SystemExit(75)", LoopState.WAITING),
    ],
)
def test_dependency_failures_report_root_causes(tmp_path, enforcement, code, expected):
    contract = Contract.model_validate(
        {
            "constraints": {
                "syntax": command(code, enforcement=enforcement),
                "middle": command("pass", needs=["syntax"], enforcement="advisory"),
                "tests": command("raise AssertionError('must not execute')", needs=["middle"]),
            },
            "loops": {"completion": loop()},
        }
    )
    result = run_cycle(tmp_path, contract, "completion")
    assert result.state == expected
    assert result.blocking_constraints == ["syntax"]


def test_dependency_environment_errors_are_not_repair_requests(tmp_path):
    contract = Contract.model_validate(
        {
            "constraints": {
                "syntax": {"kind": "command", "command": ["/nonexistent/review-command"]},
                "tests": command("pass", needs=["syntax"]),
            },
            "loops": {"completion": loop()},
        }
    )
    result = run_cycle(tmp_path, contract, "completion")
    assert result.state == LoopState.ERROR
    assert result.blocking_constraints == ["syntax"]


@pytest.mark.parametrize("split", range(1, 8))
def test_digest_frames_every_filename_content_boundary(tmp_path, split):
    combined = "abcdefghijk"
    spec = Contract.model_validate(
        {
            "constraints": {
                "report": {"kind": "artifact", "path": combined[:split]},
            }
        }
    ).constraints["report"]
    path = tmp_path / combined[:split]
    path.write_text(combined[split:])
    before = constraint_input_digest(tmp_path, "report", spec)
    path.rename(tmp_path / combined[: split + 1])
    (tmp_path / combined[: split + 1]).write_text(combined[split + 1 :])
    assert constraint_input_digest(tmp_path, "report", spec) != before


def test_deleted_artifact_cannot_reuse_ambiguous_cached_pass(tmp_path):
    contract = Contract.model_validate(
        {"constraints": {"report": {"kind": "artifact", "path": "a"}}}
    )
    (tmp_path / "a").write_text("bc")
    assert ConstraintEngine(tmp_path, contract).run(Phase.STOP).passed
    (tmp_path / "a").rename(tmp_path / "ab")
    (tmp_path / "ab").write_text("c")
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert not result.passed and not result.results[0].cached


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("kind", ["junit", "metric_null", "unexpected"])
def test_malformed_evidence_returns_valid_blocking_hook_json(tmp_path, monkeypatch, adapter, kind):
    if kind == "junit":
        (tmp_path / "report.xml").write_text("<testsuite")
        spec = dict(kind="artifact", format="junit", path="report.xml")
    else:
        spec = dict(
            kind="metric",
            command=[sys.executable, "-c", "print('{\"value\": null}')"],
            parser=dict(type="json", path="value"),
            threshold=dict(operator="gte", value=0),
        )
    write_contract(tmp_path, Contract.model_validate({"constraints": {"evidence": spec}}))
    if kind == "unexpected":

        def broken(*args):
            raise RuntimeError("password=FAKE_SENSITIVE_REVIEW_MARKER")

        monkeypatch.setattr(ConstraintEngine, "run", broken)
    result = CliRunner().invoke(
        main,
        ["hook", "--adapter", adapter, "--event", "stop", "--project", str(tmp_path)],
        input="{}",
    )
    assert result.exit_code == 0, result.output
    response = json.loads(result.output)
    assert response.get("decision") in {"deny", "block"} or response.get("continue") is False
    assert "FAKE_SENSITIVE_REVIEW_MARKER" not in result.output


@pytest.mark.parametrize("phase", list(Phase))
def test_waiver_policy_is_identical_in_engine_run_and_cycle(tmp_path, phase):
    contract = Contract.model_validate(
        {
            "constraints": {"tests": command(phases=[item.value for item in Phase])},
            "loops": {"review": loop(phase.value)},
        }
    )
    write_contract(tmp_path, contract)
    record = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    create_waiver(tmp_path, record.results[0], contract_digest(contract), "Human local exception")
    allowed = phase in {Phase.STOP, Phase.CHANGE}
    assert ConstraintEngine(tmp_path, contract).run(phase).passed is allowed
    assert (run_cycle(tmp_path, contract, "review").state == LoopState.PASSED) is allowed
    cli = CliRunner().invoke(main, ["run", "--phase", phase.value, "--project", str(tmp_path)])
    assert (cli.exit_code == 0) is allowed


def test_ci_cycle_ignores_invalid_local_overlay(tmp_path):
    contract = Contract.model_validate(
        {
            "constraints": {"tests": command("pass")},
            "loops": {"review": loop("ci")},
        }
    )
    write_contract(tmp_path, contract)
    (tmp_path / "constraintloop.local.yml").write_text("not: [valid")
    assert load_loop_contract(tmp_path, "review")[0] == contract
    result = CliRunner().invoke(main, ["cycle", "review", "--json", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_completed_task_gets_new_budget_but_unfinished_task_does_not(tmp_path):
    (tmp_path / "source").write_text("pass")
    contract = Contract.model_validate(
        {
            "constraints": {
                "tests": command(
                    "from pathlib import Path; "
                    "raise SystemExit(0 if Path('source').read_text() == 'pass' else 1)"
                )
            },
            "loops": {"completion": loop()},
        }
    )
    assert run_cycle(tmp_path, contract, "completion", now=100).state == LoopState.PASSED
    assert run_cycle(tmp_path, contract, "completion", now=800).state == LoopState.PASSED
    (tmp_path / "source").write_text("new failure")
    result = run_cycle(tmp_path, contract, "completion", now=900, goal="New feature")
    assert result.state == LoopState.REPAIR and result.repair_attempt == 0
    (tmp_path / "source").write_text("changed failure")
    result = run_cycle(tmp_path, contract, "completion", now=1600, goal="Still not done")
    assert result.state == LoopState.BUDGET_EXHAUSTED


@pytest.mark.parametrize(
    "invalid", [None, True, False, [], {}, "Infinity", "-Infinity", "NaN", "1e999", "not numeric"]
)
def test_invalid_measurements_never_pass_or_update_baselines(tmp_path, invalid):
    contract = Contract.model_validate(
        {
            "constraints": {
                "coverage": dict(
                    kind="metric",
                    command=[sys.executable, "-c", f"print({json.dumps({'value': invalid})!r})"],
                    parser=dict(type="json", path="value"),
                    threshold=dict(operator="gte", value=95),
                )
            }
        }
    )
    record = ConstraintEngine(tmp_path, contract, use_cache=False).run(Phase.STOP)
    assert not record.passed and record.results[0].verdict == Verdict.ERROR
    with pytest.raises(ValueError):
        MetricThreshold(operator="gte", value=invalid)
    with pytest.raises(ValueError):
        save_ratchet_baseline(tmp_path, "baseline.json", "coverage", invalid)
    assert not (tmp_path / "baseline.json").exists()
    (tmp_path / "baseline.json").write_text(json.dumps({"ratchets": {"coverage": invalid}}))
    assert load_ratchet_baseline(tmp_path, "baseline.json", "coverage") is None


def test_finite_numeric_strings_remain_supported(tmp_path):
    assert MetricThreshold(operator="gte", value="95.5").value == 95.5
    save_ratchet_baseline(tmp_path, "baseline.json", "coverage", "95.5")
    assert load_ratchet_baseline(tmp_path, "baseline.json", "coverage") == 95.5


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
def test_hook_pending_refresh_obeys_cycle_interval(tmp_path, monkeypatch, adapter):
    remote = tmp_path / ".constraintloop" / "remote"
    remote.parent.mkdir()
    remote.write_text("pending")
    contract = Contract.model_validate(
        {
            "constraints": {
                "ci": command(
                    "from pathlib import Path; "
                    "raise SystemExit(75 if Path('.constraintloop/remote').read_text() "
                    "== 'pending' else 0)"
                )
            },
            "loops": {"watch": loop()},
        }
    )
    write_contract(tmp_path, contract)
    monkeypatch.setattr("constraintloop.loops.time.time", lambda: 100)
    assert "waiting" in handle_hook(tmp_path, adapter, "stop", {})["reason"]
    remote.write_text("ready")
    monkeypatch.setattr("constraintloop.loops.time.time", lambda: 101)
    assert "waiting" in handle_hook(tmp_path, adapter, "stop", {})["reason"]
    monkeypatch.setattr("constraintloop.loops.time.time", lambda: 111)
    response = handle_hook(tmp_path, adapter, "stop", {"stopHookActive": True})
    assert response.get("continue") is True or response.get("decision") == "allow"
    journal = json.loads(journal_path(tmp_path, "watch").read_text())
    assert journal["repair_attempt"] == 0


def test_retained_evidence_and_hook_feedback_redact_fake_secrets(tmp_path):
    marker = "REVIEW_FAKE_SECRET_0123456789"
    contract = Contract.model_validate(
        {"constraints": {"tests": command(f"print('password={marker}'); raise SystemExit(1)")}}
    )
    write_contract(tmp_path, contract)
    record = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert marker not in record.model_dump_json()
    assert marker not in (tmp_path / ".constraintloop/state/evidence.json").read_text()
    assert marker not in json.dumps(handle_hook(tmp_path, "codex", "stop", {}))
    assert redact_value({"password": marker, "nested": [{"auth_token": marker}]}) == {
        "password": "[REDACTED]",
        "nested": [{"auth_token": "[REDACTED]"}],
    }


@pytest.mark.parametrize("adapter", ["claude", "codex", "gemini"])
@pytest.mark.parametrize(
    "prefix", ["constraintloop", "python -m constraintloop", ".venv/bin/constraintloop"]
)
def test_observed_baseline_weakening_is_denied(tmp_path, adapter, prefix):
    response = handle_hook(
        tmp_path,
        adapter,
        "pre-tool",
        {
            "tool_name": "exec",
            "tool_input": {"cmd": f"{prefix} baseline update --all --allow-regression"},
        },
    )
    assert (
        response.get("decision") == "deny"
        or response["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert (
        handle_hook(
            tmp_path,
            adapter,
            "pre-tool",
            {
                "tool_name": "exec",
                "tool_input": {"cmd": f"{prefix} baseline update --all"},
            },
        )
        == {}
    )


def test_observed_custom_baseline_edits_are_denied_but_reads_allowed(tmp_path):
    contract = Contract.model_validate(
        {
            "constraints": {
                "size": {
                    "kind": "ratchet",
                    "command": ["echo", "1"],
                    "baseline_file": "quality/custom.json",
                    "parser": {"type": "regex", "pattern": "(.*)"},
                }
            }
        }
    )
    write_contract(tmp_path, contract)
    assert (
        handle_hook(
            tmp_path,
            "codex",
            "pre-tool",
            {
                "tool_name": "edit",
                "tool_input": {"file_path": "quality/custom.json"},
            },
        )["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )
    assert (
        handle_hook(
            tmp_path,
            "codex",
            "pre-tool",
            {
                "tool_name": "exec",
                "tool_input": {"cmd": "cat quality/custom.json"},
            },
        )
        == {}
    )


@pytest.mark.parametrize(
    "prerequisite",
    [
        command(phases=["ci"]),
        command(enabled=False),
    ],
)
def test_dependency_phase_mismatch_is_rejected(prerequisite):
    with pytest.raises(ValueError, match="every dependent phase"):
        Contract.model_validate(
            {
                "constraints": {
                    "first": prerequisite,
                    "dependent": command(needs=["first"]),
                }
            }
        )


def test_supervisor_lease_renews_without_another_cycle(tmp_path):
    with loop_lease(tmp_path, "review", ttl_seconds=0.6):
        path = lease_path(tmp_path, "review")
        first = json.loads(path.read_text())["expires_at"]
        deadline = time.monotonic() + 3
        while json.loads(path.read_text())["expires_at"] == first and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(path.read_text())["expires_at"] > first
        with (
            pytest.raises(LoopError, match="active supervisor"),
            loop_lease(
                tmp_path,
                "review",
                ttl_seconds=1,
                now=first + 0.01,
            ),
        ):
            pass
    assert not path.exists()


def test_lost_supervisor_heartbeat_fails_closed_and_preserves_new_owner(tmp_path):
    with (
        pytest.raises(LoopError, match="renewal failed"),
        loop_lease(
            tmp_path,
            "review",
            ttl_seconds=0.06,
        ) as renew,
    ):
        path = lease_path(tmp_path, "review")
        path.write_text(json.dumps({"token": "new-owner", "expires_at": time.time() + 10}))
        time.sleep(0.15)
        renew()
    assert json.loads(path.read_text())["token"] == "new-owner"


def test_supervisor_lease_requires_positive_ttl(tmp_path):
    with (
        pytest.raises(LoopError, match="positive"),
        loop_lease(
            tmp_path,
            "review",
            ttl_seconds=0,
        ),
    ):
        pass


def test_ci_loop_cannot_be_added_only_in_local_policy(tmp_path):
    write_contract(tmp_path, Contract.model_validate({"constraints": {"tests": command()}}))
    (tmp_path / "constraintloop.local.yml").write_text(
        yaml.safe_dump(
            {
                "loops": {"review": loop("ci")},
            }
        )
    )
    with pytest.raises(ValueError, match="committed contract"):
        load_loop_contract(tmp_path, "review")


def test_artifact_fields_and_parse_errors_are_redacted(tmp_path):
    marker = "SYNTHETIC_CREDENTIAL_123456"
    (tmp_path / "report.json").write_text(json.dumps({"nested": {"password": marker}}))
    contract = Contract.model_validate(
        {
            "constraints": {
                "artifact": {
                    "kind": "artifact",
                    "format": "json",
                    "path": "report.json",
                    "evidence": {"data": "nested"},
                }
            }
        }
    )
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert result.passed
    assert marker not in result.model_dump_json()


def test_collection_overflow_is_an_error_not_a_pass(tmp_path):
    contract = Contract.model_validate({"constraints": {"log": command("print('x' * 9000000)")}})
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert result.results[0].verdict == Verdict.ERROR
    assert "output exceeded" in result.results[0].message
