# tests/gates/conftest.py
"""Shared process helpers for the gate tests.

`os.kill(pid, 0)` answers for a zombie, because a dead child keeps its pid until somebody reaps
it. Three MCP host tests kill a child and then wait for the pid to go, and they passed on a
workstation and failed in CI for that reason: a container may run no reaping init, so a
reparented child stays a zombie for the life of the container and answers signal 0 forever.

A zombie is gone for every purpose these tests care about. It holds no socket, it runs no code,
and it answers no request. So the check reads the process state and treats `Z` as gone.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

_ZOMBIE_STATE = "Z"


def _process_state(pid: int) -> str | None:
    """The single-letter state from procfs, or None when procfs cannot answer.

    A name may hold a space or a parenthesis, so the parse takes what follows the last `)`.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    _, _, tail = stat.rpartition(")")
    fields = tail.split()
    return fields[0] if fields else None


def pid_alive(pid: int) -> bool:
    """Report whether *pid* runs, and count a zombie as gone.

    The signal probe answers first, because it works on every platform. procfs then decides the
    zombie case, and its absence leaves the signal answer in place.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return _process_state(pid) != _ZOMBIE_STATE


def wait_until_pid_gone(pid: int, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


@pytest.fixture
def pid_alive_check():
    """The liveness check, for a test that asserts a child still runs."""
    return pid_alive


@pytest.fixture
def wait_for_pid_gone():
    """The wait, for a test that kills a child and needs the pid to go."""
    return wait_until_pid_gone
