"""Item 12 (#15): the retry hint must never reach a terminal denial.

``AgentRunner._run_tool`` builds ``hint`` ("Analyze the error above and try a different
approach.") and appends it to tool failures. That sentence is the retry this item exists to
stop: the model changes the command and asks the gate again, so the gate becomes an oracle.

``repeated_external_lookup_error`` is the precedent for a refusal instead of a retryable
error, and ``_classify_violation`` is where the runner already turns a boundary rejection
into a non-retryable result. A terminal denial joins that classifier, so all three failure
paths (a prepare error, an exception, an error result) drop the hint with one check.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agent.runner_helpers import make_run_spec
from nanoinfra.agent.runner import AgentRunner
from nanoinfra.agent.tools import ToolResult
from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE, command_digest
from nanoinfra.config.schema import AgentDefaults
from nanoinfra.gates.latch import new_denial_latch
from nanoinfra.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars
_HINT = "try a different approach"


def _denial(command: str = "systemctl restart nginx") -> ToolResult:
    """Build a real denial from the latch, so this test tracks the shipped text."""
    latch, _ = new_denial_latch()
    return latch.deny(
        session_id="session-1",
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="policy denies this class outside a change window",
        actor="telegram:12345",
        action_digest=command_digest(command),
    )


def _spec(tools: MagicMock, **overrides: object):
    provider = MagicMock()
    return make_run_spec(
        provider,
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        **overrides,
    )


def _tools(*, result: object = None, error: BaseException | None = None) -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    if error is not None:
        tools.execute = AsyncMock(side_effect=error)
    else:
        tools.execute = AsyncMock(return_value=result)
    return tools


async def _run_one(
    *,
    result: object = None,
    error: BaseException | None = None,
    fail_on_tool_error: bool = False,
) -> tuple[object, dict[str, str], BaseException | None]:
    tools = _tools(result=result, error=error)
    spec = _spec(tools, fail_on_tool_error=fail_on_tool_error)
    call = ToolCallRequest(
        id="call_1",
        name="execute_on_server",
        arguments={"command": "systemctl restart nginx"},
    )
    return await AgentRunner()._run_tool(spec, call, {}, {})


async def test_denial_result_carries_no_retry_hint() -> None:
    denial = _denial()
    payload, event, fatal = await _run_one(result=denial)
    assert str(payload) == str(denial), "the runner appended something to a denial"
    assert _HINT not in str(payload)
    assert "Analyze the error above" not in str(payload)
    assert event["status"] == "error"
    assert fatal is None


async def test_denial_event_names_the_terminal_denial() -> None:
    _, event, _ = await _run_one(result=_denial())
    assert event["detail"].startswith("terminal_denial:")


async def test_denial_does_not_abort_the_turn_even_when_tool_errors_are_fatal() -> None:
    """A denial ends one action. It is not a runtime failure, so the turn still replies."""
    payload, _, fatal = await _run_one(result=_denial(), fail_on_tool_error=True)
    assert fatal is None
    assert _HINT not in str(payload)


async def test_a_denial_raised_as_an_exception_also_loses_the_hint() -> None:
    """#8 may raise instead of returning. The marker travels in the text, so both paths hold."""
    denial = _denial()
    payload, event, fatal = await _run_one(error=PermissionError(str(denial)))
    assert str(payload) == str(denial)
    assert _HINT not in str(payload)
    assert "PermissionError" not in str(payload)
    assert event["detail"].startswith("terminal_denial:")
    assert fatal is None


async def test_an_ordinary_tool_error_still_gets_the_retry_hint() -> None:
    """The suppression is narrow. A normal failure keeps the recovery hint it had before."""
    payload, event, _ = await _run_one(result=ToolResult.error("Error: connection refused"))
    assert _HINT in str(payload)
    assert event["detail"].startswith("Error: connection refused")


async def test_a_denial_beats_the_ssrf_note() -> None:
    """The denial check runs first, so denial text is never rewritten by another classifier."""
    latch, _ = new_denial_latch()
    denial = latch.deny(
        session_id="session-1",
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="target is a private address the policy refuses",
    )
    payload, event, _ = await _run_one(result=denial)
    assert str(payload) == str(denial)
    assert "non-bypassable security boundary" not in str(payload)
    assert event["detail"].startswith("terminal_denial:")


async def test_the_model_sees_the_denial_without_a_hint_end_to_end() -> None:
    """The whole turn: the model calls a gated tool, gets the denial, and answers the user."""
    denial = _denial()
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content="restarting nginx",
                tool_calls=[
                    ToolCallRequest(
                        id="call_gated",
                        name="execute_on_server",
                        arguments={"command": "systemctl restart nginx"},
                    )
                ],
            ),
            LLMResponse(content="The gate denied that. I stopped and told you.", tool_calls=[]),
        ]
    )
    tools = _tools(result=denial)
    spec = make_run_spec(
        provider,
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    result = await AgentRunner().run(spec)

    assert result.error is None
    assert result.stop_reason == "completed"
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_messages
    content = str(tool_messages[0]["content"])
    assert "This action is over" in content
    assert _HINT not in content
    assert "Analyze the error above" not in content
