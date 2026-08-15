# tests/gates/test_executor_execution.py
"""Execution mechanics, migrated from the tool to the executor -- nanoinfraorg/nanoinfra#18.

Every property here used to live in ``tests/agent/tools/test_server_execution.py``, because the
tool held the transports, the credential store, the target guard, and the job store. #18 moved
all four into ``nanoinfra/gates/executor/server.py``, so the properties move with them.

The docstrings below record real past bugs. Each one names the defect the assertion catches. A
rename must not lose that reasoning, because the reason is why the test exists.

Two orders matter, and several tests exist only to pin them:

- A refusal decrypts no credential. The guard answers before ``SecretStore`` resolves anything.
- A refusal writes no job record. The record follows the decision, and it never precedes it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor import server as executor_module
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import MAX_OUTPUT_CHARS, ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore
from nanoinfra.servers.types import Server

_SSH_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_ANSIBLE_BACKEND = "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run"
_SSM_BACKEND = "nanoinfra.servers.execution.ssm_backend.SSMBackend.run"
_RESOLVE_PLAINTEXT = "nanoinfra.secrets.store.SecretStore.resolve_plaintext"

# The caveat the executor adds for a provider it cannot stop. Pinned as a constant, because two
# tests need it: one demands the words, and one demands their absence.
_UNSTOPPABLE_CAVEAT = "may still run"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _executor(tmp_path: Path, gates: GatesConfig | None = None) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=lambda: gates or GatesConfig())


def _request(**over: object) -> ExecuteRequest:
    """One request with an interactive context, so #8 does not refuse these tests.

    An unattended context needs a standing grant, and that refusal is correct. These tests
    exercise execution mechanics rather than policy, so they declare a present operator. Policy
    lives in tests/agent/tools/test_unattended_enforcement.py.
    """
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _ssh_server(tmp_path: Path, **over: Any) -> None:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
    }
    raw.update(over)
    ServerStore(tmp_path).create(raw)


def _secret(tmp_path: Path, name: str, value: str) -> str:
    secret = SecretStore(tmp_path).create(
        {"name": name, "kind": "ssh_key", "providerId": "local", "value": value}
    )
    return secret.id


async def _hangs(_self: Any, *_args: Any, **_kwargs: Any) -> ExecutionResult:
    """A backend that never returns. The idle timeout must end the wait."""
    await asyncio.sleep(999)
    return ExecutionResult(exit_code=0, output="unreachable", error=None)


# --------------------------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_preview_reaches_no_backend_and_writes_no_job_record(tmp_path: Path) -> None:
    """A preview connects to nothing, so it must also leave no execution record behind.

    A job record for a preview would read as an execution in the history.
    """
    _ssh_server(tmp_path)

    with patch(_SSH_BACKEND, new=AsyncMock()) as run_mock:
        response = await _executor(tmp_path).handle(_request(preview_requested=True))

    assert response.ok
    assert "Preview (not executed)" in response.output
    assert "prod-web-01" in response.output
    run_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_preview_surfaces_a_blocked_target_instead_of_an_invitation(
    tmp_path: Path,
) -> None:
    """A preview for a metadata-pointed server once returned a clean "call again" message.

    That message hid the refusal the second call was always going to get. The guard therefore
    answers ahead of the preview, and the preview reports the refusal.
    """
    ServerStore(tmp_path).create(
        {"name": "metadata-server", "providerId": "ssh", "config": {"host": "169.254.169.254"}}
    )

    with (
        patch(_SSH_BACKEND, new=AsyncMock()) as run_mock,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve_mock,
    ):
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="metadata-server", preview_requested=True)
        )

    assert not response.ok
    assert "Refusing to execute" in str(response.error)
    assert "Preview (not executed)" not in response.output
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_preview_surfaces_an_unknown_provider(tmp_path: Path) -> None:
    """ServerStore.create() refuses an unknown providerId, so only a hand-edited store file
    holds this state. The executor keeps its own defensive check for that case, and a preview
    must show the problem rather than describe an action nothing can run.
    """
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

    with patch.object(executor_module, "resolve_server", return_value=stored):
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="weird", preview_requested=True)
        )

    assert not response.ok
    assert "carrier-pigeon" in str(response.error)


# --------------------------------------------------------------------------------------------
# Order: a refusal decrypts nothing and records nothing
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_backend_resolves_before_the_credential_decrypts(tmp_path: Path) -> None:
    """A provider whose optional library is absent must fail ahead of the decryption.

    A failure after the decryption puts a plaintext in memory for a call that never dials a
    host.
    """
    secret_id = _secret(tmp_path, "web-key", "s3cr3t-key-material")
    _ssh_server(tmp_path, secretRef=secret_id)

    with (
        patch.object(
            executor_module,
            "_backend_for",
            side_effect=ImportError("No module named 'asyncssh'"),
        ),
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve_mock,
        pytest.raises(ImportError),
    ):
        await _executor(tmp_path).handle(_request())

    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_blocked_target_refuses_before_the_credential_decrypts(tmp_path: Path) -> None:
    """secretRef is set on purpose here, unlike in a minimal repro.

    The test then proves the blocked-target refusal happens ahead of any decryption, and not
    merely ahead of the backend call and the job record.
    """
    secret_id = _secret(tmp_path, "metadata-key", "s3cr3t-metadata")
    ServerStore(tmp_path).create(
        {
            "name": "metadata-server",
            "providerId": "ssh",
            "config": {"host": "169.254.169.254"},
            "secretRef": secret_id,
        }
    )

    with (
        patch(_SSH_BACKEND, new=AsyncMock()) as run_mock,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve_mock,
    ):
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="metadata-server")
        )

    assert not response.ok
    assert response.error
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_an_ansible_group_only_config_refuses_before_the_credential_decrypts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ansible-runner server with only ``group`` names no address the guard can dial.

    Critical finding from code review: this once fell straight through to the decryption and to
    AnsibleRunnerBackend.run() with no guard at all. #9 now expands the pattern and checks every
    host it names. An inventory the resolver cannot read leaves the host set unknown, so the
    guard refuses. It never reads "nothing to validate" as "proceed unguarded".
    """
    monkeypatch.chdir(tmp_path)
    secret_id = _secret(tmp_path, "ansible-key", "s3cr3t-ansible")
    ServerStore(tmp_path).create(
        {
            "name": "ansible-group-only",
            "providerId": "ansible-runner",
            "config": {"group": "web"},
            "secretRef": secret_id,
        }
    )

    with (
        patch(_ANSIBLE_BACKEND, new=AsyncMock()) as run_mock,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve_mock,
    ):
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="ansible-group-only")
        )

    assert not response.ok
    assert "Cannot validate network target" in str(response.error)
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_an_undocumented_ansible_host_key_never_satisfies_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AnsibleRunnerBackend never reads ``host``. It targets inventoryHost or group.

    A config that pairs a group with a host-shaped extra key once let the guard validate
    8.8.8.8 while the backend targeted the ``web`` group. That re-opened the group-only bypass.
    The guard must consider inventoryHost and the expanded group only, so this config has no
    checkable address and it gets a refusal.
    """
    monkeypatch.chdir(tmp_path)
    ServerStore(tmp_path).create(
        {
            "name": "ansible-group-plus-host",
            "providerId": "ansible-runner",
            "config": {"group": "web", "host": "8.8.8.8"},
        }
    )
    checked: list[str] = []

    def recording_guard(host: str) -> tuple[bool, str]:
        checked.append(host)
        return True, ""

    with (
        patch.object(executor_module, "validate_server_target", new=recording_guard),
        patch(_ANSIBLE_BACKEND, new=AsyncMock()) as run_mock,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve_mock,
    ):
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="ansible-group-plus-host")
        )

    assert not response.ok
    assert "8.8.8.8" not in checked
    run_mock.assert_not_called()
    resolve_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_an_ansible_host_only_config_refuses_instead_of_targeting_everything(
    tmp_path: Path,
) -> None:
    """With only ``host`` set, the guard once validated 10.0.1.5.

    The backend's own fallback chain then skipped to "all" and ran against the whole inventory,
    under a job record that named one server.
    """
    ServerStore(tmp_path).create(
        {
            "name": "ansible-host-only",
            "providerId": "ansible-runner",
            "config": {"host": "10.0.1.5"},
        }
    )

    with patch(_ANSIBLE_BACKEND, new=AsyncMock()) as run_mock:
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="ansible-host-only")
        )

    assert not response.ok
    assert "Cannot validate network target" in str(response.error)
    run_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_an_ssh_inventory_host_only_config_refuses(tmp_path: Path) -> None:
    """SSHBackend dials ``host`` and nothing else.

    The guard once fell back to inventoryHost, so it validated a field the backend ignores. The
    backend would then have connected to host="".
    """
    ServerStore(tmp_path).create(
        {
            "name": "ssh-inventory-only",
            "providerId": "ssh",
            "config": {"inventoryHost": "10.0.1.5"},
        }
    )

    with patch(_SSH_BACKEND, new=AsyncMock()) as run_mock:
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="ssh-inventory-only")
        )

    assert not response.ok
    assert "Cannot validate network target" in str(response.error)
    run_mock.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_an_unknown_server_refuses_and_writes_no_job_record(tmp_path: Path) -> None:
    """A name that matches nothing is a refusal, and a refusal records no execution."""
    response = await _executor(tmp_path).handle(_request(server_id_or_name="ghost"))

    assert not response.ok
    assert response.error
    assert JobStore(tmp_path).list_jobs() == []


# --------------------------------------------------------------------------------------------
# Job record states
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_creates_a_completed_job_and_calls_the_matching_backend(
    tmp_path: Path,
) -> None:
    """The record carries the terminal state and the exit code, not only the fact of a run."""
    _ssh_server(tmp_path)
    fake = ExecutionResult(exit_code=0, output="up 3 days", error=None)

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=fake)) as run_mock:
        response = await _executor(tmp_path).handle(_request())

    assert "up 3 days" in response.output
    run_mock.assert_called_once()
    jobs = JobStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert jobs[0].exit_code == 0


@pytest.mark.asyncio
async def test_a_backend_failure_marks_the_job_failed_and_not_completed(tmp_path: Path) -> None:
    """A failed connection is a terminal state of its own. "completed" would read as success."""
    _ssh_server(tmp_path)
    fake = ExecutionResult(exit_code=None, output="", error="connection refused")

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=fake)):
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    assert "connection refused" in str(response.error)
    jobs = JobStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"


@pytest.mark.asyncio
async def test_a_timeout_marks_the_job_timed_out_and_not_failed(tmp_path: Path) -> None:
    """A timeout and a failure are different facts, and the record keeps them apart.

    A timeout on some providers leaves the remote work in flight. "failed" would hide that.
    """
    _ssh_server(tmp_path)
    timeout_result = ExecutionResult(
        exit_code=None, output="", error="Idle/absolute timeout exceeded", timed_out=True
    )

    async def fake_run_with_idle_timeout(coro: Any, _tracker: Any, **_kw: Any) -> ExecutionResult:
        coro.close()
        return timeout_result

    with (
        patch(_SSH_BACKEND, new=AsyncMock()),
        patch.object(executor_module, "run_with_idle_timeout", new=fake_run_with_idle_timeout),
    ):
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    jobs = JobStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "timed_out"


# --------------------------------------------------------------------------------------------
# Partial output and truncation
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timed_out_job_keeps_the_output_streamed_before_the_timeout(
    tmp_path: Path,
) -> None:
    """The durable record once persisted output="" for a timed-out run.

    The backend had already streamed real output through on_activity, and the cancellation threw
    it away. That output is exactly what an operator needs after a hang.
    """
    _ssh_server(tmp_path)

    async def streams_then_hangs(
        _self: Any, _server: Any, _command: str, _secret: str | None, *, on_activity: Any
    ) -> ExecutionResult:
        on_activity("progress-line-1\n")
        on_activity("progress-line-2\n")
        await asyncio.sleep(999)
        return ExecutionResult(exit_code=0, output="unreachable", error=None)

    with patch(_SSH_BACKEND, new=streams_then_hangs):
        response = await _executor(tmp_path).handle(
            _request(command="tail -f log", timeout_s="1")
        )

    assert not response.ok
    assert "progress-line-1" in response.output
    job = JobStore(tmp_path).list_jobs()[0]
    assert job.status == "timed_out"
    assert "progress-line-1\nprogress-line-2\n" in job.output


@pytest.mark.asyncio
async def test_streamed_output_is_checkpointed_to_the_job_file_while_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JobStore.update_output() exists so a crash mid-run leaves the latest output on disk.

    It once had no caller at all, which made that durability claim false.
    """
    monkeypatch.setattr(executor_module, "_PARTIAL_OUTPUT_PERSIST_INTERVAL_S", 0.0)
    _ssh_server(tmp_path)
    on_disk_mid_run: list[str] = []

    async def streams_then_checks(
        _self: Any, _server: Any, _command: str, _secret: str | None, *, on_activity: Any
    ) -> ExecutionResult:
        on_activity("streamed-chunk\n")
        on_disk_mid_run.append(JobStore(tmp_path).list_jobs()[0].output)
        return ExecutionResult(exit_code=0, output="streamed-chunk\n", error=None)

    with patch(_SSH_BACKEND, new=streams_then_checks):
        await _executor(tmp_path).handle(_request(command="tail -f log"))

    assert on_disk_mid_run == ["streamed-chunk\n"]


@pytest.mark.asyncio
async def test_oversized_output_is_truncated_in_both_the_response_and_the_job_file(
    tmp_path: Path,
) -> None:
    """Nothing in the chain capped output before.

    A chatty remote command put its whole output into the persisted ServerJob JSON and into the
    model's context. One cap covers both, so neither path can regress alone.
    """
    _ssh_server(tmp_path)
    huge = "x" * (MAX_OUTPUT_CHARS + 5_000)
    fake = ExecutionResult(exit_code=0, output=huge, error=None)

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=fake)):
        response = await _executor(tmp_path).handle(_request(command="cat big"))

    assert "(5,000 chars truncated from output)" in response.output
    assert len(response.output) < MAX_OUTPUT_CHARS + 500

    job = JobStore(tmp_path).list_jobs()[0]
    assert len(job.output) < MAX_OUTPUT_CHARS + 200
    assert "(5,000 chars truncated from output)" in job.output


# --------------------------------------------------------------------------------------------
# What a timeout may claim
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timeout_admits_it_cannot_stop_ansible_or_ssm_work(tmp_path: Path) -> None:
    """asyncio.to_thread cannot be cancelled.

    After a reported timeout the ansible play, or the already-sent SSM command, still runs. A
    message that claims otherwise invites a retry, and the retry puts two copies of the command
    in flight. The caveat reaches the response and the durable record both.
    """
    ServerStore(tmp_path).create(
        {"name": "ssm-box", "providerId": "ssm", "config": {"instanceId": "i-0123456789abcdef0"}}
    )

    with patch(_SSM_BACKEND, new=_hangs):
        response = await _executor(tmp_path).handle(
            _request(server_id_or_name="ssm-box", timeout_s="1")
        )

    assert not response.ok
    assert _UNSTOPPABLE_CAVEAT in str(response.error)
    job = JobStore(tmp_path).list_jobs()[0]
    assert job.status == "timed_out"
    assert job.error is not None and _UNSTOPPABLE_CAVEAT in job.error


@pytest.mark.asyncio
async def test_an_ssh_timeout_does_not_hedge(tmp_path: Path) -> None:
    """Cancellation really tears an ssh connection down, so it ends the remote command.

    The ssh message must not inherit the ansible and ssm caveat. A caveat everywhere means
    nothing anywhere.
    """
    _ssh_server(tmp_path)

    with patch(_SSH_BACKEND, new=_hangs):
        response = await _executor(tmp_path).handle(
            _request(command="sleep 999", timeout_s="1")
        )

    assert not response.ok
    assert _UNSTOPPABLE_CAVEAT not in str(response.error)


# --------------------------------------------------------------------------------------------
# Credential handling
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_credential_never_appears_in_the_response_or_the_job_file(
    tmp_path: Path,
) -> None:
    """The backend gets the decrypted value. Nothing else does.

    The response crosses the wire to the agent, and the job file persists to disk. A plaintext
    in either one undoes the split #18 exists for.
    """
    secret_id = _secret(tmp_path, "web-key", "s3cr3t-key-material")
    _ssh_server(tmp_path, secretRef=secret_id)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=fake)) as run_mock:
        response = await _executor(tmp_path).handle(_request())

    assert "s3cr3t-key-material" not in str(response)
    job = JobStore(tmp_path).list_jobs()[0]
    assert "s3cr3t-key-material" not in str(job.to_dict())

    _, kwargs = run_mock.call_args
    passed = (
        run_mock.call_args.args[2]
        if len(run_mock.call_args.args) > 2
        else kwargs.get("secret_value")
    )
    assert passed == "s3cr3t-key-material"


# --------------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------------


def test_backend_classes_are_not_imported_at_module_level() -> None:
    """A static guard, in the spirit of tests/secrets/test_no_plaintext_leak_invariant.py.

    All four backend classes must stay lazily imported inside their own branch of
    ``_backend_for``. A hoisted import makes each optional library mandatory. asyncssh,
    ansible_runner, and boto3 all happen to be installed in this dev environment, so nothing
    else in the suite would catch such a regression.
    """
    forbidden_names = {"SSHBackend", "AnsibleRunnerBackend", "SSMBackend", "ApiBackend"}

    assert not (forbidden_names & set(vars(executor_module)))
