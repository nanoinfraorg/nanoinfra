"""The partition, through a real turn (#174).

`proposals/llm-usage-contract.md` argues that `token_calibration.py` is the strongest reason for
the typed partition, because it already depended on knowing which numbers a provider reported and
read that from a convention. These tests are that argument as assertions: a turn's tokens carry
their origin, a multi-call turn keeps both halves, and the correction factor is never taught its
own output.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanoinfra.agent.runner import AgentRunner
from nanoinfra.config.schema import AgentDefaults
from nanoinfra.providers.base import LLMProvider, LLMResponse, LLMUsage, ToolCallRequest
from nanoinfra.utils import token_calibration

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


@pytest.fixture(autouse=True)
def _clean_calibration() -> Any:
    token_calibration.reset()
    yield
    token_calibration.reset()


async def _run(*responses: LLMResponse, max_iterations: int = 4) -> Any:
    """Run one turn against a provider that answers with exactly these responses."""
    queue = list(responses)
    provider = MagicMock(spec=LLMProvider)

    async def chat_with_retry(*, messages: Any, **kwargs: Any) -> LLMResponse:
        return queue.pop(0)

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    runner = AgentRunner()
    return await runner.run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            model="test-model",
            max_iterations=max_iterations,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        )
    )


# --- the origin travels -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reported_turn_is_entirely_reported() -> None:
    result = await _run(
        LLMResponse(
            content="done",
            finish_reason="stop",
            usage=LLMUsage.reported(input_tokens=100, output_tokens=20),
        )
    )

    assert result.usage["total_tokens"] == 120
    assert result.usage["estimated_tokens"] == 0
    assert result.usage["provider_tokens"] == 120


@pytest.mark.asyncio
async def test_a_turn_the_provider_did_not_measure_is_entirely_estimated() -> None:
    """The number is still useful, and it is no longer indistinguishable from a measurement."""
    result = await _run(LLMResponse(content="done", finish_reason="stop", usage=None))

    assert result.usage["total_tokens"] > 0
    assert result.usage["estimated_tokens"] == result.usage["total_tokens"]
    assert "provider_tokens" not in result.usage


@pytest.mark.asyncio
async def test_an_error_response_costs_nothing_rather_than_an_estimate() -> None:
    """An error is not a call that cost nothing, so it is not guessed at either."""
    result = await _run(
        LLMResponse(content="Error calling LLM: boom", finish_reason="error", usage=None)
    )

    assert result.usage == {}


# --- aggregation across a turn ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_loop_keeps_both_halves_of_the_partition() -> None:
    """Several calls make one turn, and providers do not answer consistently across them: the
    first call here reported its tokens and the second did not."""
    result = await _run(
        LLMResponse(
            content="thinking",
            tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
            usage=LLMUsage.reported(input_tokens=100, output_tokens=20),
        ),
        LLMResponse(content="done", finish_reason="stop", usage=None),
    )

    usage = result.usage
    assert usage["provider_tokens"] == 120
    assert usage["estimated_tokens"] > 0
    # The invariant that had to survive the accumulation: the two halves exhaust the total.
    assert usage["provider_tokens"] + usage["estimated_tokens"] == usage["total_tokens"]
    assert usage["request_count"] == 2


@pytest.mark.asyncio
async def test_a_cache_count_on_one_call_and_not_the_other_reports_nothing() -> None:
    """Summing dicts key by key added the one number that existed, which is a cache-hit rate
    over a denominator including calls with no measurement."""
    result = await _run(
        LLMResponse(
            content="thinking",
            tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
            usage=LLMUsage.reported(input_tokens=100, output_tokens=20, cache_read_tokens=90),
        ),
        LLMResponse(
            content="done",
            finish_reason="stop",
            usage=LLMUsage.reported(input_tokens=50, output_tokens=5),
        ),
    )

    assert "cached_tokens" not in result.usage


@pytest.mark.asyncio
async def test_the_context_level_is_the_last_call_and_not_the_sum() -> None:
    """Two calls carrying 40k each did not carry 80k, and the compaction decision reads this."""
    result = await _run(
        LLMResponse(
            content="thinking",
            tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
            usage=LLMUsage.reported(input_tokens=40_000, output_tokens=20),
        ),
        LLMResponse(
            content="done",
            finish_reason="stop",
            usage=LLMUsage.reported(input_tokens=41_000, output_tokens=20),
        ),
    )

    assert result.usage["context_tokens"] == 41_000


# --- what the calibration module is allowed to learn from -------------------------------


@pytest.mark.asyncio
async def test_a_reported_prompt_size_teaches_the_correction_factor() -> None:
    await _run(
        LLMResponse(
            content="done",
            finish_reason="stop",
            usage=LLMUsage.reported(input_tokens=4_000, output_tokens=20),
        )
    )

    # The provider type is a mock, so the exact key is not the point: that a factor was learned
    # at all is, because a reported prompt size is the one moment the truth is available.
    assert any(factor > 1.0 for factor in _factors().values())


@pytest.mark.asyncio
async def test_an_estimated_call_teaches_it_nothing() -> None:
    """Otherwise the factor is fitted to its own output and drifts on every turn a provider
    happens not to report -- which the old dict could not prevent, because an estimate and a
    measurement were the same three keys."""
    await _run(LLMResponse(content="done", finish_reason="stop", usage=None))

    assert not _factors()


def _factors() -> dict[str, float]:
    return dict(token_calibration._factors)  # pyright: ignore[reportPrivateUsage]
