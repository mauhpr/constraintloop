"""Exercise failure behavior through an installed console entry point."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


def invoke(executable: Path, project: Path, *args: str, payload: dict[str, Any] | None = None):
    return subprocess.run(
        [str(executable), *args, "--project", str(project)],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def challenge_smoke(executable: Path, project: Path) -> None:
    """Exercise the installed distribution, without importing editable package code."""
    (project / "source.py").write_text("# Domain source for protocol smoke only.\n")
    contract = {
        "constraints": {
            "check": {
                "kind": "command",
                "command": [sys.executable, "-c", "pass"],
                "phases": ["stop"],
                "watch": ["source.py"],
            }
        },
        "loops": {
            "completion": {
                "phase": "stop",
                "interval_seconds": 1,
                "max_repair_attempts": 2,
                "max_unchanged_repairs": 2,
                "max_duration_seconds": 300,
                "challenge": {"count": 3, "watch": ["source.py"]},
            }
        },
    }
    (project / "constraintloop.yml").write_text(yaml.safe_dump(contract))
    initial = invoke(executable, project, "cycle", "completion", "--json")
    assert initial.returncode == 15, initial.stdout + initial.stderr
    submission_path = project / ".constraintloop/state/submission.json"
    for kind, expected in (("discovery", 16), ("verification", 0)):
        shown = invoke(executable, project, "challenge", "show", "completion")
        assert shown.returncode == 0, shown.stderr
        request = json.loads(shown.stdout)["request"]
        submission = {
            "kind": kind,
            "request_id": request["request_id"],
            "input_snapshot": request["input_snapshot"],
        }
        if kind == "discovery":
            submission.update(
                {
                    "domain_summary": "Protocol fixture, not a semantic quality evaluation.",
                    "domain_sources": ["source.py"],
                    "challenges": [
                        {
                            "id": f"case-{index}",
                            "perspective": perspective,
                            "assumption": "Recorded fixture plans remain stable.",
                            "scenario": f"Protocol roundtrip for {perspective} scenario.",
                            "expected_behavior": "The plan survives submission and verification.",
                            "verification_plan": "Inspect the request and deterministic check ID.",
                            "source_refs": ["source.py"],
                        }
                        for index, perspective in enumerate(
                            ("boundaries", "recovery", "concurrency")
                        )
                    ],
                }
            )
        else:
            assert len(request["challenges"]) == 3
            submission["resolutions"] = [
                {
                    "challenge_id": item["id"],
                    "outcome": "verified",
                    "evidence": "Roundtrip preserved this plan; check is fresh and passing.",
                    "constraint_ids": ["check"],
                    "source_refs": ["source.py"],
                }
                for item in request["challenges"]
            ]
        submission_path.write_text(json.dumps(submission))
        submitted = invoke(
            executable, project, "challenge", "submit", "completion", "--file", str(submission_path)
        )
        assert submitted.returncode == 0, submitted.stdout + submitted.stderr
        transition = invoke(executable, project, "cycle", "completion", "--json")
        assert transition.returncode == expected, transition.stdout + transition.stderr
    for adapter in ("claude", "codex", "gemini"):
        completed = invoke(
            executable,
            project,
            "hook",
            "--adapter",
            adapter,
            "--event",
            "stop",
            payload={"session_id": adapter},
        )
        assert completed.returncode == 0, completed.stderr
        response = json.loads(completed.stdout)
        assert response.get("continue") is True or response.get("decision") == "allow"


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
        for adapter in ("claude", "codex", "gemini"):
            for attempt in range(3):
                hooked = invoke(
                    executable,
                    project,
                    "hook",
                    "--adapter",
                    adapter,
                    "--event",
                    "stop",
                    payload={
                        "session_id": adapter,
                        "stop_hook_active": attempt > 0,
                    },
                )
                assert hooked.returncode == 0, hooked.stderr
                response = json.loads(hooked.stdout)
                if attempt < 2:
                    assert response.get("decision") in {"block", "deny"}
                else:
                    assert response.get("continue") is False
        challenge_smoke(executable, project)
    print("installed-package failure, recursive-hook, and session-challenge smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
