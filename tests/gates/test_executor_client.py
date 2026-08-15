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
import time
from pathlib import Path

import pytest

from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.gates.executor.client import ExecutorClient, ExecutorUnavailableError
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

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                request = decode_request(read_frame(conn))
                received.append(request.command)
                write_frame(conn, encode_response(replies))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

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


def test_a_reply_that_arrives_long_after_the_connect_still_reaches_the_caller(
    tmp_path: Path,
) -> None:
    """#38 blocks this call while a human answers, so no read deadline may cut it short.

    The connect timeout guards the connect only. A deadline on the read would report a pending
    approval as an unreachable executor, and an operator would then read a policy question as a
    deployment fault.
    """
    socket_path = tmp_path / "exec.sock"
    reply = ExecuteResponse(ok=True, output="ran", exit_code=0, error=None, reason="")

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                read_frame(conn)
                time.sleep(0.4)
                write_frame(conn, encode_response(reply))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    response = ExecutorClient(socket_path, connect_timeout_s=0.05).execute(
        server_id_or_name="prod-web-01",
        command="uptime",
        session_id="s1",
        execution_context="interactive",
        preview_requested=False,
        timeout_s=None,
    )
    thread.join(timeout=10)

    assert response.output == "ran"


def test_a_missing_socket_raises_executor_unavailable(tmp_path: Path) -> None:
    """A caller must be able to tell "the executor is not there" from "the gate refused".

    Those two need different words for an operator, and conflating them would read as a policy
    decision when it is a deployment fault.
    """
    with pytest.raises(ExecutorUnavailableError):
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

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    with pytest.raises(ExecutorUnavailableError):
        ExecutorClient(socket_path).execute(
            server_id_or_name="prod-web-01",
            command="uptime",
            session_id="s1",
            execution_context="interactive",
            preview_requested=False,
            timeout_s=None,
        )
    thread.join(timeout=10)


def test_the_request_carries_the_channel_that_raised_it(tmp_path: Path) -> None:
    """#38: the origin path comes from the bound request context, and not from a tool argument.

    #13 refuses an approval that arrives on the origin path. A tool argument would let the model
    name any path it liked, so the value comes from the channel adapter instead.
    """
    socket_path = tmp_path / "exec.sock"
    seen: list[str | None] = []

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                seen.append(decode_request(read_frame(conn)).origin_path)
                write_frame(
                    conn,
                    encode_response(
                        ExecuteResponse(ok=True, output="", exit_code=0, error=None, reason="")
                    ),
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    context = RequestContext(channel="telegram", chat_id="c1", session_key="s1")
    with request_context(context):
        ExecutorClient(socket_path).execute(
            server_id_or_name="prod-web-01",
            command="uptime",
            session_id="s1",
            execution_context="interactive",
            preview_requested=False,
            timeout_s=None,
        )
    thread.join(timeout=10)

    assert seen == ["telegram"]


def test_the_request_carries_the_person_the_channel_authenticated(tmp_path: Path) -> None:
    """#47 item 10: the origin identity comes from the bound context, like the origin path.

    A tool argument would let the model name any person, and identity independence would then
    rest on a value the model wrote. The channel adapter authenticated the sender, so the
    adapter is the only source.
    """
    socket_path = tmp_path / "exec.sock"
    seen: list[tuple[str | None, str | None]] = []

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                request = decode_request(read_frame(conn))
                seen.append((request.origin_path, request.origin_actor))
                write_frame(
                    conn,
                    encode_response(
                        ExecuteResponse(ok=True, output="", exit_code=0, error=None, reason="")
                    ),
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    context = RequestContext(
        channel="telegram", chat_id="c1", session_key="s1", sender_id="12345"
    )
    with request_context(context):
        ExecutorClient(socket_path).execute(
            server_id_or_name="prod-web-01",
            command="uptime",
            session_id="s1",
            execution_context="interactive",
            preview_requested=False,
            timeout_s=None,
        )
    thread.join(timeout=10)

    assert seen == [("telegram", "12345")]


def test_a_context_with_no_sender_names_no_person(tmp_path: Path) -> None:
    """A channel that authenticated nobody asserts nobody.

    The value is null and never the empty string. #13 reads null as "unknown", and it falls back
    to the path rule alone rather than treating the absence as a person who differs from every
    approver.
    """
    socket_path = tmp_path / "exec.sock"
    seen: list[str | None] = []

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                seen.append(decode_request(read_frame(conn)).origin_actor)
                write_frame(
                    conn,
                    encode_response(
                        ExecuteResponse(ok=True, output="", exit_code=0, error=None, reason="")
                    ),
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    context = RequestContext(channel="telegram", chat_id="c1", session_key="s1", sender_id="  ")
    with request_context(context):
        ExecutorClient(socket_path).execute(
            server_id_or_name="prod-web-01",
            command="uptime",
            session_id="s1",
            execution_context="interactive",
            preview_requested=False,
            timeout_s=None,
        )
    thread.join(timeout=10)

    assert seen == [None]


def test_a_request_with_no_bound_context_names_no_path(tmp_path: Path) -> None:
    """Fail closed. No bound context proves nothing about the transport that raised the turn."""
    socket_path = tmp_path / "exec.sock"
    seen: list[str | None] = []

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                seen.append(decode_request(read_frame(conn)).origin_path)
                write_frame(
                    conn,
                    encode_response(
                        ExecuteResponse(ok=True, output="", exit_code=0, error=None, reason="")
                    ),
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    ExecutorClient(socket_path).execute(
        server_id_or_name="prod-web-01",
        command="uptime",
        session_id="s1",
        execution_context="automation",
        preview_requested=False,
        timeout_s=None,
    )
    thread.join(timeout=10)

    assert seen == [None]


def test_the_client_signature_keeps_the_sdk_stand_in_usable() -> None:
    """``DisabledExecutorClient`` (#21) overrides ``execute`` with the same keywords.

    #38 needed the origin path on the wire. A new keyword here would break that override, and
    an embedded caller would then read a TypeError instead of the message that names the fix.
    So the origin path arrives through the bound request context.
    """
    import inspect

    from nanoinfra.sdk.clients import DisabledExecutorClient

    assert (
        inspect.signature(ExecutorClient.execute).parameters.keys()
        == inspect.signature(DisabledExecutorClient.execute).parameters.keys()
    )


def test_the_client_holds_no_credential_and_no_backend() -> None:
    """Structural, for the same reason as the tool check: the client is agent-side code."""
    imported = _imported_modules(Path("nanoinfra/gates/executor/client.py"))

    assert [name for name in _FORBIDDEN_IMPORTS if name in imported] == []


def _wait_for_listen(ready: threading.Event, timeout_s: float = 10.0) -> None:
    """Wait until the server thread has called listen().

    Existence is not readiness. `bind()` creates the socket file and `listen()` accepts a peer after
    it, so a connect between the two fails with ConnectionRefusedError. CI met that race in the
    fetcher client tests on Python 3.14, and these tests hold the same shape.

    The client is the code under test here, so the retry cannot live in it. The server thread says
    when it is ready instead.
    """
    if not ready.wait(timeout_s):
        raise AssertionError("the server thread never reached listen()")
