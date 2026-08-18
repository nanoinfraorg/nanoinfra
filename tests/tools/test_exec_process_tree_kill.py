"""A timed-out command takes its descendants with it, not just the direct child.

`_kill_process_tree` and the `process_tree` spawn flag both existed, but the one-shot path spawned
without a new session and killed only the child. `bash -c "sleep 300 &"` left the grandchild
running with nobody supervising it. Ported from upstream d64b8460 (nanoinfraorg/nanoinfra#145).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from nanoinfra.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _tool(tmp_path) -> ExecTool:
    return ExecTool(working_dir=str(tmp_path), timeout=1, restrict_to_workspace=False)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_a_timeout_kills_a_backgrounded_grandchild(tmp_path) -> None:
    """The bug, end to end: a grandchild must not outlive the timeout."""
    marker = tmp_path / "child.pid"
    command = f"sh -c 'echo $$ > {marker}; sleep 60' & sleep 60"

    result = await _tool(tmp_path).execute(command=command, timeout=1)

    assert "timed out" in str(result)
    # The pid file is written immediately; give the kill a moment to land.
    for _ in range(50):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    assert marker.exists(), "the grandchild never started, so the test proves nothing"
    pid = int(marker.read_text().strip())
    for _ in range(60):
        if not _alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not _alive(pid), f"grandchild {pid} survived the timeout"


async def test_the_one_shot_spawn_owns_a_process_group(tmp_path) -> None:
    """The mechanism: without a new session there is no group to signal."""
    result = await _tool(tmp_path).execute(command="ps -o pgid= -p $$", timeout=10)

    # The tool frames output with an exit-code line, so pick the numeric one out.
    digits = [line.strip() for line in str(result).splitlines() if line.strip().isdigit()]
    assert digits, f"expected a pgid in the output, got: {result!r}"
    # A new session means the child leads its own group rather than joining ours.
    assert int(digits[0]) != os.getpgid(0)


async def test_an_ordinary_command_still_returns_its_output(tmp_path) -> None:
    result = await _tool(tmp_path).execute(command="echo hello", timeout=10)

    assert "hello" in str(result)


async def test_a_nonzero_exit_is_still_reported(tmp_path) -> None:
    result = await _tool(tmp_path).execute(command="exit 3", timeout=10)

    assert "3" in str(result)
