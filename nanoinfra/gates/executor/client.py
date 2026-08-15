"""The agent side of the executor wire -- nanoinfraorg/nanoinfra#18.

This module is agent-side code, so it holds no credential and no transport. It writes one frame
and reads one frame. A test asserts that it imports neither the secret store nor a backend,
because the import direction is what keeps the split true.

``ExecutorUnavailableError`` is separate from a refusal on purpose. "The executor is not running" and
"the gate said no" need different words for an operator: the first is a deployment fault and the
second is a policy decision. A caller that conflates them teaches an operator to read a broken
deployment as a policy problem.

**The origin path is not a parameter (#38).** #13 refuses an approval that arrives on the path
that raised the request, so the executor needs that path. This module reads it from the bound
request context, which the channel adapter set. A parameter would let a caller beside the model
name any path, and the binding between the identity and the transport would go. An unbound
context yields no path, which fails closed at the gate.

The call also blocks for as long as an operator takes to answer. The executor holds the
deadline, so this side sets no read timeout past the connect. See
``nanoinfra/gates/pending.py`` for the four reasons the wait blocks rather than polls.
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
            origin_path=_origin_path(),
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


def _origin_path() -> str | None:
    """Return the channel that raised this turn, or None when nothing is bound.

    The import stays inside the function. ``nanoinfra.agent`` runs its package init on import,
    and this module sits under ``nanoinfra.gates``, so a module level import would tie the two
    trees together in one more place for one field.
    """
    from nanoinfra.agent.tools.context import current_request_context

    context = current_request_context()
    if context is None:
        return None
    channel = context.channel.strip()
    return channel or None


__all__ = ["DEFAULT_CONNECT_TIMEOUT_S", "ExecutorClient", "ExecutorUnavailableError"]
