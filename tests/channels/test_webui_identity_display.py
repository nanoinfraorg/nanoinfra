# tests/channels/test_webui_identity_display.py
"""Item 13 of M4 (#70): the gateway tells the WebUI who it thinks you are.

A misconfigured proxy is invisible until an approval fails, and an approval fails at the worst
moment. So the ``ready`` frame carries the actor the handshake resolved, and the WebUI reads it
beside the connection state.

**The value is a read, and never an assertion of the browser.** Every test below drives a real
socket, because that is the only way to prove that a client which sends the header from an
address outside ``trustedPeerCidrs``, or which writes the actor into its own query string, gains
nothing. If the browser could set this value, the display would lie exactly when it matters.

The frame carries the string ``gates.approvers`` compares, and the last test proves that by
answering an approval with it. An operator can therefore read the WebUI, copy the value into
config, and predict the match.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.approvals import check_approval
from nanoinfra.webui.gateway_services import build_gateway_services

_PORT = 29907
_HEADER = "Cf-Access-Authenticated-User-Email"
_OPERATOR = "alberto@example.com"


def _channel(tmp_path: Path, **over: Any) -> WebSocketChannel:
    settings: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": _PORT,
        "path": "/ws",
        "websocketRequiresToken": False,
    }
    settings.update(over)
    config = WebSocketConfig.model_validate(settings)
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
        logger=MagicMock(),
    )
    return WebSocketChannel(config, bus, gateway=gateway)


def _plain_proxy(*, cidrs: list[str] | None = None) -> dict[str, Any]:
    """A proxy that asserts a bare identity, which is the Cloudflare Access shape today."""
    return {
        "trustedPeerCidrs": cidrs if cidrs is not None else ["127.0.0.1/32", "::1/128"],
        "assertionHeader": _HEADER,
        "assertionFormat": "plain",
    }


async def _ready_frame(channel: WebSocketChannel, *, query: str = "", **connect: Any) -> Any:
    """Start the gateway, read the first frame one client receives, and stop again."""
    task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        url = f"ws://127.0.0.1:{_PORT}/ws?client_id=tester{query}"
        async with websockets.connect(url, **connect) as client:
            return json.loads(await client.recv())
    finally:
        await channel.stop()
        await task


@pytest.mark.asyncio
async def test_a_deployment_with_no_proxy_reads_as_the_path(tmp_path: Path) -> None:
    """The common install. ``webui`` is the true actor there, and it is not a fault."""
    frame = await _ready_frame(_channel(tmp_path))

    assert frame["event"] == "ready"
    assert frame["operator_actor"] == "webui"


@pytest.mark.asyncio
async def test_a_resolved_identity_reaches_the_frame(tmp_path: Path) -> None:
    frame = await _ready_frame(
        _channel(tmp_path, trustedProxyAuth=_plain_proxy()),
        additional_headers={_HEADER: _OPERATOR},
    )

    assert frame["operator_actor"] == f"webui:{_OPERATOR}"


@pytest.mark.asyncio
async def test_a_header_from_an_untrusted_peer_names_nobody(tmp_path: Path) -> None:
    """The peer check decides. A client that sets the header itself gains no name."""
    frame = await _ready_frame(
        _channel(tmp_path, trustedProxyAuth=_plain_proxy(cidrs=["10.0.0.0/8"])),
        additional_headers={_HEADER: "root@example.com"},
    )

    assert frame["operator_actor"] == "webui"


@pytest.mark.asyncio
async def test_the_client_cannot_name_itself_in_the_query(tmp_path: Path) -> None:
    """A display the browser could set would lie exactly when an operator needs it."""
    frame = await _ready_frame(
        _channel(tmp_path),
        query="&operator_actor=webui%3Aroot%40example.com",
    )

    assert frame["operator_actor"] == "webui"


@pytest.mark.asyncio
async def test_the_frame_carries_the_string_the_gate_compares(tmp_path: Path) -> None:
    """The whole point of the display: read it, write it in config, predict the match.

    ``gates.approvers`` compares the whole string and strips no prefix (#66), so a value that
    read differently in the WebUI would be a value that approves nothing.
    """
    frame = await _ready_frame(
        _channel(tmp_path, trustedProxyAuth=_plain_proxy()),
        additional_headers={_HEADER: _OPERATOR},
    )
    displayed = frame["operator_actor"]
    gates = GatesConfig.model_validate(
        {
            "approvers": [{"channel": "webui", "sender": displayed}],
            "approvalPaths": ["webui", "telegram"],
        }
    )

    check = check_approval(
        gates=gates,
        origin_path="telegram",
        origin_actor="",
        approval_path="webui",
        sender=displayed,
    )

    assert check.ok, check.reason
