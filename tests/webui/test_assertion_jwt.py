# tests/webui/test_assertion_jwt.py
"""Item 1 of M1 (#58): the JWT assertion verifier, and the algorithms that break one.

The suite signs its own tokens. It generates one RSA key pair, builds each token segment by
hand, and needs no identity provider, no container and no network. A test that needed one of
those would not run in CI.

Every rule of #58 gets a case here, and each case names the attack it closes:

* ``alg`` comes from a fixed set. ``none`` buys a token with no signature at all. ``HS256``
  buys the classic confusion attack, because a verifier that accepted it would take the RSA
  public key as an HMAC secret, and that key is public.
* ``iss`` and ``aud`` come from config. A valid token of another realm, or of another
  application of the same realm, authenticates nobody here.
* ``exp`` is enforced, and ``nbf`` and ``iat`` when present, with one fixed skew.
* the key comes from the JWKS by ``kid``, and a token with no ``kid`` refuses.
* the segment count is exactly three, and the base64url decode is strict, because a lenient
  decoder accepts bytes the signature never covered.
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

from nanoinfra.webui.assertion_jwt import (
    AssertionRefusal,
    AssertionRefusedError,
    key_set_from_jwks,
    read_key_id,
    verify_assertion,
)

_ISSUER = "https://idp.example/realms/homelab"
_AUDIENCE = "nanoinfra-gateway"
_KID = "key-2026-08"
_NOW = 1_755_000_000.0


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    """One key pair for the whole module. Generating one per test buys nothing and costs time."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def public_key(private_key: rsa.RSAPrivateKey) -> rsa.RSAPublicKey:
    return private_key.public_key()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _segment(payload: dict[str, Any]) -> str:
    return _b64url(json.dumps(payload).encode("utf-8"))


def _claims(**over: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "exp": _NOW + 300,
        "iat": _NOW - 10,
        "email": "operator@example.com",
    }
    claims.update(over)
    return {name: value for name, value in claims.items() if value is not _ABSENT}


class _Absent:
    """A marker for "this claim is not in the token", which ``None`` cannot express."""


_ABSENT = _Absent()


def _sign(
    private_key: rsa.RSAPrivateKey,
    *,
    header: dict[str, Any] | None = None,
    claims: dict[str, Any] | None = None,
) -> str:
    head = _segment(header if header is not None else {"alg": "RS256", "kid": _KID})
    body = _segment(claims if claims is not None else _claims())
    signature = private_key.sign(
        f"{head}.{body}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{head}.{body}.{_b64url(signature)}"


def _verify(token: str, public_key: rsa.RSAPublicKey, *, now: float = _NOW) -> dict[str, Any]:
    verified = verify_assertion(
        token,
        public_key=public_key,
        issuer=_ISSUER,
        audience=_AUDIENCE,
        now=now,
    )
    return dict(verified.claims)


def _refusal(
    token: str,
    public_key: rsa.RSAPublicKey,
    *,
    now: float = _NOW,
) -> AssertionRefusal:
    with pytest.raises(AssertionRefusedError) as caught:
        _verify(token, public_key, now=now)
    return caught.value.reason


# -- the happy path, so no refusal below passes for the wrong reason ----------------------


def test_a_genuine_rs256_token_verifies(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    claims = _verify(_sign(private_key), public_key)

    assert claims["email"] == "operator@example.com"
    assert read_key_id(_sign(private_key)) == _KID


# -- rule 1: the algorithm comes from a fixed set, and never from the token ---------------


def test_the_none_algorithm_refuses(private_key: rsa.RSAPrivateKey, public_key: rsa.RSAPublicKey) -> None:
    """``alg: none`` is a token with no signature. It must never be a token with no check."""
    head = _segment({"alg": "none", "kid": _KID})
    body = _segment(_claims())

    assert _refusal(f"{head}.{body}.", public_key) is AssertionRefusal.ALGORITHM_NOT_ALLOWED
    assert _refusal(f"{head}.{body}.ignored", public_key) is AssertionRefusal.ALGORITHM_NOT_ALLOWED


def test_an_hs256_token_signed_with_the_rsa_public_key_refuses(
    public_key: rsa.RSAPublicKey,
) -> None:
    """The algorithm confusion attack, performed rather than described.

    The attacker holds the public key, because a JWKS publishes it. A verifier that read
    ``alg`` from the token would call an HMAC with that key as the secret, and this token
    would verify. So the refusal must arrive before any signature check runs.
    """
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    head = _segment({"alg": "HS256", "kid": _KID})
    body = _segment(_claims())
    forged = hmac.new(public_pem, f"{head}.{body}".encode("ascii"), hashlib.sha256).digest()
    token = f"{head}.{body}.{_b64url(forged)}"

    # The MAC is genuine for this key, which is what makes the case worth a test.
    assert hmac.compare_digest(
        forged,
        hmac.new(public_pem, f"{head}.{body}".encode("ascii"), hashlib.sha256).digest(),
    )
    assert _refusal(token, public_key) is AssertionRefusal.ALGORITHM_NOT_ALLOWED
    with pytest.raises(AssertionRefusedError) as caught:
        read_key_id(token)
    assert caught.value.reason is AssertionRefusal.ALGORITHM_NOT_ALLOWED


@pytest.mark.parametrize("alg", ["RS512", "PS256", "ES256", "rs256", "", "RS256 "])
def test_every_algorithm_outside_the_set_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    alg: str,
) -> None:
    token = _sign(private_key, header={"alg": alg, "kid": _KID})

    assert _refusal(token, public_key) is AssertionRefusal.ALGORITHM_NOT_ALLOWED


@pytest.mark.parametrize("alg", [None, 256, ["RS256"], {"alg": "RS256"}])
def test_a_missing_or_non_string_algorithm_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    alg: object,
) -> None:
    header: dict[str, Any] = {"kid": _KID}
    if alg is not None:
        header["alg"] = alg
    token = _sign(private_key, header=header)

    assert _refusal(token, public_key) is AssertionRefusal.ALGORITHM_NOT_ALLOWED


def test_a_critical_header_parameter_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """RFC 7515 says a receiver must refuse a ``crit`` parameter it does not understand.

    This verifier understands none of them, so every ``crit`` header refuses.
    """
    token = _sign(private_key, header={"alg": "RS256", "kid": _KID, "crit": ["exp"]})

    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED


# -- rule 4: the key comes from the JWKS by kid -------------------------------------------


@pytest.mark.parametrize("kid", [None, "", "   ", 7, ["key-2026-08"]])
def test_a_token_without_a_usable_key_id_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    kid: object,
) -> None:
    header: dict[str, Any] = {"alg": "RS256"}
    if kid is not None:
        header["kid"] = kid
    token = _sign(private_key, header=header)

    assert _refusal(token, public_key) is AssertionRefusal.NO_KEY_ID
    with pytest.raises(AssertionRefusedError) as caught:
        read_key_id(token)
    assert caught.value.reason is AssertionRefusal.NO_KEY_ID


def test_a_key_set_builds_from_the_n_and_e_of_a_jwk(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """The JWKS carries ``n`` and ``e``, and ``cryptography`` builds the key from those two."""
    numbers = public_key.public_numbers()
    document = {
        "keys": [
            {
                "kty": "RSA",
                "kid": _KID,
                "alg": "RS256",
                "use": "sig",
                "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            },
            # An encryption key belongs in a JWKS and is not a signing key. One unusable
            # entry must not cost the whole document, or a key rotation locks everybody out.
            {"kty": "RSA", "kid": "enc-key", "use": "enc", "n": "AQAB", "e": "AQAB"},
            {"kty": "EC", "kid": "ec-key", "crv": "P-256", "x": "AQAB", "y": "AQAB"},
        ]
    }

    key_set = key_set_from_jwks(document)

    assert set(key_set) == {_KID}
    claims = verify_assertion(
        _sign(private_key),
        public_key=key_set[_KID],
        issuer=_ISSUER,
        audience=_AUDIENCE,
        now=_NOW,
    ).claims
    assert claims["email"] == "operator@example.com"


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"keys": []},
        {"keys": "not-a-list"},
        {"keys": [{"kty": "RSA", "kid": "k", "n": "!!!", "e": "AQAB"}]},
        {"keys": [{"kty": "RSA", "n": "AQAB", "e": "AQAB"}]},
        "not-a-document",
    ],
)
def test_a_jwks_with_no_usable_signing_key_builds_an_empty_key_set(document: object) -> None:
    assert key_set_from_jwks(document) == {}


# -- rule 2: iss and aud come from config -------------------------------------------------


def test_another_realm_refuses(private_key: rsa.RSAPrivateKey, public_key: rsa.RSAPublicKey) -> None:
    token = _sign(private_key, claims=_claims(iss="https://idp.example/realms/other"))

    assert _refusal(token, public_key) is AssertionRefusal.WRONG_ISSUER


def test_a_missing_issuer_refuses(private_key: rsa.RSAPrivateKey, public_key: rsa.RSAPublicKey) -> None:
    token = _sign(private_key, claims=_claims(iss=_ABSENT))

    assert _refusal(token, public_key) is AssertionRefusal.WRONG_ISSUER


def test_another_application_of_the_same_realm_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    token = _sign(private_key, claims=_claims(aud="some-other-application"))

    assert _refusal(token, public_key) is AssertionRefusal.WRONG_AUDIENCE


def test_an_audience_list_that_names_this_gateway_verifies(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """RFC 7519 allows an array, and Keycloak emits one."""
    token = _sign(private_key, claims=_claims(aud=["another-application", _AUDIENCE]))

    assert _verify(token, public_key)["email"] == "operator@example.com"


@pytest.mark.parametrize("aud", [_ABSENT, [], ["other"], 7, [7], {"aud": _AUDIENCE}])
def test_an_audience_that_does_not_name_this_gateway_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    aud: object,
) -> None:
    token = _sign(private_key, claims=_claims(aud=aud))

    assert _refusal(token, public_key) is AssertionRefusal.WRONG_AUDIENCE


# -- rule 3: exp, nbf and iat with one fixed skew -----------------------------------------


def test_an_expired_token_refuses(private_key: rsa.RSAPrivateKey, public_key: rsa.RSAPublicKey) -> None:
    token = _sign(private_key, claims=_claims(exp=_NOW - 3600))

    assert _refusal(token, public_key) is AssertionRefusal.EXPIRED


def test_a_token_that_expired_inside_the_skew_still_verifies(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """The skew exists because two clocks differ, and it is fixed so no config can widen it."""
    token = _sign(private_key, claims=_claims(exp=_NOW - 30))

    assert _verify(token, public_key)["email"] == "operator@example.com"


def test_a_token_with_no_expiry_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """A token with no ``exp`` never expires, so a stolen one authenticates forever."""
    token = _sign(private_key, claims=_claims(exp=_ABSENT))

    assert _refusal(token, public_key) is AssertionRefusal.EXPIRED


@pytest.mark.parametrize("exp", ["1755000300", None, True])
def test_an_expiry_that_is_not_a_number_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    exp: object,
) -> None:
    """One rule covers ``exp``, ``nbf`` and ``iat``: present and not a number is malformed.

    ``True`` is in the list because ``bool`` is an ``int`` in Python, and a verifier that
    read it as the number 1 would call every such token expired for the wrong reason.
    """
    token = _sign(private_key, claims=_claims(exp=exp))

    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED


def test_a_token_that_is_not_valid_yet_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    token = _sign(private_key, claims=_claims(nbf=_NOW + 3600))

    assert _refusal(token, public_key) is AssertionRefusal.NOT_YET_VALID


def test_a_not_before_inside_the_skew_verifies(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    token = _sign(private_key, claims=_claims(nbf=_NOW + 30))

    assert _verify(token, public_key)["email"] == "operator@example.com"


def test_a_token_issued_in_the_future_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    token = _sign(private_key, claims=_claims(iat=_NOW + 3600))

    assert _refusal(token, public_key) is AssertionRefusal.ISSUED_IN_THE_FUTURE


@pytest.mark.parametrize("field", ["nbf", "iat"])
def test_a_non_numeric_time_claim_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    field: str,
) -> None:
    """A string is not a time. Reading one as absent would skip the check it names."""
    token = _sign(private_key, claims=_claims(**{field: "1755000000"}))

    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED


# -- the shape of the token itself ---------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 4, 5])
def test_a_token_that_does_not_hold_three_segments_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    count: int,
) -> None:
    """A JWE holds five segments, and a signature check over three of them proves nothing."""
    segments = _sign(private_key).split(".")
    token = ".".join((segments * 3)[:count])

    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED
    with pytest.raises(AssertionRefusedError) as caught:
        read_key_id(token)
    assert caught.value.reason is AssertionRefusal.MALFORMED


@pytest.mark.parametrize("token", ["", "   ", ".."])
def test_an_empty_token_refuses(public_key: rsa.RSAPublicKey, token: str) -> None:
    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED


@pytest.mark.parametrize("suffix", ["==", "+", "/", "\n", " "])
def test_a_header_segment_outside_the_base64url_alphabet_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    suffix: str,
) -> None:
    """The padding is added by the verifier rather than trusted from the token.

    A lenient decoder accepts ``+`` and ``/`` and drops stray padding, so two different
    strings decode to the same bytes. The header is read before the signature check, so a
    lenient decode here would parse bytes no signature covered.
    """
    head, body, signature = _sign(private_key).split(".")

    token = f"{head}{suffix}.{body}.{signature}"
    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED


@pytest.mark.parametrize("suffix", ["==", "+", "/"])
def test_a_signature_segment_outside_the_base64url_alphabet_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    suffix: str,
) -> None:
    """The signature segment decodes strictly too, so one signature has one encoding."""
    head, body, signature = _sign(private_key).split(".")

    token = f"{head}.{body}.{signature}{suffix}"
    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED


def test_a_signature_segment_of_an_impossible_length_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """A length one over a multiple of four is a length no base64 encoder emits."""
    head, body, signature = _sign(private_key).split(".")
    while len(signature) % 4 != 1:
        signature += "A"

    assert _refusal(f"{head}.{body}.{signature}", public_key) is AssertionRefusal.MALFORMED


def test_whitespace_inside_a_segment_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    head, body, signature = _sign(private_key).split(".")
    broken = f"{signature[:-1]}\n{signature[-1]}"

    assert _refusal(f"{head}.{body}.{broken}", public_key) is AssertionRefusal.MALFORMED


def test_whitespace_around_the_whole_token_is_stripped_once(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """The verifier strips the token itself, and this test records that as a decision.

    An HTTP header value reaches this code with the surrounding whitespace a proxy left on
    it, and refusing that would fail a correct deployment for a reason no operator can see.
    The strip is safe because it cannot change one decoded byte of the signature: the bytes
    still have to match, and the test above holds the case of whitespace inside a segment.
    """
    token = _sign(private_key)

    assert _verify(f"  {token}\n", public_key)["email"] == "operator@example.com"


@pytest.mark.parametrize("suffix", ["==", "+", "\n"])
def test_a_re_encoded_body_refuses_on_the_signature(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    suffix: str,
) -> None:
    """The body needs no separate strictness rule, and this test says why.

    The signature covers the encoded string rather than the decoded bytes, so any body a
    lenient decoder would have accepted is already a body the signature does not cover. The
    refusal is therefore the signature, and that is the stronger of the two answers.
    """
    head, body, signature = _sign(private_key).split(".")

    token = f"{head}.{body}{suffix}.{signature}"
    assert _refusal(token, public_key) is AssertionRefusal.BAD_SIGNATURE


@pytest.mark.parametrize("payload", ['"a string"', "42", "[1, 2]", "null", "not json at all"])
def test_a_body_that_is_not_a_json_object_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    payload: str,
) -> None:
    head = _segment({"alg": "RS256", "kid": _KID})
    body = _b64url(payload.encode("utf-8"))
    signature = private_key.sign(
        f"{head}.{body}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    token = f"{head}.{body}.{_b64url(signature)}"
    assert _refusal(token, public_key) is AssertionRefusal.MALFORMED


@pytest.mark.parametrize("payload", ['"a string"', "42", "not json at all"])
def test_a_header_that_is_not_a_json_object_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    payload: str,
) -> None:
    head = _b64url(payload.encode("utf-8"))
    body = _segment(_claims())

    assert _refusal(f"{head}.{body}.AAAA", public_key) is AssertionRefusal.MALFORMED


def test_one_changed_byte_of_the_signature_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    head, body, signature = _sign(private_key).split(".")
    raw = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    raw[0] ^= 0x01

    token = f"{head}.{body}.{_b64url(bytes(raw))}"
    assert _refusal(token, public_key) is AssertionRefusal.BAD_SIGNATURE


def test_a_body_swapped_after_signing_refuses(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """The whole point of the exercise: a claim set nobody signed authenticates nobody."""
    head, _, signature = _sign(private_key).split(".")
    forged = _segment(_claims(email="attacker@example.com"))

    token = f"{head}.{forged}.{signature}"
    assert _refusal(token, public_key) is AssertionRefusal.BAD_SIGNATURE


def test_the_signature_is_checked_before_any_claim_is_read(
    private_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
) -> None:
    """Order matters. A claim of an unsigned token is attacker text, not a fact.

    This token is expired *and* unsigned. The refusal must name the signature, because a
    verifier that reported the expiry read the claims of a token it had not authenticated.
    """
    head = _segment({"alg": "RS256", "kid": _KID})
    body = _segment(_claims(exp=_NOW - 3600, iss="https://attacker.example"))

    assert _refusal(f"{head}.{body}.AAAA", public_key) is AssertionRefusal.BAD_SIGNATURE
