"""A prompt manifest says which request it describes (#208).

Read on a real turn: the manifest said `23,725` while the same `turn_end` carried
`context_tokens: 48,874`, `request_count: 23` and `prompt_tokens: 1,117,265`. Both numbers were
right about different things, and the panel presented one of them as the turn.

The manifest is built once, in `_build_initial_messages`, because that is the only place the section
attribution exists — by the time a request reaches a provider the system prompt is one string in a
flat list. So it describes the **first** request and always will. What it lacked is the two facts
that make that legible: how many requests the turn made, and how large the largest one got.

Merged where both are popped for the same turn, so a manifest can never be paired with another
session's usage.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from nanoinfra.bus.runtime_events import RuntimeEventPublisher, TurnCompleted
from nanoinfra.providers.base import LLMUsage


class _Bus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


def _manifest() -> dict[str, Any]:
    return {
        "sections": [{"name": "builtin", "chars": 40_000, "tokens": 10_273, "group": "tools"}],
        "groups": {"tools": 10_273},
        "total_tokens": 23_725,
        "measured": False,
    }


def _usage(*, requests: int, context: int | None) -> LLMUsage:
    base = LLMUsage.reported(input_tokens=1_117_265, output_tokens=11_300)
    return dataclasses.replace(base, request_count=requests, context_tokens=context)


async def _completed(
    *,
    manifest: dict[str, Any] | None,
    usage: LLMUsage | None,
    usage_session: str = "s",
) -> TurnCompleted:
    bus = _Bus()
    publisher = RuntimeEventPublisher(bus=bus)  # pyright: ignore[reportArgumentType]
    if manifest is not None:
        publisher.record_turn_prompt("s", manifest)
    if usage is not None:
        publisher.record_turn_usage(usage_session, usage)
    await publisher.turn_completed(
        channel="websocket",
        chat_id="c",
        session_key="s",
        metadata=None,
    )
    return bus.published[-1]


async def test_a_multi_call_turn_carries_the_request_count() -> None:
    event = await _completed(manifest=_manifest(), usage=_usage(requests=23, context=48_874))

    assert event.prompt_manifest is not None
    assert event.prompt_manifest["requests"] == 23


async def test_it_carries_the_largest_request_rather_than_only_the_first() -> None:
    """`context_tokens` is a level, not a sum: the largest request the turn made."""
    event = await _completed(manifest=_manifest(), usage=_usage(requests=23, context=48_874))

    assert event.prompt_manifest is not None
    assert event.prompt_manifest["peak_context_tokens"] == 48_874


async def test_a_single_call_turn_gets_no_disambiguation() -> None:
    """One request and one manifest agree. Adding "1 request" to every turn would be noise on the
    common case, and the panel would have to decide not to show it."""
    event = await _completed(manifest=_manifest(), usage=_usage(requests=1, context=23_725))

    assert event.prompt_manifest is not None
    assert "requests" not in event.prompt_manifest
    assert "peak_context_tokens" not in event.prompt_manifest


async def test_the_sections_and_total_are_left_alone() -> None:
    """The attribution is the manifest's whole reason to exist and is not recomputed here."""
    event = await _completed(manifest=_manifest(), usage=_usage(requests=23, context=48_874))

    assert event.prompt_manifest is not None
    assert event.prompt_manifest["total_tokens"] == 23_725
    assert event.prompt_manifest["sections"] == _manifest()["sections"]


async def test_a_turn_with_no_usage_still_publishes_its_manifest() -> None:
    event = await _completed(manifest=_manifest(), usage=None)

    assert event.prompt_manifest is not None
    assert event.prompt_manifest["total_tokens"] == 23_725
    assert "requests" not in event.prompt_manifest


async def test_a_turn_with_no_manifest_is_unaffected() -> None:
    event = await _completed(manifest=None, usage=_usage(requests=23, context=48_874))

    assert event.prompt_manifest is None


async def test_one_session_never_reads_another_session_usage() -> None:
    """Both recorders key by session because two turns run at once; crossing them would attach one
    turn's request count to another turn's manifest."""
    event = await _completed(
        manifest=_manifest(), usage=_usage(requests=23, context=48_874), usage_session="other"
    )

    assert event.prompt_manifest is not None
    assert "requests" not in event.prompt_manifest


async def test_a_peak_smaller_than_the_first_request_is_not_reported() -> None:
    """The manifest is an estimate from our tokenizer and the peak is the provider's count. A peak
    below the estimate means the two disagree, not that the turn shrank, and printing it as a peak
    would invite a reader to subtract two numbers that were never comparable."""
    event = await _completed(manifest=_manifest(), usage=_usage(requests=23, context=9_000))

    assert event.prompt_manifest is not None
    assert event.prompt_manifest["requests"] == 23
    assert "peak_context_tokens" not in event.prompt_manifest
