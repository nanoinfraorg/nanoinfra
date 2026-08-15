# tests/agent/tools/test_execute_tool_client.py
"""What stays agent-side after the executor split -- nanoinfraorg/nanoinfra#18.

``tests/agent/tools/test_server_execution.py`` held the execution mechanics, and #18 moved those
to ``tests/gates/test_executor_execution.py`` with the transports. One property in that file was
never about execution at all, so it stays here: the tool must reach the model.

The rest of the agent side already has homes. The denial latch (#15) lives in
tests/gates/test_latch.py. The two preview messages (#10) live in
tests/agent/tools/test_gate_owned_dry_run.py. The ``ExecutorUnavailableError`` path lives in
tests/gates/test_executor_client.py.
"""

from __future__ import annotations

from nanoinfra.agent.tools.loader import ToolLoader


def test_the_execute_tool_is_discovered() -> None:
    """The loader finds tools by a pkgutil scan, so a rename or a move can hide one silently.

    A tool the loader misses reaches no model, and nothing else fails to say so.
    """
    names = {tool.__name__ for tool in ToolLoader().discover()}

    assert "ExecuteOnServerTool" in names
