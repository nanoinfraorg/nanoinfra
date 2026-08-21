# tests/gates/test_process_liveness.py
"""A zombie must read as gone (found by CI, not by a workstation).

Three MCP host tests kill a child and wait for its pid. They passed here and failed in CI,
because `os.kill(pid, 0)` answers for a zombie and a CI container may run no reaping init. A
reparented child then stays a zombie for the life of the container.

The test forks its own child rather than use subprocess, because subprocess reaps a child on its
own schedule and the zombie would disappear mid-test.
"""

from __future__ import annotations

import contextlib
import os
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

_HAS_PROCFS = Path("/proc").is_dir()


def _state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    _, _, tail = stat.rpartition(")")
    fields = tail.split()
    return fields[0] if fields else None


@contextlib.contextmanager
def _zombie() -> Iterator[int]:
    """A dead child that nothing waited on, held for the body of the block.

    The parent reaps it at the end, so the test leaves no entry behind.
    """
    with warnings.catch_warnings():
        # Python warns that fork() in a multi-threaded process may deadlock the child. It cannot
        # here: the child's only statement is os._exit(0), which runs no Python and takes no lock.
        # A zombie is the whole point of this helper, and there is no fork-free way to make one,
        # so the warning is suppressed at the call rather than for the file.
        warnings.filterwarnings(
            "ignore",
            message="This process .* is multi-threaded",
            category=DeprecationWarning,
        )
        pid = os.fork()
    if pid == 0:
        os._exit(0)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _state(pid) != "Z":
        time.sleep(0.01)
    if _state(pid) != "Z":
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        pytest.skip("the child never reached the zombie state on this host")
    try:
        yield pid
    finally:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


@pytest.mark.skipif(not _HAS_PROCFS, reason="the zombie check reads procfs")
def test_a_zombie_reads_as_gone(pid_alive) -> None:
    """The signal probe alone calls this pid alive, so every caller would wait forever."""
    with _zombie() as pid:
        os.kill(pid, 0)  # the old check, and it still answers for a dead child

        assert pid_alive(pid) is False


@pytest.mark.skipif(not _HAS_PROCFS, reason="the zombie check reads procfs")
def test_the_wait_returns_at_once_for_a_zombie(wait_until_pid_gone) -> None:
    with _zombie() as pid:
        started = time.monotonic()

        gone = wait_until_pid_gone(pid, timeout_s=5.0)

        assert gone is True
        assert time.monotonic() - started < 1.0, "a zombie must not cost the whole timeout"


def test_this_process_reads_as_alive(pid_alive) -> None:
    """The check must not report every pid as gone, which would make the tests vacuous."""
    assert pid_alive(os.getpid()) is True
