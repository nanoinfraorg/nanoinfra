"""Session history must live outside the agent workspace.

The workspace is the on-disk scope of the agent's filesystem tools and the agent
account owns it. Session transcripts stored there are readable and rewritable by
the agent, which routes around the scoped session access layer entirely.
See nanoinfraorg/nanoinfra#136.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nanoinfra.session.manager import _WORKSPACE_ID_RE, JsonlSessionStore


def _store(tmp_path: Path, **kwargs: object) -> JsonlSessionStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    root = kwargs.pop("sessions_root", tmp_path / "state" / "sessions")
    return JsonlSessionStore(workspace, sessions_root=root)  # type: ignore[arg-type]


def test_sessions_dir_is_outside_the_workspace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = (tmp_path / "workspace").resolve()

    assert not store.sessions_dir.is_relative_to(workspace)
    assert store.sessions_dir.exists()


def test_sessions_dir_is_namespaced_by_a_stable_workspace_id(tmp_path: Path) -> None:
    """The namespace must survive a re-open; nothing may be re-derived per process."""
    first = _store(tmp_path)
    second = _store(tmp_path)

    assert first.sessions_dir == second.sessions_dir
    assert _WORKSPACE_ID_RE.fullmatch(first.sessions_dir.name)


def test_two_workspaces_get_independent_stores(tmp_path: Path) -> None:
    root = tmp_path / "state" / "sessions"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    a = JsonlSessionStore(tmp_path / "a", sessions_root=root)
    b = JsonlSessionStore(tmp_path / "b", sessions_root=root)

    assert a.sessions_dir != b.sessions_dir


def test_history_follows_a_moved_workspace(tmp_path: Path) -> None:
    """Identity is a marker inside the workspace, not a hash of its path.

    A hash of the resolved path would orphan every transcript the moment an
    operator renames or relocates the workspace.
    """
    root = tmp_path / "state" / "sessions"
    original = tmp_path / "before"
    original.mkdir()
    first = JsonlSessionStore(original, sessions_root=root)
    namespace = first.sessions_dir.name

    moved = tmp_path / "after"
    original.rename(moved)
    second = JsonlSessionStore(moved, sessions_root=root)

    assert second.sessions_dir.name == namespace


def test_root_inside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="outside the agent workspace"):
        JsonlSessionStore(workspace, sessions_root=workspace / "sessions")


def test_root_equal_to_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="outside the agent workspace"):
        JsonlSessionStore(workspace, sessions_root=workspace)


def test_sessions_root_is_not_world_readable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mode = os.stat(store.sessions_dir.parent).st_mode & 0o777

    assert mode & 0o077 == 0


def test_legacy_in_workspace_sessions_are_migrated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    old_dir = workspace / "sessions"
    old_dir.mkdir(parents=True)
    legacy = old_dir / "abc.jsonl"
    legacy.write_text(json.dumps({"role": "user", "content": "hi"}) + "\n", encoding="utf-8")

    store = JsonlSessionStore(workspace, sessions_root=tmp_path / "state" / "sessions")

    migrated = store.sessions_dir / "abc.jsonl"
    assert migrated.is_file()
    assert "hi" in migrated.read_text(encoding="utf-8")
    assert not legacy.exists()


def test_migration_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    old_dir = workspace / "sessions"
    old_dir.mkdir(parents=True)
    (old_dir / "abc.jsonl").write_text("legacy\n", encoding="utf-8")

    root = tmp_path / "state" / "sessions"
    store = JsonlSessionStore(workspace, sessions_root=root)
    (store.sessions_dir / "abc.jsonl").write_text("current\n", encoding="utf-8")

    # A second legacy file appearing at the same name must not clobber the live one.
    (old_dir / "abc.jsonl").write_text("legacy again\n", encoding="utf-8")
    JsonlSessionStore(workspace, sessions_root=root)

    assert (store.sessions_dir / "abc.jsonl").read_text(encoding="utf-8") == "current\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlinked_legacy_directory_is_not_migrated(tmp_path: Path) -> None:
    """A symlinked legacy dir could point anywhere; moving out of it is not ours to do."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "abc.jsonl").write_text("secret\n", encoding="utf-8")
    (workspace / "sessions").symlink_to(elsewhere)

    store = JsonlSessionStore(workspace, sessions_root=tmp_path / "state" / "sessions")

    assert not (store.sessions_dir / "abc.jsonl").exists()
    assert (elsewhere / "abc.jsonl").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlinked_legacy_file_is_not_migrated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    old_dir = workspace / "sessions"
    old_dir.mkdir(parents=True)
    target = tmp_path / "outside.jsonl"
    target.write_text("secret\n", encoding="utf-8")
    (old_dir / "abc.jsonl").symlink_to(target)

    store = JsonlSessionStore(workspace, sessions_root=tmp_path / "state" / "sessions")

    assert not (store.sessions_dir / "abc.jsonl").exists()
    assert target.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlinked_identity_marker_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = workspace / ".nanoinfra"
    state.mkdir(parents=True)
    (state / "workspace-id").symlink_to(tmp_path / "elsewhere-id")

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        JsonlSessionStore(workspace, sessions_root=tmp_path / "state" / "sessions")


def test_corrupt_identity_marker_is_refused(tmp_path: Path) -> None:
    """A damaged marker must stop startup, not silently start a second store."""
    workspace = tmp_path / "workspace"
    state = workspace / ".nanoinfra"
    state.mkdir(parents=True)
    (state / "workspace-id").write_text("not-a-valid-id\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid"):
        JsonlSessionStore(workspace, sessions_root=tmp_path / "state" / "sessions")


def test_identity_marker_is_not_world_readable(tmp_path: Path) -> None:
    _store(tmp_path)
    marker = (tmp_path / "workspace" / ".nanoinfra" / "workspace-id")

    assert marker.is_file()
    assert os.stat(marker).st_mode & 0o077 == 0


def test_namespace_is_recovered_when_the_marker_is_deleted(tmp_path: Path) -> None:
    """Workspace cleanup can remove the marker; the .workspace backref recovers it."""
    root = tmp_path / "state" / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = JsonlSessionStore(workspace, sessions_root=root)
    namespace = first.sessions_dir.name

    (workspace / ".nanoinfra" / "workspace-id").unlink()
    second = JsonlSessionStore(workspace, sessions_root=root)

    assert second.sessions_dir.name == namespace
