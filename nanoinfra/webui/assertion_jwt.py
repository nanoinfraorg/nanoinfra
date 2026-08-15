"""Verify a JWT assertion from a trusted proxy -- nanoinfraorg/nanoinfra#58.

A proxy in front of the gateway authenticates a person and asserts the result in a header.
Before #58 the peer CIDR was the only barrier, so anything that reached the gateway from that
CIDR could invent a person, and a proxy that forwarded a client-supplied header handed the
same power to a browser. This module removes that gap: the assertion carries a signature and
the signature is checked.

Five rules hold, and each one closes a known attack.

1. The algorithm comes from ``_ALLOWED_ALGORITHMS`` and never from the token. ``none`` is a
   token with no signature. ``HS256`` is the classic confusion attack: a verifier that read
   ``alg`` from the token would call an HMAC with the RSA public key as the secret, and a
   JWKS publishes that key to everybody.
2. ``iss`` and ``aud`` come from config. A valid token of another realm, or of another
   application of the same realm, authenticates nobody here.
3. ``exp`` is enforced, and ``nbf`` and ``iat`` when present, against one fixed skew. A token
   with no ``exp`` refuses, because a stolen one would then authenticate forever.
4. The key comes from the JWKS by ``kid``. A token with no ``kid`` refuses, because a verifier
   that guessed would try every key and turn a rotation into an oracle.
5. The token holds exactly three segments and each one decodes under a strict base64url. A
   lenient decoder accepts ``+``, ``/`` and stray padding, so two different strings decode to
   the same bytes, and the signature covers the string rather than the bytes.

**No I/O lives here, and that is deliberate.** The key arrives as an argument, so every rule
above is testable with no network and no identity provider. ``assertion_jwks.py`` owns the
fetch and holds the only outbound client.

**No new dependency.** ``cryptography`` is already a direct dependency, it verifies
RSASSA-PKCS1-v1_5 with SHA-256, and it builds a public key from the ``n`` and ``e`` of a JWK.
The rest is base64url and JSON. #20 reached Landlock through ``ctypes`` for the same reason.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# The one algorithm this gateway accepts. It is a frozenset rather than a config field,
# because an operator who could add an algorithm could add ``HS256``, and rule 1 above is
# the reason that must be impossible from config.
_ALLOWED_ALGORITHMS = frozenset({"RS256"})

# The skew that covers two clocks that disagree, in seconds. It is fixed for the same reason:
# a config field would let a deployment widen a token's life without saying so.
CLOCK_SKEW_S = 60.0

# The base64url alphabet, with no padding character. A segment that holds anything else is
# refused rather than repaired, because a repair changes the bytes the signature covered.
_B64URL_SEGMENT = re.compile(r"[A-Za-z0-9_-]*")

# The JWK fields this module reads. ``kty`` names the family, ``use`` and ``alg`` say whether
# the key signs, and ``n`` and ``e`` are the key itself.
_RSA_KEY_TYPE = "RSA"
_SIGNATURE_USE = "sig"


class AssertionRefusal(StrEnum):
    """Why the assertion did not authenticate anybody.

    A bare ``False`` would force every caller to guess between a token this gateway cannot
    parse, a key it does not hold, and a person it does not admit. Those are three different
    operator actions, so the reason is a value.

    The gateway logs the reason and the client reads none of it (#62). An error text that
    named the rule would tell an attacker which rule to attack next.
    """

    MALFORMED = "malformed"
    ALGORITHM_NOT_ALLOWED = "algorithm_not_allowed"
    NO_KEY_ID = "no_key_id"
    UNKNOWN_KEY_ID = "unknown_key_id"
    NO_KEYS_AVAILABLE = "no_keys_available"
    BAD_SIGNATURE = "bad_signature"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    ISSUED_IN_THE_FUTURE = "issued_in_the_future"
    NO_IDENTITY_CLAIM = "no_identity_claim"
    NOT_AN_ADMITTED_IDENTITY = "not_an_admitted_identity"
    CLAIM_DOES_NOT_MATCH = "claim_does_not_match"


class AssertionRefusedError(Exception):
    """One refusal, with the reason as a value and a detail for the operator log.

    ``detail`` never holds the token. A log line that carried one would put a live credential
    in a file that a wider set of accounts can read.
    """

    def __init__(self, reason: AssertionRefusal, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class VerifiedAssertion:
    """The claims of a token whose signature, issuer, audience and lifetime all checked out.

    ``claims`` is only reachable through this type, so a caller cannot hold the claims of a
    token that failed. The access decision of #62 reads them from here.
    """

    claims: Mapping[str, Any]
    key_id: str


def _decode_segment(segment: str, *, what: str) -> bytes:
    """Decode one base64url segment strictly, and add the padding here.

    The padding is computed rather than trusted. A segment that arrives already padded is a
    different string from the one the signature covered, so it refuses instead of decoding.
    """
    if _B64URL_SEGMENT.fullmatch(segment) is None:
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, f"{what} is not base64url")
    if not segment:
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, f"{what} is empty")
    if len(segment) % 4 == 1:
        # No base64 encoder emits this length. A decoder that accepted it would be guessing.
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, f"{what} has an impossible length")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, TypeError) as exc:  # pragma: no cover - the regex covers the cases
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, f"{what} does not decode") from exc


def _decode_json_object(segment: str, *, what: str) -> dict[str, Any]:
    try:
        parsed = cast(object, json.loads(_decode_segment(segment, what=what)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, f"{what} is not JSON") from exc
    if not isinstance(parsed, dict):
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, f"{what} is not a JSON object")
    return cast(dict[str, Any], parsed)


def _split_token(token: str) -> tuple[str, str, str]:
    """Split a compact JWS into its three segments, and refuse any other count.

    A JWE holds five segments and carries no readable claim set. A signature check over three
    of five would report success about a token it had not read.
    """
    segments = token.strip().split(".")
    if len(segments) != 3:
        raise AssertionRefusedError(
            AssertionRefusal.MALFORMED,
            f"a compact JWS holds 3 segments, this one holds {len(segments)}",
        )
    return segments[0], segments[1], segments[2]


def _read_header(head: str) -> dict[str, Any]:
    """Read the header, and enforce rule 1 before anything else in it is used."""
    header = _decode_json_object(head, what="header")
    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in _ALLOWED_ALGORITHMS:
        raise AssertionRefusedError(
            AssertionRefusal.ALGORITHM_NOT_ALLOWED,
            f"alg must be one of {sorted(_ALLOWED_ALGORITHMS)}",
        )
    if "crit" in header:
        # RFC 7515 4.1.11: a receiver must refuse a critical parameter it does not
        # understand. This verifier understands none, so any ``crit`` refuses.
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, "crit is not supported")
    return header


def _key_id_of(header: Mapping[str, Any]) -> str:
    """Read ``kid`` from a header, in one place, because two copies of a rule can disagree."""
    key_id = cast(object, header.get("kid"))
    if not isinstance(key_id, str) or not key_id.strip():
        raise AssertionRefusedError(AssertionRefusal.NO_KEY_ID, "the header carries no usable kid")
    return key_id.strip()


def read_key_id(token: str) -> str:
    """Return the ``kid`` of a token whose algorithm this gateway accepts.

    A caller needs the key id before it can choose a key, and choosing a key is not trusting
    the header: ``verify_assertion`` reads the header again and repeats rule 1, so a caller
    that skipped this function still gains nothing.
    """
    head, _, _ = _split_token(token)
    return _key_id_of(_read_header(head))


def _numeric_claim(claims: Mapping[str, Any], name: str) -> float | None:
    """Read a NumericDate claim, or refuse a value that is present and not a number.

    A string is not a time. Treating ``"1755000000"`` as absent would skip the check the
    claim names, which is how a lenient verifier turns an expiry into a suggestion. ``bool``
    is an ``int`` in Python, so it is refused by name.
    """
    if name not in claims:
        return None
    value = cast(object, claims[name])
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AssertionRefusedError(AssertionRefusal.MALFORMED, f"{name} is not a number")
    return float(value)


def _check_audience(claims: Mapping[str, Any], audience: str) -> None:
    """Match ``aud`` against config, in both shapes RFC 7519 allows.

    A single string is the common shape and Keycloak emits an array, so both are read. Any
    other type refuses rather than being coerced.
    """
    value = cast(object, claims.get("aud"))
    if isinstance(value, str):
        if value == audience:
            return
    elif isinstance(value, list):
        if any(isinstance(item, str) and item == audience for item in cast(list[object], value)):
            return
    raise AssertionRefusedError(AssertionRefusal.WRONG_AUDIENCE, "aud does not name this gateway")


def _check_lifetime(claims: Mapping[str, Any], *, now: float, skew_s: float) -> None:
    expires_at = _numeric_claim(claims, "exp")
    if expires_at is None:
        raise AssertionRefusedError(AssertionRefusal.EXPIRED, "the token carries no exp")
    if now > expires_at + skew_s:
        raise AssertionRefusedError(AssertionRefusal.EXPIRED, "the token expired")
    not_before = _numeric_claim(claims, "nbf")
    if not_before is not None and now < not_before - skew_s:
        raise AssertionRefusedError(AssertionRefusal.NOT_YET_VALID, "nbf is in the future")
    issued_at = _numeric_claim(claims, "iat")
    if issued_at is not None and now < issued_at - skew_s:
        raise AssertionRefusedError(AssertionRefusal.ISSUED_IN_THE_FUTURE, "iat is in the future")


def verify_assertion(
    token: str,
    *,
    public_key: rsa.RSAPublicKey,
    issuer: str,
    audience: str,
    now: float,
    skew_s: float = CLOCK_SKEW_S,
) -> VerifiedAssertion:
    """Verify one assertion against one key, or raise ``AssertionRefusedError``.

    The signature is checked before any claim is read. That order is the rule: a claim of an
    unsigned token is attacker text, so a verifier that reported "expired" for a forged token
    had already acted on a claim set nobody signed.
    """
    head, body, signature_segment = _split_token(token)
    key_id = _key_id_of(_read_header(head))
    signature = _decode_segment(signature_segment, what="signature")
    try:
        public_key.verify(
            signature,
            f"{head}.{body}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise AssertionRefusedError(AssertionRefusal.BAD_SIGNATURE, "the signature does not check") from exc

    claims = _decode_json_object(body, what="claims")
    if claims.get("iss") != issuer:
        raise AssertionRefusedError(AssertionRefusal.WRONG_ISSUER, "iss does not match config")
    _check_audience(claims, audience)
    _check_lifetime(claims, now=now, skew_s=skew_s)
    return VerifiedAssertion(claims=claims, key_id=key_id)


def _rsa_public_key_from_jwk(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey | None:
    """Build one RSA public key from a JWK, or return None when the entry cannot sign.

    A JWKS legitimately carries an encryption key and a key of another family. One unusable
    entry must not cost the whole document, because a key rotation would then lock every
    operator out of a gateway whose config is correct.
    """
    if jwk.get("kty") != _RSA_KEY_TYPE:
        return None
    use = cast(object, jwk.get("use"))
    if isinstance(use, str) and use != _SIGNATURE_USE:
        return None
    algorithm = cast(object, jwk.get("alg"))
    if isinstance(algorithm, str) and algorithm not in _ALLOWED_ALGORITHMS:
        return None
    modulus = cast(object, jwk.get("n"))
    exponent = cast(object, jwk.get("e"))
    if not isinstance(modulus, str) or not isinstance(exponent, str):
        return None
    try:
        n = int.from_bytes(_decode_segment(modulus, what="n"), "big")
        e = int.from_bytes(_decode_segment(exponent, what="e"), "big")
    except AssertionRefusedError:
        return None
    if n <= 0 or e <= 0:
        return None
    try:
        return rsa.RSAPublicNumbers(e, n).public_key()
    except ValueError:
        return None


def key_set_from_jwks(document: object) -> dict[str, rsa.RSAPublicKey]:
    """Read a JWKS document into ``kid`` -> public key.

    The parse is total: an entry this gateway cannot use is skipped and never raises, so a
    provider that adds a key of a new family does not break a working deployment. An empty
    result is the fail-closed answer, and the caller refuses every assertion on it.
    """
    if not isinstance(document, dict):
        return {}
    entries = cast(object, cast(dict[str, Any], document).get("keys"))
    if not isinstance(entries, list):
        return {}
    key_set: dict[str, rsa.RSAPublicKey] = {}
    for entry in cast(list[object], entries):
        if not isinstance(entry, dict):
            continue
        jwk = cast(dict[str, Any], entry)
        key_id = cast(object, jwk.get("kid"))
        if not isinstance(key_id, str) or not key_id.strip():
            # A key nothing can name is a key nothing can select, and rule 4 selects by kid.
            continue
        key = _rsa_public_key_from_jwk(jwk)
        if key is not None:
            key_set[key_id.strip()] = key
    return key_set


__all__ = [
    "CLOCK_SKEW_S",
    "AssertionRefusal",
    "AssertionRefusedError",
    "VerifiedAssertion",
    "key_set_from_jwks",
    "read_key_id",
    "verify_assertion",
]
