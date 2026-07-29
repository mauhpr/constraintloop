"""Contract loading and canonical hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from constraintloop.models import Contract

CONFIG_NAMES = ("constraintloop.yml", "constraintloop.yaml")


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


def load_contract(project_root: Path) -> tuple[Contract, Path]:
    path = find_contract(project_root)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Contract.model_validate(raw), path
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ContractError(f"Invalid contract {path}: {exc}") from exc


def contract_digest(contract: Contract) -> str:
    payload = json.dumps(
        contract.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
