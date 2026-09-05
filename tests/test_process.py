from __future__ import annotations

import signal
import subprocess
from typing import Any

import constraintloop._process as process_module


class _StubbornProcess:
    pid = 4321

    def __init__(self) -> None:
        self.calls = 0
        self.terminated = False
        self.killed = False

    def communicate(self, **kwargs: Any) -> tuple[str, str]:
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired("check", kwargs.get("timeout", 0))
        return "partial stdout", "partial stderr"

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_timeout_cleanup_escalates_for_a_stubborn_posix_process(monkeypatch) -> None:
    process = _StubbornProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    output = process_module._terminate_process_tree(process)  # type: ignore[arg-type]

    assert output == ("partial stdout", "partial stderr")
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]


def test_timeout_cleanup_uses_process_methods_off_posix(monkeypatch) -> None:
    process = _StubbornProcess()
    monkeypatch.setattr(process_module.os, "name", "nt")

    output = process_module._terminate_process_tree(process)  # type: ignore[arg-type]

    assert output == ("partial stdout", "partial stderr")
    assert process.terminated
    assert process.killed
