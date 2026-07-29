"""Idempotent hook installation for supported coding agents."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

ADAPTERS: dict[str, tuple[Path, dict[str, str]]] = {
    "claude": (
        Path(".claude/settings.json"),
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


def install_hooks(project_root: Path, adapter: str) -> Path:
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
    executable_path = Path(sys.argv[0]).resolve()
    if executable_path.name == "__main__.py":
        executable = f"{shlex.quote(sys.executable)} -m constraintloop"
    else:
        executable = shlex.quote(str(executable_path))
    project_argument = shlex.quote(str(project_root.resolve()))
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
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_settings(path, data)
    return path


def uninstall_hooks(project_root: Path, adapter: str) -> tuple[Path, int]:
    """Remove only ConstraintLoop hook entries and preserve all user settings."""
    relative, _ = ADAPTERS[adapter]
    path = project_root / relative
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return path, 0
    except json.JSONDecodeError as exc:
        raise ValueError(f"Refusing to modify invalid hook settings {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Refusing to modify non-object hook settings {path}")
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return path, 0

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
    return path, removed


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


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
