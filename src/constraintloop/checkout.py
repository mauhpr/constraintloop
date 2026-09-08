"""Git checkout identity without following a shared repository's main worktree."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Checkout:
    project_root: str
    worktree_root: str | None = None
    git_dir: str | None = None
    branch: str | None = None
    head: str | None = None

    def snapshot(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def state_key(self) -> str | None:
        """Keep budgets across commits on a branch, separate detached checkouts."""
        if self.git_dir is None:
            return None
        identity = (self.project_root, self.git_dir, self.branch or self.head)
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20]


def ensure_checkout_unchanged(project_root: Path, expected: Checkout) -> None:
    if checkout_context(project_root) != expected:
        raise ValueError(
            "Git checkout changed during evaluation; evidence cannot authorize completion. "
            "Run gates again in a dedicated worktree for this task."
        )


def checkout_context(project_root: Path) -> Checkout:
    root = project_root.expanduser().resolve()
    # A .git file marks linked worktrees and submodules just as a directory marks
    # a regular checkout. Projects outside Git keep their existing local scope.
    if not any((candidate / ".git").exists() for candidate in (root, *root.parents)):
        return Checkout(project_root=str(root))

    # Git hooks can export the originating checkout's repository paths. The
    # explicit project directory must remain authoritative for our inspection.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_NAMESPACE",
        }
    }

    def git(*args: str, optional: bool = False) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                env=environment,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"Could not inspect Git checkout in {root}") from exc
        if result.returncode != 0:
            if optional and result.returncode == 1:
                return None
            raise ValueError(f"Could not inspect Git checkout in {root}")
        return result.stdout.strip()

    worktree = git("rev-parse", "--show-toplevel")
    git_dir = git("rev-parse", "--absolute-git-dir")
    branch = git("symbolic-ref", "--quiet", "HEAD", optional=True)
    head = git("rev-parse", "--verify", "--quiet", "HEAD", optional=True)
    return Checkout(str(root), worktree, git_dir, branch, head)
