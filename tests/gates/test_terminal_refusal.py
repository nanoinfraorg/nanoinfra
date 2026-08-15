# tests/gates/test_terminal_refusal.py
"""Item 40 (#42): an unanswerable approval must not latch the session.

Found live, twice in one turn, on a WebUI-only deployment with the shipped defaults. The maintainer
asked for one host command, the policy said `approve`, and `approval_feasible` refused because the
deployment holds no approver on a second path. The tool then latched the capability class, so the
next attempt refused before it asked.

#15 latches a denial so the agent cannot retry with a slightly changed command until something gets
through. That reasoning needs an agent that could retry. A deployment that can answer no approval
at all gives the agent nothing to change, and the operator clears a latch their own config caused.

A latch an operator clears as routine is a control they stop reading, which is the failure #13
names for a prompt that fires forty times a week.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import (
    ExecuteResponse,
    decode_response,
    encode_response,
)
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_SERVER = "web-01"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _server(tmp_path: Path) -> None:
    ServerStore(tmp_path).create(
        {
            "name": _SERVER,
            "providerId": "ssh",
            "config": {"host": "10.0.2.11", "port": "22", "username": "ops"},
        }
    )


def _request(**over: object) -> Any:
    from nanoinfra.gates.executor.protocol import ExecuteRequest

    fields: dict[str, Any] = {
        "server_id_or_name": _SERVER,
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
        "origin_path": "websocket",
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _executor(tmp_path: Path, gates: GatesConfig) -> Executor:
    """An executor with the approval stores a real deployment wires.

    Without them the suspend refuses on the missing store, which is a different deployment fact and
    a different message. The feasibility refusal is the one an operator meets.
    """
    from nanoinfra.gates.pending import PendingApprovalStore
    from nanoinfra.gates.tokens import ApprovalTokenStore

    return Executor(
        workspace=tmp_path,
        gates_loader=lambda: gates,
        pending=PendingApprovalStore(),
        tokens=ApprovalTokenStore(),
    )


def test_a_response_is_terminal_by_default() -> None:
    """A response that says nothing must still stop a retry loop."""
    response = ExecuteResponse(ok=False, output="", exit_code=None, error=None, reason="no")

    assert response.terminal is True


def test_the_wire_carries_the_flag() -> None:
    """Both peers ship together, and the decoder refuses a frame that misses a field."""
    original = ExecuteResponse(
        ok=False, output="", exit_code=None, error=None, reason="no", terminal=False
    )

    assert decode_response(encode_response(original)).terminal is False


@pytest.mark.asyncio
async def test_an_unanswerable_approval_is_not_terminal(tmp_path: Path) -> None:
    """The case that latched the maintainer's session: no approver, so nobody may answer."""
    _server(tmp_path)
    gates = GatesConfig.model_validate(
        {"interactive": {"mutate.remote": {"host": "approve"}}, "approvers": []}
    )

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, gates).handle(_request())

    assert not response.ok
    run.assert_not_called()
    assert response.terminal is False, "a configuration gap must not latch the session"


@pytest.mark.asyncio
async def test_a_policy_denial_stays_terminal(tmp_path: Path) -> None:
    """A `deny` is an answer about this action, so the retry loop still ends."""
    _server(tmp_path)
    gates = GatesConfig.model_validate({"interactive": {"mutate.remote": {"host": "deny"}}})

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, gates).handle(_request())

    assert not response.ok
    run.assert_not_called()
    assert response.terminal is True


@pytest.mark.asyncio
async def test_the_refusal_names_the_fix_for_an_ad_hoc_command(tmp_path: Path) -> None:
    """A standing grant matches an exact command, so it solves recurring work and not this."""
    _server(tmp_path)
    gates = GatesConfig.model_validate(
        {"interactive": {"mutate.remote": {"host": "approve"}}, "approvers": []}
    )

    response = await _executor(tmp_path, gates).handle(_request())

    text = f"{response.reason} {response.output}"
    assert "second" in text.lower(), "the message must name the path fix"
    assert "grant" in text.lower(), "and the grant fix, for recurring work"
    assert "recurring" in text.lower(), "and say which case a grant suits"


class _FakeGate:
    """The latch surface the tool holds, with a record of what reached it."""

    def __init__(self) -> None:
        self.refusals: list[str] = []

    def latched_refusal(self, **_kwargs: Any) -> Any:
        return None

    def refuse_action(self, *, reason: str, **_kwargs: Any) -> Any:
        self.refusals.append(reason)
        return "LATCHED"


class _FakeClient:
    """One canned answer from the executor."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def execute(self, **_kwargs: Any) -> Any:
        return self._response


def _tool(response: Any) -> Any:
    from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool

    tool = ExecuteOnServerTool.__new__(ExecuteOnServerTool)
    tool.gate = _FakeGate()
    tool.client = _FakeClient(response)
    return tool


@pytest.mark.asyncio
async def test_the_tool_leaves_the_latch_alone_for_a_deployment_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that mattered to the operator: a second attempt still reaches the gate.

    The tool called the latch for every refusal, so a configuration gap blocked the class and the
    next attempt refused before it asked.
    """
    from nanoinfra.agent.tools import server_execution as tool_module

    response = ExecuteResponse(
        ok=False, output="preview", exit_code=None, error=None, reason="no approver", terminal=False
    )
    tool = _tool(response)
    monkeypatch.setattr(tool_module.ExecuteOnServerTool, "_session_id", lambda _self: "s1")

    result = await tool.execute(server_id_or_name=_SERVER, command="uptime", dry_run=False)

    assert tool.gate.refusals == [], "a deployment refusal must not reach the latch"
    assert "not blocked" in str(result.content if hasattr(result, "content") else result)


@pytest.mark.asyncio
async def test_the_tool_latches_a_policy_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property #15 asks for stays intact: a policy denial still ends the retry loop."""
    from nanoinfra.agent.tools import server_execution as tool_module

    response = ExecuteResponse(
        ok=False, output="preview", exit_code=None, error=None, reason="denied", terminal=True
    )
    tool = _tool(response)
    monkeypatch.setattr(tool_module.ExecuteOnServerTool, "_session_id", lambda _self: "s1")

    result = await tool.execute(server_id_or_name=_SERVER, command="uptime", dry_run=False)

    assert tool.gate.refusals == ["denied"]
    assert result == "LATCHED"
