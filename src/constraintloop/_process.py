"""Bounded subprocess execution with descendant cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path


def run_bounded(
    command: Sequence[str] | str,
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command and tear down its whole process group after a timeout."""
    process = subprocess.Popen(
        command,
        shell=shell,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _terminate_process_tree(process)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout or "", stderr or "")


def _terminate_process_tree(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate the process group, escalating quickly when descendants ignore TERM."""
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        return process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        return process.communicate()
