# tests/webui/test_operator_actor.py
"""Item 6 of M2 (#63): the actor the WebUI names, and what a failed verification costs.

Two answers exist and no third. ``webui:<claim>`` names the person a verified assertion
identified. ``webui`` names the path, and that is the honest answer for a deployment which
authenticated a shared token and nobody. The ``user:<id>`` form of the first draft of #47 does
not exist.

**A failed verification is a refusal and never a fallback.** ``webui`` is an actor a deployment
may list in ``gates.approvers``, so a verifier that answered ``webui`` after a refusal would let
a forged token buy the privileges of the shared token. That is a downgrade attack rather than a
convenience. The route tests below therefore drive a forged, an expired and an unauthorized
assertion at the one route that lifts a terminal denial, and each one must reach no route, clear
no latch and write no record.

``operator_actor`` takes the request and nothing else. It reads the identity that
``ws_http.dispatch`` resolved once per request, so it cannot read an unverified header and it
cannot invent a third form. The signature is part of that guarantee, so a test states it.

The gateway is built in process with a static key set, so this file signs its own tokens and
needs no identity provider, no proxy and no network.
"""

from __future__ import annotations

import base64
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from websockets.datastructures import Headers

from nanoinfra.channels.websocket.runtime import WebSocketConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.runtime import GateRuntime, build_gate_runtime
from nanoinfra.webui.approvals_api import APPROVAL_PATH
from nanoinfra.webui.gateway_services import build_gateway_services
from nanoinfra.webui.latch_api import (
    LATCH_CLEAR_PATH,
    LATCH_VALUES_HEADER,
    LatchOperatorSurface,
    operator_actor,
)

_SESSION = "websocket:chat-1"
_CLASS = "mutate.remote"

_ISSUER = "https://idp.example/realms/homelab"
_AUDIENCE = "nanoinfra-gateway"
_KID = "key-2026-08"
_HEADER = "X-Access-Token"
_OPERATOR = "operator@example.com"
_STRANGER = "stranger@gmail.example"

# The identity attribute ``ws_http.dispatch`` writes once per request, and the flag beside it.
_IDENTITY = "_nanoinfra_trusted_proxy_identity"
_FLAG = "_nanoinfra_trusted_proxy_authenticated"


# -- who the actor is, with no route and no key --------------------------------------------


def _resolved(identity: str | None, *, header: str = "") -> Any:
    """One request as ``dispatch`` leaves it, and a header value that must not be read.

    ``identity`` of ``None`` is a request that reached no trusted-proxy decision at all, which
    is every request of a deployment with no proxy.
    """
    request = SimpleNamespace(
        path=LATCH_CLEAR_PATH,
        headers=Headers([(_HEADER, header)] if header else []),
    )
    if identity is not None:
        setattr(request, _FLAG, bool(identity))
        setattr(request, _IDENTITY, identity)
    return request


def test_the_actor_is_the_path_when_nothing_asserts_an_identity() -> None:
    assert operator_actor(_resolved(None)) == "webui"


def test_the_actor_names_the_identity_the_dispatch_resolved() -> None:
    assert operator_actor(_resolved(_OPERATOR)) == f"webui:{_OPERATOR}"


def test_the_actor_never_reads_the_assertion_header() -> None:
    """On the ``jwt`` path the header holds the whole token, and a token prefix names nobody.

    The ``plain`` path resolves the header into the identity inside the authenticator, so one
    value carries both formats and this function reads one attribute for both.
    """
    token = "eyJhbGciOiAiUlMyNTYifQ.eyJlbWFpbCI6ICJvcHNAZXhhbXBsZS5jb20ifQ.c2ln"

    assert operator_actor(_resolved(None, header=token)) == "webui"
    assert operator_actor(_resolved(None, header=_OPERATOR)) == "webui"


def test_the_actor_takes_the_request_and_nothing_else() -> None:
    """The strongest form of "no third answer": give the function nothing else to read.

    A ``config`` parameter let this function reach ``assertionHeader`` and read the header for
    itself. One caller with a stale flag then named a person from an unverified value. The
    identity is resolved once per request, so the resolved value is the only input.
    """
    assert list(inspect.signature(operator_actor).parameters) == ["request"]


@pytest.mark.parametrize(
    "identity",
    ["", "   ", _OPERATOR, "a:b", "user-7", "webui", "  padded@example.com  ", "12345|name"],
)
def test_every_answer_is_the_path_or_the_path_and_the_claim(identity: str) -> None:
    """Two forms, and no third. The ``user:<id>`` form of the first draft does not exist."""
    actor = operator_actor(_resolved(identity))

    assert actor == "webui" or actor == f"webui:{identity.strip()}"
    assert not actor.startswith("user:")


def test_the_actor_prefix_is_the_name_of_the_approval_path() -> None:
    """The prefix is what makes ``gates.approvers`` readable, so it must be the path name.

    A deployment writes ``{"channel": "webui", "sender": "webui:ops@example.com"}``. A prefix
    that drifted from the path name would leave every approver entry matching nothing, and the
    refusal would name an identity that looks correct.
    """
    assert APPROVAL_PATH == "webui"
    assert APPROVAL_PATH in GatesConfig().approval_paths
    assert operator_actor(_resolved(_OPERATOR)).split(":", 1)[0] == APPROVAL_PATH


def test_an_identity_of_only_whitespace_names_nobody() -> None:
    """A blank name must not read as a person, and it must not read as an empty claim."""
    assert operator_actor(_resolved(" \t ")) == "webui"


# -- the same question at the route that lifts a terminal denial ----------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key() -> rsa.RSAPrivateKey:
    """A second key pair, so a forged token can carry a real signature of the wrong key."""
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


def _token(key: rsa.RSAPrivateKey, *, email: str = _OPERATOR, **over: Any) -> str:
    """One token, signed here, so no test needs a provider to produce an unusual one."""
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "exp": time.time() + 300,
        "iat": time.time() - 5,
        "email": email,
    }
    claims.update(over)
    head = _b64url(json.dumps({"alg": "RS256", "kid": _KID}).encode())
    body = _b64url(json.dumps(claims).encode())
    signature = key.sign(f"{head}.{body}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64url(signature)}"


def _handler(tmp_path: Path, static_jwks: dict[str, Any], **over: Any) -> Any:
    """One gateway with a verifying trusted-proxy block and a static key set."""
    block: dict[str, Any] = {
        "trustedPeerCidrs": ["127.0.0.1/32"],
        "assertionHeader": _HEADER,
        "assertionFormat": "jwt",
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "jwks": static_jwks,
        "identityClaim": "email",
        "allowedIdentities": [_OPERATOR],
    }
    block.update(over)
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
                "trustedProxyAuth": block,
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


def _latched(root: Path) -> tuple[LatchOperatorSurface, GateRuntime]:
    """One denied session, so the clear route has a latch to lift."""
    runtime, controller = build_gate_runtime(GatesConfig(), root=root)
    runtime.refuse_action(
        session_id=_SESSION,
        capability_class=_CLASS,
        tool="execute_on_server",
        reason="unattended mutate.remote at group scope is denied",
        execution_context="automation",
    )
    return LatchOperatorSurface(controller=controller, audit=runtime.audit), runtime


def _clear_request(token: str | None) -> Any:
    headers: list[tuple[str, str]] = [
        (LATCH_VALUES_HEADER, json.dumps({"sessionId": _SESSION, "capabilityClass": _CLASS}))
    ]
    if token is not None:
        headers.append((_HEADER, token))
    return SimpleNamespace(path=LATCH_CLEAR_PATH, headers=Headers(headers))


def _connection(host: str = "127.0.0.1") -> Any:
    return SimpleNamespace(
        remote_address=(host, 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for segment in sorted(root.glob("gate-*.jsonl")):
        for line in segment.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _cleared_actors(root: Path) -> list[Any]:
    return [record["actor"] for record in _records(root) if record["decision"] == "cleared"]


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


async def test_a_verified_operator_clears_a_latch_under_their_own_name(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The acceptance case of this item, at the route and in the record on disk.

    The assertion alone authorizes the route, so this request carries no API token at all.
    """
    root = tmp_path / "gates"
    handler = _handler(tmp_path, static_jwks)
    surface, _ = _latched(root)
    handler.attach_latch_surface(surface)

    response = await handler.dispatch(_connection(), _clear_request(_token(signing_key)))

    assert response.status_code == 200
    assert _body(response)["cleared"] is True
    assert _body(response)["actor"] == f"webui:{_OPERATOR}"
    assert _cleared_actors(root) == [f"webui:{_OPERATOR}"]


@pytest.mark.parametrize("reason", ["forged", "expired", "unauthorized"])
async def test_a_refused_assertion_reaches_no_route_and_names_no_actor(
    reason: str,
    signing_key: rsa.RSAPrivateKey,
    other_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The property this item exists for. A refusal is a refusal, and never the shared actor.

    ``webui`` is an actor a deployment may list in ``gates.approvers``, so a fallback here would
    hand a forged token whatever the shared token holds. The latch stays in place, and the log
    gains no ``cleared`` record, so the refusal is visible in the two places an operator reads.
    """
    tokens = {
        "forged": lambda: _token(other_key),
        "expired": lambda: _token(signing_key, exp=time.time() - 3600),
        "unauthorized": lambda: _token(signing_key, email=_STRANGER),
    }
    root = tmp_path / "gates"
    handler = _handler(tmp_path, static_jwks)
    surface, _ = _latched(root)
    handler.attach_latch_surface(surface)
    request = _clear_request(tokens[reason]())

    response = await handler.dispatch(_connection(), request)

    assert response.status_code == 401
    assert getattr(request, _IDENTITY) == ""
    assert operator_actor(request) == "webui"
    assert _cleared_actors(root) == []
    assert surface.payload()["latches"] != []


async def test_a_verified_token_from_an_untrusted_peer_reaches_no_route(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The signature is genuine and the packet came from somewhere else. Both facts count."""
    root = tmp_path / "gates"
    handler = _handler(tmp_path, static_jwks)
    surface, _ = _latched(root)
    handler.attach_latch_surface(surface)

    response = await handler.dispatch(
        _connection("192.0.2.7"), _clear_request(_token(signing_key))
    )

    assert response.status_code == 401
    assert _cleared_actors(root) == []


async def test_the_dispatch_never_admits_a_request_it_cannot_name(
    signing_key: rsa.RSAPrivateKey,
    other_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The flag and the identity are one decision, so they can never disagree.

    A request the gateway admitted with no name would reach the routes as the shared actor,
    which is the downgrade this item forbids. The flag is therefore derived from the identity
    rather than decided beside it.
    """
    handler = _handler(tmp_path, static_jwks)

    for token in (_token(signing_key), _token(other_key)):
        request = _clear_request(token)
        await handler.dispatch(_connection(), request)
        assert getattr(request, _FLAG) is bool(getattr(request, _IDENTITY))


async def test_a_claim_too_long_to_name_is_refused_rather_than_truncated(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A truncated name is a third answer: it belongs to nobody.

    A cut claim would also collapse two identities that share one prefix into one authority, and
    ``gates.approvers`` compares the whole string. So the seam refuses the claim, the request
    reaches no route, and no record names a person who does not exist. The bound is above the
    longest address RFC 5321 permits, so no legal email address meets it.
    """
    long_identity = f"{'a' * 300}@example.com"
    root = tmp_path / "gates"
    handler = _handler(tmp_path, static_jwks, allowedIdentities=[long_identity])
    surface, _ = _latched(root)
    handler.attach_latch_surface(surface)
    request = _clear_request(_token(signing_key, email=long_identity))

    response = await handler.dispatch(_connection(), request)

    assert response.status_code == 401
    assert getattr(request, _IDENTITY) == ""
    assert _cleared_actors(root) == []
