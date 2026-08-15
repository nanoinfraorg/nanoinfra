"""Every write route must answer over a real socket (found in production, not by a test).

The WebUI could not clear a denial latch in any deployment, and the approvals inbox could not
answer a suspended action. Both routes were a POST, and the HTTP layer of this channel serves GET
alone: a POST reaches no route, and the server closes the connection with no response. An operator
therefore read "The gateway did not answer".

87 tests covered those two features and every one of them passed, because each one called
`dispatch()` in this process. `InProcessHttpChannel` does the same for the route suite, so no test
in this repository crossed the transport. That is the gap this file closes.

The channel here is a real `WebSocketChannel` on a real port, and every request travels over TCP.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.webui.gateway_services import build_gateway_services

_PORT = 29971


def _channel(tmp_path: Path, port: int) -> WebSocketChannel:
    """A real channel, so the request crosses a socket rather than a function call."""
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    cfg = WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": port,
            "path": "/",
        }
    )
    services = build_gateway_services(
        config=cfg,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path / "workspace",
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(cfg, bus, gateway=services)


async def _request(
    method: str, url: str, headers: dict[str, str], timeout_s: float = 5.0
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
        return await client.request(method, url, headers=headers)


@pytest.mark.asyncio
async def test_the_write_routes_answer_over_a_real_socket(tmp_path: Path) -> None:
    """The property that no in-process test can hold: the transport delivers the request.

    A route the transport cannot reach answers nothing, and an operator cannot tell that from a
    refusal. So each write route must return a real status over TCP. 503 is a real answer here,
    because the gateway attaches no gate surface in this test.
    """
    channel = _channel(tmp_path, _PORT)
    token = channel.gateway.tokens.issue_api_token(300)
    auth = {"Authorization": f"Bearer {token}"}
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.4)

    try:
        clear = await _request(
            "GET",
            f"http://127.0.0.1:{_PORT}/api/webui/gates/latches/clear",
            {
                **auth,
                "X-Nanoinfra-Latch-Values": json.dumps(
                    {"sessionId": "s1", "capabilityClass": "mutate.remote"}
                ),
            },
        )
        answer = await _request(
            "GET",
            f"http://127.0.0.1:{_PORT}/api/webui/gates/approvals/answer",
            {
                **auth,
                "X-Nanoinfra-Approval-Values": json.dumps(
                    {"requestId": "r1", "decision": "deny", "reason": "probe"}
                ),
            },
        )
    finally:
        await channel.stop()
        server.cancel()
        with suppress(asyncio.CancelledError):
            await server

    assert clear.status_code != 405, "the clear must not refuse the one method the transport serves"
    assert clear.status_code in (200, 503), clear.text
    assert answer.status_code != 405, "the answer must not refuse the one method the transport serves"
    assert answer.status_code in (200, 503), answer.text


@pytest.mark.asyncio
async def test_a_post_reaches_no_route_on_this_transport(tmp_path: Path) -> None:
    """The fact the two features were built against, and it is not true.

    This test states the constraint rather than a wish. A POST here fails at the transport, so a
    route that needs a POST is unreachable. The repository therefore sends every write as a GET and
    carries the body in a values header.
    """
    channel = _channel(tmp_path, _PORT + 1)
    token = channel.gateway.tokens.issue_api_token(300)
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.4)

    try:
        with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError)):
            await _request(
                "POST",
                f"http://127.0.0.1:{_PORT + 1}/api/webui/gates/latches/clear",
                {
                    "Authorization": f"Bearer {token}",
                    "X-Nanoinfra-Latch-Values": json.dumps(
                        {"sessionId": "s1", "capabilityClass": "mutate.remote"}
                    ),
                },
            )
    finally:
        await channel.stop()
        server.cancel()
        with suppress(asyncio.CancelledError):
            await server


@pytest.mark.asyncio
async def test_a_read_route_answers_over_a_real_socket(tmp_path: Path) -> None:
    """A control, so a failure above means the write route and never the harness."""
    channel = _channel(tmp_path, _PORT + 2)
    token = channel.gateway.tokens.issue_api_token(300)
    server = asyncio.create_task(channel.start())
    await asyncio.sleep(0.4)

    try:
        resp = await _request(
            "GET",
            f"http://127.0.0.1:{_PORT + 2}/api/webui/gates/latches",
            {"Authorization": f"Bearer {token}"},
        )
    finally:
        await channel.stop()
        server.cancel()
        with suppress(asyncio.CancelledError):
            await server

    assert resp.status_code in (200, 503), resp.text
