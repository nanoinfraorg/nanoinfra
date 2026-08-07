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
