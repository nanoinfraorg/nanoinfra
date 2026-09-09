"""A remote run points at device memory, in the turn that learned something (#223 follow-up).

The read half of device memory is mention-gated on purpose, and the natural way to work a box is
`execute_on_server` with a name argument -- which is not a mention. So a deployment that never
types `@server:` never sees the block that asks for a note. One box was worked 37 times and
accumulated nothing.

The instruction has to arrive where the learning happens: attached to the result of the command
that produced it, in the same turn, while the agent still holds what it just saw. A tool
description cannot do this job -- it reads as "what this tool does if you call it", and it competes
with every other description in the prompt.

Silent when `device_notes` is not attached, because naming a tool the turn cannot call is worse
than saying nothing.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.agent.tools import groups
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import ExecuteResponse
from nanoinfra.gates.runtime import build_gate_runtime
from nanoinfra.session.automation_turns import AUTOMATION_AGENT_META


class _AllowingClient:
    def execute(self, **_kwargs: Any) -> Any:
        return ExecuteResponse(ok=True, output="up 3 days", exit_code=0, error=None, reason="")


def _turn(tool_groups: list[str] | None = None) -> RequestContext:
    """A turn, optionally answered by an agent that declared a tool-group ceiling.

    ``None`` declares no ceiling, which is every turn in a deployment that names no agent. A list
    caps it -- and an empty list caps it to nothing, which is a real config an operator writes.
    """
    metadata: dict[str, Any] = {}
    if tool_groups is not None:
        metadata[AUTOMATION_AGENT_META] = {"tool_groups": tool_groups}
    return RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="s1",
        metadata=metadata,
        execution_context=EXECUTION_CONTEXT_INTERACTIVE,
    )


async def _run(tmp_path: Any, tool_groups: list[str] | None = None) -> str:
    gate, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    tool = ExecuteOnServerTool(client=_AllowingClient(), gate=gate)  # pyright: ignore[reportArgumentType]
    with request_context(_turn(tool_groups)):
        return str(
            await tool.execute(
                server_id_or_name="barrahome", command="uptime", dry_run=False
            )
        )


async def test_a_successful_run_points_at_device_memory(tmp_path: Any) -> None:
    """The turn that learned something is the turn that is asked to record it."""
    groups.set_tool_groups(None)
    try:
        output = await _run(tmp_path)
    finally:
        groups.set_tool_groups(None)

    # The result itself is unchanged.
    assert "up 3 days" in output
    assert "barrahome" in output
    # And it now names the tool that records what was learned.
    assert "device_notes" in output
    # With the bar the tool's own description sets, so the nudge does not produce a log of
    # routine checks.
    assert "routine" in output.lower()


async def test_the_nudge_is_absent_when_the_agent_cannot_reach_the_tool(tmp_path: Any) -> None:
    """The real config this guards against: ``agents.defaults.toolGroups = []``.

    An empty declared list is not "everything" -- it caps the turn to the ungrouped tools, so
    ``device_notes`` is unreachable. Pointing at it would name a tool the turn cannot call, which
    is worse than saying nothing. Same ``is_attached`` test the notes block applies to itself.
    """
    groups.set_tool_groups(None)
    try:
        output = await _run(tmp_path, tool_groups=[])
    finally:
        groups.set_tool_groups(None)

    assert "up 3 days" in output
    assert "device_notes" not in output


async def test_an_agent_that_declared_the_servers_group_still_gets_the_nudge(
    tmp_path: Any,
) -> None:
    """The other side of the ceiling, so the guard cannot pass by suppressing everything."""
    groups.set_tool_groups(None)
    try:
        output = await _run(tmp_path, tool_groups=["servers"])
    finally:
        groups.set_tool_groups(None)

    assert "device_notes" in output
