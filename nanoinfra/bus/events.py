"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanoinfra.bus.outbound_events import OutboundEvent

# Optional ``OutboundMessage.metadata`` key for structured, channel-agnostic UI
# payloads. Value is JSON-serializable with at least ``kind``; rich clients may
# render it and other channels may ignore unknown keys.
OUTBOUND_META_AGENT_UI = "_agent_ui"

# Internal-only inbound metadata used by in-process channels to ask the agent
# loop to update runtime state without going through a user session.
INBOUND_META_RUNTIME_CONTROL = "_runtime_control"
# Trusted namespace grant for read-only persisted-session tools.
INBOUND_META_SESSION_READ_SCOPE = "_session_read_scope"
RUNTIME_CONTROL_ACK = "_ack"
RUNTIME_CONTROL_MCP_RELOAD = "mcp_reload"
# Re-register the data connectors' tools against what config says now (#194). The same
# shape as the MCP reload, because it answers the same question: the registry was built
# at boot and config has changed since.
RUNTIME_CONTROL_CONNECTOR_RELOAD = "connector_reload"
RUNTIME_CONTROL_IMAGE_GENERATION_RELOAD = "image_generation_reload"


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    #: A routing label, and never proof of who somebody is. It keys a session, it matches a
    #: channel ``allowFrom`` list, and it reaches the pairing store. Some channels authenticate
    #: it and some do not: the WebSocket channel reads it from a query parameter the browser
    #: chooses. Read ``authenticated_sender`` for an authorization decision
    #: (nanoinfraorg/nanoinfra#81).
    sender_id: str
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions
    #: Who the channel itself authenticated, when the channel authenticated anybody (#81).
    #:
    #: ``None`` means this channel proves no identity, and it must never read as a blank name. A
    #: channel sets this only from a value it verified: the WebSocket channel sets the actor its
    #: handshake resolved, where the peer address and the assertion signature were both checked.
    #:
    #: This field exists because ``sender_id`` could not carry both meanings. It keys sessions
    #: for every deployment, so narrowing it to authenticated identities would change session
    #: identity on upgrade for every WebUI user.
    authenticated_sender: str | None = None

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel.

    ``event`` carries internal runtime/UI semantics. ``metadata`` is reserved
    for channel routing context (``message_id``, thread ids, etc.) and optional
    ``OUTBOUND_META_AGENT_UI`` blobs for rich clients.
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    buttons: list[list[str]] = field(default_factory=list)
    event: "OutboundEvent | None" = None
