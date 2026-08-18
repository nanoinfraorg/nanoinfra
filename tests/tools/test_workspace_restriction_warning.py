"""An operator who set restrictToWorkspace alone should be told it is not a boundary.

The path guard reads command text, and a relative symlink inside the workspace resolves outside it
at open time without any absolute path for the guard to check. Measured on this tree: with no
sandbox, `cat link.txt` returns the target's contents; with `tools.exec.sandbox = "bwrap"` the same
command gets ENOENT. Upstream tracks the hole as HKUDS/nanobot#4072 and #4790 and has not closed it;
we cannot close it in a text guard either, so the deployment says so out loud.
See nanoinfraorg/nanoinfra#147.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from loguru import logger

from nanoinfra.agent.tools import shell as shell_mod
from nanoinfra.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and bwrap")


@pytest.fixture(autouse=True)
def _reset_warning_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_mod, "_warned_once", set())


@pytest.fixture
def warnings() -> "list[str]":
    """Capture loguru output. caplog only sees stdlib records, and this project uses loguru."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(sink_id)


def test_restriction_without_a_sandbox_warns(warnings: list[str]) -> None:
    ExecTool(working_dir="/tmp", restrict_to_workspace=True, sandbox="")

    joined = "\n".join(warnings)
    assert "restrictToWorkspace" in joined
    assert "bwrap" in joined


def test_the_warning_is_said_once_per_process(warnings: list[str]) -> None:
    """A tool is built per turn, so a per-instance log would train the operator to ignore it."""
    for _ in range(3):
        ExecTool(working_dir="/tmp", restrict_to_workspace=True, sandbox="")

    assert "\n".join(warnings).count("restrictToWorkspace") == 1


def test_a_sandboxed_deployment_is_not_warned(warnings: list[str]) -> None:
    ExecTool(working_dir="/tmp", restrict_to_workspace=True, sandbox="bwrap")

    assert "restrictToWorkspace" not in "\n".join(warnings)


def test_an_unrestricted_deployment_is_not_warned(warnings: list[str]) -> None:
    """Nothing is being claimed, so there is nothing to correct."""
    ExecTool(working_dir="/tmp", restrict_to_workspace=False, sandbox="")

    assert "restrictToWorkspace" not in "\n".join(warnings)


async def test_the_hole_the_warning_describes_is_real() -> None:
    """Pin the behaviour the warning exists for, so a future claim of containment is testable."""
    workspace = Path(tempfile.mkdtemp()) / "workspace"
    workspace.mkdir()
    secret = Path(tempfile.mkdtemp()) / "secret.txt"
    secret.write_text("SENTINEL-VALUE\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to(secret)

    tool = ExecTool(working_dir=str(workspace), timeout=10, restrict_to_workspace=True)
    result = await tool.execute(command="cat link.txt", timeout=10)

    assert "SENTINEL-VALUE" in str(result), (
        "if this now blocks, the text guard gained real containment and the warning should be "
        "revisited rather than left as a standing caveat"
    )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")
async def test_the_sandbox_closes_the_hole() -> None:
    """The other half: the warning points at bwrap, so bwrap has to actually work."""
    workspace = Path(tempfile.mkdtemp()) / "workspace"
    workspace.mkdir()
    secret = Path(tempfile.mkdtemp()) / "secret.txt"
    secret.write_text("SENTINEL-VALUE\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to(secret)

    tool = ExecTool(
        working_dir=str(workspace), timeout=10, restrict_to_workspace=True, sandbox="bwrap"
    )
    result = await tool.execute(command="cat link.txt", timeout=10)

    assert "SENTINEL-VALUE" not in str(result)
