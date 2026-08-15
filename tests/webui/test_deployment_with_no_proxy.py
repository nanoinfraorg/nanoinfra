# tests/webui/test_deployment_with_no_proxy.py
"""Item 8 of M2 (#65): a deployment with no proxy behaves exactly as it does today.

#47 added a verified identity, and it added no required config. The deployment that runs the
WebUI behind nothing keeps a shared token and keeps ``webui`` as the actor, and this file states
that as a check rather than leave it to a reviewer to trust.

Four properties carry the promise, and each one has a test below:

1. the token path still authorizes, and a request with no token still reaches no route;
2. ``operator_actor`` answers ``webui``;
3. an approver whose sender is ``webui`` still matches, so the shipped example config still works;
4. every audit record of an answered action writes ``webui``.

**Why ``webui`` is the honest answer here and not a gap.** Nothing authenticated a person, so a
record that named one would be an invention. The startup echo says so at every start, and
``gates.approvers`` names the path for this deployment. The cost is stated rather than hidden:
two operators behind one shared token are one credential, and that is the reason #47 exists.

The four properties are also a regression guard for the four surfaces of #64. Each surface now
reads an identity, and a deployment that has none must reach the same answer it reached before.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.datastructures import Headers

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.channels.websocket.runtime import WebSocketConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.executor.operator_socket import (
    ApprovalService,
    OperatorClient,
    bind_operator_socket,
    serve_operator_socket,
)
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.runtime import build_gate_runtime
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.store import ServerStore
from nanoinfra.webui.approvals_api import (
    APPROVAL_VALUES_HEADER,
    APPROVALS_ANSWER_PATH,
    ApprovalsOperatorSurface,
)
from nanoinfra.webui.gateway_services import build_gateway_services
from nanoinfra.webui.latch_api import (
    LATCH_CLEAR_PATH,
    LATCH_READ_PATH,
    LATCH_VALUES_HEADER,
    LatchOperatorSurface,
    operator_actor,
)

# The actor of a deployment that authenticated a shared token and nobody. It is the path itself.
_ACTOR = "webui"

_SESSION = "telegram:chat-1"
_CLASS = MUTATE_REMOTE
_COMMAND = "systemctl reload nginx"
_HOST = "10.0.1.5"
_SSH_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _gates() -> GatesConfig:
    """The approver list of a single-operator deployment, in the form it has always taken."""
    return GatesConfig.model_validate(
        {
            "approvers": [{"channel": "webui", "sender": _ACTOR}],
            "approvalPaths": ["webui", "telegram"],
            "approvalTimeoutS": 30,
        }
    )


def _handler(tmp_path: Path) -> Any:
    """One gateway with no ``trustedProxyAuth`` block at all, which is the case under test."""
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
    values_header: str = LATCH_VALUES_HEADER,
) -> Any:
    headers: list[tuple[str, str]] = []
    if token is not None:
        headers.append(("Authorization", f"Bearer {token}"))
    if values is not None:
        headers.append((values_header, json.dumps(values)))
    return SimpleNamespace(path=path, headers=Headers(headers))


def _connection() -> Any:
    return SimpleNamespace(
        remote_address=("127.0.0.1", 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


def _latched(root: Path) -> tuple[LatchOperatorSurface, Any]:
    runtime, controller = build_gate_runtime(GatesConfig(), root=root)
    runtime.refuse_action(
        session_id=_SESSION,
        capability_class=_CLASS,
        tool="execute_on_server",
        reason="unattended mutate.remote at group scope is denied",
        execution_context="automation",
    )
    return LatchOperatorSurface(controller=controller, audit=runtime.audit), runtime


# -- 1. the token path still authorizes ----------------------------------------------------


async def test_the_bootstrap_still_issues_the_token_that_authorizes_a_route(
    tmp_path: Path,
) -> None:
    """The whole token path, end to end, with no proxy anywhere in it.

    The bootstrap answers a local browser with a token, and that token reaches a route that
    needs one. A gateway that had started to require an assertion would fail here.
    """
    handler = _handler(tmp_path)
    surface, _ = _latched(tmp_path / "gates")
    handler.attach_latch_surface(surface)

    # The bootstrap answers a local browser, and a local browser is a loopback peer with a
    # loopback ``Host``. Both halves are the shipped rule, so the request states both.
    browser = SimpleNamespace(
        path="/webui/bootstrap", headers=Headers([("Host", "127.0.0.1:8765")])
    )
    bootstrap = await handler.dispatch(_connection(), browser)
    api_token = _body(bootstrap)["api_token"]
    response = await handler.dispatch(_connection(), _request(LATCH_READ_PATH, token=api_token))

    assert response.status_code == 200
    assert _body(response)["latches"] != []


async def test_a_request_with_no_token_still_reaches_no_route(tmp_path: Path) -> None:
    """The other half. No assertion exists to fall back on, so the 401 is unchanged."""
    handler = _handler(tmp_path)
    surface, _ = _latched(tmp_path / "gates")
    handler.attach_latch_surface(surface)

    response = await handler.dispatch(_connection(), _request(LATCH_READ_PATH))

    assert response.status_code == 401


# -- 2. the actor is the path --------------------------------------------------------------


async def test_the_actor_is_the_path_after_a_real_dispatch(tmp_path: Path) -> None:
    """The dispatch runs the identity seam on every request, and this deployment has none.

    The check reads the request that ``dispatch`` handled rather than a request built by hand,
    because the value the routes read is the one the dispatch wrote.
    """
    handler = _handler(tmp_path)
    request = _request(LATCH_READ_PATH)

    await handler.dispatch(_connection(), request)

    assert operator_actor(request) == _ACTOR


def test_the_actor_is_the_path_for_a_request_that_met_no_seam() -> None:
    """A request that reached no trusted-proxy decision at all answers the same way."""
    assert operator_actor(_request(LATCH_CLEAR_PATH)) == _ACTOR


# -- 3. an approver whose sender is the path still matches ---------------------------------


class _Executor:
    """The executor of this deployment, its audit log, and the operator socket beside it."""

    def __init__(self, tmp_path: Path) -> None:
        self.gates = _gates()
        self.audit = AuditStore(tmp_path / "gates")
        self.pending = PendingApprovalStore()
        self.tokens = ApprovalTokenStore()
        self.executor = Executor(
            workspace=tmp_path,
            gates_loader=lambda: self.gates,
            audit=self.audit,
            pending=self.pending,
            tokens=self.tokens,
        )
        service = ApprovalService(
            pending=self.pending,
            tokens=self.tokens,
            gates_loader=lambda: self.gates,
            audit=self.audit,
        )
        self.socket_path = tmp_path / "run" / "operator" / "e.op.sock"
        self.listener = bind_operator_socket(self.socket_path)
        threading.Thread(
            target=serve_operator_socket, args=(self.listener, service), daemon=True
        ).start()

    def surface(self) -> ApprovalsOperatorSurface:
        return ApprovalsOperatorSurface(client=OperatorClient(self.socket_path, timeout_s=5.0))

    async def wait_for_one_pending(self, timeout_s: float = 5.0) -> Any:
        for _ in range(int(timeout_s / 0.01)):
            items = self.pending.pending()
            if items:
                return items[0]
            await asyncio.sleep(0.01)
        raise AssertionError("the executor never suspended an action")


@pytest.fixture
def executor(tmp_path: Path):
    running = _Executor(tmp_path)
    yield running
    running.listener.close()


def _server_with_a_credential(tmp_path: Path) -> None:
    """A server that needs a stored credential, so the credential decision happens too."""
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "password", "providerId": "local", "value": "key-material"}
    )
    ServerStore(tmp_path).create(
        {
            "name": "prod-web-01",
            "providerId": "ssh",
            "config": {"host": _HOST},
            "secretRef": secret.id,
        }
    )


async def _approved_from_the_webui(executor: _Executor, handler: Any) -> Any:
    """One action, answered from the WebUI with a bare API token and no assertion."""
    handler.attach_approvals_surface(executor.surface())
    token = handler.tokens.issue_api_token(300)
    with patch(
        _SSH_BACKEND,
        new=AsyncMock(return_value=ExecutionResult(exit_code=0, output="reloaded", error=None)),
    ):
        task = asyncio.create_task(
            executor.executor.handle(
                ExecuteRequest(
                    server_id_or_name="prod-web-01",
                    command=_COMMAND,
                    session_id=_SESSION,
                    execution_context="interactive",
                    preview_requested=False,
                    timeout_s=None,
                    token_nonce=None,
                    origin_path="telegram",
                )
            )
        )
        suspended = await executor.wait_for_one_pending()
        answer = await handler.dispatch(
            _connection(),
            _request(
                APPROVALS_ANSWER_PATH,
                token=token,
                values={
                    "requestId": suspended.request_id,
                    "decision": "approve",
                    "targetDigest": suspended.target_digest,
                },
                values_header=APPROVAL_VALUES_HEADER,
            ),
        )
        response = await task
    return _body(answer), response


async def test_an_approver_named_by_the_path_still_answers_an_approval(
    executor: _Executor, tmp_path: Path
) -> None:
    """Property 3. ``{"channel": "webui", "sender": "webui"}`` is still an approver.

    The executor compares the actor against ``gates.approvers`` exactly, so a prefixed form that
    had become mandatory would refuse every approval of every deployment that has no proxy.
    """
    _server_with_a_credential(tmp_path)

    answer, response = await _approved_from_the_webui(executor, _handler(tmp_path))

    assert answer["ok"] is True, answer
    assert answer["actor"] == _ACTOR
    assert response.ok, response.reason


# -- 4. every audit record writes the path -------------------------------------------------


async def test_every_audit_record_of_an_answered_action_writes_the_path(
    executor: _Executor, tmp_path: Path
) -> None:
    """Property 4, across the approval record and the credential record it authorized.

    A ``None`` actor stays out of the comparison. The suspension record and the completion
    record hold no actor by design: nobody had answered yet, and the outcome names the decision
    it follows (#46).
    """
    _server_with_a_credential(tmp_path)

    await _approved_from_the_webui(executor, _handler(tmp_path))

    records = executor.audit.read_all()
    classes = {
        (str(record["capability_class"]), str(record["decision"])): record["actor"]
        for record in records
    }
    assert classes[(MUTATE_REMOTE, "allow")] == _ACTOR
    assert classes[(CREDENTIAL_ACCESS, "allow")] == _ACTOR
    assert {record["actor"] for record in records} - {None} == {_ACTOR}


async def test_the_latch_clear_record_writes_the_path(tmp_path: Path) -> None:
    """The fourth surface of #64 in this deployment, read back from the segment on disk."""
    root = tmp_path / "gates"
    handler = _handler(tmp_path)
    surface, runtime = _latched(root)
    handler.attach_latch_surface(surface)
    token = handler.tokens.issue_api_token(300)

    response = await handler.dispatch(
        _connection(),
        _request(
            LATCH_CLEAR_PATH,
            token=token,
            values={"sessionId": _SESSION, "capabilityClass": _CLASS},
        ),
    )

    assert _body(response)["actor"] == _ACTOR
    cleared = [record for record in runtime.audit.read_all() if record["decision"] == "cleared"]
    assert [record["actor"] for record in cleared] == [_ACTOR]
