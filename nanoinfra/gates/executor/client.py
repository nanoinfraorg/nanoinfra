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

**The origin identity is not a parameter either (#47, item 10).** It comes from the same bound
context, and for the same reason. The channel adapter authenticated the sender, so the adapter
is the only honest source. A keyword on ``execute`` would also break the SDK stand-in of #21,
which mirrors this signature.

**The delegation is not a parameter either (#251).** Which agent acts, which agent asked, and
what the asking turn was allowed to do all come from the bound context and from the ceiling the
runner declared around the delegated turn. A keyword would let the tool that asks describe its
own authority, and the whole point of the rule is that it is decided where the gate decides.

The call also blocks for as long as an operator takes to answer. The executor holds the
deadline, so this side sets no read timeout past the connect. See
``nanoinfra/gates/pending.py`` for the four reasons the wait blocks rather than polls.
"""

from __future__ import annotations

import socket
from pathlib import Path

from nanoinfra.gates.executor.protocol import (
    ConnectorRequest,
    ExecuteRequest,
    ExecuteResponse,
    ProtocolError,
    Request,
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
        return self._send(
            ExecuteRequest(
                server_id_or_name=server_id_or_name,
                command=command,
                session_id=session_id,
                execution_context=execution_context,
                preview_requested=preview_requested,
                timeout_s=timeout_s,
                token_nonce=token_nonce,
                origin_path=_origin_path(),
                origin_actor=_origin_actor(),
                acting_agent=_acting_agent(),
                delegated_by=_delegated_by(),
                inherited_capabilities=_inherited_capabilities(),
            )
        )

    def connector_call(
        self,
        *,
        connector: str,
        operation: str,
        arguments_json: str,
        session_id: str | None,
        execution_context: str,
        preview_requested: bool = False,
    ) -> ExecuteResponse:
        """Ask the executor to perform one connector operation, and return its answer.

        No token, no method and no URL cross this wire: the executor reads them from the
        installed manifest, so a caller beside the model can name a connector and cannot
        describe a call the package never declared.

        There is no ``token_nonce`` parameter, and that is deliberate rather than an omission.
        The executor issues every nonce and hands none to the agent, so this side has none to
        pass, and the field on the frame exists only to be refused.
        """
        return self._send(
            ConnectorRequest(
                connector=connector,
                operation=operation,
                arguments_json=arguments_json,
                session_id=session_id,
                execution_context=execution_context,
                preview_requested=preview_requested,
                token_nonce=None,
                origin_path=_origin_path(),
                origin_actor=_origin_actor(),
                acting_agent=_acting_agent(),
                delegated_by=_delegated_by(),
                inherited_capabilities=_inherited_capabilities(),
            )
        )

    def _send(self, request: Request) -> ExecuteResponse:
        """One frame out, one frame back. Both request kinds share it.

        A second copy of this would be a second place for the timeout rule to drift, and the
        rule is the interesting part: no read deadline past the connect, because the executor
        owns the wait and a deadline here would report a long action as an unavailable executor.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self._connect_timeout_s)
                conn.connect(str(self._socket_path))
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


def _origin_actor() -> str | None:
    """Return the person the channel authenticated for this turn, or None.

    The value is the channel's own sender id, which is the vocabulary ``gates.approvers`` uses.
    A channel that authenticated nobody, and an unbound context, both answer None. None is not
    the empty string here: #13 reads None as "unknown" and falls back to the path rule alone,
    and empty text would read as a name.
    """
    from nanoinfra.agent.tools.context import current_request_context

    context = current_request_context()
    if context is None:
        return None
    sender = (context.sender_id or "").strip()
    return sender or None


def _inherited_capabilities() -> list[str]:
    """Return the capability classes the delegating turn held, sorted.

    Empty on every turn nothing delegated, and empty on a delegation whose spawning turn declared
    no ceiling. ``nanoinfra/gates/delegation.py`` says why the second case binds nothing.

    The import stays inside the function for the reason the two above do: that module reads the
    capability vocabulary from the agent tree, and this file must not tie the two trees together
    at import time.
    """
    from nanoinfra.gates.delegation import current_inherited_capabilities

    return sorted(current_inherited_capabilities())


def _acting_agent() -> str | None:
    """Return the named agent answering this turn, or None when no agent is named.

    ``None`` on every turn of a deployment with one agent, which is what keeps the record and the
    approval prompt unchanged there. The name is normalised on arrival rather than here, because
    the executor must not trust this side to have done it.
    """
    from nanoinfra.agent.tools.context import current_request_context

    context = current_request_context()
    if context is None:
        return None
    return (context.agent or "").strip() or None


def _delegated_by() -> str | None:
    """Return the agent that delegated this turn, or None when nothing delegated it.

    Its presence is what makes the turn a delegation rather than a named agent answering
    directly, and the gate reads the pair. See ``nanoinfra/gates/delegation.py``.
    """
    from nanoinfra.agent.tools.context import current_request_context

    context = current_request_context()
    if context is None:
        return None
    return (context.delegated_by or "").strip() or None


__all__ = ["DEFAULT_CONNECT_TIMEOUT_S", "ExecutorClient", "ExecutorUnavailableError"]
