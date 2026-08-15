"""The agent side of the executor wire -- nanoinfraorg/nanoinfra#18.

This module is agent-side code, so it holds no credential and no transport. It writes one frame
and reads one frame. A test asserts that it imports neither the secret store nor a backend,
because the import direction is what keeps the split true.

``ExecutorUnavailableError`` is separate from a refusal on purpose. "The executor is not running" and
"the gate said no" need different words for an operator: the first is a deployment fault and the
second is a policy decision. A caller that conflates them teaches an operator to read a broken
deployment as a policy problem.
"""

from __future__ import annotations

import socket
from pathlib import Path

from nanoinfra.gates.executor.protocol import (
    ExecuteRequest,
    ExecuteResponse,
    ProtocolError,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)

# A remote command can run for a long time, and the executor holds the idle timeout. So this is
# a connect-and-transfer guard, not a command timeout: it must not cut a running command short.
DEFAULT_CONNECT_TIMEOUT_S = 10.0


class ExecutorUnavailableError(RuntimeError):
    """The executor could not be reached, or it dropped the connection."""


class ExecutorClient:
    """One request per connection. The executor serves one at a time."""

    def __init__(
        self, socket_path: Path | str, *, connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    ) -> None:
        self._socket_path = Path(socket_path)
        self._connect_timeout_s = connect_timeout_s

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def execute(
        self,
        *,
        server_id_or_name: str,
        command: str,
        session_id: str | None,
        execution_context: str,
        preview_requested: bool,
        timeout_s: str | None,
        token_nonce: str | None = None,
    ) -> ExecuteResponse:
        """Send one request and return the executor's answer.

        Raises ``ExecutorUnavailableError`` when the socket is absent or the peer disappears. It
        never raises for a refusal, because a refusal is a response.
        """
        request = ExecuteRequest(
            server_id_or_name=server_id_or_name,
            command=command,
            session_id=session_id,
            execution_context=execution_context,
            preview_requested=preview_requested,
            timeout_s=timeout_s,
            token_nonce=token_nonce,
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self._connect_timeout_s)
                conn.connect(str(self._socket_path))
                # No read deadline past the connect: the executor owns the idle timeout, and a
                # deadline here would cut a long command short and report it as unavailable.
                conn.settimeout(None)
                write_frame(conn, encode_request(request))
                return decode_response(read_frame(conn))
        except (OSError, ProtocolError) as exc:
            raise ExecutorUnavailableError(
                f"Could not reach the executor at {self._socket_path}: {exc}"
            ) from exc


__all__ = ["DEFAULT_CONNECT_TIMEOUT_S", "ExecutorClient", "ExecutorUnavailableError"]
