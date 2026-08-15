# tests/agent/tools/test_unattended_enforcement.py
"""Item 6 (#8): enforce gates.unattended inside execute_on_server.

Position matters as much as the decision. A refused action must not decrypt a credential on
its way to a refusal, and must not leave a job record implying it ran.

This issue enforces the UNATTENDED half only. The interactive default is `approve`, and the
approval path arrives with #13 and #14. Enforcing interactive here would break every
interactive remote command before any human could answer a prompt.
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
from nanoinfra.config.gates import GatesConfig
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_GRANTED_COMMAND = "systemctl reload nginx"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _tool(tmp_path: Path) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(
        servers=ServerStore(tmp_path), secrets=SecretStore(tmp_path), jobs=JobStore(tmp_path)
    )


def _server(tmp_path: Path, *, secret_ref: str | None = None) -> None:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
    }
    if secret_ref:
        raw["secretRef"] = secret_ref
    ServerStore(tmp_path).create(raw)


def _ctx(execution_context: str) -> RequestContext:
    return RequestContext(
        channel="telegram",
        chat_id="c1",
        session_key="s1",
        execution_context=execution_context,
    )


def _policy(**over: object) -> GatesConfig:
    return GatesConfig.model_validate(over) if over else GatesConfig()


@pytest.mark.asyncio
async def test_an_unattended_call_without_a_grant_is_refused(tmp_path: Path) -> None:
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert result.is_error
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_call_never_decrypts_a_credential(tmp_path: Path) -> None:
    """The gate runs before resolve_plaintext. A denial must not touch the secret store."""
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "ssh_key", "providerId": "local", "value": "s3cr3t"}
    )
    _server(tmp_path, secret_ref=secret.id)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_call_leaves_no_job_record(tmp_path: Path) -> None:
    """A job record for an action that never ran would misreport history."""
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_the_refusal_names_the_class_and_the_missing_grant(tmp_path: Path) -> None:
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert "mutate.remote" in str(result)
    assert "grant" in str(result)


@pytest.mark.asyncio
async def test_a_matching_grant_lets_an_unattended_call_run(tmp_path: Path) -> None:
    _server(tmp_path)
    granted = _policy(
        unattended={"mutate.remote": {"host": "grant"}},
        standingGrants=[
            {
                "id": "reload",
                "contexts": ["unattended"],
                "hosts": ["10.0.1.5"],
                "commands": [_GRANTED_COMMAND],
            }
        ],
    )
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=granted),
        patch(
            "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
            new=AsyncMock(return_value=fake),
        ) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command=_GRANTED_COMMAND, dry_run=False
        )

    assert not getattr(result, "is_error", False)
    run.assert_called_once()


@pytest.mark.asyncio
async def test_an_interactive_call_is_not_gated_yet(tmp_path: Path) -> None:
    """#8 enforces the unattended half only.

    The interactive default is `approve` and no approval path exists before #13 and #14.
    Enforcing it here would refuse every interactive remote command with no way to answer.
    """
    _server(tmp_path)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch(
            "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
            new=AsyncMock(return_value=fake),
        ) as run,
        request_context(_ctx(EXECUTION_CONTEXT_INTERACTIVE)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert not getattr(result, "is_error", False)
    run.assert_called_once()


@pytest.mark.asyncio
async def test_a_preview_is_never_refused_by_the_gate(tmp_path: Path) -> None:
    """A preview connects to nothing, so refusing it would teach nothing and block reading."""
    _server(tmp_path)

    with (
        patch("nanoinfra.agent.tools.server_execution.load_policy", return_value=_policy()),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(server_id_or_name="prod-web-01", command="uptime")

    assert "Preview (not executed)" in str(result)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_an_unreadable_policy_refuses_an_unattended_call(tmp_path: Path) -> None:
    """Unparseable policy must fail closed. A broken config is not a reason to skip the gate."""
    _server(tmp_path)

    with (
        patch("nanoinfra.config.loader.load_config", side_effect=RuntimeError("bad config")),
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run,
        request_context(_ctx(EXECUTION_CONTEXT_AUTOMATION)),
    ):
        result = await _tool(tmp_path).execute(
            server_id_or_name="prod-web-01", command="uptime", dry_run=False
        )

    assert result.is_error
    run.assert_not_called()
