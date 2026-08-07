"""SSH execution backend, via asyncssh.

Uses create_process (not the simpler run() convenience method) so
partial output can be reported to on_activity as it streams -- run()
only returns everything at once on completion, which would make the
idle-timeout tracker's "reset on activity" meaningless for this backend.

Host-key verification is deliberately disabled (``known_hosts=None``);
see .agent/security.md's "Server Execution Backends" section for the
accepted-risk record.

Target validation (loopback/link-local/metadata) happens in Task 8's
execute_on_server before this backend is ever called, not here -- this
backend's only job is the connection itself.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import asyncssh

from nanoinfra.servers.execution.base import BoundedOutput, ExecutionResult
from nanoinfra.servers.types import Server

DEFAULT_IDLE_TIMEOUT_S = 120
_READ_CHUNK_SIZE = 4096


def _looks_like_private_key(value: str) -> bool:
    return "PRIVATE KEY" in value


class SSHBackend:
    async def run(
        self,
        server: Server,
        command: str,
        secret_value: str | None,
        *,
        on_activity: Callable[[str], None],
    ) -> ExecutionResult:
        host = server.config.get("host", "")
        port = int(server.config.get("port") or 22)
        username = server.config.get("username") or None

        connect_kwargs: dict[str, object] = {
            "host": host,
            "port": port,
            "known_hosts": None,
            # asyncssh defaults to encoding="utf-8" with errors="strict" on its
            # process streams -- create_process().stdout/stderr would then
            # yield already-decoded str chunks, and a remote command emitting
            # one invalid UTF-8 byte would raise instead of returning bad
            # output. Force binary mode so _drain()'s own decode(errors=
            # "replace") is what actually handles malformed output.
            "encoding": None,
        }
        if username:
            # asyncssh's connect() expects this key omitted, not set to None,
            # when no username is configured -- passing username=None raises
            # TypeError: 'NoneType' object is not iterable. Omitting it lets
            # asyncssh fall back to its own default (the local process user).
            connect_kwargs["username"] = username
        if secret_value:
            if _looks_like_private_key(secret_value):
                connect_kwargs["client_keys"] = [asyncssh.import_private_key(secret_value)]
            else:
                connect_kwargs["password"] = secret_value

        try:
            conn = await asyncssh.connect(**connect_kwargs)
            async with conn:
                process = await conn.create_process(command)
                async with process:
                    # Bounded per stream: this is the one backend that streams
                    # unbounded remote output into gateway memory. The tool
                    # applies the final combined cap (truncate_output) before
                    # anything is persisted or returned.
                    stdout_parts = BoundedOutput()
                    stderr_parts = BoundedOutput()

                    async def _drain(stream: Any, sink: BoundedOutput) -> None:
                        while True:
                            chunk = await stream.read(_READ_CHUNK_SIZE)
                            if not chunk:
                                return
                            text = chunk.decode("utf-8", errors="replace")
                            sink.append(text)
                            on_activity(text)

                    # Both streams are drained CONCURRENTLY, never one after the
                    # other. asyncssh shares a single receive window across a
                    # channel's stdout and stderr: once buffered-but-unread data
                    # fills it (~2 MiB), asyncssh stops reading the channel
                    # entirely, so the remote's writes block. Draining stdout to
                    # EOF first would then deadlock on any command that emits
                    # enough stderr before closing stdout -- stdout never reaches
                    # EOF because the peer is blocked on stderr nobody is
                    # reading. Reading both keeps the window draining no matter
                    # which stream the output lands on.
                    await asyncio.gather(
                        _drain(process.stdout, stdout_parts),
                        _drain(process.stderr, stderr_parts),
                    )
                    completed = await process.wait()
                    output = stdout_parts.text()
                    if stderr_parts.total_chars:
                        output += "\nSTDERR:\n" + stderr_parts.text()
                    return ExecutionResult(exit_code=completed.exit_status, output=output, error=None)
        except Exception as exc:  # noqa: BLE001 -- connection/auth failures must return, not raise
            return ExecutionResult(exit_code=None, output="", error=str(exc))
        # Unreachable in practice: asyncssh's __aexit__ is typed to return bool (it
        # never actually suppresses an exception), which makes the type checker see
        # a path where control could fall past the async-with blocks above without
        # raising or returning. This satisfies strict-mode exhaustiveness for that
        # theoretical path without changing any real behavior.
        return ExecutionResult(exit_code=None, output="", error="unreachable: connection closed without a result")


__all__ = ["DEFAULT_IDLE_TIMEOUT_S", "SSHBackend"]
