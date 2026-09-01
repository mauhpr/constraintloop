"""Contract loading and canonical hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from constraintloop import __version__
from constraintloop.models import Contract

CONFIG_NAMES = ("constraintloop.yml", "constraintloop.yaml")
LOCAL_CONFIG_NAMES = ("constraintloop.local.yml", "constraintloop.local.yaml")


class ContractError(ValueError):
    """Raised when a contract is missing or invalid."""


def find_contract(project_root: Path) -> Path:
    for name in CONFIG_NAMES:
        path = project_root / name
        if path.is_file():
            return path
    raise ContractError(f"No ConstraintLoop contract found in {project_root}")


def discover_project_root(start: Path) -> Path:
    """Walk upward from a hook cwd to the nearest contract."""
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if any((candidate / name).is_file() for name in CONFIG_NAMES):
            return candidate
    return current


def load_contract(project_root: Path, *, include_local: bool = True) -> tuple[Contract, Path]:
    path = find_contract(project_root)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ContractError(f"Invalid contract {path}: top-level value must be a mapping")
        base_contract = Contract.model_validate(raw)
        overlays = (
            [project_root / name for name in LOCAL_CONFIG_NAMES if (project_root / name).is_file()]
            if include_local
            else []
        )
        if len(overlays) > 1:
            raise ContractError(
                "Multiple local contract overlays found: "
                + ", ".join(str(item) for item in overlays)
            )
        if overlays:
            overlay = yaml.safe_load(overlays[0].read_text(encoding="utf-8")) or {}
            if not isinstance(overlay, dict):
                raise ContractError(
                    f"Invalid local contract overlay {overlays[0]}: "
                    "top-level value must be a mapping"
                )
            raw = _merge_mappings(raw, overlay)
            merged_contract = Contract.model_validate(raw)
            _ensure_overlay_only_strengthens(base_contract, merged_contract, overlays[0])
            return merged_contract, path
        return base_contract, path
    except ContractError:
        raise
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        hint = ""
        if isinstance(exc, ValidationError) and any(
            error["type"] == "extra_forbidden" for error in exc.errors()
        ):
            hint = (
                f"\nConstraintLoop {__version__} does not recognize one or more keys. "
                "Check for typos; if the contract uses features from a newer release, upgrade "
                "the hook executable and rerun `constraintloop setup --adapter all --project .`."
            )
        raise ContractError(f"Invalid contract {path}: {exc}{hint}") from exc


def _merge_mappings(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; local scalars and lists replace repository values."""
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_mappings(existing, value)
        else:
            merged[key] = value
    return merged


def _ensure_overlay_only_strengthens(base: Contract, merged: Contract, overlay_path: Path) -> None:
    """Reject local replacements that could bypass committed completion policy."""
    if merged.settings.evidence_output_limit < base.settings.evidence_output_limit:
        raise ContractError(f"Local overlay {overlay_path} weakens settings.evidence_output_limit")
    if merged.settings.evaluation_bundle_limit < base.settings.evaluation_bundle_limit:
        raise ContractError(
            f"Local overlay {overlay_path} weakens settings.evaluation_bundle_limit"
        )

    for constraint_id, original in base.constraints.items():
        local = merged.constraints[constraint_id]
        if original.kind != local.kind:
            _weakened(overlay_path, constraint_id, "kind")
        if original.enabled and not local.enabled:
            _weakened(overlay_path, constraint_id, "enabled")
        if original.enforcement.value == "required" and local.enforcement.value != "required":
            _weakened(overlay_path, constraint_id, "enforcement")
        if not set(original.phases).issubset(local.phases):
            _weakened(overlay_path, constraint_id, "phases")
        if not set(original.needs).issubset(local.needs):
            _weakened(overlay_path, constraint_id, "needs")
        if local.timeout_seconds > original.timeout_seconds:
            _weakened(overlay_path, constraint_id, "timeout_seconds")

        allowed = {"description", "enabled", "enforcement", "phases", "needs", "timeout_seconds"}
        original_fields = original.model_dump(mode="json")
        local_fields = local.model_dump(mode="json")
        for field in original_fields.keys() - allowed:
            if original_fields[field] != local_fields[field]:
                _weakened(overlay_path, constraint_id, field)

    for evaluator_id, original_evaluator in base.evaluators.items():
        if merged.evaluators[evaluator_id] != original_evaluator:
            raise ContractError(
                f"Local overlay {overlay_path} cannot replace committed evaluator {evaluator_id!r}"
            )
    for loop_id, original_loop in base.loops.items():
        if merged.loops[loop_id] != original_loop:
            raise ContractError(
                f"Local overlay {overlay_path} cannot replace committed loop {loop_id!r}"
            )


def _weakened(overlay_path: Path, constraint_id: str, field: str) -> None:
    raise ContractError(
        f"Local overlay {overlay_path} cannot replace committed constraints.{constraint_id}.{field}"
    )


def contract_digest(contract: Contract) -> str:
    payload = json.dumps(
        contract.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
