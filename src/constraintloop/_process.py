"""Bounded subprocess execution with descendant cleanup."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path


class OutputLimitExceeded(OSError):
    """Output exceeded the collection bound; incomplete evidence must not be parsed."""


def run_bounded(
    command: Sequence[str] | str,
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    shell: bool = False,
    output_limit: int = 8 * 1024 * 1024,
) -> subprocess.CompletedProcess[str]:
    """Collect at most output_limit bytes; kill descendants on timeout or overflow."""
    if output_limit < 1:
        raise ValueError("output_limit must be positive")
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
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending_input = memoryview((input_text or "").encode())
    deadline = time.monotonic() + timeout
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                if pending_input:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        try:
                            written = os.write(key.fd, pending_input[:16_384])
                            pending_input = pending_input[written:]
                        except BrokenPipeError:
                            pending_input = memoryview(b"")
                        if not pending_input:
                            selector.unregister(key.fileobj)
                            assert process.stdin is not None
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 16_384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > output_limit:
                        raise OutputLimitExceeded(f"Command output exceeded {output_limit} bytes")
                    buffers[key.data].extend(chunk)
            process.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        _terminate_process_tree(process)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        buffers["stdout"].decode("utf-8", errors="replace"),
        buffers["stderr"].decode("utf-8", errors="replace"),
    )


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate the process group, escalating quickly when descendants ignore TERM."""
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
    finally:
        # The group leader can exit before a child that ignored TERM. Always
        # clean up the remaining group, without collecting unbounded output.
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
