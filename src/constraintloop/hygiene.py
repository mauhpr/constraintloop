"""Repository hygiene for local ConstraintLoop files."""

from __future__ import annotations

import subprocess
from pathlib import Path

GITIGNORE_ENTRIES = (
    ".constraintloop/state/",
    ".constraintloop/hooks-disabled.json",
    ".claude/settings.local.json",
    ".codex/hooks.json",
    ".gemini/settings.json",
    "constraintloop.local.yml",
    "constraintloop.local.yaml",
)


def ensure_local_files_ignored(project_root: Path) -> tuple[Path, list[str]]:
    """Add missing local-only paths to the selected project's .gitignore."""
    path = project_root / ".gitignore"
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        contents = ""
    existing = {
        line.strip()
        for line in contents.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [
        entry
        for entry in GITIGNORE_ENTRIES
        if entry not in existing and not is_path_ignored(project_root, entry)
    ]
    if missing:
        prefix = "" if not contents or contents.endswith("\n") else "\n"
        path.write_text(contents + prefix + "\n".join(missing) + "\n", encoding="utf-8")
    return path, missing


def is_path_ignored(project_root: Path, relative: str) -> bool:
    """Use Git's pattern semantics when available, including ignored parent directories."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", relative.rstrip("/")],
            cwd=project_root,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def tracked_state_files(project_root: Path) -> list[str]:
    """Return tracked state files relative to the selected project directory."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", ".constraintloop/state"],
            cwd=project_root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in result.stdout.splitlines() if line]
