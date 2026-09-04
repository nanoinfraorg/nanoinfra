"""Runtime event bus for agent state notifications.

This bus is separate from :mod:`nanoinfra.bus.queue`: message bus events are
user/chat delivery, while runtime events are in-process state notifications
that optional subscribers such as WebUI adapters may render.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.bus.events import InboundMessage

if TYPE_CHECKING:
    from nanoinfra.providers.base import LLMUsage
    from nanoinfra.utils.llm_runtime import LLMRuntime


@dataclass(frozen=True)
class RuntimeEventContext:
    """Routing context common to turn-scoped runtime events."""

    channel: str
    chat_id: str
    session_key: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionTurnStarted:
    """A user/system turn has loaded its session and is about to build context."""

    context: RuntimeEventContext


@dataclass(frozen=True)
class TurnRunStatusChanged:
    """Visible run status changed for a turn."""

    context: RuntimeEventContext
    status: str
    started_at: float | None = None


@dataclass(frozen=True)
class TurnCompleted:
    """A turn has delivered its final user-visible response."""

    context: RuntimeEventContext
    latency_ms: int | None = None
    runtime: LLMRuntime | None = None
    #: What the turn cost, so the surface that shows the turn can show it (#202).
    usage: LLMUsage | None = None
    #: What the turn's prompt was made of, by section (#203). Names and sizes, never content.
    prompt_manifest: dict[str, Any] | None = None
    #: Which named agent answered (#248). ``None`` is the deployment's default agent, which is
    #: every turn today. Recorded rather than inferred: switching agents mid-thread is allowed, so
    #: a reader who has to guess from the model or the tools would sometimes guess wrong, and that
    #: history cannot be reconstructed afterwards.
    agent: str | None = None


@dataclass(frozen=True)
class SessionTurnPersisted:
    """A completed turn has been written to local session storage."""

    context: RuntimeEventContext
    turn_id: str
    sender_id: str


@dataclass(frozen=True)
class GoalStateChanged:
    """A session's sustained-goal state changed."""

    context: RuntimeEventContext
    session_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeModelChanged:
    """The active runtime model/preset changed."""

    model: str
    model_preset: str | None


RuntimeEvent = (
    SessionTurnStarted
    | SessionTurnPersisted
    | TurnRunStatusChanged
    | TurnCompleted
    | GoalStateChanged
    | RuntimeModelChanged
)
RuntimeEventType = (
    type[SessionTurnStarted]
    | type[SessionTurnPersisted]
    | type[TurnRunStatusChanged]
    | type[TurnCompleted]
    | type[GoalStateChanged]
    | type[RuntimeModelChanged]
)
RuntimeEventHandler = Callable[[Any], Awaitable[None] | None]
_HandlerEntry = tuple[RuntimeEventType | None, RuntimeEventHandler]


class RuntimeEventBus:
    """Small in-process pub/sub bus for runtime state.

    Subscribers run in registration order. ``publish`` awaits async handlers so
    callers can preserve ordering when a runtime event must follow a user
    message. ``publish_nowait`` is available for synchronous call sites.
    """

    def __init__(self) -> None:
        self._handlers: list[_HandlerEntry] = []

    def subscribe(
        self,
        handler: RuntimeEventHandler,
        event_type: RuntimeEventType | None = None,
    ) -> Callable[[], None]:
        entry = (event_type, handler)
        self._handlers.append(entry)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(entry)

        return _unsubscribe

    async def publish(self, event: RuntimeEvent) -> None:
        for event_type, handler in list(self._handlers):
            if event_type is not None and not isinstance(event, event_type):
                continue
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("runtime event handler failed for {}", type(event).__name__)

    def publish_nowait(self, event: RuntimeEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("dropping runtime event without a running loop: {}", type(event).__name__)
            return
        loop.create_task(self.publish(event))


def _manifest_scope(
    manifest: dict[str, Any] | None, usage: "LLMUsage | None"
) -> dict[str, Any] | None:
    """Say which request the manifest describes, when the turn made more than one (#208).

    The manifest is built in `_build_initial_messages` because that is the only place the section
    attribution exists -- once a request reaches a provider the system prompt is one string in a
    flat list. So it describes the *first* request and always will. On a real turn it read 23,725
    while the same event carried `context_tokens: 48,874` over 23 requests, and a reader had two
    true numbers presented as one.

    Added rather than corrected: the sections and the total stay exactly as measured, because
    recomputing them from the turn's accumulated messages would over-report -- that list is the
    conversation, not any single request.
    """
    if not manifest or usage is None:
        return manifest
    requests = usage.request_count
    if requests <= 1:
        # One request and one manifest agree. Saying "1 request" on every turn is noise the panel
        # would then have to decide not to show.
        return manifest
    scoped = dict(manifest)
    scoped["requests"] = requests
    peak = usage.context_tokens
    total = manifest.get("total_tokens")
    if isinstance(peak, int) and isinstance(total, int) and peak > total:
        # Only when it exceeds our estimate. The manifest is this tokenizer's count and the peak is
        # the provider's; a smaller peak means the two disagree, not that the turn shrank, and
        # printing it would invite subtracting two numbers that were never comparable.
        scoped["peak_context_tokens"] = peak
    return scoped


class RuntimeEventPublisher:
    """Convenience publisher for turn-scoped runtime events.

    Agent code should decide when state transitions happen; this helper owns
    the mechanics of building event contexts and carrying per-turn metadata.
    """

    def __init__(self, bus: RuntimeEventBus | None = None) -> None:
        self.bus = bus or RuntimeEventBus()
        self._turn_latency_ms: dict[str, int] = {}
        self._turn_runtime: dict[str, LLMRuntime] = {}
        # Keyed by session, like the latency: two sessions can be mid-turn at once, and a
        # loop-global "last usage" would hand one session's number to the other's thread.
        self._turn_usage: dict[str, LLMUsage] = {}
        self._turn_prompt: dict[str, dict[str, Any]] = {}
        #: Which named agent is answering, per session (#248). Per session for the same reason the
        #: others are: two chats run concurrently, and one loop-global value would attribute one
        #: session's turn to the other session's agent.
        self._turn_agent: dict[str, str] = {}

    @staticmethod
    def _context(
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        metadata: dict[str, Any] | None,
        attributes: dict[str, Any] | None = None,
    ) -> RuntimeEventContext:
        return RuntimeEventContext(
            channel=channel,
            chat_id=chat_id,
            session_key=session_key,
            metadata=dict(metadata or {}),
            attributes=dict(attributes or {}),
        )

    def record_turn_runtime(self, session_key: str, runtime: LLMRuntime) -> None:
        self._turn_runtime[session_key] = runtime

    def record_turn_latency(self, session_key: str, latency_ms: int | None) -> None:
        if latency_ms is not None:
            self._turn_latency_ms[session_key] = int(latency_ms)

    def record_turn_usage(self, session_key: str, usage: LLMUsage | None) -> None:
        if usage is not None:
            self._turn_usage[session_key] = usage

    def record_turn_prompt(self, session_key: str, manifest: dict[str, Any] | None) -> None:
        if manifest:
            self._turn_prompt[session_key] = manifest

    def record_turn_agent(self, session_key: str, agent: str | None) -> None:
        """Record who is answering. Only a resolved name reaches here.

        The default agent records nothing, which is what leaves the field off the frame rather
        than putting a name on it that config never declared.
        """
        if agent:
            self._turn_agent[session_key] = agent

    def clear_turn(self, session_key: str) -> None:
        self._turn_latency_ms.pop(session_key, None)
        self._turn_runtime.pop(session_key, None)
        self._turn_usage.pop(session_key, None)
        self._turn_prompt.pop(session_key, None)
        self._turn_agent.pop(session_key, None)

    async def session_turn_started(
        self,
        msg: InboundMessage,
        session_key: str,
    ) -> None:
        await self.bus.publish(
            SessionTurnStarted(
                context=self._context(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                )
            )
        )

    async def run_status_changed(
        self,
        msg: InboundMessage,
        session_key: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        await self.bus.publish(
            TurnRunStatusChanged(
                context=self._context(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                ),
                status=status,
                started_at=started_at,
            )
        )

    async def session_turn_persisted(
        self,
        msg: InboundMessage,
        session_key: str,
        *,
        turn_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        await self.bus.publish(
            SessionTurnPersisted(
                context=self._context(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                    attributes=attributes,
                ),
                turn_id=turn_id,
                sender_id=msg.sender_id,
            )
        )

    async def turn_completed(
        self,
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        # Popped before the event is built, because the manifest needs it: a manifest describes
        # one request and a turn can make twenty-three (#208).
        usage = self._turn_usage.pop(session_key, None)
        await self.bus.publish(
            TurnCompleted(
                context=self._context(
                    channel=channel,
                    chat_id=chat_id,
                    session_key=session_key,
                    metadata=metadata,
                ),
                latency_ms=self._turn_latency_ms.pop(session_key, None),
                runtime=self._turn_runtime.pop(session_key, None),
                usage=usage,
                prompt_manifest=_manifest_scope(
                    self._turn_prompt.pop(session_key, None), usage
                ),
                agent=self._turn_agent.pop(session_key, None),
            )
        )

    def runtime_model_changed(self, model: str, model_preset: str | None) -> None:
        self.bus.publish_nowait(
            RuntimeModelChanged(model=model, model_preset=model_preset)
        )
