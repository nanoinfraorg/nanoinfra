"""Shared execution result/interface for all four Server backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from nanoinfra.servers.types import Server


@dataclass
class ExecutionResult:
    exit_code: int | None
    output: str
    error: str | None
    timed_out: bool = False


class ExecutionBackend(Protocol):
    """One implementation per providerId: ssh, ansible-runner, ssm, api.

    ``on_activity`` is called with each new output chunk as it arrives, for
    backends that can report it (ssh, ansible-runner) -- IdleTimeoutTracker's
    ``touch()`` is wired to it by the caller. Backends without a meaningful
    activity signal (ssm, api -- a single request/response) simply never
    call it; that's a deliberate difference in what "smart timeout" means
    per backend, not a gap. secret_value is the already-decrypted credential
    (or None if the Server has no secretRef) -- backends must never log it
    or place it in the returned ExecutionResult.
    """

    async def run(
        self,
        server: Server,
        command: str,
        secret_value: str | None,
        *,
        on_activity: Callable[[str], None],
    ) -> ExecutionResult: ...


__all__ = ["ExecutionBackend", "ExecutionResult"]
