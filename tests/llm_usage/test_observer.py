"""One row per attempt, from the provider that made it (#176).

The reason the observer sits on `LLMProvider._run_with_retry` rather than on an `AgentHook`: three
call sites in this tree reach a provider without going through the agent loop -- the WebUI title
generation, the evaluator, and the Dream consolidation -- so a hook counted their tokens as nothing.
These tests pin the placement, not just the plumbing.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.llm_usage.context import (
    llm_usage_source,
    source_from_request,
    source_from_session_key,
)
from nanoinfra.llm_usage.models import LLMCallRecord
from nanoinfra.providers.base import LLMProvider, LLMResponse, LLMUsage


class _FakeProvider(LLMProvider):
    """A provider whose `chat` is scripted, so the base class's retry loop really runs."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return self._responses.pop(0)

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        return await self.chat(**kwargs)


def _collector() -> tuple[list[LLMCallRecord], Any]:
    rows: list[LLMCallRecord] = []
    return rows, rows.append


async def _ask(provider: LLMProvider, **kwargs: Any) -> LLMResponse:
    return await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="fake/model", **kwargs
    )


# --- placement ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_row_per_attempt_including_the_failed_one() -> None:
    """A retried call is two rows. This is the question a daily aggregate cannot answer."""
    provider = _FakeProvider([
        LLMResponse(content=None, finish_reason="error", error_kind="timeout"),
        LLMResponse(content="ok", usage=LLMUsage.reported(input_tokens=10, output_tokens=2)),
    ])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)
    provider._CHAT_RETRY_DELAYS = (0,)  # pyright: ignore[reportAttributeAccessIssue]

    await _ask(provider)

    assert [row.finish_reason for row in rows] == ["error", "stop"]
    assert rows[0].usage is None, "an error is not a call that cost nothing"
    assert rows[1].usage is not None
    assert rows[1].usage.total_tokens == 12


@pytest.mark.asyncio
async def test_a_call_that_reaches_no_agent_loop_is_still_recorded() -> None:
    """The Dream consolidation, the evaluator and the WebUI title call all look like this."""
    provider = _FakeProvider([
        LLMResponse(content="summary", usage=LLMUsage.reported(input_tokens=500, output_tokens=20))
    ])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    await _ask(provider)

    assert len(rows) == 1
    assert rows[0].usage is not None
    assert rows[0].usage.total_tokens == 520


@pytest.mark.asyncio
async def test_no_observer_means_no_recording_and_no_error() -> None:
    """An embedding of this package writes to no store it never asked for."""
    provider = _FakeProvider([LLMResponse(content="ok")])

    response = await _ask(provider)

    assert response.content == "ok"


@pytest.mark.asyncio
async def test_an_observer_that_raises_does_not_break_the_turn() -> None:
    """Telemetry that can fail a request is worse than telemetry with a missing row."""
    provider = _FakeProvider([LLMResponse(content="ok")])

    def _explode(record: LLMCallRecord) -> None:
        raise RuntimeError("store is on fire")

    provider.set_llm_call_observer(_explode)

    response = await _ask(provider)

    assert response.content == "ok"


# --- what the row says -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_row_names_the_provider_and_the_model() -> None:
    provider = _FakeProvider([LLMResponse(content="ok")])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    await _ask(provider)

    assert rows[0].provider == "fake"
    assert rows[0].model == "fake/model"


@pytest.mark.asyncio
async def test_the_row_carries_the_bound_source() -> None:
    provider = _FakeProvider([LLMResponse(content="ok")])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    with llm_usage_source("cron"):
        await _ask(provider)

    assert rows[0].source == "cron"


@pytest.mark.asyncio
async def test_an_unattributed_call_is_system_rather_than_user() -> None:
    """Over-counting `user` would flatter the figure that matters most."""
    provider = _FakeProvider([LLMResponse(content="ok")])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    await _ask(provider)

    assert rows[0].source == "system"


# --- the estimate at the boundary --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_that_reports_nothing_gets_an_estimate() -> None:
    """Otherwise the store would be precise about the calls it can see and silent about the ones
    it had to guess -- which is the opposite of the partition."""
    provider = _FakeProvider([LLMResponse(content="a fairly long answer here", usage=None)])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    response = await _ask(provider)

    assert response.usage is not None
    assert response.usage.source == "estimated"
    assert rows[0].usage is not None
    assert rows[0].usage.estimated_tokens == rows[0].usage.total_tokens


@pytest.mark.asyncio
async def test_an_error_is_left_unmeasured() -> None:
    provider = _FakeProvider([
        LLMResponse(content="Error calling LLM: boom", finish_reason="error", error_kind="timeout")
    ])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)
    provider._CHAT_RETRY_DELAYS = ()  # pyright: ignore[reportAttributeAccessIssue]

    await _ask(provider)

    assert rows[-1].usage is None


# --- classification ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("session_key", "expected"),
    [
        ("dream:abc", "dream"),
        ("heartbeat", "cron"),
        ("cron:job-1", "cron"),
        ("api:key-7", "api"),
        ("system:model-switch", "system"),
        ("websocket:chat-1", "user"),
        (None, "user"),
    ],
)
def test_a_session_key_is_classified_and_not_kept(session_key: str | None, expected: str) -> None:
    assert source_from_session_key(session_key) == expected


def test_a_cron_trigger_is_a_cron_turn_whatever_its_session_key_says() -> None:
    assert (
        source_from_request(
            "websocket:chat-1", channel="websocket", metadata={"_cron_trigger": {"id": "j"}}
        )
        == "cron"
    )


def test_the_api_channel_is_an_api_turn() -> None:
    assert source_from_request("chat-1", channel="api", metadata=None) == "api"
