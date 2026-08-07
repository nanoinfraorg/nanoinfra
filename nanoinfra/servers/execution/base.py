"""Shared execution result/interface for all four Server backends."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from nanoinfra.servers.types import Server

# Absolute wall-clock ceiling for one execution, regardless of activity. Lives
# here rather than in the tool module so backends that need to size their own
# transport timeouts (api_backend's httpx client) can stay consistent with the
# orchestrator that will actually cancel them.
ABSOLUTE_CEILING_S = 1800

# Same budget and truncation-note wording as
# nanoinfra/agent/tools/exec_session.py's MAX_OUTPUT_CHARS, so a command's output
# is capped the same way whether it ran locally through the exec tool or remotely
# through a Server backend. Deliberately not imported from there: that module is
# an agent tool with a much heavier import graph, and nothing in
# nanoinfra/servers/ should depend on it.
MAX_OUTPUT_CHARS = 50_000


class BoundedOutput:
    """Accumulate streamed chunks under a fixed character budget.

    Keeps the head and the most recent tail (the head shows what the command
    started doing, the tail usually carries the failure), and reports how much
    was dropped in between. Without this, a single chatty remote command could
    accumulate unbounded output in the gateway's memory, then have all of it
    written into a ServerJob JSON file and handed to the model.
    """

    def __init__(self, max_chars: int = MAX_OUTPUT_CHARS) -> None:
        self.max_chars = max(2, max_chars)
        self._head = ""
        self._tail: deque[str] = deque()
        self._tail_chars = 0
        self._total_chars = 0

    @property
    def total_chars(self) -> int:
        return self._total_chars

    def append(self, text: str) -> None:
        if not text:
            return
        self._total_chars += len(text)
        head_budget = self.max_chars // 2
        if len(self._head) < head_budget:
            take = head_budget - len(self._head)
            self._head += text[:take]
            text = text[take:]
            if not text:
                return

        self._tail.append(text)
        self._tail_chars += len(text)
        tail_budget = self.max_chars - head_budget
        while self._tail_chars > tail_budget:
            excess = self._tail_chars - tail_budget
            first = self._tail[0]
            if len(first) <= excess:
                self._tail.popleft()
                self._tail_chars -= len(first)
            else:
                self._tail[0] = first[excess:]
                self._tail_chars -= excess

    def text(self) -> str:
        """Retained output, with a truncation note appended when anything was dropped."""
        retained = self._head + "".join(self._tail)
        omitted = self._total_chars - len(retained)
        if omitted <= 0:
            return retained
        return f"{retained}\n({omitted:,} chars truncated from output)"


def truncate_output(output: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Cap an already-complete output string, keeping head and tail.

    Applied once centrally by execute_on_server to whatever any backend returns,
    so no backend has to remember to do it and the truncation logic exists in
    exactly one place.
    """
    if len(output) <= max_chars:
        return output
    buffer = BoundedOutput(max_chars)
    buffer.append(output)
    return buffer.text()


@dataclass
class ExecutionResult:
    exit_code: int | None
    output: str
    error: str | None
    timed_out: bool = False


class ExecutionBackend(Protocol):
    """One implementation per providerId: ssh, ansible-runner, ssm, api.

    ``on_activity`` is the IdleTimeoutTracker's ``touch()``, wired in by the
    caller. What a backend passes to it differs, and callers must not assume it
    is output text:

    - ssh streams real output chunks as they arrive, so the idle clock genuinely
      resets mid-run and the chunks are the command's own output.
    - ansible-runner, ssm and api have no incremental signal (one blocking
      library call, one poll-to-terminal-status loop, one request/response), so
      each calls ``on_activity`` exactly once with a short status token
      ("successful", "Success", "200") immediately before returning. That is
      harmless -- it only ever moves the idle clock forward, never past the
      absolute ceiling -- but it is a status token, not output.

    secret_value is the already-decrypted credential (or None if the Server has
    no secretRef) -- backends must never log it or place it in the returned
    ExecutionResult.

    Backends should keep their own accumulation bounded (see BoundedOutput);
    execute_on_server additionally applies truncate_output() to whatever comes
    back before persisting or returning it.
    """

    async def run(
        self,
        server: Server,
        command: str,
        secret_value: str | None,
        *,
        on_activity: Callable[[str], None],
    ) -> ExecutionResult: ...


__all__ = [
    "ABSOLUTE_CEILING_S",
    "MAX_OUTPUT_CHARS",
    "BoundedOutput",
    "ExecutionBackend",
    "ExecutionResult",
    "truncate_output",
]
