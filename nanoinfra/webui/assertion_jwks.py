"""Where the signing keys come from -- nanoinfraorg/nanoinfra#59.

A rotation of the identity provider's signing key must not lock every operator out, so a
static list of keys in config is not the primary answer. The gateway fetches the JWKS from
``jwksUrl``, caches it with a TTL, and refreshes once when a token names an unknown ``kid``.

**The rate limit on that refresh is a security property, not a performance one.** The ``kid``
arrives inside an unverified header, so an attacker chooses it. Without the limit, a stream of
forged key ids becomes a stream of outbound requests from the gateway.

**This module holds the only outbound HTTP client in the gateway process, and that weakens a
boundary #19 built.** The statement to preserve is that the agent process holds no transport.
Four things carry it, and the fourth is the honest cost:

1. The target is one operator-configured URL from git-reviewed config. No model output and no
   web page reaches it.
2. The address check is ``servers/network_guard.py`` and **not** ``security/network.py``. See
   ``guard_jwks_target`` for the reason, which is the whole point of the choice.
3. An AST import closure keeps every module under ``agent/tools/`` away from this module
   (``tests/webui/test_assertion_isolation.py``, #60).
4. A compromised agent that runs arbitrary code inside the gateway defeats item 3. It then
   holds one HTTP client aimed at one host. That is a real widening of the egress surface, and
   it is recorded in ``.agent/security.md`` beside the other accepted risks.

A deployment that refuses even that cost keeps a static ``jwks`` in config and uses
``StaticJwksSource``. It accepts that a key rotation needs a config change, and it gains a
gateway that opens no socket for this at all.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from loguru import logger as _default_log

from nanoinfra.servers.network_guard import validate_server_target
from nanoinfra.webui.assertion_jwt import key_set_from_jwks

# How long a fetched key set is used without asking the provider again.
JWKS_TTL_S = 600.0

# The floor between two fetch attempts. The window is what turns a stream of forged key ids
# into one request. It counts attempts rather than successes, so a provider that is down does
# not buy an attacker an unlimited request rate.
MIN_REFRESH_INTERVAL_S = 60.0

# One fetch has this long in total. A handshake waits on it, so a slow provider must not hold
# a connection open for minutes.
FETCH_TIMEOUT_S = 5.0

# The largest JWKS this gateway reads. A real one is a few kilobytes. The body is read in
# pieces and the count is checked as it goes, so an answer of a gigabyte costs this many bytes
# rather than a gigabyte of memory.
MAX_JWKS_BYTES = 256 * 1024

_HTTP_SCHEMES = frozenset({"http", "https"})


class JwksFetchError(Exception):
    """One failed fetch. The reason is for the operator log and never for a client."""


class JwksSource(Protocol):
    """The seam a verifier holds. It answers with a key or with nothing."""

    async def key_for(self, key_id: str) -> rsa.RSAPublicKey | None:
        """Return the signing key for ``key_id``, or None when this source has none."""
        ...


def guard_jwks_target(url: str) -> tuple[bool, str]:
    """Check the address of the JWKS URL with the narrow guard, and say why that one.

    ``servers/network_guard.py`` blocks loopback, link-local, the cloud metadata address and
    the unspecified address, and it **allows** RFC1918, because inventoried infrastructure
    legitimately lives there. A homelab identity provider is exactly that case.

    ``security/network.py`` is the wrong guard here. It blocks RFC1918 as well, because it
    exists for arbitrary and possibly attacker-influenced URLs. It would refuse a private
    identity provider outright, which is the common deployment this feature is for.

    The trade-off is stated rather than hidden: a gateway on a cloud instance can be pointed
    at any RFC1918 address in its VPC. The URL comes from git-reviewed config, so the operator
    who could do that could also change the config in twenty other ways.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in _HTTP_SCHEMES:
        return False, f"jwksUrl must be http or https, not {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return False, "jwksUrl names no host"
    return validate_server_target(host)


async def fetch_jwks_document(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> object:
    """Fetch and parse one JWKS document, or raise ``JwksFetchError``.

    ``transport`` exists so the fetch itself is testable with no network. Production passes
    none and httpx builds its own.

    The address is checked before any client is built, and redirects are not followed. A
    redirect would leave the address the guard just checked, so it is a new target rather
    than a hop, and this code refuses to chase one.
    """
    allowed, reason = await asyncio.to_thread(guard_jwks_target, url)
    if not allowed:
        raise JwksFetchError(reason)
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=False,
            transport=transport,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise JwksFetchError(f"the provider answered {response.status_code}")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_JWKS_BYTES:
                        raise JwksFetchError(f"the JWKS is larger than {MAX_JWKS_BYTES} bytes")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise JwksFetchError(f"the fetch failed: {exc}") from exc
    try:
        return cast(object, json.loads(b"".join(chunks)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise JwksFetchError("the answer is not JSON") from exc


class StaticJwksSource:
    """The keys an operator wrote into config. It opens no socket and never expires.

    A key rotation at the provider needs a config change on this path. That cost is the
    reason it is the alternative rather than the default.
    """

    def __init__(self, document: object) -> None:
        self._keys = key_set_from_jwks(document)

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    async def key_for(self, key_id: str) -> rsa.RSAPublicKey | None:
        return self._keys.get(key_id)


class HttpJwksSource:
    """The keys the provider publishes, with a TTL cache and a rate-limited refresh.

    Two rules decide every answer:

    * An expired cache with no successful refresh answers nothing. A source that answered
      from a stale cache forever would keep accepting tokens signed by a revoked key.
    * An unknown key id costs at most one fetch per window, and a failed fetch costs the
      window as well.
    """

    def __init__(
        self,
        url: str,
        *,
        fetch: Callable[[str], Awaitable[object]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = JWKS_TTL_S,
        min_refresh_interval_s: float = MIN_REFRESH_INTERVAL_S,
        log: Any = _default_log,
    ) -> None:
        self._url = url
        self._fetch = fetch if fetch is not None else fetch_jwks_document
        self._clock = clock
        self._ttl_s = ttl_s
        self._min_refresh_interval_s = min_refresh_interval_s
        self._log = log
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched_at: float | None = None
        self._last_attempt_at: float | None = None
        # One lock serializes every lookup. A burst therefore costs one fetch instead of one
        # fetch per request. It also makes the rate limit exact rather than racy. The cost is
        # that a burst waits on one fetch, which is bounded by FETCH_TIMEOUT_S.
        self._lock = asyncio.Lock()

    async def key_for(self, key_id: str) -> rsa.RSAPublicKey | None:
        async with self._lock:
            if self._cache_is_stale():
                await self._refresh_if_allowed()
                if self._cache_is_stale():
                    # Fail closed. Refusing every assertion is the correct answer for a key
                    # set nobody could confirm.
                    return None
            key = self._keys.get(key_id)
            if key is not None:
                return key
            await self._refresh_if_allowed()
            return self._keys.get(key_id)

    def _cache_is_stale(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at > self._ttl_s

    async def _refresh_if_allowed(self) -> None:
        now = self._clock()
        if (
            self._last_attempt_at is not None
            and now - self._last_attempt_at < self._min_refresh_interval_s
        ):
            return
        self._last_attempt_at = now
        try:
            document = await self._fetch(self._url)
        except Exception as exc:
            # Every failure is one line, and the line never carries a token. The URL is from
            # config, so naming it helps the operator and tells an attacker nothing new.
            self._log.warning("JWKS refresh from {} failed: {}", self._url, exc)
            return
        keys = key_set_from_jwks(document)
        if not keys:
            # An answer with no usable signing key is a failed refresh rather than a
            # rotation. Replacing a working key set with an empty one would turn one bad
            # answer into an outage for every operator.
            self._log.warning("JWKS from {} carried no usable RS256 signing key", self._url)
            return
        self._keys = keys
        self._fetched_at = self._clock()


__all__ = [
    "FETCH_TIMEOUT_S",
    "JWKS_TTL_S",
    "MAX_JWKS_BYTES",
    "MIN_REFRESH_INTERVAL_S",
    "HttpJwksSource",
    "JwksFetchError",
    "JwksSource",
    "StaticJwksSource",
    "fetch_jwks_document",
    "guard_jwks_target",
]
