"""ConstraintLoop command-line interface."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

import click

from constraintloop.config import (
    ContractError,
    contract_digest,
    load_contract,
)
from constraintloop.digest import constraint_input_digest
from constraintloop.engine import ConstraintEngine, format_summary
from constraintloop.environment import load_project_environment, project_environment_path
from constraintloop.hooks import handle_hook
from constraintloop.loops import CYCLE_EXIT_CODES, LoopError, loop_prompt, run_cycle, supervise
from constraintloop.models import (
    CommandEvaluatorConfig,
    Enforcement,
    LoopState,
    Phase,
    RubricConstraint,
    Verdict,
)
from constraintloop.scaffold import (
    authoring_proposal,
    enhancement_proposal,
    write_initial_contract,
    write_proposal,
)
from constraintloop.setup_hooks import ADAPTERS, install_hooks, uninstall_hooks
from constraintloop.state import (
    create_advisory_acknowledgment,
    create_waiver,
    load_cached_result,
    load_latest_result,
)


def _root(value: Path) -> Path:
    return value.expanduser().resolve()


@click.group()
@click.version_option()
def main() -> None:
    """Evidence-based completion gates for AI coding agents."""


@main.command("init")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
@click.option("--force", is_flag=True, help="Replace an existing contract.")
def init_command(project: Path, force: bool) -> None:
    """Generate an explicit contract from detected project tooling."""
    root = _root(project)
    path = root / "constraintloop.yml"
    if path.exists() and not force:
        raise click.ClickException(f"{path} already exists; use --force to replace it")
    path = write_initial_contract(root)
    click.echo(f"Created {path}")


@main.command("setup")
@click.option(
    "--adapter",
    type=click.Choice([*ADAPTERS, "all"]),
    default="all",
    show_default=True,
)
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def setup_command(adapter: str, project: Path) -> None:
    """Install idempotent Claude, Codex, and Gemini hook entries."""
    root = _root(project)
    adapters = list(ADAPTERS) if adapter == "all" else [adapter]
    for name in adapters:
        try:
            path = install_hooks(root, name)
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"Could not install {name} hooks: {exc}") from exc
        click.echo(f"Updated {path}")


@main.command("uninstall")
@click.option(
    "--adapter",
    type=click.Choice([*ADAPTERS, "all"]),
    default="all",
    show_default=True,
)
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def uninstall_command(adapter: str, project: Path) -> None:
    """Remove ConstraintLoop hooks while preserving unrelated settings."""
    root = _root(project)
    adapters = list(ADAPTERS) if adapter == "all" else [adapter]
    for name in adapters:
        try:
            path, removed = uninstall_hooks(root, name)
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"Could not remove {name} hooks: {exc}") from exc
        click.echo(f"Removed {removed} ConstraintLoop hook(s) from {path}")


def _run_phase(project: Path, phase: Phase, json_output: bool, no_cache: bool) -> None:
    root = _root(project)
    try:
        contract, _ = load_contract(root)
    except ContractError as exc:
        raise click.ClickException(str(exc)) from exc
    record = ConstraintEngine(
        root,
        contract,
        use_cache=not no_cache and phase != Phase.CI,
        allow_waivers=phase != Phase.CI,
    ).run(phase)
    click.echo(
        record.model_dump_json(indent=2)
        if json_output
        else format_summary(record, include_output=True)
    )
    if not record.passed:
        raise click.exceptions.Exit(1)


@main.command("run")
@click.option("--phase", type=click.Choice(["change", "stop"]), default="stop")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
@click.option("--json", "json_output", is_flag=True)
@click.option("--no-cache", is_flag=True)
def run_command(phase: str, project: Path, json_output: bool, no_cache: bool) -> None:
    """Run local gates for a lifecycle phase."""
    _run_phase(project, Phase(phase), json_output, no_cache)


@main.command("ci")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
@click.option("--json", "json_output", is_flag=True)
def ci_command(project: Path, json_output: bool) -> None:
    """Run authoritative gates, ignoring local cache and waivers."""
    _run_phase(project, Phase.CI, json_output, True)


@main.command("cycle")
@click.argument("loop_name")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
@click.option("--json", "json_output", is_flag=True)
def cycle_command(loop_name: str, project: Path, json_output: bool) -> None:
    """Execute exactly one convergence-loop transition."""
    root = _root(project)
    try:
        contract, _ = load_contract(root)
        result = run_cycle(root, contract, loop_name)
    except (ContractError, LoopError) as exc:
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "schema_version": 1,
                        "loop": loop_name,
                        "state": "error",
                        "message": str(exc),
                        "next_action": "Inspect the loop configuration or state.",
                    },
                    sort_keys=True,
                )
            )
        else:
            click.echo(f"error: {exc}", err=True)
        raise click.exceptions.Exit(CYCLE_EXIT_CODES[LoopState.ERROR]) from exc
    click.echo(
        result.model_dump_json() if json_output else f"{result.state.value}: {result.next_action}"
    )
    raise click.exceptions.Exit(CYCLE_EXIT_CODES[result.state])


@main.command("supervise")
@click.argument("loop_name")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def supervise_command(loop_name: str, project: Path) -> None:
    """Poll a loop under a single-writer lease and emit JSON Lines."""
    root = _root(project)
    try:
        contract, _ = load_contract(root)
        final = None
        for result in supervise(root, contract, loop_name):
            final = result
            click.echo(result.model_dump_json())
    except (ContractError, LoopError) as exc:
        click.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "loop": loop_name,
                    "state": "error",
                    "message": str(exc),
                    "next_action": "Inspect the loop configuration or state.",
                },
                sort_keys=True,
            )
        )
        raise click.exceptions.Exit(CYCLE_EXIT_CODES[LoopState.ERROR]) from exc
    if final is not None:
        raise click.exceptions.Exit(CYCLE_EXIT_CODES[final.state])


@main.command("loop-prompt")
@click.argument("loop_name")
@click.option("--adapter", type=click.Choice(["claude", "codex"]), required=True)
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def loop_prompt_command(loop_name: str, adapter: str, project: Path) -> None:
    """Print a provider-neutral bounded-loop prompt for a native agent."""
    root = _root(project)
    try:
        contract, _ = load_contract(root)
    except ContractError as exc:
        raise click.ClickException(str(exc)) from exc
    if loop_name not in contract.loops:
        raise click.ClickException(f"Unknown loop {loop_name!r}")
    click.echo(loop_prompt(loop_name, adapter))


@main.command("status")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def status_command(project: Path) -> None:
    """Show fresh cached evidence without executing commands."""
    root = _root(project)
    try:
        contract, _ = load_contract(root)
    except ContractError as exc:
        raise click.ClickException(str(exc)) from exc
    identity = contract_digest(contract)
    for constraint_id, spec in contract.constraints.items():
        digest = constraint_input_digest(root, constraint_id, spec, contract_digest=identity)
        result = (
            load_latest_result(root, constraint_id)
            if isinstance(spec, RubricConstraint)
            else load_cached_result(root, constraint_id, digest)
        )
        if result is not None and result.input_digest != digest:
            result = None
        if result is None:
            click.echo(f"STALE {constraint_id}: no evidence for current inputs")
        else:
            click.echo(f"{result.verdict.value.upper()} {constraint_id}: {result.message}")


@main.command("doctor")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def doctor_command(project: Path) -> None:
    """Validate the contract and report its deterministic identity."""
    root = _root(project)
    try:
        contract, path = load_contract(root)
    except ContractError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"OK {path}")
    click.echo(f"contract digest: {contract_digest(contract)}")
    click.echo(f"constraints: {len(contract.constraints)}; evaluators: {len(contract.evaluators)}")
    try:
        project_environment = load_project_environment(root)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for evaluator_id, config in contract.evaluators.items():
        api_key_env = getattr(config, "api_key_env", None)
        if api_key_env:
            status = (
                "configured"
                if os.environ.get(api_key_env) or project_environment.get(api_key_env)
                else "missing"
            )
            click.echo(f"evaluator {evaluator_id}: {api_key_env} {status}")
    click.echo(f"local secrets file: {project_environment_path(root)}")


def _resolve_executable(root: Path, command: str) -> Path | None:
    candidate = Path(command)
    if candidate.parent != Path("."):
        resolved = candidate if candidate.is_absolute() else root / candidate
        return resolved.resolve() if resolved.is_file() else None
    found = shutil.which(command)
    return Path(found).resolve() if found else None


@main.command("debug")
@click.argument("constraint_id")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def debug_command(constraint_id: str, project: Path) -> None:
    """Explain cached evidence and evaluator availability without running gates."""
    root = _root(project)
    try:
        contract, _ = load_contract(root)
    except ContractError as exc:
        raise click.ClickException(str(exc)) from exc
    if constraint_id not in contract.constraints:
        raise click.ClickException(f"Unknown constraint {constraint_id!r}")

    spec = contract.constraints[constraint_id]
    identity = contract_digest(contract)
    digest = constraint_input_digest(root, constraint_id, spec, contract_digest=identity)
    click.echo(f"constraint: {constraint_id} ({spec.kind})")
    click.echo(f"current input digest: {digest}")

    latest = load_latest_result(root, constraint_id)
    if latest is None:
        click.echo("evidence: MISSING")
    else:
        freshness = "FRESH" if latest.input_digest == digest else "STALE"
        click.echo(f"evidence: {freshness} ({latest.verdict.value.upper()})")
        click.echo(f"last input digest: {latest.input_digest}")
        click.echo(f"message: {latest.message}")
        if latest.output_tail:
            click.echo("output tail:")
            click.echo(latest.output_tail)
        if latest.evaluator_calls:
            click.echo("evaluator calls:")
            for call in latest.evaluator_calls:
                click.echo(
                    f"- {call.provider}/{call.model}: {call.status}; "
                    f"{call.attempts} attempt(s); {call.duration_ms:.0f}ms"
                )

    if not isinstance(spec, RubricConstraint):
        click.echo("evaluator: none (deterministic constraint)")
        return
    evaluator = contract.evaluators[spec.evaluator]
    click.echo(f"evaluator: {spec.evaluator} ({evaluator.type})")
    if not isinstance(evaluator, CommandEvaluatorConfig):
        api_key_env = getattr(evaluator, "api_key_env", None)
        if api_key_env:
            availability = "SET" if os.environ.get(api_key_env) else "MISSING"
            click.echo(f"credential environment: {api_key_env} {availability}")
        return

    if isinstance(evaluator.command, str):
        click.echo(f"command: {evaluator.command}")
        click.echo("executable: shell command; resolution deferred to the shell")
    elif not evaluator.command:
        click.echo("command: EMPTY")
    else:
        click.echo(f"command: {shlex.join(evaluator.command)}")
        executable_path = _resolve_executable(root, evaluator.command[0])
        click.echo(f"executable: {executable_path if executable_path else 'NOT FOUND'}")
    for adapter in ("codex", "claude"):
        native_path = shutil.which(adapter)
        click.echo(f"native CLI {adapter}: {native_path or 'NOT FOUND'}")
    click.echo("debug mode did not execute the evaluator or consume model quota")


@main.command("acknowledge")
@click.argument("constraint_id")
@click.option("--reason", required=True)
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def acknowledge_command(constraint_id: str, reason: str, project: Path) -> None:
    """Record an agent disposition for exact advisory evidence without waiving it."""
    root = _root(project)
    try:
        contract, _ = load_contract(root)
    except ContractError as exc:
        raise click.ClickException(str(exc)) from exc
    if constraint_id not in contract.constraints:
        raise click.ClickException(f"Unknown constraint {constraint_id!r}")
    spec = contract.constraints[constraint_id]
    if spec.enforcement != Enforcement.ADVISORY:
        raise click.ClickException("Only advisory constraints may be acknowledged")
    digest = constraint_input_digest(
        root,
        constraint_id,
        spec,
        contract_digest=contract_digest(contract),
    )
    result = load_latest_result(root, constraint_id)
    if result is None or result.input_digest != digest:
        raise click.ClickException("No fresh evidence exists for this advisory constraint")
    if result.verdict in {Verdict.PASS, Verdict.SKIPPED, Verdict.WAIVED}:
        raise click.ClickException(f"Advisory result is already {result.verdict.value}")
    explanation = reason.strip()
    if not explanation:
        raise click.ClickException("Acknowledgment reason must not be empty")
    create_advisory_acknowledgment(root, result, explanation)
    click.echo(
        f"Acknowledged {constraint_id} for exact evidence {result.input_digest[:12]}; "
        "the verdict remains advisory and unchanged."
    )


@main.command("waive")
@click.argument("constraint_id")
@click.option("--reason", required=True)
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def waive_command(constraint_id: str, reason: str, project: Path) -> None:
    """Waive fresh non-passing local evidence for the exact current inputs."""
    root = _root(project)
    try:
        contract, _ = load_contract(root)
    except ContractError as exc:
        raise click.ClickException(str(exc)) from exc
    if constraint_id not in contract.constraints:
        raise click.ClickException(f"Unknown constraint {constraint_id!r}")
    spec = contract.constraints[constraint_id]
    if isinstance(spec, RubricConstraint):
        raise click.ClickException(
            "Rubric constraints cannot be waived; acknowledge advisory evidence "
            "or obtain fresh quorum"
        )
    explanation = reason.strip()
    if not explanation:
        raise click.ClickException("Waiver reason must not be empty")
    digest = constraint_input_digest(
        root,
        constraint_id,
        spec,
        contract_digest=contract_digest(contract),
    )
    result = load_cached_result(root, constraint_id, digest)
    if result is None:
        raise click.ClickException(
            "No fresh evidence exists; run the constraint before deciding whether to waive it"
        )
    if result.verdict in {Verdict.PASS, Verdict.SKIPPED, Verdict.WAIVED}:
        raise click.ClickException(f"Constraint result is already {result.verdict.value}")
    create_waiver(root, result, contract_digest(contract), explanation)
    click.echo(f"Waived {constraint_id} locally for input {digest[:12]}")
    click.echo("CI ignores local waivers.")


@main.command("hook", hidden=True)
@click.option("--adapter", type=click.Choice(list(ADAPTERS)), required=True)
@click.option(
    "--event",
    type=click.Choice(
        ["session-start", "user-prompt", "pre-tool", "post-tool", "pre-compact", "stop"]
    ),
    required=True,
)
@click.option("--project", type=click.Path(path_type=Path), required=True)
def hook_command(adapter: str, event: str, project: Path) -> None:
    """Process one native agent hook payload from stdin."""
    try:
        raw = sys.stdin.read()
        decoded: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        click.echo(json.dumps({"continue": False, "stopReason": f"Invalid hook JSON: {exc}"}))
        return
    if not isinstance(decoded, dict):
        click.echo(
            json.dumps(
                {
                    "continue": False,
                    "stopReason": "Invalid hook JSON: top-level value must be an object",
                }
            )
        )
        return
    payload: dict[str, Any] = decoded
    response = handle_hook(_root(project), adapter, event, payload)
    click.echo(json.dumps(response))


@main.command("enhance")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def enhance_command(project: Path) -> None:
    """Propose stronger tools and gates without installing or enabling them."""
    root = _root(project)
    path = write_proposal(root, "enhance", enhancement_proposal(root))
    click.echo(f"Wrote review-only proposal {path}")


@main.command("author")
@click.option("--project", type=click.Path(path_type=Path), default=Path("."))
def author_command(project: Path) -> None:
    """Create a review-only test-authoring proposal."""
    root = _root(project)
    path = write_proposal(root, "author", authoring_proposal(root))
    click.echo(f"Wrote review-only proposal {path}")


if __name__ == "__main__":
    main()
