"""An edit that changes nothing must fail, not report success.

A no-op replacement reads to the model as "done" and sends the turn onward, so the mistake it was
trying to fix silently survives. Ported from upstream b3b05176.
"""

from __future__ import annotations

from pathlib import Path

from nanoinfra.agent.tools.filesystem import EditFileTool


def _tool(workspace: Path) -> EditFileTool:
    return EditFileTool(workspace=workspace, restrict_to_workspace=False)


async def test_identical_old_and_new_text_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("hello world\n", encoding="utf-8")

    result = await _tool(tmp_path).execute(path=str(target), old_text="hello", new_text="hello")

    assert "must be different" in str(result)
    assert target.read_text(encoding="utf-8") == "hello world\n"


async def test_a_real_edit_still_applies(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("hello world\n", encoding="utf-8")

    await _tool(tmp_path).execute(path=str(target), old_text="hello", new_text="goodbye")

    assert target.read_text(encoding="utf-8") == "goodbye world\n"


async def test_create_semantics_still_allow_empty_to_empty(tmp_path: Path) -> None:
    """The guard is for existing files; creating an empty file is a real act."""
    target = tmp_path / "new.txt"

    await _tool(tmp_path).execute(path=str(target), old_text="", new_text="")

    assert target.exists()
