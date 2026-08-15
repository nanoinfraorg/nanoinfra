# tests/gates/test_executor_client.py
"""Item 15 (#18): the agent side of the wire, and the property that makes the split real.

The tool submits a request and renders the reply. The strongest test here is not a behaviour
test: it is the import check. A tool that cannot import a backend cannot open a transport, and
that is checkable rather than merely intended.
"""

from __future__ import annotations

import ast
import socket
import threading
from pathlib import Path

import pytest

from nanoinfra.gates.executor.client import ExecutorClient, ExecutorUnavailable
from nanoinfra.gates.executor.protocol import (
    ExecuteResponse,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)

_TOOL = Path("nanoinfra/agent/tools/server_execution.py")

# What the agent must not be able to reach. A module that imports any of these holds the means
# to dial a host or to read a credential.
_FORBIDDEN_IMPORTS = (
    "nanoinfra.secrets.store",
    "nanoinfra.servers.execution.ssh_backend",
    "nanoinfra.servers.execution.ansible_backend",
    "nanoinfra.servers.execution.ssm_backend",
    "nanoinfra.servers.execution.api_backend",
    "nanoinfra.gates.executor.server",
)


def _imported_modules(path: Path) -> set[str]:
    """Every module name the file imports, at any depth, including inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_tool_imports_no_transport_and_no_secret_store() -> None:
    """The acceptance criterion of #18, as a check rather than a promise.

    A lazy import inside a function would satisfy a naive grep, so this walks the whole tree.
    """
    imported = _imported_modules(_TOOL)

    assert [name for name in _FORBIDDEN_IMPORTS if name in imported] == []


def test_the_tool_cannot_construct_a_backend() -> None:
    """The runtime half of the same property."""
    import nanoinfra.agent.tools.server_execution as tool

    for attribute in ("SSHBackend", "SecretStore", "AnsibleRunnerBackend", "Executor"):
        assert not hasattr(tool, attribute)


def test_the_client_carries_a_request_and_returns_the_reply(tmp_path: Path) -> None:
    socket_path = tmp_path / "exec.sock"
    replies = ExecuteResponse(ok=True, output="up 3 days", exit_code=0, error=None, reason="")
    received: list[str] = []

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            conn, _ = server.accept()
            with conn:
                request = decode_request(read_frame(conn))
                received.append(request.command)
                write_frame(conn, encode_response(replies))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for(socket_path)

    response = ExecutorClient(socket_path).execute(
        server_id_or_name="prod-web-01",
        command="uptime",
        session_id="s1",
        execution_context="interactive",
        preview_requested=False,
        timeout_s=None,
    )
    thread.join(timeout=10)

    assert received == ["uptime"]
    assert response.output == "up 3 days"


def test_a_missing_socket_raises_executor_unavailable(tmp_path: Path) -> None:
    """A caller must be able to tell "the executor is not there" from "the gate refused".

    Those two need different words for an operator, and conflating them would read as a policy
    decision when it is a deployment fault.
    """
    with pytest.raises(ExecutorUnavailable):
        ExecutorClient(tmp_path / "absent.sock").execute(
            server_id_or_name="prod-web-01",
            command="uptime",
            session_id="s1",
            execution_context="interactive",
            preview_requested=False,
            timeout_s=None,
        )


def test_a_socket_that_dies_mid_reply_raises_executor_unavailable(tmp_path: Path) -> None:
    socket_path = tmp_path / "exec.sock"

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            conn, _ = server.accept()
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for(socket_path)

    with pytest.raises(ExecutorUnavailable):
        ExecutorClient(socket_path).execute(
            server_id_or_name="prod-web-01",
            command="uptime",
            session_id="s1",
            execution_context="interactive",
            preview_requested=False,
            timeout_s=None,
        )
    thread.join(timeout=10)


def test_the_client_holds_no_credential_and_no_backend() -> None:
    """Structural, for the same reason as the tool check: the client is agent-side code."""
    imported = _imported_modules(Path("nanoinfra/gates/executor/client.py"))

    assert [name for name in _FORBIDDEN_IMPORTS if name in imported] == []


def _wait_for(path: Path, timeout_s: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} never appeared")
