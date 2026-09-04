"""The runner writes one tool-call row per call (#232).

Its own test file, because the placement is the claim. `_run_tool` resolves a tool through
`prepare_call` and then awaits it directly, so `ToolRegistry.execute` is never reached for a tool
the registry resolved -- and a row written only there would have missed every call the agent makes.
These tests fail if that seam moves back.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.runner_helpers import make_run_spec
from nanoinfra.agent.runner import AgentRunner
from nanoinfra.agent.tools.base import Tool
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.llm_usage import get_llm_usage_store, reset_llm_usage_stores
from nanoinfra.llm_usage.gate_join import note_gate_decision
from nanoinfra.llm_usage.store import LLMUsageStore
from nanoinfra.llm_usage.tool_calls import reset_tool_call_seq
from nanoinfra.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _SilentProvider(LLMProvider):
    """Never asked anything: these tests drive `_execute_tools` directly."""

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="")

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="")


class _CountingTool(Tool):
    capability_class = "read"

    def __init__(self, *, name: str = "read_file", before: Any = None) -> None:
        self._name = name
        self._before = before
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "read a file"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, path: str = "", **kwargs: Any
    ) -> Any:
        self.calls += 1
        if self._before is not None:
            self._before()
        return f"read {path}"


@pytest.fixture
def store() -> Any:
    reset_llm_usage_stores()
    reset_tool_call_seq()
    yield get_llm_usage_store()
    reset_llm_usage_stores()


def _spec(registry: ToolRegistry, *, concurrent: bool = False) -> Any:
    return make_run_spec(
        _SilentProvider(),
        model="fake/model",
        initial_messages=[],
        tools=registry,
        max_iterations=1,
        max_tool_result_chars=4_000,
        concurrent_tools=concurrent,
    )


def _turn() -> RequestContext:
    return RequestContext(
        channel="webui",
        chat_id="1",
        session_key="webui:alberto",
        turn_id="turn-1",
        sender_id="alberto",
        execution_context="interactive",
    )


async def test_the_runner_writes_one_row_per_call_it_executed(store: LLMUsageStore) -> None:
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    calls = [
        ToolCallRequest(id="a", name="read_file", arguments={"path": "/etc/hostname"}),
        ToolCallRequest(id="b", name="read_file", arguments={"path": "/etc/hosts"}),
    ]

    with request_context(_turn()):
        await AgentRunner()._execute_tools(  # pyright: ignore[reportPrivateUsage]
            _spec(registry), calls, {}, {}
        )

    rows = store.tool_calls()

    assert tool.calls == 2, "the tool ran through the direct path, not through registry.execute"
    assert len(rows) == 2
    assert {row["capability_class"] for row in rows} == {"read"}
    assert sorted(int(row["seq"]) for row in rows) == [0, 1]
    assert {row["outcome"] for row in rows} == {"ok"}


async def test_a_concurrent_batch_gets_one_row_each(store: LLMUsageStore) -> None:
    """Each call runs in its own task, which gets a *copy* of the context. The counter is not."""
    registry = ToolRegistry()
    registry.register(_CountingTool())
    calls = [
        ToolCallRequest(id=str(index), name="read_file", arguments={"path": f"/tmp/{index}"})
        for index in range(4)
    ]

    with request_context(_turn()):
        await AgentRunner()._execute_tools(  # pyright: ignore[reportPrivateUsage]
            _spec(registry, concurrent=True), calls, {}, {}
        )

    assert sorted(int(row["seq"]) for row in store.tool_calls()) == [0, 1, 2, 3]


async def test_the_runner_row_carries_the_gate_decision(store: LLMUsageStore) -> None:
    registry = ToolRegistry()
    registry.register(
        _CountingTool(
            before=lambda: note_gate_decision(
                decision="denied", reason="read at host scope is deny", actor=None
            )
        )
    )
    calls = [ToolCallRequest(id="a", name="read_file", arguments={"path": "/etc/shadow"})]

    with request_context(_turn()):
        await AgentRunner()._execute_tools(  # pyright: ignore[reportPrivateUsage]
            _spec(registry), calls, {}, {}
        )

    row = store.tool_calls()[0]

    assert row["gate_decision"] == "denied"
    assert row["outcome"] == "denied"


async def test_a_call_the_runner_refused_before_execution_is_still_a_row(
    store: LLMUsageStore,
) -> None:
    """A name that resolved to nothing is a call that happened and answered an error."""
    registry = ToolRegistry()
    registry.register(_CountingTool())
    calls = [ToolCallRequest(id="a", name="nope", arguments={})]

    with request_context(_turn()):
        await AgentRunner()._execute_tools(  # pyright: ignore[reportPrivateUsage]
            _spec(registry), calls, {}, {}
        )

    row = store.tool_calls()[0]

    assert row["tool"] == "nope"
    assert row["outcome"] == "error"
    assert row["error_kind"] == "not_found"
    assert row["capability_class"] is None
