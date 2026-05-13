"""Tests for ChannelManager routing of model reasoning content.

Reasoning is delivered as a separate plugin action (``send_reasoning``)
rather than a metadata flag on a regular outbound. The manager routes
``_reasoning`` messages only to channels that opt in via
``channel.show_reasoning``; channels without a low-emphasis UI primitive
keep the base no-op and the content silently drops at dispatch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config


class _MockChannel(BaseChannel):
    name = "mock"
    display_name = "Mock"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self._send_mock = AsyncMock()
        self._send_reasoning_mock = AsyncMock()

    async def start(self):  # pragma: no cover - not exercised
        pass

    async def stop(self):  # pragma: no cover - not exercised
        pass

    async def send(self, msg):
        return await self._send_mock(msg)

    async def send_reasoning(self, msg):
        return await self._send_reasoning_mock(msg)


@pytest.fixture
def manager() -> ChannelManager:
    mgr = ChannelManager(Config(), MessageBus())
    mgr.channels["mock"] = _MockChannel({}, mgr.bus)
    return mgr


@pytest.mark.asyncio
async def test_reasoning_routes_to_send_reasoning_not_send(manager):
    channel = manager.channels["mock"]
    msg = OutboundMessage(
        channel="mock",
        chat_id="c1",
        content="step-by-step thinking",
        metadata={"_progress": True, "_reasoning": True},
    )
    await manager._send_once(channel, msg)
    channel._send_reasoning_mock.assert_awaited_once_with(msg)
    channel._send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_drops_reasoning_when_channel_opts_out(manager):
    channel = manager.channels["mock"]
    channel.show_reasoning = False
    msg = OutboundMessage(
        channel="mock",
        chat_id="c1",
        content="hidden thinking",
        metadata={"_progress": True, "_reasoning": True},
    )
    await manager.bus.publish_outbound(msg)

    pumped = await _pump_one(manager)

    assert pumped is True
    channel._send_reasoning_mock.assert_not_awaited()
    channel._send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_delivers_reasoning_when_channel_opts_in(manager):
    channel = manager.channels["mock"]
    channel.show_reasoning = True
    msg = OutboundMessage(
        channel="mock",
        chat_id="c1",
        content="visible thinking",
        metadata={"_progress": True, "_reasoning": True},
    )
    await manager.bus.publish_outbound(msg)

    pumped = await _pump_one(manager)

    assert pumped is True
    channel._send_reasoning_mock.assert_awaited_once()
    delivered = channel._send_reasoning_mock.await_args.args[0]
    assert delivered.content == "visible thinking"


@pytest.mark.asyncio
async def test_dispatch_silently_drops_reasoning_for_unknown_channel(manager):
    msg = OutboundMessage(
        channel="ghost",
        chat_id="c1",
        content="nobody home",
        metadata={"_progress": True, "_reasoning": True},
    )
    await manager.bus.publish_outbound(msg)

    pumped = await _pump_one(manager)

    assert pumped is True
    # Mock channel must not receive anything destined for a different channel.
    manager.channels["mock"]._send_reasoning_mock.assert_not_awaited()
    manager.channels["mock"]._send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_channel_send_reasoning_is_noop_safe():
    """Plugins that don't override `send_reasoning` must not blow up."""

    class _Plain(BaseChannel):
        name = "plain"
        display_name = "Plain"

        async def start(self):  # pragma: no cover
            pass

        async def stop(self):  # pragma: no cover
            pass

        async def send(self, msg):  # pragma: no cover
            pass

    channel = _Plain({}, MessageBus())
    # No exception, returns None.
    assert await channel.send_reasoning(
        OutboundMessage(channel="plain", chat_id="c", content="x", metadata={})
    ) is None


@pytest.mark.asyncio
async def test_reasoning_routing_does_not_consult_send_progress(manager):
    """`show_reasoning` is orthogonal to `send_progress` — turning off
    progress streaming must not silence reasoning."""
    channel = manager.channels["mock"]
    channel.send_progress = False
    channel.show_reasoning = True
    msg = OutboundMessage(
        channel="mock",
        chat_id="c1",
        content="still surfaces",
        metadata={"_progress": True, "_reasoning": True},
    )
    await manager.bus.publish_outbound(msg)

    pumped = await _pump_one(manager)

    assert pumped is True
    channel._send_reasoning_mock.assert_awaited_once()


async def _pump_one(manager: ChannelManager) -> bool:
    """Drive the dispatcher for exactly one message, then cancel."""
    import asyncio

    task = asyncio.create_task(manager._dispatch_outbound())
    # Yield control until the queue drains.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if manager.bus.outbound.qsize() == 0:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return True
