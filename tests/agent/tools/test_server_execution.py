# tests/agent/tools/test_server_execution.py
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.agent.tools import server_execution
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import ExecuteOnServerTool
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import MAX_OUTPUT_CHARS, ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore
from nanoinfra.servers.types import Server


@pytest.fixture(autouse=True)
def _interactive_turn():
    """Bind an interactive turn so the #8 gate does not refuse these tests.

    With no request context bound, execution_context falls back to unattended (#5), and #8
    refuses an unattended remote action without a standing grant. That refusal is correct.
    These tests exercise execution mechanics rather than policy, so they declare a present
    operator. Policy itself is covered by tests/agent/tools/test_unattended_enforcement.py.
    """
    ctx = RequestContext(
        channel="telegram",
        chat_id="c1",
        session_key="s1",
        execution_context=EXECUTION_CONTEXT_INTERACTIVE,
    )
    with request_context(ctx):
        yield

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
async def test_dry_run_surfaces_a_blocked_target_instead_of_inviting_confirmation(
    tmp_path: Path,
) -> None:
    """A preview used to return a clean "call again with dry_run=false" message for a
    metadata-pointed server, hiding the refusal that call was always going to get."""
    server_store = ServerStore(tmp_path)
    server_store.create(
        {"name": "metadata-server", "providerId": "ssh", "config": {"host": "169.254.169.254"}}
    )
    tool = _tool(tmp_path)

    with (
        patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run_mock,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve_mock,
    ):
        result = await tool.execute(server_id_or_name="metadata-server", command="uptime")

    assert result.is_error
    assert "dry_run=false" not in str(result)
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_dry_run_surfaces_an_unknown_provider(tmp_path: Path) -> None:
    # ServerStore.create() rejects unknown providerIds, so this state can only come
    # from a hand-edited or downgrade-written store file -- which is exactly why the
    # tool keeps its own defensive check, and why the preview should show it.
    tool = _tool(tmp_path)
    stored = Server(
        id="a" * 32,
        name="weird",
        provider_id="carrier-pigeon",
        config={"host": "10.0.1.5"},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )

    with patch.object(server_execution, "resolve_server", return_value=stored):
        result = await tool.execute(server_id_or_name="weird", command="uptime")

    assert result.is_error
    assert "carrier-pigeon" in str(result)


@pytest.mark.asyncio
async def test_backend_is_resolved_before_the_secret_is_decrypted(tmp_path: Path) -> None:
    """A provider whose optional library isn't installed must fail before a
    credential is decrypted, not after."""
    secret_store = SecretStore(tmp_path)
    secret = secret_store.create(
        {"name": "web-key", "kind": "ssh_key", "providerId": "local", "value": "s3cr3t-key-material"}
    )
    server_store = ServerStore(tmp_path)
    server_store.create(
        {
            "name": "prod-web-01",
            "providerId": "ssh",
            "config": {"host": "10.0.1.5"},
            "secretRef": secret.id,
        }
    )
    tool = _tool(tmp_path)

    with (
        patch.object(
            server_execution,
            "_backend_and_default_timeout",
            side_effect=ImportError("No module named 'asyncssh'"),
        ),
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve_mock,
        pytest.raises(ImportError),
    ):
        await tool.execute(server_id_or_name="prod-web-01", command="uptime", dry_run=False)

    resolve_mock.assert_not_called()
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
async def test_ansible_runner_undocumented_host_key_does_not_satisfy_the_guard(tmp_path: Path) -> None:
    """AnsibleRunnerBackend never reads `host` -- it targets inventoryHost/group. A
    config that pairs a group with a host-shaped extra key used to let the guard
    validate 8.8.8.8 while the backend targeted the `web` group, re-opening the
    group-only bypass. The guard must only ever consider inventoryHost here, so
    this config has nothing checkable and must be refused.
    """
    server_store = ServerStore(tmp_path)
    server_store.create(
        {
            "name": "ansible-group-plus-host",
            "providerId": "ansible-runner",
            "config": {"group": "web", "host": "8.8.8.8"},
        }
    )
    tool = _tool(tmp_path)

    with (
        patch("nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()) as run_mock,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve_mock,
    ):
        result = await tool.execute(
            server_id_or_name="ansible-group-plus-host", command="uptime", dry_run=False
        )

    assert result.is_error
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_ansible_runner_host_only_config_refuses_instead_of_targeting_everything(
    tmp_path: Path,
) -> None:
    """With only `host` set, the guard used to validate 10.0.1.5 while the backend's
    own fallback chain skipped straight to "all" -- running against the entire
    inventory under a job record naming one server."""
    server_store = ServerStore(tmp_path)
    server_store.create(
        {"name": "ansible-host-only", "providerId": "ansible-runner", "config": {"host": "10.0.1.5"}}
    )
    tool = _tool(tmp_path)

    with patch(
        "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run", new=AsyncMock()
    ) as run_mock:
        result = await tool.execute(server_id_or_name="ansible-host-only", command="uptime", dry_run=False)

    assert result.is_error
    run_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_ssh_inventory_host_only_config_refuses(tmp_path: Path) -> None:
    """SSHBackend only ever dials `host`; the guard used to fall back to
    inventoryHost, validating a field the backend ignores (it would then have
    connected to host="" instead)."""
    server_store = ServerStore(tmp_path)
    server_store.create(
        {"name": "ssh-inventory-only", "providerId": "ssh", "config": {"inventoryHost": "10.0.1.5"}}
    )
    tool = _tool(tmp_path)

    with patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=AsyncMock()) as run_mock:
        result = await tool.execute(server_id_or_name="ssh-inventory-only", command="uptime", dry_run=False)

    assert result.is_error
    run_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_timeout_marks_job_timed_out_not_failed(tmp_path: Path) -> None:
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    timeout_result = ExecutionResult(
        exit_code=None, output="", error="Idle/absolute timeout exceeded", timed_out=True
    )

    async def _fake_run_with_idle_timeout(coro, tracker, **kwargs):  # noqa: ANN001, ANN003, ARG001
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
async def test_timed_out_job_keeps_the_output_streamed_before_the_timeout(tmp_path: Path) -> None:
    """The durable-job record used to persist output="" for a timed-out run, even
    though the backend had streamed real output through on_activity first."""
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    async def streams_then_hangs(_self, server, command, secret_value, *, on_activity):  # noqa: ANN001, ANN202, ARG001
        on_activity("progress-line-1\n")
        on_activity("progress-line-2\n")
        await asyncio.sleep(999)
        return ExecutionResult(exit_code=0, output="unreachable", error=None)

    with patch(
        "nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=streams_then_hangs
    ):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="tail -f log", timeout_s="1", dry_run=False
        )

    assert result.is_error
    assert "progress-line-1" in str(result)
    job = JobStore(tmp_path).list_jobs()[0]
    assert job.status == "timed_out"
    assert "progress-line-1\nprogress-line-2\n" in job.output


@pytest.mark.asyncio
async def test_streamed_output_is_checkpointed_to_the_job_file_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JobStore.update_output() exists so a crash mid-run leaves the most recent
    known output on disk; it previously had no callers at all, making that
    docstring's durability claim false."""
    monkeypatch.setattr(server_execution, "_PARTIAL_OUTPUT_PERSIST_INTERVAL_S", 0.0)
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    on_disk_mid_run: list[str] = []

    async def streams_then_checks(_self, server, command, secret_value, *, on_activity):  # noqa: ANN001, ANN202, ARG001
        on_activity("streamed-chunk\n")
        on_disk_mid_run.append(JobStore(tmp_path).list_jobs()[0].output)
        return ExecutionResult(exit_code=0, output="streamed-chunk\n", error=None)

    with patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=streams_then_checks):
        await tool.execute(server_id_or_name="prod-web-01", command="tail -f log", dry_run=False)

    assert on_disk_mid_run == ["streamed-chunk\n"]


@pytest.mark.asyncio
async def test_timeout_message_admits_it_cannot_stop_ansible_or_ssm_work(tmp_path: Path) -> None:
    """asyncio.to_thread cannot be cancelled: after a reported timeout the ansible
    play (or the already-sent SSM command) is still running. Saying otherwise
    invites a retry that puts two copies of the command in flight."""
    server_store = ServerStore(tmp_path)
    server_store.create(
        {"name": "ssm-box", "providerId": "ssm", "config": {"instanceId": "i-0123456789abcdef0"}}
    )
    tool = _tool(tmp_path)

    async def hangs(_self, server, command, secret_value, *, on_activity):  # noqa: ANN001, ANN202, ARG001
        await asyncio.sleep(999)
        return ExecutionResult(exit_code=0, output="unreachable", error=None)

    with patch("nanoinfra.servers.execution.ssm_backend.SSMBackend.run", new=hangs):
        result = await tool.execute(
            server_id_or_name="ssm-box", command="uptime", timeout_s="1", dry_run=False
        )

    assert result.is_error
    assert "may still be running" in str(result)
    job = JobStore(tmp_path).list_jobs()[0]
    assert job.error is not None and "may still be running" in job.error


@pytest.mark.asyncio
async def test_ssh_timeout_message_does_not_hedge(tmp_path: Path) -> None:
    """SSH's connection really is torn down by cancellation, so its message must not
    inherit the ansible/ssm caveat."""
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    async def hangs(_self, server, command, secret_value, *, on_activity):  # noqa: ANN001, ANN202, ARG001
        await asyncio.sleep(999)
        return ExecutionResult(exit_code=0, output="unreachable", error=None)

    with patch("nanoinfra.servers.execution.ssh_backend.SSHBackend.run", new=hangs):
        result = await tool.execute(
            server_id_or_name="prod-web-01", command="sleep 999", timeout_s="1", dry_run=False
        )

    assert result.is_error
    assert "may still be running" not in str(result)


@pytest.mark.asyncio
async def test_oversized_output_is_truncated_in_both_the_result_and_the_job_file(
    tmp_path: Path,
) -> None:
    """Nothing in the chain capped output before: a chatty remote command's entire
    output went into the persisted ServerJob JSON and the model's context."""
    server_store = ServerStore(tmp_path)
    server_store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    tool = _tool(tmp_path)

    huge = "x" * (MAX_OUTPUT_CHARS + 5_000)
    fake_result = ExecutionResult(exit_code=0, output=huge, error=None)
    with patch(
        "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
        new=AsyncMock(return_value=fake_result),
    ):
        result = await tool.execute(server_id_or_name="prod-web-01", command="cat big", dry_run=False)

    assert "(5,000 chars truncated from output)" in result
    assert len(result) < MAX_OUTPUT_CHARS + 500

    job = JobStore(tmp_path).list_jobs()[0]
    assert len(job.output) < MAX_OUTPUT_CHARS + 200
    assert "(5,000 chars truncated from output)" in job.output


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
