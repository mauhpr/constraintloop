"""Shared finite-number policy for measurements and quality thresholds."""

from __future__ import annotations

import math


def finite_number(value: object) -> float:
    """Accept finite numbers and numeric strings, but never booleans or null."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("expected a finite number or numeric string, not bool/null/container")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("expected a finite number or numeric string") from exc
    if not math.isfinite(number):
        raise ValueError("expected a finite number")
    return number
