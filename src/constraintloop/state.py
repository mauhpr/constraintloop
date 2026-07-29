"""Local evidence, session, and human-waiver persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

from constraintloop.digest import project_key
from constraintloop.models import ConstraintResult


def cache_root(project_root: Path) -> Path:
    override = os.environ.get("CONSTRAINTLOOP_CACHE_DIR")
    if override:
        return Path(override).expanduser() / project_key(project_root)
    return project_root.resolve() / ".constraintloop" / "state"


def _project_dir(project_root: Path) -> Path:
    return cache_root(project_root)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
        os.replace(temp_name, path)
    finally:
        with suppress(OSError):
            os.unlink(temp_name)


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-write operations across local processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        flock(handle.fileno(), LOCK_EX)
        try:
            yield
        finally:
            flock(handle.fileno(), LOCK_UN)


def evidence_path(project_root: Path) -> Path:
    return _project_dir(project_root) / "evidence.json"


def load_cached_result(
    project_root: Path,
    constraint_id: str,
    input_digest: str,
) -> ConstraintResult | None:
    raw = _read_json(evidence_path(project_root), {})
    item = raw.get(constraint_id)
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("result"), dict):
        if item.get("cache_digest") != input_digest:
            return None
        result_payload = item["result"]
    else:
        result_payload = item
    try:
        result = ConstraintResult.model_validate(result_payload)
    except Exception:
        return None
    if "result" not in item and result.input_digest != input_digest:
        return None
    result.cached = True
    return result


def load_latest_result(project_root: Path, constraint_id: str) -> ConstraintResult | None:
    """Load the last parseable result regardless of freshness."""
    raw = _read_json(evidence_path(project_root), {})
    item = raw.get(constraint_id)
    if not isinstance(item, dict):
        return None
    result_payload = item.get("result") if isinstance(item.get("result"), dict) else item
    try:
        return ConstraintResult.model_validate(result_payload)
    except Exception:
        return None


def save_cached_result(
    project_root: Path,
    result: ConstraintResult,
    *,
    cache_digest: str | None = None,
) -> None:
    path = evidence_path(project_root)
    with _write_lock(path):
        raw = _read_json(path, {})
        raw[result.constraint_id] = {
            "cache_digest": cache_digest or result.input_digest,
            "result": result.model_dump(mode="json"),
        }
        _write_json(path, raw)


def advisory_acknowledgments_path(project_root: Path) -> Path:
    return _project_dir(project_root) / "advisory-acknowledgments.json"


def result_evidence_digest(result: ConstraintResult) -> str:
    payload = {
        "constraint_id": result.constraint_id,
        "input_digest": result.input_digest,
        "verdict": result.verdict.value,
        "message": result.message,
        "exit_code": result.exit_code,
        "value": result.value,
        "output_tail": result.output_tail,
        "findings": [
            finding.model_dump(mode="json", exclude_none=True) for finding in result.findings
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_advisory_acknowledgment(
    project_root: Path,
    result: ConstraintResult,
    reason: str,
) -> None:
    path = advisory_acknowledgments_path(project_root)
    with _write_lock(path):
        acknowledgments = _read_json(path, {})
        acknowledgments[result.constraint_id] = {
            "evidence_digest": result_evidence_digest(result),
            "reason": reason,
        }
        _write_json(path, acknowledgments)


def advisory_acknowledgment_reason(
    project_root: Path,
    result: ConstraintResult,
) -> str | None:
    acknowledgments = _read_json(advisory_acknowledgments_path(project_root), {})
    item = acknowledgments.get(result.constraint_id)
    if not isinstance(item, dict):
        return None
    if item.get("evidence_digest") != result_evidence_digest(result):
        return None
    reason = item.get("reason")
    return reason if isinstance(reason, str) and reason else None


def session_path(project_root: Path, session_id: str) -> Path:
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in session_id)
    return _project_dir(project_root) / "sessions" / f"{safe}.json"


def load_session(project_root: Path, session_id: str) -> dict[str, Any]:
    raw = _read_json(session_path(project_root, session_id), {})
    return raw if isinstance(raw, dict) else {}


def save_session(project_root: Path, session_id: str, state: dict[str, Any]) -> None:
    path = session_path(project_root, session_id)
    with _write_lock(path):
        _write_json(path, state)


def waivers_path(project_root: Path) -> Path:
    return _project_dir(project_root) / "waivers.json"


def create_waiver(
    project_root: Path,
    result: ConstraintResult,
    contract_digest: str,
    reason: str,
) -> None:
    path = waivers_path(project_root)
    with _write_lock(path):
        waivers = _read_json(path, {})
        waivers[result.constraint_id] = {
            "input_digest": result.input_digest,
            "contract_digest": contract_digest,
            "evidence_digest": result_evidence_digest(result),
            "reason": reason,
        }
        _write_json(path, waivers)


def waiver_reason(
    project_root: Path,
    result: ConstraintResult,
    contract_digest: str,
) -> str | None:
    waivers = _read_json(waivers_path(project_root), {})
    waiver = waivers.get(result.constraint_id)
    if not isinstance(waiver, dict):
        return None
    if waiver.get("input_digest") != result.input_digest:
        return None
    if waiver.get("contract_digest") != contract_digest:
        return None
    if waiver.get("evidence_digest") != result_evidence_digest(result):
        return None
    reason = waiver.get("reason")
    return reason if isinstance(reason, str) and reason else None
