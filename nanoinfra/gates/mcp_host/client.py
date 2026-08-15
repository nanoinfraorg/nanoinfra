"""The agent side of the MCP host wire -- nanoinfraorg/nanoinfra#22.

This module runs in the agent, so it starts no program and holds no MCP server. It opens one
connection per stdio MCP server, and it writes one frame per request. A test walks its whole
syntax tree and asserts it imports neither ``subprocess`` nor the host's server module, because
the import direction is what keeps the split true.

The session it returns looks like an MCP ``ClientSession`` to the tool wrappers. It validates each
reply back into the real SDK result model, so ``MCPToolWrapper`` reads ``result.content`` and
``result.isError`` exactly as it did before the split.

Two failure words stay apart on purpose. ``MCPHostUnavailableError`` means the host is not running
or the connection died, and that is a deployment fault. ``MCPHostError`` means the host answered
with a refusal, and that is a result. A caller that conflates them teaches an operator to read a
broken deployment as a broken MCP server.

The connection is the session. Closing it ends the stdio child in the host, so a dead agent leaves
no orphan MCP server behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.gates.mcp_host.protocol import (
    CallToolRequest,
    GetPromptRequest,
    HostRequest,
    HostResponse,
    ListPromptsRequest,
    ListResourcesRequest,
    ListToolsRequest,
    OpenRequest,
    ProtocolError,
    ReadResourceRequest,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)

if TYPE_CHECKING:
    from mcp.types import (
        CallToolResult,
        GetPromptResult,
        ListPromptsResult,
        ListResourcesResult,
        ListToolsResult,
        ReadResourceResult,
    )

# Where the host listens when a caller passes no path. The supervisor binds the same place.
DEFAULT_SOCKET_NAME = "mcp_host.sock"

# The deployment names the socket, because the socket directory belongs outside the agent's home.
# Write rights on a parent directory allow a rename of any entry inside it, so a directory the
# agent can write is a directory where the agent could present its own socket.
SOCKET_ENV_VAR = "NANOINFRA_MCP_HOST_SOCKET"

# The host answers an open in the time a stdio server needs to start. A tool call has no deadline
# here: the caller owns that timeout, and the wrappers already apply the server's tool_timeout.
DEFAULT_CONNECT_TIMEOUT_S = 30.0


class MCPHostError(RuntimeError):
    """The host answered with a refusal or a failure."""


class MCPHostUnavailableError(MCPHostError):
    """The host could not be reached, or it dropped the connection."""


def default_socket_path() -> Path:
    """Return the socket the host binds, from the environment or from the data directory.

    The environment wins, because only the deployment knows where it put the socket. The container
    puts it under /run on a root-owned path, and a pip install puts it in the data directory.
    """
    named = os.environ.get(SOCKET_ENV_VAR, "").strip()
    if named:
        return Path(named)

    from nanoinfra.config.paths import get_data_dir

    return get_data_dir() / "run" / DEFAULT_SOCKET_NAME


class MCPHostSession:
    """One MCP server that runs in the host, as the agent sees it.

    The method set matches the part of ``ClientSession`` the tool wrappers call. Each call sends
    one frame and awaits the reply that carries its own request id.
    """

    def __init__(
        self,
        server_name: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        socket_path: Path,
    ) -> None:
        self._server_name = server_name
        self._reader = reader
        self._writer = writer
        self._socket_path = socket_path
        self._pending: dict[int, asyncio.Future[HostResponse]] = {}
        self._next_id = 0
        self._closed = False
        self._broken: MCPHostUnavailableError | None = None
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    async def open(self) -> None:
        """Ask the host to start the configured server, and start the reply reader."""
        self._reader_task = asyncio.create_task(
            self._read_replies(), name=f"mcp-host-client:{self._server_name}"
        )
        await self._request(OpenRequest(request_id=self._take_id(), server_name=self._server_name))

    async def list_tools(self) -> ListToolsResult:
        from mcp.types import ListToolsResult

        payload = await self._request(ListToolsRequest(request_id=self._take_id()))
        return ListToolsResult.model_validate(payload)

    async def list_resources(self) -> ListResourcesResult:
        from mcp.types import ListResourcesResult

        payload = await self._request(ListResourcesRequest(request_id=self._take_id()))
        return ListResourcesResult.model_validate(payload)

    async def list_prompts(self) -> ListPromptsResult:
        from mcp.types import ListPromptsResult

        payload = await self._request(ListPromptsRequest(request_id=self._take_id()))
        return ListPromptsResult.model_validate(payload)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult:
        from mcp.types import CallToolResult

        payload = await self._request(
            CallToolRequest(
                request_id=self._take_id(), tool_name=name, arguments=dict(arguments or {})
            )
        )
        return CallToolResult.model_validate(payload)

    async def read_resource(self, uri: Any) -> ReadResourceResult:
        from mcp.types import ReadResourceResult

        payload = await self._request(
            ReadResourceRequest(request_id=self._take_id(), uri=str(uri))
        )
        return ReadResourceResult.model_validate(payload)

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> GetPromptResult:
        from mcp.types import GetPromptResult

        payload = await self._request(
            GetPromptRequest(
                request_id=self._take_id(), prompt_name=name, arguments=dict(arguments or {})
            )
        )
        return GetPromptResult.model_validate(payload)

    async def aclose(self) -> None:
        """Close the connection, which ends the stdio child in the host."""
        if self._closed:
            return
        self._closed = True
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        self._fail_pending(
            MCPHostUnavailableError(f"the MCP host session for {self._server_name!r} is closed")
        )

    def _take_id(self) -> int:
        # Ids start at 1, because 0 is what a refusal carries when no id could be read.
        self._next_id += 1
        return self._next_id

    async def _request(self, request: HostRequest) -> dict[str, Any]:
        """Send one request and return the result payload of its reply."""
        if self._broken is not None:
            raise self._broken
        if self._closed:
            raise MCPHostUnavailableError(
                f"the MCP host session for {self._server_name!r} is closed"
            )

        future: asyncio.Future[HostResponse] = asyncio.get_running_loop().create_future()
        self._pending[request.request_id] = future
        try:
            await write_frame(self._writer, encode_request(request))
            response = await future
        except ProtocolError as exc:
            raise MCPHostUnavailableError(
                f"Could not reach the MCP host at {self._socket_path}: {exc}"
            ) from exc
        finally:
            # A caller that times out abandons this request. The entry goes now, so a late reply
            # finds no future and gets dropped rather than read as the next answer.
            self._pending.pop(request.request_id, None)

        if not response.ok:
            raise _failure_of(response)
        return response.result or {}

    async def _read_replies(self) -> None:
        """Read every reply and hand it to the call that asked for it."""
        try:
            while True:
                response = decode_response(await read_frame(self._reader))
                future = self._pending.pop(response.request_id, None)
                if future is None:
                    logger.debug(
                        "MCP host: dropped a late reply for request {} of server '{}'",
                        response.request_id,
                        self._server_name,
                    )
                    continue
                if not future.done():
                    future.set_result(response)
        except asyncio.CancelledError:
            raise
        except (ProtocolError, OSError) as exc:
            # "connection closed" is load-bearing wording. ``_is_session_terminated`` in
            # ``nanoinfra/agent/tools/mcp.py`` reads it, and a host that restarts must leave the
            # agent able to reconnect rather than stuck for the life of the process.
            self._fail_pending(
                MCPHostUnavailableError(
                    f"The MCP host connection closed for {self._server_name!r}: {exc}"
                )
            )
        except Exception as exc:  # noqa: BLE001 -- the reader must fail the callers, not vanish
            logger.exception("MCP host: the reply reader for '{}' failed", self._server_name)
            self._fail_pending(
                MCPHostUnavailableError(
                    f"The MCP host connection closed for {self._server_name!r} on an error: {exc}"
                )
            )

    def _fail_pending(self, error: MCPHostUnavailableError) -> None:
        """Fail every waiting call, and refuse the next one for the same reason."""
        self._broken = error
        pending = list(self._pending.items())
        self._pending.clear()
        for _request_id, future in pending:
            if not future.done():
                future.set_exception(error)


def _failure_of(response: HostResponse) -> Exception:
    """Turn one failed reply into the exception the wrappers already handle.

    An ``McpError`` keeps its code and its message, so the prompt wrapper reports what the server
    said, and the reconnect path still recognises a terminated session.
    """
    if response.error_data is not None:
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData

        with contextlib.suppress(Exception):
            return McpError(ErrorData.model_validate(response.error_data))
    return MCPHostError(response.error or "the MCP host refused this request without a reason")


@asynccontextmanager
async def open_stdio_session(
    server_name: str,
    *,
    socket_path: Path | str | None = None,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> AsyncGenerator[MCPHostSession, None]:
    """Open one stdio MCP server in the host, and yield the session.

    The agent names the server. The host reads the command from its own config, so this call
    starts no program in the agent's process.
    """
    path = Path(socket_path) if socket_path is not None else default_socket_path()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=connect_timeout_s
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise MCPHostUnavailableError(
            f"Could not reach the MCP host at {path}: {exc}. Stdio MCP servers stay unavailable "
            "until that process runs."
        ) from exc

    session = MCPHostSession(server_name, reader, writer, path)
    try:
        await session.open()
    except BaseException:
        await session.aclose()
        raise
    try:
        yield session
    finally:
        await session.aclose()


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_SOCKET_NAME",
    "SOCKET_ENV_VAR",
    "MCPHostError",
    "MCPHostSession",
    "MCPHostUnavailableError",
    "default_socket_path",
    "open_stdio_session",
]
