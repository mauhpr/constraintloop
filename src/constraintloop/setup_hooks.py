"""Idempotent hook installation for supported coding agents."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from constraintloop import __version__

ADAPTERS: dict[str, tuple[Path, dict[str, str]]] = {
    "claude": (
        Path(".claude/settings.local.json"),
        {
            "SessionStart": "session-start",
            "UserPromptSubmit": "user-prompt",
            "PreToolUse": "pre-tool",
            "PostToolUse": "post-tool",
            "PreCompact": "pre-compact",
            "Stop": "stop",
        },
    ),
    "codex": (
        Path(".codex/hooks.json"),
        {
            "SessionStart": "session-start",
            "UserPromptSubmit": "user-prompt",
            "PreToolUse": "pre-tool",
            "PostToolUse": "post-tool",
            "PreCompact": "pre-compact",
            "Stop": "stop",
        },
    ),
    "gemini": (
        Path(".gemini/settings.json"),
        {
            "SessionStart": "session-start",
            "BeforeAgent": "user-prompt",
            "BeforeTool": "pre-tool",
            "AfterTool": "post-tool",
            "PreCompress": "pre-compact",
            "AfterAgent": "stop",
        },
    ),
}

LEGACY_HOOK_PATHS: dict[str, tuple[Path, ...]] = {
    "claude": (Path(".claude/settings.json"),),
}
_DISABLED_HOOKS_PATH = Path(".constraintloop/hooks-disabled.json")
_PRE_PUSH_MARKER = "# Managed by ConstraintLoop; reinstall with constraintloop setup --pre-push"


def install_hooks(project_root: Path, adapter: str, hook_executable: str | None = None) -> Path:
    relative, events = ADAPTERS[adapter]
    path = project_root / relative
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Refusing to overwrite invalid hook settings {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Refusing to overwrite non-object hook settings {path}")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Existing hooks value is not an object in {path}")
    executable = hook_executable or _portable_executable(project_root)
    project_argument = _portable_project_argument(project_root)
    for native_event, event in events.items():
        command = (
            f"{executable} hook --adapter {adapter} --event {event} --project {project_argument}"
        )
        groups = hooks.setdefault(native_event, [])
        _validate_event_groups(path, native_event, groups)
        if _has_or_update_command(groups, command, adapter, event):
            continue
        hook: dict[str, Any] = {
            "type": "command",
            "command": command,
            "statusMessage": f"ConstraintLoop: {event}",
        }
        if adapter == "gemini":
            hook["name"] = f"constraintloop-{event}"
        group: dict[str, Any] = {"hooks": [hook]}
        if native_event in {"PreToolUse", "PostToolUse", "BeforeTool", "AfterTool"}:
            group["matcher"] = (
                "Bash|Edit|Write"
                if adapter in {"claude", "codex"}
                else "run_shell_command|write_file|replace"
            )
        groups.append(group)
    for legacy in LEGACY_HOOK_PATHS.get(adapter, ()):
        _uninstall_from_path(project_root / legacy, adapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_settings(path, data)
    _set_hook_disabled(project_root, adapter, disabled=False)
    return path


def _portable_executable(project_root: Path) -> str:
    """Return a hook executable without embedding machine-specific paths."""
    executable_path = Path(sys.argv[0]).resolve()
    if executable_path.name == "__main__.py":
        return "python -m constraintloop"
    if _looks_like_uvx(executable_path):
        return f"uvx --from constraintloop=={__version__} constraintloop"
    git_root = _git_root(project_root)
    try:
        relative = executable_path.relative_to(git_root)
    except ValueError:
        return shlex.quote(executable_path.name)
    return f'"$(git rev-parse --show-toplevel)/{relative.as_posix()}"'


def _portable_project_argument(project_root: Path) -> str:
    git_root = _git_root(project_root)
    try:
        relative = project_root.resolve().relative_to(git_root)
    except ValueError:
        return shlex.quote(str(project_root.resolve()))
    suffix = "" if relative == Path(".") else f"/{relative.as_posix()}"
    return f'"$(git rev-parse --show-toplevel){suffix}"'


def _git_root(project_root: Path) -> Path:
    resolved = project_root.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def _looks_like_uvx(path: Path) -> bool:
    value = path.as_posix().lower()
    return "/uv/archive-" in value or "/.cache/uv/" in value or "/caches/uv/" in value


def uninstall_hooks(project_root: Path, adapter: str) -> tuple[Path, int]:
    """Remove owned entries and persist a local tombstone against restored wiring."""
    relative, _ = ADAPTERS[adapter]
    primary = project_root / relative
    removed = sum(
        _uninstall_from_path(project_root / candidate, adapter)
        for candidate in (relative, *LEGACY_HOOK_PATHS.get(adapter, ()))
    )
    _set_hook_disabled(project_root, adapter, disabled=True)
    return primary, removed


def hooks_disabled(project_root: Path, adapter: str) -> bool:
    """Return whether this adapter was explicitly uninstalled in the project."""
    path = project_root / _DISABLED_HOOKS_PATH
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, OSError):
        return True
    if not isinstance(data, dict):
        return True
    disabled = data.get("disabled") if isinstance(data, dict) else None
    return not isinstance(disabled, list) or adapter in disabled


def install_pre_push_hook(project_root: Path, hook_executable: str | None = None) -> Path:
    """Install an opt-in, repository-local pre-push gate."""
    path = _git_hooks_dir(project_root) / "pre-push"
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    if existing and _PRE_PUSH_MARKER not in existing:
        raise ValueError(
            f"Refusing to replace existing pre-push hook {path}; wire "
            "`constraintloop run --phase push` into the existing hook manually"
        )
    executable = hook_executable or _portable_executable(project_root)
    project_argument = _portable_project_argument(project_root)
    contents = (
        "#!/bin/sh\n"
        f"{_PRE_PUSH_MARKER}\n"
        f"{executable} run --phase push --project {project_argument}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def uninstall_pre_push_hook(project_root: Path) -> tuple[Path, bool]:
    """Remove the pre-push hook only when ConstraintLoop owns the whole file."""
    path = _git_hooks_dir(project_root) / "pre-push"
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path, False
    if _PRE_PUSH_MARKER not in contents:
        raise ValueError(f"Refusing to remove non-ConstraintLoop pre-push hook {path}")
    path.unlink()
    return path, True


def _uninstall_from_path(path: Path, adapter: str) -> int:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except json.JSONDecodeError as exc:
        raise ValueError(f"Refusing to modify invalid hook settings {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Refusing to modify non-object hook settings {path}")
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0

    marker = f" hook --adapter {adapter} "
    removed = 0
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        retained_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                retained_groups.append(group)
                continue
            retained_entries = []
            for entry in group["hooks"]:
                command = entry.get("command") if isinstance(entry, dict) else None
                if isinstance(command, str) and "constraintloop" in command and marker in command:
                    removed += 1
                else:
                    retained_entries.append(entry)
            if retained_entries:
                retained_group = dict(group)
                retained_group["hooks"] = retained_entries
                retained_groups.append(retained_group)
        if retained_groups:
            hooks[event] = retained_groups
        else:
            hooks.pop(event)
    if removed:
        _write_settings(path, data)
    return removed


def _set_hook_disabled(project_root: Path, adapter: str, *, disabled: bool) -> None:
    path = project_root / _DISABLED_HOOKS_PATH
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw_disabled = data.get("disabled", [])
    if not isinstance(raw_disabled, list):
        raw_disabled = []
    adapters = {item for item in raw_disabled if isinstance(item, str) and item in ADAPTERS}
    if disabled:
        adapters.add(adapter)
    else:
        adapters.discard(adapter)
    if adapters:
        _write_settings(path, {"schema_version": 1, "disabled": sorted(adapters)})
    else:
        with suppress(FileNotFoundError):
            path.unlink()


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    _write_text(path, json.dumps(data, indent=2) + "\n")


def _write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def _git_hooks_dir(project_root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=project_root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        path = Path(result.stdout.strip())
        return path if path.is_absolute() else (project_root / path).resolve()
    return _git_root(project_root) / ".git" / "hooks"


def _validate_event_groups(path: Path, event: str, groups: object) -> None:
    if not isinstance(groups, list):
        raise ValueError(f"Existing {event} hooks value is not a list in {path}")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError(f"Existing {event} hook group is invalid in {path}")
        if any(not isinstance(entry, dict) for entry in group["hooks"]):
            raise ValueError(f"Existing {event} hook entry is invalid in {path}")


def _has_or_update_command(groups: object, command: str, adapter: str, event: str) -> bool:
    if not isinstance(groups, list):
        return False
    marker = f" hook --adapter {adapter} --event {event}"
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            existing = hook.get("command")
            if existing == command:
                return True
            if isinstance(existing, str) and "constraintloop" in existing and marker in existing:
                hook["command"] = command
                return True
    return False
