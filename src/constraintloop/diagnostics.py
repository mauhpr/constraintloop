"""Read-only deep diagnostics for project contracts."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from constraintloop.digest import matching_files
from constraintloop.hygiene import GITIGNORE_ENTRIES, is_path_ignored, tracked_state_files
from constraintloop.models import CommandConstraint, Contract, MetricConstraint

_ENV_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def deep_diagnostics(
    project_root: Path, contract: Contract, project_environment: dict[str, str]
) -> list[str]:
    issues: list[str] = []
    issues.extend(_repository_hygiene_issues(project_root))
    available = {**project_environment, **os.environ}
    for evaluator_id, evaluator in contract.evaluators.items():
        api_key_env = getattr(evaluator, "api_key_env", None)
        if api_key_env and api_key_env not in available:
            issues.append(
                f"evaluator {evaluator_id}: environment variable is missing: {api_key_env}"
            )
    for constraint_id, spec in contract.constraints.items():
        if spec.enabled and not matching_files(project_root, spec.watch):
            issues.append(f"constraint {constraint_id}: watch globs match no files")
        if not isinstance(spec, (CommandConstraint, MetricConstraint)):
            continue
        cwd = (project_root / spec.cwd).resolve()
        if not cwd.is_dir():
            issues.append(f"constraint {constraint_id}: cwd does not exist: {spec.cwd}")
            continue
        tokens = _tokens(spec.command)
        if not tokens:
            continue
        executable = tokens[0]
        if not spec.shell and _resolve_executable(cwd, executable) is None:
            issues.append(f"constraint {constraint_id}: executable not found: {executable}")
        joined = spec.command if isinstance(spec.command, str) else " ".join(spec.command)
        for match in _ENV_REFERENCE.finditer(joined):
            name = match.group(1) or match.group(2)
            if name not in available:
                issues.append(
                    f"constraint {constraint_id}: environment variable is missing: {name}"
                )
        issues.extend(_python_invocation_issues(constraint_id, cwd, tokens))
        issues.extend(_referenced_env_file_issues(constraint_id, cwd, tokens))
    return issues


def _repository_hygiene_issues(project_root: Path) -> list[str]:
    issues: list[str] = []
    try:
        ignored = {
            line.strip()
            for line in (project_root / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError:
        ignored = set()
    for entry in GITIGNORE_ENTRIES:
        if entry not in ignored and not is_path_ignored(project_root, entry):
            issues.append(f"repository hygiene: {entry} is not in .gitignore")
    tracked = tracked_state_files(project_root)
    if tracked:
        issues.append(
            "repository hygiene: .constraintloop/state contains tracked files: "
            + ", ".join(tracked[:5])
        )
    return issues


def _tokens(command: list[str] | str) -> list[str]:
    if isinstance(command, list):
        return command
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _resolve_executable(cwd: Path, executable: str) -> Path | None:
    candidate = Path(executable)
    if candidate.parent != Path("."):
        path = candidate if candidate.is_absolute() else cwd / candidate
        return path if path.is_file() else None
    found = shutil.which(executable)
    return Path(found) if found else None


def _python_invocation_issues(constraint_id: str, cwd: Path, tokens: list[str]) -> list[str]:
    if not tokens or not Path(tokens[0]).name.lower().startswith("python") or len(tokens) < 2:
        return []
    if tokens[1] == "-m" and len(tokens) >= 3:
        module = tokens[2]
        if not _module_exists_without_import(cwd, tokens[0], module):
            return [f"constraint {constraint_id}: Python module not found: {module}"]
    elif tokens[1].endswith(".py") and not (cwd / tokens[1]).is_file():
        return [f"constraint {constraint_id}: Python script not found: {tokens[1]}"]
    return []


def _module_exists_without_import(cwd: Path, executable: str, module: str) -> bool:
    if module.split(".", 1)[0] in sys.stdlib_module_names:
        return True
    module_parts = module.split(".")
    roots = [cwd]
    roots.extend(Path(item) for item in sys.path if item)
    executable_path = _resolve_executable(cwd, executable)
    if executable_path is not None:
        prefix = executable_path.resolve().parent.parent
        roots.extend(prefix.glob("lib/python*/site-packages"))
        roots.append(prefix / "Lib" / "site-packages")
    for root in roots:
        candidate = root.joinpath(*module_parts)
        if candidate.with_suffix(".py").is_file() or candidate.is_dir():
            return True
    return False


def _referenced_env_file_issues(constraint_id: str, cwd: Path, tokens: list[str]) -> list[str]:
    issues: list[str] = []
    for index, token in enumerate(tokens):
        candidate: str | None = None
        if token in {"--env-file", "--dotenv"} and index + 1 < len(tokens):
            candidate = tokens[index + 1]
        elif token.startswith("--env-file="):
            candidate = token.partition("=")[2]
        elif token in {".env", ".env.local"}:
            candidate = token
        if candidate and not (cwd / candidate).is_file():
            issues.append(
                f"constraint {constraint_id}: referenced environment file is missing: {candidate}"
            )
    return issues
