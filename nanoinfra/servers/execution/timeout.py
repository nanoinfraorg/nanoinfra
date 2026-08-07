"""Idle-based smart timeout -- resets on real activity, capped by an
absolute ceiling regardless of activity.

Model borrowed from nanoinfra/agent/tools/exec_session.py's
ExecSessionManager (idle_timeout, last_access), adapted for a single
awaited coroutine instead of a long-lived polled session -- that file's
sessions are polled repeatedly by the caller across separate tool calls;
a Server execution job is one tool call that waits for the whole thing,
so the equivalent here is racing the backend coroutine against a
periodic expiry check rather than a poll() the caller drives itself.

Known limitation -- what "timed out" does and does not mean per backend:
cancelling the awaited coroutine stops *this process from waiting*, and
for SSH it also tears the asyncssh connection down, which really does end
the remote command. AnsibleRunnerBackend and SSMBackend, though, wrap a
blocking call in ``asyncio.to_thread``, and a thread that has already
started cannot be cancelled -- the ansible play keeps running to
completion in a pool thread, and an SSM command that has already been
sent keeps running on the instance. In those two cases a reported timeout
means "this tool stopped waiting and discarded the result", NOT "the
remote work stopped". Callers must surface that distinction rather than
implying the command was stopped (see execute_on_server's timeout
message), because a blind retry can otherwise put two copies of the same
command in flight. Fixing it properly needs per-provider cancellation
(SSM's CommandId makes this feasible; ansible-runner's does not without
process-level isolation) and is out of scope here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any, Callable

from nanoinfra.servers.execution.base import ExecutionResult

_DEFAULT_POLL_INTERVAL_S = 1.0


class IdleTimeoutTracker:
    def __init__(
        self,
        idle_timeout_s: float,
        absolute_ceiling_s: float = 1800,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_timeout_s = idle_timeout_s
        self.absolute_ceiling_s = absolute_ceiling_s
        self._clock = clock
        self._start = self._clock()
        self._last_activity = self._start

    def touch(self, _chunk: str = "") -> None:
        """Signature accepts an (unused) chunk so it can be passed directly
        as an ExecutionBackend's on_activity callback."""
        self._last_activity = self._clock()

    def remaining_s(self) -> float:
        now = self._clock()
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
    partial_output: Callable[[], str] | None = None,
) -> ExecutionResult:
    """Run ``coro`` to completion, or cancel it once ``tracker`` expires.

    ``partial_output`` is an optional getter for whatever output the caller has
    accumulated so far (streaming backends report chunks through ``on_activity``).
    Cancelling the backend coroutine destroys its own buffers, so without this the
    timeout path threw away everything already read -- exactly the output a user
    most needs to see when asking "what did it get through before it hung?".
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            remaining = tracker.remaining_s()
            if remaining <= 0:
                # Deliberate tie-break: if the backend coroutine finishes at the exact
                # instant the deadline hits, we still discard its result and report
                # timed_out=True -- "timeout wins" on ties, rather than racing to see
                # which check the event loop happens to schedule first.
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                recovered = partial_output() if partial_output is not None else ""
                return ExecutionResult(
                    exit_code=None,
                    output=recovered,
                    error="Idle/absolute timeout exceeded",
                    timed_out=True,
                )
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=min(remaining, poll_interval_s))
            except asyncio.TimeoutError:
                continue
    finally:
        if not task.done():
            task.cancel()


__all__ = ["IdleTimeoutTracker", "run_with_idle_timeout"]
