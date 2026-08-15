# tests/channels/test_identity_posture_echo.py
"""Item 15 of M4 (#72): the gateway names its identity posture at every start.

Four postures, and each one reads on a line of its own:

* no trusted proxy, which is every deployment that runs a token alone;
* a verified ``jwt`` assertion;
* a ``plain`` assertion, as a warning, because there the proxy alone decides who reaches
  the agent;
* ``allowAnyVerifiedIdentity``, as a warning, at every start, because it is the setting
  somebody turns on once and forgets.

``gates.identityIndependence`` is the fifth line, and ``tests/cli/test_gate_identity_posture.py``
holds it, because the gate config reaches the CLI echo and not this channel.

**No line carries a secret.** A log reaches more accounts than a live credential should, and it
is shipped to a collector often enough that an operator cannot choose its readers. The last test
in this file asserts that over every line one start writes, and not over the posture lines
alone, because a start line that leaked a token would leak it just the same.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from cryptography.hazmat.primitives.asymmetric import rsa

from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.webui.gateway_services import build_gateway_services

_ISSUER = "https://idp.example/realms/homelab"
_AUDIENCE = "nanoinfra-gateway"
_KID = "key-2026-08"
_HEADER = "X-Access-Token"
_OPERATOR = "operator@example.com"

# The two values a WebUI deployment holds that must never reach a log line.
_STATIC_TOKEN = "static-token-nobody-may-read"
_ISSUE_SECRET = "issue-secret-nobody-may-read"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _static_jwks() -> dict[str, Any]:
    """One RSA public key, in the shape an operator pastes into ``jwks``."""
    numbers = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    public = numbers.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": _KID,
                "alg": "RS256",
                "use": "sig",
                "n": _b64url(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _channel(tmp_path: Path, **over: Any) -> WebSocketChannel:
    """A gateway on a Unix socket, so the test needs no port and leaves nothing behind."""
    settings: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 29903,
        "path": "/",
        "unixSocketPath": str(tmp_path / "ws.sock"),
    }
    settings.update(over)
    config = WebSocketConfig.model_validate(settings)
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
        logger=MagicMock(),
    )
    return WebSocketChannel(config, bus, gateway=gateway)


async def _start_and_read_lines(channel: WebSocketChannel) -> list[tuple[str, str]]:
    """Start the channel, answer every line it logged with its level, and stop it again."""
    lines: list[tuple[str, str]] = []

    def record(level: str) -> Any:
        return lambda template, *args: lines.append((level, str(template).format(*args)))

    channel.logger = SimpleNamespace(  # type: ignore[assignment]
        info=record("info"),
        warning=record("warning"),
        debug=lambda *_a, **_k: None,
        error=record("error"),
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


def _posture(lines: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The posture lines, which every reader finds by the one prefix they share."""
    return [(level, text) for level, text in lines if text.startswith("identity:")]


def _jwt_block(**over: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "trustedPeerCidrs": ["127.0.0.1/32"],
        "assertionHeader": _HEADER,
        "assertionFormat": "jwt",
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "jwks": _static_jwks(),
        "identityClaim": "email",
        "allowedIdentities": [_OPERATOR],
    }
    block.update(over)
    return {name: value for name, value in block.items() if value is not None}


# -- one line for each posture ---------------------------------------------------------------


async def test_a_deployment_with_no_proxy_reads_as_normal(tmp_path: Path) -> None:
    """The common install. It states the actor, and it states no fault."""
    lines = await _start_and_read_lines(_channel(tmp_path))

    posture = _posture(lines)
    assert len(posture) == 1
    level, text = posture[0]
    assert level == "info"
    assert "no trusted proxy is configured" in text
    assert '"webui"' in text


async def test_a_verified_assertion_names_the_issuer_and_the_claim(tmp_path: Path) -> None:
    lines = await _start_and_read_lines(_channel(tmp_path, trustedProxyAuth=_jwt_block()))

    posture = _posture(lines)
    assert len(posture) == 1
    level, text = posture[0]
    assert level == "info"
    assert _ISSUER in text
    assert "email" in text


async def test_a_plain_assertion_reads_as_a_warning(tmp_path: Path) -> None:
    """On that path the proxy alone decides who reaches the agent, and the line says so."""
    lines = await _start_and_read_lines(
        _channel(
            tmp_path,
            trustedProxyAuth={
                "trustedPeerCidrs": ["127.0.0.1/32"],
                "assertionHeader": _HEADER,
                "assertionFormat": "plain",
            },
        )
    )

    posture = _posture(lines)
    assert len(posture) == 1
    level, text = posture[0]
    assert level == "warning"
    assert "never verified" in text
    assert "the proxy alone decides" in text


async def test_allow_any_verified_identity_is_named_at_every_start(tmp_path: Path) -> None:
    """The setting somebody turns on once. It reads at every start, as a warning."""
    lines = await _start_and_read_lines(
        _channel(
            tmp_path,
            trustedProxyAuth=_jwt_block(allowedIdentities=None, allowAnyVerifiedIdentity=True),
        )
    )

    posture = _posture(lines)
    assert len(posture) == 1
    level, text = posture[0]
    assert level == "warning"
    assert "allowAnyVerifiedIdentity" in text


# -- no secret, and no token, on any line ----------------------------------------------------


async def test_no_line_of_a_start_carries_a_secret(tmp_path: Path) -> None:
    """Every credential this gateway holds, and none of them in the log.

    The key material is checked too. A static ``jwks`` holds a public key, so it is not a
    secret, and a modulus in a log line is still noise that hides the line an operator needs.
    """
    jwks = _static_jwks()
    channel = _channel(
        tmp_path,
        token=_STATIC_TOKEN,
        tokenIssueSecret=_ISSUE_SECRET,
        tokenIssuePath="/auth/token",
        trustedProxyAuth=_jwt_block(jwks=jwks),
    )

    lines = await _start_and_read_lines(channel)

    assert _posture(lines), lines
    forbidden = [_STATIC_TOKEN, _ISSUE_SECRET, jwks["keys"][0]["n"]]
    for _level, text in lines:
        for secret in forbidden:
            assert secret not in text, text
