# tests/tools/test_server_execution_approvals.py
"""Item 36 (#38) on the agent side: what the tool does with a suspended action.

The executor holds the wait, so the tool does very little here. It has to do that little
correctly, because two of the item's properties land in this file.

**A denial latches.** The executor returns a refusal for a denied approval and for an expired
one. The tool must turn both into a terminal denial, so the retry hint never reaches the model
and #15's latch catches the next attempt in that session.

**A latched class asks nobody.** The second attempt must not reach the executor at all. A fresh
request could produce a fresh prompt, and a fresh prompt is the brute-force oracle #15 removes.

The tests drive a fake client. The socket has its own tests in tests/gates/, and the real
executor process has tests/gates/test_approval_process.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import PREVIEW_WITHHELD_NOTE, ExecuteOnServerTool
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import ExecuteResponse
from nanoinfra.gates.latch import TerminalDenial
from nanoinfra.gates.runtime import build_gate_runtime

_DENIED_REASON = (
    "an operator denied this action: the change window is closed."
)
_EXPIRED_REASON = (
    "no operator answered before the deadline, so the action expired. Ask again when an "
    "approver is present, or declare a standing grant"
)


class _FakeClient:
    """Stands in for the socket, and records every request the tool sent."""

    def __init__(self, response: ExecuteResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def execute(self, **kwargs: Any) -> ExecuteResponse:
        self.calls.append(kwargs)
        return self._response


def _refusal(reason: str) -> ExecuteResponse:
    """The shape the executor returns for every gate refusal, including an approval one."""
    return ExecuteResponse(
        ok=False,
        output="Preview (not executed): server='prod-web-01' command='systemctl reload nginx'",
        exit_code=None,
        error=None,
        reason=reason,
    )


def _tool(response: ExecuteResponse, *, gate: Any) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(client=_FakeClient(response), gate=gate)  # pyright: ignore[reportArgumentType]


def _runtime(tmp_path: Path):
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    return runtime


def _ctx() -> RequestContext:
    return RequestContext(
        channel="telegram",
        chat_id="c1",
        session_key="s1",
        execution_context=EXECUTION_CONTEXT_INTERACTIVE,
    )


async def _run(tool: ExecuteOnServerTool, command: str = "systemctl reload nginx") -> Any:
    with request_context(_ctx()):
        return await tool.execute(
            server_id_or_name="prod-web-01", command=command, dry_run=False
        )


@pytest.mark.asyncio
async def test_a_denied_approval_becomes_a_terminal_denial(tmp_path: Path) -> None:
    """#15: a refusal must not read as an ordinary error, or the retry hint returns."""
    tool = _tool(_refusal(_DENIED_REASON), gate=_runtime(tmp_path))

    result = await _run(tool)

    assert isinstance(result, TerminalDenial)
    assert "change window" in str(result)


@pytest.mark.asyncio
async def test_an_expired_approval_becomes_a_terminal_denial(tmp_path: Path) -> None:
    """An expiry is a refusal too. A model that reads it as an error retries at once."""
    tool = _tool(_refusal(_EXPIRED_REASON), gate=_runtime(tmp_path))

    result = await _run(tool)

    assert isinstance(result, TerminalDenial)
    assert "expired" in str(result)


@pytest.mark.asyncio
async def test_the_second_attempt_after_a_denial_never_reaches_the_executor(
    tmp_path: Path,
) -> None:
    """The latch answers before the request leaves, so no second prompt can form."""
    runtime = _runtime(tmp_path)
    first = _tool(_refusal(_DENIED_REASON), gate=runtime)
    second = _tool(_refusal(_DENIED_REASON), gate=runtime)

    await _run(first)
    result = await _run(second, command="uptime")

    client: Any = second.client
    assert isinstance(result, TerminalDenial)
    assert client.calls == []


@pytest.mark.asyncio
async def test_the_terminal_denial_carries_the_reason_and_no_retry_hint(tmp_path: Path) -> None:
    """#15 keeps this text minimal, so the resolved action line stays out of it.

    The line names the command, and the reason travels into the audit record. #16 keeps command
    text out of that record by default, so the two rules together decide what this text holds:
    the operator's reason, the class, and the terminal marker.
    """
    tool = _tool(_refusal(_DENIED_REASON), gate=_runtime(tmp_path))

    result = await _run(tool)

    assert "change window" in str(result)
    assert "mutate.remote" in str(result)
    assert "systemctl reload nginx" not in str(result)


@pytest.mark.asyncio
async def test_a_deployment_without_a_gate_still_shows_the_withheld_action() -> None:
    """No gate runtime means no latch and no terminal denial, and the action is still refused."""
    tool = _tool(_refusal(_DENIED_REASON), gate=None)

    result = await _run(tool)

    assert result.is_error
    assert "Preview (not executed)" in str(result)
    assert PREVIEW_WITHHELD_NOTE in str(result)


@pytest.mark.asyncio
async def test_a_denial_reaches_the_audit_log(tmp_path: Path) -> None:
    """#16 records every decision, and the tool's own refusal is one of them."""
    runtime = _runtime(tmp_path)
    tool = _tool(_refusal(_DENIED_REASON), gate=runtime)

    await _run(tool)

    records = runtime.audit.read_all()
    assert "denied" in [record["decision"] for record in records]
    # #16 keeps command text out of the record by default, and the reason must not smuggle it.
    assert "systemctl reload nginx" not in str(records)


def test_the_deployment_can_name_the_executor_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """entrypoint.sh exports this path, because the container binds the socket outside $HOME.

    Nothing read the variable before #38. The tool then looked in the agent's home while the
    executor listened under /run, so every remote action read as an unreachable executor.
    """
    from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV, default_socket_path

    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, "/run/nanoinfra-exec/executor.sock")

    assert default_socket_path() == Path("/run/nanoinfra-exec/executor.sock")


def test_an_empty_socket_variable_falls_back_to_the_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank value is not a path. A deployment that exports one gets the default."""
    from nanoinfra.agent.tools.server_execution import (
        DEFAULT_SOCKET_NAME,
        EXECUTOR_SOCKET_ENV,
        default_socket_path,
    )

    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, "   ")

    assert default_socket_path().name == DEFAULT_SOCKET_NAME


@pytest.mark.asyncio
async def test_the_tool_sends_no_nonce(tmp_path: Path) -> None:
    """The executor issues every nonce and hands none to the agent (#38).

    A tool that proposed one would hand the model a field to fill, and the executor refuses such
    a request outright.
    """
    tool = _tool(_refusal(_DENIED_REASON), gate=_runtime(tmp_path))

    await _run(tool)

    client: Any = tool.client
    assert client.calls[0].get("token_nonce") is None
