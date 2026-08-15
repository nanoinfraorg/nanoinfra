# tests/channels/test_trusted_proxy_jwt_admission.py
"""Item 5 of M1 (#62) at the two doors it guards: the WebSocket handshake and the REST routes.

``tests/webui/test_assertion_identity.py`` holds the access decision on its own. This file holds
the wiring, because the decision only matters where it is read.

**In trusted-proxy mode the assertion alone authorizes both doors.** ``/webui/bootstrap`` answers
its metadata with no bootstrap token and no REST API token, and the handshake needs no token
either. So whoever is admitted gets a chat session with the agent. Every test below therefore
asserts an admission or a refusal at a real route rather than at a helper.

The gateway is built in process with a static key set, so the suite signs its own tokens and
needs no identity provider, no proxy and no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.webui.gateway_services import build_gateway_services

_ISSUER = "https://idp.example/realms/homelab"
_AUDIENCE = "nanoinfra-gateway"
_KID = "key-2026-08"
_HEADER = "X-Access-Token"
_OPERATOR = "operator@example.com"
_STRANGER = "stranger@gmail.example"


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


def _token(signing_key: rsa.RSAPrivateKey, *, email: str = _OPERATOR, **over: Any) -> str:
    """One token, signed here, so no test needs a provider to produce an unusual one."""
    import time

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
    signature = signing_key.sign(
        f"{head}.{body}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{head}.{body}.{_b64url(signature)}"


class _Conn:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.remote_address = (host, 45678)

    def respond(self, status: int, body: str) -> Any:
        return SimpleNamespace(status_code=status, body=body.encode())


class _Req:
    def __init__(self, headers: dict[str, str], *, path: str = "/webui/bootstrap") -> None:
        self.headers = headers
        self.path = path


def _channel(
    static_jwks: dict[str, Any],
    tmp_path: Path,
    *,
    log: Any,
    **over: Any,
) -> WebSocketChannel:
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
    block = {name: value for name, value in block.items() if value is not None}
    config = WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 29901,
            "path": "/",
            "trustedProxyAuth": block,
        }
    )
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
        logger=log,
    )
    return WebSocketChannel(config, bus, gateway=gateway)


# -- the REST door -------------------------------------------------------------------------


async def test_a_verified_operator_reaches_the_rest_routes_with_no_api_token(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """503 is the route answering, because this gateway has no session manager attached.

    A 401 would mean the assertion bought nothing. The identity is also recorded on the request,
    which is what M2 reads to name the actor of an approval.
    """
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    request = _Req({_HEADER: _token(signing_key)}, path="/api/sessions")

    response = await channel.gateway.http.dispatch(_Conn(), request)

    assert response.status_code == 503
    assert getattr(request, "_nanoinfra_trusted_proxy_authenticated") is True
    assert getattr(request, "_nanoinfra_trusted_proxy_identity") == _OPERATOR


async def test_a_stranger_with_a_genuine_token_reaches_no_rest_route(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The case the item exists for. The signature verified and the person is still a stranger."""
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    request = _Req({_HEADER: _token(signing_key, email=_STRANGER)}, path="/api/sessions")

    response = await channel.gateway.http.dispatch(_Conn(), request)

    assert response.status_code == 401
    assert getattr(request, "_nanoinfra_trusted_proxy_authenticated") is False


async def test_the_client_learns_only_that_it_is_not_authorized(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The refusal names the claim value in the log and nothing in the answer.

    An error text that named the rule would tell an attacker which rule to attack next.
    """
    log = MagicMock()
    channel = _channel(static_jwks, tmp_path, log=log)
    token = _token(signing_key, email=_STRANGER)

    response = await channel.gateway.http.dispatch(
        _Conn(), _Req({_HEADER: token}, path="/api/sessions")
    )

    body = bytes(response.body).decode()
    assert _STRANGER not in body
    assert "allowedIdentities" not in body
    logged = repr(log.mock_calls)
    assert _STRANGER in logged
    assert token not in logged


# -- the bootstrap door --------------------------------------------------------------------


async def test_bootstrap_answers_a_verified_operator(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    channel = _channel(static_jwks, tmp_path, log=MagicMock())

    response = await channel.gateway.http.dispatch(_Conn(), _Req({_HEADER: _token(signing_key)}))

    assert response.status_code == 200
    payload = json.loads(bytes(response.body).decode())
    assert "token" not in payload
    assert "api_token" not in payload


@pytest.mark.parametrize("email", [_STRANGER, ""])
async def test_bootstrap_refuses_an_identity_this_deployment_never_named(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
    email: str,
) -> None:
    channel = _channel(static_jwks, tmp_path, log=MagicMock())

    response = await channel.gateway.http.dispatch(
        _Conn(), _Req({"Host": "nanoinfra.example", _HEADER: _token(signing_key, email=email)})
    )

    assert response.status_code == 403


async def test_bootstrap_refuses_an_expired_token(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    token = _token(signing_key, exp=1_600_000_000)

    response = await channel.gateway.http.dispatch(
        _Conn(), _Req({"Host": "nanoinfra.example", _HEADER: token})
    )

    assert response.status_code == 403


async def test_bootstrap_refuses_a_forged_signature(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """An operator's own claim set, resigned by nobody."""
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    head, body, _ = _token(signing_key).split(".")

    response = await channel.gateway.http.dispatch(
        _Conn(), _Req({"Host": "nanoinfra.example", _HEADER: f"{head}.{body}.AAAA"})
    )

    assert response.status_code == 403


async def test_a_genuine_token_from_an_untrusted_peer_gains_nothing(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The peer check survives verification, so both facts must hold together."""
    channel = _channel(static_jwks, tmp_path, log=MagicMock())

    response = await channel.gateway.http.dispatch(
        _Conn("203.0.113.9"),
        _Req({"Host": "nanoinfra.example", _HEADER: _token(signing_key)}),
    )

    assert response.status_code == 403


# -- the handshake door --------------------------------------------------------------------


async def test_the_handshake_admits_a_verified_operator(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    connection = _Conn()

    response = await channel._authorize_websocket_handshake(
        connection, {}, {_HEADER: _token(signing_key)}
    )

    assert response is None
    assert connection in channel._webui_connections


@pytest.mark.parametrize(
    "header_value",
    ["not-a-jwt", "a.b.c", "", "   "],
)
async def test_the_handshake_refuses_an_assertion_it_cannot_verify(
    static_jwks: dict[str, Any],
    tmp_path: Path,
    header_value: str,
) -> None:
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    connection = _Conn()

    response = await channel._authorize_websocket_handshake(connection, {}, {_HEADER: header_value})

    assert response is not None
    assert response.status_code == 401
    assert connection not in channel._webui_connections


async def test_the_handshake_refuses_a_stranger(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    connection = _Conn()

    response = await channel._authorize_websocket_handshake(
        connection, {}, {_HEADER: _token(signing_key, email=_STRANGER)}
    )

    assert response is not None
    assert response.status_code == 401
    assert connection not in channel._webui_connections


async def test_an_issued_token_still_admits_under_a_jwt_config(
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A refused assertion falls through to the checks the deployment already had.

    So a `jwt` block never removes the token path, and both may be on at once.
    """
    channel = _channel(static_jwks, tmp_path, log=MagicMock())
    token = channel.gateway.tokens.issue_token(300, audience="webui")
    connection = _Conn()

    response = await channel._authorize_websocket_handshake(connection, {"token": [token]}, {})

    assert response is None
    assert connection in channel._webui_connections


# -- the explicit opt-out, at the door -----------------------------------------------------


async def test_allow_any_verified_identity_admits_a_stranger(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The posture an operator has to write down, doing exactly what it says."""
    channel = _channel(
        static_jwks,
        tmp_path,
        log=MagicMock(),
        allowedIdentities=None,
        allowAnyVerifiedIdentity=True,
    )
    request = _Req({_HEADER: _token(signing_key, email=_STRANGER)}, path="/api/sessions")

    response = await channel.gateway.http.dispatch(_Conn(), request)

    assert response.status_code == 503
    assert getattr(request, "_nanoinfra_trusted_proxy_identity") == _STRANGER


async def test_a_required_claim_admits_a_domain_at_the_door(
    signing_key: rsa.RSAPrivateKey,
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    channel = _channel(
        static_jwks,
        tmp_path,
        log=MagicMock(),
        allowedIdentities=None,
        requiredClaims={"hd": "example.com"},
    )
    request = _Req(
        {_HEADER: _token(signing_key, email="anybody@example.com", hd="example.com")},
        path="/api/sessions",
    )

    response = await channel.gateway.http.dispatch(_Conn(), request)

    assert response.status_code == 503
    assert getattr(request, "_nanoinfra_trusted_proxy_identity") == "anybody@example.com"


# -- the startup echo, at the start ---------------------------------------------------------


async def _started_channel_log(channel: WebSocketChannel) -> list[str]:
    """Start the channel, read every line it logged, and stop it again.

    The server binds a Unix socket under ``tmp_path``, so the test needs no port and leaves
    nothing behind.
    """
    lines: list[str] = []
    channel.logger = SimpleNamespace(  # type: ignore[assignment]
        info=lambda template, *args: lines.append(str(template).format(*args)),
        warning=lambda template, *args: lines.append(str(template).format(*args)),
        debug=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )
    task = asyncio.create_task(channel.start())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if lines:
            break
    await channel.stop()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return lines


async def test_the_allow_any_posture_is_echoed_at_every_start(
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    """#62 asks for the echo at the start rather than in a document nobody rereads."""
    channel = _channel(
        static_jwks,
        tmp_path,
        log=MagicMock(),
        allowedIdentities=None,
        allowAnyVerifiedIdentity=True,
        unixSocketPath=str(tmp_path / "ws.sock"),
    )

    lines = await _started_channel_log(channel)

    assert any("allowAnyVerifiedIdentity" in line for line in lines), lines


async def test_a_closed_posture_is_echoed_too(
    static_jwks: dict[str, Any],
    tmp_path: Path,
) -> None:
    channel = _channel(
        static_jwks,
        tmp_path,
        log=MagicMock(),
        unixSocketPath=str(tmp_path / "ws.sock"),
    )

    lines = await _started_channel_log(channel)

    assert any(_ISSUER in line for line in lines), lines
