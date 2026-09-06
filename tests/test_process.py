from __future__ import annotations

import signal
import subprocess
import sys
from typing import Any

import pytest

import constraintloop._process as process_module


class _StubbornProcess:
    pid = 4321

    def __init__(self) -> None:
        self.calls = 0
        self.terminated = False
        self.killed = False

    def wait(self, **kwargs: Any) -> int:
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired("check", kwargs.get("timeout", 0))
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_timeout_cleanup_escalates_for_a_stubborn_posix_process(monkeypatch) -> None:
    process = _StubbornProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    process_module._terminate_process_tree(process)  # type: ignore[arg-type]

    assert signals[0] == (process.pid, signal.SIGTERM)
    assert all(item == (process.pid, signal.SIGKILL) for item in signals[1:])


def test_timeout_cleanup_uses_process_methods_off_posix(monkeypatch) -> None:
    process = _StubbornProcess()
    monkeypatch.setattr(process_module.os, "name", "nt")

    process_module._terminate_process_tree(process)  # type: ignore[arg-type]

    assert process.terminated
    assert process.killed


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_fails_instead_of_parsing_truncated_evidence(stream):
    with pytest.raises(process_module.OutputLimitExceeded, match="4096 bytes"):
        process_module.run_bounded(
            [sys.executable, "-c", f"import sys; sys.{stream}.write('x' * 1000000)"],
            timeout=5,
            output_limit=4096,
        )


@pytest.mark.parametrize("input_text", ["", "é" * 100000], ids=["empty", "large-utf8"])
def test_bounded_process_streams_input_and_decodes_utf8(input_text):
    result = process_module.run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(sys.stdin.read()); sys.stderr.write('ok')",
        ],
        timeout=5,
        input_text=input_text,
    )
    assert result.stdout == input_text
    assert result.stderr == "ok"


def test_bounded_process_handles_child_closing_stdin_early():
    result = process_module.run_bounded(
        [sys.executable, "-c", "import os; os.close(0); print('done')"],
        timeout=5,
        input_text="x" * 1000000,
    )
    assert result.stdout.strip() == "done"


def test_bounded_process_timeout_after_child_closes_output():
    with pytest.raises(subprocess.TimeoutExpired):
        process_module.run_bounded(
            [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(10)"],
            timeout=0.1,
        )


def test_bounded_process_rejects_invalid_limit():
    with pytest.raises(ValueError, match="positive"):
        process_module.run_bounded(["true"], timeout=5, output_limit=0)
