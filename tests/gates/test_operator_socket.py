# tests/gates/test_operator_socket.py
"""Item 36 (#38): the operator answers on a second socket, and one wait blocks nobody else.

Two properties carry this file.

**A second socket.** The agent holds the execute socket. An answer accepted there would let a
compromised agent approve its own action, so the executor owns a separate listener for answers.
The socket carries mode 0660 and an operator group. A single-uid install has no such
separation, and the module docstring states that limit.

**Concurrency.** A blocked approval holds one connection for the whole wait. A serial accept
loop would then stop every other action, which is a denial of service on the whole agent. So
each connection gets its own thread.
"""

from __future__ import annotations

import json
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.operator_socket import (
    OP_APPROVE,
    OP_DENY,
    OP_PENDING,
    OPERATOR_PROTOCOL_VERSION,
    ApprovalService,
    OperatorClient,
    OperatorRequest,
    OperatorUnavailableError,
    decode_operator_request,
    default_operator_socket_path,
    encode_operator_request,
    serve_operator_forever,
)
from nanoinfra.gates.executor.protocol import (
    ExecuteRequest,
    ProtocolError,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)
from nanoinfra.gates.executor.server import serve_forever
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.secrets import crypto
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.store import ServerStore

_SSH_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_COMMAND = "systemctl reload nginx"
_PAYLOAD = "nanoinfra approval request v1\n"
_DIGEST = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


@pytest.fixture(autouse=True)
def _no_operator_socket_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment must not point this suite at a deployment's own socket."""
    monkeypatch.delenv("NANOINFRA_OPERATOR_SOCKET", raising=False)


def _gates() -> GatesConfig:
    return GatesConfig.model_validate(
        {
            "approvers": [{"channel": "webui", "sender": "operator-1"}],
            "approvalPaths": ["webui", "telegram"],
            "approvalTimeoutS": 30,
        }
    )


def _service() -> tuple[ApprovalService, PendingApprovalStore]:
    pending = PendingApprovalStore()
    service = ApprovalService(
        pending=pending, tokens=ApprovalTokenStore(), gates_loader=_gates
    )
    return service, pending


def _suspend(pending: PendingApprovalStore, *, session_id: str = "s1"):
    return pending.create(
        session_id=session_id,
        origin_path="telegram",
        execution_context="interactive",
        capability_class="mutate.remote",
        scope="host",
        hosts=("10.0.1.5",),
        command=_COMMAND,
        payload=_PAYLOAD,
        target_digest=_DIGEST,
        timeout_s=30.0,
    )


# ------------------------------------------------------------------------------- the wire


def test_each_verb_round_trips() -> None:
    for request in (
        OperatorRequest(op=OP_PENDING),
        OperatorRequest(
            op=OP_APPROVE,
            request_id="r1",
            actor="operator-1",
            approval_path="webui",
            target_digest=_DIGEST,
        ),
        OperatorRequest(op=OP_DENY, request_id="r1", actor="operator-1", approval_path="webui"),
    ):
        assert decode_operator_request(encode_operator_request(request)) == request


def test_an_approval_frame_without_a_digest_is_refused() -> None:
    """The digest proves the answer describes the payload the executor rendered."""
    payload = json.loads(encode_operator_request(OperatorRequest(op=OP_PENDING)))
    payload["op"] = OP_APPROVE
    payload["request_id"] = "r1"
    payload["actor"] = "operator-1"
    payload["approval_path"] = "webui"

    with pytest.raises(ProtocolError):
        decode_operator_request(json.dumps(payload).encode())


def test_an_unknown_field_and_an_unknown_verb_are_refused() -> None:
    """Fail closed on a wire the peer does not share, the same as the execute wire."""
    extra = json.loads(encode_operator_request(OperatorRequest(op=OP_PENDING)))
    extra["surprise"] = "x"
    verb = {"v": OPERATOR_PROTOCOL_VERSION, "op": "delete_the_audit_log"}

    with pytest.raises(ProtocolError):
        decode_operator_request(json.dumps(extra).encode())
    with pytest.raises(ProtocolError):
        decode_operator_request(json.dumps(verb).encode())


def test_a_future_version_and_a_missing_version_are_refused() -> None:
    payload = json.loads(encode_operator_request(OperatorRequest(op=OP_PENDING)))
    future = dict(payload, v=OPERATOR_PROTOCOL_VERSION + 1)
    bare = {"op": OP_PENDING}

    with pytest.raises(ProtocolError):
        decode_operator_request(json.dumps(future).encode())
    with pytest.raises(ProtocolError):
        decode_operator_request(json.dumps(bare).encode())


# ---------------------------------------------------------------------------- the socket


def test_the_socket_lists_approves_and_denies(tmp_path: Path) -> None:
    """One real socket round trip per verb, because the socket is the boundary under test."""
    service, pending = _service()
    first = _suspend(pending)
    second = _suspend(pending, session_id="s2")
    socket_path = tmp_path / "op" / "operator.sock"

    with _Serving(socket_path, service, max_requests=3):
        client = OperatorClient(socket_path)
        listed = client.pending()
        approved = client.approve(
            request_id=first.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=_DIGEST,
        )
        denied = client.deny(
            request_id=second.request_id,
            actor="operator-1",
            approval_path="webui",
            reason="not now",
        )

    assert [item["request_id"] for item in listed] == [first.request_id, second.request_id]
    assert listed[0]["payload"] == _PAYLOAD
    assert listed[0]["hosts"] == ["10.0.1.5"]
    assert approved.ok
    assert denied.ok
    assert pending.pending() == ()


def test_the_socket_excludes_every_account_outside_the_operator_group(tmp_path: Path) -> None:
    """Mode 0660. A connect needs write rights, so an outside account cannot answer.

    The mode is the whole filesystem control here. entrypoint.sh sets the group, because the
    executor holds no privilege to create one.
    """
    service, _ = _service()
    socket_path = tmp_path / "op" / "operator.sock"

    with _Serving(socket_path, service, max_requests=1):
        mode = stat.S_IMODE(socket_path.stat().st_mode)
        OperatorClient(socket_path).pending()

    assert mode == 0o660
    assert mode & 0o007 == 0


def test_the_socket_directory_starts_private(tmp_path: Path) -> None:
    """Private first is the fail-closed order. A root start opens it to the operator group."""
    service, _ = _service()
    socket_path = tmp_path / "op" / "operator.sock"

    with _Serving(socket_path, service, max_requests=1):
        mode = stat.S_IMODE(socket_path.parent.stat().st_mode)
        OperatorClient(socket_path).pending()

    assert mode == 0o700


def test_a_malformed_frame_gets_a_refusal_and_not_a_crash(tmp_path: Path) -> None:
    """A peer that speaks nonsense must not take the operator socket down."""
    service, pending = _service()
    record = _suspend(pending)
    socket_path = tmp_path / "op" / "operator.sock"

    with _Serving(socket_path, service, max_requests=2):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(socket_path))
            write_frame(conn, b"not a frame")
            read_frame(conn)
        still_listed = OperatorClient(socket_path).pending()

    assert [item["request_id"] for item in still_listed] == [record.request_id]


def test_the_socket_file_goes_away_on_exit(tmp_path: Path) -> None:
    """A stale socket file blocks the next bind, so the server cleans up after itself."""
    service, _ = _service()
    socket_path = tmp_path / "op" / "operator.sock"

    with _Serving(socket_path, service, max_requests=1):
        OperatorClient(socket_path).pending()

    _wait_until(lambda: not socket_path.exists())
    assert not socket_path.exists()


def test_a_missing_socket_reads_as_unavailable(tmp_path: Path) -> None:
    """"The executor is not there" and "the answer did not count" need different words."""
    with pytest.raises(OperatorUnavailableError):
        OperatorClient(tmp_path / "absent.sock").pending()


def test_the_default_path_sits_beside_the_execute_socket_in_its_own_directory() -> None:
    """A sibling name in the shared directory would inherit the agent's group through setgid.

    The agent could then connect and answer, which is the one thing this socket exists to stop.
    """
    execute = Path("/run/nanoinfra-exec/executor.sock")

    operator = default_operator_socket_path(execute)

    assert operator.parent != execute.parent
    assert operator.parent.parent == execute.parent


def test_two_executors_in_one_run_directory_get_two_operator_sockets() -> None:
    """The SDK names each execute socket after its process (#21), so two can share a directory.

    One fixed operator name would let the second executor unlink the first one's socket.
    """
    run = Path("/data/run")

    first = default_operator_socket_path(run / "sdk-111-aaaa.sock")
    second = default_operator_socket_path(run / "sdk-222-bbbb.sock")

    assert first != second
    assert first.parent == second.parent


def test_the_environment_can_place_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """entrypoint.sh sets this variable, because it owns the two accounts and the group."""
    monkeypatch.setenv("NANOINFRA_OPERATOR_SOCKET", "/run/nanoinfra-op/answers.sock")

    assert default_operator_socket_path(Path("/run/x/executor.sock")) == Path(
        "/run/nanoinfra-op/answers.sock"
    )


# ------------------------------------------------- one wait blocks nobody else (#38)


def test_a_pending_approval_does_not_freeze_another_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The denial-of-service property. A blocked approval holds one connection only.

    The first session asks for an action the policy sends to approval, and it waits. The second
    session asks for an action a standing grant covers, and it must run while the first waits.
    """
    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )
    ServerStore(tmp_path).create(
        {"name": "prod-web-02", "providerId": "ssh", "config": {"host": "10.0.1.6"}}
    )
    _write_policy(
        monkeypatch,
        tmp_path,
        {
            "approvers": [{"channel": "webui", "sender": "operator-1"}],
            "approvalPaths": ["webui", "telegram"],
            "approvalTimeoutS": 30,
            "interactive": {"mutate.remote": {"host": "approve"}},
            "standingGrants": [
                {
                    "id": "granted",
                    "contexts": ["interactive"],
                    "hosts": ["10.0.1.6"],
                    "commands": [_COMMAND],
                }
            ],
        },
    )
    execute_socket = tmp_path / "run" / "executor.sock"
    operator_socket = default_operator_socket_path(execute_socket)
    fake = ExecutionResult(exit_code=0, output="reloaded", error=None)
    answers: list[Any] = []

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=fake)):
        # No request cap here. The cap ends the accept loop, and the loop's exit closes the
        # operator socket. This test must answer on that socket while an action still waits, so
        # the server runs to the end of the process instead. The thread is a daemon.
        server = threading.Thread(
            target=serve_forever,
            kwargs={"socket_path": execute_socket, "workspace": tmp_path},
            daemon=True,
        )
        server.start()
        _wait_until(lambda: execute_socket.exists() and operator_socket.exists())

        blocked = _Submitter(execute_socket, _request(server_id_or_name="prod-web-01"))
        blocked.start()
        # The second action arrives while the first one waits, and it must answer at once.
        _wait_until(lambda: bool(OperatorClient(operator_socket).pending()))
        granted = _submit(execute_socket, _request(server_id_or_name="prod-web-02"))

        record = OperatorClient(operator_socket).pending()[0]
        answers.append(
            OperatorClient(operator_socket).approve(
                request_id=record["request_id"],
                actor="operator-1",
                approval_path="webui",
                target_digest=record["target_digest"],
            )
        )
        blocked.join(timeout=20)

    assert granted.ok  # the second session ran while the first waited
    assert answers[0].ok
    assert blocked.response is not None
    assert blocked.response.ok


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
        "origin_path": "telegram",
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _submit(socket_path: Path, request: ExecuteRequest):
    """Send one execute request and read the reply, on one connection."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(30.0)
        conn.connect(str(socket_path))
        write_frame(conn, encode_request(request))
        return decode_response(read_frame(conn))


class _Submitter(threading.Thread):
    """One execute request in its own thread, so a blocked reply does not block the test."""

    def __init__(self, socket_path: Path, request: ExecuteRequest) -> None:
        super().__init__(daemon=True)
        self._socket_path = socket_path
        self._request = request
        self.response: Any = None

    def run(self) -> None:
        self.response = _submit(self._socket_path, self._request)


def _write_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gates: dict[str, Any]
) -> None:
    """Give the executor its own config and its own audit root, inside tmp_path.

    ``serve_forever`` reads the policy and opens the audit store through the data dir. A test
    must not read the developer's policy. The loader path is pinned as well as HOME, because
    ``get_config_path`` prefers the loader's module global and another test may have set it.
    """
    config = tmp_path / "home" / ".nanoinfra" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"gates": gates}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", config)


class _Serving:
    """Run one operator socket for the length of a block."""

    def __init__(
        self, socket_path: Path, service: ApprovalService, *, max_requests: int
    ) -> None:
        self._socket_path = socket_path
        self._service = service
        self._max_requests = max_requests
        self._thread: threading.Thread | None = None

    def __enter__(self) -> None:
        self._thread = threading.Thread(
            target=serve_operator_forever,
            args=(self._socket_path, self._service),
            kwargs={"max_requests": self._max_requests},
            daemon=True,
        )
        self._thread.start()
        _wait_until(self._socket_path.exists)

    def __exit__(self, *_exc: object) -> None:
        if self._thread is not None:
            self._thread.join(timeout=10)


def _wait_until(predicate: Any, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the condition never became true")
