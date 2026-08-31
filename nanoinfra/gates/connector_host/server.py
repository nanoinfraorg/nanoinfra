"""Serve one connector request per frame, holding nothing between them (#195, part 4).

The host is deliberately close to stateless. It has no credential store, no config, and no memory
of the last request: it receives a rendered URL, a token, and a deadline, makes the call, projects
the response down to the fields the package declared, and answers. A host that kept a token between
requests would be a host whose compromise outlived the request that fed it.

Two checks happen here even though the executor already did them, and both are cheap:

- **The package is re-read from disk.** The executor sent a directory name; this side loads that
  package's own `connector.json` and confirms the operation exists and the URL belongs to it. A
  frame that named a real package and a URL from somewhere else would otherwise be a request this
  process makes with a live token.
- **The projection is the package's.** The response is reduced to the declared `returns` before it
  crosses back, so a connector that returned more than it promised cannot use this process to carry
  it into the model's context.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from nanoinfra.connectors.engine import project
from nanoinfra.connectors.package import ConnectorPackageError, load_connector_package
from nanoinfra.gates.connector_host.protocol import (
    ConnectorHostRequest,
    ConnectorHostResponse,
    ProtocolError,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)
from nanoinfra.gates.socket_group import (
    CONNECTOR_HOST_SOCKET_GROUP_ENV,
    apply_socket_group,
)

#: The socket directory mode when this process creates it. The executor is the only client, so the
#: group is the executor's -- unlike the MCP host's, whose group includes the agent.
_SOCKET_DIR_MODE = 0o2710

#: A ceiling on any one call, whatever the frame asked for. A deadline a caller controls is a
#: deadline, not a bound.
MAX_TIMEOUT_S = 60.0


class ConnectorHost:
    """Answers one connector call per frame."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    payload = await read_frame(reader)
                except ProtocolError:
                    # A closed peer, or a frame this side will not interpret. Either way there is
                    # nothing to answer to.
                    return
                try:
                    request = decode_request(payload)
                except ProtocolError as exc:
                    logger.warning("connector host refused a frame: {}", exc)
                    return
                response = await self.handle(request)
                await write_frame(writer, encode_response(response))
        finally:
            with contextlib.suppress(OSError):
                writer.close()

    async def handle(self, request: ConnectorHostRequest) -> ConnectorHostResponse:
        """Make one call, or refuse it. A refusal is a response, never an exception."""
        try:
            plugin = load_connector_package(self._root / request.package)
        except ConnectorPackageError as exc:
            return self._refuse(request, f"package {request.package!r} is not loadable: {exc}")
        operation = plugin.operation(request.operation)
        if operation is None:
            return self._refuse(
                request, f"package {request.package!r} declares no operation {request.operation!r}"
            )
        if operation.method.upper() != request.method.upper():
            return self._refuse(
                request,
                f"{request.operation!r} is declared {operation.method} and the frame said "
                f"{request.method}",
            )
        host = urlsplit(request.url).hostname or ""
        declared_host = urlsplit(plugin.base_url).hostname or ""
        if not request.url.startswith("https://") or host != declared_host:
            # The frame carries a rendered URL, and a rendered URL from somewhere else would be a
            # request this process makes with a live token.
            return self._refuse(
                request,
                f"the frame's URL host {host!r} is not this package's {declared_host!r}",
            )

        timeout = min(max(1.0, request.timeout_s), MAX_TIMEOUT_S)
        headers = dict(request.headers)
        if request.access_token:
            headers["Authorization"] = f"Bearer {request.access_token}"
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                api = await client.request(
                    request.method.upper(),
                    request.url,
                    params=request.query or None,
                    json=request.body if request.body is not None else None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            return ConnectorHostResponse(
                request_id=request.request_id,
                ok=False,
                status=None,
                payload=None,
                error=f"the request failed: {type(exc).__name__}",
                retryable=True,
            )

        if api.status_code >= 400:
            # The provider's error body is not forwarded. It is the field most likely to quote the
            # request back, and a projection built from it would read as data.
            return ConnectorHostResponse(
                request_id=request.request_id,
                ok=False,
                status=api.status_code,
                payload=None,
                error=f"the API answered {api.status_code}",
                retryable=api.status_code == 429 or api.status_code >= 500,
            )
        try:
            body: Any = api.json()
        except ValueError:
            return self._refuse(request, "the API answered with something that is not JSON")
        return ConnectorHostResponse(
            request_id=request.request_id,
            ok=True,
            status=api.status_code,
            payload=project(body, operation),
            error=None,
        )

    @staticmethod
    def _refuse(request: ConnectorHostRequest, reason: str) -> ConnectorHostResponse:
        logger.warning("connector host refused {}: {}", request.operation, reason)
        return ConnectorHostResponse(
            request_id=request.request_id,
            ok=False,
            status=None,
            payload=None,
            error=reason,
        )


def serve_forever(socket_path: Path | str, *, workspace: Path | str) -> None:
    """Bind the socket and serve until terminated. One entry-point shape with the other helpers."""
    asyncio.run(_serve(Path(socket_path), workspace=Path(workspace)))


async def _serve(path: Path, *, workspace: Path) -> None:
    from nanoinfra.connectors.registry import workspace_connector_root

    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, _SOCKET_DIR_MODE)
    if path.exists():
        path.unlink()

    host = ConnectorHost(workspace_connector_root(workspace))
    server = await asyncio.start_unix_server(host.serve_connection, path=str(path), backlog=8)
    apply_socket_group(path, env_var=CONNECTOR_HOST_SOCKET_GROUP_ENV)
    logger.info("gates: connector host listening on {} (workspace {})", path, workspace)
    try:
        async with server:
            await server.serve_forever()
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


__all__ = ["MAX_TIMEOUT_S", "ConnectorHost", "serve_forever"]
