"""The executor side of the connector host wire (#195, part 4).

This module runs in the **executor**, not in the agent: the agent never speaks to this socket, and
the host's group does not include the agent's account. That is the difference from the MCP host,
whose group does -- an MCP tool call originates in the agent, and a connector call originates in the
executor after the gate answered.

One connection per call. A pooled connection would be a token-carrying channel kept open across
actions, and this wire's whole point is that nothing outlives the request it belongs to.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.gates.connector_host.protocol import (
    ConnectorHostRequest,
    ConnectorHostResponse,
    ProtocolError,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)

DEFAULT_SOCKET_NAME = "connector_host.sock"

#: The deployment names the socket, for the reason the other helpers do: write rights on a parent
#: directory allow a rename of any entry inside it, so a directory the caller can write is a
#: directory where a caller could present its own socket.
SOCKET_ENV_VAR = "NANOINFRA_CONNECTOR_HOST_SOCKET"

DEFAULT_CONNECT_TIMEOUT_S = 10.0


class ConnectorHostError(RuntimeError):
    """The host answered with a refusal or a failure."""


class ConnectorHostUnavailableError(ConnectorHostError):
    """The host could not be reached. A deployment fault, not a refused action.

    Kept apart from `ConnectorHostError` for the reason the MCP client keeps its two words apart:
    a caller that conflates them teaches an operator to read a broken deployment as a broken
    package.
    """


def default_socket_path() -> Path:
    named = os.environ.get(SOCKET_ENV_VAR, "").strip()
    if named:
        return Path(named)
    from nanoinfra.config.paths import get_data_dir

    return get_data_dir() / "run" / DEFAULT_SOCKET_NAME


class ConnectorHostClient:
    """One call per connection, over the host's socket."""

    def __init__(self, socket_path: Path | str | None = None) -> None:
        self._socket_path = Path(socket_path) if socket_path else default_socket_path()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def available(self) -> bool:
        return self._socket_path.exists()

    async def call(
        self,
        *,
        package: str,
        operation: str,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        access_token: str,
        timeout_s: float = 30.0,
    ) -> ConnectorHostResponse:
        """Send one rendered request and return the host's answer."""
        request = ConnectorHostRequest(
            request_id=1,
            package=package,
            operation=operation,
            method=method.upper(),
            url=url,
            headers=dict(headers or {}),
            query=dict(query or {}),
            body=body,
            access_token=access_token,
            timeout_s=timeout_s,
        )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)),
                timeout=DEFAULT_CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectorHostUnavailableError(
                f"the connector host at {self._socket_path} is not reachable: {exc}"
            ) from exc
        try:
            await write_frame(writer, encode_request(request))
            payload = await asyncio.wait_for(read_frame(reader), timeout=timeout_s + 5.0)
            response = decode_response(payload)
        except ProtocolError as exc:
            raise ConnectorHostUnavailableError(f"the connector host wire failed: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise ConnectorHostUnavailableError(
                "the connector host did not answer within the deadline"
            ) from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if response.request_id != request.request_id:
            # One call per connection, so a mismatched id means the wire is confused rather than
            # late. Refused instead of read as this call's answer.
            raise ConnectorHostUnavailableError("the connector host answered another request")
        logger.debug(
            "connector host answered {}/{}: ok={} status={}",
            package,
            operation,
            response.ok,
            response.status,
        )
        return response


__all__ = [
    "DEFAULT_SOCKET_NAME",
    "SOCKET_ENV_VAR",
    "ConnectorHostClient",
    "ConnectorHostError",
    "ConnectorHostUnavailableError",
    "default_socket_path",
]
