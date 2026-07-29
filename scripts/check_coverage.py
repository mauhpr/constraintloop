"""Enforce independent statement and branch coverage floors."""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATEMENT_FLOOR = 90.0
BRANCH_FLOOR = 80.0


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    totals = json.loads(path.read_text(encoding="utf-8"))["totals"]
    statements = float(totals["percent_statements_covered"])
    branches = float(totals["percent_branches_covered"])
    print(f"statement coverage: {statements:.2f}% (required {STATEMENT_FLOOR:.0f}%)")
    print(f"branch coverage: {branches:.2f}% (required {BRANCH_FLOOR:.0f}%)")
    return int(statements < STATEMENT_FLOOR or branches < BRANCH_FLOOR)


if __name__ == "__main__":
    raise SystemExit(main())
