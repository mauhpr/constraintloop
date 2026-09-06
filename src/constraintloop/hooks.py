"""Normalize agent hook payloads into one constraint lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

from constraintloop.config import ContractError, load_contract
from constraintloop.engine import ConstraintEngine, blocking_results, format_summary
from constraintloop.loops import LoopError, run_cycle
from constraintloop.models import EvidenceRecord, LoopState, Phase, RatchetConstraint, Verdict
from constraintloop.redaction import redact_text, redact_value
from constraintloop.setup_hooks import hooks_disabled
from constraintloop.state import advisory_acknowledgment_reason, load_session, save_session


def handle_hook(
    project_root: Path,
    adapter: str,
    event: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Always return protocol-valid, fail-closed output on unexpected hook errors."""
    try:
        return cast(
            dict[str, Any], redact_value(_handle_hook(project_root, adapter, event, payload))
        )
    except Exception as exc:
        reason = redact_text(
            f"ConstraintLoop could not safely evaluate {event}: {exc}. Ask a human."
        )
        if event == "pre-tool":
            return _deny_response(adapter, reason)
        return _human_required_response(adapter, reason)


def _handle_hook(
    project_root: Path,
    adapter: str,
    event: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if hooks_disabled(project_root, adapter):
        return {}

    # Project hooks also run inside subagents, whose files may be intentionally
    # incomplete. Main-thread gates own lifecycle evaluation.
    if event in {"post-tool", "stop"} and (
        payload.get("agent_id") is not None or payload.get("agentId") is not None
    ):
        return {}

    # A turn paused for background work or a scheduled wakeup is not a final
    # completion boundary. Evaluate when the main agent yields with no work in flight.
    if event == "stop" and any(
        isinstance(payload.get(field), list) and payload[field]
        for field in ("background_tasks", "backgroundTasks", "session_crons", "sessionCrons")
    ):
        return {}

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
        if _protected_mutation(payload, serialized, project_root):
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
            return _human_required_response(adapter, str(exc))
        return _context_response(adapter, event, str(exc))

    challenge_loop = next(
        (
            name
            for name, config in contract.loops.items()
            if config.phase == Phase.STOP and config.challenge is not None
        ),
        None,
    )
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
            + ". Do not edit the contract or create waivers."
            + (
                f" Session challenge gate {challenge_loop} is required. Before completion, "
                f"run `constraintloop cycle {challenge_loop} --json` and follow next_action "
                "in this same session."
                if challenge_loop
                else ""
            ),
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
            "stop gates before claiming the task is complete."
            + (
                f" Resume saved challenge work with `constraintloop cycle {challenge_loop} "
                "--json`; follow next_action in this session and preserve the recorded scenarios."
                if challenge_loop
                else ""
            ),
        )

    if event != "stop":
        return {}

    completion_loop = next(
        (loop_name for loop_name, config in contract.loops.items() if config.phase == Phase.STOP),
        None,
    )
    records: list[EvidenceRecord] = []
    try:
        cycle = (
            run_cycle(
                project_root,
                contract,
                completion_loop,
                goal=state.get("goal"),
                agent_adapter=adapter,
                continuation=True,
                on_record=records.append,
            )
            if completion_loop is not None
            else None
        )
    except (LoopError, OSError) as exc:
        return _human_required_response(
            adapter, f"ConstraintLoop could not evaluate the loop: {exc}"
        )
    record = (
        records[0]
        if records
        else ConstraintEngine(
            project_root,
            contract,
            goal=state.get("goal"),
            agent_adapter=adapter,
            refresh_pending=True,
        ).run(Phase.STOP)
    )
    failures = blocking_results(record)
    if challenge_loop is not None:
        state["challenge_loop_active"] = challenge_loop
        save_session(project_root, session_id, state)
    # Loop-owned challenge work can block even when every ordinary gate passes.
    if cycle is not None and cycle.state != LoopState.PASSED:
        summary = format_summary(
            record, include_output=True, output_limit=contract.settings.hook_output_limit
        )
        detail = (
            f"\nLoop {cycle.loop}: {cycle.state.value}; "
            f"repair attempt {cycle.repair_attempt}. {cycle.next_action}"
        )
        if cycle.state in {
            LoopState.REPAIR,
            LoopState.WAITING,
            LoopState.CHALLENGE,
            LoopState.VERIFY,
        }:
            return _block_response(adapter, summary + detail)
        return _human_required_response(adapter, summary + detail)
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
        if advisories:
            snapshot = _result_snapshot(advisories)
            summary = format_summary(
                record,
                include_output=True,
                output_limit=contract.settings.hook_output_limit,
            )
            if state.get("advisory_feedback_snapshot") != snapshot:
                state["advisory_feedback_snapshot"] = snapshot
                return _bounded_retry(
                    project_root,
                    session_id,
                    state,
                    contract.settings.max_auto_retries,
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
                return _bounded_retry(
                    project_root,
                    session_id,
                    state,
                    contract.settings.max_auto_retries,
                    adapter,
                    summary
                    + "\nAdvisory feedback was delivered but has no snapshot-bound disposition "
                    f"for: {', '.join(missing)}. Address it or explicitly acknowledge it.",
                )
            state["attempts"] = 0
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
        state["attempts"] = 0
        save_session(project_root, session_id, state)
        return _allow_response(
            adapter,
            format_summary(
                record,
                include_output=True,
                output_limit=contract.settings.hook_output_limit,
            ),
        )

    summary = format_summary(
        record,
        include_output=True,
        output_limit=contract.settings.hook_output_limit,
    )
    return _bounded_retry(
        project_root,
        session_id,
        state,
        contract.settings.max_auto_retries,
        adapter,
        summary + "\nRepair the failures and try again.",
    )


def _bounded_retry(
    project_root: Path,
    session_id: str,
    state: dict[str, Any],
    limit: int,
    adapter: str,
    reason: str,
) -> dict[str, Any]:
    attempts = int(state.get("attempts", 0)) + 1
    state["attempts"] = attempts
    save_session(project_root, session_id, state)
    if attempts <= limit:
        return _block_response(adapter, reason + f"\n({attempts}/{limit} automatic retries).")
    return _human_required_response(
        adapter,
        reason + "\nThe continuation budget is exhausted. Stop automatic repair and ask a human "
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
    if event == "pre-compact":
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
    reason = "[ConstraintLoop continuation]\n" + reason
    if adapter == "gemini":
        return {"decision": "deny", "reason": reason}
    return {"decision": "block", "reason": reason}


def _allow_response(adapter: str, summary: str) -> dict[str, Any]:
    if adapter == "gemini":
        return {"decision": "allow", "systemMessage": summary}
    return {"continue": True, "systemMessage": summary}


def _human_required_response(adapter: str, reason: str) -> dict[str, Any]:
    if adapter == "gemini":
        return {"continue": False, "stopReason": reason, "systemMessage": reason}
    return {"continue": False, "stopReason": reason, "systemMessage": reason}


def _is_hook_feedback(prompt: str) -> bool:
    stripped = prompt.strip()
    return stripped.startswith("[ConstraintLoop continuation]\n") or (
        stripped.startswith("<hook_prompt ") and stripped.endswith("</hook_prompt>")
    )


def _protected_mutation(
    payload: dict[str, Any], serialized: str, project_root: Path | None = None
) -> bool:
    protected: tuple[str, ...] = (
        "constraintloop." + "yml",
        "constraintloop." + "yaml",
        ".constraintloop/" + "secrets.env",
        ".constraintloop/" + "hooks-disabled.json",
        ".constraintloop/state/" + "loops/",
        "constraintloop-baselines.json",
    )
    if project_root is not None and any(
        (project_root / name).is_file() for name in ("constraintloop.yml", "constraintloop.yaml")
    ):
        contract, _ = load_contract(project_root)
        protected += tuple(
            spec.baseline_file
            for spec in contract.constraints.values()
            if isinstance(spec, RatchetConstraint)
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
        if re.search(r"\bconstraintloop\s+baseline\s+update\b", command) and re.search(
            r"(?:^|\s)--allow-regression(?:\s|$|[;&|])", command
        ):
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
