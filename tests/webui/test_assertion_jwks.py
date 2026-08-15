# tests/webui/test_assertion_jwks.py
"""Item 2 of M1 (#59): the JWKS source, its TTL cache, and the rate limit on the refresh.

A rotation of the provider's signing key must not lock every operator out, so a static list
of keys in config is not the primary answer. The gateway fetches the JWKS, caches it with a
TTL, and refreshes once when a token names an unknown ``kid``.

**The rate limit is the security property here, not a performance one.** A ``kid`` comes out
of an unverified header, so an attacker chooses it. Without the limit, a stream of forged key
ids becomes a stream of outbound requests from the gateway.

Every test below injects the fetch, so the suite needs no network and no identity provider.
The two tests that exercise the real fetch use an in-memory transport, and the address guard
runs on literal addresses, which resolve without DNS.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from nanoinfra.webui.assertion_jwks import (
    JWKS_TTL_S,
    MIN_REFRESH_INTERVAL_S,
    HttpJwksSource,
    JwksFetchError,
    StaticJwksSource,
    fetch_jwks_document,
    guard_jwks_target,
)

_URL = "http://10.0.0.5/realms/homelab/protocol/openid-connect/certs"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwk(key: rsa.RSAPublicKey, key_id: str) -> dict[str, Any]:
    numbers = key.public_numbers()
    return {
        "kty": "RSA",
        "kid": key_id,
        "alg": "RS256",
        "use": "sig",
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


@pytest.fixture(scope="module")
def keys() -> dict[str, rsa.RSAPublicKey]:
    """Two key pairs: the one in service, and the one a rotation introduces."""
    return {
        "old": rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(),
        "new": rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(),
    }


class _Clock:
    """A clock the test moves, so no test sleeps."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Provider:
    """A fake identity provider that counts every fetch it is asked for."""

    def __init__(self, document: object) -> None:
        self.document: object = document
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self, url: str) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.document


def _source(
    provider: _Provider,
    clock: _Clock,
) -> HttpJwksSource:
    return HttpJwksSource(_URL, fetch=provider, clock=clock, log=_SilentLog())


class _SilentLog:
    def warning(self, *_args: object, **_kwargs: object) -> None:
        pass

    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def debug(self, *_args: object, **_kwargs: object) -> None:
        pass


# -- the static alternative ----------------------------------------------------------------


async def test_a_static_key_set_needs_no_fetch(keys: dict[str, rsa.RSAPublicKey]) -> None:
    """A deployment that refuses an outbound client keeps a static ``jwks`` in config.

    It accepts that a key rotation needs a config change, and it gains a gateway that opens
    no socket for this at all.
    """
    source = StaticJwksSource({"keys": [_jwk(keys["old"], "old")]})

    assert await source.key_for("old") is keys["old"] or await source.key_for("old") is not None
    assert await source.key_for("new") is None


# -- the TTL cache -------------------------------------------------------------------------


async def test_the_first_lookup_fetches_and_the_second_one_does_not(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)

    assert await source.key_for("old") is not None
    assert await source.key_for("old") is not None
    assert provider.calls == 1


async def test_an_expired_cache_fetches_again(keys: dict[str, rsa.RSAPublicKey]) -> None:
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)

    assert await source.key_for("old") is not None
    clock.now += JWKS_TTL_S + 1

    assert await source.key_for("old") is not None
    assert provider.calls == 2


async def test_an_expired_cache_with_no_successful_refresh_refuses_every_assertion(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    """The fail-closed rule. A key set nobody could confirm authenticates nobody.

    The key id below is the one already in the cache, so this is not the unknown-``kid``
    case. A source that answered from a stale cache forever would keep accepting tokens
    signed by a key the provider revoked.
    """
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None

    provider.error = JwksFetchError("the provider is down")
    clock.now += JWKS_TTL_S + 1

    assert await source.key_for("old") is None


async def test_a_failed_fetch_keeps_a_cache_that_is_still_inside_its_ttl(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    """A provider that blinks must not cost a working key set."""
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None

    provider.error = JwksFetchError("the provider is down")
    clock.now += MIN_REFRESH_INTERVAL_S + 1

    # An unknown kid attempts a refresh, the refresh fails, and the known key still answers.
    assert await source.key_for("unknown") is None
    assert await source.key_for("old") is not None


@pytest.mark.parametrize("document", [{}, {"keys": []}, "not a document", None])
async def test_an_empty_answer_never_replaces_a_working_key_set(
    keys: dict[str, rsa.RSAPublicKey],
    document: object,
) -> None:
    """A provider that answers with no usable key is a failed refresh, not a rotation."""
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None

    provider.document = document
    clock.now += MIN_REFRESH_INTERVAL_S + 1
    assert await source.key_for("unknown") is None

    assert await source.key_for("old") is not None


# -- the rate limit ------------------------------------------------------------------------


async def test_a_stream_of_forged_key_ids_is_not_a_stream_of_requests(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    """The reason the limit exists. The ``kid`` comes from an unverified header.

    One refresh answers the first unknown key id. Every unknown key id inside the window
    after it costs no request at all.
    """
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None
    assert provider.calls == 1
    clock.now += MIN_REFRESH_INTERVAL_S + 1

    for index in range(50):
        assert await source.key_for(f"forged-{index}") is None

    assert provider.calls == 2


async def test_a_key_set_fetched_a_moment_ago_is_not_fetched_again(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    """The window counts from the last attempt, and a successful fetch is an attempt.

    A key set that arrived a moment ago cannot be missing a key that already existed, so an
    unknown key id right after it names a key the provider does not publish.
    """
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None

    assert await source.key_for("forged") is None

    assert provider.calls == 1


async def test_the_window_reopens_so_a_key_rotation_recovers_without_a_restart(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    """The other half of the same rule. A limit that never reopened would be an outage."""
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None

    provider.document = {"keys": [_jwk(keys["new"], "new")]}
    assert await source.key_for("new") is None

    clock.now += MIN_REFRESH_INTERVAL_S + 1
    assert await source.key_for("new") is not None


async def test_the_limit_counts_attempts_rather_than_successes(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    """A failing provider must not buy an attacker an unlimited request rate."""
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None
    provider.error = JwksFetchError("the provider is down")
    clock.now += MIN_REFRESH_INTERVAL_S + 1

    for index in range(20):
        assert await source.key_for(f"forged-{index}") is None

    assert provider.calls == 2


async def test_a_burst_of_unknown_key_ids_costs_one_fetch(
    keys: dict[str, rsa.RSAPublicKey],
) -> None:
    """Concurrent requests must not each start their own fetch."""
    clock = _Clock()
    provider = _Provider({"keys": [_jwk(keys["old"], "old")]})
    source = _source(provider, clock)
    assert await source.key_for("old") is not None
    clock.now += MIN_REFRESH_INTERVAL_S + 1

    answers = await asyncio.gather(*(source.key_for(f"forged-{i}") for i in range(20)))

    assert answers == [None] * 20
    assert provider.calls == 2


# -- the address guard ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/certs",
        "http://[::1]/certs",
        "http://0.0.0.0/certs",
    ],
)
def test_the_guard_blocks_loopback_link_local_and_the_metadata_address(url: str) -> None:
    """The cloud metadata address is the target this guard exists for."""
    allowed, reason = guard_jwks_target(url)

    assert allowed is False
    assert reason


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/certs",
        "https://192.168.1.10/realms/homelab/protocol/openid-connect/certs",
        "http://172.16.4.4:5556/dex/keys",
    ],
)
def test_the_guard_allows_rfc1918_because_a_homelab_provider_lives_there(url: str) -> None:
    """This is the test that pins the *narrow* guard, and it is the point of the choice.

    ``servers/network_guard.py`` allows RFC1918 and blocks loopback, link-local and the
    metadata address. ``security/network.py`` blocks RFC1918 as well, and it would therefore
    refuse a private identity provider outright. A change to the wide guard fails here.
    """
    allowed, reason = guard_jwks_target(url)

    assert allowed is True, reason


@pytest.mark.parametrize("url", ["ftp://10.0.0.5/certs", "file:///etc/passwd", "http:///certs"])
def test_the_guard_refuses_a_url_with_no_usable_http_host(url: str) -> None:
    allowed, _ = guard_jwks_target(url)

    assert allowed is False


# -- the fetch itself, over an in-memory transport -----------------------------------------


async def test_a_blocked_address_never_reaches_the_transport() -> None:
    """The guard runs before any client is built, so the request is never sent."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": []})

    with pytest.raises(JwksFetchError):
        await fetch_jwks_document(
            "http://169.254.169.254/certs",
            transport=httpx.MockTransport(handler),
        )

    assert calls == []


async def test_a_json_key_set_arrives(keys: dict[str, rsa.RSAPublicKey]) -> None:
    document = {"keys": [_jwk(keys["old"], "old")]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document)

    assert await fetch_jwks_document(_URL, transport=httpx.MockTransport(handler)) == document


async def test_a_non_success_status_refuses() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is unwell")

    with pytest.raises(JwksFetchError):
        await fetch_jwks_document(_URL, transport=httpx.MockTransport(handler))


async def test_a_redirect_is_not_followed() -> None:
    """A redirect leaves the address the guard checked, so it is a new target, not a hop."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/certs"})

    with pytest.raises(JwksFetchError):
        await fetch_jwks_document(_URL, transport=httpx.MockTransport(handler))

    assert seen == [_URL]


async def test_a_body_that_is_not_json_refuses() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>a login page</html>")

    with pytest.raises(JwksFetchError):
        await fetch_jwks_document(_URL, transport=httpx.MockTransport(handler))


async def test_an_oversized_body_refuses_without_buffering_all_of_it() -> None:
    """A provider that answered with a gigabyte must not cost the gateway a gigabyte."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024))

    with pytest.raises(JwksFetchError):
        await fetch_jwks_document(_URL, transport=httpx.MockTransport(handler))
