"""Runtime files under a tracked directory must stay ignored.

`!memory/` un-ignores the directory so git descends into it, and in doing so stops the top-level
`/*` rule from covering its contents. Every runtime file the agent later wrote there --
`history.jsonl`, `.cursor`, `raw/` -- became permanently untracked and showed up as noise in every
status. Addresses upstream HKUDS/nanobot#5246 (nanoinfraorg/nanoinfra#146).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nanoinfra.agent.memory import GIT_TRACKED_DIRS, GIT_TRACKED_FILES
from nanoinfra.utils.gitstore import GitStore


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "memory" / "MEMORY.md").write_text("m\n", encoding="utf-8")
    (workspace / "SOUL.md").write_text("s\n", encoding="utf-8")
    (workspace / "USER.md").write_text("u\n", encoding="utf-8")
    (workspace / "skills" / "s1").mkdir(parents=True)
    (workspace / "skills" / "s1" / "SKILL.md").write_text("k\n", encoding="utf-8")
    return workspace


def _store(workspace: Path) -> GitStore:
    return GitStore(
        workspace,
        tracked_files=list(GIT_TRACKED_FILES),
        tracked_dirs=list(GIT_TRACKED_DIRS),
    )


def _status(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _tracked(workspace: Path) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=workspace, capture_output=True, text=True, check=False
    )
    return set(result.stdout.split())


@pytest.fixture
def initialized(tmp_path: Path) -> Path:
    workspace = _workspace(tmp_path)
    _store(workspace).init()
    return workspace


def test_runtime_files_under_a_tracked_dir_are_ignored(initialized: Path) -> None:
    (initialized / "memory" / "history.jsonl").write_text("x\n", encoding="utf-8")
    (initialized / "memory" / ".cursor").write_text("c\n", encoding="utf-8")
    (initialized / "memory" / "raw").mkdir()
    (initialized / "memory" / "raw" / "a.md").write_text("r\n", encoding="utf-8")

    assert _status(initialized) == "", "runtime files must not show as untracked"


def test_the_tracked_files_are_still_tracked(initialized: Path) -> None:
    """The re-ignore must not outrank the negations it precedes."""
    tracked = _tracked(initialized)

    assert "memory/MEMORY.md" in tracked
    assert "memory/.dream_cursor" in tracked
    assert "SOUL.md" in tracked


def test_a_tracked_directory_of_unknown_files_is_unaffected(initialized: Path) -> None:
    """`skills/` is tracked wholesale, so its contents must stay visible to git."""
    assert "skills/s1/SKILL.md" in _tracked(initialized)


def test_a_new_skill_is_still_picked_up(initialized: Path) -> None:
    (initialized / "skills" / "s2").mkdir()
    (initialized / "skills" / "s2" / "SKILL.md").write_text("k2\n", encoding="utf-8")

    assert "skills/s2/SKILL.md" in _status(initialized)


def test_an_existing_workspace_is_repaired(tmp_path: Path) -> None:
    """init() returns early once .git exists, so an old ignore file needs repairing in place."""
    workspace = _workspace(tmp_path)
    store = _store(workspace)
    store.init()
    # Rewrite the ignore file to its pre-fix shape.
    broken = "\n".join(
        line
        for line in (workspace / ".gitignore").read_text(encoding="utf-8").splitlines()
        if not line.endswith("/*") or line == "/*"
    )
    (workspace / ".gitignore").write_text(broken + "\n", encoding="utf-8")
    (workspace / "memory" / "history.jsonl").write_text("x\n", encoding="utf-8")
    assert "memory/history.jsonl" in _status(workspace)

    assert store.ensure_gitignore() is True

    # `.gitignore` itself is tracked and this test edited it, so it legitimately shows as
    # modified. What matters is that the runtime file is no longer reported.
    assert "memory/history.jsonl" not in _status(workspace)
    assert "memory/MEMORY.md" in _tracked(workspace)


def test_the_repair_is_idempotent(initialized: Path) -> None:
    store = _store(initialized)
    before = (initialized / ".gitignore").read_text(encoding="utf-8")

    assert store.ensure_gitignore() is False
    assert (initialized / ".gitignore").read_text(encoding="utf-8") == before


def test_the_repair_keeps_operator_rules(tmp_path: Path) -> None:
    """An operator's own lines are theirs: the repair appends and never rewrites."""
    workspace = _workspace(tmp_path)
    store = _store(workspace)
    store.init()
    path = workspace / ".gitignore"
    path.write_text("/*\n!memory/\n!memory/MEMORY.md\n# my own rule\n*.tmp\n", encoding="utf-8")

    store.ensure_gitignore()
    content = path.read_text(encoding="utf-8")

    assert "# my own rule" in content
    assert "*.tmp" in content
    assert "memory/*" in content


def test_the_repair_does_nothing_without_a_repo(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    assert _store(workspace).ensure_gitignore() is False
