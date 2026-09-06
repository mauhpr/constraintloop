"""Best-effort scrubbing before evidence is retained or shown to an agent."""

from __future__ import annotations

import os
import re
from typing import Any

_SENSITIVE_KEY = re.compile(r"(?i)(password|secret|(?:api|access|auth)[_-]?(?:key|token))")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,\"']+)"
)


def redact_text(value: str) -> str:
    redacted = value
    for name, secret in os.environ.items():
        if len(secret) >= 8 and any(
            marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            redacted = redacted.replace(secret, "[REDACTED]")
    return _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted
    )


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.fullmatch(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    return value
