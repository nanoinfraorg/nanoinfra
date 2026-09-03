"""The API server drains the outbound bus, or a long turn hangs (#211 follow-up).

Found on the demo, not in a test: the Codex CLI's first real turn never answered. Not the gate --
today's gate log held two allowed reads and nothing else -- and not the ports, which are 8765 and
8900 in separate namespaces. `bus.outbound` is bounded at 1000 and `publish_outbound` awaits a full
queue, which its docstring calls safe "because the consumer is the channel manager, a different
task". `gateway` drains through `ChannelManager` and `nanoinfra agent` runs its own consumer.
`serve` did neither, so a turn that emitted more than a thousand progress, stream or trace events
stopped mid-flight and its HTTP request hung until the timeout.

Short answers never reached the bound. That is why every hand-written check passed and the first
real client hung.
"""

from __future__ import annotations

import asyncio

import pytest

from nanoinfra.api.server import drain_outbound
from nanoinfra.bus.events import OutboundMessage
from nanoinfra.bus.queue import MessageBus


def _message(index: int) -> OutboundMessage:
    return OutboundMessage(channel="api", chat_id="default", content=f"event {index}")


async def test_a_publisher_past_the_bound_is_not_blocked() -> None:
    """The regression itself. Without the drain this test hangs at the fifth publish."""
    bus = MessageBus(outbound_maxsize=4)
    task = asyncio.create_task(drain_outbound(bus))
    try:
        for index in range(20):
            await asyncio.wait_for(bus.publish_outbound(_message(index)), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_the_queue_does_not_grow_without_bound() -> None:
    bus = MessageBus(outbound_maxsize=4)
    task = asyncio.create_task(drain_outbound(bus))
    try:
        for index in range(50):
            await bus.publish_outbound(_message(index))
        for _ in range(50):
            if bus.outbound.qsize() == 0:
                break
            await asyncio.sleep(0.01)

        assert bus.outbound.qsize() == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_without_the_drain_the_publisher_blocks() -> None:
    """States the failure this exists to prevent, so a later reader does not delete the drain as
    dead code: the fifth publish onto a four-deep queue never returns."""
    bus = MessageBus(outbound_maxsize=4)
    for index in range(4):
        await bus.publish_outbound(_message(index))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.publish_outbound(_message(99)), timeout=0.2)


async def test_cancelling_the_drain_reraises_so_shutdown_can_await_it() -> None:
    bus = MessageBus(outbound_maxsize=4)
    task = asyncio.create_task(drain_outbound(bus))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
