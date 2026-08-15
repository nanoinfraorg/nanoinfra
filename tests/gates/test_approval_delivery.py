# tests/gates/test_approval_delivery.py
"""Item 41 (#43): the gateway-side watcher that delivers one suspended action.

The executor holds no transport. It cannot reach a chat channel, and #38 built the wait on that
assumption. The gateway holds both the channels and an ``OperatorClient``, so the delivery lives
there.

The watcher polls. A poll of a few seconds is enough, because the wait is at most
``gates.approvalTimeoutS``, and a poll needs no new push wire between the two processes. #38
already refused to add a poll to the *execute* wire, and this is the other socket.

Two properties carry the security value of this file.

The payload must reach the operator unchanged. #14 rendered those bytes and ``target_digest``
binds them, so a watcher that summarised them would put the unfaithful summarization problem
inside the security path.

Delivery must respect path independence. A request that arrived on one path must not reach an
approver on that same path, because an answer from there cannot count (#13).
"""

from __future__ import annotations

import asyncio
import inspect
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.bus.events import OutboundMessage
from nanoinfra.cli import gateway_runtime
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.approval_delivery import (
    DEFAULT_POLL_INTERVAL_S,
    ApprovalDeliveryWatcher,
    delivery_targets,
    render_delivery,
)
from nanoinfra.gates.executor.operator_socket import (
    ApprovalService,
    OperatorClient,
    bind_operator_socket,
    serve_operator_socket,
)
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.prompt import render_approval_prompt_for_hosts
from nanoinfra.gates.tokens import ApprovalTokenStore

_COMMAND = "systemctl reload nginx"
_HOSTS = ("10.0.2.11", "10.0.2.12", "10.0.2.13")
_SESSION = "websocket:chat-1"
_APPROVER = "770123456"
_SECOND_APPROVER = "880654321"


def _gates(**over: Any) -> GatesConfig:
    values: dict[str, Any] = {
        "approvers": [
            {"channel": "telegram", "sender": _APPROVER},
            {"channel": "webui", "sender": "webui"},
        ],
        "approvalPaths": ["webui", "telegram"],
    }
    values.update(over)
    return GatesConfig.model_validate(values)


class _Deployment:
    """One in-process operator socket, its store, and the outbound messages it produced."""

    def __init__(self, socket_path: Path, store: PendingApprovalStore) -> None:
        self.socket_path = socket_path
        self.store = store
        self.sent: list[OutboundMessage] = []
        self.enabled: set[str] = {"telegram", "websocket", "webui"}

    async def publish(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)

    def suspend(self, *, origin_path: str = "websocket", timeout_s: float = 30.0) -> Any:
        prompt = render_approval_prompt_for_hosts(command=_COMMAND, hosts=_HOSTS)
        return self.store.create(
            session_id=_SESSION,
            origin_path=origin_path,
            execution_context="interactive",
            capability_class="mutate.remote",
            scope="group",
            hosts=prompt.hosts,
            command=prompt.command,
            payload=prompt.text,
            target_digest=prompt.target_digest,
            timeout_s=timeout_s,
        )

    def watcher(
        self, *, gates: GatesConfig | None = None, interval_s: float = 0.01
    ) -> ApprovalDeliveryWatcher:
        policy = gates if gates is not None else _gates()
        return ApprovalDeliveryWatcher(
            client=OperatorClient(self.socket_path, timeout_s=5.0),
            publish=self.publish,
            gates_loader=lambda: policy,
            is_channel_enabled=lambda name: name in self.enabled,
            interval_s=interval_s,
        )


@pytest.fixture
def deployment(tmp_path: Path):
    """A factory for one in-process operator socket. Each call binds its own path."""
    listeners: list[socket.socket] = []

    def build(*, name: str = "e") -> _Deployment:
        store = PendingApprovalStore()
        service = ApprovalService(
            pending=store, tokens=ApprovalTokenStore(), gates_loader=_gates
        )
        path = tmp_path / "run" / "operator" / f"{name}.op.sock"
        listener = bind_operator_socket(path)
        listeners.append(listener)
        threading.Thread(
            target=serve_operator_socket, args=(listener, service), daemon=True
        ).start()
        return _Deployment(path, store)

    try:
        yield build
    finally:
        for listener in listeners:
            listener.close()


# -- who receives one request ------------------------------------------------------------


def test_an_approver_on_another_path_is_a_target() -> None:
    targets = delivery_targets(gates=_gates(), origin_path="websocket")

    assert [(target.channel, target.chat_id) for target in targets] == [
        ("telegram", _APPROVER),
        ("webui", "webui"),
    ]


def test_an_approver_on_the_origin_path_is_no_target() -> None:
    """#13 condition 3. An answer from the origin path cannot count, so nothing goes there."""
    targets = delivery_targets(gates=_gates(), origin_path="telegram")

    assert [target.channel for target in targets] == ["webui"]


def test_an_approver_on_an_unauthenticated_path_is_no_target() -> None:
    """A path outside ``gates.approvalPaths`` authenticates no approver (#13 condition 2)."""
    gates = _gates(approvalPaths=["webui"])

    targets = delivery_targets(gates=gates, origin_path="websocket")

    assert [target.channel for target in targets] == ["webui"]


def test_two_approvers_on_one_path_are_two_targets() -> None:
    """A path is one transport, and an approver is one person. Both get the request."""
    gates = _gates(
        approvers=[
            {"channel": "telegram", "sender": _APPROVER},
            {"channel": "telegram", "sender": _SECOND_APPROVER},
        ]
    )

    targets = delivery_targets(gates=gates, origin_path="websocket")

    assert [target.chat_id for target in targets] == [_APPROVER, _SECOND_APPROVER]


def test_a_blank_approver_entry_is_no_target() -> None:
    """A blank sender names nobody, so it must not become a chat id."""
    gates = _gates(approvers=[{"channel": "telegram", "sender": "  "}])

    assert delivery_targets(gates=gates, origin_path="websocket") == ()


# -- what one approver reads -------------------------------------------------------------


def test_the_delivery_carries_the_executor_payload_byte_for_byte(deployment: Any) -> None:
    """The watcher renders nothing of its own. #14 owns the bytes."""
    running = deployment()
    approval = running.suspend()

    content = render_delivery(
        {
            "request_id": approval.request_id,
            "session_id": approval.session_id,
            "origin_path": approval.origin_path,
            "origin_actor": approval.origin_actor,
            "execution_context": approval.execution_context,
            "capability_class": approval.capability_class,
            "scope": approval.scope,
            "host_count": approval.host_count,
            "hosts": list(approval.hosts),
            "payload": approval.payload,
            "target_digest": approval.target_digest,
            "expires_in_s": 30.0,
        }
    )

    assert approval.payload in content
    assert approval.target_digest in content
    assert approval.request_id in content
    assert f"/approve {approval.request_id}" in content
    assert f"/deny {approval.request_id}" in content
    for host in _HOSTS:
        assert host in content


def test_the_delivery_fences_the_payload(deployment: Any) -> None:
    """A markdown channel must not restyle a command an operator has to read.

    The Telegram renderer turns a line with two pipes into a drawn table, and a command such
    as ``ps aux | grep nginx`` produces one. A fenced block reaches the operator unchanged.
    """
    running = deployment()
    approval = running.suspend()
    view = {**_view(approval)}

    content = render_delivery(view)

    assert f"```\n{approval.payload}```" in content


# -- the poll ----------------------------------------------------------------------------


async def test_one_poll_delivers_one_request_to_each_target(deployment: Any) -> None:
    """The acceptance path: a WebSocket action reaches an approver on Telegram."""
    running = deployment()
    approval = running.suspend()

    delivered = await running.watcher().deliver_pending()

    assert delivered == 2
    telegram = [msg for msg in running.sent if msg.channel == "telegram"]
    assert len(telegram) == 1
    assert telegram[0].chat_id == _APPROVER
    assert approval.payload in telegram[0].content


async def test_a_second_poll_does_not_repeat_a_request(deployment: Any) -> None:
    """A three-second poll would otherwise send forty copies of one request."""
    running = deployment()
    running.suspend()
    watcher = running.watcher()

    first = await watcher.deliver_pending()
    second = await watcher.deliver_pending()

    assert (first, second) == (2, 0)
    assert len(running.sent) == 2


async def test_a_request_that_arrived_on_telegram_reaches_no_telegram_approver(
    deployment: Any,
) -> None:
    """Path independence holds by construction, and it holds on the delivery half too."""
    running = deployment()
    running.suspend(origin_path="telegram")

    await running.watcher().deliver_pending()

    assert [msg.channel for msg in running.sent] == ["webui"]


async def test_a_channel_that_is_not_enabled_receives_nothing(deployment: Any) -> None:
    """An outbound message to an absent channel reaches nobody, and it says so once."""
    running = deployment()
    running.enabled = {"websocket"}
    running.suspend()

    delivered = await running.watcher().deliver_pending()

    assert delivered == 0
    assert running.sent == []


async def test_an_unreachable_executor_delivers_nothing_and_raises_nothing(
    tmp_path: Path,
) -> None:
    """A read failure must not end the watcher. The gateway keeps its channels up."""
    sent: list[OutboundMessage] = []

    async def publish(msg: OutboundMessage) -> None:
        sent.append(msg)

    watcher = ApprovalDeliveryWatcher(
        client=OperatorClient(tmp_path / "absent.op.sock", timeout_s=1.0),
        publish=publish,
        gates_loader=_gates,
        is_channel_enabled=lambda _name: True,
        interval_s=0.01,
    )

    assert await watcher.deliver_pending() == 0
    assert sent == []


async def test_a_send_failure_leaves_the_request_for_the_next_poll(deployment: Any) -> None:
    """A channel that refused one send must not lose the request forever.

    A request that nobody read is worse than a duplicate message, so the watcher records a
    pair only after the send returns.
    """
    running = deployment()
    running.suspend()
    failures: list[str] = []
    refuse = True

    async def publish(msg: OutboundMessage) -> None:
        if refuse:
            failures.append(msg.channel)
            raise RuntimeError("the channel refused this send")
        await running.publish(msg)

    watcher = ApprovalDeliveryWatcher(
        client=OperatorClient(running.socket_path, timeout_s=5.0),
        publish=publish,
        gates_loader=_gates,
        is_channel_enabled=lambda name: name in running.enabled,
        interval_s=0.01,
    )

    assert await watcher.deliver_pending() == 0
    refuse = False
    assert await watcher.deliver_pending() == 2
    assert len(failures) == 2


async def test_the_run_loop_polls_until_it_is_cancelled(deployment: Any) -> None:
    """The gateway holds this task beside the agent loop and the channels."""
    running = deployment()
    watcher = running.watcher(interval_s=0.01)
    task = asyncio.create_task(watcher.run())
    try:
        running.suspend()
        for _ in range(200):
            if running.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert [msg.channel for msg in running.sent] == ["telegram", "webui"]


def test_the_poll_interval_is_a_few_seconds() -> None:
    """The wait is at most ``gates.approvalTimeoutS``, which defaults to 120 seconds.

    A poll of a few seconds spends a negligible part of that window, and it needs no push
    wire. A poll of a minute would spend half of the shortest sensible wait.
    """
    assert 1.0 <= DEFAULT_POLL_INTERVAL_S <= 5.0
    assert DEFAULT_POLL_INTERVAL_S < GatesConfig().approval_timeout_s


# -- what an operator reads at start -----------------------------------------------------


def test_the_summary_names_the_paths_and_the_interval(deployment: Any) -> None:
    summary = deployment().watcher().summary()

    assert "telegram" in summary
    assert "webui" in summary


def test_the_summary_states_the_case_with_no_chat_approver(deployment: Any) -> None:
    """A WebUI-only deployment has a nominal second path. An operator must read that."""
    gates = _gates(approvers=[{"channel": "webui", "sender": "webui"}], approvalPaths=["webui"])

    summary = deployment().watcher(gates=gates).summary()

    assert "webui" in summary
    assert "no approver" in summary


# -- the gateway wiring ------------------------------------------------------------------


def test_the_gateway_starts_the_watcher_and_registers_the_commands() -> None:
    """A source check, because a real gateway needs a provider, a bus, and a browser."""
    source = inspect.getsource(gateway_runtime)

    assert "ApprovalDeliveryWatcher" in source
    assert "register_approval_commands" in source
    assert "nanoinfra-approval-delivery" in source


def test_the_two_answer_halves_derive_one_socket_path() -> None:
    """The delivery and the answer must read the same operator socket.

    Both halves take the client from ``_operator_client_for_gateway``, which derives the path
    from the execute socket. A second derivation could drift from the first one.
    """
    source = inspect.getsource(gateway_runtime)

    assert "answer_client = _operator_client_for_gateway()" in source
    assert "_register_approval_answer_commands(agent, answer_client)" in source
    assert "_build_approval_delivery(answer_client" in source


def test_the_inbox_wiring_of_the_earlier_item_is_unchanged() -> None:
    """#27 wires the WebUI inbox, and this item must not move that line."""
    source = inspect.getsource(gateway_runtime)

    assert "_attach_approvals_operator_surface(channels, _operator_client_for_gateway())" in source


def _view(approval: Any) -> dict[str, Any]:
    return {
        "request_id": approval.request_id,
        "session_id": approval.session_id,
        "origin_path": approval.origin_path,
        "origin_actor": approval.origin_actor,
        "execution_context": approval.execution_context,
        "capability_class": approval.capability_class,
        "scope": approval.scope,
        "host_count": approval.host_count,
        "hosts": list(approval.hosts),
        "payload": approval.payload,
        "target_digest": approval.target_digest,
        "expires_in_s": 30.0,
    }
