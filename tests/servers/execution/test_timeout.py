# tests/servers/execution/test_timeout.py
from __future__ import annotations

import asyncio

import pytest

from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.execution.timeout import IdleTimeoutTracker, run_with_idle_timeout


def test_touch_resets_the_idle_clock():
    tracker = IdleTimeoutTracker(idle_timeout_s=10, absolute_ceiling_s=1800)
    assert tracker.remaining_s() > 9.9
    tracker.touch()
    assert tracker.remaining_s() > 9.9  # still fresh right after touch


def test_expired_false_when_fresh():
    tracker = IdleTimeoutTracker(idle_timeout_s=10, absolute_ceiling_s=1800)
    assert tracker.expired() is False


@pytest.mark.asyncio
async def test_expired_true_after_idle_timeout_elapses():
    tracker = IdleTimeoutTracker(idle_timeout_s=0.05, absolute_ceiling_s=1800)
    await asyncio.sleep(0.1)
    assert tracker.expired() is True


@pytest.mark.asyncio
async def test_touch_prevents_idle_expiry():
    tracker = IdleTimeoutTracker(idle_timeout_s=0.1, absolute_ceiling_s=1800)
    for _ in range(4):
        await asyncio.sleep(0.05)
        tracker.touch()
    assert tracker.expired() is False


@pytest.mark.asyncio
async def test_absolute_ceiling_fires_even_with_constant_activity():
    tracker = IdleTimeoutTracker(idle_timeout_s=10, absolute_ceiling_s=0.1)
    for _ in range(4):
        await asyncio.sleep(0.05)
        tracker.touch()
    assert tracker.expired() is True


@pytest.mark.asyncio
async def test_run_with_idle_timeout_returns_normal_result_when_fast():
    tracker = IdleTimeoutTracker(idle_timeout_s=10, absolute_ceiling_s=1800)

    async def fast_run() -> ExecutionResult:
        return ExecutionResult(exit_code=0, output="done", error=None, timed_out=False)

    result = await run_with_idle_timeout(fast_run(), tracker)
    assert result.exit_code == 0
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_run_with_idle_timeout_cancels_when_idle_expires():
    tracker = IdleTimeoutTracker(idle_timeout_s=0.05, absolute_ceiling_s=1800)

    async def hangs_forever() -> ExecutionResult:
        await asyncio.sleep(999)
        return ExecutionResult(exit_code=0, output="unreachable", error=None, timed_out=False)

    result = await run_with_idle_timeout(hangs_forever(), tracker, poll_interval_s=0.02)
    assert result.timed_out is True
    assert result.exit_code is None


class _FakeClock:
    """A controllable clock: advance() moves time forward deterministically,
    with no dependence on real wall-clock sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


def test_idle_expiry_is_deterministic_with_fake_clock():
    clock = _FakeClock()
    tracker = IdleTimeoutTracker(idle_timeout_s=10, absolute_ceiling_s=1800, clock=clock)

    clock.advance(5)
    assert tracker.expired() is False  # 5s idle, 10s idle budget: not expired yet

    tracker.touch()
    clock.advance(9)
    assert tracker.expired() is False  # touch reset the idle clock, so 9s since is fine

    clock.advance(2)
    assert tracker.expired() is True  # 11s since last touch exceeds the 10s idle budget


def test_absolute_ceiling_is_deterministic_with_fake_clock():
    clock = _FakeClock()
    tracker = IdleTimeoutTracker(idle_timeout_s=1000, absolute_ceiling_s=20, clock=clock)

    for _ in range(4):
        clock.advance(5)
        tracker.touch()  # constant activity keeps the idle clock fresh...

    # ...but 20s have elapsed since _start, which the ceiling always enforces.
    assert tracker.expired() is True


@pytest.mark.asyncio
async def test_timeout_returns_partial_output_instead_of_discarding_it():
    """Cancelling the backend coroutine destroys its own buffers, so the timeout
    path used to return output="" no matter how much had already been read --
    discarding output on precisely the case a user most needs it for."""
    tracker = IdleTimeoutTracker(idle_timeout_s=0.05, absolute_ceiling_s=1800)
    seen: list[str] = []

    async def streams_then_hangs() -> ExecutionResult:
        seen.append("line-1\n")
        seen.append("line-2\n")
        await asyncio.sleep(999)
        return ExecutionResult(exit_code=0, output="unreachable", error=None)

    result = await run_with_idle_timeout(
        streams_then_hangs(),
        tracker,
        poll_interval_s=0.02,
        partial_output=lambda: "".join(seen),
    )

    assert result.timed_out is True
    assert result.output == "line-1\nline-2\n"


@pytest.mark.asyncio
async def test_timeout_without_a_partial_output_getter_still_reports_empty_output():
    tracker = IdleTimeoutTracker(idle_timeout_s=0.05, absolute_ceiling_s=1800)

    async def hangs_forever() -> ExecutionResult:
        await asyncio.sleep(999)
        return ExecutionResult(exit_code=0, output="unreachable", error=None)

    result = await run_with_idle_timeout(hangs_forever(), tracker, poll_interval_s=0.02)

    assert result.timed_out is True
    assert result.output == ""
