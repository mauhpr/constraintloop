"""Reject local runtime state and generated artifacts from a source distribution."""

from __future__ import annotations

import sys
import tarfile
from pathlib import PurePosixPath


def main() -> int:
    archive = sys.argv[1]
    forbidden_roots = {
        ".constraintloop",
        ".codex",
        ".claude",
        ".gemini",
        ".github",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "htmlcov",
    }
    forbidden_files = {"coverage.json", ".coverage"}
    violations: list[str] = []
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            parts = PurePosixPath(member.name).parts
            relative = parts[1:] if len(parts) > 1 else ()
            if not relative:
                continue
            if relative[0] in forbidden_roots or relative[-1] in forbidden_files:
                violations.append("/".join(relative))
    if violations:
        print("Forbidden sdist entries: " + ", ".join(sorted(violations)))
        return 1
    print("sdist content policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
