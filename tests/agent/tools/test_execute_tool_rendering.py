# tests/agent/tools/test_execute_tool_rendering.py
"""What stays on the agent side after the split (#18).

The tool holds no transport and no credential. It keeps three jobs: ask the latch, carry the
request, and render the reply. These tests drive a fake client, because the socket is the
executor's boundary and it has its own tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import (
    PREVIEW_ON_REQUEST_NOTE,
    PREVIEW_WITHHELD_NOTE,
    ExecuteOnServerTool,
)
from nanoinfra.gates.executor.client import ExecutorUnavailableError
from nanoinfra.gates.executor.protocol import ExecuteResponse
from nanoinfra.gates.latch import TerminalDenial


class _FakeClient:
    """Stands in for the socket. Records what the tool asked for."""

    def __init__(self, response: ExecuteResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def execute(self, **kwargs: Any) -> ExecuteResponse:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _tool(response: ExecuteResponse | Exception, *, gate: Any = None) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(client=_FakeClient(response), gate=gate)  # pyright: ignore[reportArgumentType]


def _ctx(execution_context: str = EXECUTION_CONTEXT_INTERACTIVE) -> RequestContext:
    return RequestContext(
        channel="webui", chat_id="c1", session_key="s1", execution_context=execution_context
    )


def _preview(reason: str = "the caller asked for a preview") -> ExecuteResponse:
    return ExecuteResponse(
        ok=True,
        output="Preview (not executed): server='prod-web-01' command='uptime'",
        exit_code=None,
        error=None,
        reason=reason,
    )


def _withheld(reason: str) -> ExecuteResponse:
    return ExecuteResponse(
        ok=False,
        output="Preview (not executed): server='prod-web-01' command='uptime'",
        exit_code=None,
        error=None,
        reason=reason,
    )


@pytest.mark.asyncio
async def test_a_requested_preview_says_the_caller_asked_for_it() -> None:
    tool = _tool(_preview())

    with request_context(_ctx()):
        result = await tool.execute(server_id_or_name="prod-web-01", command="uptime")

    assert PREVIEW_ON_REQUEST_NOTE in str(result)
    assert PREVIEW_WITHHELD_NOTE not in str(result)


@pytest.mark.asyncio
async def test_a_withheld_preview_says_policy_would_not_permit_execution() -> None:
    tool = _tool(_withheld("mutate.remote at host scope is deny"))

    with request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert result.is_error
    assert PREVIEW_WITHHELD_NOTE in str(result)
    assert PREVIEW_ON_REQUEST_NOTE not in str(result)


@pytest.mark.asyncio
async def test_the_two_preview_cases_never_share_a_message() -> None:
    """An operator who cannot tell the cases apart learns that a preview means nothing."""
    asked = _tool(_preview())
    withheld = _tool(_withheld("no grant covers this"))

    with request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)):
        asked_result = str(await asked.execute(server_id_or_name="s", command="c"))
        withheld_result = str(
            await withheld.execute(server_id_or_name="s", command="c", dry_run=False)
        )

    assert asked_result != withheld_result
    assert PREVIEW_ON_REQUEST_NOTE not in withheld_result
    assert PREVIEW_WITHHELD_NOTE not in asked_result


@pytest.mark.asyncio
async def test_a_withheld_preview_still_shows_what_would_have_run() -> None:
    """The action line is what tells an operator which grant to write."""
    tool = _tool(_withheld("no grant covers this"))

    with request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert "Preview (not executed)" in str(result)
    assert "no grant covers this" in str(result)


@pytest.mark.asyncio
async def test_the_request_carries_the_caller_intent_and_not_an_authorization() -> None:
    """`dry_run` asks. It never authorizes, so it travels as `preview_requested`."""
    tool = _tool(_preview())

    with request_context(_ctx()):
        await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)

    client: Any = tool.client
    assert client.calls[0]["preview_requested"] is False
    assert client.calls[0]["execution_context"] == EXECUTION_CONTEXT_INTERACTIVE


def test_the_schema_keeps_dry_run_and_stops_calling_it_a_confirmation() -> None:
    """Existing sessions and transcripts hold the argument, so it stays in the schema."""
    tool = _tool(_preview())
    schema = tool.parameters["properties"]["dry_run"]

    assert "confirm" not in schema["description"].lower()
    assert "does not authorize" in schema["description"]


@pytest.mark.asyncio
async def test_an_unreachable_executor_reads_as_a_deployment_fault() -> None:
    """A missing executor and a refusal need different words.

    An operator who reads "denied" for a stopped process edits policy instead of starting a
    service.
    """
    tool = _tool(ExecutorUnavailableError("no such socket"))

    with request_context(_ctx()):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert result.is_error
    assert "deployment fault" in str(result)
    assert PREVIEW_WITHHELD_NOTE not in str(result)


@pytest.mark.asyncio
async def test_a_latched_session_never_reaches_the_executor(tmp_path) -> None:
    """The latch answers first (#15). Asking again could produce a prompt, and that is the oracle."""
    from nanoinfra.config.gates import GatesConfig
    from nanoinfra.gates.runtime import build_gate_runtime

    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    runtime.refuse_action(
        session_id="s1",
        capability_class="mutate.remote",
        tool="execute_on_server",
        reason="no grant",
        execution_context="automation",
    )
    tool = _tool(_preview(), gate=runtime)

    with request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    client: Any = tool.client
    assert isinstance(result, TerminalDenial)
    assert client.calls == []


@pytest.mark.asyncio
async def test_a_refusal_becomes_terminal_when_a_gate_is_present(tmp_path) -> None:
    from nanoinfra.config.gates import GatesConfig
    from nanoinfra.gates.runtime import build_gate_runtime

    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    tool = _tool(_withheld("no grant covers this"), gate=runtime)

    with request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert isinstance(result, TerminalDenial)


@pytest.mark.asyncio
async def test_a_tool_without_a_gate_still_surfaces_the_refusal() -> None:
    """No runtime means no latch and no terminal denial. The action is still refused."""
    tool = _tool(_withheld("no grant covers this"))

    with request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert result.is_error
    assert not isinstance(result, TerminalDenial)


@pytest.mark.asyncio
async def test_an_executor_error_reaches_the_caller_unchanged() -> None:
    """A resolution failure is the executor's message, and the tool does not reword it."""
    tool = _tool(
        ExecuteResponse(
            ok=False, output="", exit_code=None, error="No server matches 'nope'.", reason=""
        )
    )

    with request_context(_ctx()):
        result = await tool.execute(server_id_or_name="nope", command="uptime")

    assert result.is_error
    assert "No server matches 'nope'." in str(result)


@pytest.mark.asyncio
async def test_a_successful_run_reports_the_exit_code_and_output() -> None:
    tool = _tool(
        ExecuteResponse(ok=True, output="up 3 days", exit_code=0, error=None, reason="")
    )

    with request_context(_ctx()):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert "up 3 days" in str(result)
    assert "exit code 0" in str(result)


@pytest.mark.asyncio
async def test_the_tool_creates_its_client_from_the_context_socket(tmp_path) -> None:
    """The gateway passes the socket path, and the default lands in the data dir."""
    from nanoinfra.agent.tools.context import ToolContext

    ctx = ToolContext(config=None, workspace=str(tmp_path))  # pyright: ignore[reportArgumentType]
    with patch.object(ToolContext, "__init__", ToolContext.__init__):
        tool = ExecuteOnServerTool.create(ctx)

    assert isinstance(tool, ExecuteOnServerTool)
    assert tool.client.socket_path.name.endswith(".sock")


@pytest.mark.asyncio
async def test_another_session_is_not_latched_by_this_one(tmp_path) -> None:
    """Migrated from test_gate_wiring.py. The latch keys on the session, and one denial must
    not stop an unrelated session from acting."""
    from nanoinfra.config.gates import GatesConfig
    from nanoinfra.gates.runtime import build_gate_runtime

    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    runtime.refuse_action(
        session_id="s1",
        capability_class="mutate.remote",
        tool="execute_on_server",
        reason="no grant",
        execution_context="automation",
    )
    tool = _tool(
        ExecuteResponse(ok=True, output="up 3 days", exit_code=0, error=None, reason=""),
        gate=runtime,
    )
    other = RequestContext(
        channel="webui",
        chat_id="c2",
        session_key="s2",
        execution_context=EXECUTION_CONTEXT_INTERACTIVE,
    )

    with request_context(other):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert "up 3 days" in str(result)
    assert not isinstance(result, TerminalDenial)
