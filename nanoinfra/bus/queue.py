"""Async message queue for decoupled channel-agent communication."""

import asyncio
from typing import Any

from loguru import logger

from nanoinfra.bus.events import InboundMessage, OutboundMessage

# Both queues are bounded, because an unbounded one is a memory-exhaustion path a channel peer
# controls: flood faster than the agent consumes and the process grows until it dies
# (upstream HKUDS/nanobot#4780). The caps are generous rather than tight -- they exist to stop
# unbounded growth, not to throttle normal traffic. A backlog this deep already means the agent is
# far behind, which is worth a log line either way.
DEFAULT_INBOUND_MAXSIZE = 1000
DEFAULT_OUTBOUND_MAXSIZE = 1000

# How deep a queue gets before it says so, once per crossing rather than per message.
_BACKLOG_WARN_RATIO = 0.8


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.

    **Why the two publish paths differ.** A channel's receive loop is a separate task from the
    agent, so blocking it on a full queue is real backpressure: the platform buffers or the poller
    slows, and no message is lost. The agent is different -- it publishes inbound messages itself
    (a subagent result, a generated image) while *being* the only inbound consumer, so a blocking
    put there would stop the agent inside its own turn and it could never drain the queue it is
    waiting on. Those callers use ``publish_inbound_nowait``, which refuses loudly instead of
    deadlocking. Dropping an internal notification is bad; hanging the agent is worse.
    """

    def __init__(
        self,
        inbound_maxsize: int = DEFAULT_INBOUND_MAXSIZE,
        outbound_maxsize: int = DEFAULT_OUTBOUND_MAXSIZE,
    ):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbound_maxsize)
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=outbound_maxsize)
        self._warned_inbound = False
        self._warned_outbound = False

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent, waiting if the queue is full.

        Only for callers that are not the agent's own consumer task. An agent-side caller must use
        :meth:`publish_inbound_nowait`.
        """
        self._note_backlog(self.inbound, "inbound")
        await self.inbound.put(msg)

    def publish_inbound_nowait(self, msg: InboundMessage) -> bool:
        """Publish without waiting; return whether it was accepted.

        For agent-side callers. Waiting here would deadlock the process that has to drain the
        queue, so a full queue is reported and the message is dropped rather than blocking.
        """
        try:
            self.inbound.put_nowait(msg)
        except asyncio.QueueFull:
            logger.error(
                "Inbound queue is full ({} messages); dropped an agent-side message for {}. "
                "The agent is not keeping up with its own notifications.",
                self.inbound.maxsize,
                msg.channel,
            )
            return False
        return True

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels.

        Blocking is safe here: the consumer is the channel manager, a different task, so a full
        outbound queue slows the agent rather than deadlocking it.
        """
        self._note_backlog(self.outbound, "outbound")
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    def _note_backlog(self, queue: "asyncio.Queue[Any]", label: str) -> None:
        """Say once when a queue is nearly full, and once when it recovers.

        A queue this deep means the consumer is far behind. Silence until the cap is reached would
        turn a gradual problem into a sudden stall.
        """
        warned = self._warned_inbound if label == "inbound" else self._warned_outbound
        deep = queue.qsize() >= queue.maxsize * _BACKLOG_WARN_RATIO
        if deep and not warned:
            logger.warning(
                "{} queue backlog is {}/{}; the consumer is falling behind",
                label,
                queue.qsize(),
                queue.maxsize,
            )
        elif not deep and warned:
            logger.info("{} queue backlog recovered ({} pending)", label, queue.qsize())
        if label == "inbound":
            self._warned_inbound = deep
        else:
            self._warned_outbound = deep

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
