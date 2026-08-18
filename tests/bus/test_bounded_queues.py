"""The bus queues are bounded, and bounding them must not deadlock the agent.

An unbounded queue is a memory-exhaustion path a channel peer controls (upstream
HKUDS/nanobot#4780). The trap is that the agent publishes inbound messages itself while being the
only inbound consumer, so a blocking put on a full queue stops the very task that would drain it.
Channels block (real backpressure); agent-side callers refuse loudly instead.
See nanoinfraorg/nanoinfra#147.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.bus.queue import (
    DEFAULT_INBOUND_MAXSIZE,
    DEFAULT_OUTBOUND_MAXSIZE,
    MessageBus,
)


def _inbound(text: str = "hi") -> InboundMessage:
    return InboundMessage(channel="websocket", chat_id="c1", sender_id="u1", content=text)


def _outbound(text: str = "hi") -> OutboundMessage:
    return OutboundMessage(channel="websocket", chat_id="c1", content=text)


@pytest.fixture
def warnings() -> "list[str]":
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(sink)


def test_the_queues_are_bounded_by_default() -> None:
    bus = MessageBus()

    assert bus.inbound.maxsize == DEFAULT_INBOUND_MAXSIZE
    assert bus.outbound.maxsize == DEFAULT_OUTBOUND_MAXSIZE


async def test_a_channel_publish_applies_backpressure() -> None:
    """Blocking a channel's receive loop is correct: nothing is lost and the peer slows."""
    bus = MessageBus(inbound_maxsize=1)
    await bus.publish_inbound(_inbound("first"))

    blocked = asyncio.create_task(bus.publish_inbound(_inbound("second")))
    await asyncio.sleep(0.05)
    assert not blocked.done(), "a full inbound queue must make a channel wait"

    assert (await bus.consume_inbound()).content == "first"
    await asyncio.wait_for(blocked, timeout=1.0)
    assert (await bus.consume_inbound()).content == "second"


async def test_an_agent_side_publish_never_blocks(warnings: list[str]) -> None:
    """The deadlock this design exists to avoid."""
    bus = MessageBus(inbound_maxsize=1)
    await bus.publish_inbound(_inbound("first"))

    accepted = bus.publish_inbound_nowait(_inbound("second"))

    assert accepted is False
    assert bus.inbound_size == 1


async def test_an_agent_side_publish_succeeds_when_there_is_room() -> None:
    bus = MessageBus(inbound_maxsize=4)

    assert bus.publish_inbound_nowait(_inbound("only")) is True
    assert (await bus.consume_inbound()).content == "only"


async def test_a_refused_agent_side_publish_is_logged_as_an_error() -> None:
    """Dropping a notification must never be silent."""
    errors: list[str] = []
    sink = logger.add(errors.append, level="ERROR", format="{message}")
    try:
        bus = MessageBus(inbound_maxsize=1)
        await bus.publish_inbound(_inbound("first"))
        bus.publish_inbound_nowait(_inbound("second"))
    finally:
        logger.remove(sink)

    assert any("Inbound queue is full" in line for line in errors)


async def test_outbound_blocks_rather_than_dropping_a_reply() -> None:
    """The consumer is the channel manager, a different task, so blocking is safe here."""
    bus = MessageBus(outbound_maxsize=1)
    await bus.publish_outbound(_outbound("first"))

    blocked = asyncio.create_task(bus.publish_outbound(_outbound("second")))
    await asyncio.sleep(0.05)
    assert not blocked.done()

    assert (await bus.consume_outbound()).content == "first"
    await asyncio.wait_for(blocked, timeout=1.0)


async def test_a_deep_backlog_warns_once(warnings: list[str]) -> None:
    bus = MessageBus(inbound_maxsize=10)
    for i in range(9):
        await bus.publish_inbound(_inbound(str(i)))

    backlog_warnings = [line for line in warnings if "backlog" in line]
    assert len(backlog_warnings) == 1, "one warning per crossing, not one per message"


async def test_the_backlog_warning_rearms_after_recovery(warnings: list[str]) -> None:
    bus = MessageBus(inbound_maxsize=10)
    for i in range(9):
        await bus.publish_inbound(_inbound(str(i)))
    for _ in range(9):
        await bus.consume_inbound()
    for i in range(9):
        await bus.publish_inbound(_inbound(str(i)))

    assert len([line for line in warnings if "backlog" in line]) == 2


async def test_ordinary_traffic_neither_blocks_nor_warns(warnings: list[str]) -> None:
    bus = MessageBus()
    for i in range(50):
        await bus.publish_inbound(_inbound(str(i)))
        await bus.publish_outbound(_outbound(str(i)))

    assert warnings == []
    assert bus.inbound_size == 50
    assert bus.outbound_size == 50
