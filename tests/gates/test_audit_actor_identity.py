# tests/gates/test_audit_actor_identity.py
"""Item 7 of M2 (#64): every audit record that names an actor names the person.

Four surfaces write the ``actor`` field of a gate audit record. Each one has to name the person
where a person is verified, because #16 exists so a reviewer months later can answer who decided.

| Surface | Where the actor comes from |
|---|---|
| the latch clear (#28) | ``operator_actor``, from the identity the gateway resolved |
| the approval answer (#27) | ``operator_actor``, and the executor records the outcome |
| the chat answer (#43) | the sender id the channel itself authenticated |
| the credential decision (#39) | the approval that released the action |

Two of the four read one function, so #63 carries them both. The chat answer is a different
shape: a channel authenticates its own sender and no assertion reaches it. The credential
decision writes no actor of its own at all, because one action costs one human decision, so it
can only be right while the approval is right. A test states that rather than assume it.

**The record on disk is the object under test.** A surface that reported an actor the log did not
hold would leave a reviewer with no account of who decided, and the two answers are written by
different processes. So every test below reads the segment back.

The gateway is built in process with a static key set, so this file signs its own tokens and
needs no identity provider, no proxy and no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from websockets.datastructures import Headers

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.channels.websocket.runtime import WebSocketConfig
from nanoinfra.command.approvals import ApprovalAnswerSurface, approval_actor
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
    LATCH_VALUES_HEADER,
    LatchOperatorSurface,
)

_ISSUER = "https://idp.example/realms/homelab"
_AUDIENCE = "nanoinfra-gateway"
_KID = "key-2026-08"
_HEADER = "X-Access-Token"

# The person, and the two spellings of them. The claim is what the token carries. The actor is
# what the WebUI path makes of it, and ``gates.approvers`` names that second form.
_PERSON = "operator@example.com"
_WEBUI_ACTOR = f"webui:{_PERSON}"

# The chat operator. Telegram builds ``<account-id>|<username>``, and a username changes at will,
# so the account id is the half the channel authenticates.
_TELEGRAM_SENDER = "43110|opsuser"
_TELEGRAM_ACTOR = "43110"

_SESSION = "telegram:chat-1"
_CLASS = MUTATE_REMOTE
_COMMAND = "systemctl reload nginx"
_HOST = "10.0.1.5"
_SECRET_VALUE = "s3cr3t-key-material"
_SSH_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _gates(**over: Any) -> GatesConfig:
    """A deployment with two paths, and one named person on each of them."""
    raw: dict[str, Any] = {
        "approvers": [
            {"channel": "webui", "sender": _WEBUI_ACTOR},
            {"channel": "telegram", "sender": _TELEGRAM_ACTOR},
        ],
        "approvalPaths": ["webui", "telegram"],
        "approvalTimeoutS": 30,
    }
    raw.update(over)
    return GatesConfig.model_validate(raw)


# -- one signed assertion ------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def static_jwks(signing_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    numbers = signing_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": _KID,
                "alg": "RS256",
                "use": "sig",
                "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _token(key: rsa.RSAPrivateKey, *, email: str = _PERSON) -> str:
    claims = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "exp": time.time() + 300,
        "iat": time.time() - 5,
        "email": email,
    }
    head = _b64url(json.dumps({"alg": "RS256", "kid": _KID}).encode())
    body = _b64url(json.dumps(claims).encode())
    signature = key.sign(f"{head}.{body}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64url(signature)}"


# -- one gateway that verifies it ----------------------------------------------------------


def _handler(tmp_path: Path, static_jwks: dict[str, Any]) -> Any:
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
                "trustedProxyAuth": {
                    "trustedPeerCidrs": ["127.0.0.1/32"],
                    "assertionHeader": _HEADER,
                    "assertionFormat": "jwt",
                    "issuer": _ISSUER,
                    "audience": _AUDIENCE,
                    "jwks": static_jwks,
                    "identityClaim": "email",
                    "allowedIdentities": [_PERSON],
                },
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


def _connection() -> Any:
    return SimpleNamespace(
        remote_address=("127.0.0.1", 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _asserted_request(path: str, token: str, values: dict[str, Any], header: str) -> Any:
    """One WebUI request that carries a verified assertion and no API token.

    The assertion alone authorizes these routes, so the absence of a token is the point rather
    than an omission.
    """
    return SimpleNamespace(
        path=path,
        headers=Headers([(_HEADER, token), (header, json.dumps(values))]),
    )


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


# -- surface 1: the latch clear (#28) ------------------------------------------------------


async def test_the_latch_clear_records_the_person_who_lifted_it(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The clear is the one control that lifts a terminal denial, so the record names a person.

    ``LatchController.clear`` emits the event and the recorder in ``nanoinfra/gates/runtime.py``
    writes it, so this asserts the record and never the return value of the route.
    """
    root = tmp_path / "gates"
    runtime, controller = build_gate_runtime(GatesConfig(), root=root)
    runtime.refuse_action(
        session_id=_SESSION,
        capability_class=_CLASS,
        tool="execute_on_server",
        reason="unattended mutate.remote at group scope is denied",
        execution_context="automation",
    )
    handler = _handler(tmp_path, static_jwks)
    handler.attach_latch_surface(
        LatchOperatorSurface(controller=controller, audit=runtime.audit)
    )

    response = await handler.dispatch(
        _connection(),
        _asserted_request(
            LATCH_CLEAR_PATH,
            _token(signing_key),
            {"sessionId": _SESSION, "capabilityClass": _CLASS},
            LATCH_VALUES_HEADER,
        ),
    )

    assert _body(response)["cleared"] is True
    cleared = [record for record in runtime.audit.read_all() if record["decision"] == "cleared"]
    assert [record["actor"] for record in cleared] == [_WEBUI_ACTOR]


# -- surfaces 2 and 4: the approval answer (#27) and the credential decision (#39) ----------


class _Harness:
    """One executor with its audit log, and the operator socket the two answers arrive on.

    The executor is the real one, because the approval record and the credential record are
    written there and not by the surface that answered. Two processes hold the two halves in a
    deployment, and one audit store is the only place their answers meet.
    """

    def __init__(self, tmp_path: Path, gates: GatesConfig) -> None:
        self.gates = gates
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

    def client(self) -> OperatorClient:
        return OperatorClient(self.socket_path, timeout_s=5.0)

    def records(self, capability_class: str, decision: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.audit.read_all()
            if record["capability_class"] == capability_class and record["decision"] == decision
        ]

    async def wait_for_one_pending(self, timeout_s: float = 5.0) -> Any:
        for _ in range(int(timeout_s / 0.01)):
            items = self.pending.pending()
            if items:
                return items[0]
            await asyncio.sleep(0.01)
        raise AssertionError("the executor never suspended an action")


@pytest.fixture
def harness(tmp_path: Path):
    """One executor per test, with the socket closed when the test ends."""
    built: list[_Harness] = []

    def build(gates: GatesConfig | None = None) -> _Harness:
        running = _Harness(tmp_path, gates if gates is not None else _gates())
        built.append(running)
        return running

    yield build
    for running in built:
        running.listener.close()


def _server_that_needs_a_credential(tmp_path: Path) -> str:
    """One server with a stored credential, so the action reaches the credential decision."""
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "password", "providerId": "local", "value": _SECRET_VALUE}
    )
    ServerStore(tmp_path).create(
        {
            "name": "prod-web-01",
            "providerId": "ssh",
            "config": {"host": _HOST},
            "secretRef": secret.id,
        }
    )
    return secret.id


def _execute_request(*, origin_path: str = "telegram") -> ExecuteRequest:
    """One interactive action, from a path other than the one that answers it (#13)."""
    return ExecuteRequest(
        server_id_or_name="prod-web-01",
        command=_COMMAND,
        session_id=_SESSION,
        execution_context="interactive",
        preview_requested=False,
        timeout_s=None,
        token_nonce=None,
        origin_path=origin_path,
    )


async def _answered_from_the_webui(
    harness: _Harness,
    handler: Any,
    token: str,
) -> Any:
    """Run one action to the end, with the WebUI route answering the approval it raises."""
    handler.attach_approvals_surface(ApprovalsOperatorSurface(client=harness.client()))
    with patch(
        _SSH_BACKEND,
        new=AsyncMock(return_value=ExecutionResult(exit_code=0, output="reloaded", error=None)),
    ):
        task = asyncio.create_task(harness.executor.handle(_execute_request()))
        suspended = await harness.wait_for_one_pending()
        answer = await handler.dispatch(
            _connection(),
            _asserted_request(
                APPROVALS_ANSWER_PATH,
                token,
                {
                    "requestId": suspended.request_id,
                    "decision": "approve",
                    "targetDigest": suspended.target_digest,
                },
                APPROVAL_VALUES_HEADER,
            ),
        )
        response = await task
    assert _body(answer)["ok"] is True, _body(answer)
    assert response.ok, response.reason
    return suspended


async def test_the_approval_answer_records_the_person_who_approved_it(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    harness: Any,
    tmp_path: Path,
) -> None:
    """The acceptance case of #47: an approval by one person names that person in the record.

    The answer crosses a process boundary. The WebUI resolves the identity, the executor holds
    the decision, and the record is written on the executor's side of that boundary.
    """
    _server_that_needs_a_credential(tmp_path)
    running = harness()

    await _answered_from_the_webui(running, _handler(tmp_path, static_jwks), _token(signing_key))

    allowed = running.records(MUTATE_REMOTE, "allow")
    assert [record["actor"] for record in allowed] == [_WEBUI_ACTOR]
    assert allowed[0]["approval_path"] == "webui"
    assert allowed[0]["origin_path"] == "telegram"


async def test_the_credential_decision_records_the_person_the_approval_named(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    harness: Any,
    tmp_path: Path,
) -> None:
    """One action costs one human decision, so this record inherits the approval's actor.

    A reviewer asks which approval authorized this decryption, and the record answers with the
    request id and with the person who answered it.
    """
    _server_that_needs_a_credential(tmp_path)
    running = harness()

    suspended = await _answered_from_the_webui(
        running, _handler(tmp_path, static_jwks), _token(signing_key)
    )

    credential = running.records(CREDENTIAL_ACCESS, "allow")
    assert [record["actor"] for record in credential] == [_WEBUI_ACTOR]
    assert credential[0]["approval_id"] == suspended.request_id


# -- surface 3: the chat answer (#43) ------------------------------------------------------


def test_the_chat_actor_is_the_half_the_channel_authenticated() -> None:
    """A username is user-controlled and it changes at will, so it names no authority."""
    assert approval_actor("telegram", _TELEGRAM_SENDER) == _TELEGRAM_ACTOR


async def test_the_chat_answer_records_the_person_the_channel_authenticated(
    harness: Any,
    tmp_path: Path,
) -> None:
    """The one surface that reads no assertion, and it still names a person.

    ``InboundMessage.channel`` and ``InboundMessage.sender_id`` carry the identity the channel
    authenticated, so this path needs no proxy to name somebody. The request therefore arrives
    on ``webui`` here, and Telegram is the second path that answers it.
    """
    _server_that_needs_a_credential(tmp_path)
    running = harness()
    surface = ApprovalAnswerSurface(client=running.client())
    request = _execute_request(origin_path="webui")

    with patch(
        _SSH_BACKEND,
        new=AsyncMock(return_value=ExecutionResult(exit_code=0, output="reloaded", error=None)),
    ):
        task = asyncio.create_task(running.executor.handle(request))
        suspended = await running.wait_for_one_pending()
        sentence = await surface.approve(
            channel="telegram",
            sender_id=_TELEGRAM_SENDER,
            request_id=suspended.request_id,
        )
        response = await task

    assert "Approved" in sentence, sentence
    assert response.ok, response.reason
    assert [record["actor"] for record in running.records(MUTATE_REMOTE, "allow")] == [
        _TELEGRAM_ACTOR
    ]
    assert [record["actor"] for record in running.records(CREDENTIAL_ACCESS, "allow")] == [
        _TELEGRAM_ACTOR
    ]


# -- the four together ---------------------------------------------------------------------


async def test_no_record_of_an_answered_action_names_the_bare_path(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    harness: Any,
    tmp_path: Path,
) -> None:
    """The regression this item exists to stop.

    Before #47 every WebUI answer wrote the constant ``webui``, so a reviewer read "the WebUI
    approved it" and two operators were one credential. Where an identity is verified, no record
    of the answered action may name the path alone.
    """
    _server_that_needs_a_credential(tmp_path)
    running = harness()

    await _answered_from_the_webui(running, _handler(tmp_path, static_jwks), _token(signing_key))

    actors = {record["actor"] for record in running.audit.read_all()} - {None}
    assert actors == {_WEBUI_ACTOR}
