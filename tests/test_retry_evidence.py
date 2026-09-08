from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest
import yaml
from click.testing import CliRunner

import constraintloop.runners as runners_module
from constraintloop.cli import main
from constraintloop.engine import ConstraintEngine
from constraintloop.models import CommandRetryPolicy, Contract, Phase
from constraintloop.state import (
    create_waiver,
    evidence_path,
    load_latest_result,
    result_evidence_digest,
    save_ratchet_baseline,
)


def _contract(command: list[str], **options) -> Contract:
    return Contract.model_validate(
        {
            "constraints": {
                "probe": {
                    "kind": "command",
                    "command": command,
                    "watch": ["input.txt"],
                    **options,
                },
            },
        }
    )


def test_restored_executable_recovers_cached_environment_error_and_dependents(tmp_path):
    tool = tmp_path / "restored-python"
    contract = _contract([str(tool), "-c", "print('recovered')"])
    contract = Contract.model_validate(
        {
            "constraints": {
                **contract.model_dump()["constraints"],
                "dependent": {
                    "kind": "command",
                    "command": [sys.executable, "-c", "pass"],
                    "needs": ["probe"],
                    "watch": ["input.txt"],
                },
            },
        }
    )
    first = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert [r.verdict.value for r in first.results] == ["error", "error"]
    assert all(r.failure_category.value == "environment" for r in first.results)
    assert first.results[1].blocked_by == ["probe"]

    # A 0.5.1 cache entry has no attempt history. It must also recover on upgrade.
    path = evidence_path(tmp_path)
    cached = json.loads(path.read_text())
    cached["probe"]["result"].pop("attempts")
    path.write_text(json.dumps(cached))
    tool.symlink_to(sys.executable)

    recovered = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert recovered.passed
    assert all(not r.cached for r in recovered.results)
    assert recovered.results[0].input_digest == first.results[0].input_digest
    reused = ConstraintEngine(tmp_path, contract).run(Phase.STOP)
    assert reused.passed
    assert all(r.cached for r in reused.results)


def test_environment_waiver_is_still_honored(tmp_path):
    contract = _contract([str(tmp_path / "missing")])
    engine = ConstraintEngine(tmp_path, contract)
    failure = engine.run(Phase.STOP).results[0]
    create_waiver(tmp_path, failure, engine.contract_digest, "local tooling unavailable")
    result = engine.run(Phase.STOP).results[0]
    assert result.verdict.value == "waived"
    assert result.cached


@pytest.mark.parametrize("final_code", [0, 1])
def test_refresh_replaces_evidence_but_no_cache_and_ci_leave_it_intact(tmp_path, final_code):
    # The return code represents external state, deliberately outside watch inputs.
    outcome = tmp_path / "outcome"
    outcome.write_text(str(1 - final_code))
    contract = _contract(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; raise SystemExit(int(Path('outcome').read_text()))",
        ]
    )
    engine = ConstraintEngine(tmp_path, contract)
    first = engine.run(Phase.STOP).results[0]
    saved = evidence_path(tmp_path).read_bytes()
    outcome.write_text(str(final_code))
    assert engine.run(Phase.STOP).results[0].cached
    for phase in (Phase.STOP, Phase.CI):
        fresh = ConstraintEngine(tmp_path, contract, use_cache=False).run(phase).results[0]
        assert fresh.exit_code == final_code
        assert not fresh.cached
        assert evidence_path(tmp_path).read_bytes() == saved
    ci = ConstraintEngine(tmp_path, contract, refresh_cache=True).run(Phase.CI).results[0]
    assert ci.exit_code == final_code
    assert evidence_path(tmp_path).read_bytes() == saved
    refreshed = ConstraintEngine(tmp_path, contract, refresh_cache=True).run(Phase.STOP).results[0]
    assert not refreshed.cached
    assert refreshed.exit_code == final_code
    assert refreshed.input_digest == first.input_digest
    assert engine.run(Phase.STOP).results[0].exit_code == final_code
    assert engine.run(Phase.STOP).results[0].cached


def test_cli_refresh_saves_fresh_json_evidence_and_rejects_no_cache(tmp_path):
    code = "from pathlib import Path; raise SystemExit(not Path('ready').exists())"
    contract = _contract([sys.executable, "-c", code])
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(contract.model_dump(mode="json")))
    runner = CliRunner()
    args = ["run", "--project", str(tmp_path), "--json"]
    assert runner.invoke(main, args).exit_code == 1
    saved = evidence_path(tmp_path).read_bytes()
    invalid = runner.invoke(main, [*args, "--refresh", "--no-cache"])
    assert invalid.exit_code == 2
    assert "cannot be used together" in invalid.output
    assert evidence_path(tmp_path).read_bytes() == saved
    (tmp_path / "ready").touch()
    assert runner.invoke(main, [*args, "--no-cache"]).exit_code == 0
    assert runner.invoke(main, args).exit_code == 1
    refreshed = runner.invoke(main, [*args, "--refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    assert not json.loads(refreshed.output)["results"][0]["cached"]
    reused = runner.invoke(main, args)
    assert reused.exit_code == 0
    assert json.loads(reused.output)["results"][0]["cached"]
    with pytest.raises(ValueError, match="requires use_cache"):
        ConstraintEngine(tmp_path, contract, use_cache=False, refresh_cache=True)


@pytest.mark.parametrize(
    ("kind", "final"),
    [
        (kind, final)
        for kind in ("command", "metric", "ratchet")
        for final in ("pass", "fail", "pending", "parse_error")
        if (kind, final) != ("command", "parse_error")
    ],
)
def test_every_attempt_survives_result_and_cache_roundtrip(tmp_path, kind, final):
    final_code = {"pass": 0, "fail": 125, "pending": 75, "parse_error": 0}[final]
    final_output = "invalid-json" if final == "parse_error" else '{"value": 5}'
    code = "\n".join(
        [
            "from pathlib import Path",
            "import sys",
            "marker = Path('attempt-marker')",
            "if not marker.exists():",
            "    marker.touch()",
            "    print('FIRST_FAILURE_OUTPUT')",
            "    print('FIRST_FAILURE_STDERR', file=sys.stderr)",
            "    sys.exit(125)",
            f"print({final_output!r})",
            f"sys.exit({final_code})",
        ]
    )
    options = {
        "kind": kind,
        "retry": {"max_attempts": 2, "exit_codes": [125], "delay_seconds": 0},
    }
    if kind != "command":
        options["parser"] = {"type": "json", "path": "value"}
        if kind == "metric":
            options["threshold"] = {"operator": "eq", "value": 5}
        else:
            options["baseline_file"] = "baseline.json"
            save_ratchet_baseline(tmp_path, "baseline.json", "probe", 5)
    contract = _contract([sys.executable, "-c", code], **options)
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    assert result.verdict.value == ("error" if final == "parse_error" else final)
    assert result.output_tail == final_output
    assert result.exit_code == final_code or final == "parse_error"
    assert [a.attempt for a in result.attempts] == [1, 2]
    assert [a.exit_code for a in result.attempts] == [125, final_code]
    assert result.attempts[0].output_tail == "FIRST_FAILURE_OUTPUT\nFIRST_FAILURE_STDERR"
    assert result.attempts[1].output_tail == final_output
    assert all(a.error is None and a.duration_ms > 0 for a in result.attempts)
    starts = [datetime.fromisoformat(a.started_at) for a in result.attempts]
    assert starts[0] <= starts[1]
    assert all(start.utcoffset().total_seconds() == 0 for start in starts)
    assert result.duration_ms >= sum(a.duration_ms for a in result.attempts)
    stored = load_latest_result(tmp_path, "probe")
    assert stored.attempts == result.attempts
    (tmp_path / "constraintloop.yml").write_text(yaml.safe_dump(contract.model_dump(mode="json")))
    debug = CliRunner().invoke(main, ["debug", "probe", "--project", str(tmp_path)])
    assert debug.exit_code == 0, debug.output
    assert "attempt 1: exit code 125" in debug.output
    assert "FIRST_FAILURE_OUTPUT" in debug.output
    assert "FIRST_FAILURE_STDERR" in debug.output
    assert f"attempt 2: exit code {final_code}" in debug.output


@pytest.mark.parametrize("kind", ["command", "metric", "ratchet"])
def test_start_error_then_pass_keeps_error_and_null_exit_code(tmp_path, monkeypatch, kind):
    real_run = runners_module.run_bounded
    calls = 0

    def initially_unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError("temporarily unavailable")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(runners_module, "run_bounded", initially_unavailable)
    options = {"kind": kind, "retry": {"exit_codes": [], "delay_seconds": 0}}
    if kind != "command":
        options["parser"] = {"type": "json", "path": "value"}
        if kind == "metric":
            options["threshold"] = {"operator": "eq", "value": 5}
        else:
            options["baseline_file"] = "baseline.json"
            save_ratchet_baseline(tmp_path, "baseline.json", "probe", 5)
    contract = _contract([sys.executable, "-c", "print('{\"value\":5}')"], **options)
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    assert result.verdict.value == "pass"
    assert len(result.attempts) == 2
    assert result.attempts[0].exit_code is None
    assert "temporarily unavailable" in result.attempts[0].error
    assert result.attempts[1].exit_code == 0


def test_timeout_retry_keeps_partial_output(tmp_path):
    contract = _contract(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "from pathlib import Path",
                    "import sys, time",
                    "marker = Path('started')",
                    "if not marker.exists():",
                    "    marker.touch()",
                    "    print('BEFORE_TIMEOUT', flush=True)",
                    "    print('TIMEOUT_STDERR', file=sys.stderr, flush=True)",
                    "    time.sleep(10)",
                    "print('RECOVERED')",
                ]
            ),
        ],
        timeout_seconds=0.3,
        retry={
            "exit_codes": [],
            "retry_timeouts": True,
            "delay_seconds": 0,
            "total_timeout_seconds": 3,
        },
    )
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    assert result.verdict.value == "pass"
    assert result.attempts[0].exit_code is None
    assert "timed out" in result.attempts[0].error
    assert result.attempts[0].output_tail == "BEFORE_TIMEOUT\nTIMEOUT_STDERR"
    assert result.attempts[1].output_tail == "RECOVERED"


def test_output_overflow_preserves_bounded_diagnostics_as_an_error(tmp_path):
    contract = _contract(
        [sys.executable, "-c", "print('x' * (9 * 1024 * 1024))"],
        retry={
            "exit_codes": [1],
            "delay_seconds": 0,
        },
    )
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    assert result.verdict.value == "error"
    assert "output exceeded" in result.message
    assert len(result.attempts) == 1
    assert result.attempts[0].error == result.message
    assert result.attempts[0].exit_code is None
    assert result.attempts[0].output_tail.endswith("x" * 1024)
    assert len(result.attempts[0].output_tail) <= 65536 + len("[output truncated]\n")


def test_retry_budget_does_not_invent_an_unstarted_attempt(tmp_path, monkeypatch):
    elapsed = 0.0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(runners_module.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(runners_module.time, "sleep", advance)
    contract = _contract(
        [sys.executable, "-c", "print('FAILURE'); raise SystemExit(125)"],
        timeout_seconds=1,
        retry={"exit_codes": [125], "delay_seconds": 2},
    )
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    assert "across retry attempts" in result.message
    assert len(result.attempts) == 1
    assert result.attempts[0].exit_code == 125
    assert result.attempts[0].output_tail == "FAILURE"


def test_attempt_output_is_bounded_and_redacted_before_persistence(tmp_path):
    secret = "sensitive-value-12345"
    code = f"print('password={secret}'); print('x' * 2000); raise SystemExit(125)"
    contract = _contract(
        [sys.executable, "-c", code],
        retry={
            "exit_codes": [125],
            "delay_seconds": 0,
        },
    )
    contract.settings.evidence_output_limit = 1024
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    assert len(result.attempts) == 2
    assert secret not in evidence_path(tmp_path).read_text()
    for attempt in result.attempts:
        assert attempt.output_tail.startswith("[output truncated]\n")
        assert len(attempt.output_tail.encode()) <= 1024 + len("[output truncated]\n")

    # Revalidate incoming evidence as well as locally generated output.
    payload = result.model_dump()
    payload["attempts"][0]["output_tail"] = f"password={secret}"
    payload["attempts"][0]["error"] = f"api_key={secret}"
    scrubbed = type(result).model_validate(payload)
    assert secret not in scrubbed.model_dump_json()


def test_explicit_infrastructure_retry_policy_does_not_retry_assertions(tmp_path):
    assert CommandRetryPolicy().exit_codes == [1]  # Existing compatibility default.
    contract = _contract(
        [sys.executable, "-c", "print('assertion failed'); raise SystemExit(1)"],
        retry={"exit_codes": [125], "delay_seconds": 0},
    )
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    assert result.verdict.value == "fail"
    assert len(result.attempts) == 1


def test_attempt_evidence_identity_ignores_timing_but_tracks_prior_failures(tmp_path):
    contract = _contract(
        [sys.executable, "-c", "raise SystemExit(1)"],
        retry={
            "exit_codes": [1],
            "delay_seconds": 0,
        },
    )
    result = ConstraintEngine(tmp_path, contract).run(Phase.STOP).results[0]
    original = result_evidence_digest(result)
    result.attempts[0].duration_ms += 1
    result.attempts[0].started_at = "2026-01-01T00:00:00+00:00"
    assert result_evidence_digest(result) == original
    result.attempts[0].output_tail = "new failure evidence"
    assert result_evidence_digest(result) != original
