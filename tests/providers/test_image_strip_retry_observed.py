"""The image-strip recovery call is a call, so it gets a row (#176).

`_run_with_retry` retries once without image content when a non-transient error arrives carrying
images. That retry is frequently the call that produces the answer the user reads, and it was the
one attempt the observer never saw: two provider calls, one row, and the tokens the successful call
burned charged to nobody. Upstream observes inside `_safe_chat`, so their tree records it; moving
observation out to the retry loop (#172/#173) left this branch behind.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from nanoinfra.llm_usage.models import LLMCallRecord
from nanoinfra.providers.base import LLMProvider, LLMResponse, LLMUsage


def _image_messages() -> list[dict[str, Any]]:
    """A fresh transcript per test: the recovery path strips images *in-place*, by design."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this screenshot?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]


class _ImageRejectingProvider(LLMProvider):
    """Rejects any request that still carries an image, then answers the stripped one.

    The shape a vision-less endpoint really produces: HTTP 400, `error_should_retry=False`, so the
    retry loop classifies it as non-transient and takes the image-strip branch rather than the
    backoff one.
    """

    def __init__(self, *, deltas: list[str] | None = None) -> None:
        super().__init__()
        self.calls: list[list[dict[str, Any]]] = []
        self._deltas = deltas or []

    def get_default_model(self) -> str:
        return "fake/vision-less"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        messages = kwargs["messages"]
        self.calls.append(messages)
        if self._contains_image_content(messages):
            return LLMResponse(
                content="Error: this model does not support image input",
                finish_reason="error",
                error_status_code=400,
                error_should_retry=False,
            )
        return LLMResponse(
            content="a terminal window",
            usage=LLMUsage.reported(input_tokens=5_400, output_tokens=12),
        )

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        on_content_delta = kwargs.get("on_content_delta")
        response = await self.chat(**kwargs)
        if response.finish_reason == "error" or on_content_delta is None:
            return response
        delta: Callable[[str], Awaitable[None]] = on_content_delta
        for chunk in self._deltas or [response.content or ""]:
            # A real gap between chunks, so `_StreamTiming` has something to measure and a
            # zero would mean "not measured" rather than "measured as instant".
            await asyncio.sleep(0.02)
            await delta(chunk)
        return response


def _collector() -> tuple[list[LLMCallRecord], Callable[[LLMCallRecord], None]]:
    rows: list[LLMCallRecord] = []
    return rows, rows.append


@pytest.mark.asyncio
async def test_the_stripped_retry_is_billed() -> None:
    provider = _ImageRejectingProvider()
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    response = await provider.chat_with_retry(
        messages=_image_messages(),
        model="fake/vision-less",
    )

    assert response.content == "a terminal window"
    assert len(provider.calls) == 2, "the strip retry must actually have happened"
    assert [row.finish_reason for row in rows] == ["error", "stop"], (
        "two provider calls, two rows -- the recovery call was the invisible one"
    )
    assert rows[0].usage is None, "an error is not a call that cost nothing"
    assert rows[1].usage is not None
    assert rows[1].usage.total_tokens == 5_412


@pytest.mark.asyncio
async def test_the_stripped_retry_keeps_its_own_timing() -> None:
    """Rebased on the recovery call, not on the attempt that failed before it."""
    provider = _ImageRejectingProvider(deltas=["a terminal ", "window"])
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    async def _delta(_text: str) -> None:
        return None

    response = await provider.chat_stream_with_retry(
        messages=_image_messages(),
        model="fake/vision-less",
        on_content_delta=_delta,
    )

    assert response.content == "a terminal window"
    assert len(rows) == 2
    retry_row = rows[1]
    assert retry_row.stream is True
    assert retry_row.usage is not None
    assert retry_row.usage.ttft_ms > 0, "time to first token was measured on this call"
    assert retry_row.usage.generation_ms > 0
    assert retry_row.usage.timed_requests == 1
    assert retry_row.usage.measured_output_tokens == 12


@pytest.mark.asyncio
async def test_the_input_buckets_stay_disjoint_after_recovery() -> None:
    """One row per attempt, never a sum: the retry's cost may not be folded into the failure's."""
    provider = _ImageRejectingProvider()
    rows, observer = _collector()
    provider.set_llm_call_observer(observer)

    await provider.chat_with_retry(
        messages=_image_messages(),
        model="fake/vision-less",
    )

    usage = rows[1].usage
    assert usage is not None
    assert usage.reported_tokens + usage.estimated_tokens == usage.total_tokens
    assert usage.total_tokens == usage.input_tokens + usage.output_tokens
    assert (usage.cache_read_tokens or 0) + (usage.cache_write_tokens or 0) <= usage.input_tokens
    assert usage.request_count == 1, "the recovery call is one request, counted once"
