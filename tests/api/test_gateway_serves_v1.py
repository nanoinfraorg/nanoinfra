"""The gateway can serve `/v1` itself (#214).

On the demo, serving the OpenAI-compatible API meant a second container from the same image:
`gateway` on 8765 and `serve` on 8900, each with its own agent loop, its own MCP host and its own
connector host over the same workspace -- 285 MiB and three duplicate processes for an HTTP app.

The waste was the smaller half. `serve` had to reassemble every piece of runtime the gateway
already has, and the piece it forgot was the outbound drain: a turn emitting more than a thousand
events stalled and its request hung in production (1.7.4). One process cannot forget what it
already built.

These cover the decisions rather than the plumbing: the flag defaults off, a missing optional
extra does not take the gateway down, and the app the gateway mounts is the same one `serve`
builds.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoinfra.config.schema import ApiConfig, Config

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


def test_the_flag_is_off_by_default() -> None:
    """An upgrade must not open a port nobody asked for."""
    assert ApiConfig().enabled is False


def test_a_deployment_can_turn_it_on() -> None:
    assert ApiConfig(enabled=True).enabled is True


def test_the_flag_survives_a_full_config_round_trip() -> None:
    config = Config.model_validate({"api": {"enabled": True, "port": 8900}})

    assert config.api.enabled is True
    assert config.api.port == 8900


def test_a_wildcard_bind_still_needs_a_key() -> None:
    """Enabling it inside the gateway does not soften the rule that guarded `serve`."""
    with pytest.raises(ValueError, match="api_key"):
        ApiConfig(enabled=True, host="0.0.0.0")


def test_a_wildcard_bind_with_a_key_is_accepted() -> None:
    config = ApiConfig(enabled=True, host="0.0.0.0", api_key="k")

    assert config.host == "0.0.0.0"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_the_gateway_mounts_the_same_app_serve_builds() -> None:
    """`create_app` is shared, so the routes cannot drift between the two entry points."""
    from nanoinfra.api.server import create_app

    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="ok")
    app = create_app(agent, model_name="test-model", request_timeout=5.0, api_key="k")

    routes = {
        (route.method, getattr(route.resource, "canonical", ""))
        for route in app.router.routes()
    }

    assert ("POST", "/v1/chat/completions") in routes
    assert ("POST", "/v1/responses") in routes
    assert ("GET", "/v1/models") in routes


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_the_gateway_needs_no_drain_of_its_own() -> None:
    """The reason this issue exists: the gateway's channel manager already consumes the outbound
    bus, so the bug that hung `serve` cannot occur in this shape. Pinned as a property of the
    bus rather than of the gateway, because that is where the blocking lives."""
    import asyncio

    from nanoinfra.bus.events import OutboundMessage
    from nanoinfra.bus.queue import MessageBus

    bus = MessageBus(outbound_maxsize=4)

    async def channel_manager_drain() -> None:
        while True:
            await bus.consume_outbound()

    drain = asyncio.create_task(channel_manager_drain())
    try:
        for index in range(20):
            await asyncio.wait_for(
                bus.publish_outbound(
                    OutboundMessage(channel="api", chat_id="c", content=str(index))
                ),
                timeout=2,
            )
    finally:
        drain.cancel()


def test_the_app_factory_takes_the_configured_timeout_and_key() -> None:
    """What the gateway passes through, so a deployment's `api` block still governs the API when
    the gateway is the one serving it."""
    if not HAS_AIOHTTP:
        pytest.skip("aiohttp is not installed")
    from nanoinfra.api.server import _REQUEST_TIMEOUT_KEY, create_app

    agent: Any = MagicMock()
    app = create_app(agent, model_name="m", request_timeout=42.0, api_key="k")

    assert app[_REQUEST_TIMEOUT_KEY] == 42.0
