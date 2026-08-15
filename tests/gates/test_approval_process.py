# tests/gates/test_approval_process.py
"""Item 36 (#38) end to end, against a real executor process.

The acceptance criterion of the item names this file. An in-process test proves that the code
calls the code. Only a real child proves the whole sequence:

- The agent submits an unusual interactive action over the execute socket, and the call blocks.
- The payload reaches an operator on a second socket that the agent does not own.
- The action runs after the approval, it expires without one, and a denial refuses it.
- The approval covers one command, so the same session cannot reuse it for another command.

The child dials a host that no route reaches, so ``timeout_s`` bounds every run to one second.
The proof of execution is the job record. A refused action writes none, and an executed action
writes one whatever the host answered.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.gates.executor.operator_socket import (
    OperatorClient,
    OperatorUnavailableError,
    PendingView,
)
from nanoinfra.gates.executor.protocol import (
    ExecuteRequest,
    ExecuteResponse,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)
from nanoinfra.gates.executor.supervisor import start_executor
from nanoinfra.gates.prompt import digest_rendered_prompt
from nanoinfra.secrets import crypto
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_COMMAND = "systemctl reload nginx"
_OTHER_COMMAND = "rm -rf /var/log/nginx"
_HOST = "10.0.1.5"
_GROUP_HOSTS = ("10.0.2.11", "10.0.2.12", "10.0.2.13")

# One second of idle timeout. The child really dials, and no route reaches this address, so the
# run must not hold the test for a provider default.
_RUN_TIMEOUT_S = "1"


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """One executor child, its policy, its inventory, and the two socket paths."""
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    home = tmp_path / "home"
    (home / ".nanoinfra").mkdir(parents=True)
    (home / ".nanoinfra" / "config.json").write_text(
        json.dumps(
            {
                "gates": {
                    "approvers": [{"channel": "webui", "sender": "operator-1"}],
                    "approvalPaths": ["webui", "telegram"],
                    "approvalTimeoutS": 2,
                    # Both tiers say approve. A ScopePolicy field that the block leaves out
                    # falls back to deny rather than to the shipped interactive default, so a
                    # group action would refuse before it ever reached the approval path.
                    "interactive": {"mutate.remote": {"host": "approve", "group": "approve"}},
                }
            }
        ),
        encoding="utf-8",
    )
    # The child is a separate process, so HOME is what places its config and its audit root.
    monkeypatch.setenv("HOME", str(home))
    # The child resolves a group from the local inventory with its own parser. ``ansible-inventory``
    # is authoritative when PATH names one (#30), and a user-site install of it reads HOME, so the
    # fake HOME above would break it. The scope resolver has its own tests, and this file asks
    # about the two sockets, so the child gets a PATH with no ansible on it. The launch is
    # unaffected, because the supervisor runs an absolute interpreter path.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    ServerStore(workspace).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": _HOST}}
    )

    execute_socket = tmp_path / "r" / "e.sock"
    operator_socket = tmp_path / "r" / "op.sock"
    # The environment places the operator socket, the same way entrypoint.sh does.
    monkeypatch.setenv("NANOINFRA_OPERATOR_SOCKET", str(operator_socket))

    handle = start_executor(
        socket_path=execute_socket, workspace=workspace, timeout_s=20.0
    )
    try:
        operator = OperatorClient(operator_socket)
        # Readiness is an answer, and never the existence of the path. `bind()` creates the file
        # and `listen()` accepts a peer after that, so a wait on `exists` returns inside the gap
        # and the first real call raises OperatorUnavailableError. The client does not retry, and
        # it must not: a production caller reads an unreachable executor as a deployment fault.
        _wait_until(
            lambda: _operator_answers(operator), hint=lambda: handle.read_log_tail(tail=20)
        )
        yield _Deployment(
            workspace=workspace,
            execute_socket=execute_socket,
            operator=operator,
            handle=handle,
        )
    finally:
        handle.stop(timeout_s=10)


class _Deployment:
    """What one test drives: the two sockets, the workspace, and the child."""

    def __init__(
        self,
        *,
        workspace: Path,
        execute_socket: Path,
        operator: OperatorClient,
        handle: Any,
    ) -> None:
        self.workspace = workspace
        self.execute_socket = execute_socket
        self.operator = operator
        self.handle = handle

    def submit(self, **over: object) -> _Submitter:
        """Start one execute request in its own thread, because the call blocks."""
        submitter = _Submitter(self.execute_socket, _request(**over))
        submitter.start()
        return submitter

    def waiting(self) -> PendingView:
        """Return the one action that waits for an answer."""
        _wait_until(lambda: bool(self.operator.pending()), hint=self._log)
        return self.operator.pending()[0]

    def jobs(self) -> list[Any]:
        return JobStore(self.workspace).list_jobs()

    def _log(self) -> str:
        return "\n".join(self.handle.read_log_tail(tail=30))


def _group_server(workspace: Path) -> None:
    """One ansible-runner server whose group names three hosts in a local inventory."""
    project = workspace / "ansible-project"
    project.mkdir(exist_ok=True)
    (project / "inventory").write_text(
        "[web]\n" + "\n".join(_GROUP_HOSTS) + "\n", encoding="utf-8"
    )
    ServerStore(workspace).create(
        {
            "name": "webservers",
            "providerId": "ansible-runner",
            "config": {"group": "web", "projectPath": str(project)},
        }
    )


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": _RUN_TIMEOUT_S,
        "token_nonce": None,
        "origin_path": "telegram",
    }
    fields.update(over)
    return ExecuteRequest(**fields)


class _Submitter(threading.Thread):
    """One blocking execute call, held off the test's own thread."""

    def __init__(self, socket_path: Path, request: ExecuteRequest) -> None:
        super().__init__(daemon=True)
        self._socket_path = socket_path
        self._request = request
        self.response: ExecuteResponse | None = None

    def run(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(60.0)
            conn.connect(str(self._socket_path))
            write_frame(conn, encode_request(self._request))
            self.response = decode_response(read_frame(conn))

    def answer(self, timeout_s: float = 30.0) -> ExecuteResponse:
        self.join(timeout=timeout_s)
        assert self.response is not None, "the executor never answered"
        return self.response


# ------------------------------------------------------------------ the whole sequence


def test_an_action_suspends_reaches_an_operator_and_then_runs(deployment: _Deployment) -> None:
    """The acceptance sequence, through two real sockets and one real child.

    The reply proves the action reached the backend rather than the gate. The job record proves
    it too, and it survives the reply.
    """
    call = deployment.submit()
    waiting = deployment.waiting()

    assert waiting["session_id"] == "s1"
    assert waiting["origin_path"] == "telegram"
    assert _HOST in waiting["payload"]
    assert waiting["target_digest"] == digest_rendered_prompt(waiting["payload"])
    assert deployment.jobs() == []  # nothing ran while the action waited

    answer = deployment.operator.approve(
        request_id=waiting["request_id"],
        actor="operator-1",
        approval_path="webui",
        target_digest=waiting["target_digest"],
    )
    response = call.answer()

    assert answer.ok
    assert response.reason == ""  # no gate refusal reached the caller
    jobs = deployment.jobs()
    assert len(jobs) == 1
    assert jobs[0].status in {"failed", "timed_out"}  # no route reaches the host


def test_a_group_action_reaches_the_operator_as_every_named_host(
    deployment: _Deployment,
) -> None:
    """The literal case of the item: an unusual interactive **group** action.

    ``group: web`` reaches the operator as three named hosts and a count. It never reaches them
    as a label, because a count with no names hides the one host an operator would have refused.

    The test denies the action rather than approve it. The run half is the ssh case above, and a
    real ansible play here would add a slow process for a property the payload already carries.
    """
    _group_server(deployment.workspace)

    call = deployment.submit(server_id_or_name="webservers")
    waiting = deployment.waiting()

    assert waiting["scope"] == "group"
    assert waiting["host_count"] == len(_GROUP_HOSTS)
    assert sorted(waiting["hosts"]) == sorted(_GROUP_HOSTS)
    for host in _GROUP_HOSTS:
        assert host in waiting["payload"]
    assert f"Hosts: {len(_GROUP_HOSTS)}" in waiting["payload"]
    assert waiting["target_digest"] == digest_rendered_prompt(waiting["payload"])

    deployment.operator.deny(
        request_id=waiting["request_id"],
        actor="operator-1",
        approval_path="webui",
        reason="the group is too wide today",
    )
    response = call.answer()

    assert not response.ok
    assert "too wide" in response.reason
    assert deployment.jobs() == []


def test_an_action_expires_when_nobody_answers(deployment: _Deployment) -> None:
    """The deadline ends the wait, and the action reaches no host."""
    call = deployment.submit()
    deployment.waiting()

    response = call.answer()

    assert not response.ok
    assert "expired" in response.reason
    assert deployment.jobs() == []
    assert deployment.operator.pending() == ()


def test_a_denial_refuses_the_action_and_names_the_reason(deployment: _Deployment) -> None:
    """A denial is terminal (#15), and the words an operator typed reach the caller."""
    call = deployment.submit()
    waiting = deployment.waiting()

    answer = deployment.operator.deny(
        request_id=waiting["request_id"],
        actor="operator-1",
        approval_path="webui",
        reason="the change window is closed",
    )
    response = call.answer()

    assert answer.ok
    assert not response.ok
    assert "change window" in response.reason
    assert deployment.jobs() == []


def test_the_execute_socket_never_accepts_an_answer(deployment: _Deployment) -> None:
    """The agent holds the execute socket, so an answer there would approve its own action.

    The wire carries no answer verb at all. A caller that sends an operator frame to the execute
    socket gets a malformed-frame refusal, and the action keeps waiting.
    """
    from nanoinfra.gates.executor.operator_socket import (
        OP_APPROVE,
        OperatorRequest,
        encode_operator_request,
    )

    call = deployment.submit()
    waiting = deployment.waiting()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(20.0)
        conn.connect(str(deployment.execute_socket))
        write_frame(
            conn,
            encode_operator_request(
                OperatorRequest(
                    op=OP_APPROVE,
                    request_id=waiting["request_id"],
                    actor="operator-1",
                    approval_path="webui",
                    target_digest=waiting["target_digest"],
                )
            ),
        )
        refusal = decode_response(read_frame(conn))

    response = call.answer()

    assert not refusal.ok
    assert "Malformed request" in str(refusal.error)
    assert not response.ok  # the action still expired, because no answer ever landed
    assert deployment.jobs() == []


def test_an_answer_from_a_sender_outside_the_config_does_not_count(
    deployment: _Deployment,
) -> None:
    """Reachability grants nothing. The approver set lives in git-reviewed config (#13)."""
    call = deployment.submit()
    waiting = deployment.waiting()

    answer = deployment.operator.approve(
        request_id=waiting["request_id"],
        actor="somebody-else",
        approval_path="webui",
        target_digest=waiting["target_digest"],
    )
    response = call.answer()

    assert not answer.ok
    assert "gates.approvers" in str(answer.error)
    assert not response.ok
    assert deployment.jobs() == []


def test_an_approval_cannot_be_replayed_for_another_command_in_the_same_session(
    deployment: _Deployment,
) -> None:
    """#12 rule 4, through the sockets. A different command needs a different approval.

    The operator approves one command. The same session then asks for another command, and the
    executor suspends again. The digest of the first payload cannot answer the second request,
    and the first request id cannot take a second answer.
    """
    first_call = deployment.submit()
    first = deployment.waiting()
    deployment.operator.approve(
        request_id=first["request_id"],
        actor="operator-1",
        approval_path="webui",
        target_digest=first["target_digest"],
    )
    assert first_call.answer().reason == ""
    assert len(deployment.jobs()) == 1

    second_call = deployment.submit(command=_OTHER_COMMAND)
    second = deployment.waiting()

    assert second["request_id"] != first["request_id"]
    assert second["target_digest"] != first["target_digest"]

    replay = deployment.operator.approve(
        request_id=second["request_id"],
        actor="operator-1",
        approval_path="webui",
        target_digest=first["target_digest"],
    )
    reused = deployment.operator.approve(
        request_id=first["request_id"],
        actor="operator-1",
        approval_path="webui",
        target_digest=first["target_digest"],
    )
    response = second_call.answer()

    assert not replay.ok
    assert replay.refusal == "digest_mismatch"
    assert not reused.ok
    assert reused.refusal in {"already_answered", "unknown_request"}
    assert not response.ok
    assert len(deployment.jobs()) == 1  # the second command never ran


def _operator_answers(operator: OperatorClient) -> bool:
    """Report whether the operator socket answers a real request.

    ``pending`` is the read-only verb, so a readiness probe costs nothing and changes no state.
    """
    try:
        operator.pending()
    except OperatorUnavailableError:
        return False
    return True


def _wait_until(
    predicate: Any, timeout_s: float = 20.0, hint: Any = None
) -> None:
    """Wait for a condition the child produces, or fail with the child's own log."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    detail = f"\n{hint()}" if hint is not None else ""
    raise AssertionError(f"the condition never became true{detail}")
