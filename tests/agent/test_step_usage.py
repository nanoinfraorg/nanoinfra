"""A step carries the cost of the call behind it (#208).

Read on a real turn: all 23 `stream_end` records held exactly `stream_id`, `turn_id`, `turn_phase`,
`turn_seq`, `resuming` and `created_at_ms`. No tokens, no duration of their own -- while the same
process had every one of them per call in `llm-usage.sqlite3`. So a step could only repeat the
turn's single `latency_ms` and single usage total, which is how eight consecutive clusters came to
read `7m 57s` and why the panel described the cheapest moment of the turn as the whole cost.

The chain each of these covers one link of: the runner measures the call, the hook offers it, the
event carries it, the manager passes it to a channel that asks, and the frame projects it.

**And one that covers the chain itself.** Every link above passed while the chain delivered
nothing: 51 `stream_end` records across 13 real sessions carried no usage at all, because
`AgentLoop` wraps the delivery callback in a `_tracked_stream_end` that declared only `resuming`
and `merge_next` -- and the hook offers a fact only to a callback that names it. A wrapper is
invisible to a per-link test by construction, so the last test here drives a real turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoinfra.agent.hook import AgentHookContext
from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.progress_hook import AgentProgressHook
from nanoinfra.bus.events import InboundMessage
from nanoinfra.bus.outbound_events import StreamEndEvent
from nanoinfra.bus.queue import MessageBus
from nanoinfra.channels.manager import ChannelManager
from nanoinfra.providers.base import LLMResponse, LLMUsage
from nanoinfra.session.webui_turns import WebuiTurnCoordinator, WebuiTurnRoutePolicy


def _usage() -> LLMUsage:
    return LLMUsage.reported(input_tokens=21_000, output_tokens=1_500, cache_read_tokens=20_160)


# --- the hook offers it ---------------------------------------------------------------------


async def test_the_hook_passes_the_call_usage_to_a_callback_that_takes_it() -> None:
    seen: dict[str, Any] = {}

    async def on_stream_end(*, resuming: bool, usage: LLMUsage | None = None,
                            request_ms: int | None = None) -> None:
        seen.update({"resuming": resuming, "usage": usage, "request_ms": request_ms})

    hook = AgentProgressHook(on_stream_end=on_stream_end)
    context = AgentHookContext(iteration=1, messages=[], usage=_usage())
    context.request_ms = 47_300

    await hook.on_stream_end(context, resuming=True)

    assert seen["usage"] == _usage()
    assert seen["request_ms"] == 47_300


async def test_a_callback_that_does_not_take_it_is_called_as_before() -> None:
    """`on_stream_end` is a callback other channels supply; a new argument cannot break them."""
    calls: list[bool] = []

    async def on_stream_end(*, resuming: bool) -> None:
        calls.append(resuming)

    hook = AgentProgressHook(on_stream_end=on_stream_end)
    context = AgentHookContext(iteration=1, messages=[], usage=_usage())
    context.request_ms = 100

    await hook.on_stream_end(context, resuming=False)

    assert calls == [False]


async def test_a_call_that_reported_nothing_offers_nothing() -> None:
    """An error response has no usage, and a zero would read as a measured zero."""
    seen: dict[str, Any] = {}

    async def on_stream_end(**kwargs: Any) -> None:
        seen.update(kwargs)

    hook = AgentProgressHook(on_stream_end=on_stream_end)

    await hook.on_stream_end(AgentHookContext(iteration=1, messages=[]), resuming=False)

    assert "usage" not in seen
    assert "request_ms" not in seen


# --- the event carries it -------------------------------------------------------------------


def test_the_event_holds_the_value_rather_than_a_projection() -> None:
    event = StreamEndEvent(stream_id="s", usage=_usage(), request_ms=47_300)

    assert event.usage is not None
    assert event.usage.input_tokens == 21_000
    assert event.request_ms == 47_300


def test_a_stream_end_without_usage_is_unchanged() -> None:
    event = StreamEndEvent(stream_id="s")

    assert event.usage is None
    assert event.request_ms is None


# --- the manager offers it to a channel that asks -------------------------------------------


class _Asking:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
        merge_next: bool = False,
        step_usage: LLMUsage | None = None,
        step_ms: int | None = None,
    ) -> None:
        self.kwargs = {"step_usage": step_usage, "step_ms": step_ms, "stream_end": stream_end}


class _Plain:
    def __init__(self) -> None:
        self.called = False

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        self.called = True


def _message(event: StreamEndEvent) -> Any:
    from nanoinfra.bus.outbound_events import outbound_message_for_event

    return outbound_message_for_event(channel="websocket", chat_id="c", event=event)


async def test_a_channel_that_declares_the_parameters_receives_them() -> None:
    channel = _Asking()
    event = StreamEndEvent(stream_id="s", usage=_usage(), request_ms=47_300)

    await ChannelManager._send_stream_event(channel, _message(event), event)  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert channel.kwargs["step_usage"] == _usage()
    assert channel.kwargs["step_ms"] == 47_300


async def test_a_channel_that_does_not_still_receives_its_stream_end() -> None:
    """Telegram, Discord and Matrix have no surface for this and must not start raising."""
    channel = _Plain()
    event = StreamEndEvent(stream_id="s", usage=_usage(), request_ms=47_300)

    await ChannelManager._send_stream_event(channel, _message(event), event)  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert channel.called is True


def test_the_accepted_kwargs_helper_reads_a_var_keyword_signature() -> None:
    async def anything(chat_id: str, delta: str, metadata: Any = None, **kwargs: Any) -> None:
        return None

    accepted = ChannelManager._accepted_kwargs(anything, {"step_ms": 1, "merge_next": True})  # pyright: ignore[reportPrivateUsage]

    assert accepted == {"step_ms": 1, "merge_next": True}


def test_the_accepted_kwargs_helper_drops_what_is_not_declared() -> None:
    async def narrow(chat_id: str, delta: str, *, merge_next: bool = False) -> None:
        return None

    accepted = ChannelManager._accepted_kwargs(narrow, {"step_ms": 1, "merge_next": True})  # pyright: ignore[reportPrivateUsage]

    assert accepted == {"merge_next": True}


# --- the projection -------------------------------------------------------------------------


def test_the_step_projection_omits_a_cache_metric_the_provider_never_reported() -> None:
    """3 of the 23 calls reported no `cached_tokens`, between neighbours at 99% and 93%. A zero
    there would render a cold cache that never happened."""
    silent = LLMUsage.reported(input_tokens=21_000, output_tokens=1_500)

    assert "cached_tokens" not in silent.to_turn_dict()
    assert _usage().to_turn_dict()["cached_tokens"] == 20_160


def test_the_step_projection_is_the_same_shape_the_turn_uses() -> None:
    """One reader parses both, which is why the step reuses `to_turn_dict` rather than inventing
    a second spelling of the same numbers."""
    projected = _usage().to_turn_dict()

    assert projected["prompt_tokens"] == 21_000
    assert projected["completion_tokens"] == 1_500
    assert projected["request_count"] == 1


# --- the whole chain ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_turn_puts_the_call_cost_on_its_stream_end(tmp_path: Path) -> None:
    """The end-to-end assertion the per-link tests could not make.

    `AgentLoop` does not hand the runner's hook the delivery callback directly -- it wraps it, to
    track whether the segment streamed any content. The hook offers `usage` only to a callback
    whose signature names it, so a wrapper that omits the parameter silently drops the fact while
    every link on either side keeps passing.
    """
    bus = MessageBus()
    provider = MagicMock()
    provider.supports_progress_deltas = True
    provider.get_default_model.return_value = "openai-codex/gpt-5.5"

    async def chat_stream_with_retry(*, on_content_delta: Any, **_kwargs: Any) -> LLMResponse:
        await on_content_delta("Hello")
        return LLMResponse(content="Hello", tool_calls=[], usage=_usage())

    provider.chat_stream_with_retry = chat_stream_with_retry
    provider.chat_with_retry = AsyncMock()

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="openai-codex/gpt-5.5",
    )
    loop.turn_delivery_factory.route_policy = WebuiTurnRoutePolicy(loop.sessions)
    WebuiTurnCoordinator(
        bus=bus,
        sessions=loop.sessions,
        schedule_background=lambda coro: loop.schedule_background(coro),
    ).subscribe(loop.runtime_events)
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    await loop._dispatch(  # pyright: ignore[reportPrivateUsage]
        InboundMessage(
            channel="websocket",
            sender_id="u1",
            chat_id="chat-step-chain",
            content="say hello",
            metadata={"_wants_stream": True},
        )
    )

    endings: list[StreamEndEvent] = []
    while bus.outbound_size > 0:
        message = await bus.consume_outbound()
        if isinstance(message.event, StreamEndEvent):
            endings.append(message.event)

    assert endings, "the turn published no stream_end at all"
    carried = [event.usage for event in endings if event.usage is not None]
    assert carried, "no stream_end carried the cost of the call behind it"
    assert carried[0].input_tokens == 21_000
    assert carried[0].cache_read_tokens == 20_160
