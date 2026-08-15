# tests/webui/test_assertion_identity.py
"""Item 5 of M1 (#62): who may enter, because the approver list was never that list.

The proxy's own allowlist decides who reaches the agent. ``gates.approvers`` decides whose
approval counts. They are not the same list and they do not protect the same thing.

**In trusted-proxy mode the assertion alone authorizes the WebSocket handshake and the REST
routes**, so whoever is admitted gets a chat session with the agent, which is ``read`` and
``mutate.local`` in the ``interactive`` context. With a public identity provider that is a real
exposure: any account that completes the flow with the deployment's client id holds a token
whose signature, issuer and audience all check out. Verification is doing its job correctly and
the person is still a stranger. They are not in ``gates.approvers``, so they cannot approve a
remote action, and they can talk to the agent.

So the gateway does not rely on the operator's proxy configuration for this. A ``jwt`` block
declares who may enter, and a block that names nobody refuses at load.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic_core import ValidationError

from nanoinfra.channels.websocket.runtime import TrustedProxyAuthConfig
from nanoinfra.webui.assertion_identity import (
    AssertionPosture,
    TrustedProxyAuthenticator,
    admit_identity,
    describe_trusted_proxy_posture,
)
from nanoinfra.webui.assertion_jwks import StaticJwksSource
from nanoinfra.webui.assertion_jwt import AssertionRefusal, AssertionRefusedError

_ISSUER = "https://idp.example/realms/homelab"
_AUDIENCE = "nanoinfra-gateway"
_KID = "key-2026-08"
_NOW = 1_755_000_000.0
_HEADER = "X-Access-Token"
_OPERATOR = "operator@example.com"


# -- the access decision, which is pure ----------------------------------------------------


def _admit(claims: dict[str, Any], **over: Any) -> str:
    settings: dict[str, Any] = {
        "identity_claim": "email",
        "allowed_identities": [_OPERATOR],
        "required_claims": {},
        "allow_any_verified_identity": False,
    }
    settings.update(over)
    return admit_identity(claims, **settings)


def _refusal(claims: dict[str, Any], **over: Any) -> AssertionRefusedError:
    with pytest.raises(AssertionRefusedError) as caught:
        _admit(claims, **over)
    return caught.value


def test_a_named_identity_enters() -> None:
    assert _admit({"email": _OPERATOR}) == _OPERATOR


def test_a_stranger_with_a_valid_token_does_not_enter() -> None:
    """The case the whole item exists for.

    The signature verified, the issuer matched and the audience matched. The person is still
    somebody this deployment never named.
    """
    refusal = _refusal({"email": "stranger@gmail.example"})

    assert refusal.reason is AssertionRefusal.NOT_AN_ADMITTED_IDENTITY


def test_the_refusal_reports_the_claim_value_it_read() -> None:
    """An operator debugging a misconfigured proxy needs the value, not "denied".

    The value is quoted rather than pasted, so a claim that carried a newline cannot forge a
    second log line.
    """
    refusal = _refusal({"email": "stranger@gmail.example"})

    assert "stranger@gmail.example" in refusal.detail
    assert "email" in refusal.detail


def test_the_identity_match_is_exact() -> None:
    """Exact, so two spellings are two identities.

    The cost is that a provider which emits a different case needs the config to match. The
    refusal above reports the value it read, so an operator sees that in one log line. The
    gain is that no two accounts of one provider can ever collapse into one authority.
    """
    assert _refusal({"email": "Operator@Example.com"}).reason is (
        AssertionRefusal.NOT_AN_ADMITTED_IDENTITY
    )
    assert _refusal({"email": f" {_OPERATOR}x"}).reason is (
        AssertionRefusal.NOT_AN_ADMITTED_IDENTITY
    )


@pytest.mark.parametrize("value", [None, "", "   ", 7, ["a@b.c"], {"email": "a@b.c"}])
def test_a_token_with_no_usable_identity_claim_refuses(value: object) -> None:
    claims: dict[str, Any] = {} if value is None else {"email": value}

    assert _refusal(claims).reason is AssertionRefusal.NO_IDENTITY_CLAIM


def test_another_claim_can_name_the_person() -> None:
    """``sub`` is available for a deployment that prefers a stable opaque id."""
    claims = {"email": _OPERATOR, "sub": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6"}

    admitted = _admit(
        claims,
        identity_claim="sub",
        allowed_identities=["f81d4fae-7dec-11d0-a765-00a0c91e6bf6"],
    )

    assert admitted == "f81d4fae-7dec-11d0-a765-00a0c91e6bf6"


# -- requiredClaims covers a group without naming every person -----------------------------


def test_a_required_claim_admits_a_whole_domain() -> None:
    """The reason ``requiredClaims`` exists: a workspace domain, with no list of people."""
    claims = {"email": "anybody@example.com", "hd": "example.com"}

    admitted = _admit(claims, allowed_identities=[], required_claims={"hd": "example.com"})

    assert admitted == "anybody@example.com"


def test_a_required_claim_that_does_not_match_refuses() -> None:
    claims = {"email": _OPERATOR, "hd": "other.example"}

    refusal = _refusal(claims, allowed_identities=[], required_claims={"hd": "example.com"})

    assert refusal.reason is AssertionRefusal.CLAIM_DOES_NOT_MATCH
    assert "hd" in refusal.detail


def test_a_missing_required_claim_refuses() -> None:
    refusal = _refusal({"email": _OPERATOR}, required_claims={"hd": "example.com"})

    assert refusal.reason is AssertionRefusal.CLAIM_DOES_NOT_MATCH


def test_a_list_valued_claim_never_matches_and_that_is_the_documented_answer() -> None:
    """``requiredClaims`` compares exactly, so a list-valued claim needs a mapped scalar.

    A Keycloak ``groups`` claim is an array. Reading membership out of it would widen the rule
    from "matches exactly" to "contains", and a rule with two meanings is a rule an operator
    cannot predict. The provider maps a scalar claim instead.
    """
    claims = {"email": _OPERATOR, "groups": ["operators", "readers"]}

    refusal = _refusal(claims, required_claims={"groups": "operators"})

    assert refusal.reason is AssertionRefusal.CLAIM_DOES_NOT_MATCH


def test_both_lists_apply_together() -> None:
    """A named identity from the wrong domain still refuses."""
    claims = {"email": _OPERATOR, "hd": "other.example"}

    refusal = _refusal(claims, required_claims={"hd": "example.com"})

    assert refusal.reason is AssertionRefusal.CLAIM_DOES_NOT_MATCH


# -- the explicit opt-out ------------------------------------------------------------------


def test_allow_any_verified_identity_opens_the_door() -> None:
    claims = {"email": "stranger@gmail.example"}

    admitted = _admit(
        claims,
        allowed_identities=[],
        allow_any_verified_identity=True,
    )

    assert admitted == "stranger@gmail.example"


def test_allow_any_still_obeys_a_required_claim() -> None:
    """The two are read together, so the wider flag never cancels the narrower filter."""
    claims = {"email": "stranger@gmail.example"}

    refusal = _refusal(
        claims,
        allowed_identities=[],
        allow_any_verified_identity=True,
        required_claims={"hd": "example.com"},
    )

    assert refusal.reason is AssertionRefusal.CLAIM_DOES_NOT_MATCH


def test_a_rule_set_that_names_nobody_admits_nobody() -> None:
    """There is no implicit "every verified identity may enter".

    The schema refuses such a block at load, and this is the run-time half of the same rule.
    Two checks, because a config that reached this code by another route must still fail
    closed rather than open.
    """
    refusal = _refusal({"email": _OPERATOR}, allowed_identities=[])

    assert refusal.reason is AssertionRefusal.NOT_AN_ADMITTED_IDENTITY


# -- the schema half -----------------------------------------------------------------------


def _block(**over: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "trustedPeerCidrs": ["127.0.0.1/32"],
        "assertionHeader": _HEADER,
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "jwksUrl": "https://idp.example/certs",
        "allowedIdentities": [_OPERATOR],
    }
    block.update(over)
    return {name: value for name, value in block.items() if value is not None}


def test_a_jwt_block_that_names_nobody_refuses_at_load() -> None:
    """The load-time half, and the message names all three ways to fix it."""
    with pytest.raises(ValidationError) as caught:
        TrustedProxyAuthConfig.model_validate(_block(allowedIdentities=None))

    text = str(caught.value)
    assert "allowedIdentities" in text
    assert "requiredClaims" in text
    assert "allowAnyVerifiedIdentity" in text


def test_an_empty_identity_list_is_the_same_as_no_list() -> None:
    """An operator who empties the list has not opened the gateway, they have closed it."""
    with pytest.raises(ValidationError):
        TrustedProxyAuthConfig.model_validate(_block(allowedIdentities=[]))


@pytest.mark.parametrize(
    "over",
    [
        {"requiredClaims": {"hd": "example.com"}, "allowedIdentities": None},
        {"allowAnyVerifiedIdentity": True, "allowedIdentities": None},
        {"allowedIdentities": [_OPERATOR]},
    ],
)
def test_each_way_of_naming_somebody_validates(over: dict[str, Any]) -> None:
    config = TrustedProxyAuthConfig.model_validate(_block(**over))

    assert config.assertion_format == "jwt"


# -- the startup echo ----------------------------------------------------------------------


def test_no_proxy_echoes_the_path_actor() -> None:
    """A deployment with no proxy behaves as it did before #58, and the echo says so."""
    posture = describe_trusted_proxy_posture(None)

    assert isinstance(posture, AssertionPosture)
    assert posture.warn is False
    assert "webui" in posture.message


def test_a_plain_deployment_gains_a_warning_that_names_what_it_trusts() -> None:
    config = TrustedProxyAuthConfig.model_validate(
        {
            "trustedPeerCidrs": ["127.0.0.1/32"],
            "assertionHeader": "Cf-Access-Authenticated-User-Email",
            "assertionFormat": "plain",
        }
    )

    posture = describe_trusted_proxy_posture(config)

    assert posture.warn is True
    assert "plain" in posture.message
    assert "proxy alone decides" in posture.message


def test_allow_any_verified_identity_is_named_every_time_the_gateway_starts() -> None:
    """The posture an operator must not be able to forget about."""
    config = TrustedProxyAuthConfig.model_validate(
        _block(allowedIdentities=None, allowAnyVerifiedIdentity=True)
    )

    posture = describe_trusted_proxy_posture(config)

    assert posture.warn is True
    assert "allowAnyVerifiedIdentity" in posture.message
    assert _ISSUER in posture.message


def test_a_closed_jwt_deployment_echoes_its_counts_without_a_warning() -> None:
    config = TrustedProxyAuthConfig.model_validate(
        _block(requiredClaims={"hd": "example.com"})
    )

    posture = describe_trusted_proxy_posture(config)

    assert posture.warn is False
    assert _ISSUER in posture.message


def test_the_echo_names_no_identity() -> None:
    """The echo reaches a log an operator may ship elsewhere, so it counts rather than lists."""
    config = TrustedProxyAuthConfig.model_validate(_block())

    posture = describe_trusted_proxy_posture(config)

    assert _OPERATOR not in posture.message
    assert "1" in posture.message


# -- the authenticator, end to end with an injected key set --------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def key_source(signing_key: rsa.RSAPrivateKey) -> StaticJwksSource:
    numbers = signing_key.public_key().public_numbers()
    return StaticJwksSource(
        {
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
    )


def _token(
    signing_key: rsa.RSAPrivateKey,
    *,
    header: dict[str, Any] | None = None,
    **over: Any,
) -> str:
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "exp": _NOW + 300,
        "email": _OPERATOR,
    }
    claims.update(over)
    head = _b64url(json.dumps(header or {"alg": "RS256", "kid": _KID}).encode())
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


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, template: str, *args: object) -> None:
        self.lines.append(template.format(*args))

    warning = _record
    info = _record
    debug = _record


def _authenticator(
    key_source: StaticJwksSource,
    log: _Log,
    **over: Any,
) -> TrustedProxyAuthenticator:
    config = TrustedProxyAuthConfig.model_validate(_block(**over))
    return TrustedProxyAuthenticator(
        config,
        key_source=key_source,
        clock=lambda: _NOW,
        log=log,
    )


async def test_a_verified_named_identity_is_admitted(
    signing_key: rsa.RSAPrivateKey,
    key_source: StaticJwksSource,
) -> None:
    log = _Log()
    authenticator = _authenticator(key_source, log)

    identity = await authenticator.authenticate(
        _Conn(), {_HEADER: _token(signing_key)}
    )

    assert identity == _OPERATOR
    assert log.lines == []


async def test_an_assertion_from_an_untrusted_peer_gains_nothing(
    signing_key: rsa.RSAPrivateKey,
    key_source: StaticJwksSource,
) -> None:
    """The peer check survives #58. A genuine token from the wrong address buys no session."""
    authenticator = _authenticator(key_source, _Log())

    identity = await authenticator.authenticate(
        _Conn("192.0.2.7"), {_HEADER: _token(signing_key)}
    )

    assert identity == ""


async def test_a_missing_header_is_not_an_authentication(key_source: StaticJwksSource) -> None:
    authenticator = _authenticator(key_source, _Log())

    assert await authenticator.authenticate(_Conn(), {}) == ""
    assert await authenticator.authenticate(_Conn(), {_HEADER: "   "}) == ""


async def test_an_unknown_key_id_refuses_and_the_log_never_holds_the_token(
    signing_key: rsa.RSAPrivateKey,
    key_source: StaticJwksSource,
) -> None:
    """A log line is read by more accounts than a live credential should reach."""
    log = _Log()
    authenticator = _authenticator(key_source, log)
    token = _token(signing_key, header={"alg": "RS256", "kid": "rotated-away"})

    assert await authenticator.authenticate(_Conn(), {_HEADER: token}) == ""

    assert log.lines
    assert token not in " ".join(log.lines)
    assert "rotated-away" in " ".join(log.lines)


async def test_a_stranger_is_refused_and_the_log_names_the_claim_value(
    signing_key: rsa.RSAPrivateKey,
    key_source: StaticJwksSource,
) -> None:
    log = _Log()
    authenticator = _authenticator(key_source, log)
    token = _token(signing_key, email="stranger@gmail.example")

    assert await authenticator.authenticate(_Conn(), {_HEADER: token}) == ""

    assert "stranger@gmail.example" in " ".join(log.lines)
    assert token not in " ".join(log.lines)


async def test_an_hs256_token_signed_with_the_public_key_is_refused_end_to_end(
    signing_key: rsa.RSAPrivateKey,
    key_source: StaticJwksSource,
) -> None:
    """The confusion attack against the whole admission path, not just the verifier."""
    public_pem = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    head = _b64url(json.dumps({"alg": "HS256", "kid": _KID}).encode())
    body = _b64url(
        json.dumps({"iss": _ISSUER, "aud": _AUDIENCE, "exp": _NOW + 300, "email": _OPERATOR}).encode()
    )
    forged = hmac.new(public_pem, f"{head}.{body}".encode("ascii"), hashlib.sha256).digest()
    log = _Log()
    authenticator = _authenticator(key_source, log)

    identity = await authenticator.authenticate(
        _Conn(), {_HEADER: f"{head}.{body}.{_b64url(forged)}"}
    )

    assert identity == ""
    assert "algorithm_not_allowed" in " ".join(log.lines)


async def test_an_expired_token_is_refused(
    signing_key: rsa.RSAPrivateKey,
    key_source: StaticJwksSource,
) -> None:
    authenticator = _authenticator(key_source, _Log())
    token = _token(signing_key, exp=_NOW - 3600)

    assert await authenticator.authenticate(_Conn(), {_HEADER: token}) == ""


async def test_a_failed_verification_never_degrades_to_the_anonymous_actor(
    signing_key: rsa.RSAPrivateKey,
    key_source: StaticJwksSource,
) -> None:
    """A refusal is a refusal. Falling back to ``webui`` would be a downgrade attack.

    A forged token would then buy the privileges of the shared token, which is the opposite of
    what verification is for.
    """
    authenticator = _authenticator(key_source, _Log())
    head, body, _ = _token(signing_key).split(".")

    identity = await authenticator.authenticate(_Conn(), {_HEADER: f"{head}.{body}.AAAA"})

    assert identity == ""


async def test_a_plain_block_reads_the_header_as_the_identity() -> None:
    """The older path is unchanged, and one seam now carries both formats."""
    config = TrustedProxyAuthConfig.model_validate(
        {
            "trustedPeerCidrs": ["127.0.0.1/32"],
            "assertionHeader": _HEADER,
            "assertionFormat": "plain",
        }
    )
    authenticator = TrustedProxyAuthenticator(config, key_source=None, log=_Log())

    assert await authenticator.authenticate(_Conn(), {_HEADER: _OPERATOR}) == _OPERATOR
    assert await authenticator.authenticate(_Conn("192.0.2.7"), {_HEADER: _OPERATOR}) == ""


async def test_a_jwt_block_with_no_key_source_admits_nobody(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """Fail closed. A verifier with no key cannot verify, so it must not admit."""
    config = TrustedProxyAuthConfig.model_validate(_block())
    authenticator = TrustedProxyAuthenticator(config, key_source=None, log=_Log())

    assert await authenticator.authenticate(_Conn(), {_HEADER: _token(signing_key)}) == ""
