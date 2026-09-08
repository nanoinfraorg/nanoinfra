"""A read-only working copy is detached, and storage never sees it.

`SessionManager.read_only_copy` is the one mechanism behind the SDK's `ephemeral=True`
guarantee: `AgentLoop` hands the turn one of these instead of the cached session, and every
save site in that turn no-ops on its own. These tests pin the two halves of the copy -- the
detachment and the non-persistence -- at the manager, where they are stated.
"""

from __future__ import annotations

from nanoinfra.session.manager import SessionManager


def test_read_only_copy_carries_the_stored_history(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    stored = manager.get_or_create("cli:direct")
    stored.add_message("user", "remember this")
    manager.save(stored)

    copy = manager.read_only_copy("cli:direct")

    assert copy.persist is False
    assert [message["content"] for message in copy.messages] == ["remember this"]


def test_read_only_copy_does_not_share_state_with_the_cached_session(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    stored = manager.get_or_create("cli:direct")
    stored.add_message("user", "remember this")
    manager.save(stored)

    copy = manager.read_only_copy("cli:direct")
    copy.add_message("user", "do not keep me")
    copy.metadata["title"] = "ephemeral"

    assert len(stored.messages) == 1
    assert stored.metadata == {}
    assert manager.get_or_create("cli:direct") is stored


def test_saving_a_read_only_copy_writes_nothing(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    stored = manager.get_or_create("cli:direct")
    stored.add_message("user", "remember this")
    manager.save(stored)
    before = {path.name: path.read_bytes() for path in manager.sessions_dir.glob("*.jsonl")}

    copy = manager.read_only_copy("cli:direct")
    copy.add_message("user", "do not keep me")
    manager.save(copy, fsync=True)

    assert {
        path.name: path.read_bytes() for path in manager.sessions_dir.glob("*.jsonl")
    } == before


def test_read_only_copy_of_an_unknown_key_creates_no_cache_entry(tmp_path) -> None:
    manager = SessionManager(tmp_path)

    copy = manager.read_only_copy("cli:unknown")
    copy.add_message("user", "do not keep me")
    manager.save(copy)

    assert copy.messages
    assert manager.get_cached("cli:unknown") is None
    assert list(manager.sessions_dir.glob("*.jsonl")) == []


def test_a_read_only_copy_is_never_flushed_at_shutdown(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    ordinary = manager.get_or_create("cli:ordinary")
    ordinary.add_message("user", "remember this")
    copy = manager.read_only_copy("cli:direct")
    copy.add_message("user", "do not keep me")

    assert manager.flush_all() == 1
    assert [
        SessionManager.decode_storage_key(path.stem)
        for path in manager.sessions_dir.glob("*.jsonl")
    ] == ["cli:ordinary"]
