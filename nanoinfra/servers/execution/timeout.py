"""Idle-based smart timeout -- resets on real activity, capped by an
absolute ceiling regardless of activity.

Model borrowed from nanoinfra/agent/tools/exec_session.py's
ExecSessionManager (idle_timeout, last_access), adapted for a single
awaited coroutine instead of a long-lived polled session -- that file's
sessions are polled repeatedly by the caller across separate tool calls;
a Server execution job is one tool call that waits for the whole thing,
so the equivalent here is racing the backend coroutine against a
periodic expiry check rather than a poll() the caller drives itself.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from nanoinfra.servers.execution.base import ExecutionResult

_DEFAULT_POLL_INTERVAL_S = 1.0


class IdleTimeoutTracker:
    def __init__(self, idle_timeout_s: float, absolute_ceiling_s: float = 1800) -> None:
        self.idle_timeout_s = idle_timeout_s
        self.absolute_ceiling_s = absolute_ceiling_s
        self._start = time.monotonic()
        self._last_activity = self._start

    def touch(self, _chunk: str = "") -> None:
        """Signature accepts an (unused) chunk so it can be passed directly
        as an ExecutionBackend's on_activity callback."""
        self._last_activity = time.monotonic()

    def remaining_s(self) -> float:
        now = time.monotonic()
        idle_remaining = self.idle_timeout_s - (now - self._last_activity)
        ceiling_remaining = self.absolute_ceiling_s - (now - self._start)
        return max(0.0, min(idle_remaining, ceiling_remaining))

    def expired(self) -> bool:
        return self.remaining_s() <= 0


async def run_with_idle_timeout(
    coro: Coroutine[Any, Any, ExecutionResult],
    tracker: IdleTimeoutTracker,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> ExecutionResult:
    """Run ``coro`` to completion, or cancel it once ``tracker`` expires."""
    task = asyncio.ensure_future(coro)
    try:
        while True:
            remaining = tracker.remaining_s()
            if remaining <= 0:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                return ExecutionResult(exit_code=None, output="", error="Idle/absolute timeout exceeded", timed_out=True)
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=min(remaining, poll_interval_s))
            except asyncio.TimeoutError:
                continue
    finally:
        if not task.done():
            task.cancel()


__all__ = ["IdleTimeoutTracker", "run_with_idle_timeout"]
