"""Exercise failure behavior through an installed console entry point."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def main() -> int:
    executable = Path(sys.argv[1])
    with tempfile.TemporaryDirectory(prefix="constraintloop-failure-smoke-") as directory:
        project = Path(directory)
        name = "constraintloop" + ".yml"
        payload = {
            "version": 1,
            "constraints": {
                "deliberate_failure": {
                    "kind": "command",
                    "command": [sys.executable, "-c", "raise SystemExit(23)"],
                    "phases": ["stop", "ci"],
                }
            },
        }
        (project / name).write_text(yaml.safe_dump(payload), encoding="utf-8")
        result = subprocess.run(
            [str(executable), "ci", "--project", str(project), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        record = json.loads(result.stdout)
        observed = record["results"][0]
        if result.returncode != 1 or observed["exit_code"] != 23:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            return 1
    print("installed-wheel failure smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
