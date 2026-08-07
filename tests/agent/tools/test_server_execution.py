# tests/agent/tools/test_server_execution.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _tool(tmp_path: Path) -> ExecuteOnServerTool:
    return ExecuteOnServerTool(
        servers=ServerStore(tmp_path),
        secrets=SecretStore(tmp_path),
        jobs=JobStore(tmp_path),
    )


@pytest.mark.asyncio
async def test_dry_run_does_not_execute_or_create_a_job(tmp_path: Path) -> None:
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    with patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run_mock:
        result = await tool.execute(server_id_or_name="prod-web-01", command="uptime")

    assert "Preview (not executed)" in result
    assert "prod-web-01" in result
    run_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_confirmed_run_creates_a_job_and_calls_the_matching_backend(tmp_path: Path) -> None:
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    fake_result = ExecutionResult(exit_code=0, output="up 3 days", error=None)
    with patch(
        "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
        new=AsyncMock(return_value=fake_result),
    ):
        result = await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)

    assert "up 3 days" in result
    jobs = JobStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert jobs[0].exit_code == 0


@pytest.mark.asyncio
async def test_unknown_server_returns_error(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = await tool.execute(server_id_or_name="ghost", command="uptime", dry_run=False)
    assert result.is_error


@pytest.mark.asyncio
async def test_secret_value_never_appears_in_the_tool_result(tmp_path: Path) -> None:
    secret_store = SecretStore(tmp_path)
    secret = secret_store.create({"name": "web-key", "kind": "ssh_key", "providerId": "local", "value": "s3cr3t-key-material"})
    server_store = ServerStore(tmp_path)
    server_store.create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}, "secretRef": secret.id}
    )
    tool = _tool(tmp_path)

    fake_result = ExecutionResult(exit_code=0, output="ok", error=None)
    with patch(
        "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
        new=AsyncMock(return_value=fake_result),
    ) as run_mock:
        result = await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)

    assert "s3cr3t-key-material" not in result
    # The backend must have received the *decrypted* value, even though the tool result never shows it.
    _, kwargs = run_mock.call_args
    passed_secret = run_mock.call_args.args[2] if len(run_mock.call_args.args) > 2 else kwargs.get("secret_value")
    assert passed_secret == "s3cr3t-key-material"


@pytest.mark.asyncio
async def test_blocked_target_returns_error_without_creating_a_job(tmp_path: Path) -> None:
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "metadata-server", "providerId": "ssh", "config": {"host": "169.254.169.254"}})
    tool = _tool(tmp_path)

    with patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run_mock:
        result = await tool.execute(server_id_or_name="metadata-server", command="uptime", dry_run=False)

    assert result.is_error
    run_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_backend_failure_marks_job_failed_not_completed(tmp_path: Path) -> None:
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    fake_result = ExecutionResult(exit_code=None, output="", error="connection refused")
    with patch(
        "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
        new=AsyncMock(return_value=fake_result),
    ):
        result = await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)

    assert "connection refused" in result
    jobs = JobStore(tmp_path).list_jobs()
    assert jobs[0].status == "failed"


def test_tool_is_discovered() -> None:
    from nanoinfra.agent.tools.loader import ToolLoader

    names = {tool.__name__ for tool in ToolLoader().discover()}
    assert "ExecuteOnServerTool" in names
