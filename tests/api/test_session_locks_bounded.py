"""The API's per-session lock map must not grow without bound, and must stay correct.

`session_id` arrives in the request body, so a caller names as many sessions as it likes and a
plain dict grows one lock per id forever. Bounding it is easy; bounding it *without* breaking
serialization is the actual problem, because a lock evicted while in use hands the next request for
that session a different object. Addresses upstream HKUDS/nanobot#4883 (nanoinfraorg/nanoinfra#147).
"""

from __future__ import annotations

import asyncio

import pytest

from nanoinfra.api.server import SessionLocks


async def test_the_map_is_bounded_once_locks_go_idle() -> None:
    locks = SessionLocks(max_idle=4)

    for i in range(50):
        async with locks.acquire(f"api:{i}"):
            pass

    assert len(locks) <= 4


async def test_the_same_session_serializes() -> None:
    """The property the bound must not break."""
    locks = SessionLocks(max_idle=4)
    order: list[str] = []

    async def worker(name: str) -> None:
        async with locks.acquire("api:same"):
            order.append(f"{name}-in")
            await asyncio.sleep(0.01)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))

    # No interleaving: each worker's in/out are adjacent.
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


async def test_different_sessions_do_not_block_each_other() -> None:
    locks = SessionLocks(max_idle=8)
    started = asyncio.Event()

    async def holder() -> None:
        async with locks.acquire("api:one"):
            started.set()
            await asyncio.sleep(0.2)

    async def other() -> None:
        await started.wait()
        async with locks.acquire("api:two"):
            return

    # If sessions shared a lock this would time out.
    await asyncio.wait_for(asyncio.gather(holder(), other()), timeout=2.0)


async def test_a_lock_in_use_is_never_evicted() -> None:
    """The subtle half. Eviction under pressure must not split a held session's lock."""
    locks = SessionLocks(max_idle=1)
    inside = asyncio.Event()
    release = asyncio.Event()
    seen: list[int] = []

    async def holder() -> None:
        async with locks.acquire("api:held"):
            inside.set()
            await release.wait()

    async def churn() -> None:
        await inside.wait()
        for i in range(20):
            async with locks.acquire(f"api:other-{i}"):
                pass
        # The held session must still be tracked as in use throughout.
        seen.append(1 if locks.in_use("api:held") else 0)
        release.set()

    await asyncio.gather(holder(), churn())

    assert seen == [1], "the held lock was evicted while a request owned it"


async def test_a_waiting_request_keeps_the_lock_alive() -> None:
    """A waiter counts as a user, or it would wake up holding an orphaned lock."""
    locks = SessionLocks(max_idle=1)
    first_inside = asyncio.Event()
    second_waiting = asyncio.Event()
    done: list[str] = []

    async def first() -> None:
        async with locks.acquire("api:contended"):
            first_inside.set()
            await second_waiting.wait()
            await asyncio.sleep(0.05)
            done.append("first")

    async def second() -> None:
        await first_inside.wait()
        second_waiting.set()
        async with locks.acquire("api:contended"):
            done.append("second")

    async def churn() -> None:
        await second_waiting.wait()
        for i in range(10):
            async with locks.acquire(f"api:x{i}"):
                pass

    await asyncio.gather(first(), second(), churn())

    # Serialization survived the churn: the waiter ran after the holder, not beside it.
    assert done == ["first", "second"]


async def test_the_bound_is_exceeded_rather_than_breaking_correctness() -> None:
    """When every lock is busy there is nothing safe to drop, so the cap yields."""
    locks = SessionLocks(max_idle=2)
    release = asyncio.Event()

    async def holder(key: str) -> None:
        async with locks.acquire(key):
            await release.wait()

    holders = [asyncio.create_task(holder(f"api:h{i}")) for i in range(6)]
    await asyncio.sleep(0.05)

    assert len(locks) == 6, "a busy lock must not be dropped to satisfy the bound"

    release.set()
    await asyncio.gather(*holders)


async def test_locks_are_released_even_when_the_body_raises() -> None:
    locks = SessionLocks(max_idle=4)

    with pytest.raises(RuntimeError):
        async with locks.acquire("api:boom"):
            raise RuntimeError("boom")

    assert not locks.in_use("api:boom")
    # And it can be taken again immediately.
    async with locks.acquire("api:boom"):
        pass


def test_a_nonpositive_bound_is_refused() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SessionLocks(max_idle=0)
