"""Canonical session files are serialized across processes.

Reproduced before the fix: two stores over one sessions root each loaded, appended a message, and
saved, and the second save silently discarded the first one's turn. Addresses upstream
HKUDS/nanobot#4798 (nanoinfraorg/nanoinfra#152).

The distinction that matters: serializing the individual operations is *not* enough to prevent a
lost update, because the mutation happens between two locked calls. `locked_session_files()` is what
makes a read-modify-write atomic, and these tests hold that line explicitly.
"""

from __future__ import annotations

import datetime
import multiprocessing
from pathlib import Path

import pytest

from nanoinfra.session.manager import JsonlSessionStore, Session


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _session(key: str, content: str) -> Session:
    now = _now()
    return Session(
        key=key,
        messages=[{"role": "user", "content": content}],
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "workspace", tmp_path / "sessions"


def _store(roots: tuple[Path, Path]) -> JsonlSessionStore:
    workspace, sessions_root = roots
    return JsonlSessionStore(workspace, sessions_root=sessions_root)


def test_the_lock_is_exposed_for_read_modify_write(roots: tuple[Path, Path]) -> None:
    store = _store(roots)

    with store.locked_session_files() as directory:
        assert directory == store.sessions_dir


def test_a_read_modify_write_under_the_lock_keeps_both_turns(
    roots: tuple[Path, Path],
) -> None:
    """The fix, stated as the property it buys."""
    a, b = _store(roots), _store(roots)
    a.save(_session("chat:1", "seed"))

    for store, text in ((a, "A-second"), (b, "B-second")):
        with store.locked_session_files():
            loaded = store.load("chat:1")
            assert loaded is not None
            loaded.messages.append({"role": "user", "content": text})
            store.save(loaded)

    final = a.load("chat:1")
    assert final is not None
    contents = [m["content"] for m in final.messages]
    assert contents == ["seed", "A-second", "B-second"]


def test_the_lock_is_reentrant_so_nesting_does_not_deadlock(
    roots: tuple[Path, Path],
) -> None:
    """Every public operation takes the lock, so they must nest inside a held one."""
    store = _store(roots)

    with store.locked_session_files():
        store.save(_session("chat:1", "hello"))
        assert store.load("chat:1") is not None
        assert store.read("chat:1") is not None
        assert store.read_metadata("chat:1") is not None
        assert any(info["key"] == "chat:1" for info in store.list_sessions())
        assert store.delete("chat:1") is True


def test_the_lock_file_lives_beside_the_sessions(roots: tuple[Path, Path]) -> None:
    store = _store(roots)

    with store.locked_session_files():
        pass

    assert (store.sessions_dir / ".session-files.lock").exists()


def test_the_lock_file_is_not_mistaken_for_a_session(roots: tuple[Path, Path]) -> None:
    """list_sessions globs the directory the lock file now sits in."""
    store = _store(roots)
    store.save(_session("chat:1", "hello"))

    keys = [info["key"] for info in store.list_sessions()]

    assert keys == ["chat:1"]


def _hold_then_write(sessions_root: str, workspace: str, text: str, ready, done) -> None:
    """Child process: take the lock, append under it, release."""
    store = JsonlSessionStore(Path(workspace), sessions_root=Path(sessions_root))
    with store.locked_session_files():
        ready.set()
        loaded = store.load("chat:1")
        if loaded is not None:
            loaded.messages.append({"role": "user", "content": text})
            store.save(loaded)
    done.set()


def test_the_lock_holds_across_real_processes(roots: tuple[Path, Path]) -> None:
    """A thread lock would pass the tests above and still lose a turn to a second process."""
    workspace, sessions_root = roots
    store = _store(roots)
    store.save(_session("chat:1", "seed"))

    ctx = multiprocessing.get_context("spawn")
    ready, done = ctx.Event(), ctx.Event()
    children = [
        ctx.Process(
            target=_hold_then_write,
            args=(str(sessions_root), str(workspace), f"child-{i}", ready, done),
        )
        for i in range(3)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=60)
        assert child.exitcode == 0, "a child failed or deadlocked on the lock"

    final = store.load("chat:1")
    assert final is not None
    contents = [m["content"] for m in final.messages]
    assert contents[0] == "seed"
    # Every child's turn survived, in some order.
    assert sorted(contents[1:]) == ["child-0", "child-1", "child-2"]
