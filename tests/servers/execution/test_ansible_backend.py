# tests/servers/execution/test_ansible_backend.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nanoinfra.servers.execution.ansible_backend import AnsibleRunnerBackend
from nanoinfra.servers.types import Server


def _server() -> Server:
    return Server(
        id="a" * 32,
        name="test-server",
        provider_id="ansible-runner",
        config={"inventoryHost": "10.0.1.5", "group": "web", "projectPath": "/srv/ansible/project"},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


def _fake_runner(*, rc: int, stdout_text: str) -> MagicMock:
    runner = MagicMock()
    runner.rc = rc
    runner.status = "successful" if rc == 0 else "failed"
    stdout_file = MagicMock()
    stdout_file.read.return_value = stdout_text
    runner.stdout = stdout_file
    return runner


@pytest.mark.asyncio
async def test_run_invokes_ansible_runner_with_module_and_args():
    fake_runner = _fake_runner(rc=0, stdout_text="ok: [10.0.1.5]")

    with patch("ansible_runner.run", return_value=fake_runner) as run_mock:
        backend = AnsibleRunnerBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None)

    assert result.exit_code == 0
    assert "ok: [10.0.1.5]" in result.output
    _, kwargs = run_mock.call_args
    assert kwargs["module"] == "command"
    assert kwargs["module_args"] == "uptime"
    assert kwargs["host_pattern"] == "10.0.1.5"
    assert kwargs["private_data_dir"] == "/srv/ansible/project"


@pytest.mark.asyncio
async def test_run_uses_group_as_host_pattern_when_no_inventory_host():
    fake_runner = _fake_runner(rc=0, stdout_text="ok")
    server = Server(
        id="a" * 32, name="n", provider_id="ansible-runner",
        config={"group": "web", "projectPath": "/srv/ansible/project"},
        secret_ref=None, tags=[], created_at="t", updated_at="t",
    )

    with patch("ansible_runner.run", return_value=fake_runner) as run_mock:
        backend = AnsibleRunnerBackend()
        await backend.run(server, "uptime", None, on_activity=lambda _c: None)

    _, kwargs = run_mock.call_args
    assert kwargs["host_pattern"] == "web"


@pytest.mark.asyncio
async def test_run_passes_secret_value_as_ssh_key():
    fake_runner = _fake_runner(rc=0, stdout_text="ok")

    with patch("ansible_runner.run", return_value=fake_runner) as run_mock:
        backend = AnsibleRunnerBackend()
        await backend.run(_server(), "uptime", "-----BEGIN KEY-----", on_activity=lambda _c: None)

    _, kwargs = run_mock.call_args
    assert kwargs["ssh_key"] == "-----BEGIN KEY-----"


@pytest.mark.asyncio
async def test_nonzero_rc_reported_without_raising():
    fake_runner = _fake_runner(rc=2, stdout_text="fatal: [10.0.1.5]: FAILED!")

    with patch("ansible_runner.run", return_value=fake_runner):
        backend = AnsibleRunnerBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None)

    assert result.exit_code == 2
    assert "FAILED" in result.output


@pytest.mark.asyncio
async def test_exception_is_reported_not_raised():
    with patch("ansible_runner.run", side_effect=RuntimeError("boom")):
        backend = AnsibleRunnerBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None)

    assert result.exit_code is None
    assert "boom" in result.error
