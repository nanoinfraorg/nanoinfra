# tests/servers/execution/test_ssh_backend.py
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanoinfra.servers.execution.ssh_backend import SSHBackend
from nanoinfra.servers.types import Server


def _server(**config: str) -> Server:
    return Server(
        id="a" * 32,
        name="test-server",
        provider_id="ssh",
        config={"host": "10.0.1.5", "port": "22", "username": "deploy", **config},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


class _FakeStreamReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_run_returns_exit_code_and_output_and_reports_activity():
    fake_process = MagicMock()
    fake_process.stdout = _FakeStreamReader([b"hello\n", b"world\n", b""])
    fake_process.stderr = _FakeStreamReader([b""])
    fake_process.wait = AsyncMock(return_value=MagicMock(exit_status=0))
    fake_process.__aenter__ = AsyncMock(return_value=fake_process)
    fake_process.__aexit__ = AsyncMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.create_process = AsyncMock(return_value=fake_process)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    activity_chunks: list[str] = []

    with patch("asyncssh.connect", AsyncMock(return_value=fake_conn)):
        backend = SSHBackend()
        result = await backend.run(_server(), "echo hi", None, on_activity=activity_chunks.append)

    assert result.exit_code == 0
    assert "hello" in result.output
    assert "world" in result.output
    assert activity_chunks == ["hello\n", "world\n"]


@pytest.mark.asyncio
async def test_run_passes_secret_value_as_password_when_no_key_markers():
    fake_process = MagicMock()
    fake_process.stdout = _FakeStreamReader([b""])
    fake_process.stderr = _FakeStreamReader([b""])
    fake_process.wait = AsyncMock(return_value=MagicMock(exit_status=0))
    fake_process.__aenter__ = AsyncMock(return_value=fake_process)
    fake_process.__aexit__ = AsyncMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.create_process = AsyncMock(return_value=fake_process)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    connect_mock = AsyncMock(return_value=fake_conn)
    with patch("asyncssh.connect", connect_mock):
        backend = SSHBackend()
        result = await backend.run(
            _server(), "echo hi", "s3cr3t-password", on_activity=lambda _c: None
        )

    assert result.exit_code == 0
    assert result.error is None
    _, kwargs = connect_mock.call_args
    assert kwargs.get("password") == "s3cr3t-password"
    assert "client_keys" not in kwargs or not kwargs["client_keys"]


@pytest.mark.asyncio
async def test_run_passes_secret_value_as_private_key_when_pem_shaped():
    pem_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    fake_process = MagicMock()
    fake_process.stdout = _FakeStreamReader([b""])
    fake_process.stderr = _FakeStreamReader([b""])
    fake_process.wait = AsyncMock(return_value=MagicMock(exit_status=0))
    fake_process.__aenter__ = AsyncMock(return_value=fake_process)
    fake_process.__aexit__ = AsyncMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.create_process = AsyncMock(return_value=fake_process)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    connect_mock = AsyncMock(return_value=fake_conn)
    with patch("asyncssh.connect", connect_mock), patch("asyncssh.import_private_key") as import_key:
        import_key.return_value = "parsed-key-object"
        backend = SSHBackend()
        result = await backend.run(_server(), "echo hi", pem_key, on_activity=lambda _c: None)

    assert result.exit_code == 0
    assert result.error is None
    import_key.assert_called_once_with(pem_key)
    _, kwargs = connect_mock.call_args
    assert kwargs.get("client_keys") == ["parsed-key-object"]
    assert kwargs.get("password") is None


class _BlockedUntilReader:
    """stdout that cannot make progress until stderr has been consumed.

    This is the mock-level analogue of asyncssh's real flow control: stdout and
    stderr share one channel receive window, so a peer with unread stderr
    buffered stops sending stdout altogether. A backend that drains stdout to
    EOF *before* touching stderr therefore waits forever on a stream the peer
    will never advance.
    """

    def __init__(self, chunks: list[bytes], gate: asyncio.Event) -> None:
        self._chunks = list(chunks)
        self._gate = gate

    async def read(self, _n: int) -> bytes:
        await self._gate.wait()
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _OpensTheGateReader:
    """stderr whose chunks, once read, unblock stdout."""

    def __init__(self, chunks: list[bytes], gate: asyncio.Event) -> None:
        self._chunks = list(chunks)
        self._gate = gate

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        self._gate.set()
        return b""


@pytest.mark.asyncio
async def test_stdout_and_stderr_are_drained_concurrently_not_sequentially():
    """Regression test for a real deadlock: draining stdout to EOF and only then
    stderr hangs forever when the remote can't advance stdout until its buffered
    stderr is consumed. With concurrent draining, both complete. The wait_for is
    what makes the failure a fast failure instead of a hung test run.
    """
    gate = asyncio.Event()

    fake_process = MagicMock()
    fake_process.stdout = _BlockedUntilReader([b"late-stdout\n"], gate)
    fake_process.stderr = _OpensTheGateReader([b"early-stderr\n"], gate)
    fake_process.wait = AsyncMock(return_value=MagicMock(exit_status=0))
    fake_process.__aenter__ = AsyncMock(return_value=fake_process)
    fake_process.__aexit__ = AsyncMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.create_process = AsyncMock(return_value=fake_process)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    with patch("asyncssh.connect", AsyncMock(return_value=fake_conn)):
        backend = SSHBackend()
        result = await asyncio.wait_for(
            backend.run(_server(), "noisy-command", None, on_activity=lambda _c: None),
            timeout=5,
        )

    assert result.exit_code == 0
    assert "late-stdout" in result.output
    assert "early-stderr" in result.output


@pytest.mark.asyncio
async def test_connection_error_is_reported_not_raised():
    with patch("asyncssh.connect", AsyncMock(side_effect=OSError("connection refused"))):
        backend = SSHBackend()
        result = await backend.run(_server(), "echo hi", None, on_activity=lambda _c: None)

    assert result.exit_code is None
    assert result.error is not None
    assert "connection refused" in result.error


@pytest.mark.asyncio
async def test_connect_forces_binary_mode_so_decode_matches_asyncssh_streams():
    """Regression test for a real bug found via a live SSH connection (not
    caught by any mock, since fixtures here always returned bytes to match
    the code's assumption): asyncssh defaults create_process() streams to
    encoding="utf-8" (text mode), so process.stdout/stderr.read() returns
    str -- and _drain()'s `chunk.decode(...)` then raises
    AttributeError: 'str' object has no attribute 'decode'. The backend
    must explicitly request encoding=None (binary mode) so its own
    decode(errors="replace") is what actually runs, not asyncssh's
    stricter default."""
    fake_process = MagicMock()
    fake_process.stdout = _FakeStreamReader([b""])
    fake_process.stderr = _FakeStreamReader([b""])
    fake_process.wait = AsyncMock(return_value=MagicMock(exit_status=0))
    fake_process.__aenter__ = AsyncMock(return_value=fake_process)
    fake_process.__aexit__ = AsyncMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.create_process = AsyncMock(return_value=fake_process)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    connect_mock = AsyncMock(return_value=fake_conn)
    with patch("asyncssh.connect", connect_mock):
        backend = SSHBackend()
        result = await backend.run(_server(), "echo hi", None, on_activity=lambda _c: None)

    assert result.error is None
    _, kwargs = connect_mock.call_args
    assert kwargs.get("encoding") is None
