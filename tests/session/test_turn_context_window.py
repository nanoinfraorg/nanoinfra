"""The context window a turn ran under travels with the turn, not with the current preset.

`usage.context_tokens` says how much context the turn carried. On its own that is a token count:
to read it as *how close to full*, a surface needs the window. Reading the window from the active
preset would be wrong the moment a thread switches presets -- every earlier turn would then be
restated against a denominator it never ran against, silently and with no way to notice.

So the window is taken from the turn's own `LLMRuntime` and sent beside the used figure.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from nanoinfra.bus.outbound_events import TurnEndEvent
from nanoinfra.bus.runtime_events import RuntimeEventContext, TurnCompleted
from nanoinfra.providers.base import GenerationSettings, LLMUsage
from nanoinfra.session.webui_turns import WebuiTurnCoordinator
from nanoinfra.utils.llm_runtime import LLMRuntime


class _Bus:
    def __init__(self) -> None:
        self.outbound: list[Any] = []

    async def publish_outbound(self, message: Any) -> None:
        self.outbound.append(message)


class _Session:
    metadata: dict[str, Any] = {}


class _Sessions:
    def get_or_create(self, _session_key: str) -> _Session:
        return _Session()


def _coordinator() -> tuple[WebuiTurnCoordinator, _Bus]:
    bus = _Bus()
    coordinator = WebuiTurnCoordinator(
        bus=bus,  # pyright: ignore[reportArgumentType]
        sessions=_Sessions(),  # pyright: ignore[reportArgumentType]
        schedule_background=lambda awaitable: awaitable.close(),
    )
    return coordinator, bus


def _runtime(context_window_tokens: int) -> LLMRuntime:
    return LLMRuntime(
        provider=object(),  # pyright: ignore[reportArgumentType]
        model="claude-opus-5",
        generation=GenerationSettings(),
        context_window_tokens=context_window_tokens,
    )


def _completed(runtime: LLMRuntime | None) -> TurnCompleted:
    usage = dataclasses.replace(
        LLMUsage.reported(input_tokens=87_300, output_tokens=900),
        context_tokens=87_300,
    )
    return TurnCompleted(
        context=RuntimeEventContext(
            channel="websocket",
            chat_id="chat-window",
            session_key="websocket:chat-window",
            metadata={},
        ),
        latency_ms=1_200,
        runtime=runtime,
        usage=usage,
    )


def _turn_end(bus: _Bus) -> TurnEndEvent:
    events = [
        message.event
        for message in bus.outbound
        if isinstance(getattr(message, "event", None), TurnEndEvent)
    ]
    assert len(events) == 1
    return events[0]


@pytest.mark.asyncio
async def test_turn_end_carries_the_window_of_the_runtime_that_answered() -> None:
    coordinator, bus = _coordinator()

    await coordinator._handle_turn_completed_event(_completed(_runtime(200_000)))

    event = _turn_end(bus)
    assert event.context_window_tokens == 200_000
    assert event.usage is not None
    assert event.usage.context_tokens == 87_300


@pytest.mark.asyncio
async def test_turn_end_reports_no_window_when_the_turn_recorded_no_runtime() -> None:
    """A cancelled or directly-driven turn may complete with no runtime recorded. `None` then, so
    the client shows a token count rather than a fraction against a window nobody captured."""
    coordinator, bus = _coordinator()

    await coordinator._handle_turn_completed_event(_completed(None))

    assert _turn_end(bus).context_window_tokens is None
