"""Content-addressed project snapshots and git context."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from constraintloop.models import ConstraintSpec, RatchetConstraint

_IGNORED_PARTS = {
    ".git",
    ".constraintloop",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.env",
}
_SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
    r"(\s*[:=]\s*[\"']?)([^\s,\"']{8,})"
)


def matching_files(project_root: Path, patterns: Iterable[str]) -> list[Path]:
    """Return stable, unique files matching glob patterns under the project."""
    found: dict[str, Path] = {}
    for pattern in patterns:
        for path in project_root.glob(pattern):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(project_root)
                path.resolve().relative_to(project_root.resolve())
            except ValueError:
                continue
            if any(part in _IGNORED_PARTS for part in relative.parts):
                continue
            found[relative.as_posix()] = path
    return [found[key] for key in sorted(found)]


def constraint_input_digest(
    project_root: Path,
    constraint_id: str,
    spec: ConstraintSpec,
    *,
    contract_digest: str | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(constraint_id.encode())
    if contract_digest is not None:
        digest.update(contract_digest.encode())
    digest.update(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    )
    if isinstance(spec, RatchetConstraint):
        baseline_path = (project_root / spec.baseline_file).resolve()
        try:
            baseline_path.relative_to(project_root.resolve())
            digest.update(baseline_path.read_bytes())
        except (OSError, ValueError) as exc:
            digest.update(f"<missing-baseline:{exc}>".encode())
    for path in matching_files(project_root, spec.watch):
        relative = path.relative_to(project_root).as_posix()
        digest.update(relative.encode())
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"<unreadable:{exc}>".encode())
    return digest.hexdigest()


def project_key(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()[:20]


def git_diff(
    project_root: Path,
    *,
    patterns: Iterable[str] = ("**/*",),
    limit: int | None = None,
) -> str:
    """Return staged, unstaged, and untracked changes as a bounded text bundle."""
    pattern_list = list(patterns)
    eligible = sorted(
        {
            relative
            for group in _changed_path_groups(project_root, include_untracked=True)
            if all(_allowed_disclosure_path(path, pattern_list) for path in group)
            for relative in group
        }
    )
    chunks: list[str] = []
    for args in (["git", "diff", "--no-ext-diff"], ["git", "diff", "--cached", "--no-ext-diff"]):
        if not eligible:
            continue
        try:
            result = subprocess.run(
                [*args, "--", *eligible],
                cwd=project_root,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.stdout:
            chunks.append(result.stdout)

    for relative in eligible:
        path = project_root / relative
        if not path.is_file() or _is_git_tracked(project_root, relative):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(
            f"diff --git a/{relative} b/{relative}\nnew file\n+++ b/{relative}\n{content}"
        )

    output = redact_text("\n".join(chunks))
    if limit is not None and len(output.encode()) > limit:
        return output.encode()[:limit].decode("utf-8", errors="ignore") + "\n[diff truncated]"
    return output


def changed_files(project_root: Path, *, include_untracked: bool = True) -> list[str]:
    return sorted(
        {
            relative
            for group in _changed_path_groups(project_root, include_untracked=include_untracked)
            for relative in group
        }
    )


def _changed_path_groups(project_root: Path, *, include_untracked: bool) -> list[tuple[str, ...]]:
    args = ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"]
    try:
        result = subprocess.run(
            args,
            cwd=project_root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    changed: list[tuple[str, ...]] = []
    entries = result.stdout.split("\0")
    index = 0
    while index < len(entries):
        line = entries[index]
        index += 1
        if len(line) < 4:
            continue
        status = line[:2]
        if status == "??" and not include_untracked:
            continue
        name = line[3:]
        if any(marker in {"R", "C"} for marker in status) and index < len(entries):
            previous_name = entries[index]
            index += 1
            changed.append((name, previous_name))
        else:
            changed.append((name,))
    return changed


def _allowed_disclosure_path(relative: str, patterns: list[str]) -> bool:
    if not is_disclosable_path(relative):
        return False
    path = PurePosixPath(relative)
    return any(
        path.match(pattern)
        or (pattern.startswith("**/") and path.match(pattern.removeprefix("**/")))
        for pattern in patterns
    )


def is_disclosable_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    name = path.name.lower()
    return not (
        name in _SECRET_NAMES
        or (name.startswith(".env.") and name != ".env.example")
        or path.suffix.lower() in _SECRET_SUFFIXES
    )


def redact_text(value: str) -> str:
    redacted = value
    for name, secret in os.environ.items():
        if (
            secret
            and len(secret) >= 8
            and any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        ):
            redacted = redacted.replace(secret, "[REDACTED]")
    return _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )


def _is_git_tracked(project_root: Path, relative: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=project_root,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
