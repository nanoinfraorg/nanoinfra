# tests/channels/test_trusted_proxy_config.py
"""Item 4 of M1 (#61): the config for a verified assertion, and a schema that refuses half of one.

``trustedProxyAuth`` used to carry two fields, and the peer CIDR was the only barrier. It now
declares which shape of assertion arrives, and a `jwt` block that is half written refuses at
load rather than at the first handshake.

**`jwt` is the default, and that is a deliberate breaking change.** A deployment that wants the
older CIDR-only trust writes ``"assertionFormat": "plain"``, so the cost is stated in the config
file rather than discovered in the documentation. The schema requires the operator to write the
weaker posture rather than fall into it.

Every refusal names the field the operator has to fix, in the camelCase form they wrote it in.
A message that said "invalid configuration" would send them to the issue tracker.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic_core import ValidationError

from nanoinfra.channels.websocket.runtime import TrustedProxyAuthConfig, WebSocketConfig

_ISSUER = "https://idp.example/realms/homelab"
_JWKS_URL = "https://idp.example/realms/homelab/protocol/openid-connect/certs"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _static_jwks() -> dict[str, Any]:
    numbers = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "static-key",
                "alg": "RS256",
                "use": "sig",
                "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _block(**over: Any) -> dict[str, Any]:
    """A complete `jwt` block, which each test then removes one field from."""
    block: dict[str, Any] = {
        "trustedPeerCidrs": ["127.0.0.1/32"],
        "assertionHeader": "X-Access-Token",
        "issuer": _ISSUER,
        "audience": "nanoinfra-gateway",
        "jwksUrl": _JWKS_URL,
        "identityClaim": "email",
        "allowedIdentities": ["operator@example.com"],
    }
    block.update(over)
    return {name: value for name, value in block.items() if value is not _DROP}


class _Drop:
    """A marker for "leave this field out", which ``None`` cannot express."""


_DROP = _Drop()


def _validate(**over: Any) -> TrustedProxyAuthConfig:
    return TrustedProxyAuthConfig.model_validate(_block(**over))


def _refusal(**over: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        _validate(**over)
    return str(caught.value)


# -- the default -----------------------------------------------------------------------------


def test_jwt_is_the_default_assertion_format() -> None:
    """An operator who writes no format gets the verified one."""
    config = _validate()

    assert config.assertion_format == "jwt"


def test_the_identity_claim_defaults_to_email() -> None:
    """An operator maintains ``gates.approvers`` in git and has to be able to read it.

    A human-readable claim is therefore the right default. ``sub`` is available for a
    deployment that prefers a stable opaque id, and the cost of ``email`` is that a person who
    changes theirs needs a config change before their approval counts again.
    """
    config = _validate(identityClaim=_DROP)

    assert config.identity_claim == "email"


def test_todays_two_field_block_no_longer_validates() -> None:
    """The breaking half of "jwt is the default", asserted rather than described.

    A block with only a CIDR list and a header name used to mean "trust whatever arrives". It
    now refuses, and the message tells the operator both ways forward.
    """
    with pytest.raises(ValidationError) as caught:
        TrustedProxyAuthConfig.model_validate(
            {"trustedPeerCidrs": ["127.0.0.1/32"], "assertionHeader": "X-Access-Token"}
        )

    text = str(caught.value)
    assert "issuer" in text
    assert "assertionFormat" in text
    assert "plain" in text


# -- a jwt block that is half written --------------------------------------------------------


@pytest.mark.parametrize(("field", "named"), [("issuer", "issuer"), ("audience", "audience")])
def test_a_missing_field_refuses_and_names_itself(field: str, named: str) -> None:
    assert named in _refusal(**{field: _DROP})


@pytest.mark.parametrize("field", ["issuer", "audience", "identityClaim"])
@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_field_refuses_the_same_way_a_missing_one_does(field: str, value: str) -> None:
    """A blank string is not a value. Accepting one would put an empty issuer into a compare."""
    assert field in _refusal(**{field: value})


def test_a_jwt_block_with_no_key_source_refuses() -> None:
    text = _refusal(jwksUrl=_DROP)

    assert "jwksUrl" in text
    assert "jwks" in text


def test_a_jwt_block_with_both_key_sources_refuses() -> None:
    """Two sources are two answers for one ``kid``, so the schema asks for one.

    A fetch and a static list disagree the moment the provider rotates, and a reader of the
    config cannot tell which one is in force.
    """
    text = _refusal(jwks=_static_jwks())

    assert "jwksUrl" in text
    assert "jwks" in text


def test_a_static_key_set_is_a_complete_alternative_to_a_url() -> None:
    """The deployment that refuses an outbound client at all."""
    config = _validate(jwksUrl=_DROP, jwks=_static_jwks())

    assert config.jwks is not None
    assert config.jwks_url == ""


@pytest.mark.parametrize(
    "jwks",
    [{}, {"keys": []}, {"keys": [{"kty": "EC", "kid": "k"}]}, {"keys": "not a list"}],
)
def test_a_static_key_set_with_no_usable_signing_key_refuses(jwks: dict[str, Any]) -> None:
    """Fail closed at load. Such a block would refuse every assertion at run time instead.

    An operator reads a startup failure. Nobody reads a handshake that silently never works.
    """
    assert "jwks" in _refusal(jwksUrl=_DROP, jwks=jwks)


@pytest.mark.parametrize(
    "url",
    ["ftp://idp.example/certs", "file:///etc/passwd", "idp.example/certs", "   "],
)
def test_a_jwks_url_that_is_not_http_refuses(url: str) -> None:
    assert "jwksUrl" in _refusal(jwksUrl=url)


# -- the plain path keeps today's semantics --------------------------------------------------


def test_a_plain_block_needs_nothing_else() -> None:
    """``plain`` keeps the older trust, because a bare string carries no signature.

    No amount of code makes one verifiable, so the format exists to be named rather than to be
    improved. The Cloudflare Access deployment the guide documents runs on it.
    """
    config = TrustedProxyAuthConfig.model_validate(
        {
            "trustedPeerCidrs": ["127.0.0.1/32"],
            "assertionHeader": "Cf-Access-Authenticated-User-Email",
            "assertionFormat": "plain",
        }
    )

    assert config.assertion_format == "plain"
    assert config.issuer == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", _ISSUER),
        ("audience", "nanoinfra-gateway"),
        ("jwksUrl", _JWKS_URL),
        ("allowedIdentities", ["operator@example.com"]),
        ("requiredClaims", {"hd": "example.com"}),
        ("allowAnyVerifiedIdentity", True),
    ],
)
def test_a_plain_block_refuses_a_field_that_only_the_jwt_path_reads(
    field: str,
    value: object,
) -> None:
    """A field that does nothing must not look like a field that does something.

    An operator who writes ``allowedIdentities`` under ``plain`` believes the gateway enforces
    it. On that path the proxy alone decides, so the schema refuses instead of ignoring it.
    """
    with pytest.raises(ValidationError) as caught:
        TrustedProxyAuthConfig.model_validate(
            {
                "trustedPeerCidrs": ["127.0.0.1/32"],
                "assertionHeader": "Cf-Access-Authenticated-User-Email",
                "assertionFormat": "plain",
                field: value,
            }
        )

    assert field in str(caught.value)


@pytest.mark.parametrize("assertion_format", ["JWT", "Plain", "none", "", "opaque"])
def test_an_unknown_assertion_format_refuses(assertion_format: str) -> None:
    assert "assertionFormat" in _refusal(assertionFormat=assertion_format)


# -- the aliases -----------------------------------------------------------------------------


def test_every_new_field_takes_its_camel_case_alias() -> None:
    """The rest of this schema is camelCase in JSON, and an operator writes JSON."""
    config = TrustedProxyAuthConfig.model_validate(
        {
            "trustedPeerCidrs": ["10.0.0.0/8"],
            "assertionHeader": "X-Access-Token",
            "assertionFormat": "jwt",
            "issuer": _ISSUER,
            "audience": "nanoinfra-gateway",
            "jwksUrl": _JWKS_URL,
            "identityClaim": "sub",
            "allowedIdentities": ["operator@example.com"],
            "requiredClaims": {"hd": "example.com"},
            "allowAnyVerifiedIdentity": False,
        }
    )

    assert config.jwks_url == _JWKS_URL
    assert config.identity_claim == "sub"
    assert config.allowed_identities == ["operator@example.com"]
    assert config.required_claims == {"hd": "example.com"}
    assert config.allow_any_verified_identity is False


def test_the_snake_case_form_still_validates() -> None:
    """``populate_by_name`` is on for every block in this schema, and this one keeps it."""
    config = TrustedProxyAuthConfig.model_validate(
        {
            "trusted_peer_cidrs": ["10.0.0.0/8"],
            "assertion_header": "X-Access-Token",
            "assertion_format": "jwt",
            "issuer": _ISSUER,
            "audience": "nanoinfra-gateway",
            "jwks_url": _JWKS_URL,
            "identity_claim": "email",
            "allow_any_verified_identity": True,
        }
    )

    assert config.jwks_url == _JWKS_URL
    assert config.allow_any_verified_identity is True


def test_the_block_round_trips_through_the_channel_config() -> None:
    """The block is reached through ``WebSocketConfig``, so it validates in place too."""
    config = WebSocketConfig.model_validate({"trustedProxyAuth": _block()})

    assert config.trusted_proxy_auth is not None
    assert config.trusted_proxy_auth.assertion_format == "jwt"
    assert config.trusted_proxy_auth.issuer == _ISSUER
