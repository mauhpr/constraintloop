"""Load project-local secrets without evaluating shell code."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def project_environment_path(project_root: Path) -> Path:
    override = os.environ.get("CONSTRAINTLOOP_ENV_FILE")
    if override:
        return Path(os.path.abspath(Path(override).expanduser()))
    return project_root.resolve() / ".constraintloop" / "secrets.env"


def load_project_environment(project_root: Path) -> dict[str, str]:
    """Parse project-local variables without mutating the process environment."""
    path = project_environment_path(project_root)
    if path.exists():
        if path.is_symlink():
            raise ValueError(f"Environment file must not be a symlink: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ValueError(f"Environment file permissions must be 0600 or stricter: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    loaded: dict[str, str] = {}
    for number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            raise ValueError(f"Invalid environment entry at {path}:{number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value and key not in os.environ:
            loaded[key] = value
    return loaded
