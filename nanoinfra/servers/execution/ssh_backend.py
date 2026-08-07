"""SSH execution backend, via asyncssh.

Uses create_process (not the simpler run() convenience method) so
partial output can be reported to on_activity as it streams -- run()
only returns everything at once on completion, which would make the
idle-timeout tracker's "reset on activity" meaningless for this backend.

Target validation (loopback/link-local/metadata) happens in Task 8's
execute_on_server before this backend is ever called, not here -- this
backend's only job is the connection itself.
"""

from __future__ import annotations

import asyncssh

from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.types import Server

DEFAULT_IDLE_TIMEOUT_S = 120
_READ_CHUNK_SIZE = 4096


def _looks_like_private_key(value: str) -> bool:
    return "PRIVATE KEY" in value


class SSHBackend:
    async def run(self, server: Server, command: str, secret_value, *, on_activity) -> ExecutionResult:  # noqa: ANN001
        host = server.config.get("host", "")
        port = int(server.config.get("port") or 22)
        username = server.config.get("username") or None

        connect_kwargs: dict[str, object] = {"host": host, "port": port, "username": username, "known_hosts": None}
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
                    stdout_parts: list[str] = []
                    stderr_parts: list[str] = []
                    while True:
                        chunk = await process.stdout.read(_READ_CHUNK_SIZE)
                        if not chunk:
                            break
                        text = chunk.decode("utf-8", errors="replace")
                        stdout_parts.append(text)
                        on_activity(text)
                    while True:
                        chunk = await process.stderr.read(_READ_CHUNK_SIZE)
                        if not chunk:
                            break
                        text = chunk.decode("utf-8", errors="replace")
                        stderr_parts.append(text)
                        on_activity(text)
                    completed = await process.wait()
                    output = "".join(stdout_parts)
                    if stderr_parts:
                        output += "\nSTDERR:\n" + "".join(stderr_parts)
                    return ExecutionResult(exit_code=completed.exit_status, output=output, error=None)
        except Exception as exc:  # noqa: BLE001 -- connection/auth failures must return, not raise
            return ExecutionResult(exit_code=None, output="", error=str(exc))


__all__ = ["DEFAULT_IDLE_TIMEOUT_S", "SSHBackend"]
