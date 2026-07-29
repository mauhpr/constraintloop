"""Normalize agent hook payloads into one constraint lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from constraintloop.config import ContractError, load_contract
from constraintloop.engine import ConstraintEngine, blocking_results, format_summary
from constraintloop.loops import run_cycle
from constraintloop.models import LoopState, Phase, Verdict
from constraintloop.state import advisory_acknowledgment_reason, load_session, save_session


def handle_hook(
    project_root: Path,
    adapter: str,
    event: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session_id = str(
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("conversation_id")
        or "default"
    )
    state = load_session(project_root, session_id)

    if event == "user-prompt":
        prompt = payload.get("prompt") or payload.get("user_prompt")
        if not prompt and isinstance(payload.get("input"), str):
            prompt = payload["input"]
        if isinstance(prompt, str) and prompt.strip():
            if _is_hook_feedback(prompt):
                state["last_hook_feedback"] = prompt.strip()[-8000:]
                save_session(project_root, session_id, state)
                return _context_response(
                    adapter, event, "ConstraintLoop captured evaluator feedback."
                )
            state["goal"] = prompt.strip()[-8000:]
            save_session(project_root, session_id, state)
        return _context_response(adapter, event, "ConstraintLoop captured the task goal.")

    if event == "pre-tool":
        serialized = json.dumps(
            payload.get("tool_input", payload.get("toolInput", payload)),
            sort_keys=True,
        )
        if _protected_mutation(payload, serialized):
            return _deny_response(
                adapter,
                "Agent writes to protected quality policy or creates a local exception. "
                "Ask the human to make this change outside the agent session.",
            )
        return {}

    try:
        contract, _ = load_contract(project_root)
    except ContractError as exc:
        if event == "stop":
            return _block_response(adapter, str(exc))
        return _context_response(adapter, event, str(exc))

    if event == "session-start":
        required = [
            constraint_id
            for constraint_id, spec in contract.constraints.items()
            if spec.enabled and spec.enforcement.value == "required"
        ]
        return _context_response(
            adapter,
            event,
            "ConstraintLoop completion contract is active. Required gates: "
            + (", ".join(required) if required else "none")
            + ". Do not edit the contract or create waivers.",
        )

    if event == "post-tool":
        record = ConstraintEngine(
            project_root,
            contract,
            goal=state.get("goal"),
            agent_adapter=adapter,
        ).run(Phase.CHANGE)
        if record.results:
            return _context_response(adapter, event, format_summary(record))
        return {}

    if event == "pre-compact":
        return _context_response(
            adapter,
            event,
            "ConstraintLoop remains authoritative at completion. Run or repair all required "
            "stop gates before claiming the task is complete.",
        )

    if event != "stop":
        return {}

    record = ConstraintEngine(
        project_root,
        contract,
        goal=state.get("goal"),
        agent_adapter=adapter,
    ).run(Phase.STOP)
    failures = blocking_results(record)
    completion_loop = next(
        (loop_name for loop_name, config in contract.loops.items() if config.phase == Phase.STOP),
        None,
    )
    cycle = (
        run_cycle(
            project_root,
            contract,
            completion_loop,
            record=record,
            goal=state.get("goal"),
            agent_adapter=adapter,
        )
        if completion_loop is not None
        else None
    )
    advisories = [
        result
        for result in record.results
        if not result.blocks
        and result.verdict
        not in {
            Verdict.PASS,
            Verdict.SKIPPED,
            Verdict.WAIVED,
        }
    ]
    if not failures:
        state["attempts"] = 0
        state.pop("failed_snapshot", None)
        if advisories:
            snapshot = _result_snapshot(advisories)
            summary = format_summary(record, include_output=True)
            if state.get("advisory_feedback_snapshot") != snapshot:
                state["advisory_feedback_snapshot"] = snapshot
                save_session(project_root, session_id, state)
                return _block_response(
                    adapter,
                    summary
                    + "\nAdvisory feedback requires an agent disposition. Address it until fresh "
                    "evidence passes, or record why no change is appropriate with "
                    '`constraintloop acknowledge CONSTRAINT --reason "..."`, then try '
                    "completion again.",
                )
            reasons = {
                result.constraint_id: advisory_acknowledgment_reason(project_root, result)
                for result in advisories
            }
            missing = [constraint_id for constraint_id, reason in reasons.items() if not reason]
            if missing:
                return _block_response(
                    adapter,
                    summary
                    + "\nAdvisory feedback was delivered but has no snapshot-bound disposition "
                    f"for: {', '.join(missing)}. Address it or explicitly acknowledge it.",
                )
            save_session(project_root, session_id, state)
            return _allow_response(
                adapter,
                summary
                + "\nAdvisory feedback was explicitly acknowledged for this exact evidence: "
                + "; ".join(
                    f"{constraint_id}: {reason}" for constraint_id, reason in reasons.items()
                ),
            )
        state.pop("advisory_feedback_snapshot", None)
        save_session(project_root, session_id, state)
        return _allow_response(adapter, format_summary(record, include_output=True))

    if cycle is not None:
        summary = format_summary(record, include_output=True)
        detail = (
            f"\nLoop {cycle.loop}: {cycle.state.value}; "
            f"repair attempt {cycle.repair_attempt}. {cycle.next_action}"
        )
        if cycle.state in {LoopState.REPAIR, LoopState.WAITING}:
            return _block_response(adapter, summary + detail)
        return _human_required_response(adapter, summary + detail)

    snapshot = _result_snapshot(failures)
    attempts = int(state.get("attempts", 0)) + 1 if state.get("failed_snapshot") == snapshot else 1
    state["attempts"] = attempts
    state["failed_snapshot"] = snapshot
    save_session(project_root, session_id, state)
    summary = format_summary(record, include_output=True)
    if attempts <= contract.settings.max_auto_retries:
        return _block_response(
            adapter,
            summary + f"\nRepair the failures and try again "
            f"({attempts}/{contract.settings.max_auto_retries} automatic retries).",
        )
    return _human_required_response(
        adapter,
        summary + "\nThe same evidence failed repeatedly. Stop automatic repair and ask a human "
        "to fix the implementation, revise the contract, or create a local waiver.",
    )


def _result_snapshot(results: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            [(item.constraint_id, item.input_digest, item.verdict.value) for item in results],
            sort_keys=True,
        ).encode()
    ).hexdigest()


_NATIVE_EVENTS = {
    "claude": {
        "session-start": "SessionStart",
        "user-prompt": "UserPromptSubmit",
        "post-tool": "PostToolUse",
        "pre-compact": "PreCompact",
        "stop": "Stop",
    },
    "codex": {
        "session-start": "SessionStart",
        "user-prompt": "UserPromptSubmit",
        "post-tool": "PostToolUse",
        "pre-compact": "PreCompact",
        "stop": "Stop",
    },
    "gemini": {
        "session-start": "SessionStart",
        "user-prompt": "BeforeAgent",
        "post-tool": "AfterTool",
        "pre-compact": "PreCompress",
        "stop": "AfterAgent",
    },
}


def _context_response(adapter: str, event: str, text: str) -> dict[str, Any]:
    native_event = _NATIVE_EVENTS[adapter].get(event)
    if event == "pre-compact" and adapter in {"claude", "codex"}:
        return {"systemMessage": text}
    if native_event is None:
        return {"systemMessage": text}
    return {
        "hookSpecificOutput": {
            "hookEventName": native_event,
            "additionalContext": text,
        }
    }


def _deny_response(adapter: str, reason: str) -> dict[str, Any]:
    if adapter == "gemini":
        return {"decision": "deny", "reason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _block_response(adapter: str, reason: str) -> dict[str, Any]:
    if adapter == "gemini":
        return {"decision": "deny", "reason": reason}
    return {"decision": "block", "reason": reason}


def _allow_response(adapter: str, summary: str) -> dict[str, Any]:
    if adapter == "gemini":
        return {"decision": "allow", "systemMessage": summary}
    return {"continue": True, "systemMessage": summary}


def _human_required_response(adapter: str, reason: str) -> dict[str, Any]:
    if adapter == "gemini":
        return {"decision": "deny", "reason": reason}
    return {"continue": False, "stopReason": reason, "systemMessage": reason}


def _is_hook_feedback(prompt: str) -> bool:
    stripped = prompt.strip()
    return stripped.startswith("<hook_prompt ") and stripped.endswith("</hook_prompt>")


def _protected_mutation(payload: dict[str, Any], serialized: str) -> bool:
    protected = (
        "constraintloop." + "yml",
        "constraintloop." + "yaml",
        ".constraintloop/" + "secrets.env",
    )
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "").lower()
    tool_input = payload.get("tool_input", payload.get("toolInput", payload))
    if not isinstance(tool_input, dict):
        tool_input = {}
    text_values = [value for value in tool_input.values() if isinstance(value, str)]
    if "patch" in tool_name or any(value.startswith("*** Begin Patch") for value in text_values):
        headers = re.findall(
            r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$",
            "\n".join(text_values),
            re.MULTILINE,
        )
        return any(any(name in header for name in protected) for header in headers)
    command = tool_input.get("cmd") or tool_input.get("command")
    if isinstance(command, str):
        forbidden_subcommand = "constraintloop " + "waive"
        if forbidden_subcommand in command:
            return True
        mutator = re.search(
            r"(^|[;&|]\s*)(rm|mv|cp|tee|touch|truncate|sed\s+-i|perl\s+-i)\b|[>]",
            command,
        )
        if mutator and any(name in command for name in protected):
            return True
    if any("*** " in value for value in text_values):
        headers = re.findall(
            r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$",
            "\n".join(text_values),
            re.MULTILINE,
        )
        return any(any(name in header for name in protected) for header in headers)
    if any(marker in tool_name for marker in ("write", "edit", "delete", "move")):
        path_values = [
            value
            for key, value in tool_input.items()
            if key.lower() in {"path", "file_path", "filepath", "destination", "target"}
            and isinstance(value, str)
        ]
        return any(any(name in value for name in protected) for value in path_values)
    return False
