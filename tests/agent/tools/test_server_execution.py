# tests/agent/tools/test_server_execution.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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
    # secretRef is set here (unlike a minimal repro) specifically so this test can prove
    # the blocked-target refusal happens *before* any secret is ever decrypted, not just
    # before a job/backend call -- see also test_ansible_runner_group_only_config_refuses_*
    # below, which holds the "no-host-to-check" refusal to the same standard.
    secret_store = SecretStore(tmp_path)
    secret = secret_store.create(
        {"name": "metadata-key", "kind": "ssh_key", "providerId": "local", "value": "s3cr3t-metadata"}
    )
    server_store = ServerStore(tmp_path)
    server_store.create(
        {
            "name": "metadata-server",
            "providerId": "ssh",
            "config": {"host": "169.254.169.254"},
            "secretRef": secret.id,
        }
    )
    tool = _tool(tmp_path)

    with (
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run_mock,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve_mock,
    ):
        result = await tool.execute(server_id_or_name="metadata-server", command="uptime", dry_run=False)

    assert result.is_error
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_ansible_runner_group_only_config_refuses_before_secret_resolution(tmp_path: Path) -> None:
    """An ansible-runner server configured with only `group` (no host/inventoryHost)
    has no network address _target_host() can check -- that must be treated as
    "refuse, cannot validate", not "nothing to validate, proceed unguarded". Critical
    finding from code review: this used to fall straight through to secret decryption
    and AnsibleRunnerBackend.run() with no guard at all.
    """
    secret_store = SecretStore(tmp_path)
    secret = secret_store.create(
        {"name": "ansible-key", "kind": "ssh_key", "providerId": "local", "value": "s3cr3t-ansible"}
    )
    server_store = ServerStore(tmp_path)
    server_store.create(
        {
            "name": "ansible-group-only",
            "providerId": "ansible-runner",
            "config": {"group": "web"},
            "secretRef": secret.id,
        }
    )
    tool = _tool(tmp_path)

    with (
        patch("nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()) as run_mock,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve_mock,
    ):
        result = await tool.execute(
            server_id_or_name="ansible-group-only", command="uptime", dry_run=False
        )

    assert result.is_error
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_timeout_marks_job_timed_out_not_failed(tmp_path: Path) -> None:
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    timeout_result = ExecutionResult(
        exit_code=None, output="", error="Idle/absolute timeout exceeded", timed_out=True
    )

    async def _fake_run_with_idle_timeout(coro, tracker):  # noqa: ANN001, ARG001
        coro.close()
        return timeout_result

    with (
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()),
        patch(
            "nanoinfra.agent.tools.server_execution.run_with_idle_timeout",
            new=_fake_run_with_idle_timeout,
        ),
    ):
        result = await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)

    assert result.is_error
    jobs = JobStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "timed_out"


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


def test_backend_classes_are_not_imported_at_module_level() -> None:
    """Static guard, same spirit as tests/secrets/test_no_plaintext_leak_invariant.py:
    all four backend classes must stay lazily imported inside their own
    `if provider_id == "..."` branch in _backend_and_default_timeout, never hoisted
    to the top of this module. asyncssh/ansible_runner/boto3 all happen to be
    installed in this dev environment, so nothing else in the suite would catch an
    accidental top-level import regressing this.
    """
    import nanoinfra.agent.tools.server_execution as module

    forbidden_names = {"SSHBackend", "AnsibleRunnerBackend", "SSMBackend", "ApiBackend"}
    assert not (forbidden_names & set(vars(module)))
