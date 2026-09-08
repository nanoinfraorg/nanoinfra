"""A background task that raises must name itself in the log.

``schedule_background`` carries memory consolidation and idle auto-compaction.
With a bare ``set.discard`` as the done callback the exception was never
retrieved, so the only trace was asyncio's unattributed "Task exception was
never retrieved" at GC time -- consolidation could fail on every turn and
nothing would say which coroutine failed, or that anything had.
"""

from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.bus.queue import MessageBus
from nanoinfra.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (10, "test-counter")
    response = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=response)
    provider.chat_stream_with_retry = AsyncMock(return_value=response)
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )


async def _drain(loop: AgentLoop) -> None:
    """Wait until the done callback has retired every tracked task."""
    for _ in range(200):
        if not loop._background_tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background task never retired")


@pytest.mark.asyncio
async def test_background_failure_logs_traceback_and_coroutine_name(tmp_path) -> None:
    agent = _make_loop(tmp_path)
    sink = io.StringIO()
    handler = logger.add(sink, format="{message}", level="ERROR", backtrace=True)

    async def maybe_consolidate_by_tokens() -> None:
        raise RuntimeError("consolidation-sentinel")

    try:
        agent.schedule_background(maybe_consolidate_by_tokens())
        await _drain(agent)
    finally:
        logger.remove(handler)

    out = sink.getvalue()
    # The exception, its traceback and the coroutine that raised it. Before the
    # fix all three were absent: nothing reached any sink at all.
    assert "consolidation-sentinel" in out
    assert "Traceback (most recent call last)" in out
    assert "maybe_consolidate_by_tokens" in out


@pytest.mark.asyncio
async def test_background_success_logs_nothing(tmp_path) -> None:
    agent = _make_loop(tmp_path)
    sink = io.StringIO()
    handler = logger.add(sink, format="{message}", level="ERROR")

    async def _fine() -> None:
        return None

    try:
        agent.schedule_background(_fine())
        await _drain(agent)
    finally:
        logger.remove(handler)

    assert sink.getvalue() == ""


@pytest.mark.asyncio
async def test_cancelled_background_task_is_not_reported(tmp_path) -> None:
    """Shutdown cancels the drain; a cancellation is not a failure to report."""
    agent = _make_loop(tmp_path)
    sink = io.StringIO()
    handler = logger.add(sink, format="{message}", level="ERROR")

    async def _sleeps() -> None:
        await asyncio.sleep(30)

    try:
        agent.schedule_background(_sleeps())
        task = next(iter(agent._background_tasks))
        task.cancel()
        await _drain(agent)
    finally:
        logger.remove(handler)

    assert sink.getvalue() == ""
