# tests/agent/tools/test_gate_wiring.py
"""Item 31 (#33): the gate actually calls the latch, the audit store, and the tokens.

Before this, a refusal was a plain ToolResult.error: no denial was terminal, no latch formed,
and no audit record landed. The log-only recorder from #3 kept writing observations, which is
why the gap was easy to miss.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_AUTOMATION,
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
from nanoinfra.agent.tools.servers import UpdateServerTool
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.latch import TerminalDenial
from nanoinfra.gates.runtime import build_gate_runtime
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_POLICY = "nanoinfra.agent.tools.server_execution.load_policy"
_INVENTORY_POLICY = "nanoinfra.agent.tools.servers.load_policy"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _ctx(execution_context: str) -> RequestContext:
    return RequestContext(
        channel="cron", chat_id="c1", session_key="s1", execution_context=execution_context
    )


def _server(tmp_path: Path) -> ServerStore:
    store = ServerStore(tmp_path)
    store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    return store


def _tool(tmp_path: Path, gate: Any) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(
        servers=_server(tmp_path),
        secrets=SecretStore(tmp_path),
        jobs=JobStore(tmp_path),
        gate=gate,
    )


@pytest.mark.asyncio
async def test_a_refusal_is_a_terminal_denial(tmp_path: Path) -> None:
    """Without this, the runner appends "try a different approach" and the oracle returns."""
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")

    with (
        patch(_POLICY, return_value=GatesConfig()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path, runtime).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert isinstance(result, TerminalDenial)


@pytest.mark.asyncio
async def test_a_second_attempt_refuses_without_asking_policy(tmp_path: Path) -> None:
    """The latch answers first. Re-asking policy is what produces a fresh prompt."""
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    tool = _tool(tmp_path, runtime)

    with (
        patch(_POLICY, return_value=GatesConfig()) as policy,
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)
        calls_after_first = policy.call_count
        second = await tool.execute(
            server_id_or_name="prod-web-01", command="whoami", dry_run=False
        )

    assert isinstance(second, TerminalDenial)
    assert policy.call_count == calls_after_first


@pytest.mark.asyncio
async def test_a_denial_writes_one_audit_record(tmp_path: Path) -> None:
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")

    with (
        patch(_POLICY, return_value=GatesConfig()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        await _tool(tmp_path, runtime).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    decisions = [r["decision"] for r in runtime.audit.read_all()]
    assert "denied" in decisions


@pytest.mark.asyncio
async def test_an_allowed_action_is_recorded_too(tmp_path: Path) -> None:
    """A record only of denials would leave a reviewer unable to see what ran."""
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    granted = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {"id": "ok", "contexts": ["unattended"], "hosts": ["10.0.1.5"], "commands": ["uptime"]}
            ],
        }
    )
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with (
        patch(_POLICY, return_value=granted),
        patch(
            "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
            new=AsyncMock(return_value=fake),
        ),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        await _tool(tmp_path, runtime).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    records = runtime.audit.read_all()
    assert [r["decision"] for r in records] == ["allow"]
    assert records[0]["grant_id"] == "ok"


@pytest.mark.asyncio
async def test_an_audit_write_failure_refuses_the_action(tmp_path: Path) -> None:
    """An action that nothing recorded must not run. #16 raises so the gate can fail closed."""
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    granted = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {"id": "ok", "contexts": ["unattended"], "hosts": ["10.0.1.5"], "commands": ["uptime"]}
            ],
        }
    )

    with (
        patch(_POLICY, return_value=granted),
        patch.object(runtime.audit, "record", side_effect=OSError("disk full")),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path, runtime).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert result.is_error
    run.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_a_tool_without_a_gate_still_applies_policy(tmp_path: Path) -> None:
    """An embedded or test construction passes no runtime. Policy must still refuse."""
    with (
        patch(_POLICY, return_value=GatesConfig()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path, None).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert result.is_error
    run.assert_not_called()


@pytest.mark.asyncio
async def test_an_interactive_call_is_not_latched_by_an_unattended_denial(
    tmp_path: Path,
) -> None:
    """The latch keys on the session, so a different session stays unaffected."""
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    tool = _tool(tmp_path, runtime)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with (
        patch(_POLICY, return_value=GatesConfig()),
        patch(
            "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
            new=AsyncMock(return_value=fake),
        ) as run,
    ):
        with request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)):
            await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)
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

    assert not getattr(result, "is_error", False)
    run.assert_called_once()


@pytest.mark.asyncio
async def test_an_unattended_inventory_write_is_a_terminal_denial(tmp_path: Path) -> None:
    """#23's gate joins the same runtime, so its refusal latches and records too."""
    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates")
    store = _server(tmp_path)
    server = store.list_servers()[0]

    with (
        patch(_INVENTORY_POLICY, return_value=GatesConfig()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await UpdateServerTool(store, gate=runtime).execute(
            server_id=server.id,
            name="prod-web-01",
            providerId="ssh",
            config={"host": "10.9.9.9"},
            dry_run=False,
        )

    assert isinstance(result, TerminalDenial)
    assert store.get(server.id).config["host"] == "10.0.1.5"
    assert "denied" in [r["decision"] for r in runtime.audit.read_all()]
