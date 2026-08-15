# tests/gates/test_approval_delivery_process.py
"""Item 41 (#43) end to end, against a real executor process.

The acceptance criterion of the item names this file. An in-process test proves that the code
calls the code. Only a real child proves the whole sequence:

- The agent submits an unusual interactive action, and the call blocks inside the executor.
- The gateway-side watcher reads the pending list and delivers the payload to an approver on
  Telegram, which is a path the request did not arrive on.
- The approver answers with ``/approve <request-id>`` through the command router, and the action
  runs.
- A sender that ``gates.approvers`` does not name answers nothing, and the refusal says why.
- A request that arrived on Telegram takes no answer on Telegram, and the refusal says why.

The chat side is real code and a fake transport. The router, the surface, the watcher, and both
sockets are the shipped objects. The bus is a list, because a real Telegram bot needs a token and
a network.

The child dials a host that no route reaches, so ``timeout_s`` bounds every run to one second.
The proof of execution is the job record. A refused action writes none, and an executed action
writes one whatever the host answered.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.command.approvals import (
    APPROVE_COMMAND,
    DENY_COMMAND,
    ApprovalAnswerSurface,
    register_approval_commands,
)
from nanoinfra.command.router import CommandContext, CommandRouter
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.approval_delivery import ApprovalDeliveryWatcher
from nanoinfra.gates.executor.operator_socket import OperatorClient
from nanoinfra.gates.executor.protocol import (
    ExecuteRequest,
    ExecuteResponse,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)
from nanoinfra.gates.executor.supervisor import start_executor
from nanoinfra.secrets import crypto
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_COMMAND = "systemctl reload nginx"
_HOST = "10.0.1.5"

# The approver's Telegram account, and the sender id the channel puts on an inbound message.
_APPROVER = "770123456"
_APPROVER_SENDER_ID = f"{_APPROVER}|ops_lead"
_STRANGER_SENDER_ID = "999000111|passer_by"

# One second of idle timeout. The child really dials, and no route reaches this address.
_RUN_TIMEOUT_S = "1"

# The line an approver copies the request id out of.
_REQUEST_ID_RE = re.compile(r"^Request id: (\S+)$", re.MULTILINE)

# Two approvers on two paths. The WebUI approver is what makes a Telegram-origin request
# answerable at all: with a Telegram approver alone the executor refuses such a request before it
# suspends, because no correct approval could exist for it (#38 asks that question first).
_GATES = GatesConfig.model_validate(
    {
        "approvers": [
            {"channel": "telegram", "sender": _APPROVER},
            {"channel": "webui", "sender": "webui"},
        ],
        "approvalPaths": ["webui", "telegram"],
        "approvalTimeoutS": 30,
        "interactive": {"mutate.remote": {"host": "approve", "group": "approve"}},
    }
)


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """One executor child, its policy, its inventory, and the chat side of the gateway."""
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    home = tmp_path / "home"
    (home / ".nanoinfra").mkdir(parents=True)
    (home / ".nanoinfra" / "config.json").write_text(
        json.dumps({"gates": _GATES.model_dump(by_alias=True, mode="json")}),
        encoding="utf-8",
    )
    # The child is a separate process, so HOME is what places its config and its audit root.
    monkeypatch.setenv("HOME", str(home))
    # The child resolves a scope from the local inventory with its own parser, and an
    # ``ansible-inventory`` on PATH would read the fake HOME above. The scope resolver has its
    # own tests, and this file asks about the delivery and the answer.
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

    handle = start_executor(socket_path=execute_socket, workspace=workspace, timeout_s=20.0)
    try:
        _wait_until(operator_socket.exists, hint=lambda: handle.read_log_tail(tail=20))
        yield _Deployment(
            workspace=workspace,
            execute_socket=execute_socket,
            operator_socket=operator_socket,
            handle=handle,
        )
    finally:
        handle.stop(timeout_s=10)


class _Deployment:
    """The chat half of one gateway, plus the two sockets of one real executor child."""

    def __init__(
        self,
        *,
        workspace: Path,
        execute_socket: Path,
        operator_socket: Path,
        handle: Any,
    ) -> None:
        self.workspace = workspace
        self.execute_socket = execute_socket
        self.operator_socket = operator_socket
        self.handle = handle
        self.sent: list[OutboundMessage] = []
        self.watcher = ApprovalDeliveryWatcher(
            client=OperatorClient(operator_socket, timeout_s=10.0),
            publish=self._publish,
            gates_loader=lambda: _GATES,
            is_channel_enabled=lambda name: name == "telegram",
            interval_s=0.05,
        )
        self.router = CommandRouter()
        register_approval_commands(
            self.router,
            surface=ApprovalAnswerSurface(
                client=OperatorClient(operator_socket, timeout_s=10.0)
            ),
        )

    async def _publish(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)

    def submit(self, **over: object) -> _Submitter:
        """Start one execute request in its own thread, because the call blocks."""
        submitter = _Submitter(self.execute_socket, _request(**over))
        submitter.start()
        return submitter

    async def deliver(self) -> OutboundMessage:
        """Poll until the watcher delivers one request, and return the message it sent."""
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if await self.watcher.deliver_pending():
                break
            time.sleep(0.02)
        assert self.sent, f"the watcher delivered nothing\n{self._log()}"
        return self.sent[-1]

    async def answer(self, text: str, *, sender: str = _APPROVER_SENDER_ID) -> str:
        """Route one Telegram message the way the agent loop routes a priority command."""
        assert self.router.is_priority(text)
        msg = InboundMessage(
            channel="telegram", sender_id=sender, chat_id=_APPROVER, content=text
        )
        ctx = CommandContext(
            msg=msg,
            session=None,
            key=f"telegram:{_APPROVER}",
            raw=text,
            loop=MagicMock(),
        )
        reply = await self.router.dispatch_priority(ctx)
        assert reply is not None
        return reply.content

    def jobs(self) -> list[Any]:
        return JobStore(self.workspace).list_jobs()

    def _log(self) -> str:
        return "\n".join(self.handle.read_log_tail(tail=30))


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": _RUN_TIMEOUT_S,
        "token_nonce": None,
        # The WebUI chat arrives on ``websocket``. That is exactly why the WebUI inbox alone is
        # a nominal second path, and why this item exists.
        "origin_path": "websocket",
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
            conn.settimeout(90.0)
            conn.connect(str(self._socket_path))
            write_frame(conn, encode_request(self._request))
            self.response = decode_response(read_frame(conn))

    def result(self, timeout_s: float = 60.0) -> ExecuteResponse:
        self.join(timeout=timeout_s)
        assert self.response is not None, "the executor never answered"
        return self.response


# ------------------------------------------------------------------ the whole sequence


async def test_a_webui_action_reaches_telegram_and_the_reply_runs_it(
    deployment: _Deployment,
) -> None:
    """The acceptance sequence, through two real sockets, one real child, and one channel.

    The reply proves the action reached the backend rather than the gate. The job record proves
    it too, and it survives the reply.
    """
    call = deployment.submit()

    delivered = await deployment.deliver()
    match = _REQUEST_ID_RE.search(delivered.content)

    assert delivered.channel == "telegram"
    assert delivered.chat_id == _APPROVER
    assert match is not None, "an approver must read the request id out of the message"
    assert "nanoinfra approval request" in delivered.content  # the executor's own header
    assert _HOST in delivered.content
    assert deployment.jobs() == []  # nothing ran while the action waited

    request_id = match.group(1)
    reply = await deployment.answer(f"{APPROVE_COMMAND} {request_id}")
    response = call.result()

    assert "Approved" in reply
    assert response.reason == ""  # no gate refusal reached the caller
    jobs = deployment.jobs()
    assert len(jobs) == 1
    assert jobs[0].status in {"failed", "timed_out"}  # no route reaches the host


async def test_the_delivery_carries_the_digest_and_no_summary(
    deployment: _Deployment,
) -> None:
    """The payload is the executor's rendering, verbatim, and the digest binds it."""
    call = deployment.submit()
    delivered = await deployment.deliver()
    request_id = _request_id(delivered_content=delivered.content)

    # The answer ends the blocked call, so the child does not hold the connection for the
    # whole approval window.
    await deployment.answer(f"{DENY_COMMAND} {request_id} the test read the payload")
    call.result()

    assert "Binding digest: " in delivered.content
    assert f"  | {_COMMAND}" in delivered.content  # the executor's own command layout
    assert "Hosts: 1" in delivered.content


async def test_a_sender_the_config_does_not_name_answers_nothing(
    deployment: _Deployment,
) -> None:
    """Reachability grants nothing. ``gates.approvers`` is the only source of authority.

    The refusal names the rule and the identity that failed it. It names no approver.
    """
    call = deployment.submit()
    delivered = await deployment.deliver()
    request_id = _request_id(delivered_content=delivered.content)

    refusal = await deployment.answer(
        f"{APPROVE_COMMAND} {request_id}", sender=_STRANGER_SENDER_ID
    )

    assert "gates.approvers" in refusal
    assert _APPROVER not in refusal
    assert deployment.jobs() == []

    # The action still waits, so the approver can still answer it.
    approved = await deployment.answer(f"{APPROVE_COMMAND} {request_id}")
    call.result()

    assert "Approved" in approved
    assert len(deployment.jobs()) == 1


async def test_a_request_that_arrived_on_telegram_takes_no_answer_there(
    deployment: _Deployment,
) -> None:
    """Path independence holds by construction, and the refusal says why (#13)."""
    call = deployment.submit(origin_path="telegram")
    # Telegram is the origin here, so the watcher delivers nothing to Telegram. The request id
    # comes from the operator socket instead, the way the WebUI inbox reads it.
    request_id = _wait_for_pending(deployment)["request_id"]

    refusal = await deployment.answer(f"{APPROVE_COMMAND} {request_id}")
    denial = await deployment.answer(f"{DENY_COMMAND} {request_id} no")

    assert deployment.sent == []  # the watcher delivered nothing to the origin path
    assert "another authenticated path" in refusal
    assert "another authenticated path" in denial
    assert deployment.jobs() == []

    # The WebUI inbox is the path that may answer this request, and it still can.
    answered = OperatorClient(deployment.operator_socket, timeout_s=10.0).deny(
        request_id=request_id,
        actor="webui",
        approval_path="webui",
        reason="the origin path answered nothing",
    )
    response = call.result()

    assert answered.ok
    assert not response.ok
    assert "origin path answered nothing" in response.reason
    assert deployment.jobs() == []


async def test_a_denial_from_telegram_refuses_the_action(deployment: _Deployment) -> None:
    """A denial costs one message, and the words an operator typed reach the caller."""
    call = deployment.submit()
    delivered = await deployment.deliver()
    request_id = _request_id(delivered_content=delivered.content)

    reply = await deployment.answer(f"{DENY_COMMAND} {request_id} the change window is closed")
    response = call.result()

    assert "Denied" in reply
    assert not response.ok
    assert "change window" in response.reason
    assert deployment.jobs() == []


def _request_id(*, delivered_content: str) -> str:
    match = _REQUEST_ID_RE.search(delivered_content)
    assert match is not None, "the delivery must name the request id"
    return match.group(1)


def _wait_for_pending(deployment: _Deployment) -> Any:
    """Read the one suspended action off the operator socket."""
    client = OperatorClient(deployment.operator_socket, timeout_s=10.0)
    _wait_until(lambda: bool(client.pending()), hint=deployment._log)  # noqa: SLF001
    return client.pending()[0]


def _wait_until(predicate: Any, timeout_s: float = 20.0, hint: Any = None) -> None:
    """Wait for a condition the child produces, or fail with the child's own log."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    detail = f"\n{hint()}" if hint is not None else ""
    raise AssertionError(f"the condition never became true{detail}")
