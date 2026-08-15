# tests/agent/test_execution_context.py
"""Item 3 (#5): ``execution_context`` on :class:`RequestContext`.

Policy in #8 and #13 must know whether a person waits on the turn. Before this item a
subagent built its context from the origin channel. It looked exactly like the interactive
session above it. The field now states the truth at every construction site. An omission
reads as unattended, so a forgotten site costs a refusal.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from nanoinfra.agent.loop import AgentLoop, TurnContext, TurnKind
from nanoinfra.agent.runner import AgentRunResult, AgentRunSpec
from nanoinfra.agent.subagent import SubagentManager, SubagentStatus
from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE, record_observation
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_INTERACTIVE,
    EXECUTION_CONTEXT_SUBAGENT,
    EXECUTION_CONTEXTS,
    UNATTENDED_EXECUTION_CONTEXTS,
    RequestContext,
    current_request_context,
    current_request_execution_context,
    is_unattended_execution_context,
    request_context,
)
from nanoinfra.bus.events import InboundMessage
from nanoinfra.bus.queue import MessageBus
from nanoinfra.cron.session_turns import CRON_TRIGGER_META
from nanoinfra.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanoinfra.session.goal_state import GOAL_STATE_KEY
from nanoinfra.triggers.local_session_turns import LOCAL_TRIGGER_META
from nanoinfra.utils.llm_runtime import LLMRuntime

# ---------------------------------------------------------------------------
# The value vocabulary and the fail-closed default
# ---------------------------------------------------------------------------


def test_the_permitted_values_are_exactly_the_three_named_ones() -> None:
    assert EXECUTION_CONTEXTS == {
        EXECUTION_CONTEXT_INTERACTIVE,
        EXECUTION_CONTEXT_AUTOMATION,
        EXECUTION_CONTEXT_SUBAGENT,
    }


def test_an_omitted_execution_context_reads_unattended() -> None:
    """A construction site added later can forget the field. It must cost a refusal."""
    ctx = RequestContext(channel="slack", chat_id="C123")

    assert ctx.execution_context in UNATTENDED_EXECUTION_CONTEXTS
    assert ctx.is_unattended


def test_only_interactive_reads_attended() -> None:
    assert not is_unattended_execution_context(EXECUTION_CONTEXT_INTERACTIVE)
    assert is_unattended_execution_context(EXECUTION_CONTEXT_AUTOMATION)
    assert is_unattended_execution_context(EXECUTION_CONTEXT_SUBAGENT)


def test_an_unknown_value_reads_unattended() -> None:
    """A typo or a value from a future item must not buy attended trust."""
    assert is_unattended_execution_context("Interactive")
    assert is_unattended_execution_context("evaluation")
    assert is_unattended_execution_context(None)


def test_automation_and_subagent_stay_distinct_in_the_record() -> None:
    """Policy treats both as unattended. An operator still needs to see which one ran."""
    assert EXECUTION_CONTEXT_AUTOMATION != EXECUTION_CONTEXT_SUBAGENT
    assert UNATTENDED_EXECUTION_CONTEXTS == {
        EXECUTION_CONTEXT_AUTOMATION,
        EXECUTION_CONTEXT_SUBAGENT,
    }


def test_no_bound_context_reads_unattended() -> None:
    assert current_request_context() is None
    assert is_unattended_execution_context(current_request_execution_context())


def test_a_bound_context_reports_its_own_value() -> None:
    with request_context(
        RequestContext(
            channel="websocket",
            chat_id="c",
            execution_context=EXECUTION_CONTEXT_INTERACTIVE,
        )
    ):
        assert current_request_execution_context() == EXECUTION_CONTEXT_INTERACTIVE


# ---------------------------------------------------------------------------
# Channel-driven turns and automation turns
# ---------------------------------------------------------------------------


def _loop(workspace: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok"))
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
    )


def _turn_context(
    loop: AgentLoop,
    metadata: dict[str, Any] | None = None,
    *,
    session_metadata: dict[str, Any] | None = None,
) -> TurnContext:
    msg = InboundMessage(
        channel="telegram",
        sender_id="u1",
        chat_id="C1",
        content="check the disk",
        metadata=metadata or {},
    )
    key = f"{msg.channel}:{msg.chat_id}"
    session = loop.sessions.get_or_create(key)
    if session_metadata:
        session.metadata.update(session_metadata)
    return TurnContext(
        msg=msg,
        session_key=key,
        turn_id="turn-1",
        runtime=loop.llm_runtime(),
        kind=TurnKind.USER,
        delivery=loop.turn_delivery_factory.create(msg, key),
        session=session,
    )


def test_a_channel_turn_reads_interactive(tmp_path: Path) -> None:
    loop = _loop(tmp_path)

    ctx = loop._request_context_for_turn(_turn_context(loop))

    assert ctx.execution_context == EXECUTION_CONTEXT_INTERACTIVE
    assert not ctx.is_unattended


def test_a_cron_turn_reads_automation(tmp_path: Path) -> None:
    """A schedule fires this turn. Nobody is present to answer a prompt."""
    loop = _loop(tmp_path)
    metadata = {
        CRON_TRIGGER_META: {"job_id": "job-1", "job_name": "nightly", "run_id": "run-1"},
    }

    ctx = loop._request_context_for_turn(_turn_context(loop, metadata))

    assert ctx.execution_context == EXECUTION_CONTEXT_AUTOMATION
    assert ctx.is_unattended
    assert ctx.channel == "telegram"


def test_a_local_trigger_turn_reads_automation(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    metadata = {
        LOCAL_TRIGGER_META: {
            "trigger_id": "trg-1",
            "trigger_name": "webhook",
            "delivery_id": "dlv-1",
        },
    }

    ctx = loop._request_context_for_turn(_turn_context(loop, metadata))

    assert ctx.execution_context == EXECUTION_CONTEXT_AUTOMATION


def test_a_goal_command_turn_reads_automation(tmp_path: Path) -> None:
    """The /goal turn keeps working long after the operator leaves the chat."""
    loop = _loop(tmp_path)

    ctx = loop._request_context_for_turn(_turn_context(loop, {"original_command": "/goal"}))

    assert ctx.execution_context == EXECUTION_CONTEXT_AUTOMATION


def test_a_turn_under_an_active_goal_reads_automation(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    goal = {GOAL_STATE_KEY: {"status": "active", "objective": "migrate the fleet"}}

    ctx = loop._request_context_for_turn(_turn_context(loop, session_metadata=goal))

    assert ctx.execution_context == EXECUTION_CONTEXT_AUTOMATION
    assert ctx.is_unattended


# ---------------------------------------------------------------------------
# Subagent turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_subagent_reads_subagent_and_keeps_its_origin_channel(tmp_path: Path) -> None:
    """The value overrides the inherited channel instead of deriving from it."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    provider.generation = GenerationSettings()
    manager = SubagentManager(
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=16_000,
    )
    seen: list[RequestContext] = []

    async def _run(spec: AgentRunSpec) -> AgentRunResult:
        bound = current_request_context()
        assert bound is not None
        seen.append(bound)
        return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

    manager.runner.run = AsyncMock(side_effect=_run)
    manager._announce_result = AsyncMock()
    status = SubagentStatus(
        task_id="t1", label="label", task_description="task", started_at=0.0
    )

    await manager._run_subagent(
        "t1",
        "task",
        "label",
        {"channel": "telegram", "chat_id": "C1", "session_key": "telegram:C1"},
        status,
        LLMRuntime.capture(provider, "test", context_window_tokens=128_000),
    )

    assert len(seen) == 1
    assert seen[0].execution_context == EXECUTION_CONTEXT_SUBAGENT
    assert seen[0].is_unattended
    # The origin stays readable. Only the execution context changes.
    assert seen[0].channel == "telegram"
    assert seen[0].chat_id == "C1"


# ---------------------------------------------------------------------------
# The observation record (item 1, #3)
# ---------------------------------------------------------------------------


@pytest.fixture
def observations() -> Iterator[list[dict[str, Any]]]:
    """Every log-only observation emitted while the test runs, in order."""
    captured: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        record = message.record["extra"].get("gate_observation")
        if record is not None:
            captured.append(record)

    sink_id = logger.add(sink, level=0)
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def test_the_observation_record_carries_the_execution_context(
    observations: list[dict[str, Any]],
) -> None:
    with request_context(
        RequestContext(
            channel="telegram",
            chat_id="C1",
            session_key="telegram:C1",
            execution_context=EXECUTION_CONTEXT_SUBAGENT,
        )
    ):
        record_observation(
            capability_class=MUTATE_REMOTE, decision="would_gate", tool="execute_on_server"
        )

    assert len(observations) == 1
    assert observations[0]["execution_context"] == EXECUTION_CONTEXT_SUBAGENT
    assert observations[0]["session_id"] == "telegram:C1"


def test_the_observation_record_reads_unattended_without_a_bound_context(
    observations: list[dict[str, Any]],
) -> None:
    record_observation(
        capability_class=MUTATE_REMOTE, decision="preview", tool="execute_on_server"
    )

    assert is_unattended_execution_context(observations[0]["execution_context"])
