"""Message bus module for decoupled channel-agent communication."""

from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
