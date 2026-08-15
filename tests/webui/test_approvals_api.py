# tests/webui/test_approvals_api.py
"""Item 25 (#27): the inbox that answers on the executor's operator socket.

#38 built the wait. An ``approve`` decision suspends the action, a pending store holds it, and
an operator answers on a second Unix socket. Nothing answered on that socket, so every approve
decision waited for the deadline and then refused. This file drives the surface that answers.

Two properties carry the security value, and neither one is a layout test.

The approve path must stay out of every tool import closure. The WebUI answers from inside the
gateway process, so the file mode on the operator socket protects nothing against the agent. The
import graph is the whole protection, and the last test in this file walks it.

The payload must reach the operator unchanged. #14 rendered those bytes, and ``target_digest``
binds them. A surface that summarised the payload would put the unfaithful summarization problem
inside the security path.

The rest covers the two routes, the actor that the server establishes, and every refusal an
operator can meet.
"""

from __future__ import annotations

import ast
import collections
import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.datastructures import Headers

from nanoinfra.channels.websocket.runtime import WebSocketConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.operator_socket import (
    ApprovalService,
    OperatorClient,
    bind_operator_socket,
    serve_operator_socket,
)
from nanoinfra.gates.pending import ApprovalState, PendingApprovalStore
from nanoinfra.gates.prompt import render_approval_prompt_for_hosts
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.webui.approvals_api import (
    APPROVAL_VALUES_HEADER,
    APPROVALS_ANSWER_PATH,
    APPROVALS_READ_PATH,
    ApprovalAnswerError,
    ApprovalsOperatorSurface,
    approval_values_from_request,
)
from nanoinfra.webui.gateway_services import build_gateway_services

_COMMAND = "systemctl restart nginx"
_HOSTS = tuple(f"web-{index:02d}" for index in range(1, 15))
_SESSION = "telegram:chat-1"
_ORIGIN = "telegram"

# The actor a bare-token deployment produces. ``operator_actor`` returns the path itself when no
# trusted proxy asserts a name, so the approver list has to name that value.
_ACTOR = "webui"

# Every module the agent can load, at any import depth. The approve path must stay out of it.
_TOOLS = Path("nanoinfra/agent/tools")
_FORBIDDEN_FOR_TOOLS = (
    "nanoinfra.webui.approvals_api",
    "nanoinfra.webui.ws_http",
)


def _gates(**over: Any) -> GatesConfig:
    """A policy that lets the WebUI answer a request from another path."""
    values: dict[str, Any] = {
        "approvers": [{"channel": "webui", "sender": _ACTOR}],
        "approvalPaths": ["webui", "telegram"],
    }
    values.update(over)
    return GatesConfig.model_validate(values)


# -- one in-process operator socket -----------------------------------------------------


@dataclass(slots=True)
class _Executor:
    """The half of #38 that this file drives: the socket, and the store behind it."""

    socket_path: Path
    pending: PendingApprovalStore

    def suspend(
        self,
        *,
        command: str = _COMMAND,
        hosts: tuple[str, ...] = _HOSTS,
        origin_path: str = _ORIGIN,
        scope: str = "group",
        timeout_s: float = 30.0,
    ) -> Any:
        """Register one suspended action, the way the executor registers one."""
        prompt = render_approval_prompt_for_hosts(command=command, hosts=hosts)
        return self.pending.create(
            session_id=_SESSION,
            origin_path=origin_path,
            execution_context="interactive",
            capability_class="mutate.remote",
            scope=scope,
            hosts=prompt.hosts,
            command=prompt.command,
            payload=prompt.text,
            target_digest=prompt.target_digest,
            timeout_s=timeout_s,
        )

    def client(self) -> OperatorClient:
        return OperatorClient(self.socket_path, timeout_s=5.0)

    def surface(self) -> ApprovalsOperatorSurface:
        return ApprovalsOperatorSurface(client=self.client())


@pytest.fixture
def executor(tmp_path: Path):
    """A factory for one in-process operator socket. Each call binds its own path."""
    listeners: list[socket.socket] = []

    def build(*, gates: GatesConfig | None = None, name: str = "e") -> _Executor:
        policy = gates if gates is not None else _gates()
        store = PendingApprovalStore()
        service = ApprovalService(
            pending=store,
            tokens=ApprovalTokenStore(),
            gates_loader=lambda: policy,
        )
        path = tmp_path / "run" / "operator" / f"{name}.op.sock"
        listener = bind_operator_socket(path)
        listeners.append(listener)
        threading.Thread(
            target=serve_operator_socket, args=(listener, service), daemon=True
        ).start()
        return _Executor(socket_path=path, pending=store)

    yield build
    for listener in listeners:
        listener.close()


def _await_state(store: PendingApprovalStore, request_id: str) -> Any:
    """Run the executor's own wait in a thread, and return the outcome it reads."""
    outcome: list[Any] = []
    thread = threading.Thread(target=lambda: outcome.append(store.wait(request_id)), daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    assert outcome, "the wait did not end"
    return outcome[0]


# -- the read surface -------------------------------------------------------------------


def test_an_empty_queue_reports_no_pending_action(executor: Any) -> None:
    payload = executor().surface().pending()

    assert payload["pending"] == []
    assert payload["count"] == 0
    assert payload["degraded"] is False


def test_a_pending_action_shows_every_resolved_host_name_and_the_count(executor: Any) -> None:
    """The acceptance case. Fourteen hosts read as fourteen names, and never as a label."""
    running = executor()
    running.suspend()

    entry = running.surface().pending()["pending"][0]

    assert entry["hostCount"] == 14
    assert entry["hosts"] == list(_HOSTS)
    assert "webservers" not in json.dumps(entry)


def test_the_read_carries_the_executor_payload_byte_for_byte(executor: Any) -> None:
    """The surface renders resolver output. It builds no summary of its own."""
    running = executor()
    approval = running.suspend()

    entry = running.surface().pending()["pending"][0]

    assert entry["payload"] == approval.payload
    assert entry["targetDigest"] == approval.target_digest


def test_the_read_reports_the_origin_path_and_the_approval_path(executor: Any) -> None:
    running = executor()
    running.suspend()

    payload = running.surface().pending()

    assert payload["approvalPath"] == "webui"
    assert payload["pending"][0]["originPath"] == _ORIGIN
    assert payload["pending"][0]["samePath"] is False


def test_a_request_from_the_webui_marks_the_same_path(executor: Any) -> None:
    """#13 refuses an approval on the origin path. The screen must say so before the click."""
    running = executor()
    running.suspend(origin_path="webui")

    assert running.surface().pending()["pending"][0]["samePath"] is True


def test_the_read_reports_the_remaining_time(executor: Any) -> None:
    """An operator reads a countdown, so the wire carries a remaining time and not a deadline."""
    running = executor()
    running.suspend(timeout_s=30.0)

    entry = running.surface().pending()["pending"][0]

    assert 0.0 < entry["expiresInS"] <= 30.0


def test_an_expired_action_leaves_the_queue(executor: Any) -> None:
    running = executor()
    running.suspend(timeout_s=0.05)
    time.sleep(0.1)

    assert running.surface().pending()["pending"] == []


def test_an_unreachable_executor_reports_degraded_and_never_an_empty_queue(
    tmp_path: Path,
) -> None:
    """An empty list must not read as "no action waits". The socket may be down instead."""
    surface = ApprovalsOperatorSurface(
        client=OperatorClient(tmp_path / "absent.op.sock", timeout_s=1.0)
    )

    payload = surface.pending()

    assert payload["degraded"] is True
    assert payload["pending"] == []
    assert payload["count"] == 0


# -- the answer surface -----------------------------------------------------------------


def test_an_approval_ends_the_wait_and_names_the_actor(executor: Any) -> None:
    running = executor()
    approval = running.suspend()

    answer = running.surface().answer(
        {
            "requestId": approval.request_id,
            "decision": "approve",
            "targetDigest": approval.target_digest,
        },
        actor=_ACTOR,
    )

    assert answer["ok"] is True
    assert answer["refusal"] is None
    outcome = _await_state(running.pending, approval.request_id)
    assert outcome.state is ApprovalState.APPROVED
    assert outcome.actor == _ACTOR
    assert outcome.approval_path == "webui"


def test_a_denial_needs_no_digest_so_it_never_costs_more_than_an_approval(
    executor: Any,
) -> None:
    """Rule 4. A deny that cost an extra step would make a human the rate limiter."""
    running = executor()
    approval = running.suspend()

    answer = running.surface().answer(
        {"requestId": approval.request_id, "decision": "deny", "reason": "not today"},
        actor=_ACTOR,
    )

    assert answer["ok"] is True
    outcome = _await_state(running.pending, approval.request_id)
    assert outcome.state is ApprovalState.DENIED
    assert outcome.reason == "not today"


def test_an_approval_of_other_bytes_refuses_and_leaves_the_action_pending(
    executor: Any,
) -> None:
    """The digest is the whole point. A mismatch authorizes nothing and costs nothing."""
    running = executor()
    approval = running.suspend()

    answer = running.surface().answer(
        {
            "requestId": approval.request_id,
            "decision": "approve",
            "targetDigest": "sha256:0000",
        },
        actor=_ACTOR,
    )

    assert answer["ok"] is False
    assert answer["refusal"] == "digest_mismatch"
    assert running.surface().pending()["count"] == 1


def test_a_second_answer_reads_as_already_answered(executor: Any) -> None:
    running = executor()
    approval = running.suspend()
    values = {"requestId": approval.request_id, "decision": "deny"}
    surface = running.surface()
    surface.answer(values, actor=_ACTOR)

    answer = surface.answer(values, actor=_ACTOR)

    assert answer["refusal"] == "already_answered"


def test_an_answer_after_the_deadline_reads_as_expired(executor: Any) -> None:
    running = executor()
    approval = running.suspend(timeout_s=0.05)
    time.sleep(0.1)

    answer = running.surface().answer(
        {"requestId": approval.request_id, "decision": "deny"}, actor=_ACTOR
    )

    assert answer["refusal"] == "expired"


def test_an_answer_for_an_unknown_request_says_so(executor: Any) -> None:
    answer = executor().surface().answer(
        {"requestId": "no-such-id", "decision": "deny"}, actor=_ACTOR
    )

    assert answer["refusal"] == "unknown_request"


def test_an_answer_on_the_origin_path_refuses_and_names_the_rule(executor: Any) -> None:
    """The same-path case from #13. One compromised account must not supply both halves."""
    running = executor()
    approval = running.suspend(origin_path="webui")

    answer = running.surface().answer(
        {
            "requestId": approval.request_id,
            "decision": "approve",
            "targetDigest": approval.target_digest,
        },
        actor=_ACTOR,
    )

    assert answer["refusal"] == "same_path"


def test_an_actor_outside_the_approver_list_refuses(executor: Any) -> None:
    running = executor(gates=_gates(approvers=[{"channel": "webui", "sender": "operator-1"}]))
    approval = running.suspend()

    answer = running.surface().answer(
        {
            "requestId": approval.request_id,
            "decision": "approve",
            "targetDigest": approval.target_digest,
        },
        actor=_ACTOR,
    )

    assert answer["refusal"] == "not_an_approver"


def test_a_deployment_with_one_path_names_the_missing_second_path(executor: Any) -> None:
    running = executor(gates=_gates(approvalPaths=["telegram"]))
    approval = running.suspend()

    answer = running.surface().answer(
        {"requestId": approval.request_id, "decision": "deny"}, actor=_ACTOR
    )

    assert answer["refusal"] == "no_second_path"


def test_the_browser_cannot_name_its_own_actor(executor: Any) -> None:
    """Rule 2. The actor comes from the server-side session, and never from the payload."""
    running = executor(gates=_gates(approvers=[{"channel": "webui", "sender": "operator-1"}]))
    approval = running.suspend()

    answer = running.surface().answer(
        {
            "requestId": approval.request_id,
            "decision": "approve",
            "targetDigest": approval.target_digest,
            "actor": "operator-1",
            "approvalPath": "telegram",
        },
        actor=_ACTOR,
    )

    assert answer["refusal"] == "not_an_approver"
    assert answer["actor"] == _ACTOR


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"decision": "approve"},
        {"requestId": "abc"},
        {"requestId": "", "decision": "deny"},
        {"requestId": "abc", "decision": "maybe"},
        {"requestId": "abc", "decision": 7},
        {"requestId": "abc", "decision": "approve"},
    ],
)
def test_an_answer_needs_a_request_a_decision_and_a_digest(
    executor: Any, values: dict[str, Any]
) -> None:
    """A missing field is a client fault. The route answers 400, and the action stays pending."""
    with pytest.raises(ApprovalAnswerError):
        executor().surface().answer(values, actor=_ACTOR)


def test_an_unreachable_executor_reports_a_failed_answer(tmp_path: Path) -> None:
    surface = ApprovalsOperatorSurface(
        client=OperatorClient(tmp_path / "absent.op.sock", timeout_s=1.0)
    )

    answer = surface.answer({"requestId": "abc", "decision": "deny"}, actor=_ACTOR)

    assert answer["ok"] is False
    assert answer["degraded"] is True


# -- the shape of the surface -----------------------------------------------------------


def test_the_surface_refuses_anything_that_is_not_the_operator_client() -> None:
    """A request carries strings. Those must fail at the door, the way #28 fails them."""
    with pytest.raises(TypeError):
        ApprovalsOperatorSurface(client={"requestId": "abc"})


def test_the_surface_exposes_the_two_operations_and_nothing_else(executor: Any) -> None:
    """A route holds this object. It must not be able to hand the client on."""
    surface = executor().surface()

    assert sorted(name for name in dir(surface) if not name.startswith("_")) == [
        "answer",
        "pending",
    ]


# -- the routes -------------------------------------------------------------------------


def _handler(tmp_path: Path) -> Any:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    services = build_gateway_services(
        config=WebSocketConfig.model_validate(
            {
                "enabled": True,
                "allowFrom": ["*"],
                "host": "127.0.0.1",
                "port": 8765,
                "path": "/",
            }
        ),
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path / "workspace",
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return services.http


def _request(
    path: str,
    *,
    token: str | None = None,
    values: dict[str, Any] | None = None,
    method: str | None = None,
) -> Any:
    headers: list[tuple[str, str]] = []
    if token is not None:
        headers.append(("Authorization", f"Bearer {token}"))
    if values is not None:
        headers.append((APPROVAL_VALUES_HEADER, json.dumps(values)))
    request = SimpleNamespace(path=path, headers=Headers(headers))
    if method is not None:
        setattr(request, "method", method)
    return request


def _connection() -> Any:
    return SimpleNamespace(
        remote_address=("127.0.0.1", 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


async def test_both_routes_refuse_a_request_with_no_token(
    tmp_path: Path, executor: Any
) -> None:
    handler = _handler(tmp_path)
    running = executor()
    running.suspend()
    handler.attach_approvals_surface(running.surface())

    for path in (APPROVALS_READ_PATH, APPROVALS_ANSWER_PATH):
        response = await handler.dispatch(_connection(), _request(path))
        assert response.status_code == 401, path
    assert running.surface().pending()["count"] == 1


async def test_both_routes_answer_503_until_the_gateway_attaches_the_surface(
    tmp_path: Path,
) -> None:
    """A WebUI with no operator client must say so, and never read as an empty queue."""
    handler = _handler(tmp_path)
    token = handler.tokens.issue_api_token(300)

    for path in (APPROVALS_READ_PATH, APPROVALS_ANSWER_PATH):
        response = await handler.dispatch(_connection(), _request(path, token=token))
        assert response.status_code == 503, path


async def test_the_read_route_returns_the_pending_action(
    tmp_path: Path, executor: Any
) -> None:
    handler = _handler(tmp_path)
    running = executor()
    approval = running.suspend()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(), _request(APPROVALS_READ_PATH, token=token)
    )

    assert response.status_code == 200
    entry = _body(response)["pending"][0]
    assert entry["requestId"] == approval.request_id
    assert entry["payload"] == approval.payload
    assert entry["hosts"] == list(_HOSTS)


async def test_the_answer_route_approves_the_action(tmp_path: Path, executor: Any) -> None:
    handler = _handler(tmp_path)
    running = executor()
    approval = running.suspend()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(),
        _request(
            APPROVALS_ANSWER_PATH,
            token=token,
            values={
                "requestId": approval.request_id,
                "decision": "approve",
                "targetDigest": approval.target_digest,
            },
        ),
    )

    assert response.status_code == 200
    assert _body(response)["ok"] is True
    assert _body(response)["actor"] == "webui"
    assert _await_state(running.pending, approval.request_id).state is ApprovalState.APPROVED


async def test_the_answer_route_denies_the_action(tmp_path: Path, executor: Any) -> None:
    handler = _handler(tmp_path)
    running = executor()
    approval = running.suspend()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(),
        _request(
            APPROVALS_ANSWER_PATH,
            token=token,
            values={"requestId": approval.request_id, "decision": "deny"},
        ),
    )

    assert response.status_code == 200
    assert _body(response)["ok"] is True
    assert _await_state(running.pending, approval.request_id).state is ApprovalState.DENIED


async def test_the_answer_route_rejects_a_payload_with_no_request_id(
    tmp_path: Path, executor: Any
) -> None:
    handler = _handler(tmp_path)
    running = executor()
    running.suspend()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(),
        _request(APPROVALS_ANSWER_PATH, token=token, values={"decision": "deny"}),
    )

    assert response.status_code == 400
    assert running.surface().pending()["count"] == 1


async def test_the_answer_route_rejects_a_missing_values_header(
    tmp_path: Path, executor: Any
) -> None:
    handler = _handler(tmp_path)
    running = executor()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(), _request(APPROVALS_ANSWER_PATH, token=token)
    )

    assert response.status_code == 400


async def test_the_read_route_refuses_a_write_method(tmp_path: Path, executor: Any) -> None:
    handler = _handler(tmp_path)
    running = executor()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(), _request(APPROVALS_READ_PATH, token=token, method="DELETE")
    )

    assert response.status_code == 405


async def test_the_answer_route_refuses_a_read_method(tmp_path: Path, executor: Any) -> None:
    handler = _handler(tmp_path)
    running = executor()
    approval = running.suspend()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(),
        _request(
            APPROVALS_ANSWER_PATH,
            token=token,
            method="GET",
            values={"requestId": approval.request_id, "decision": "deny"},
        ),
    )

    assert response.status_code == 405
    assert running.surface().pending()["count"] == 1


async def test_the_answer_route_takes_the_actor_from_the_trusted_proxy(
    tmp_path: Path, executor: Any
) -> None:
    """A named operator reaches the audit record. The name comes from the peer check."""
    handler = _handler(tmp_path)
    running = executor(
        gates=_gates(approvers=[{"channel": "webui", "sender": "webui:ops@example.com"}])
    )
    approval = running.suspend()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)
    request = _request(
        APPROVALS_ANSWER_PATH,
        token=token,
        values={
            "requestId": approval.request_id,
            "decision": "approve",
            "targetDigest": approval.target_digest,
        },
    )
    request.headers["Cf-Access-Authenticated-User-Email"] = "ops@example.com"
    # The peer check runs inside dispatch, so the test configures a real trusted network. A
    # header alone must not name an operator.
    handler.config.trusted_proxy_auth = SimpleNamespace(
        assertion_header="Cf-Access-Authenticated-User-Email",
        _trusted_peer_networks=(ipaddress.ip_network("127.0.0.1/32"),),
    )

    response = await handler.dispatch(_connection(), request)

    assert _body(response)["actor"] == "webui:ops@example.com"
    assert _await_state(running.pending, approval.request_id).actor == "webui:ops@example.com"


async def test_an_untrusted_peer_cannot_name_the_operator(
    tmp_path: Path, executor: Any
) -> None:
    """Rule 2 at the route. The assertion header counts only after the peer check passes."""
    handler = _handler(tmp_path)
    running = executor(
        gates=_gates(approvers=[{"channel": "webui", "sender": "webui:ops@example.com"}])
    )
    approval = running.suspend()
    handler.attach_approvals_surface(running.surface())
    token = handler.tokens.issue_api_token(300)
    request = _request(
        APPROVALS_ANSWER_PATH,
        token=token,
        values={
            "requestId": approval.request_id,
            "decision": "approve",
            "targetDigest": approval.target_digest,
        },
    )
    request.headers["Cf-Access-Authenticated-User-Email"] = "ops@example.com"
    handler.config.trusted_proxy_auth = SimpleNamespace(
        assertion_header="Cf-Access-Authenticated-User-Email",
        _trusted_peer_networks=(ipaddress.ip_network("10.9.9.9/32"),),
    )

    response = await handler.dispatch(_connection(), request)

    assert _body(response)["actor"] == "webui"
    assert _body(response)["refusal"] == "not_an_approver"
    assert running.surface().pending()["count"] == 1


def test_the_answer_values_come_from_the_header() -> None:
    values = {"requestId": "abc", "decision": "deny"}

    assert approval_values_from_request(_request(APPROVALS_ANSWER_PATH, values=values)) == values


@pytest.mark.parametrize("raw", ["", "not json", '"a string"', "[1, 2]"])
def test_a_malformed_values_header_is_an_invalid_payload(raw: str) -> None:
    request = SimpleNamespace(
        path=APPROVALS_ANSWER_PATH, headers=Headers([(APPROVAL_VALUES_HEADER, raw)])
    )

    assert approval_values_from_request(request) is None


# -- how the client gets there ----------------------------------------------------------


async def test_the_gateway_hands_the_operator_client_to_the_webui_handler(
    tmp_path: Path, executor: Any
) -> None:
    """The gateway owns the client, so the gateway is the only caller that hands it on."""
    from nanoinfra.cli.gateway_runtime import _attach_approvals_operator_surface

    handler = _handler(tmp_path)
    running = executor()
    approval = running.suspend()
    websocket = SimpleNamespace(gateway=SimpleNamespace(http=handler))

    _attach_approvals_operator_surface(
        SimpleNamespace(channels={"websocket": websocket}), running.client()
    )

    token = handler.tokens.issue_api_token(300)
    response = await handler.dispatch(
        _connection(), _request(APPROVALS_READ_PATH, token=token)
    )
    assert response.status_code == 200
    assert _body(response)["pending"][0]["requestId"] == approval.request_id


def test_a_gateway_with_no_webui_channel_still_boots(tmp_path: Path, executor: Any) -> None:
    """No WebUI means no inbox. That is a warning, and never a failed boot."""
    from nanoinfra.cli.gateway_runtime import _attach_approvals_operator_surface

    _attach_approvals_operator_surface(
        SimpleNamespace(channels={"telegram": SimpleNamespace()}), executor().client()
    )


def test_the_gateway_client_points_at_the_operator_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The client dials the socket beside the execute socket, and never the execute socket."""
    from nanoinfra.cli.gateway_runtime import _operator_client_for_gateway

    execute = tmp_path / "run" / "executor.sock"
    monkeypatch.setenv("NANOINFRA_EXECUTOR_SOCKET", str(execute))
    monkeypatch.delenv("NANOINFRA_OPERATOR_SOCKET", raising=False)

    client = _operator_client_for_gateway()

    assert client.socket_path != execute
    assert client.socket_path.parent == execute.parent / "operator"


def test_the_gateway_client_follows_the_deployment_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """entrypoint.sh names this path, so the gateway must read the same variable."""
    from nanoinfra.cli.gateway_runtime import _operator_client_for_gateway

    named = tmp_path / "elsewhere" / "op.sock"
    monkeypatch.setenv("NANOINFRA_OPERATOR_SOCKET", str(named))

    assert _operator_client_for_gateway().socket_path == named


def test_the_gateway_wires_the_inbox() -> None:
    """A route that answered 503 in every deployment would be a route and not a feature."""
    import inspect

    from nanoinfra.cli import gateway_runtime

    source = inspect.getsource(gateway_runtime)

    assert "_attach_approvals_operator_surface(channels, _operator_client_for_gateway())" in source


# -- the property that makes the split real ---------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every module name a file imports, at any depth, including inside a function."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _first_party_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in Path("nanoinfra").rglob("*.py"):
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _tool_import_closure() -> set[str]:
    """Every first-party module the tool package reaches, transitively.

    A one-level check passes while a two-hop path stays open, so this walks the whole graph.
    """
    modules = _first_party_modules()
    graph = {
        name: {edge for edge in _imported_modules(path) if edge in modules}
        for name, path in modules.items()
    }
    seeds = [name for name in modules if name.startswith("nanoinfra.agent.tools")]
    seen = set(seeds)
    queue = collections.deque(seeds)
    while queue:
        for edge in graph.get(queue.popleft(), ()):
            if edge not in seen:
                seen.add(edge)
                queue.append(edge)
    return seen


def test_no_tool_module_reaches_the_approve_path() -> None:
    """The security property of #27, as a check rather than a promise.

    The WebUI answers on the operator socket from inside the gateway process. The mode on that
    socket therefore protects nothing against the agent, because both run as one account. The
    import graph is what stops a tool from holding this client.
    """
    closure = _tool_import_closure()

    assert [name for name in _FORBIDDEN_FOR_TOOLS if name in closure] == []


def test_no_tool_module_imports_the_approvals_surface_directly() -> None:
    offenders = [
        str(path)
        for path in _TOOLS.rglob("*.py")
        if "nanoinfra.webui.approvals_api" in _imported_modules(path)
    ]

    assert offenders == []
