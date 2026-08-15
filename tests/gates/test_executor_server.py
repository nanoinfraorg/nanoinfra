# tests/gates/test_executor_server.py
"""Item 15 (#18): the executor owns everything the agent must lose.

The credential store, the four transports, the target guard, the scope resolver, and the gate
all live on this side. The agent submits a structured request and renders the reply, so
compromising the agent yields the ability to ask rather than the ability to act.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import (
    ExecuteRequest,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)
from nanoinfra.gates.executor.server import Executor, serve_forever
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_SECRET = "s3cr3t-key-material"
_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _workspace(tmp_path: Path, *, with_secret: bool = False) -> Path:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
    }
    if with_secret:
        secret = SecretStore(tmp_path).create(
            {"name": "web-key", "kind": "ssh_key", "providerId": "local", "value": _SECRET}
        )
        raw["secretRef"] = secret.id
    ServerStore(tmp_path).create(raw)
    return tmp_path


def _executor(tmp_path: Path, gates: GatesConfig | None = None) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=lambda: gates or GatesConfig())


def _request(**over: object) -> ExecuteRequest:
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


@pytest.mark.asyncio
async def test_the_executor_runs_an_allowed_action(tmp_path: Path) -> None:
    _workspace(tmp_path)
    fake = ExecutionResult(exit_code=0, output="up 3 days", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)):
        response = await _executor(tmp_path).handle(_request())

    assert response.ok
    assert "up 3 days" in response.output
    assert response.exit_code == 0


@pytest.mark.asyncio
async def test_the_executor_refuses_an_unattended_action_without_a_grant(tmp_path: Path) -> None:
    """The gate lives here now. The agent cannot reach a transport by asking twice."""
    _workspace(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request(execution_context="automation"))

    assert not response.ok
    assert "grant" in response.reason
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_preview_reaches_no_backend(tmp_path: Path) -> None:
    _workspace(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request(preview_requested=True))

    assert response.ok
    assert "Preview" in response.output
    run.assert_not_called()


@pytest.mark.asyncio
async def test_the_response_never_carries_the_credential(tmp_path: Path) -> None:
    """The executor resolves the plaintext and keeps it. Only the backend sees the value."""
    _workspace(tmp_path, with_secret=True)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)) as run:
        response = await _executor(tmp_path).handle(_request())

    assert _SECRET not in str(response)
    passed = run.call_args.args[2] if len(run.call_args.args) > 2 else None
    assert passed == _SECRET


@pytest.mark.asyncio
async def test_a_blocked_target_is_refused_here(tmp_path: Path) -> None:
    """The target guard moved with the transports, so the agent cannot skip it."""
    ServerStore(tmp_path).create(
        {"name": "metadata", "providerId": "ssh", "config": {"host": "169.254.169.254"}}
    )

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request(server_id_or_name="metadata"))

    assert not response.ok
    run.assert_not_called()


@pytest.mark.asyncio
async def test_an_unknown_server_is_refused(tmp_path: Path) -> None:
    _workspace(tmp_path)

    response = await _executor(tmp_path).handle(_request(server_id_or_name="nope"))

    assert not response.ok
    assert "nope" in str(response.error)


@pytest.mark.asyncio
async def test_the_executor_writes_the_job_record(tmp_path: Path) -> None:
    """Job records follow the transports. The agent no longer writes them."""
    _workspace(tmp_path)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)):
        await _executor(tmp_path).handle(_request())

    assert len(JobStore(tmp_path).list_jobs()) == 1


def test_the_socket_serves_a_request_end_to_end(tmp_path: Path) -> None:
    """One real socket round trip, because the wire is the boundary under test."""
    _workspace(tmp_path)
    socket_path = tmp_path / "exec.sock"
    fake = ExecutionResult(exit_code=0, output="up 3 days", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)):
        thread = threading.Thread(
            target=serve_forever,
            kwargs={"socket_path": socket_path, "workspace": tmp_path, "max_requests": 1},
            daemon=True,
        )
        thread.start()
        _wait_for(socket_path)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            write_frame(client, encode_request(_request()))
            response = decode_response(read_frame(client))
        thread.join(timeout=10)

    assert response.ok
    assert "up 3 days" in response.output


def test_the_socket_directory_excludes_other_users(tmp_path: Path) -> None:
    """A socket's own mode is not honoured everywhere, so the directory carries the control."""
    _workspace(tmp_path)
    socket_dir = tmp_path / "run"
    socket_path = socket_dir / "exec.sock"

    thread = threading.Thread(
        target=serve_forever,
        kwargs={"socket_path": socket_path, "workspace": tmp_path, "max_requests": 1},
        daemon=True,
    )
    thread.start()
    _wait_for(socket_path)
    try:
        assert socket_dir.stat().st_mode & 0o077 == 0
    finally:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            write_frame(client, encode_request(_request(preview_requested=True)))
            read_frame(client)
        thread.join(timeout=10)


def test_a_malformed_frame_gets_a_refusal_and_not_a_crash(tmp_path: Path) -> None:
    """A peer that speaks nonsense must not take the executor down."""
    _workspace(tmp_path)
    socket_path = tmp_path / "exec.sock"

    thread = threading.Thread(
        target=serve_forever,
        kwargs={"socket_path": socket_path, "workspace": tmp_path, "max_requests": 1},
        daemon=True,
    )
    thread.start()
    _wait_for(socket_path)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        write_frame(client, b"not a request")
        response = decode_response(read_frame(client))
    thread.join(timeout=10)

    assert not response.ok
    assert response.error


def test_the_socket_is_removed_on_exit(tmp_path: Path) -> None:
    """A stale socket file blocks the next bind, so the server cleans up after itself."""
    _workspace(tmp_path)
    socket_path = tmp_path / "exec.sock"

    thread = threading.Thread(
        target=serve_forever,
        kwargs={"socket_path": socket_path, "workspace": tmp_path, "max_requests": 1},
        daemon=True,
    )
    thread.start()
    _wait_for(socket_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        write_frame(client, encode_request(_request(preview_requested=True)))
        read_frame(client)
    thread.join(timeout=10)

    assert not socket_path.exists()


def _wait_for(path: Path, timeout_s: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} never appeared")
