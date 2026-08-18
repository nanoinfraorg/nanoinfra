"""Per-session file state must not grow without bound.

FileStateStore held one entry per session key in a plain dict. A gateway sees an unbounded number
of session keys over its life, so that is a slow leak. Ported from upstream 42afebb0
(nanoinfraorg/nanoinfra#145).
"""

from __future__ import annotations

import pytest

from nanoinfra.agent.tools.file_state import FileStates, FileStateStore


def test_the_store_evicts_the_least_recently_used_session() -> None:
    store = FileStateStore(max_sessions=2)

    first = store.for_session("a")
    store.for_session("b")
    store.for_session("c")

    # "a" was the oldest, so it is gone and a fresh tracker comes back.
    assert store.for_session("a") is not first


def test_touching_a_session_keeps_it_alive() -> None:
    """LRU, not FIFO: use of a key has to protect it."""
    store = FileStateStore(max_sessions=2)
    first = store.for_session("a")
    store.for_session("b")

    store.for_session("a")  # "a" becomes most recent, so "b" is now the oldest
    store.for_session("c")

    assert store.for_session("a") is first
    assert store.for_session("b") is not first


def test_the_same_session_gets_the_same_tracker() -> None:
    store = FileStateStore()

    assert store.for_session("a") is store.for_session("a")


def test_a_missing_key_uses_the_shared_default() -> None:
    store = FileStateStore()

    assert store.for_session(None) is store.for_session(None)
    assert store.for_session(None) is not store.for_session("a")


def test_discard_forgets_one_session() -> None:
    store = FileStateStore()
    first = store.for_session("a")
    other = store.for_session("b")

    store.discard("a")

    assert store.for_session("a") is not first
    assert store.for_session("b") is other


def test_discard_of_an_unknown_key_is_harmless() -> None:
    store = FileStateStore()

    store.discard("never-seen")
    store.discard(None)


def test_clear_forgets_everything() -> None:
    store = FileStateStore()
    first = store.for_session("a")

    store.clear()

    assert store.for_session("a") is not first


def test_a_nonpositive_bound_is_refused() -> None:
    """A zero bound would evict the entry it just created on every call."""
    with pytest.raises(ValueError, match="must be positive"):
        FileStateStore(max_sessions=0)
    with pytest.raises(ValueError, match="must be positive"):
        FileStateStore(max_sessions=-1)


def test_eviction_returns_a_usable_tracker() -> None:
    """Losing an entry costs a read-dedup miss, never a broken tracker."""
    store = FileStateStore(max_sessions=1)
    store.for_session("a")

    replacement = store.for_session("b")

    assert isinstance(replacement, FileStates)
    replacement.record_write(__file__)
