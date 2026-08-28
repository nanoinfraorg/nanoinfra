# tests/webui/test_identity_panel.py
"""The identity block the gate panel reads -- nanoinfraorg/nanoinfra#85.

#70 put the resolved actor in the connection badge. The badge answers "who am I" and nothing
else, so two states still look alike there:

* a shared token, which is normal, and a proxy that is configured and did not assert anybody;
* a verified assertion, and a ``plain`` header that this gateway never checked.

**The warning is the reason this file exists.** A ``trustedProxyAuth`` block plus a resolved
actor of the bare path name means the assertion did not arrive or did not verify. Every approval
in that state names nobody while the deployment believes it names somebody.

Two rules hold over the whole payload:

* it carries a posture *kind* and facts, and never an English sentence. The WebUI carries ten
  locales, and a sentence from the server reaches nine of them in the wrong language.
* it carries no secret, no token, no key material and no address list. #72 holds that line for
  the startup log. This file holds it for the payload.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from websockets.datastructures import Headers

from nanoinfra.channels.websocket.runtime import TrustedProxyAuthConfig, WebSocketConfig
from nanoinfra.webui import assertion_identity
from nanoinfra.webui.assertion_identity import (
    POSTURE_ANY_VERIFIED,
    POSTURE_NO_PROXY,
    POSTURE_PLAIN,
    POSTURE_VERIFIED,
    describe_trusted_proxy_posture,
    identity_panel_payload,
    trusted_proxy_posture_kind,
)
from nanoinfra.webui.gateway_services import build_gateway_services
from nanoinfra.webui.latch_api import operator_actor

_ISSUER = "https://idp.example/realms/homelab"
_AUDIENCE = "nanoinfra-gateway"
_HEADER = "X-Access-Token"
_OPERATOR = "operator@example.com"
_CIDR = "10.4.7.9/32"
_JWKS_URL = "https://idp.example/certs"

# The three routes below all answer a settings payload, and the panel reads all three.
_SETTINGS_PATH = "/api/settings"
_GATES_UPDATE_PATH = "/api/settings/gates/update"
_AGENT_UPDATE_PATH = "/api/settings/update"

# A gate policy the save route accepts. The panel sends the whole document, so the test does too.
_POLICY: dict[str, Any] = {
    "approvers": [{"channel": "webui", "sender": "webui"}],
    "approvalPaths": ["webui", "telegram"],
}

# The identity attribute ``ws_http.dispatch`` writes once per request.
_IDENTITY = "_nanoinfra_trusted_proxy_identity"

# Every key the payload holds. The set is fixed, so a field somebody adds later has to arrive
# in this test as well as in the payload -- which is how the identity/workspace fields below
# got here.
_KEYS = {
    "posture",
    "issuer",
    "identityClaim",
    "workspaceKeyClaim",
    "assertionHeader",
    "actor",
    "workspace",
    "workspacePersonal",
    "signOutPath",
    "assertionMissing",
}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _static_jwks() -> dict[str, Any]:
    """One RSA public key, in the shape an operator pastes into ``jwks``."""
    public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    numbers = public.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "key-2026-08",
                "alg": "RS256",
                "use": "sig",
                "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _jwt_block(**over: Any) -> TrustedProxyAuthConfig:
    block: dict[str, Any] = {
        "trustedPeerCidrs": [_CIDR],
        "assertionHeader": _HEADER,
        "assertionFormat": "jwt",
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "jwksUrl": _JWKS_URL,
        "identityClaim": "email",
        "allowedIdentities": [_OPERATOR],
    }
    block.update(over)
    return TrustedProxyAuthConfig.model_validate(
        {name: value for name, value in block.items() if value is not None}
    )


def _plain_block() -> TrustedProxyAuthConfig:
    return TrustedProxyAuthConfig.model_validate(
        {
            "trustedPeerCidrs": [_CIDR],
            "assertionHeader": _HEADER,
            "assertionFormat": "plain",
        }
    )


def _request(identity: str | None) -> Any:
    """One request as ``dispatch`` leaves it.

    ``None`` is a request that met no trusted-proxy decision, which is every request of a
    deployment with no proxy. An empty string is a request whose assertion was refused.
    """
    request = SimpleNamespace(path="/api/settings", headers=Headers([]))
    if identity is not None:
        setattr(request, _IDENTITY, identity)
    return request


# -- one posture for each deployment -------------------------------------------------------


def test_a_deployment_with_no_proxy_reads_as_a_shared_token() -> None:
    """The common install. The actor is the path, and that is normal rather than a fault."""
    block = identity_panel_payload(None, _request(None))

    assert block["posture"] == POSTURE_NO_PROXY
    assert block["actor"] == "webui"
    assert block["assertionMissing"] is False


def test_a_verified_deployment_names_the_issuer_and_the_claim() -> None:
    block = identity_panel_payload(_jwt_block(), _request(_OPERATOR))

    assert block["posture"] == POSTURE_VERIFIED
    assert block["issuer"] == _ISSUER
    assert block["identityClaim"] == "email"
    assert block["actor"] == f"webui:{_OPERATOR}"
    assert block["assertionMissing"] is False


def test_a_plain_deployment_names_the_header_it_reads() -> None:
    """A ``plain`` block carries no issuer and no claim, so the payload names neither."""
    block = identity_panel_payload(_plain_block(), _request(_OPERATOR))

    assert block["posture"] == POSTURE_PLAIN
    assert block["assertionHeader"] == _HEADER
    assert block["issuer"] == ""
    assert block["identityClaim"] == ""


def test_allow_any_verified_identity_is_a_posture_of_its_own() -> None:
    """The setting somebody turns on once and forgets. It must not read as ``verified``."""
    block = identity_panel_payload(
        _jwt_block(allowedIdentities=None, allowAnyVerifiedIdentity=True),
        _request(_OPERATOR),
    )

    assert block["posture"] == POSTURE_ANY_VERIFIED
    assert block["issuer"] == _ISSUER


# -- the warning -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [_jwt_block(), _jwt_block(allowedIdentities=None, allowAnyVerifiedIdentity=True)],
    ids=["verified", "any_verified"],
)
def test_a_configured_proxy_that_asserted_nobody_warns(config: TrustedProxyAuthConfig) -> None:
    """The state this issue exists for.

    The deployment configured a proxy, and the request arrived with no identity. The gateway
    read the shared token instead, so the actor is the path and every approval here names
    nobody. That is a configuration failure and never a normal state.
    """
    block = identity_panel_payload(config, _request(""))

    assert block["assertionMissing"] is True
    assert block["actor"] == "webui"


def test_a_plain_proxy_that_asserted_nobody_warns() -> None:
    """The ``plain`` path fails the same way, so it carries the same warning."""
    block = identity_panel_payload(_plain_block(), _request(None))

    assert block["assertionMissing"] is True


def test_a_deployment_with_no_proxy_never_warns() -> None:
    """``webui`` is the whole actor there. A warning about a normal state teaches nobody."""
    assert identity_panel_payload(None, _request(""))["assertionMissing"] is False


# -- one source for the actor ----------------------------------------------------------------


@pytest.mark.parametrize("identity", [None, "", "   ", _OPERATOR, "webui", "12345|name"])
def test_the_actor_is_the_answer_the_gate_reads(identity: str | None) -> None:
    """The panel displays what ``gates.approvers`` compares, or the panel teaches a mismatch.

    ``operator_actor`` is the function the gate reads. A second rule for the ``webui:`` prefix
    here would drift, and the drift would appear as an approval that does not count.
    """
    request = _request(identity)

    assert identity_panel_payload(_jwt_block(), request)["actor"] == operator_actor(request)


# -- no secret, no key material, no address list ----------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        None,
        _jwt_block(),
        _jwt_block(jwksUrl=None, jwks=_static_jwks()),
        _jwt_block(allowedIdentities=None, allowAnyVerifiedIdentity=True),
        _plain_block(),
    ],
    ids=["no_proxy", "verified", "static_keys", "any_verified", "plain"],
)
def test_no_posture_sends_the_config_block(config: TrustedProxyAuthConfig | None) -> None:
    """The panel needs a short list of facts. A client that held the rest would leak the
    deployment shape.

    The address list is the clearest case: ``trustedPeerCidrs`` names the internal address of
    the proxy, and no browser needs it. The key material is checked too, because a public
    modulus is not a secret and it still describes the deployment to every reader of the page.
    """
    block = identity_panel_payload(config, _request(_OPERATOR))
    text = json.dumps(block)

    assert set(block) == _KEYS
    assert _CIDR not in text
    assert _JWKS_URL not in text
    assert "10.4.7.9" not in text
    for key in (config.jwks or {}).get("keys", []) if config is not None else []:
        assert key["n"] not in text
    assert _AUDIENCE not in text


def test_the_allowed_identity_list_stays_on_the_server() -> None:
    """The list names people. The panel needs the actor of this request and no other name."""
    block = identity_panel_payload(_jwt_block(allowedIdentities=["a@x.example"]), _request(""))

    assert "a@x.example" not in json.dumps(block)


# -- the log line and the panel cannot disagree -----------------------------------------------


@pytest.mark.parametrize(
    ("config", "kind"),
    [
        (None, POSTURE_NO_PROXY),
        (_jwt_block(), POSTURE_VERIFIED),
        (_jwt_block(allowedIdentities=None, allowAnyVerifiedIdentity=True), POSTURE_ANY_VERIFIED),
        (_plain_block(), POSTURE_PLAIN),
    ],
    ids=["no_proxy", "verified", "any_verified", "plain"],
)
def test_the_payload_names_the_posture_the_kind_function_answers(
    config: TrustedProxyAuthConfig | None,
    kind: str,
) -> None:
    assert trusted_proxy_posture_kind(config) == kind
    assert identity_panel_payload(config, _request(_OPERATOR))["posture"] == kind


def test_the_log_line_and_the_panel_read_one_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """One function decides which posture applies. Both answers read it.

    The check replaces that one function and reads both answers. A second copy of the rule in
    either answer would survive this, and the screen would then contradict the log for the same
    deployment.
    """
    config = _jwt_block()
    monkeypatch.setattr(assertion_identity, "trusted_proxy_posture_kind", lambda _c: POSTURE_PLAIN)

    assert identity_panel_payload(config, _request(_OPERATOR))["posture"] == POSTURE_PLAIN
    assert "never verified" in describe_trusted_proxy_posture(config).message


def test_the_posture_kinds_are_the_four_the_panel_knows() -> None:
    """A fifth kind would reach the panel as an unknown value and render nothing."""
    assert {POSTURE_NO_PROXY, POSTURE_VERIFIED, POSTURE_ANY_VERIFIED, POSTURE_PLAIN} == {
        "no_proxy",
        "verified",
        "any_verified",
        "plain",
    }


# -- where the block travels: the gate settings payload ---------------------------------------


def _handler(tmp_path: Path, **over: Any) -> Any:
    """One gateway, with the channel config this deployment runs."""
    settings: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
    }
    settings.update(over)
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    services = build_gateway_services(
        config=WebSocketConfig.model_validate(settings),
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


def _local_proxy_block(assertion_format: str) -> dict[str, Any]:
    """A proxy block whose trusted peer is the loopback address the test dispatches from."""
    if assertion_format == "plain":
        return {
            "trustedPeerCidrs": ["127.0.0.1/32"],
            "assertionHeader": _HEADER,
            "assertionFormat": "plain",
        }
    return {
        "trustedPeerCidrs": ["127.0.0.1/32"],
        "assertionHeader": _HEADER,
        "assertionFormat": "jwt",
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "jwksUrl": _JWKS_URL,
        "identityClaim": "email",
        "allowedIdentities": [_OPERATOR],
    }


def _connection() -> Any:
    return SimpleNamespace(
        remote_address=("127.0.0.1", 51234),
        respond=lambda status, text: SimpleNamespace(status_code=status, body=text.encode()),
    )


def _route_request(path: str, *, token: str | None = None, assertion: str | None = None) -> Any:
    headers: list[tuple[str, str]] = []
    if token is not None:
        headers.append(("Authorization", f"Bearer {token}"))
    if assertion is not None:
        headers.append((_HEADER, assertion))
    return SimpleNamespace(path=path, headers=Headers(headers))


def _body(response: Any) -> dict[str, Any]:
    return json.loads(bytes(response.body).decode("utf-8"))


async def _api_token(handler: Any) -> str:
    """The token a local browser gets from the bootstrap, which is how the panel loads."""
    browser = SimpleNamespace(
        path="/webui/bootstrap",
        headers=Headers([("Host", "127.0.0.1:8765")]),
    )
    return str(_body(await handler.dispatch(_connection(), browser))["api_token"])


async def _identity_of(handler: Any, request: Any) -> dict[str, Any]:
    response = await handler.dispatch(_connection(), request)
    assert response.status_code == 200, _body(response)
    gates = _body(response)["advanced"]["gates"]
    identity = gates["identity"]
    assert isinstance(identity, dict)
    return cast("dict[str, Any]", identity)


async def test_the_settings_route_answers_the_identity_block(tmp_path: Path) -> None:
    """The panel reads one request. The gate block carries the posture and the actor."""
    handler = _handler(tmp_path)
    token = await _api_token(handler)

    identity = await _identity_of(handler, _route_request(_SETTINGS_PATH, token=token))

    assert identity["posture"] == POSTURE_NO_PROXY
    assert identity["actor"] == "webui"
    assert identity["assertionMissing"] is False


async def test_a_plain_assertion_reaches_the_panel_as_the_person_it_named(tmp_path: Path) -> None:
    """The assertion authorizes the route, so the block names the same person the gate reads."""
    handler = _handler(tmp_path, trustedProxyAuth=_local_proxy_block("plain"))

    identity = await _identity_of(
        handler,
        _route_request(_SETTINGS_PATH, assertion=_OPERATOR),
    )

    assert identity["posture"] == POSTURE_PLAIN
    assert identity["actor"] == f"webui:{_OPERATOR}"
    assert identity["assertionHeader"] == _HEADER
    assert identity["assertionMissing"] is False


async def test_a_configured_proxy_whose_assertion_did_not_arrive_warns_on_the_route(
    tmp_path: Path,
) -> None:
    """The acceptance case of #85, through a real dispatch.

    The deployment configured a proxy. This request carried no assertion, so the shared token
    authorized it and the actor is the path. Nothing on the screen said so before this issue.
    """
    handler = _handler(tmp_path, trustedProxyAuth=_local_proxy_block("plain"))
    token = await _api_token(handler)

    identity = await _identity_of(handler, _route_request(_SETTINGS_PATH, token=token))

    assert identity["assertionMissing"] is True
    assert identity["actor"] == "webui"


async def test_a_jwt_deployment_names_the_issuer_on_the_route(tmp_path: Path) -> None:
    """A ``jwt`` block with no key of its own still states what it verifies against."""
    handler = _handler(tmp_path, trustedProxyAuth=_local_proxy_block("jwt"))
    token = await _api_token(handler)

    identity = await _identity_of(handler, _route_request(_SETTINGS_PATH, token=token))

    assert identity["posture"] == POSTURE_VERIFIED
    assert identity["issuer"] == _ISSUER
    assert identity["identityClaim"] == "email"


async def test_the_block_survives_a_save_from_this_panel(tmp_path: Path) -> None:
    """The panel replaces its whole payload after a save, so the save must answer the block."""
    handler = _handler(tmp_path, trustedProxyAuth=_local_proxy_block("plain"))
    token = await _api_token(handler)
    save = _route_request(f"{_GATES_UPDATE_PATH}?policy={quote(json.dumps(_POLICY))}", token=token)

    identity = await _identity_of(handler, save)

    assert identity["posture"] == POSTURE_PLAIN
    assert identity["assertionMissing"] is True


async def test_the_block_survives_a_save_from_another_panel(tmp_path: Path) -> None:
    """One settings payload feeds every panel. A save elsewhere must not drop this block."""
    handler = _handler(tmp_path, trustedProxyAuth=_local_proxy_block("plain"))
    token = await _api_token(handler)

    identity = await _identity_of(handler, _route_request(_AGENT_UPDATE_PATH, token=token))

    assert identity["posture"] == POSTURE_PLAIN


async def test_the_route_sends_no_part_of_the_config_block(tmp_path: Path) -> None:
    """The whole response, and not the block alone. A leak anywhere in it is a leak."""
    handler = _handler(tmp_path, trustedProxyAuth=_local_proxy_block("jwt"))
    token = await _api_token(handler)

    response = await handler.dispatch(
        _connection(),
        _route_request(_SETTINGS_PATH, token=token),
    )

    text = bytes(response.body).decode("utf-8")
    assert _JWKS_URL not in text
    assert "127.0.0.1/32" not in text
    assert _OPERATOR not in text


def test_the_panel_learns_whose_workspace_this_is() -> None:
    """A person reads this block to answer "who am I and where do my files go". The
    deployment's workspace is a true answer to the second question only when nobody
    signed in."""
    block = identity_panel_payload(
        None,
        _request(_OPERATOR),
        workspace="/data/workspaces/u-abc/default",
        workspace_personal=True,
    )
    assert block["workspace"] == "/data/workspaces/u-abc/default"
    assert block["workspacePersonal"] is True


def test_a_shared_deployment_says_the_workspace_is_not_personal() -> None:
    block = identity_panel_payload(None, _request(_OPERATOR), workspace="/data/workspaces/default")
    assert block["workspace"] == "/data/workspaces/default"
    assert block["workspacePersonal"] is False


def test_the_sign_out_path_is_a_path_and_not_a_url() -> None:
    """The cookie belongs to the proxy in front, so the gateway can only send the
    browser to a route that proxy serves. A URL here would let config send every
    reader of the page somewhere else."""
    import pytest as _pytest
    from pydantic import ValidationError

    from nanoinfra.channels.websocket.runtime import TrustedProxyAuthConfig

    ok = TrustedProxyAuthConfig(
        trusted_peer_cidrs=["10.4.7.9/32"],
        assertion_header="X-Access-Token",
        assertion_format="plain",
        sign_out_path="/oauth2/sign_out",
    )
    assert ok.sign_out_path == "/oauth2/sign_out"
    assert identity_panel_payload(ok, _request(_OPERATOR))["signOutPath"] == "/oauth2/sign_out"

    for bad in ("https://elsewhere.example/logout", "//elsewhere.example/logout", "oauth2/out"):
        with _pytest.raises(ValidationError):
            TrustedProxyAuthConfig(
                trusted_peer_cidrs=["10.4.7.9/32"],
                assertion_header="X-Access-Token",
                assertion_format="plain",
                sign_out_path=bad,
            )
