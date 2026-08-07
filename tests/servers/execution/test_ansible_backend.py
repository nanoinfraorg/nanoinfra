# tests/servers/execution/test_ansible_backend.py
from __future__ import annotations

from unittest.mock import patch

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


class _FakeStdoutFile:
    """A single opened handle onto a fake runner's stdout, tracking close()
    and optionally raising on read() -- mirrors the plain file object the
    real ``Runner.stdout`` property hands back on each access."""

    def __init__(self, text: str = "", raise_on_read: Exception | None = None) -> None:
        self._text = text
        self._raise_on_read = raise_on_read
        self.closed = False

    def read(self) -> str:
        if self._raise_on_read is not None:
            raise self._raise_on_read
        return self._text

    def close(self) -> None:
        self.closed = True


class _FakeRunner:
    """Stands in for ``ansible_runner.runner.Runner`` closely enough to
    catch the regression a plain ``MagicMock`` attribute can't: the real
    ``Runner.stdout`` is a ``@property`` that opens a *fresh* file object
    on every access and raises (rather than returning something falsy) if
    the underlying stdout file doesn't exist. This fake reproduces both:
    ``stdout`` is a real property, it records how many times it was
    accessed and every file object it handed out, and it can be told to
    raise either on access (simulating ``AnsibleRunnerException("stdout
    missing")``) or on read (simulating a mid-read failure) so tests can
    assert the backend accesses it exactly once and always closes what it
    opens, even on the error paths.
    """

    def __init__(
        self,
        *,
        rc: int,
        stdout_text: str = "",
        raise_on_stdout_access: Exception | None = None,
        raise_on_stdout_read: Exception | None = None,
    ) -> None:
        self.rc = rc
        self.status = "successful" if rc == 0 else "failed"
        self._stdout_text = stdout_text
        self._raise_on_stdout_access = raise_on_stdout_access
        self._raise_on_stdout_read = raise_on_stdout_read
        self.stdout_access_count = 0
        self.opened_files: list[_FakeStdoutFile] = []

    @property
    def stdout(self) -> _FakeStdoutFile:
        self.stdout_access_count += 1
        if self._raise_on_stdout_access is not None:
            raise self._raise_on_stdout_access
        opened = _FakeStdoutFile(self._stdout_text, raise_on_read=self._raise_on_stdout_read)
        self.opened_files.append(opened)
        return opened


def _fake_runner(*, rc: int, stdout_text: str) -> _FakeRunner:
    return _FakeRunner(rc=rc, stdout_text=stdout_text)


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


@pytest.mark.asyncio
async def test_run_reads_stdout_property_exactly_once_and_closes_it():
    # Regression test: the plan brief's original code was
    # `runner.stdout.read() if runner.stdout else ""`, which accesses the
    # real (property-backed) `stdout` twice -- once for the truthiness
    # check, once for `.read()` -- opening and leaking a second file
    # handle. A plain MagicMock attribute can't detect this because
    # repeated access just returns the same cached mock. `_FakeRunner`
    # can, because its `stdout` is a real property that hands out a new
    # object each time it's accessed.
    fake_runner = _FakeRunner(rc=0, stdout_text="ok")

    with patch("ansible_runner.run", return_value=fake_runner):
        backend = AnsibleRunnerBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None)

    assert result.output == "ok"
    assert fake_runner.stdout_access_count == 1
    assert len(fake_runner.opened_files) == 1
    assert fake_runner.opened_files[0].closed is True


@pytest.mark.asyncio
async def test_missing_stdout_reported_without_raising():
    # Simulates the real Runner.stdout property's behavior when the
    # artifact dir's stdout file doesn't exist: it raises
    # AnsibleRunnerException("stdout missing") rather than returning
    # something falsy. The backend must report this via ExecutionResult,
    # not let it propagate out of run().
    fake_runner = _FakeRunner(rc=0, raise_on_stdout_access=Exception("stdout missing"))

    with patch("ansible_runner.run", return_value=fake_runner):
        backend = AnsibleRunnerBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None)

    assert result.error is not None
    assert "stdout missing" in result.error
    assert fake_runner.stdout_access_count == 1
    assert fake_runner.opened_files == []


@pytest.mark.asyncio
async def test_stdout_file_closed_even_when_read_fails():
    fake_runner = _FakeRunner(rc=0, raise_on_stdout_read=OSError("boom"))

    with patch("ansible_runner.run", return_value=fake_runner):
        backend = AnsibleRunnerBackend()
        result = await backend.run(_server(), "uptime", None, on_activity=lambda _c: None)

    assert result.error is not None
    assert "boom" in result.error
    assert len(fake_runner.opened_files) == 1
    assert fake_runner.opened_files[0].closed is True
